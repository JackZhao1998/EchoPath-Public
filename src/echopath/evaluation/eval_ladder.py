"""Four ways to find the element a replayed workflow needs to click.

Every condition performs the same steps -- the workflow is not the variable.
What differs is how the one hard click is resolved when the page has moved on:

  model-only     a vision model reads the screenshot, every time
  jev-only       OCR the window and let a decision model choose, every time
  echopath       the saved crop; a vision model when that fails
  echopath+jev   the saved crop; the decision model when that fails; a vision
                 model only when the decision is not confident

Success is the task's own outcome -- a URL that changed, a cart that grew --
never a string comparison against the chosen text. An earlier version of this
scored "correct" with a regex, and sidebar junk sailed through it; a criterion
that can be satisfied by the wrong element measures nothing.

Note on the model-only baseline: it is charged for grounding each click, not for
planning the workflow from scratch. Planning from nothing measured 19 calls and
148 s for a single task, so this baseline is conservative -- the real cost of
having no memory is higher than what is reported here.
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
EVAL = ROOT / "local_data" / "ladder"
RESULTS = EVAL / "results.jsonl"
PROGRESS = EVAL / "progress.json"
CROPS = EVAL / "crops"

BROWSER = "Google Chrome"
TEMPLATE_THRESHOLD = 0.74
JEV_FLOOR = 0.40          # set from the pilot's calibration curve, not guessed
CONDITIONS = ("model-only", "jev-only", "echopath", "echopath+jev")


@dataclass
class Task:
    """A workflow, its variations, and how to tell whether it worked."""

    name: str
    url_for: Callable[[str], str]
    question: str
    succeeded: Callable[[str, str], bool]   # (url_before, url_after) -> bool
    variations: List[str] = field(default_factory=list)
    skip_above: float = 0.25


@dataclass
class Result:
    task: str
    variation: str
    condition: str
    completed: bool = False
    latency_ms: int = 0
    tokens: int = 0
    cost: float = 0.0
    confidence: Optional[float] = None
    escalated: bool = False
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
        time.sleep(4.5)
    osa(f'tell application "{BROWSER}" to activate')
    osa(f'tell application "{BROWSER}" to set bounds of front window to {{0, 25, 1800, 1100}}')
    time.sleep(1.5)


def window_shot() -> Tuple[Image.Image, Tuple[int, int]]:
    shot, _ms, offset = capture(region=front_window_region(BROWSER))
    return shot, offset


def rows_on_page(task: Task) -> Tuple[List[Row], int]:
    screen = read_screen(region=front_window_region(BROWSER))
    rows = screen.rows_below(int(screen.height * task.skip_above), min_regions=1)
    return rows, screen.ocr_ms + screen.capture_ms


def click(point: Tuple[int, int]) -> None:
    pyautogui.moveTo(point[0], point[1], duration=0.15)
    time.sleep(0.15)
    pyautogui.click()
    time.sleep(4.0)


# --------------------------------------------------------------------------
# the four ways to choose a target
# --------------------------------------------------------------------------

def by_vision_model(task: Task, rows: List[Row]) -> Tuple[Optional[Row], int, int, int]:
    """Show the screenshot to a model and match its answer back to a row."""
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
        return None, 0, 0, ms
    payload = json.loads(proc.stdout)
    usage = payload.get("usage") or {}
    answer = str(payload.get("output_text") or "").strip().lower()
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)

    # The model replies with text; the click needs a row. Match on the longest
    # shared run of words rather than equality, since it paraphrases.
    best, score = None, 0.0
    words = set(re.findall(r"\w+", answer))
    for row in rows:
        overlap = len(words & set(re.findall(r"\w+", row.text.lower())))
        if overlap > score:
            best, score = row, overlap
    return best, tokens, 0, ms


def by_jev(task: Task, rows: List[Row], client: JevClient):
    offered = rows[:60]
    options = {r.key: f"position {i+1}: {r.text[:110]}" for i, r in enumerate(offered)}
    decision = client.choose(instructions=task.question, options=options,
                             state={"task": task.name})
    picked = next((r for r in offered if r.key == decision.choice), None)
    return picked, decision


def by_template(reference: Optional[Path], rows: List[Row]):
    """EchoPath's own rung: find the saved crop, then take the row under it."""
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
    cy = location[1] + template.shape[0] // 2 + offset[1]
    under = min((r for r in rows), key=lambda r: abs(r.y - cy), default=None)
    return under, float(score), ms


# --------------------------------------------------------------------------
# one attempt
# --------------------------------------------------------------------------

