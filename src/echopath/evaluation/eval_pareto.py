"""Two families of task, and the three ways of resolving a click across both.

Replay finds a click target by matching a crop saved when the workflow was
demonstrated. That works while the target looks the same and breaks when the
target *is* the content. A decision model reading the screen as text covers the
second case -- unless the target has no text, in which case it is not even among
the options.

So the two mechanisms fail on different families:

  changing   open the first search result. Different every query, so the crop
             misses. The title is text, so the decision model reads it.
  wordless   zoom a map in. The control is a single glyph that OCR discards, so
             the decision model is offered street names and nothing else. The
             control never moves, so the crop hits it exactly.

Run both families under all three conditions and neither mechanism alone covers
the set. That is the claim this measures -- not that either is better, but that
their failures do not overlap.

Every step is scored by its own effect on the URL, never by matching the text of
the element chosen: a URL that gained a zoom level, a host that changed. An
element can be picked for the wrong reason and still read plausibly; it cannot
change the URL for the wrong reason.

    python -m echopath.evaluation.eval_pareto --pilot
    python -m echopath.evaluation.eval_pareto
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pyautogui
from PIL import Image

from echopath.jev_client import JevClient, JevError
from echopath.ocr_extractor import Row, capture, front_window_region, read_screen

from echopath.project_paths import project_root

ROOT = project_root()
EVAL = ROOT / "local_data" / "pareto"
RESULTS = EVAL / "results.jsonl"
PROGRESS = EVAL / "progress.json"
CROPS = EVAL / "crops"

BROWSER = "Google Chrome"
TEMPLATE_THRESHOLD = 0.74
JEV_FLOOR = 0.40
CONDITIONS = ("echopath", "jev-only", "echopath+jev")
# Half the width of the patch saved around a demonstrated click. A person points
# at a control, not at a text box, so the crop is a fixed window around the point
# rather than whatever bounding box OCR happened to draw.
CROP_RADIUS = 26


@dataclass
class Task:
    name: str
    family: str                       # "changing" or "wordless"
    url_for: Callable[[str], str]
    question: str                     # asked of whichever model must decide
    verify: Callable[[str, str, str], bool]   # (variation, before, after)
    # How the demonstrator points at the target: a text predicate over OCR rows,
    # or a fixed screen point for controls that carry no text at all.
    locate_row: Optional[Callable[[str, List[Row]], Optional[Row]]] = None
    demo_point: Optional[Tuple[int, int]] = None
    variations: List[str] = field(default_factory=list)
    skip_above: float = 0.18


@dataclass
class Result:
    task: str
    family: str
    variation: str
    condition: str
    completed: bool = False
    latency_ms: int = 0
    tokens: int = 0
    cost: float = 0.0
    crop_score: Optional[float] = None
    jev_confidence: Optional[float] = None
    used_crop: bool = False
    used_jev: bool = False
    used_vision: bool = False
    picked: str = ""
    note: str = ""


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

_started = False


def osa(script: str) -> str:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True).stdout.strip()


def current_url() -> str:
    return osa(f'tell application "{BROWSER}" to get URL of active tab of front window')


def open_page(url: str) -> None:
    global _started
    if not _started:
        osa(f'tell application "{BROWSER}" to quit')
        time.sleep(2.0)
        subprocess.Popen(["open", "-na", BROWSER, "--args", "--incognito", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(9.0)
        _started = True
    else:
        osa(f'tell application "{BROWSER}" to set URL of active tab of front window to "{url}"')
        time.sleep(5.0)
    osa(f'tell application "{BROWSER}" to activate')
    osa(f'tell application "{BROWSER}" to set bounds of front window to {{0, 25, 1800, 1100}}')
    time.sleep(2.0)


def window_shot() -> Tuple[Image.Image, Tuple[int, int]]:
    shot, _ms, offset = capture(region=front_window_region(BROWSER))
    return shot, offset


def rows_for(task: Task) -> Tuple[List[Row], int]:
    screen = read_screen(region=front_window_region(BROWSER))
    return (screen.rows_below(int(screen.height * task.skip_above), min_regions=1),
            screen.ocr_ms + screen.capture_ms)


def click(point: Tuple[int, int]) -> None:
    pyautogui.moveTo(point[0], point[1], duration=0.15)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(3.5)


# --------------------------------------------------------------------------
# the three resolvers
# --------------------------------------------------------------------------

def by_crop(reference: Optional[Path]) -> Tuple[Optional[Tuple[int, int]], float, int]:
    """Find the demonstrated patch on the live screen."""
    if reference is None or not reference.exists():
        return None, 0.0, 0
    started = time.perf_counter()
    shot, offset = window_shot()
    screen = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(np.array(Image.open(reference).convert("RGB")), cv2.COLOR_RGB2GRAY)
    if template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
        return None, 0.0, int((time.perf_counter() - started) * 1000)
    matched = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(matched)
    ms = int((time.perf_counter() - started) * 1000)
    if score < TEMPLATE_THRESHOLD:
        return None, float(score), ms
    point = (location[0] + template.shape[1] // 2 + offset[0],
             location[1] + template.shape[0] // 2 + offset[1])
    return point, float(score), ms


def by_jev(task: Task, rows: List[Row], client: JevClient):
    offered = rows[:60]
    if not offered:
        return None, None
    options = {r.key: f"position {i+1}: {r.text[:110]}" for i, r in enumerate(offered)}
    decision = client.choose(instructions=task.question, options=options,
                             state={"note": "positions are top-first on screen"})
    picked = next((r for r in offered if r.key == decision.choice), None)
    return (picked.center if picked else None), decision


def by_vision(task: Task, rows: List[Row]) -> Tuple[Optional[Tuple[int, int]], int, int]:
    EVAL.mkdir(parents=True, exist_ok=True)
    path = EVAL / "shot.png"
    shot, _ = window_shot()
    shot.save(path)
    request = {
        "model": "default",
        "input": [{"type": "local_image", "path": str(path)},
                  {"type": "text", "text": task.question + " Reply with only that element's visible text."}],
        "instructions": None, "metadata": {}, "reasoning_effort": "low",
        "temperature": None, "max_output_tokens": None, "extra_body": {},
    }
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bridges" / "codex_cli" / "run_codex_cli.py")],
        input=json.dumps(request), capture_output=True, text=True, timeout=180, cwd=ROOT)
    ms = int((time.perf_counter() - started) * 1000)
    if proc.returncode != 0:
        return None, 0, ms
    payload = json.loads(proc.stdout)
    usage = payload.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    answer = set(re.findall(r"\w+", str(payload.get("output_text") or "").lower()))
    best, score = None, 0
    for row in rows:
        overlap = len(answer & set(re.findall(r"\w+", row.text.lower())))
        if overlap > score:
            best, score = row, overlap
    return (best.center if best else None), tokens, ms


# --------------------------------------------------------------------------
# demonstration -- what a person does once, and what gets recorded
# --------------------------------------------------------------------------

def demonstrate(task: Task, variation: str) -> Tuple[Optional[Path], str]:
    """Perform the task once the way a person would, and keep the evidence.

    EchoPath learns from a run someone drove to success; it does not search for
    a target. So the demonstrator points at the element -- by a text predicate
    where the element has text, by a screen point where it has none -- and the
    crop is cut from around that point before the click.
    """
    open_page(task.url_for(variation))
    rows, _ = rows_for(task)

    if task.demo_point is not None:
        point = task.demo_point
        label = f"point {point}"
    else:
        row = task.locate_row(variation, rows) if task.locate_row else None
        if row is None:
            seen = " | ".join(r.text[:34] for r in rows[:8])
            return None, f"demonstrator could not find the target ({seen})"
        point, label = row.center, row.text[:50]

    shot, offset = window_shot()
    x, y = point[0] - offset[0], point[1] - offset[1]
    box = (max(0, x - CROP_RADIUS), max(0, y - CROP_RADIUS),
           min(shot.width, x + CROP_RADIUS), min(shot.height, y + CROP_RADIUS))
    if box[2] - box[0] < 12 or box[3] - box[1] < 12:
        return None, "target too close to the window edge to crop"

    before = current_url()
    click(point)
    if not task.verify(variation, before, current_url()):
        return None, f"the demonstrated click had no effect ({label})"

    CROPS.mkdir(parents=True, exist_ok=True)
    path = CROPS / f"{task.name}.png"
    shot.crop(box).save(path)
    return path, f"recorded from {label}"


# --------------------------------------------------------------------------
# one replay
# --------------------------------------------------------------------------

def attempt(task: Task, variation: str, condition: str,
            reference: Optional[Path], client: JevClient) -> Result:
    out = Result(task.name, task.family, variation, condition)
    started = time.perf_counter()
    open_page(task.url_for(variation))
    rows, ocr_ms = rows_for(task)
    point: Optional[Tuple[int, int]] = None

    if condition == "jev-only":
        out.latency_ms += ocr_ms
        try:
            point, decision = by_jev(task, rows, client)
        except JevError as error:
            out.note = f"jev error: {error}"
            return out
        if decision is not None:
            out.used_jev = True
            out.cost += decision.cost
            out.jev_confidence = decision.confidence
            out.latency_ms += decision.latency_ms
            out.note = f"jev {decision.confidence:.2f}"
    else:
        point, score, ms = by_crop(reference)
        out.crop_score, out.latency_ms = score, out.latency_ms + ms
        if point is not None:
            out.used_crop = True
            out.note = f"crop {score:.2f}"
        elif condition == "echopath":
            out.latency_ms += ocr_ms
            point, tokens, vms = by_vision(task, rows)
            out.used_vision, out.tokens = True, tokens
            out.latency_ms += vms
            out.note = f"crop {score:.2f} -> vision"
        else:  # echopath+jev
            out.latency_ms += ocr_ms
            try:
                point, decision = by_jev(task, rows, client)
            except JevError as error:
                out.note = f"jev error: {error}"
                return out
            if decision is not None:
                out.used_jev = True
                out.cost += decision.cost
                out.jev_confidence = decision.confidence
                out.latency_ms += decision.latency_ms
                out.note = f"crop {score:.2f} -> jev {decision.confidence:.2f}"
                if decision.confidence < JEV_FLOOR:
                    point, tokens, vms = by_vision(task, rows)
                    out.used_vision, out.tokens = True, tokens
                    out.latency_ms += vms
                    out.note += " -> vision"

    if point is None:
        out.note += " | no target"
        out.latency_ms = int((time.perf_counter() - started) * 1000)
        return out

    out.picked = f"{point}"
    before = current_url()
    click(point)
    out.completed = task.verify(variation, before, current_url())
    out.latency_ms = int((time.perf_counter() - started) * 1000)
    return out


# --------------------------------------------------------------------------
# the two families
# --------------------------------------------------------------------------

# The query words also appear in the search box and the results summary, which
# sit above the results and are not links. Matching on the words alone picked
# "kalman filter | Search | Results 1-20 of 419" and the demonstration clicked a
# label, so the task was dropped before it could run.
# Kept to patterns a query cannot contain. A first pass excluded "filter" for the
# sidebar and so threw away the "Kalman filter" article itself.
_NOT_A_RESULT = re.compile(r"(results?\s+\d|\bsearch\b|advanced|"
                           r"\bnamespace|sort by)", re.I)


def _first_article(variation: str, rows: List[Row]) -> Optional[Row]:
    """The demonstrator's eye: the first result row naming what was searched."""
    words = [w for w in variation.lower().split() if len(w) > 3]
    for row in rows:
        text = row.text.lower()
        if _NOT_A_RESULT.search(row.text):
            continue
        if all(w in text for w in words) and len(row.text) > 8:
            return row
    return None


