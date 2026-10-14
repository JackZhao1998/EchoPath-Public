"""An always-on-top banner saying the desktop is busy, for macOS.

A desktop run takes the mouse and keyboard with nothing on screen to say so.
Touching the mouse mid-run derails the replay or trips PyAutoGUI's fail-safe, so
the useful signal is simply: is it safe to touch this machine right now.

EchoPath ships an equivalent for Windows in ``desktop_overlay.py``; that one is
gated to win32 and drives Tkinter, which Homebrew Python does not install. This
uses PyObjC, which is already a dependency, and asks Cocoa for a window that
never becomes key and ignores the mouse, so it cannot take focus away from the
app being driven.

    python -m echopath.desktop_status "Running Stocks demo"
    python -m echopath.desktop_status --progress local_data/eval_progress.json

With --progress it polls a JSON file and shows how far through a batch it is and
how long is left, which is the difference between a wait you can plan around and
one you just have to sit through. The file is written by whatever is running the
batch and looks like:

    {"total": 100, "completed": 34, "started_at": 1790000000.0,
     "label": "sort / coffee filters", "condition": "ax+jev"}

Runs until killed. The banner sits over the menu bar, away from app windows,
because it does appear in the screenshots EchoPath takes and anything it covers
is hidden from visual grounding.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path
from typing import Optional

import AppKit
import Foundation

BAR_HEIGHT = 34.0
BAR_WIDTH = 660.0
PROGRESS_HEIGHT = 3.0


def _format_duration(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class Banner:
    def __init__(self, message: str, progress_path: Optional[Path],
                 stale_after: float = 180.0) -> None:
        self.base_message = message
        self.progress_path = progress_path
        self.stale_after = stale_after
        self.started_at = time.time()
        # One banner per display. NSScreen.mainScreen() is whichever screen
        # holds the key window, not the one the person is looking at, so on a
        # two-display desk a single banner lands on the wrong one about half
        # the time -- and a warning nobody sees is the same as no warning.
        self.windows = []
        self.labels = []
        self.bars = []
        for screen in AppKit.NSScreen.screens():
            window, label, bar = self._build(screen)
            self.windows.append(window)
            self.labels.append(label)
            self.bars.append(bar)
        self.window = self.windows[0] if self.windows else None

    def shutdown(self, reason: str) -> None:
        """Take the banners down and exit, leaving nothing covering the screen."""
        print(f"desktop_status: exiting ({reason})")
        for window in self.windows:
            window.orderOut_(None)
        AppKit.NSApp().terminate_(None)

    def _build(self, screen) -> tuple:
        frame = screen.frame()
        # Cocoa's origin is bottom-left, and a second display sits at its own
        # offset in that shared space -- so the origin has to be added, or every
        # screen's banner is drawn on the primary, stacked on top of each other.
        rect = Foundation.NSMakeRect(
            frame.origin.x + (frame.size.width - BAR_WIDTH) / 2.0,
            frame.origin.y + frame.size.height - BAR_HEIGHT,
            BAR_WIDTH,
            BAR_HEIGHT,
        )
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
        )
        # Above normal windows and the menu bar, but below system alerts.
        window.setLevel_(AppKit.NSStatusWindowLevel)
        window.setOpaque_(False)
        window.setBackgroundColor_(AppKit.NSColor.clearColor())
        window.setIgnoresMouseEvents_(True)
        window.setHasShadow_(True)
        # Stay put across Spaces, since a run may switch apps.
        window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )

        view = AppKit.NSView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, BAR_WIDTH, BAR_HEIGHT)
        )
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.70, 0.13, 0.09, 0.95
            ).CGColor()
        )
        view.layer().setCornerRadius_(8.0)

        label = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(12, PROGRESS_HEIGHT + 5, BAR_WIDTH - 24, 20)
        )
        label.setStringValue_(self.base_message)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setTextColor_(AppKit.NSColor.whiteColor())
        label.setAlignment_(AppKit.NSTextAlignmentCenter)
        label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13.0))
        view.addSubview_(label)

        # A fill along the bottom edge: the count says where you are, this says
        # it at a glance from across the room.
        progress = AppKit.NSView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, 0, PROGRESS_HEIGHT)
        )
        progress.setWantsLayer_(True)
        progress.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 1.0, 1.0, 0.85
            ).CGColor()
        )
        view.addSubview_(progress)

        window.setContentView_(view)
        window.orderFrontRegardless()
        return window, label, progress

    def refresh(self) -> None:
        """Re-read the progress file and update the bar. Never raises."""
        if self.progress_path is None:
            return

        # A banner that outlives its run covers the screen until someone
        # notices and kills it by hand. Whoever started it is meant to stop it,
        # but that promise is broken by a crash or a kill -9, so the banner
        # also watches for its run going quiet and stands itself down.
        if self.stale_after > 0:
            try:
                idle = time.time() - self.progress_path.stat().st_mtime
            except OSError:
                idle = time.time() - self.started_at
            if idle > self.stale_after:
                self.shutdown("progress went stale")
                return

        try:
            data = json.loads(self.progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The writer may be mid-write, or not started yet. Keep the last
            # good text rather than flickering an error onto the screen.
            return

        total = int(data.get("total") or 0)
        completed = int(data.get("completed") or 0)
        if total <= 0:
            return
        if completed >= total:
            self.shutdown("batch finished")
            return

        parts = [f"case {min(completed + 1, total)}/{total}"]

        started_at = float(data.get("started_at") or 0.0)
        if started_at and completed > 0:
            per_case = (time.time() - started_at) / completed
            parts.append(f"~{_format_duration(per_case * (total - completed))} left")
        elif started_at:
            parts.append("estimating…")

        for key in ("condition", "label"):
            value = str(data.get(key) or "").strip()
            if value:
                parts.append(value)

        text = "EchoPath  ·  " + "  ·  ".join(parts)
        width = BAR_WIDTH * min(1.0, completed / total)
        for label in self.labels:
            label.setStringValue_(text)
        for bar in self.bars:
            bar.setFrame_(Foundation.NSMakeRect(0, 0, width, PROGRESS_HEIGHT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="*", help="what is running")
    parser.add_argument("--progress", help="JSON file holding batch progress")
    parser.add_argument("--stale-after", type=float, default=180.0,
                        help="quit if the progress file stops being written (0 disables)")
    args = parser.parse_args()

    task = " ".join(args.task).strip() or "task"
    message = f"EchoPath is driving the screen  ·  {task}"
    progress_path = Path(args.progress).expanduser() if args.progress else None

    app = AppKit.NSApplication.sharedApplication()
    # Accessory keeps it out of the Dock and stops it stealing focus on launch.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    banner = Banner(message, progress_path, stale_after=args.stale_after)

    def quit_now(_signum, _frame):
        banner.window.orderOut_(None)
        AppKit.NSApp().terminate_(None)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, quit_now)
    signal.signal(signal.SIGINT, quit_now)

    # Cocoa's run loop blocks the main thread, and Python only runs a signal
    # handler between bytecodes on that thread -- so without something to wake
    # the loop, a SIGTERM is delivered and never acted on, and the banner
    # outlives the run it was announcing. This timer both wakes the loop and
    # refreshes the progress text.
    Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        0.25, True, lambda _timer: banner.refresh()
    )

    try:
        app.run()
    except SystemExit:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
