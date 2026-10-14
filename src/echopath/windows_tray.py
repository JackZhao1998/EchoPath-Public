"""Windows notification-area controls for the shared browser Control Room."""
from __future__ import annotations

import threading
import time
import webbrowser
from urllib.parse import urlencode

from .run_state import ACTIVE


def run_tray(runner, url):
    try:
        import pystray
    except ImportError:
        raise RuntimeError('Windows tray support needs: pip install "echopath-core[windows]"') from None
    from PIL import Image, ImageDraw
    from .tray import status_title
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill='#2167df')
    for box in ((19, 16, 26, 48), (26, 16, 46, 23), (26, 29, 42, 36), (26, 41, 46, 48)):
        draw.rectangle(box, fill='white')
    stopped = threading.Event()
    runner.enable_local_app_host()

    def safely(action):
        def invoke(icon, item):
            try:
                action()
            except (ValueError, OSError, RuntimeError) as error:
                icon.notify(str(error), 'EchoPath')
        return invoke

    def controls(path=''):
        # Opening host controls must not compete with an active local GUI task.
        paused = []
        for run in runner.registry.list():
            if run['target'] == 'local' and run['status'] in ('queued', 'running'):
                runner.registry.request(run['id'], 'pause')
                paused.append(run['id'])
        deadline = time.monotonic() + 15
        while paused and time.monotonic() < deadline:
            paused = [run['id'] for run in runner.registry.list() if run['id'] in paused and run['status'] in ('queued', 'running', 'pausing')]
            if paused:
                time.sleep(.1)
        if paused:
            raise ValueError('Waiting for the local task to pause. Open controls again after it reaches a safe stopping point.')
        webbrowser.open(url.rstrip('/') + '/' + path)

    def run_menu():
        for run in runner.registry.list():
            if run['status'] not in ACTIVE:
                continue
            run_id = run['id']
            items = []
            if run['status'] == 'paused':
                items.append(pystray.MenuItem('Resume', safely(lambda rid=run_id: runner.registry.request(rid, 'resume'))))
            elif run['status'] in ('queued', 'running', 'pausing'):
                items.append(pystray.MenuItem('Pause for manual control', safely(lambda rid=run_id: runner.registry.request(rid, 'pause'))))
            items.append(pystray.MenuItem('Stop', safely(lambda rid=run_id: runner.registry.request(rid, 'stop'))))
            yield pystray.MenuItem(run['status'].title() + ' · ' + run['task'][:70], pystray.Menu(*items))
        yield pystray.MenuItem('Open task controls', safely(controls))

    def desktop():
        state = runner.settings.vm.status()
        if not state['connected']:
            raise ValueError('Connect the VM in Configuration first.')
        webbrowser.open(url.rstrip('/') + '/vm?' + urlencode({'endpoint': state['endpoint']}))

    def quit_controls(icon, item):
        stopped.set()
        icon.stop()

    icon = pystray.Icon('EchoPath', image, 'EchoPath', pystray.Menu(
        pystray.MenuItem('Open Control Room', safely(controls), default=True),
        pystray.MenuItem('Configuration', safely(lambda: controls('config'))),
        pystray.MenuItem('Tasks', pystray.Menu(lambda: tuple(run_menu()))),
        pystray.MenuItem('Open VM desktop', safely(desktop)),
        pystray.MenuItem('Open VM window', safely(runner.settings.vm.open_window),
                         visible=lambda item: runner.settings.vm.config()['profile'] is not None and runner.settings.vm.profile()['mode'] == 'virtualbox'),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit controls (tasks keep running)', quit_controls),
    ))

    def setup(icon):
        icon.visible = True
        known = {r['id']: r['status'] for r in runner.registry.list()}
        while not stopped.wait(1):
            try:
                runs = runner.registry.list()
                title = status_title(runs, set(), time.time())
                icon.title = 'EchoPath' + (' · ' + title if title else '')
                for run in runs:
                    if known.get(run['id']) != run['status'] and run['status'] in ('completed', 'error', 'interrupted'):
                        icon.notify(run['task'][:180], 'EchoPath · ' + run['status'].title())
                latest = {r['id']: r['status'] for r in runs}
                if latest != known:
                    icon.update_menu()
                    known = latest
            except (OSError, ValueError, RuntimeError):
                pass  # A transient database/config read should not close controls.
    try:
        icon.run(setup=setup)
    finally:
        stopped.set()
