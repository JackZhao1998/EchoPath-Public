from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


VISUAL_GROUNDING_TARGET_KEYS = (
    "visual_grounding_targets",
    "cv_grounding_targets",
    "grounding_targets",
)
VISUAL_GROUNDING_SINGLE_TARGET_KEYS = (
    "visual_grounding_target",
    "cv_grounding_target",
    "grounding_target",
)
REFERENCE_IMAGE_PATH_KEYS = (
    "reference_image_path",
    "reference_path",
    "image_path",
    "path",
)
TARGET_BBOX_KEYS = (
    "target_bbox",
    "target_box",
    "bbox",
    "box",
)
HIGHLIGHT_BBOX_KEYS = (
    "highlight_bbox",
    "highlight_box",
    "red_box",
    "red_bbox",
)
DEFAULT_VISUAL_GROUNDING_THRESHOLD = 0.78
DEFAULT_MIN_SCALE = 0.5
DEFAULT_MAX_SCALE = 2.0
DEFAULT_SCALE_STEP = 0.05

# Narrow band used for the fast first pass. Replay normally runs on the same
# display that recorded the memory, so the true template scale is 1.0; the wide
# DEFAULT_MIN_SCALE..DEFAULT_MAX_SCALE sweep only earns its cost when the
# resolution or DPI actually changed, which is handled by a fallback pass.
DEFAULT_NARROW_MIN_SCALE = 0.9
DEFAULT_NARROW_MAX_SCALE = 1.1

# A match at or above this score is treated as decisive, so scale and candidate
# scanning can stop instead of scoring every remaining combination.
DEFAULT_EARLY_EXIT_SCORE = 0.98


@dataclass(frozen=True)
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def as_dict(self) -> Dict[str, int]:
        return {
            "x": int(self.x),
            "y": int(self.y),
            "width": int(self.width),
            "height": int(self.height),
        }

    def clipped(self, width: int, height: int) -> Optional["BoundingBox"]:
        x1 = min(max(0, self.x), max(0, width))
        y1 = min(max(0, self.y), max(0, height))
        x2 = min(max(0, self.right), max(0, width))
        y2 = min(max(0, self.bottom), max(0, height))
        if x2 <= x1 or y2 <= y1:
            return None
        return BoundingBox(x=x1, y=y1, width=x2 - x1, height=y2 - y1)

    def shrunk(self, inset: int) -> "BoundingBox":
        amount = max(0, int(inset))
        if self.width <= amount * 2 or self.height <= amount * 2:
            return self
        return BoundingBox(
            x=self.x + amount,
            y=self.y + amount,
            width=self.width - amount * 2,
            height=self.height - amount * 2,
        )

    def expanded(self, margin: int) -> "BoundingBox":
        amount = max(0, int(margin))
        return BoundingBox(
            x=self.x - amount,
            y=self.y - amount,
            width=self.width + amount * 2,
            height=self.height + amount * 2,
        )


