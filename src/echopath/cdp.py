"""A minimal DevTools Protocol client, so the oracle never touches pixels.

Scoring an in-app click by comparing screenshots would be template matching --
the very mechanism under test. Reading the page's own state instead keeps the
oracle on a channel no resolver can use.
"""
import json, subprocess, time, urllib.request
import websocket


class Page:
    def __init__(self, port=9333, profile=None, headless=False, url="about:blank"):
        self.port = port
        # A fresh profile each run: a directory still held by a stray
        # Chrome makes the new one exit before DevTools ever listens.
        profile = profile or f"/tmp/echopath-cdp-{port}-{int(time.time())}"
        self.log = open(f"{profile}.log", "w+")
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"],
                       capture_output=True)
        time.sleep(1)
        args = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate", "--window-size=1500,975",
                # Without this Chrome may open on a second display, where
                # screen coordinates go negative and the mouse cannot follow.
                "--window-position=0,25", url]
        if headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=self.log)
        self.ws = None
        for _ in range(40):
            try:
                self._connect(); break
            except Exception:
                time.sleep(0.5)
        else:
            self.log.seek(0)
            raise RuntimeError("could not reach DevTools\n" + self.log.read()[-600:])
        self._id = 0

    def _connect(self):
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json"))
        page = [t for t in tabs if t["type"] == "page"][0]
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=25)

    def send(self, method, **params):
        self._id = getattr(self, "_id", 0) + 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def on_primary(self):
        """True when the window sits where the mouse and screenshots can reach."""
        x, y = self.js("[window.screenX, window.screenY]") or [0, 0]
        return x >= 0 and y >= 0

    def goto(self, url, wait=6.0):
        self.send("Page.navigate", url=url)
        time.sleep(wait)

    def js(self, expression):
        r = self.send("Runtime.evaluate", expression=expression,
                      returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
