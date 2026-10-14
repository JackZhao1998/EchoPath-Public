"""Read the screen as a list of typed elements instead of a picture.

Replay currently remembers what a target looked like and finds it again by pixel
correlation. That holds for fixed interface furniture and breaks the moment the
target *is* the data -- a different product, a different filing -- because the
picture changes even though the role does not.

The accessibility tree describes what an element *is*: a row, the first one,
inside a table. That survives a content change. This module turns the frontmost
window into candidates carrying role, text and bounds, which serves two callers:

* ordinal addressing, when the structure is unambiguous (a real table) -- free
  and deterministic, no model involved;
* a decision model, when it is not (a page of nested groups), which needs the
  candidates as options to choose between.

macOS only. Chrome publishes its web tree only when launched with
``--force-renderer-accessibility``; without it you get the browser toolbar and
nothing of the page.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:  # pragma: no cover - platform specific
    import AppKit
    import ApplicationServices as AS

    AX_AVAILABLE = True
except ImportError:  # pragma: no cover
    AX_AVAILABLE = False


# Containers whose children form a repeating structure worth addressing by index.
REPEATING_CONTAINERS = {"AXTable", "AXOutline", "AXList", "AXGrid"}
# Roles a pointer action plausibly targets.
CLICKABLE = {
    "AXLink", "AXButton", "AXRow", "AXCell", "AXMenuItem",
    "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXTextField",
    "AXSearchField", "AXStaticText", "AXImage",
}

MAX_DEPTH = 20
MAX_CHILDREN_PER_NODE = 80
DEFAULT_MAX_CANDIDATES = 120


class AccessibilityUnavailable(RuntimeError):
    pass


@dataclass
class Candidate:
    """One addressable element on screen."""

    key: str
    role: str
    text: str
    x: int
    y: int
    width: int
    height: int
    role_path: str
    depth: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def describe(self) -> str:
        """One line for a human or a decision model: what it is, then what it says."""
        label = self.text.strip() or "(no text)"
        return f"{self.role} — {label}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "role": self.role,
            "text": self.text,
            "role_path": self.role_path,
            "bounds": {"x": self.x, "y": self.y, "w": self.width, "h": self.height},
        }


@dataclass
class ScreenStructure:
    app_name: str
    candidates: List[Candidate] = field(default_factory=list)
    containers: List[Candidate] = field(default_factory=list)

    def by_key(self, key: str) -> Optional[Candidate]:
        return next((c for c in self.candidates if c.key == key), None)

    def rows_of_largest_table(self) -> List[Candidate]:
        """Children of the biggest repeating container, header row dropped.

        A results table's first AXRow is usually the column headings, which is
        never the thing a workflow meant to click.
        """
        if not self.containers:
            return []
        best = max(self.containers, key=lambda c: c.height * c.width)
        rows = [
            c for c in self.candidates
            if c.role == "AXRow" and c.role_path.startswith(best.role_path)
        ]
        rows.sort(key=lambda c: c.y)
        if len(rows) > 1 and _looks_like_header(rows[0], rows[1:]):
            return rows[1:]
        return rows


def _looks_like_header(first: Candidate, rest: Sequence[Candidate]) -> bool:
    """A header repeats its words in no other row and sits above all of them."""
    if not rest:
        return False
    if first.y > min(r.y for r in rest):
        return False
    words = {w for w in first.text.lower().split() if len(w) > 3}
    if not words:
        return False
    shared = sum(1 for r in rest[:5] if words & set(r.text.lower().split()))
    return shared == 0


# A menu that happens to be open is listed among an app's windows, and Chrome's
# menus carry thousands of items. Walking one finds no page content at all, so
# these are skipped rather than searched.
MENU_ROLES = {"AXMenu", "AXMenuBar", "AXMenuItem", "AXMenuBarItem"}


def _attr(element: Any, name: str) -> Optional[Any]:
    error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def _usable_windows(element: Any) -> list:
    """The app's real windows, biggest first, with menus excluded.

    AXWindows is not ordered front-to-back and includes any open menu, so taking
    the first entry sometimes hands back a menu bar holding four thousand items
    and no page. Ranking by area puts the document window first.
    """
    focused = _attr(element, "AXFocusedWindow")
    windows = list(_attr(element, "AXWindows") or [])
    if focused is not None:
        windows.insert(0, focused)

    usable = []
    for window in windows:
        role = str(_attr(window, "AXRole") or "")
        if role in MENU_ROLES:
            continue
        _, _, width, height = _point_size(window)
        if width > 200 and height > 200:
            usable.append((width * height, window))
    usable.sort(key=lambda pair: -pair[0])
    if usable:
        return [window for _area, window in usable]

    # Chrome does not always hand back window elements here -- AXWindows can
    # come back holding the application itself, with no frame. Walking the
    # application is equivalent for our purposes: the menus are skipped during
    # traversal, so what is left below it is the window and its web content.
    return [element]


def _point_size(element: Any) -> tuple[int, int, int, int]:
    """AXPosition/AXSize come back as opaque AXValues; unwrap to plain numbers."""
    x = y = w = h = 0
    position, size = _attr(element, "AXPosition"), _attr(element, "AXSize")
    if position is not None:
        ok, point = AS.AXValueGetValue(position, AS.kAXValueCGPointType, None)
        if ok:
            x, y = int(point.x), int(point.y)
    if size is not None:
        ok, extent = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        if ok:
            w, h = int(extent.width), int(extent.height)
    return x, y, w, h


def _text_of(element: Any, depth: int = 0) -> str:
    """An element's own label, else the text of the descendants that carry it."""
    for name in ("AXTitle", "AXDescription", "AXValue"):
        value = _attr(element, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if depth >= 4:
        return ""
    parts: List[str] = []
    for child in (_attr(element, "AXChildren") or [])[:12]:
        piece = _text_of(child, depth + 1)
        if piece and piece not in parts:
            parts.append(piece)
        if len(" | ".join(parts)) > 140:
            break
    return " | ".join(parts)[:160]


def extract(
    app_name: Optional[str] = None,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    min_text_length: int = 0,
) -> ScreenStructure:
    """Walk the frontmost (or named) app's front window into candidates."""
    if not AX_AVAILABLE:
        raise AccessibilityUnavailable(
            "PyObjC ApplicationServices is unavailable; install "
            "pyobjc-framework-ApplicationServices."
        )

    matches = []
    for running in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        name = str(running.localizedName() or "")
        if app_name:
            if app_name.lower() in name.lower():
                matches.append(running)
        elif running.isActive():
            matches.append(running)
    if not matches:
        raise AccessibilityUnavailable(f"no running app matched {app_name!r}")

    # A relaunched app can leave its predecessor around for a moment, and the
    # one still shutting down publishes no windows. Taking the first match makes
    # extraction intermittently return nothing, so prefer an instance that
    # actually has a window.
    element = None
    windows: list = []
    app = matches[0]
    for candidate_app in matches:
        candidate = AS.AXUIElementCreateApplication(candidate_app.processIdentifier())
        # Chrome builds its web tree lazily, and only for a client that asks.
        AS.AXUIElementSetAttributeValue(candidate, "AXManualAccessibility", True)
        time.sleep(0.35)
        candidate_windows = _usable_windows(candidate)
        if candidate_windows:
            element, windows, app = candidate, candidate_windows, candidate_app
            break
        if element is None:
            element = candidate

    structure = ScreenStructure(app_name=str(app.localizedName() or ""))
    if not windows:
        return structure

    counter = {"n": 0}

    def walk(node: Any, depth: int, path: str) -> None:
        # The cap limits how many candidates we keep, never how far we walk:
        # stopping the traversal at the cap means anything later in the tree --
        # including the results table, which tends to sit below a page of
        # navigation -- is never seen at all.
        if depth > MAX_DEPTH:
            return
        role = str(_attr(node, "AXRole") or "")
        if role in MENU_ROLES:
            return
        x, y, w, h = _point_size(node)
        here = f"{path}/{role}"

        if role in REPEATING_CONTAINERS:
            structure.containers.append(
                Candidate(f"container_{len(structure.containers)}", role, "", x, y, w, h, here, depth)
            )

        if role in CLICKABLE and w > 0 and h > 0 and counter["n"] < max_candidates:
            text = _text_of(node)
            if len(text) >= min_text_length:
                key = f"elem_{counter['n']}"
                counter["n"] += 1
                structure.candidates.append(
                    Candidate(key, role, text, x, y, w, h, here, depth)
                )

        for index, child in enumerate((_attr(node, "AXChildren") or [])[:MAX_CHILDREN_PER_NODE]):
            walk(child, depth + 1, f"{here}[{index}]")

    walk(windows[0], 0, "")
    return structure
