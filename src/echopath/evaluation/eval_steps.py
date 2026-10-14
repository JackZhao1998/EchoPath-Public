"""Three ways to resolve a replayed click, over workflows with more than one.

A single-click task cannot separate these conditions. If the crop matches, the
decision model is never called and echopath+jev ties echopath; if the crop never
matches, the crop contributes nothing and echopath+jev ties jev-only. Real
workflows mix the two -- a filter that never moves, then a result that changes
every time -- and that mix is where a ladder is worth having:

    step 1  click the filter      crop matches   -> free
    step 2  click the first result crop misses   -> decision model, cheap

  echopath       free, then a vision model              ~15k tokens
  jev-only       a decision call at both steps          2 calls
  echopath+jev   free, then one decision call           1 call

Success is checked per step, by the effect the step should have had -- a URL
parameter appearing, a host changing. Checking only the final state lets a run
flunk one step and stumble into a passing end state, which is how an earlier
version scored a click on page furniture as six correct answers.
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
EVAL = ROOT / "local_data" / "steps"
RESULTS = EVAL / "results.jsonl"
PROGRESS = EVAL / "progress.json"
CROPS = EVAL / "crops"

BROWSER = "Google Chrome"
TEMPLATE_THRESHOLD = 0.97
JEV_FLOOR = 0.40
CONDITIONS = ("echopath", "jev-only", "echopath+jev")


@dataclass
class Step:
    """One click, and the effect that proves it landed."""

    question: str
    verify: Callable[[str, str, str], bool]  # (variation, before, after)
    skip_above: float = 0.18


@dataclass
class Task:
    name: str
    url_for: Callable[[str], str]
    steps: List[Step]
    variations: List[str] = field(default_factory=list)


@dataclass
class Result:
    task: str
    variation: str
    condition: str
    completed: bool = False
    steps_done: int = 0
    steps_total: int = 0
    latency_ms: int = 0
    tokens: int = 0
    cost: float = 0.0
    jev_calls: int = 0
    vision_calls: int = 0
    crop_hits: int = 0
    crop_scores: List[float] = field(default_factory=list)
    failed_at: Optional[int] = None
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


def rows_for(step: Step) -> Tuple[List[Row], int]:
    screen = read_screen(region=front_window_region(BROWSER))
    rows = screen.rows_below(int(screen.height * step.skip_above), min_regions=1)
    return rows, screen.ocr_ms + screen.capture_ms


def click(point: Tuple[int, int]) -> None:
    pyautogui.moveTo(point[0], point[1], duration=0.15)
    time.sleep(0.15)
    pyautogui.click()
    time.sleep(3.5)


# --------------------------------------------------------------------------
# resolving one target
# --------------------------------------------------------------------------

def by_vision(step: Step, rows: List[Row]) -> Tuple[Optional[Row], int, int]:
    EVAL.mkdir(parents=True, exist_ok=True)
    path = EVAL / "shot.png"
    shot, _ = window_shot()
    shot.save(path)
    request = {
        "model": "default",
        "input": [{"type": "local_image", "path": str(path)},
                  {"type": "text", "text": step.question + " Reply with only that element's visible text."}],
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
    return best, tokens, ms


def by_jev(step: Step, rows: List[Row], client: JevClient):
    offered = rows[:60]
    options = {r.key: f"position {i+1}: {r.text[:110]}" for i, r in enumerate(offered)}
    decision = client.choose(instructions=step.question, options=options,
                             state={"note": "positions are top-first on screen"})
    return next((r for r in offered if r.key == decision.choice), None), decision


def by_crop(reference: Optional[Path], rows: List[Row]):
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
    return min(rows, key=lambda r: abs(r.y - cy), default=None), float(score), ms


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------

def attempt(task: Task, variation: str, condition: str,
            references: Dict[int, Optional[Path]], client: JevClient) -> Result:
    out = Result(task.name, variation, condition, steps_total=len(task.steps))
    started = time.perf_counter()
    open_page(task.url_for(variation))

    for index, step in enumerate(task.steps):
        rows, ocr_ms = rows_for(step)
        out.latency_ms += ocr_ms
        if not rows:
            out.failed_at, out.note = index, f"step {index}: nothing read"
            break

        target: Optional[Row] = None

        if condition == "jev-only":
            try:
                target, decision = by_jev(step, rows, client)
            except JevError as error:
                out.failed_at, out.note = index, f"jev error: {error}"
                break
            out.jev_calls += 1
            out.cost += decision.cost
            out.latency_ms += decision.latency_ms

        else:
            target, score, ms = by_crop(references.get(index), rows)
            out.latency_ms += ms
            out.crop_scores.append(round(score, 3))
            if target is not None:
                out.crop_hits += 1
            elif condition == "echopath":
                target, tokens, vms = by_vision(step, rows)
                out.vision_calls += 1
                out.tokens += tokens
                out.latency_ms += vms
            else:  # echopath+jev
                try:
                    target, decision = by_jev(step, rows, client)
                except JevError as error:
                    out.failed_at, out.note = index, f"jev error: {error}"
                    break
                out.jev_calls += 1
                out.cost += decision.cost
                out.latency_ms += decision.latency_ms
                # A calibrated number earns its keep by saying when not to act.
                if decision.confidence < JEV_FLOOR:
                    target, tokens, vms = by_vision(step, rows)
                    out.vision_calls += 1
                    out.tokens += tokens
                    out.latency_ms += vms

        if target is None:
            out.failed_at, out.note = index, f"step {index}: no target chosen"
            break

        before = current_url()
        click(target.center)
        if not step.verify(variation, before, current_url()):
            out.failed_at = index
            out.note = f"step {index} did not take effect: {target.text[:44]}"
            break
        out.steps_done += 1

    out.completed = out.steps_done == out.steps_total
    out.latency_ms = int((time.perf_counter() - started) * 1000)
    return out


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def preflight(task: Task, variation: str, max_tries: int = 20
              ) -> Tuple[Dict[int, Optional[Path]], str]:
    """Walk the task once by brute force, saving a crop for each step.

    Doubles as the task's validity check: a step no row can satisfy means the
    task is not single-click at that point, and it is excluded by name rather
    than contributing zeros to every condition.
    """
    references: Dict[int, Optional[Path]] = {}
    # Which row satisfied each step, so a later step can be re-reached by
    # repeating the same clicks rather than guessing row zero every time.
    chosen_rows: Dict[int, int] = {}
    CROPS.mkdir(parents=True, exist_ok=True)
    open_page(task.url_for(variation))

    for index, step in enumerate(task.steps):
        rows, _ = rows_for(step)
        if not rows:
            return references, f"step {index}: nothing read from the page"

        # Try whole rows first, then the individual text regions inside them.
        # A dense toolbar reads as one row -- "Read Edit View history:" is a
        # single region spanning three links -- so a row-level click can never
        # reach the middle one. That is exactly the case a crop exists to cover,
        # and preflight has to be able to record it.
        chosen = None
        targets: List[Tuple[str, int, int]] = [("row", i, 0)
                                               for i in range(min(max_tries, len(rows)))]
        for row_index in range(min(max_tries, len(rows))):
            for region_index in range(len(rows[row_index].regions)):
                targets.append(("region", row_index, region_index))

        for kind, row_index, region_index in targets:
            rows_now, _ = rows_for(step)
            if row_index >= len(rows_now):
                break
            row = rows_now[row_index]
            if kind == "row":
                point = row.center
            else:
                if region_index >= len(row.regions):
                    continue
                point = row.regions[region_index].center
            before = current_url()
            click(point)
            if step.verify(variation, before, current_url()):
                chosen = (kind, row_index, region_index)
                break
            # Reload rather than go back: a rejected candidate may have opened a
            # panel or toggled an option that back does not undo, and the row
            # list then shifts under the next candidate.
            open_page(task.url_for(variation))

        if chosen is None:
            return references, f"step {index}: no row satisfied it in {max_tries} tries"
        chosen_rows[index] = chosen

        # Re-reach this step and crop the row that worked, as it looks *before*
        # the click -- which is the state replay will be matching against.
        open_page(task.url_for(variation))
        for earlier in range(index):
            rows_e, _ = rows_for(task.steps[earlier])
            wanted = chosen_rows.get(earlier)
            if wanted is None or wanted[1] >= len(rows_e):
                return references, f"step {index}: could not re-reach it"
            kind_e, row_e, region_e = wanted
            row_obj = rows_e[row_e]
            click(row_obj.regions[region_e].center
                  if kind_e == "region" and region_e < len(row_obj.regions)
                  else row_obj.center)
        rows_here, _ = rows_for(step)
        kind, row_index, region_index = chosen
        if row_index >= len(rows_here):
            return references, f"step {index}: page changed shape"
        row_here = rows_here[row_index]
        regions = ([row_here.regions[region_index]]
                   if kind == "region" and region_index < len(row_here.regions)
                   else row_here.regions)
        target_point = regions[0].center if kind == "region" else row_here.center
        shot, offset = window_shot()
        x0 = max(0, min(r.x for r in regions) - offset[0])
        y0 = max(0, min(r.y for r in regions) - offset[1])
        x1 = min(shot.width, max(r.x + r.width for r in regions) - offset[0])
        y1 = min(shot.height, max(r.y + r.height for r in regions) - offset[1])
        if x1 - x0 < 10 or y1 - y0 < 8:
            return references, f"step {index}: target too small to crop"
        path = CROPS / f"{task.name}_{index}.png"
        shot.crop((x0, y0, x1, y1)).save(path)
        references[index] = path
        click(target_point)   # advance so the next step is reached

    return references, "ok"


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def build_tasks(pilot: bool) -> List[Task]:
    """A workflow whose two steps defeat each method on its own.

    Step 1 is the first search result: different text on every query, so the
    saved crop misses and echopath has nothing left but a vision model.

    Step 2 is the Edit tab on the article. OCR reads the whole tab strip as one
    region -- "Read Edit View history:" -- so a chooser working from text cannot
    address Edit separately from its neighbours; clicking that region's centre
    lands on View history. The crop has the pixels and hits it exactly.

    Neither method reaches both steps. The pair does, which is the point: the
    crop is not redundant once a decision model exists, it covers precisely what
    the decision model cannot see.

    Nothing is submitted: opening an edit form is a navigation, and the run
    never saves.
    """
    n = 3 if pilot else 8
    queries = ["kalman filter", "photosynthesis", "tectonics", "antibiotics",
               "semaphore", "cryptography", "opera", "thermodynamics"][:n]

    return [
        Task(
            name="open+edit",
            url_for=lambda q: ("https://en.wikipedia.org/w/index.php?search="
                               f"{q.replace(' ', '+')}&fulltext=1"),
            steps=[
                Step(question=("Which row is the first article result in the search "
                               "results list? Ignore navigation, search options, "
                               "appearance settings and sister-project links."),
                     verify=lambda q, a, b: ("/wiki/" in b and "search=" not in b
                                             and "Main_Page" not in b
                                             and any(w in b.lower()
                                                     for w in q.lower().split()
                                                     if len(w) > 3)),
                     skip_above=0.22),
                Step(question=("Which row holds the article's Edit tab, the one that "
                               "opens the page for editing?"),
                     verify=lambda q, a, b: "action=edit" in b,
                     skip_above=0.10),
            ],
            variations=queries,
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


def summarise(results: List[Result]) -> List[str]:
    print("\n" + "=" * 92)
    print(f"{'condition':15}{'completed':>12}{'steps':>9}{'tokens':>10}{'$ cost':>10}"
          f"{'latency':>12}{'crop':>7}{'jev':>6}{'vision':>8}")
    print("-" * 92)
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if not rows:
            continue
        print(f"{condition:15}{sum(r.completed for r in rows):>5}/{len(rows):<6}"
              f"{sum(r.steps_done for r in rows):>4}/{sum(r.steps_total for r in rows):<4}"
              f"{sum(r.tokens for r in rows):>10,}{sum(r.cost for r in rows):>10.5f}"
              f"{sum(r.latency_ms for r in rows)//len(rows):>10}ms"
              f"{sum(r.crop_hits for r in rows):>7}{sum(r.jev_calls for r in rows):>6}"
              f"{sum(r.vision_calls for r in rows):>8}")
    print("=" * 92)

    warnings = []
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if rows and not any(r.completed for r in rows):
            warnings.append(f"{condition}: completed 0/{len(rows)} -- suspect the harness")
    for task in {r.task for r in results}:
        rows = [r for r in results if r.task == task]
        if not any(r.completed for r in rows):
            warnings.append(f"task '{task}': no condition completed it")
    return warnings


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
                references, why = preflight(task, task.variations[0])
                ok = len(references) == len(task.steps)
                print(f"[pre] {task.name}: {'ready' if ok else 'EXCLUDED'} -- {why}")
                if not ok:
                    continue
                for variation in task.variations:
                    for condition in CONDITIONS:
                        write_progress(total, completed, started_at,
                                       f"{task.name} / {variation}", condition)
                        try:
                            result = attempt(task, variation, condition, references, client)
                        except Exception as error:
                            result = Result(task.name, variation, condition,
                                            note=f"error: {type(error).__name__}: {error}")
                        results.append(result)
                        sink.write(json.dumps(asdict(result)) + "\n")
                        sink.flush()
                        completed += 1
                        mark = "OK " if result.completed else "   "
                        print(f"{mark}{task.name:8}{variation:17}{condition:14}"
                              f"{result.steps_done}/{result.steps_total} "
                              f"{result.latency_ms:6}ms {result.tokens:7,}tok  {result.note[:38]}")
        write_progress(total, total, started_at, "done", "")
    finally:
        stop_banner(banner)

    for warning in summarise(results):
        print(f"WARNING  {warning}")
    print(f"results: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
