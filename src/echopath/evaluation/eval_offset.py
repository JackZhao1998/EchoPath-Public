"""Where a text chooser cannot reach, and why it is not the OCR's fault.

Two resolvers are compared on tasks built so their failures cannot overlap.

  offset     Select a search result by its checkbox. The row IS text, so the
             chooser finds it confidently -- and returns where the TEXT is.
             The control sits 170-350px to its left, so the click lands on the
             title link and navigates away. Better OCR does not fix this: OCR
             reports where text is, not where the control is.
  wordless   Mute a video, or seek it. The controls are glyphs. OCR returns no
             option for them, so the chooser is never shown the target.

Three things this harness does that the earlier one did not, each because a
review found the earlier result uninterpretable without it:

  * The demonstrated variation is NOT scored. Matching a template against the
    screenshot it was cut from is train-on-test, and it accounted for four of
    the seven crop hits previously published.
  * The vision rung returns COORDINATES. Previously its text answer was matched
    by word overlap against the same OCR rows the chooser got, so two of the
    three conditions shared one bottleneck -- the bottleneck under test.
  * Success is read from the page's own state over DevTools Protocol, never
    from pixels. A pixel oracle would be template matching, which is the
    mechanism under test, and would hand the crop arm the answer.

Each task also runs a negative control: a deliberately wrong click in the same
band must FAIL the check, or the check proves nothing.

    python -m echopath.evaluation.eval_offset --pilot
    python -m echopath.evaluation.eval_offset
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
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np
import pyautogui
from PIL import Image

from echopath.cdp import Page
from echopath.jev_client import JevClient, JevError
from echopath.ocr_extractor import Row, read_screen

from echopath.project_paths import project_root

ROOT = project_root()
EVAL = ROOT / "local_data" / "offset"
RESULTS = EVAL / "results.jsonl"
CROPS = EVAL / "crops"

TEMPLATE_THRESHOLD = 0.74
JEV_FLOOR = 0.50          # raised from 0.40: honest blindness reports 0.19-0.25
CROP_RADIUS = 26
CONDITIONS = ("echopath", "jev-only", "echopath+jev")


@dataclass
class Task:
    name: str
    family: str
    url_for: Callable[[str], str]
    question: str = ""
    # Some tasks name their target only once the page is open ("the 5 September
    # revision"), so the question is built from the state read before the click.
    question_from: Optional[Callable[[object], str]] = None
    # Jev normally chooses a ROW, and a row-click lands on its leftmost text.
    # Where the target is one word among several on a line -- "prev" beside a
    # date -- the row is the wrong unit and Jev would miss for a reason that has
    # nothing to do with whether it chose correctly.
    option_mode: str = "rows"
    # Where the demonstrator points, read from the page itself. This stands in
    # for a person pointing at the control; it is never consulted during replay.
    demo_point_js: str = "null"
    # Some controls only exist once the app is in a particular state -- a video
    # player builds its control bar only after playback starts. This click is
    # setup, not the step under test.
    prepare_point_js: Optional[str] = None
    # Run before anything is read or clicked. A target below the fold is not in
    # the screenshot, so it is not in the OCR options either -- the chooser is
    # then asked to pick something it was never shown. A person scrolls to it.
    prepare_js: Optional[str] = None
    # Put the page back to a known state. Without this the previous attempt's
    # result is still on screen -- PubMed remembers selected results across
    # navigations, so every later run "passed" without clicking anything.
    reset_js: str = "true"
    # Page state before and after, and whether the change is the one asked for.
    state_js: str = "null"
    succeeded: Callable[[object, object], bool] = lambda a, b: False
    variations: List[str] = field(default_factory=list)
    settle: float = 8.0


@dataclass
class Choice:
    """One thing Jev may pick, described the way a person would see it."""
    key: str
    text: str
    center: Tuple[int, int]


def choices_for(task: "Task", screen) -> List[Choice]:
    rows = screen.rows(min_regions=1)
    if task.option_mode != "regions":
        return [Choice(r.key, r.text, r.center) for r in rows]
    # Region-level, but each word keeps the line it belongs to. Without that
    # context every "prev" on the page is the same option and the question is
    # unanswerable for a reason that has nothing to do with the model.
    out: List[Choice] = []
    for row in rows:
        line = row.text.strip()[:90]
        for region in row.regions:
            word = region.text.strip()
            if not word:
                continue
            out.append(Choice(region.key, f"{word}  \u2014 on the line: {line}",
                              region.center))
    return out


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
# browser, driven for state; the mouse does the clicking
# --------------------------------------------------------------------------

class Browser:
    def __init__(self) -> None:
        self.page = Page(headless=False)
        time.sleep(2)
        if not self.page.on_primary():
            spot = self.page.js("[window.screenX, window.screenY]")
            raise RuntimeError(
                f"browser opened off the primary display at {spot}; the mouse "
                "and screenshots only reach the primary, so every coordinate "
                "would be wrong")

    def open(self, url: str, settle: float) -> None:
        self.page.goto(url, wait=settle)

    def offset(self) -> Tuple[int, int]:
        """Where the viewport's origin sits on screen."""
        got = self.page.js("[window.screenX, window.screenY,"
                           " window.outerHeight - window.innerHeight]")
        return int(got[0]), int(got[1]) + int(got[2])

    def to_screen(self, x: float, y: float) -> Tuple[int, int]:
        ox, oy = self.offset()
        return int(ox + x), int(oy + y)

    def state(self, task: Task, variation: str = ""):
        return self.page.js(render(task.state_js, variation))

    def close(self) -> None:
        self.page.close()