def _first_story(variation: str, rows: List[Row]) -> Optional[Row]:
    for row in rows:
        if "http" in row.text.lower() and len(row.text) > 30:
            return row
    return None


def build_tasks(pilot: bool) -> List[Task]:
    n = 3 if pilot else 6
    wiki = ["kalman filter", "photosynthesis", "tectonics",
            "antibiotics", "cryptography", "thermodynamics"][:n]
    hn = ["raft consensus", "sqlite internals", "bloom filter",
          "erlang otp", "column store", "vector database"][:n]
    places = ["37.3861,-122.0839", "40.7580,-73.9855", "51.5074,-0.1278",
              "48.8566,2.3522", "35.6762,139.6503", "-33.8688,151.2093"][:n]

    return [
        Task(
            name="wiki-result", family="changing",
            url_for=lambda q: ("https://en.wikipedia.org/w/index.php?search="
                               f"{q.replace(' ', '+')}&fulltext=1"),
            question=("Which row is the first article result in the search results "
                      "list? Ignore navigation, search options and sister projects."),
            verify=lambda q, a, b: ("/wiki/" in b and "search=" not in b
                                    and any(w in b.lower() for w in q.lower().split()
                                            if len(w) > 3)),
            locate_row=_first_article, variations=wiki, skip_above=0.22,
        ),
        Task(
            name="hn-story", family="changing",
            url_for=lambda q: f"https://hn.algolia.com/?q={q.replace(' ', '+')}",
            question=("Which row is the first story result? Ignore the search box, "
                      "filters, tabs and navigation."),
            verify=lambda q, a, b: "hn.algolia.com" not in b and b.startswith("http"),
            locate_row=_first_story, variations=hn, skip_above=0.16,
        ),
        Task(
            name="map-zoom-in", family="wordless",
            url_for=lambda p: f"https://www.google.com/maps/@{p},13z",
            question=("Which row is the control that zooms the map in? It is the "
                      "plus button in the zoom controls."),
            # The zoom level is written into the URL, so the effect is unambiguous.
            verify=lambda p, a, b: _zoom(b) is not None and _zoom(b) > (_zoom(a) or 0),
            demo_point=(1767, 1012), variations=places, skip_above=0.10,
        ),
        Task(
            name="map-zoom-out", family="wordless",
            url_for=lambda p: f"https://www.google.com/maps/@{p},13z",
            question=("Which row is the control that zooms the map out? It is the "
                      "minus button in the zoom controls."),
            verify=lambda p, a, b: (_zoom(b) is not None and _zoom(a) is not None
                                    and _zoom(b) < _zoom(a)),
            demo_point=(1767, 1038), variations=places, skip_above=0.10,
        ),
    ]