def attempt(task: Task, variation: str, condition: str,
            reference: Optional[Path], client: JevClient) -> Result:
    out = Result(task.name, variation, condition)
    started = time.perf_counter()

    open_page(task.url_for(variation))
    before = current_url()
    rows, ocr_ms = rows_on_page(task)
    if not rows:
        out.note = "nothing read from page"
        out.latency_ms = int((time.perf_counter() - started) * 1000)
        return out

    target: Optional[Row] = None

    if condition == "model-only":
        target, tokens, _, ms = by_vision_model(task, rows)
        out.tokens, out.latency_ms = tokens, ocr_ms + ms
        out.note = "vision model grounded the click"

    elif condition == "jev-only":
        try:
            target, decision = by_jev(task, rows, client)
        except JevError as error:
            out.note = f"jev error: {error}"
            return out
        out.cost, out.confidence = decision.cost, decision.confidence
        out.latency_ms = ocr_ms + decision.latency_ms
        out.note = f"jev {decision.confidence:.2f}, always trusted"

    elif condition == "echopath":
        target, score, ms = by_template(reference, rows)
        out.confidence, out.latency_ms = score, ocr_ms + ms
        if target is None:
            found, tokens, _, vms = by_vision_model(task, rows)
            target, out.tokens, out.escalated = found, tokens, True
            out.latency_ms += vms
            out.note = f"crop {score:.2f} -> vision model"
        else:
            out.note = f"crop matched {score:.2f}"

    else:  # echopath+jev
        target, score, ms = by_template(reference, rows)
        out.confidence, out.latency_ms = score, ocr_ms + ms
        if target is None:
            try:
                target, decision = by_jev(task, rows, client)
            except JevError as error:
                out.note = f"jev error: {error}"
                return out
            out.cost, out.confidence = decision.cost, decision.confidence
            out.latency_ms += decision.latency_ms
            out.note = f"crop {score:.2f} -> jev {decision.confidence:.2f}"
            # A calibrated number is only worth having if it is acted on.
            if decision.confidence < JEV_FLOOR:
                found, tokens, _, vms = by_vision_model(task, rows)
                target, out.tokens, out.escalated = found, tokens, True
                out.latency_ms += vms
                out.note += " -> vision model"
        else:
            out.note = f"crop matched {score:.2f}"

    if target is None:
        out.note += " | no target chosen"
        out.latency_ms = int((time.perf_counter() - started) * 1000)
        return out

    out.picked = target.text[:80]
    click(target.center)
    out.completed = task.succeeded(variation, before, current_url())
    out.latency_ms = int((time.perf_counter() - started) * 1000)
    return out


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    return text.strip().lower().replace(" ", "_")


def build_tasks(pilot: bool) -> List[Task]:
    n = 2 if pilot else 6
    return [
        Task(
            name="wikipedia",
            # fulltext=1 forces the results page. Without it an exact title match
            # redirects straight to the article, so there is nothing to click and
            # every condition "succeeds" on page load.
            url_for=lambda q: ("https://en.wikipedia.org/w/index.php?search="
                               f"{q.replace(' ', '+')}&fulltext=1"),
            question=("Which row is the first article result in the search results "
                      "list? Ignore navigation, search tools, sister projects and "
                      "category links."),
            # Reaching *an* article is not the task; reaching the one searched for
            # is. A looser rule scored a click on page furniture as a pass, because
            # it happened to navigate somewhere under /wiki/.
            succeeded=lambda q, a, b: ("/wiki/" in b and "search=" not in b
                                       and _slug(q).split("_")[0] in b.lower()),
            variations=["photosynthesis", "kalman filter", "tectonics",
                        "antibiotics", "opera", "semaphore"][:n],
            skip_above=0.20,
        ),
        Task(
            name="hackernews",
            url_for=lambda q: f"https://hn.algolia.com/?q={q.replace(' ', '+')}",
            question=("Which row is the first story result in the list? Ignore the "
                      "search box, filters, tabs and navigation."),
            # Stories link out to their own domain, so leaving the search host at
            # all is unambiguous evidence that a result was opened.
            succeeded=lambda q, a, b: ("hn.algolia.com" not in b
                                       and b.startswith("http")),
            variations=["raft consensus", "sqlite internals", "kalman filter",
                        "erlang otp", "bloom filter", "column store"][:n],
            skip_above=0.18,
        ),
    ]


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
             "--progress", str(PROGRESS), "--stale-after", "300"],
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