def render(js: str, variation: str) -> str:
    """Point a task's JavaScript at this variation's target row.

    The target must move between variations. Holding it at the row that was
    demonstrated lets a template match succeed by landing on the same index,
    which measures nothing about telling identical controls apart.
    """
    index = variation.split("|")[1] if "|" in variation else "0"
    return js.replace("__IDX__", index)


def arrive(browser: "Browser", task: Task, variation: str) -> None:
    """Open the page and put it in the state the step assumes."""
    browser.open(task.url_for(variation), task.settle)
    pyautogui.moveTo(750, 500, duration=0.2)
    time.sleep(1.2)
    if task.prepare_js:
        browser.page.js(render(task.prepare_js, variation))
        time.sleep(1.4)
    if task.prepare_point_js:
        spot = browser.page.js(task.prepare_point_js)
        if spot:
            click(browser.to_screen(spot[0], spot[1]))
            time.sleep(4.5)
            # the control bar hides itself again unless the pointer is near it
            pyautogui.moveTo(700, 690, duration=0.25)
            time.sleep(1.5)
    browser.page.js(task.reset_js)
    time.sleep(0.6)


def click(point: Tuple[int, int]) -> None:
    pyautogui.moveTo(point[0], point[1], duration=0.18)
    time.sleep(0.25)
    pyautogui.click()
    time.sleep(2.8)


def window_shot() -> Image.Image:
    shot = pyautogui.screenshot()
    size = pyautogui.size()
    if shot.size != (size[0], size[1]):
        shot = shot.resize((size[0], size[1]), Image.LANCZOS)
    return shot.convert("RGB")


def rows_now() -> Tuple[List[Row], int]:
    screen = read_screen()
    return screen.rows(min_regions=1), screen.ocr_ms + screen.capture_ms


# --------------------------------------------------------------------------
# the three resolvers
# --------------------------------------------------------------------------