def _zoom(url: str) -> Optional[float]:
    match = re.search(r",([\d.]+)z", url or "")
    return float(match.group(1)) if match else None


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def write_progress(total, completed, started_at, label, condition) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps({
        "total": total, "completed": completed, "started_at": started_at,
        "label": label, "condition": condition}), encoding="utf-8")


def start_banner(total: int):
    write_progress(total, 0, time.time(), "starting", "")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "echopath.desktop_status",
             "--progress", str(PROGRESS), "--stale-after", "420"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def stop_banner(banner) -> None:
    if banner is None:
        return
    banner.terminate()
    try:
        banner.wait(timeout=3)
    except subprocess.TimeoutExpired:
        banner.kill()


def summarise(results: List[Result]) -> None:
    print("\n" + "=" * 94)
    print(f"{'condition':15}{'changing':>12}{'wordless':>12}{'overall':>11}"
          f"{'tokens':>10}{'$ cost':>10}{'latency':>11}")
    print("-" * 94)
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if not rows:
            continue
        def rate(family):
            sub = [r for r in rows if r.family == family]
            return f"{sum(r.completed for r in sub)}/{len(sub)}" if sub else "-"
        print(f"{condition:15}{rate('changing'):>12}{rate('wordless'):>12}"
              f"{sum(r.completed for r in rows):>6}/{len(rows):<4}"
              f"{sum(r.tokens for r in rows):>10,}{sum(r.cost for r in rows):>10.5f}"
              f"{sum(r.latency_ms for r in rows)//len(rows):>9}ms")
    print("=" * 94)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()

    tasks = build_tasks(args.pilot)
    client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
    EVAL.mkdir(parents=True, exist_ok=True)
    total = sum(len(t.variations) for t in tasks) * len(CONDITIONS)
    started_at, completed = time.time(), 0
    results: List[Result] = []
    banner = start_banner(total)

    try:
        with RESULTS.open("w", encoding="utf-8") as sink:
            for task in tasks:
                reference, why = demonstrate(task, task.variations[0])
                print(f"[demo] {task.name} ({task.family}): "
                      f"{'ok -- ' + why if reference else 'FAILED -- ' + why}")
                if reference is None:
                    continue
                for variation in task.variations:
                    for condition in CONDITIONS:
                        write_progress(total, completed, started_at,
                                       f"{task.name} / {variation[:18]}", condition)
                        try:
                            result = attempt(task, variation, condition, reference, client)
                        except Exception as error:
                            result = Result(task.name, task.family, variation, condition,
                                            note=f"error: {type(error).__name__}")
                        results.append(result)
                        sink.write(json.dumps(asdict(result)) + "\n")
                        sink.flush()
                        completed += 1
                        mark = "OK " if result.completed else "   "
                        print(f"{mark}{task.name:14}{variation[:16]:17}{condition:14}"
                              f"{result.latency_ms:6}ms {result.tokens:7,}tok  {result.note[:34]}")
        write_progress(total, total, started_at, "done", "")
    finally:
        stop_banner(banner)

    summarise(results)
    print(f"results: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