def capture_reference(task: Task, variation: str,
                      max_tries: int = 12) -> Tuple[Optional[Path], str]:
    """Find a target that actually completes the task, and save its crop.

    The earlier version saved whichever row came first, which on every one of
    these pages was furniture -- a heading or a results count. Furniture looks
    identical on every page, so the crop matched at 1.00 everywhere, the click
    went nowhere, and the condition scored zero while reporting a perfect match.
    A reference has to be a click that was seen to work, which is what EchoPath
    records and what makes the comparison meaningful.

    Doubles as the task's preflight: a task where no row completes it is broken
    -- multi-click, or the rows are not links -- and is excluded loudly rather
    than contributing silent zeros to every condition.
    """
    for index in range(max_tries):
        open_page(task.url_for(variation))
        rows, _ = rows_on_page(task)
        if not rows:
            return None, "no rows could be read"
        if index >= len(rows):
            return None, f"no row completed the task in {len(rows)} tries"

        row = rows[index]
        before = current_url()
        click(row.center)
        if not task.succeeded(variation, before, current_url()):
            continue

        CROPS.mkdir(parents=True, exist_ok=True)
        path = CROPS / f"{task.name}.png"
        # Re-open so the crop is cut from the page as it looks *before* the
        # click, which is the state replay will be matching against.
        open_page(task.url_for(variation))
        rows_again, _ = rows_on_page(task)
        if index >= len(rows_again):
            return None, "page changed shape between tries"
        target = rows_again[index]
        shot, offset = window_shot()
        x0 = max(0, min(r.x for r in target.regions) - offset[0])
        y0 = max(0, min(r.y for r in target.regions) - offset[1])
        x1 = min(shot.width, max(r.x + r.width for r in target.regions) - offset[0])
        y1 = min(shot.height, max(r.y + r.height for r in target.regions) - offset[1])
        if x1 - x0 < 10 or y1 - y0 < 8:
            return None, "target too small to crop"
        shot.crop((x0, y0, x1, y1)).save(path)
        return path, f"row {index}: {target.text[:60]}"

    return None, f"no row completed the task in {max_tries} tries"


def check_results(results: List["Result"]) -> List[str]:
    """Flag the shapes that mean the harness broke, not that a method lost."""
    warnings = []
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if not rows:
            continue
        if all(not r.completed for r in rows):
            warnings.append(
                f"{condition}: completed 0/{len(rows)} -- a whole condition failing "
                "usually means a harness fault, not a losing method")
        crops = [r.confidence for r in rows
                 if r.condition.startswith("echopath") and r.confidence is not None]
        if crops and min(crops) > 0.95:
            warnings.append(
                f"{condition}: every crop matched above 0.95 -- the reference is "
                "probably page furniture that appears on all pages")
    by_task: Dict[str, List[Result]] = {}
    for r in results:
        by_task.setdefault(r.task, []).append(r)
    for task, rows in by_task.items():
        if all(not r.completed for r in rows):
            warnings.append(f"task '{task}': nothing completed it under any "
                            "condition -- likely multi-click or non-clickable")
    return warnings


def summarise(results: List[Result]) -> None:
    print("\n" + "=" * 82)
    print(f"{'condition':15}{'completed':>13}{'tokens':>10}{'$ cost':>10}"
          f"{'avg latency':>13}{'vision calls':>14}")
    print("-" * 82)
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if not rows:
            continue
        done = sum(1 for r in rows if r.completed)
        tokens = sum(r.tokens for r in rows)
        cost = sum(r.cost for r in rows)
        latency = sum(r.latency_ms for r in rows) / len(rows)
        vision = sum(1 for r in rows if r.tokens > 0)
        print(f"{condition:15}{done:>6}/{len(rows):<6}{tokens:>10,}{cost:>10.5f}"
              f"{latency:>11.0f}ms{vision:>9}/{len(rows)}")
    print("=" * 82)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()

    tasks = build_tasks(args.pilot)
    client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
    EVAL.mkdir(parents=True, exist_ok=True)

    total = sum(len(t.variations) for t in tasks) * len(CONDITIONS)
    started_at = time.time()
    completed = 0
    results: List[Result] = []
    banner = start_banner(total)

    try:
        with RESULTS.open("w", encoding="utf-8") as sink:
            for task in tasks:
                reference, why = capture_reference(task, task.variations[0])
                print(f"[ref] {task.name}: "
                      f"{'captured -- ' + why if reference else 'EXCLUDED -- ' + why}")
                if reference is None:
                    # Running it anyway would add a column of zeros to every
                    # condition and read as though each method had failed.
                    continue
                for variation in task.variations:
                    for condition in CONDITIONS:
                        write_progress(total, completed, started_at,
                                       f"{task.name} / {variation}", condition)
                        try:
                            result = attempt(task, variation, condition, reference, client)
                        except Exception as error:      # one bad page must not end the run
                            result = Result(task.name, variation, condition,
                                            note=f"error: {type(error).__name__}: {error}")
                        results.append(result)
                        sink.write(json.dumps(asdict(result)) + "\n")
                        sink.flush()
                        completed += 1
                        mark = "OK " if result.completed else "   "
                        print(f"{mark}{task.name:11}{variation:18}{condition:14}"
                              f"{result.latency_ms:6}ms {result.tokens:7,}tok  {result.note[:44]}")
        write_progress(total, total, started_at, "done", "")
    finally:
        stop_banner(banner)

    summarise(results)
    for warning in check_results(results):
        print(f"WARNING  {warning}")
    print(f"results: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
