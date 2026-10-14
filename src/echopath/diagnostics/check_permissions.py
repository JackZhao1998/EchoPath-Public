"""Check whether this process can actually drive the desktop.

Run it from the app whose permissions you want to test:

    .venv/bin/python -m echopath.diagnostics.check_permissions

The functional test is the one that matters. macOS will report a process as
trusted the moment the Accessibility toggle is switched on, but the events it
posts are ignored until the app is relaunched, so the toggle alone proves
nothing.
"""

import argparse
import ctypes
import ctypes.util
import time

import pyautogui
from PIL import Image


def _load(name):
    return ctypes.cdll.LoadLibrary(ctypes.util.find_library(name))


def report_declared_state():
    services = _load("ApplicationServices")
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    trusted = services.AXIsProcessTrusted()

    carbon = _load("Carbon")
    carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
    secure_input = carbon.IsSecureEventInputEnabled()

    print(f"  AXIsProcessTrusted()          : {trusted}")
    print(f"  secure input enabled          : {secure_input}")
    return trusted, secure_input


def report_screen_recording():
    try:
        shot = pyautogui.screenshot()
    except Exception as error:
        print(f"  screenshot                    : FAILED ({error})")
        return False
    black = shot.convert("L").getextrema()[1] < 10
    print(f"  screenshot                    : {shot.size}{' (all black)' if black else ''}")
    return not black


def report_event_delivery():
    """Open the Apple menu and see whether it actually opens.

    Earlier versions hovered the Dock, which only reacts when Dock magnification
    is switched on, so the test reported noise on machines that have it off. The
    Apple menu is always present and always paints a large, obvious panel, which
    makes it a dependable signal on any Mac.
    """
    width, height = pyautogui.size()

    def capture():
        return pyautogui.screenshot().resize((width, height), Image.LANCZOS)

    corner = (0, 0, width // 3, height // 2)

    origin = pyautogui.position()
    changed = 0.0
    try:
        before = capture().crop(corner)
        pyautogui.click(20, 10)
        time.sleep(1.0)
        after = capture().crop(corner)
        changed = ImageChops_difference(before, after)
        pyautogui.press("escape")
        time.sleep(0.3)
    finally:
        pyautogui.moveTo(origin.x, origin.y, duration=0.2)

    print(f"  Apple menu opened             : {changed:.2f}% of that corner changed")
    return changed > 3.0


def ImageChops_difference(before, after):
    import numpy as np

    a = np.asarray(before.convert("L"), dtype=int)
    b = np.asarray(after.convert("L"), dtype=int)
    return (abs(a - b) > 25).mean() * 100


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print("Declared permission state")
    trusted, secure_input = report_declared_state()

    print("\nScreen Recording")
    screen_ok = report_screen_recording()

    print("\nEvent delivery (the test that actually matters)")
    print("  clicking the Apple menu; keep hands off the mouse")
    delivery_ok = report_event_delivery()

    print("\nVerdict")
    if not screen_ok:
        print("  Screen Recording is not working. Grant it, then relaunch this app.")
    if secure_input:
        print("  Secure input is on, which blocks synthetic keystrokes system-wide.")
        print("  It is usually a password field; close it and re-run.")
    if trusted and not delivery_ok:
        print("  Accessibility is granted but events are NOT reaching apps.")
        print("  Quit this app completely (Cmd+Q) and reopen it, then re-run.")
    elif trusted and delivery_ok:
        print("  Accessibility is granted and events are being delivered.")
    elif not trusted:
        print("  Not trusted. Add this app under Privacy & Security > Accessibility.")


if __name__ == "__main__":
    main()