def locate_visual_grounding_target(
    *,
    observation_path: Path,
    next_step: Mapping[str, Any],
    task_payload: Mapping[str, Any],
    working_directory: Path,
) -> Optional[Dict[str, Any]]:
    """Locate a planned GUI target from saved visual-memory reference images.

    The expected reference is an image with the desired UI target highlighted by a red
    rectangle. The red rectangle is removed from the matching template so the stored
    memory can be matched against a normal live screenshot.
    """

    targets = _configured_visual_targets(task_payload)
    if not targets:
        return None

    try:
        cv2 = _require_cv2()
    except RuntimeError as error:
        return _failed_result(
            attempted=True,
            reason=str(error),
            target_count=len(targets),
        )

    resolved_observation_path = _resolve_path(observation_path, working_directory)
    observation_image = cv2.imread(str(resolved_observation_path), cv2.IMREAD_COLOR)
    if observation_image is None:
        return _failed_result(
            attempted=True,
            reason=f"Observation image could not be read: {resolved_observation_path}",
            target_count=len(targets),
        )

    started_at = time.perf_counter()
    candidates_evaluated = 0
    candidate_early_exit = False
    best_result: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    ranked_targets = _rank_visual_targets(targets, next_step)
    for target in ranked_targets:
        try:
            reference_path = _target_reference_path(target, working_directory)
            reference_image = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
            if reference_image is None:
                errors.append(f"Reference image could not be read: {reference_path}")
                continue

            prepared = _prepare_reference_template(
                target,
                reference_image=reference_image,
                reference_path=reference_path,
                cv2=cv2,
            )
            if prepared is None:
                errors.append(f"No target/highlight box found in {reference_path}")
                continue

            match = _match_template(
                observation_image=observation_image,
                template_image=prepared["template_image"],
                target=target,
                task_payload=task_payload,
                cv2=cv2,
            )
            result = _build_grounding_result(
                match=match,
                target=target,
                reference_path=reference_path,
                prepared=prepared,
                task_payload=task_payload,
            )
            candidates_evaluated += 1
            if _visual_grounding_result_is_better(result, best_result):
                best_result = result

            # Crop candidates accumulate on a node as it is reused, so scoring
            # every one makes a well-used memory replay slower. A decisive hit
            # is enough.
            if bool(result.get("located")) and float(
                result.get("confidence", 0.0) or 0.0
            ) >= _configured_early_exit_score(target, task_payload):
                candidate_early_exit = True
                break
        except Exception as error:  # pragma: no cover - defensive per-target isolation
            errors.append(str(error))

    grounding_metrics = {
        "candidate_count": len(targets),
        "candidates_evaluated": candidates_evaluated,
        "candidate_early_exit": candidate_early_exit,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
    }

    if best_result is None:
        failure = _failed_result(
            attempted=True,
            reason="No visual grounding target could be prepared."
            + (f" Errors: {'; '.join(errors[:3])}" if errors else ""),
            target_count=len(targets),
        )
        failure["metrics"] = grounding_metrics
        return failure

    if not best_result.get("located") and errors:
        best_result["errors"] = errors[:3]
    match_metrics = best_result.get("match")
    if isinstance(match_metrics, Mapping):
        grounding_metrics["scales_available"] = match_metrics.get("scales_available")
        grounding_metrics["scales_evaluated"] = match_metrics.get("scales_evaluated")
    best_result["metrics"] = grounding_metrics
    return best_result


def _visual_grounding_result_is_better(
    candidate: Mapping[str, Any],
    current: Optional[Mapping[str, Any]],
) -> bool:
    if current is None:
        return True
    candidate_located = bool(candidate.get("located"))
    current_located = bool(current.get("located"))
    if candidate_located != current_located:
        return candidate_located
    return float(candidate.get("confidence", 0.0) or 0.0) > float(
        current.get("confidence", 0.0) or 0.0
    )