def by_crop(reference: Path) -> Tuple[Optional[Tuple[int, int]], float, int]:
    started = time.perf_counter()
    shot = window_shot()
    screen = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(np.array(Image.open(reference).convert("RGB")), cv2.COLOR_RGB2GRAY)
    if template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
        return None, 0.0, int((time.perf_counter() - started) * 1000)
    matched = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, at = cv2.minMaxLoc(matched)
    ms = int((time.perf_counter() - started) * 1000)
    if score < TEMPLATE_THRESHOLD:
        return None, float(score), ms
    return (at[0] + template.shape[1] // 2, at[1] + template.shape[0] // 2), float(score), ms


def by_jev(question: str, choices, client: JevClient):
    offered = choices[:60]
    if not offered:
        return None, None
    options = {c.key: f"position {i+1}: {c.text[:110]}" for i, c in enumerate(offered)}
    decision = client.choose(instructions=question, options=options,
                             state={"note": "positions are top-first on screen"})
    picked = next((c for c in offered if c.key == decision.choice), None)
    return picked, decision


_POINT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*[,x]\s*(\d{1,3}(?:\.\d+)?)")


def by_vision(question: str) -> Tuple[Optional[Tuple[int, int]], int, int]:
    """Ask for a point, not a phrase.

    The previous harness matched the model's words against the same OCR rows
    the chooser was given, so this rung could never beat the option generator.
    Asking for coordinates makes it an independent baseline.
    """
    EVAL.mkdir(parents=True, exist_ok=True)
    path = EVAL / "shot.png"
    shot = window_shot()
    shot.save(path)
    prompt = (question + " Reply with ONLY the click point as two numbers, "
              "the percentage across and the percentage down the image, like: 42.5, 88.1")
    request = {
        "model": "default",
        "input": [{"type": "local_image", "path": str(path)},
                  {"type": "text", "text": prompt}],
        "instructions": None, "metadata": {}, "reasoning_effort": "low",
        "temperature": None, "max_output_tokens": None, "extra_body": {},
    }
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bridges" / "codex_cli" / "run_codex_cli.py")],
                          input=json.dumps(request), capture_output=True, text=True,
                          cwd=str(ROOT))
    ms = int((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as error:
        # A silent return here reads as "the model found nothing" and costs
        # nothing, which is indistinguishable from the rung working and losing.
        print(f"    !! vision rung failed: {type(error).__name__} "
              f"{(proc.stderr or '').strip()[-120:]}", flush=True)
        return None, 0, ms
    # The bridge reports input_tokens/output_tokens, not total_tokens. Reading
    # the wrong key returns 0 and the expensive rung looks free.
    usage = payload.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0) or (
        int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
    found = _POINT.search(str(payload.get("output_text") or ""))
    if not found:
        return None, tokens, ms
    across, down = float(found.group(1)), float(found.group(2))
    if not (0 <= across <= 100 and 0 <= down <= 100):
        return None, tokens, ms
    return (int(shot.width * across / 100), int(shot.height * down / 100)), tokens, ms


# --------------------------------------------------------------------------
# one attempt
# --------------------------------------------------------------------------

def attempt(browser: Browser, task: Task, variation: str, condition: str,
            reference: Path, client: Optional[JevClient]) -> Result:
    out = Result(task=task.name, family=task.family, variation=variation, condition=condition)
    arrive(browser, task, variation)
    before = browser.state(task, variation)
    question = task.question_from(before) if task.question_from else task.question

    started = time.perf_counter()
    point, trail = None, []

    if condition in ("echopath", "echopath+jev"):
        point, score, _ms = by_crop(reference)
        out.crop_score = score
        trail.append(f"crop {score:.2f}")
        out.used_crop = point is not None

    if point is None and condition in ("jev-only", "echopath+jev") and client is not None:
        choices = choices_for(task, read_screen())
        try:
            row, decision = by_jev(question, choices, client)
        except JevError as error:
            row, decision = None, None
            trail.append(f"jev error {error}")
        if decision is not None:
            out.jev_confidence = decision.confidence
            out.picked = (row.text[:70] if row is not None else "")
            out.cost += decision.cost or 0.0
            trail.append(f"jev {decision.confidence:.2f}")
            if decision.confidence >= JEV_FLOOR and row is not None:
                point, out.used_jev = row.center, True

    if point is None and condition in ("echopath", "echopath+jev"):
        point, tokens, _ms = by_vision(question)
        out.tokens += tokens
        out.used_vision = True
        trail.append("vision" if point else "vision | no point")

    if point is not None:
        click(point)
    out.latency_ms = int((time.perf_counter() - started) * 1000)

    after = browser.state(task, variation)
    out.completed = bool(task.succeeded(before, after))
    out.note = " -> ".join(trail)
    return out


def demonstrate(browser: Browser, task: Task) -> Tuple[Optional[Path], str]:
    """Do the task once the way a person would, and keep the picture.

    The demonstrator is told where the control is, exactly as a human knows
    where they are pointing. This is not a search: one look, one click.
    """
    variation = task.variations[0]
    arrive(browser, task, variation)
    spot = browser.page.js(render(task.demo_point_js, variation))
    if not spot:
        return None, "demonstrator could not locate the control"
    point = browser.to_screen(spot[0], spot[1])
    before = browser.state(task, variation)
    shot = window_shot()
    CROPS.mkdir(parents=True, exist_ok=True)
    box = (max(0, point[0] - CROP_RADIUS), max(0, point[1] - CROP_RADIUS),
           point[0] + CROP_RADIUS, point[1] + CROP_RADIUS)
    reference = CROPS / f"{task.name}.png"
    shot.crop(box).save(reference)

    click(point)
    after = browser.state(task, variation)
    if not task.succeeded(before, after):
        return None, f"the demonstrated click had no effect (state {after!r})"
    return reference, f"recorded at {point}"


def negative_control(browser: Browser, task: Task) -> str:
    """A wrong click in the same band must fail the check."""
    arrive(browser, task, task.variations[0])
    spot = browser.page.js(render(task.demo_point_js, task.variations[0]))
    if not spot:
        return "skipped: no control located"
    before = browser.state(task, task.variations[0])
    # A fixed offset is not necessarily a wrong click: a search-result title is
    # wide enough that 260px to the right is still the same link, and the
    # control then "passes" while proving nothing. Ask the page for an offset
    # that genuinely lands on a different element.
    away = browser.page.js(f"""
      (() => {{
        const tx = {spot[0]}, ty = {spot[1]};
        const target = document.elementFromPoint(tx, ty);
        if (!target) return null;
        for (const d of [[0,150],[0,220],[0,300],[-300,0],[0,-160],[0,420]]) {{
          const e = document.elementFromPoint(tx + d[0], ty + d[1]);
          if (e && e !== target && !target.contains(e) && !e.contains(target)) {{
            return d;
          }}
        }}
        return null;
      }})()""")
    if not away:
        return "skipped: no genuinely different element nearby"
    click(browser.to_screen(spot[0] + away[0], spot[1] + away[1]))
    after = browser.state(task, task.variations[0])
    return (f"FAILS AS IT SHOULD (wrong click at offset {tuple(away)})"
            if not task.succeeded(before, after)
            else "!! PASSED ON A WRONG CLICK -- oracle is useless")


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def build_tasks(n: int) -> List[Task]:
    terms = ["kalman filter", "photosynthesis", "tectonics",
             "antibiotics", "cryptography", "thermodynamics"][:n]

    # "article|row" -- the row to act on differs per article, so a match that
    # simply lands on the demonstrated index cannot pass.
    articles = ["Kalman_filter|2", "Photosynthesis|6", "Plate_tectonics|9",
                "Antibiotic|4", "Cryptography|11", "Thermodynamics|7"][:n]

    return [
        Task(
            name="wiki-history", family="identical",
            url_for=lambda a: (f"https://en.wikipedia.org/w/index.php?"
                               f"title={a.split('|')[0]}&action=history"),
            # Named at run time, because the dates differ per article.
            question_from=lambda st: (
                f"Which link opens the diff for the revision dated "
                f"{(st or {}).get('target_label', '?')}? "
                f"Choose the 'prev' link on that revision's line."),
            # One word among several on a line, so rows are the wrong unit.
            option_mode="regions",
            demo_point_js="""
              (() => {
                const li = document.querySelectorAll('#pagehistory li')[__IDX__];
                if (!li) return null;
                const a = [...li.querySelectorAll('a')]
                  .find(x => x.textContent.trim() === 'prev');
                if (!a) return null;
                const r = a.getBoundingClientRect();
                return r.width ? [r.x + r.width/2, r.y + r.height/2] : null;
              })()""",
            prepare_js="""
              (() => {
                const li = document.querySelectorAll('#pagehistory li')[__IDX__];
                if (li) { li.scrollIntoView({block: 'center'}); }
                return true;
              })()""",
            reset_js="true",
            # The target is the third revision down. Its id is read before the
            # click; afterwards only the address bar is consulted.
            state_js="""
              (() => {
                const li = document.querySelectorAll('#pagehistory li')[__IDX__];
                let label = null, oldid = null;
                if (li) {
                  const d = li.querySelector('a.mw-changeslist-date');
                  label = d ? d.textContent.trim() : null;
                  const a = [...li.querySelectorAll('a')]
                    .find(x => x.textContent.trim() === 'prev');
                  if (a) { oldid = new URL(a.href).searchParams.get('oldid'); }
                }
                return {search: location.search, target_label: label,
                        target_oldid: oldid,
                        // guard: nothing here may ever reach a save form
                        on_edit_form: !!document.querySelector('#wpSave, #wpSaveWidget')};
              })()""",
            succeeded=lambda a, b: bool(
                b and a and a.get("target_oldid")
                and f"oldid={a['target_oldid']}" in (b.get("search") or "")
                and not b.get("on_edit_form")),
            variations=articles, settle=8.0,
        ),
        Task(
            name="pubmed-open", family="changing",
            url_for=lambda q: ("https://pubmed.ncbi.nlm.nih.gov/?term="
                               + q.split("|")[0].replace(" ", "+")),
            # The article title is different for every query, so the saved
            # picture cannot match it -- and the title IS the link, so a text
            # chooser can both read the target and aim at it.
            question_from=lambda st: (
                "Which link opens the article titled "
                f"\u201c{(st or {}).get('target_title', '?')}\u201d?"),
            option_mode="regions",
            demo_point_js="""
              (() => {
                const a = document.querySelectorAll(
                  'article.full-docsum a.docsum-title')[__IDX__];
                if (!a) return null;
                const r = a.getBoundingClientRect();
                return r.width ? [r.x + Math.min(r.width/2, 150), r.y + r.height/2] : null;
              })()""",
            prepare_js="""
              (() => {
                const a = document.querySelectorAll(
                  'article.full-docsum a.docsum-title')[__IDX__];
                if (a) { a.scrollIntoView({block: 'center'}); }
                return true;
              })()""",
            reset_js="true",
            state_js=r"""
              (() => {
                const a = document.querySelectorAll(
                  'article.full-docsum a.docsum-title')[__IDX__];
                let title = null, pmid = null;
                if (a) {
                  title = a.innerText.trim().replace(/\s+/g, ' ').slice(0, 70);
                  const art = a.closest('article.full-docsum');
                  const cb = art ? art.querySelector('input[type=checkbox]') : null;
                  pmid = cb ? cb.value : null;
                }
                return {url: location.pathname, target_title: title, target_pmid: pmid};
              })()""",
            succeeded=lambda a, b: bool(
                b and a and a.get("target_pmid")
                and a["target_pmid"] in (b.get("url") or "")),
            variations=[f"{t}|{i}" for t, i in
                        zip(terms, [0, 3, 5, 1, 4, 2])][:n],
            settle=8.0,
        ),
        Task(
            name="pubmed-select", family="offset",
            url_for=lambda q: f"https://pubmed.ncbi.nlm.nih.gov/?term={q.replace(' ', '+')}",
            question=("Which row is the first search result, the one to select with its "
                      "checkbox? Reply with that row."),
            demo_point_js="""
              (() => {
                const l = document.querySelector('article.full-docsum label.search-result-position');
                if (!l) return null;
                const r = l.getBoundingClientRect();
                return [r.x + r.width/2, r.y + r.height/2];
              })()""",
            reset_js="""
              (() => {
                document.querySelectorAll('article.full-docsum input[type=checkbox]')
                  .forEach(b => { if (b.checked) { b.checked = false;
                    b.dispatchEvent(new Event('change', {bubbles: true})); } });
                try { sessionStorage.clear(); } catch (e) {}
                return true;
              })()""",
            state_js="""
              (() => {
                const boxes = [...document.querySelectorAll(
                  'article.full-docsum input[type=checkbox]')];
                const first = boxes[0] ? boxes[0].value : null;
                const on = boxes.filter(b => b.checked).map(b => b.value);
                return {first: first, checked: on, url: location.pathname};
              })()""",
            # Nothing selected before, exactly the first result after, and we
            # never left the results page.
            succeeded=lambda a, b: bool(
                b and a and a.get("checked") == [] and a.get("first")
                and b.get("checked") == [a.get("first")]
                and b.get("url") == a.get("url")),
            variations=terms, settle=8.0,
        ),
        Task(
            name="commons-mute", family="wordless",
            url_for=lambda f: f"https://commons.wikimedia.org/wiki/File:{f}",
            question="Which element mutes the video?",
            # Open the player first: its control bar does not exist until then.
            prepare_point_js="""
              (() => {
                const b = document.querySelector('.mw-tmh-play');
                if (!b) return null;
                const r = b.getBoundingClientRect();
                return r.width ? [r.x + r.width/2, r.y + r.height/2] : null;
              })()""",
            demo_point_js="""
              (() => {
                const b = document.querySelector('.vjs-mute-control');
                if (!b) return null;
                const r = b.getBoundingClientRect();
                return r.width ? [r.x + r.width/2, r.y + r.height/2] : null;
              })()""",
            reset_js="""
              (() => {
                const vs = [...document.querySelectorAll('video')];
                const v = vs.find(x => x.currentTime > 0) || vs[vs.length - 1];
                if (v) { v.muted = false; }
                return true;
              })()""",
            state_js="""
              (() => {
                const vs = [...document.querySelectorAll('video')];
                const v = vs.find(x => x.currentTime > 0) || vs[vs.length - 1];
                return v ? {muted: v.muted, playing: !v.paused} : null;
              })()""",
            succeeded=lambda a, b: bool(b and a and not a.get("muted") and b.get("muted")),
            variations=["Big_Buck_Bunny_medium.ogv", "Elephants_Dream_(2006).webm",
                        "Tears_of_Steel_1080p.webm",
                        "Caminandes-_Llama_Drama_-_Short_Movie.ogv",
                        "Sintel_movie_4K.webm",
                        "Cosmos_Laundromat_-_First_Cycle_-_Official_Blender_Foundation_release.webm"][:n],
            settle=11.0,
        ),
    ]


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true", help="3 variations per task")
    parser.add_argument("--only", help="run one task by name")
    args = parser.parse_args()

    tasks = build_tasks(3 if args.pilot else 6)
    if args.only:
        tasks = [t for t in tasks if t.name == args.only]

    EVAL.mkdir(parents=True, exist_ok=True)
    RESULTS.unlink(missing_ok=True)
    pyautogui.FAILSAFE = False

    try:
        client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
    except JevError as error:
        print(f"jev unavailable: {error}")
        return 1

    browser = Browser()
    results: List[Result] = []
    try:
        for task in tasks:
            reference, why = demonstrate(browser, task)
            print(f"[demo] {task.name} ({task.family}): "
                  f"{'ok -- ' + why if reference else 'FAILED -- ' + why}", flush=True)
            if reference is None:
                continue
            print(f"[control] {task.name}: {negative_control(browser, task)}", flush=True)

            # The demonstrated variation is not scored -- matching a template
            # against the shot it was cut from is train-on-test.
            for variation in task.variations[1:]:
                for condition in CONDITIONS:
                    out = attempt(browser, task, variation, condition, reference, client)
                    results.append(out)
                    with RESULTS.open("a") as handle:
                        handle.write(json.dumps(asdict(out)) + "\n")
                    print(f"  {task.name:14} {variation[:16]:16} {condition:13} "
                          f"{'ok' if out.completed else 'NO':3} {out.note}", flush=True)
    finally:
        browser.close()

    print(f"\n{'condition':14}" + "".join(f"{f:>12}" for f in ("offset", "wordless"))
          + f"{'overall':>10}{'tokens':>9}{'$':>9}")
    for condition in CONDITIONS:
        runs = [r for r in results if r.condition == condition]
        if not runs:
            continue
        cells = ""
        for family in ("offset", "wordless"):
            sub = [r for r in runs if r.family == family]
            cells += (f"{sum(r.completed for r in sub)}/{len(sub)}" if sub else "-").rjust(12)
        tokens = sum(r.tokens for r in runs)
        total = sum(r.cost for r in runs) + tokens / 1e6 * 3.0
        print(f"{condition:14}{cells}"
              f"{f'{sum(r.completed for r in runs)}/{len(runs)}':>10}{tokens:>9,}{total:>9.4f}")
    print(f"\nwritten to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
