"""A per-run Windows status window, isolated from the MCP server's event loop."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path


class DesktopOverlay:
    def __init__(self):
        self.process = None
        self.stopped = threading.Event()
        self.replies = queue.Queue()

    def start(self):
        if sys.platform != "win32":
            return
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--window"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
            self._request("show", text="Preparing desktop task")
        except (OSError, RuntimeError):
            self.close()

    def _read(self, process):
        for line in process.stdout:
            if line.strip() == "stop":
                self.stopped.set()
            else:
                self.replies.put(line.strip())

    def _request(self, command, **fields):
        if self.process is None:
            return
        try:
            self.process.stdin.write(json.dumps({"command": command, **fields}) + "\n")
            self.process.stdin.flush()
            if self.replies.get(timeout=2) != "ok":
                raise RuntimeError("Overlay did not acknowledge its state")
        except (OSError, ValueError, queue.Empty, RuntimeError):
            # Kill a stalled window before allowing an action to continue.
            self.close()

    def check(self):
        if self.stopped.is_set():
            raise RuntimeError("Desktop task stopped by the user from the EchoPath overlay.")

    def update(self, text):
        self.check()
        self._request("update", text=text)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


def _window():
    import ctypes
    import time
    import tkinter as tk
    from ctypes import wintypes

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.configure(background="#172435")
    width, height = 460, 64
    root.geometry(f"{width}x{height}+{max(0, (root.winfo_screenwidth()-width)//2)}+8")
    title = tk.Label(root, text="●  EchoPath is operating your computer",
                     bg="#172435", fg="#edf4ff", font=("Segoe UI", 11, "bold"))
    title.place(x=14, y=8)
    detail = tk.Label(root, bg="#172435", fg="#b6c7dd", font=("Segoe UI", 9))
    detail.place(x=14, y=35)
    started = time.monotonic()
    status = "Preparing desktop task"
    stopped = False

    def stop():
        nonlocal stopped, status
        stopped = True
        status = "Stopping — waiting for the current action"
        button.config(state="disabled", text="Stopping")
        print("stop", flush=True)

    button = tk.Button(root, text="Stop", command=stop, takefocus=False,
                       bg="#b63c49", fg="white", relief="flat", font=("Segoe UI", 10))
    button.place(x=378, y=16, width=70, height=32)
    root.update_idletasks()
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    hwnd = user32.GetParent(root.winfo_id())
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    # Tool window + NOACTIVATE: no taskbar entry, and clicking Stop preserves focus.
    style = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
    commands = queue.Queue()

    def read_commands():
        for line in sys.stdin:
            commands.put(json.loads(line))
        commands.put({"command": "close"})  # Parent exited; never leave an orphan window.

    threading.Thread(target=read_commands, daemon=True).start()

    def tick():
        nonlocal status
        while not commands.empty():
            message = commands.get_nowait()
            command = message["command"]
            if command == "close":
                root.destroy()
                return
            if not stopped:
                status = message.get("text", status)
            if command == "show":
                user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0013)
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            root.update_idletasks()
            ctypes.windll.dwmapi.DwmFlush()
            print("ok", flush=True)
        elapsed = int(time.monotonic() - started)
        detail.config(text=f"{status[:53]}  ·  {elapsed//60}:{elapsed%60:02d}")
        root.after(40, tick)

    root.after(0, tick)
    root.mainloop()


if __name__ == "__main__":
    _window()
