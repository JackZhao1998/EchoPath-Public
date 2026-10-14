"""Small host OS helpers shared by the app and VM workers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def process_alive(pid):
    try:
        pid = int(pid or 0)
        if pid <= 0:
            return False
        if os.name != 'nt':
            try:
                os.kill(pid, 0)
            except PermissionError:
                return True
            return True
        # os.kill is a signal operation on Windows, including signal zero.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied still means it exists.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, TypeError):
        return False


def private_directory(path):
    """Create an owner-only directory before writing VM passwords or bridge keys."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        account = subprocess.check_output(['whoami'], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                        account + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        path.chmod(0o700)
    return path
