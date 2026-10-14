"""Measure what an OCR + decision-model rung saves a GUI replay.

A recorded workflow clicks something. On replay the page has moved on -- a
different company, a different product -- and the crop it memorised no longer
matches. What happens next is the whole question:

  echopath           template match, then a vision model looks at the screenshot
  echopath+ocr+jev   template match, then OCR the window and let a decision model
                     pick from the text it found, falling back to the vision
                     model only when that decision is not confident

Both reach the same place. The difference is what each attempt costs, so every
run records tokens, latency and whether the element found was the right one.

Each workflow spends its first variation recording a reference, exactly as
EchoPath does, and later variations are scored against it -- a memory built on
one input, run on another.

    python -m echopath.evaluation.eval_harness --pilot
    python -m echopath.evaluation.eval_harness
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from echopath.jev_client import JevClient, JevError
from echopath.ocr_extractor import (
    Row,
    capture,
    front_window_region,
    read_screen,
)

from echopath.project_paths import project_root
from .eval_cases import Case, build_cases

ROOT = project_root()
EVAL_DIR = ROOT / "local_data" / "eval"
RESULTS = EVAL_DIR / "results.jsonl"
PROGRESS = EVAL_DIR / "progress.json"
CROPS = EVAL_DIR / "crops"
SHOTS = EVAL_DIR / "shots"

BROWSER = "Google Chrome"
# EchoPath's own acceptance threshold for a template match.
TEMPLATE_THRESHOLD = 0.74
# Below this a decision is a coin flip, so the ladder pays for the vision model.
JEV_CONFIDENCE_FLOOR = 0.90
PAGE_SETTLE_SECONDS = 9.0
CONDITIONS = ("echopath", "echopath+ocr+jev")


@dataclass
class Outcome:
    workflow: str
    variation: str
    condition: str
    correct: bool = False
    text: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    confidence: Optional[float] = None
    used_vision: bool = False
    # Jev's own answer, kept even when the ladder escalates past it -- otherwise
    # the vision model's result is recorded as though the cheap rung produced it
    # and the rung can never be evaluated on its own.
    jev_confidence: Optional[float] = None
    jev_correct: Optional[bool] = None
    jev_text: str = ""
    note: str = ""


# --------------------------------------------------------------------------
# page control
# --------------------------------------------------------------------------

_browser_started = False


def open_page(url: str) -> None:
    """Point the browser at a URL, launching it once."""
    global _browser_started
    if not _browser_started:
        subprocess.run(["osascript", "-e", f'tell application "{BROWSER}" to quit'],
                       capture_output=True)
        time.sleep(2.0)
        subprocess.Popen(["open", "-na", BROWSER, "--args", "--incognito", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(PAGE_SETTLE_SECONDS)
        _browser_started = True
    else:
        # Some of these URLs differ only after the '#', where a change alone may
        # not re-run the page; without the reload the next case would be scored
        # against the previous one's results and look like a clean pass.
        subprocess.run(["osascript", "-e",
                        f'tell application "{BROWSER}" to set URL of active tab '
                        f'of front window to "{url}"'], capture_output=True)
        time.sleep(1.0)
        subprocess.run(["osascript", "-e",
                        f'tell application "{BROWSER}" to reload active tab of front window'],
                       capture_output=True)
        time.sleep(4.0)

    subprocess.run(["osascript", "-e", f'tell application "{BROWSER}" to activate'],
                   capture_output=True)
    time.sleep(1.0)


def window_shot() -> Tuple[Image.Image, Tuple[int, int]]:
    shot, _ms, offset = capture(region=front_window_region(BROWSER))
    return shot, offset


def candidate_rows(case: Case) -> Tuple[List[Row], int, int]:
    """OCR the window and return the rows worth choosing between."""
    screen = read_screen(region=front_window_region(BROWSER))
    cutoff = int(screen.height * case.skip_above)
    rows = [r for r in screen.rows_below(cutoff, min_regions=2)]
    return rows, screen.capture_ms, screen.ocr_ms


def judge(case: Case, text: str) -> bool:
    if not text:
        return False
    if case.reject and re.search(case.reject, text, re.I):
        return False
    return bool(re.search(case.expect, text, re.I))


# --------------------------------------------------------------------------
# the rungs
# --------------------------------------------------------------------------

def template_match(reference: Optional[Path]) -> Tuple[float, Optional[Tuple[int, int]], int]:
    """EchoPath's own first rung: find the saved crop on the live screen."""
    if reference is None or not reference.exists():
        return 0.0, None, 0
    started = time.perf_counter()
    shot, offset = window_shot()
    screen = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(np.array(Image.open(reference).convert("RGB")), cv2.COLOR_RGB2GRAY)
    if template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
        return 0.0, None, int((time.perf_counter() - started) * 1000)
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    centre = (location[0] + template.shape[1] // 2 + offset[0],
              location[1] + template.shape[0] // 2 + offset[1])
    return float(score), centre, int((time.perf_counter() - started) * 1000)


def ask_vision_model(case: Case) -> Tuple[str, int, int, int]:
    """What EchoPath does when re-aiming fails: hand the screenshot to a model."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / "current.png"
    shot, _offset = window_shot()
    shot.save(path)

    request = {
        "model": "default",
        "input": [
            {"type": "local_image", "path": str(path)},
            {"type": "text", "text": case.question + " Reply with only that element's text."},
        ],
        "instructions": None, "metadata": {}, "reasoning_effort": "low",
        "temperature": None, "max_output_tokens": None, "extra_body": {},
    }
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, str(ROOT / "bridges" / "codex_cli" / "run_codex_cli.py")],
        input=json.dumps(request), capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    latency = int((time.perf_counter() - started) * 1000)
    if result.returncode != 0:
        return "", 0, 0, latency
    payload = json.loads(result.stdout)
    usage = payload.get("usage") or {}
    return (str(payload.get("output_text") or "").strip(),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            latency)


def run_echopath(case: Case, reference: Optional[Path]) -> Outcome:
    """Template match; on failure, a vision model reads the screenshot."""
    out = Outcome(case.workflow, case.variation, "echopath")
    score, _centre, match_ms = template_match(reference)
    out.confidence = score
    out.latency_ms = match_ms

    if score >= TEMPLATE_THRESHOLD:
        # Matched, but on a changed page the match is of the old content, so it
        # is not evidence the right element was found. Scored as a miss unless
        # the text under it says otherwise -- which OCR can answer cheaply.
        rows, _c, ocr_ms = candidate_rows(case)
        out.latency_ms += ocr_ms
        out.text = rows[0].text[:140] if rows else ""
        out.correct = judge(case, out.text)
        out.note = "template matched"
        return out

    text, tokens_in, tokens_out, vision_ms = ask_vision_model(case)
    out.used_vision = True
    out.latency_ms += vision_ms
    out.tokens_in, out.tokens_out = tokens_in, tokens_out
    out.text = text[:140]
    out.correct = judge(case, out.text)
    out.note = f"template {score:.2f} -> vision model"
    return out


def run_ocr_jev(case: Case, reference: Optional[Path], client: JevClient) -> Outcome:
    """Template match; on failure, OCR the window and let Jev choose."""
    out = Outcome(case.workflow, case.variation, "echopath+ocr+jev")
    score, _centre, match_ms = template_match(reference)
    out.confidence = score
    out.latency_ms = match_ms

    rows, capture_ms, ocr_ms = candidate_rows(case)
    out.latency_ms += capture_ms + ocr_ms
    if not rows:
        out.note = "no rows found"
        return out

    if score >= TEMPLATE_THRESHOLD:
        out.text = rows[0].text[:140]
        out.correct = judge(case, out.text)
        out.note = "template matched"
        return out

    offered = rows[:60]
    options = {
        r.key: f"position {i + 1} of {len(offered)}: {r.text[:110]}"
        for i, r in enumerate(offered)
    }
    try:
        decision = client.choose(
            instructions=case.question,
            options=options,
            state={"page": case.url,
                   "note": "positions are in on-screen reading order, top first"},
        )
    except JevError as error:
        out.note = f"jev error: {error}"
        return out

    out.latency_ms += decision.latency_ms
    out.cost += decision.cost
    out.confidence = decision.confidence
    picked = next((r for r in offered if r.key == decision.choice), None)
    out.text = picked.text[:140] if picked else ""
    out.correct = judge(case, out.text)
    out.jev_confidence = decision.confidence
    out.jev_text = out.text
    out.jev_correct = out.correct
    out.note = f"jev {decision.confidence:.2f}"

    # The point of a calibrated number is to know when not to trust it.
    if decision.confidence < JEV_CONFIDENCE_FLOOR:
        text, tokens_in, tokens_out, vision_ms = ask_vision_model(case)
        out.used_vision = True
        out.latency_ms += vision_ms
        out.tokens_in, out.tokens_out = tokens_in, tokens_out
        out.text = text[:140]
        out.correct = judge(case, out.text)
        out.note += " -> escalated to vision model"
    return out


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def capture_reference(case: Case) -> Optional[Path]:
    """Save the crop a recording would have kept for this workflow."""
    rows, _c, _o = candidate_rows(case)
    if not rows:
        return None
    target = rows[0]
    CROPS.mkdir(parents=True, exist_ok=True)
    path = CROPS / f"{case.workflow}.png"
    shot, offset = window_shot()
    xs = [r.x for r in target.regions]
    ys = [r.y for r in target.regions]
    x0, y0 = min(xs) - offset[0], min(ys) - offset[1]
    x1 = max(r.x + r.width for r in target.regions) - offset[0]
    y1 = max(r.y + r.height for r in target.regions) - offset[1]
    box = (max(0, x0), max(0, y0), min(shot.width, x1), min(shot.height, y1))
    if box[2] - box[0] < 10 or box[3] - box[1] < 8:
        return None
    shot.crop(box).save(path)
    return path


def start_banner(total: int) -> Optional[subprocess.Popen]:
    """Own the banner, so it cannot outlive the run and sit on the screen."""
    write_progress(total, 0, time.time(), "starting", "")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "echopath.desktop_status",
             "--progress", str(PROGRESS), "--stale-after", "240"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def stop_banner(banner: Optional[subprocess.Popen]) -> None:
    if banner is None:
        return
    banner.terminate()
    try:
        banner.wait(timeout=3)
    except subprocess.TimeoutExpired:
        banner.kill()


def write_progress(total: int, completed: int, started_at: float,
                   label: str, condition: str) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps({
        "total": total, "completed": completed, "started_at": started_at,
        "label": label, "condition": condition}), encoding="utf-8")


def summarise(results: List[Outcome]) -> None:
    print("\n" + "=" * 78)
    print(f"{'condition':18} {'solved':>10} {'tokens':>10} {'cost':>10} "
          f"{'latency':>10} {'vision':>8}")
    print("-" * 78)
    for condition in CONDITIONS:
        rows = [r for r in results if r.condition == condition]
        if not rows:
            continue
        correct = sum(1 for r in rows if r.correct)
        tokens = sum(r.tokens_in + r.tokens_out for r in rows)
        cost = sum(r.cost for r in rows)
        latency = sum(r.latency_ms for r in rows) / len(rows)
        vision = sum(1 for r in rows if r.used_vision)
        print(f"{condition:18} {correct:4}/{len(rows):<5} {tokens:10,} "
              f"${cost:<9.5f} {latency:8.0f}ms {vision:4}/{len(rows)}")
    jev = [r for r in results if r.condition == "echopath+ocr+jev"
           and r.jev_correct is not None]
    if jev:
        hits = sum(1 for r in jev if r.jev_correct)
        print(f"\nJev alone, ignoring the floor: {hits}/{len(jev)} correct")
        print("floor   trusted  of those right   escalations")
        for floor in (0.0, 0.3, 0.4, 0.5, 0.7, 0.9):
            trusted = [r for r in jev if (r.jev_confidence or 0) >= floor]
            right = sum(1 for r in trusted if r.jev_correct)
            print(f"{floor:5.2f}   {len(trusted):3}/{len(jev):<3}   "
                  f"{right:3}/{len(trusted) or 1:<3}          {len(jev) - len(trusted)}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true", help="short run")
    args = parser.parse_args()

    cases = build_cases(pilot=args.pilot)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
    started_at = time.time()
    total = len(cases) * len(CONDITIONS)
    completed = 0
    results: List[Outcome] = []
    references: Dict[str, Optional[Path]] = {}

    banner = start_banner(total)
    try:
        with RESULTS.open("w", encoding="utf-8") as sink:
            for case in cases:
                open_page(case.url)

                # A workflow's first variation is its recording, not a test.
                if case.workflow not in references:
                    references[case.workflow] = capture_reference(case)
                    state = "captured" if references[case.workflow] else "FAILED"
                    print(f"[ref] {case.workflow}: {state} ({case.variation})")
                    continue

                for condition in CONDITIONS:
                    write_progress(total, completed, started_at,
                                   f"{case.workflow} / {case.variation}", condition)
                    if condition == "echopath":
                        outcome = run_echopath(case, references[case.workflow])
                    else:
                        outcome = run_ocr_jev(case, references[case.workflow], client)
                    results.append(outcome)
                    sink.write(json.dumps(asdict(outcome)) + "\n")
                    sink.flush()
                    completed += 1
                    mark = "OK " if outcome.correct else "   "
                    print(f"{mark}{case.workflow:8} {case.variation:15} {condition:17} "
                          f"{outcome.latency_ms:6}ms {outcome.tokens_in:7,}tok  "
                          f"{outcome.text[:40]}")
        write_progress(total, total, started_at, "done", "")
    finally:
        stop_banner(banner)

    summarise(results)
    print(f"results: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
