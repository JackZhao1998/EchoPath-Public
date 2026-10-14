"""Dump the accessibility tree of the frontmost app, to see what replay could aim at.

Template matching remembers what a target looked like, which breaks as soon as the
content changes -- a different product, a different filing. The accessibility tree
describes what a target *is* (a row, the first one, inside a table), which is the
thing that stays true. This prints what is actually available so we can tell
whether "the first row of the results" is addressable before building on it.

    python -m echopath.diagnostics.ax_probe            # frontmost app
    python -m echopath.diagnostics.ax_probe Chrome     # a named app
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Optional

import AppKit
import ApplicationServices as AS

# Roles worth reporting: containers that repeat, and things you can click.
INTERESTING = {
    "AXTable", "AXOutline", "AXList", "AXGrid",
    "AXRow", "AXCell", "AXLink", "AXButton",
    "AXTextField", "AXSearchField", "AXPopUpButton", "AXCheckBox",
    "AXRadioButton", "AXMenuItem", "AXStaticText", "AXGroup",
}
# Containers whose children are the repeating structure we care about.
CONTAINERS = {"AXTable", "AXOutline", "AXList", "AXGrid"}

MAX_DEPTH = 14
MAX_CHILDREN = 60


def attr(element: Any, name: str) -> Optional[Any]:
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def describe(element: Any) -> dict:
    role = attr(element, "AXRole") or "?"
    title = attr(element, "AXTitle") or attr(element, "AXDescription") or ""
    value = attr(element, "AXValue")
    text = str(title or (value if isinstance(value, str) else "") or "").strip()
    pos, size = attr(element, "AXPosition"), attr(element, "AXSize")
    return {"role": str(role), "text": text[:70], "pos": pos, "size": size}


def walk(element: Any, depth: int, out: list, path: str = "") -> None:
    if depth > MAX_DEPTH:
        return
    info = describe(element)
    role = info["role"]

    if role in INTERESTING and (info["text"] or role in CONTAINERS or role == "AXRow"):
        out.append((depth, role, info["text"], path))

    children = attr(element, "AXChildren") or []
    # A huge child list is a repeating structure; the first few show its shape,
    # and the count is what matters for "is this addressable by ordinal".
    if role in CONTAINERS and len(children) > 3:
        out.append((depth, f"{role} ▸ {len(children)} children", "", path))
    for index, child in enumerate(list(children)[:MAX_CHILDREN]):
        walk(child, depth + 1, out, f"{path}/{role}[{index}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="*", help="app name (default: frontmost app)")
    args = parser.parse_args()
    wanted = " ".join(args.app).strip()
    workspace = AppKit.NSWorkspace.sharedWorkspace()

    app = None
    for running in workspace.runningApplications():
        name = str(running.localizedName() or "")
        if wanted:
            if wanted.lower() in name.lower():
                app = running
                break
        elif running.isActive():
            app = running
            break

    if app is None:
        print(f"no app found for {wanted!r}")
        return 1

    print(f"app: {app.localizedName()}  pid={app.processIdentifier()}")
    element = AS.AXUIElementCreateApplication(app.processIdentifier())

    # Chrome builds its tree lazily and only when an assistive client asks for it.
    AS.AXUIElementSetAttributeValue(element, "AXManualAccessibility", True)
    time.sleep(1.2)

    windows = attr(element, "AXWindows") or []
    print(f"windows: {len(windows)}")
    if not windows:
        print("  (no windows exposed -- the app may not publish an AX tree)")
        return 1

    out: list = []
    walk(windows[0], 0, out, "")

    if not out:
        print("  (tree is empty)")
        return 1

    print(f"\n{len(out)} elements of interest:\n")
    for depth, role, text, _path in out[:110]:
        print(f"{'  ' * min(depth, 10)}{role:22} {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
