"""Pick a replay click target by its on-screen text before asking the vision model.

When a learned step's saved crop no longer matches, the next rung has been the
vision model: several seconds and thousands of tokens per step. Most replayed
clicks land on something with words on it -- a field label, a link, a menu
entry -- so this rung reads the screen with local OCR and asks the Jev decision
model (PR #1) which line the step meant. Jev returns a calibrated probability;
only a confident answer is clicked, anything else falls through to vision.

Off unless desktop.replay_decision_model is true. It needs an OpenRouter key,
and the OCR text of the current screen is sent to OpenRouter with the question.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from PIL import Image

MAX_OPTIONS = 120


def decision_model_settings(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    settings = payload.get("memory_replay_decision_model")
    if not isinstance(settings, Mapping) or not settings.get("enabled"):
        return None
    return dict(settings)


def load_api_key(settings: Mapping[str, Any], working_directory: Path) -> str:
    """The OpenRouter key from the named environment variable, else credentials/api_keys.yaml."""
    key = os.environ.get(str(settings.get("api_key_env") or "OPENROUTER_API_KEY"), "").strip()
    if key:
        return key
    from .jev_client import _load_key

    path = settings.get("credentials_path") or Path(working_directory) / "credentials" / "api_keys.yaml"
    return _load_key(Path(path))


def _reference_text(visual_binding: Mapping[str, Any], working_directory: Path) -> str:
    """Words printed on the saved crops, when they have any (blank crops give nothing)."""
    from .ocr_extractor import read_screen

    paths = [visual_binding.get("reference_image_path")]
    for candidate in visual_binding.get("reference_candidates") or []:
        if isinstance(candidate, Mapping):
            paths.append(candidate.get("reference_image_path"))
    words: List[str] = []
    for raw in paths[:4]:
        if not raw:
            continue
        path = Path(str(raw))
        path = path if path.is_absolute() else Path(working_directory) / path
        try:
            with Image.open(path) as crop:
                text = " ".join(r.text for r in read_screen(crop.convert("RGB")).regions)
        except Exception:
            continue
        if text and text not in words:
            words.append(text)
    return " / ".join(words)[:200]


def build_question(
    *,
    goal: str,
    stored_action: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    iteration: int,
    image_size: Sequence[int],
    reference_text: str,
) -> str:
    parts = [f"Task: {goal}", f"This is step {iteration} of {len(actions)} in a recorded procedure."]
    x, y = stored_action.get("x"), stored_action.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and image_size[0] and image_size[1]:
        parts.append(
            f"When recorded, the click was about {round(100 * x / image_size[0])}% from the left and "
            f"{round(100 * y / image_size[1])}% from the top of the screen."
        )
    if stored_action.get("target_description"):
        parts.append(f"The step clicks: {stored_action['target_description']}.")
    if stored_action.get("expected_outcome"):
        parts.append(f"Expected result: {stored_action['expected_outcome']}")
    if reference_text:
        parts.append(f"The saved picture of the target reads: {reference_text!r}.")
    following = actions[iteration] if iteration < len(actions) else None
    if isinstance(following, Mapping) and following.get("action") in ("paste_text", "type", "write") and following.get("text"):
        parts.append(f"The next step types {str(following['text'])[:80]!r} into what this click focuses.")
    parts.append("Which on-screen text is the element this step should click?")
    return "\n".join(parts)


def choose_replay_target(
    *,
    goal: str,
    observation: Mapping[str, Any],
    stored_action: Mapping[str, Any],
    visual_binding: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    iteration: int,
    settings: Mapping[str, Any],
    working_directory: Path,
    client: Any = None,
) -> Dict[str, Any]:
    """Return a grounding dict ({"located": bool, "x", "y", ...}) plus timing and cost."""
    started = time.perf_counter()
    report: Dict[str, Any] = {"located": False, "source": "decision_model"}
    if sys.platform != "darwin":
        report["reason"] = "Local OCR needs macOS Vision."
        return report
    from .jev_client import JevClient, JevError
    from .ocr_extractor import OCRUnavailable, read_screen

    try:
        with Image.open(str(observation.get("path"))) as shot:
            screen = read_screen(shot.convert("RGB"))
        reference_text = _reference_text(visual_binding, working_directory)
    except (OCRUnavailable, OSError, ValueError) as error:
        report["reason"] = f"OCR failed: {error}"
        return report
    report["ocr_ms"] = screen.ocr_ms
    regions = sorted(screen.regions, key=lambda r: (r.y, r.x))[:MAX_OPTIONS]
    if not regions:
        report["reason"] = "No text on screen to choose from."
        return report

    question = build_question(goal=goal, stored_action=stored_action, actions=actions, iteration=iteration,
                              image_size=(screen.width, screen.height), reference_text=reference_text)
    options = {
        r.key: f"{r.text[:110]} (at {round(100 * r.center[0] / screen.width)}% across, "
               f"{round(100 * r.center[1] / screen.height)}% down)"
        for r in regions
    }
    try:
        client = client or JevClient(api_key=load_api_key(settings, working_directory))
        decision = client.choose(instructions=question, options=options,
                                 state={"note": "options are on-screen text, listed top to bottom"})
    except JevError as error:
        report["reason"] = str(error)
        return report

    minimum = float(settings.get("min_confidence", 0.9))
    picked = next((r for r in regions if r.key == decision.choice), None)
    report.update(
        choice_text=picked.text if picked else None,
        confidence=decision.confidence,
        cost=decision.cost,
        decision_ms=decision.latency_ms,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        question=question,
    )
    if picked is None or decision.confidence < minimum:
        report["reason"] = (f"Decision model was not confident enough ({decision.confidence:.2f} < {minimum:.2f})."
                            if picked else "Decision model chose no listed option.")
        return report
    # OCR ran on the observation image, so its pixels are the observation's coordinates.
    scale_x = scale_y = 1.0
    image_size = observation.get("image_size")
    if isinstance(image_size, Mapping) and image_size.get("width") and image_size.get("height"):
        scale_x = float(image_size["width"]) / screen.width
        scale_y = float(image_size["height"]) / screen.height
    x, y = picked.center
    report.update(located=True, x=int(round(x * scale_x)), y=int(round(y * scale_y)),
                  target_role="memory_target", reason=f"Chose {picked.text!r} at {decision.confidence:.2f}.")
    return report