def detect_red_highlight_boxes(
    image_path: Path,
    *,
    working_directory: Optional[Path] = None,
    min_area: int = 100,
    max_fill_ratio: float = 0.45,
) -> List[Dict[str, Any]]:
    cv2 = _require_cv2()
    resolved_path = _resolve_path(image_path, working_directory or Path.cwd())
    image = cv2.imread(str(resolved_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Image could not be read: {resolved_path}")
    mask = _red_mask(image, cv2=cv2)
    boxes = _red_highlight_boxes_from_mask(
        mask,
        cv2=cv2,
        min_area=min_area,
        max_fill_ratio=max_fill_ratio,
    )
    return [
        {
            "bbox": box["bbox"].as_dict(),
            "area": box["bbox"].area,
            "red_fill_ratio": box["red_fill_ratio"],
        }
        for box in boxes
    ]


def visual_grounding_allows_model_fallback(task_payload: Mapping[str, Any]) -> bool:
    if bool(task_payload.get("visual_grounding_only") or task_payload.get("cv_grounding_only")):
        return False
    for key in (
        "visual_grounding_model_fallback",
        "cv_grounding_model_fallback",
        "grounding_model_fallback",
    ):
        if key in task_payload:
            return bool(task_payload.get(key))
    return True


def _configured_visual_targets(task_payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if task_payload.get("visual_grounding_enabled") is False:
        return []
    if task_payload.get("cv_grounding_enabled") is False:
        return []

    targets: List[Dict[str, Any]] = []
    for key in VISUAL_GROUNDING_TARGET_KEYS:
        value = task_payload.get(key)
        if isinstance(value, list):
            targets.extend(dict(item) for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            targets.append(dict(value))

    for key in VISUAL_GROUNDING_SINGLE_TARGET_KEYS:
        value = task_payload.get(key)
        if isinstance(value, Mapping):
            targets.append(dict(value))

    if any(key in task_payload for key in REFERENCE_IMAGE_PATH_KEYS):
        targets.append(dict(task_payload))

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        signature = repr(sorted((str(key), str(value)) for key, value in target.items()))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(target)
    return deduped


def _rank_visual_targets(
    targets: Sequence[Mapping[str, Any]],
    next_step: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for _, item in sorted(
            ((_target_step_score(target, next_step), dict(target)) for target in targets),
            key=lambda pair: pair[0],
            reverse=True,
        )
    ]


def _target_step_score(target: Mapping[str, Any], next_step: Mapping[str, Any]) -> float:
    requested_ids = [
        str(next_step.get(key, "")).strip()
        for key in ("visual_target_id", "memory_target_id", "grounding_target_id", "target_id")
    ]
    requested_ids = [item for item in requested_ids if item]
    target_ids = [
        str(target.get(key, "")).strip()
        for key in ("id", "name", "label", "target_id", "memory_target_id")
    ]
    target_ids = [item for item in target_ids if item]
    if requested_ids and target_ids:
        if any(
            _normalize_text(requested) == _normalize_text(target_id)
            for requested in requested_ids
            for target_id in target_ids
        ):
            return 10.0
        return -10.0

    step_terms = [
        str(next_step.get(key, "")).strip()
        for key in ("target_description", "target_text", "description", "text")
    ]
    target_terms = [
        str(target.get(key, "")).strip()
        for key in ("target_description", "target_text", "description", "label", "name")
    ]
    step_terms = [item for item in step_terms if item]
    target_terms = [item for item in target_terms if item]
    if not step_terms or not target_terms:
        return 0.0

    best = 0.0
    for step_term in step_terms:
        normalized_step = _normalize_text(step_term)
        step_tokens = set(normalized_step.split())
        for target_term in target_terms:
            normalized_target = _normalize_text(target_term)
            if not normalized_step or not normalized_target:
                continue
            if normalized_step == normalized_target:
                best = max(best, 6.0)
            elif normalized_step in normalized_target or normalized_target in normalized_step:
                best = max(best, 4.0)
            else:
                target_tokens = set(normalized_target.split())
                if step_tokens and target_tokens:
                    overlap = len(step_tokens & target_tokens) / max(
                        len(step_tokens),
                        len(target_tokens),
                    )
                    best = max(best, overlap)
    return best


def _target_reference_path(target: Mapping[str, Any], working_directory: Path) -> Path:
    for key in REFERENCE_IMAGE_PATH_KEYS:
        value = str(target.get(key, "")).strip()
        if value:
            return _resolve_path(value, working_directory)
    raise ValueError("Visual grounding target requires reference_image_path.")


def _prepare_reference_template(
    target: Mapping[str, Any],
    *,
    reference_image: Any,
    reference_path: Path,
    cv2: Any,
) -> Optional[Dict[str, Any]]:
    image_height, image_width = reference_image.shape[:2]
    explicit_target_bbox = _first_bbox(target, TARGET_BBOX_KEYS)
    highlight_bbox = _first_bbox(target, HIGHLIGHT_BBOX_KEYS)
    detected_highlight: Optional[Dict[str, Any]] = None

    if explicit_target_bbox is not None:
        template_bbox = explicit_target_bbox
        if bool(target.get("bbox_includes_highlight") or target.get("target_bbox_is_highlight")):
            border_px = _highlight_border_px(target, explicit_target_bbox)
            template_bbox = explicit_target_bbox.shrunk(border_px)
            highlight_bbox = explicit_target_bbox
    else:
        if highlight_bbox is None:
            mask = _red_mask(reference_image, cv2=cv2)
            boxes = _red_highlight_boxes_from_mask(
                mask,
                cv2=cv2,
                min_area=max(100, int(target.get("min_highlight_area", 100) or 100)),
                max_fill_ratio=float(target.get("max_highlight_fill_ratio", 0.45) or 0.45),
            )
            if not boxes:
                return None
            highlight_index = max(0, int(target.get("highlight_index", 0) or 0))
            detected_highlight = boxes[min(highlight_index, len(boxes) - 1)]
            highlight_bbox = detected_highlight["bbox"]
        border_px = _highlight_border_px(target, highlight_bbox)
        template_bbox = highlight_bbox.shrunk(border_px)

    margin = int(target.get("template_margin_px", 0) or 0)
    if margin:
        template_bbox = template_bbox.expanded(margin)

    clipped_template_bbox = template_bbox.clipped(image_width, image_height)
    if clipped_template_bbox is None or clipped_template_bbox.area <= 0:
        return None

    template_image = reference_image[
        clipped_template_bbox.y : clipped_template_bbox.bottom,
        clipped_template_bbox.x : clipped_template_bbox.right,
    ].copy()
    if template_image.size == 0:
        return None

    return {
        "template_image": template_image,
        "template_bbox": clipped_template_bbox,
        "highlight_bbox": highlight_bbox.clipped(image_width, image_height).as_dict()
        if highlight_bbox is not None and highlight_bbox.clipped(image_width, image_height)
        else None,
        "detected_highlight": (
            {
                "bbox": detected_highlight["bbox"].as_dict(),
                "red_fill_ratio": detected_highlight["red_fill_ratio"],
            }
            if detected_highlight is not None
            else None
        ),
        "reference_size": {"width": image_width, "height": image_height},
        "reference_path": str(reference_path),
    }


def _match_template(
    *,
    observation_image: Any,
    template_image: Any,
    target: Mapping[str, Any],
    task_payload: Mapping[str, Any],
    cv2: Any,
) -> Dict[str, Any]:
    observation_height, observation_width = observation_image.shape[:2]
    search_bbox = _first_bbox(target, ("search_region", "search_bbox", "region"))
    if search_bbox is None:
        search_bbox = _first_bbox(
            task_payload,
            ("visual_grounding_search_region", "cv_grounding_search_region"),
        )
    clipped_search_bbox = (
        search_bbox.clipped(observation_width, observation_height)
        if search_bbox is not None
        else BoundingBox(x=0, y=0, width=observation_width, height=observation_height)
    )
    if clipped_search_bbox is None:
        clipped_search_bbox = BoundingBox(
            x=0,
            y=0,
            width=observation_width,
            height=observation_height,
        )

    search_image = observation_image[
        clipped_search_bbox.y : clipped_search_bbox.bottom,
        clipped_search_bbox.x : clipped_search_bbox.right,
    ]
    match_mode = str(
        target.get("match_mode")
        or task_payload.get("visual_grounding_match_mode")
        or "gray"
    )
    search_prepared = _prepare_image_for_matching(
        search_image,
        mode=match_mode,
        cv2=cv2,
    )

    scales = list(_configured_scales(target, task_payload))
    if not scales:
        scales = [1.0]

    early_exit_score = _configured_early_exit_score(target, task_payload)
    minimum_margin = _configured_min_match_margin(target, task_payload)

    started_at = time.perf_counter()
    scales_evaluated = 0
    early_exit = False
    best: Optional[Dict[str, Any]] = None
    template_height, template_width = template_image.shape[:2]
    for scale in scales:
        scaled_width = max(1, int(round(template_width * scale)))
        scaled_height = max(1, int(round(template_height * scale)))
        if scaled_width > clipped_search_bbox.width or scaled_height > clipped_search_bbox.height:
            continue

        scaled_template = cv2.resize(
            template_image,
            (scaled_width, scaled_height),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR,
        )
        template_prepared = _prepare_image_for_matching(
            scaled_template,
            mode=match_mode,
            cv2=cv2,
        )
        method, score, location, second_score = _score_template_match(
            search_prepared,
            template_prepared,
            cv2=cv2,
        )
        candidate = {
            "score": score,
            "second_score": second_score,
            "margin": max(0.0, score - second_score),
            "scale": scale,
            "method": method,
            "bbox": BoundingBox(
                x=clipped_search_bbox.x + int(location[0]),
                y=clipped_search_bbox.y + int(location[1]),
                width=scaled_width,
                height=scaled_height,
            ),
        }
        scales_evaluated += 1
        if best is None or score > best["score"]:
            best = candidate

        # Scales are ordered nearest-to-1.0 first, so a decisive hit normally
        # lands on the first iteration and the rest of the sweep is wasted work.
        if score >= early_exit_score and candidate["margin"] >= minimum_margin:
            early_exit = True
            break

    metrics = {
        "scales_available": len(scales),
        "scales_evaluated": scales_evaluated,
        "early_exit": early_exit,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
    }

    if best is None:
        return {
            "score": 0.0,
            "second_score": 0.0,
            "margin": 0.0,
            "scale": None,
            "method": None,
            "bbox": None,
            "search_region": clipped_search_bbox.as_dict(),
            "metrics": metrics,
        }

    best["search_region"] = clipped_search_bbox.as_dict()
    best["metrics"] = metrics
    return best


def _build_grounding_result(
    *,
    match: Mapping[str, Any],
    target: Mapping[str, Any],
    reference_path: Path,
    prepared: Mapping[str, Any],
    task_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    threshold = float(
        target.get(
            "threshold",
            task_payload.get(
                "visual_grounding_threshold",
                task_payload.get(
                    "cv_grounding_threshold",
                    DEFAULT_VISUAL_GROUNDING_THRESHOLD,
                ),
            ),
        )
        or DEFAULT_VISUAL_GROUNDING_THRESHOLD
    )
    confidence = _clamp_float(float(match.get("score", 0.0)), 0.0, 1.0)
    second_score = _clamp_float(float(match.get("second_score", 0.0)), 0.0, 1.0)
    match_margin = max(0.0, confidence - second_score)
    minimum_margin = _configured_min_match_margin(target, task_payload)
    bbox = match.get("bbox")
    located = (
        isinstance(bbox, BoundingBox)
        and confidence >= threshold
        and match_margin >= minimum_margin
    )
    click_ratio = _click_ratio(target)

    x: Optional[int] = None
    y: Optional[int] = None
    match_bbox_payload = None
    if isinstance(bbox, BoundingBox):
        match_bbox_payload = bbox.as_dict()
        x = int(round(bbox.x + bbox.width * click_ratio[0]))
        y = int(round(bbox.y + bbox.height * click_ratio[1]))

    target_label = _target_label(target)
    if located:
        reason = (
            f"Visual memory target"
            f"{f' {target_label!r}' if target_label else ''} matched at score {confidence:.3f}."
        )
    elif confidence < threshold:
        reason = (
            f"Best visual memory match"
            f"{f' for {target_label!r}' if target_label else ''} scored {confidence:.3f}, "
            f"below threshold {threshold:.3f}."
        )
    else:
        reason = (
            f"Best visual memory match"
            f"{f' for {target_label!r}' if target_label else ''} was ambiguous: "
            f"margin {match_margin:.3f} was below {minimum_margin:.3f}."
        )

    return {
        "attempted": True,
        "located": located,
        "x": x if located else None,
        "y": y if located else None,
        "confidence": confidence,
        "reason": reason,
        "source": "cv:visual_memory",
        "reference_image_path": str(reference_path),
        "target_label": target_label,
        "target_bbox": prepared["template_bbox"].as_dict()
        if isinstance(prepared.get("template_bbox"), BoundingBox)
        else None,
        "highlight_bbox": prepared.get("highlight_bbox"),
        "bbox": match_bbox_payload,
        "match": {
            "score": confidence,
            "second_score": second_score,
            "margin": match_margin,
            "minimum_margin": minimum_margin,
            "scale": match.get("scale"),
            "method": match.get("method"),
            "threshold": threshold,
            "search_region": match.get("search_region"),
            "scales_available": (match.get("metrics") or {}).get("scales_available"),
            "scales_evaluated": (match.get("metrics") or {}).get("scales_evaluated"),
            "scale_early_exit": (match.get("metrics") or {}).get("early_exit"),
            "match_elapsed_ms": (match.get("metrics") or {}).get("elapsed_ms"),
        },
    }


def _red_mask(image: Any, *, cv2: Any) -> Any:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_red_a = (0, 70, 80)
    upper_red_a = (12, 255, 255)
    lower_red_b = (170, 70, 80)
    upper_red_b = (180, 255, 255)
    return cv2.bitwise_or(
        cv2.inRange(hsv, lower_red_a, upper_red_a),
        cv2.inRange(hsv, lower_red_b, upper_red_b),
    )


def _red_highlight_boxes_from_mask(
    mask: Any,
    *,
    cv2: Any,
    min_area: int,
    max_fill_ratio: float,
) -> List[Dict[str, Any]]:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: List[Dict[str, Any]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        bbox = BoundingBox(x=int(x), y=int(y), width=int(width), height=int(height))
        if bbox.area < int(min_area) or bbox.width < 8 or bbox.height < 8:
            continue
        red_pixels = int(cv2.countNonZero(mask[y : y + height, x : x + width]))
        fill_ratio = red_pixels / max(1, bbox.area)
        if fill_ratio > float(max_fill_ratio):
            continue
        candidates.append(
            {
                "bbox": bbox,
                "red_fill_ratio": fill_ratio,
                "score": bbox.area * max(0.0, 1.0 - fill_ratio),
            }
        )

    return sorted(candidates, key=lambda item: item["score"], reverse=True)


def _prepare_image_for_matching(image: Any, *, mode: str, cv2: Any) -> Any:
    normalized_mode = str(mode or "gray").strip().lower()
    if normalized_mode == "color":
        return image

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if normalized_mode in {"edge", "edges", "canny"}:
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.Canny(blurred, 50, 150)
    return gray


def _score_template_match(
    search_image: Any,
    template_image: Any,
    *,
    cv2: Any,
) -> Tuple[str, float, Tuple[int, int], float]:
    _, stddev = cv2.meanStdDev(template_image)
    std = float(stddev.max()) if stddev is not None else 0.0
    if std < 1.0:
        result = cv2.matchTemplate(search_image, template_image, cv2.TM_SQDIFF_NORMED)
        min_value, _, min_location, _ = cv2.minMaxLoc(result)
        score = 1.0 - float(min_value)
        second_value = _second_template_match_value(
            result,
            location=min_location,
            template_shape=template_image.shape[:2],
            fill_value=1.0,
            cv2=cv2,
            use_minimum=True,
        )
        second_score = 1.0 - second_value if second_value is not None else 0.0
        return (
            "TM_SQDIFF_NORMED",
            _clamp_float(score, 0.0, 1.0),
            min_location,
            _clamp_float(second_score, 0.0, 1.0),
        )

    result = cv2.matchTemplate(search_image, template_image, cv2.TM_CCOEFF_NORMED)
    _, max_value, _, max_location = cv2.minMaxLoc(result)
    second_value = _second_template_match_value(
        result,
        location=max_location,
        template_shape=template_image.shape[:2],
        fill_value=-1.0,
        cv2=cv2,
        use_minimum=False,
    )
    return (
        "TM_CCOEFF_NORMED",
        _clamp_float(float(max_value), 0.0, 1.0),
        max_location,
        _clamp_float(float(second_value or 0.0), 0.0, 1.0),
    )


def _second_template_match_value(
    result: Any,
    *,
    location: Tuple[int, int],
    template_shape: Tuple[int, int],
    fill_value: float,
    cv2: Any,
    use_minimum: bool,
) -> Optional[float]:
    if result.size <= 1:
        return None
    suppressed = result.copy()
    result_height, result_width = suppressed.shape[:2]
    template_height, template_width = template_shape
    center_x = int(location[0])
    center_y = int(location[1])
    left = max(0, center_x - max(1, template_width // 2))
    top = max(0, center_y - max(1, template_height // 2))
    right = min(result_width, center_x + max(1, template_width // 2) + 1)
    bottom = min(result_height, center_y + max(1, template_height // 2) + 1)
    suppressed[top:bottom, left:right] = fill_value
    min_value, max_value, _, _ = cv2.minMaxLoc(suppressed)
    candidate = float(min_value if use_minimum else max_value)
    if candidate == float(fill_value):
        return None
    return candidate


def _configured_min_match_margin(
    target: Mapping[str, Any],
    task_payload: Mapping[str, Any],
) -> float:
    return max(
        0.0,
        float(
            target.get(
                "min_match_margin",
                task_payload.get("visual_grounding_min_match_margin", 0.0),
            )
            or 0.0
        ),
    )


def _configured_early_exit_score(
    target: Mapping[str, Any],
    task_payload: Mapping[str, Any],
) -> float:
    """Score at or above which scanning may stop. Non-positive disables early exit."""

    value = target.get(
        "early_exit_score",
        task_payload.get(
            "visual_grounding_early_exit_score",
            DEFAULT_EARLY_EXIT_SCORE,
        ),
    )
    try:
        score = float(value)
    except (TypeError, ValueError):
        return DEFAULT_EARLY_EXIT_SCORE
    if score <= 0:
        return math.inf
    return score


def _configured_scales(
    target: Mapping[str, Any],
    task_payload: Mapping[str, Any],
) -> Iterable[float]:
    explicit_scales = target.get(
        "scales",
        task_payload.get(
            "visual_grounding_scales",
            task_payload.get("cv_grounding_scales"),
        ),
    )
    if isinstance(explicit_scales, list):
        values = []
        for item in explicit_scales:
            try:
                scale = float(item)
            except (TypeError, ValueError):
                continue
            if scale > 0:
                values.append(scale)
        return sorted(set(values), key=lambda value: (abs(value - 1.0), value))

    min_scale = float(
        target.get(
            "min_scale",
            task_payload.get("visual_grounding_min_scale", DEFAULT_MIN_SCALE),
        )
        or DEFAULT_MIN_SCALE
    )
    max_scale = float(
        target.get(
            "max_scale",
            task_payload.get("visual_grounding_max_scale", DEFAULT_MAX_SCALE),
        )
        or DEFAULT_MAX_SCALE
    )
    step = float(
        target.get(
            "scale_step",
            task_payload.get("visual_grounding_scale_step", DEFAULT_SCALE_STEP),
        )
        or DEFAULT_SCALE_STEP
    )
    if min_scale <= 0 or max_scale <= 0 or step <= 0:
        return [1.0]
    if max_scale < min_scale:
        min_scale, max_scale = max_scale, min_scale

    scale_count = int(math.floor((max_scale - min_scale) / step)) + 1
    scales = [round(min_scale + index * step, 4) for index in range(scale_count + 1)]
    scales.append(1.0)
    return sorted(
        {scale for scale in scales if min_scale <= scale <= max_scale and scale > 0},
        key=lambda value: (abs(value - 1.0), value),
    )


def _first_bbox(payload: Mapping[str, Any], keys: Sequence[str]) -> Optional[BoundingBox]:
    for key in keys:
        if key not in payload:
            continue
        bbox = _coerce_bbox(payload.get(key))
        if bbox is not None:
            return bbox
    return None


def _coerce_bbox(value: Any) -> Optional[BoundingBox]:
    if isinstance(value, Mapping):
        if all(key in value for key in ("x", "y", "width", "height")):
            return BoundingBox(
                x=int(round(float(value["x"]))),
                y=int(round(float(value["y"]))),
                width=int(round(float(value["width"]))),
                height=int(round(float(value["height"]))),
            )
        if all(key in value for key in ("left", "top", "right", "bottom")):
            left = int(round(float(value["left"])))
            top = int(round(float(value["top"])))
            right = int(round(float(value["right"])))
            bottom = int(round(float(value["bottom"])))
            return BoundingBox(x=left, y=top, width=right - left, height=bottom - top)
        if all(key in value for key in ("x1", "y1", "x2", "y2")):
            x1 = int(round(float(value["x1"])))
            y1 = int(round(float(value["y1"])))
            x2 = int(round(float(value["x2"])))
            y2 = int(round(float(value["y2"])))
            return BoundingBox(x=x1, y=y1, width=x2 - x1, height=y2 - y1)

    if isinstance(value, (list, tuple)) and len(value) == 4:
        x, y, width, height = value
        return BoundingBox(
            x=int(round(float(x))),
            y=int(round(float(y))),
            width=int(round(float(width))),
            height=int(round(float(height))),
        )
    return None


def _highlight_border_px(target: Mapping[str, Any], bbox: BoundingBox) -> int:
    if "red_border_px" in target:
        return max(0, int(target.get("red_border_px") or 0))
    if "highlight_border_px" in target:
        return max(0, int(target.get("highlight_border_px") or 0))
    return max(2, min(12, int(round(min(bbox.width, bbox.height) * 0.06))))


def _click_ratio(target: Mapping[str, Any]) -> Tuple[float, float]:
    value = target.get("click_offset", target.get("click_ratio", target.get("click_point")))
    if isinstance(value, Mapping):
        x_value = value.get("x", value.get("rx", value.get("left", 0.5)))
        y_value = value.get("y", value.get("ry", value.get("top", 0.5)))
        return (_ratio_value(x_value), _ratio_value(y_value))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (_ratio_value(value[0]), _ratio_value(value[1]))
    return (0.5, 0.5)


def _ratio_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if 0.0 <= number <= 1.0:
        return number
    return 0.5


def _target_label(target: Mapping[str, Any]) -> Optional[str]:
    for key in ("id", "name", "label", "target_description", "target_text"):
        text = str(target.get(key, "")).strip()
        if text:
            return text
    return None


def _failed_result(*, attempted: bool, reason: str, target_count: int) -> Dict[str, Any]:
    return {
        "attempted": attempted,
        "located": False,
        "x": None,
        "y": None,
        "confidence": 0.0,
        "reason": reason,
        "source": "cv:visual_memory",
        "target_count": target_count,
    }


def _resolve_path(value: Any, working_directory: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(working_directory) / path).resolve()


def _normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", str(value).casefold()).split())


def _clamp_float(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _require_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            "OpenCV and NumPy are required for visual memory grounding. "
            "Install opencv-python and numpy to enable the CV grounding fast path."
        ) from error
    return cv2
