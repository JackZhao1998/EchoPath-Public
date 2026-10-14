"""First-launch macOS desktop-control permission requests.

macOS owns these decisions: EchoPath can request the system prompts and verify
the result, but it can never grant consent itself.  This module intentionally
runs inside EchoPath.app so TCC associates the grants with that bundle.
"""
from __future__ import annotations

import sys


def local_desktop_control_status() -> dict[str, bool]:
    """Return the grants held by the *current* macOS process without prompting."""
    if sys.platform != 'darwin':
        return {'accessibility': False, 'screen_recording': False}

    import ApplicationServices
    import Quartz

    return {
        'accessibility': bool(ApplicationServices.AXIsProcessTrusted()),
        'screen_recording': bool(Quartz.CGPreflightScreenCaptureAccess()),
    }


def request_local_desktop_control() -> dict[str, bool]:
    """Request system-owned prompts and return the immediately known state."""
    if sys.platform != 'darwin':
        return {'accessibility': False, 'screen_recording': False}

    import ApplicationServices
    import Quartz

    permissions = local_desktop_control_status()
    if not permissions['accessibility']:
        ApplicationServices.AXIsProcessTrustedWithOptions({
            ApplicationServices.kAXTrustedCheckOptionPrompt: True,
        })
    if not permissions['screen_recording']:
        # The system displays and owns this prompt. Its result is only final
        # after the user answers and, on some macOS versions, restarts the app.
        Quartz.CGRequestScreenCaptureAccess()
    return local_desktop_control_status()
