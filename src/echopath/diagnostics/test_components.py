"""Checks for the failures that actually happened, so they cannot happen quietly.

Every bug in this work so far failed silently: extraction returned an empty list,
a crop matched the wrong element at 1.00, a banner outlived its run, coordinates
came back in the wrong space. None raised. Each one cost a full eval run to find,
and the run reported zeros as though a method had lost.

These are the cheapest checks that would have caught each of them.

    python -m echopath.diagnostics.test_components            # needs a browser on a results page
    python -m echopath.diagnostics.test_components --quick    # skips the checks that need the network
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Callable, List, Tuple

import cv2
import numpy as np
import pyautogui
from PIL import Image

from echopath.ocr_extractor import front_window_region, read_screen

from echopath.project_paths import project_root

ROOT = project_root()
BROWSER = "Google Chrome"
# The score at which EchoPath treats a template match as a hit.
TEMPLATE_THRESHOLD = 0.74
PASS, FAIL = "PASS", "FAIL"


def osa(script: str) -> str:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True).stdout.strip()


def open_page(url: str, fresh: bool = False) -> None:
    if fresh:
        osa(f'tell application "{BROWSER}" to quit')
        time.sleep(2.0)
        subprocess.Popen(["open", "-na", BROWSER, "--args", "--incognito", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(9.0)
    else:
        osa(f'tell application "{BROWSER}" to set URL of active tab of front window to "{url}"')
        time.sleep(5.0)
    osa(f'tell application "{BROWSER}" to activate')
    osa(f'tell application "{BROWSER}" to set bounds of front window to {{0, 25, 1800, 1100}}')
    time.sleep(1.5)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_screen_space() -> Tuple[str, str]:
    """Screenshots must arrive in the space clicks are issued in.

    A Retina capture is twice the size of the coordinate space PyAutoGUI uses.
    Reading a target off the raw image sends every click to double its position,
    which walks the pointer off-screen and trips the fail-safe.
    """
    logical = pyautogui.size()
    raw = pyautogui.screenshot().size
    screen = read_screen()
    if (screen.width, screen.height) != (logical[0], logical[1]):
        return FAIL, (f"read_screen reports {screen.width}x{screen.height}, "
                      f"clicks use {logical[0]}x{logical[1]}")
    return PASS, f"raw {raw[0]}x{raw[1]} normalised to {logical[0]}x{logical[1]}"


def check_ocr_returns_text() -> Tuple[str, str]:
    """An empty read is the failure mode that scored three evals at zero."""
    screen = read_screen(region=front_window_region(BROWSER))
    if len(screen.regions) < 5:
        return FAIL, f"only {len(screen.regions)} text regions -- page not readable"
    return PASS, f"{len(screen.regions)} regions in {screen.ocr_ms}ms"


def check_rows_are_ordered() -> Tuple[str, str]:
    """Rows must come back top-to-bottom; 'the first result' depends on it."""
    screen = read_screen(region=front_window_region(BROWSER))
    rows = screen.rows(min_regions=1)
    if len(rows) < 3:
        return FAIL, f"only {len(rows)} rows clustered"
    ys = [r.y for r in rows]
    if ys != sorted(ys):
        return FAIL, "rows are not in top-to-bottom order"
    return PASS, f"{len(rows)} rows, correctly ordered"


def check_coords_on_screen() -> Tuple[str, str]:
    """Every click target must land inside the display."""
    width, height = pyautogui.size()
    screen = read_screen(region=front_window_region(BROWSER))
    stray = [r for r in screen.regions
             if not (0 <= r.center[0] <= width and 0 <= r.center[1] <= height)]
    if stray:
        return FAIL, f"{len(stray)} regions centre outside the screen"
    return PASS, f"all {len(screen.regions)} targets within {width}x{height}"


def check_crop_discriminates(quick: bool) -> Tuple[str, str]:
    """A reference crop must identify one page, not match every page.

    This is the bug that made a whole condition score zero while reporting a
    perfect match: the crop was page furniture, so it matched at 1.00 on pages
    it had never seen and the click went somewhere meaningless.
    """
    if quick:
        return "SKIP", "needs the network"

    first = "https://en.wikipedia.org/w/index.php?search=photosynthesis"
    second = "https://en.wikipedia.org/w/index.php?search=tectonics"

    def rows_and_shot(url):
        open_page(url)
        screen = read_screen(region=front_window_region(BROWSER))
        shot = pyautogui.screenshot()
        width, height = pyautogui.size()
        if shot.size != (width, height):
            shot = shot.resize((width, height), Image.LANCZOS)
        return screen.rows_below(int(screen.height * 0.20), min_regions=1), shot

    rows_a, shot_a = rows_and_shot(first)
    rows_b, _shot_b = rows_and_shot(second)
    if not rows_a or not rows_b:
        return FAIL, "no rows on one of the pages"

    # Row zero is page chrome on most sites, so cropping it tests nothing except
    # that chrome repeats. Find content unique to this page -- which is what a
    # recording should anchor to -- and check that crop is specific.
    shared = {r.text.strip().lower() for r in rows_b}
    unique = [r for r in rows_a if r.text.strip().lower() not in shared
              and len(r.text.strip()) > 12]
    if not unique:
        return FAIL, "no row on the first page was unique to it"
    target = unique[0]

    box = (min(r.x for r in target.regions), min(r.y for r in target.regions),
           max(r.x + r.width for r in target.regions),
           max(r.y + r.height for r in target.regions))
    if box[2] - box[0] < 10 or box[3] - box[1] < 8:
        return FAIL, "target too small to crop"
    template = cv2.cvtColor(np.array(shot_a.crop(box).convert("RGB")), cv2.COLOR_RGB2GRAY)

    open_page(second)
    other = pyautogui.screenshot()
    width, height = pyautogui.size()
    if other.size != (width, height):
        other = other.resize((width, height), Image.LANCZOS)
    grey = cv2.cvtColor(np.array(other), cv2.COLOR_RGB2GRAY)
    if template.shape[0] > grey.shape[0] or template.shape[1] > grey.shape[1]:
        return FAIL, "template larger than the screen"
    _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(grey, template, cv2.TM_CCOEFF_NORMED))
    # Judged against the score EchoPath acts on: a crop clearing 0.74 on a page
    # it has never seen is accepted as a hit, and the click lands on whatever
    # happens to sit there.
    if score > TEMPLATE_THRESHOLD:
        return FAIL, (f"even content unique to a page ('{target.text[:26]}') scores "
                      f"{score:.2f} elsewhere, above the {TEMPLATE_THRESHOLD} threshold")
    return PASS, (f"unique content '{target.text[:26]}' scores {score:.2f} "
                  f"elsewhere, under {TEMPLATE_THRESHOLD}")


def check_banner_lifecycle() -> Tuple[str, str]:
    """A banner that outlives its run sits on the screen until killed by hand."""
    progress = ROOT / "local_data" / "test_progress.json"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text('{"total":10,"completed":1,"started_at":0,"label":"t","condition":"c"}')
    proc = subprocess.Popen(
        [sys.executable, "-m", "echopath.desktop_status", "--progress", str(progress)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    if proc.poll() is not None:
        return FAIL, "banner exited immediately"
    proc.terminate()
    deadline = time.time() + 5
    while time.time() < deadline:
        if proc.poll() is not None:
            leftover = subprocess.run(["pgrep", "-f", "echopath.desktop_status"],
                                      capture_output=True, text=True).stdout.strip()
            if leftover:
                subprocess.run(["pkill", "-9", "-f", "echopath.desktop_status"])
                return FAIL, "a banner process survived"
            return PASS, "starts and stops on request"
        time.sleep(0.25)
    proc.kill()
    return FAIL, "banner ignored terminate"


def check_banner_on_every_screen() -> Tuple[str, str]:
    """With two displays a single banner lands on whichever has focus."""
    import AppKit
    screens = AppKit.NSScreen.screens()
    if len(screens) < 2:
        return "SKIP", "only one display attached"
    # Each screen sits at its own origin in a shared coordinate space; a banner
    # positioned from size alone stacks every copy onto the primary.
    origins = {(s.frame().origin.x, s.frame().origin.y) for s in screens}
    if len(origins) < len(screens):
        return FAIL, "screens report identical origins"
    return PASS, f"{len(screens)} displays at distinct origins"


def check_jev(quick: bool) -> Tuple[str, str]:
    if quick:
        return "SKIP", "needs the network"
    from echopath.jev_client import JevClient, JevError
    try:
        client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
        decision = client.choose(
            instructions="Which option is a fruit?",
            options={"a": "hammer", "b": "banana", "c": "regret"},
            state={"check": "sanity"})
    except JevError as error:
        return FAIL, str(error)[:80]
    if decision.choice != "b":
        return FAIL, f"chose {decision.choice!r} for an unambiguous question"
    return PASS, f"answered in {decision.latency_ms}ms at {decision.confidence:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    if not args.quick:
        open_page("https://en.wikipedia.org/w/index.php?search=photosynthesis", fresh=True)

    checks: List[Tuple[str, Callable[[], Tuple[str, str]]]] = [
        ("screenshot is in click space", check_screen_space),
        ("ocr returns text", check_ocr_returns_text),
        ("rows are ordered top to bottom", check_rows_are_ordered),
        ("click targets are on screen", check_coords_on_screen),
        ("reference crop discriminates", lambda: check_crop_discriminates(args.quick)),
        ("banner starts and stops", check_banner_lifecycle),
        ("banner covers every display", check_banner_on_every_screen),
        ("jev answers", lambda: check_jev(args.quick)),
    ]

    failures = 0
    print(f"{'check':36} {'result':6} detail")
    print("-" * 96)
    for name, run in checks:
        try:
            status, detail = run()
        except Exception as error:
            status, detail = FAIL, f"{type(error).__name__}: {error}"
        if status == FAIL:
            failures += 1
        print(f"{name:36} {status:6} {detail}")
    print("-" * 96)
    print("all checks passed" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
