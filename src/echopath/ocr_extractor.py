"""Read the screen as text with positions, using Apple's Vision framework.

The accessibility tree carries more -- roles, hierarchy, exact control bounds --
but it has to be coaxed out of each app: Chrome builds it lazily and needs a
retry handshake, an app that is not frontmost may collapse it, and every failure
comes back as an empty list rather than an error. OCR asks the screen what it
says and always answers, on any app, including Electron, apps with no
accessibility support, and remote desktops where no such API exists at all.

What it gives up is knowing an element is *clickable*. It reports where text is,
not where the control is, which is fine for links and table rows and wrong for
icon-only buttons.

Rows are recovered geometrically rather than structurally: text that shares a
horizontal band is one row. That recovers "the first result" -- the thing a
replay usually means -- without needing a real table underneath.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from PIL import Image

try:  # pragma: no cover - platform specific
    import Vision
    from Foundation import NSData

    VISION_AVAILABLE = True
except ImportError:  # pragma: no cover
    VISION_AVAILABLE = False


# Text shorter than this is almost always chrome furniture ("OK", "All").
MIN_TEXT_LENGTH = 2
# Two regions belong to the same row when their vertical centres sit within
# this fraction of screen height. Roughly one line of body text.
ROW_BAND = 0.012


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class TextRegion:
    key: str
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float = 0.0

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def describe(self) -> str:
        return self.text.strip() or "(blank)"


@dataclass
class Row:
    """Text regions sharing a horizontal band, read left to right."""

    key: str
    regions: List[TextRegion] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " | ".join(r.text for r in self.regions if r.text.strip())

    @property
    def y(self) -> int:
        return min(r.y for r in self.regions)

    @property
    def center(self) -> Tuple[int, int]:
        # Aim at the first region: in a results row that is the link, whereas
        # the row's midpoint often lands in whitespace between columns.
        return self.regions[0].center if self.regions else (0, 0)

    def describe(self) -> str:
        return self.text[:160]


@dataclass
class ScreenText:
    width: int
    height: int
    regions: List[TextRegion] = field(default_factory=list)
    ocr_ms: int = 0
    capture_ms: int = 0

    def rows(self, *, min_regions: int = 1) -> List[Row]:
        """Group regions into rows by vertical position, top first."""
        ordered = sorted(self.regions, key=lambda r: (r.y, r.x))
        band = max(6, int(self.height * ROW_BAND))
        rows: List[Row] = []
        current: List[TextRegion] = []
        for region in ordered:
            if current and abs(region.y - current[0].y) > band:
                rows.append(Row(f"row_{len(rows)}", sorted(current, key=lambda r: r.x)))
                current = []
            current.append(region)
        if current:
            rows.append(Row(f"row_{len(rows)}", sorted(current, key=lambda r: r.x)))
        return [r for r in rows if len(r.regions) >= min_regions]

    def rows_below(self, y: int, *, min_regions: int = 2) -> List[Row]:
        """Rows beneath a point -- used to skip a page's header furniture."""
        return [r for r in self.rows(min_regions=min_regions) if r.y > y]


def front_window_region(app_name: str) -> Optional[Tuple[int, int, int, int]]:
    """The app's front window as (x, y, width, height), or None.

    Reading the whole screen hands every other window's text to whatever picks
    the target next -- a code editor's contents are not candidates for a click
    on a web page. Cropping to the window is the difference between 88 regions
    of mixed provenance and only the ones that belong to the task.
    """
    import subprocess

    result = subprocess.run(
        ["osascript", "-e",
         f'tell application "{app_name}" to get bounds of front window'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        left, top, right, bottom = [int(v.strip()) for v in result.stdout.split(",")]
    except ValueError:
        return None
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return left, top, width, height


def capture(
    image: Optional[Image.Image] = None,
    region: Optional[Sequence[int]] = None,
) -> Tuple[Image.Image, int, Tuple[int, int]]:
    """Grab the screen (or one window) and report where the crop starts.

    Returns the offset too, so coordinates can be put back into screen space --
    a click has to land on the screen, not inside the crop.
    """
    started = time.perf_counter()
    shot = image
    if shot is None:
        import pyautogui

        shot = pyautogui.screenshot()
        # A live screenshot comes back in physical pixels; clicks are issued in
        # logical points. A supplied image is already in its caller's space
        # (a replay observation, or a VM's screen), so it is left alone.
        width, height = pyautogui.size()
        if shot.size != (width, height):
            shot = shot.resize((width, height), Image.LANCZOS)

    offset = (0, 0)
    if region:
        x, y, w, h = (int(v) for v in region)
        box = (max(0, x), max(0, y), min(shot.width, x + w), min(shot.height, y + h))
        if box[2] - box[0] > 50 and box[3] - box[1] > 50:
            shot = shot.crop(box)
            offset = (box[0], box[1])

    return shot, int((time.perf_counter() - started) * 1000), offset


def read_screen(
    image: Optional[Image.Image] = None,
    *,
    region: Optional[Sequence[int]] = None,
    fast: bool = False,
    min_confidence: float = 0.3,
) -> ScreenText:
    """OCR the screen, or one window, into text regions carrying pixel bounds."""
    if not VISION_AVAILABLE:
        raise OCRUnavailable(
            "PyObjC Vision is unavailable; install pyobjc-framework-Vision."
        )

    shot, capture_ms, offset = capture(image, region)
    buffer = io.BytesIO()
    shot.save(buffer, "PNG")
    payload = buffer.getvalue()
    data = NSData.dataWithBytes_length_(payload, len(payload))

    started = time.perf_counter()
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelFast
        if fast
        else Vision.VNRequestTextRecognitionLevelAccurate
    )
    # Language correction "fixes" product names and tickers into real words,
    # which is exactly the text we need left alone.
    request.setUsesLanguageCorrection_(False)
    handler.performRequests_error_([request], None)
    ocr_ms = int((time.perf_counter() - started) * 1000)

    screen = ScreenText(width=shot.width, height=shot.height,
                        ocr_ms=ocr_ms, capture_ms=capture_ms)

    for index, observation in enumerate(request.results() or []):
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        text = str(best.string() or "").strip()
        if len(text) < MIN_TEXT_LENGTH:
            continue
        confidence = float(best.confidence())
        if confidence < min_confidence:
            continue
        box = observation.boundingBox()
        # Vision normalises to a bottom-left origin; screens are top-left.
        x = int(box.origin.x * screen.width) + offset[0]
        width = int(box.size.width * screen.width)
        height = int(box.size.height * screen.height)
        y = int((1.0 - box.origin.y - box.size.height) * screen.height) + offset[1]
        screen.regions.append(
            TextRegion(f"t_{index}", text, x, y, width, height, confidence)
        )

    return screen
