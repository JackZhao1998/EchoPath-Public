"""macOS menu-bar status item with a popover of run controls; no overlay over the browser being operated."""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from urllib.parse import urlencode

from .run_state import ACTIVE


def vm_viewer_url(runner, url, run_id):
    """Open the selected task's VM through the local viewer, never an arbitrary URL."""
    from .dashboard import validate_endpoint
    endpoint = runner.registry.get(run_id)['target']
    validate_endpoint(endpoint)
    return url.rstrip('/') + '/vm?' + urlencode({'endpoint': endpoint})


def status_title(runs, seen, now):
    """Short menu-bar label: elapsed time while working, otherwise the state that needs attention."""
    active = [r for r in runs if r['status'] in ACTIVE]
    if active:
        states = {r['status'] for r in active}
        if 'resetting' in states:
            return 'Resetting VM'
        if 'awaiting_native' in states:
            return 'Waiting'
        if states == {'paused'}:
            return 'Paused'
        if 'pausing' in states:
            return 'Pausing'
        running = [r for r in active if r['status'] == 'running']
        if running:
            elapsed = int(now - min(r['started'] for r in running))
            return f'{elapsed // 60}:{elapsed % 60:02d}'
        return 'Stopping'
    last = runs[0] if runs else None
    if last and last['id'] not in seen:
        if last['status'] == 'completed' and now - last['updated'] < 60:
            return 'Done'
        if last['status'] in ('error', 'interrupted'):
            return 'Failed' if last['status'] == 'error' else 'Interrupted'
    return ''


def run_tray(runner, url):
    if sys.platform == 'win32':
        from .windows_tray import run_tray as windows_tray
        return windows_tray(runner, url)
    if sys.platform != 'darwin':
        raise RuntimeError('The menu-bar app currently supports macOS. Use the web Control Room on other platforms.')
    try:
        from WebKit import WKWebView, WKWebViewConfiguration
    except ImportError as error:
        raise RuntimeError('The menu-bar app needs PyObjC WebKit: pip install "echopath-core[macos]".') from error
    from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyAccessory, NSEvent, NSEventTypeApplicationDefined, NSFont, NSImage, NSImageLeft,
                        NSMinYEdge, NSPopover, NSPopoverBehaviorTransient, NSScreen, NSStatusBar,
                        NSVariableStatusItemLength, NSViewController)
    from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL, NSURLRequest
    import objc

    class TrayDelegate(NSObject, protocols=[objc.protocolNamed('WKScriptMessageHandler')]):
        @objc.python_method
        def setup(self):
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            button = self.item.button()
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_('dot.radiowaves.right', 'EchoPath')
            if image:
                image.setTemplate_(True)
                button.setImage_(image)
                button.setImagePosition_(NSImageLeft)
            else:
                button.setTitle_('◉')
            button.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(NSFont.systemFontSize(), 0))
            button.setTarget_(self)
            button.setAction_('toggle:')
            config = WKWebViewConfiguration.alloc().init()
            config.userContentController().addScriptMessageHandler_name_(self, 'echopath')
            self.web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 360, 560), config)
            self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url + '/tray')))
            controller = NSViewController.alloc().init()
            controller.setView_(self.web)
            self.popover = NSPopover.alloc().init()
            self.popover.setContentSize_(NSMakeSize(360, 560))
            self.popover.setBehavior_(NSPopoverBehaviorTransient)
            self.popover.setContentViewController_(controller)
            self.popover.setDelegate_(self)
            # Old failures should not nag on launch; only runs that end from now on do.
            self.seen = {r['id'] for r in runner.registry.list() if r['status'] not in ACTIVE}
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(.6, self, 'refresh:', None, True)
            NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
            self.refresh_(None)

        def toggle_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
                return
            # Opening the controls is itself a request to yield the host screen.
            runs = runner.registry.list()
            for run in runs:
                if run['target'] == 'local' and run['status'] == 'running':
                    try:
                        runner.registry.request(run['id'], 'pause')
                    except ValueError:
                        pass
            self.seen.update(r['id'] for r in runs if r['status'] not in ACTIVE)
            button = self.item.button()
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, NSMinYEdge)
            self.web.evaluateJavaScript_completionHandler_('window.trayOpened && window.trayOpened()', None)
            self.refresh_(None)

        def popoverDidClose_(self, notification):
            # The hidden page stops polling until the popover opens again.
            self.web.evaluateJavaScript_completionHandler_('window.trayClosed && window.trayClosed()', None)

        def userContentController_didReceiveScriptMessage_(self, controller, message):
            body = str(message.body())
            if body == 'quit':
                app = NSApplication.sharedApplication()
                app.stop_(None)
                # stop: takes effect after the next event; a script message is not one, so post one.
                wake = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    NSEventTypeApplicationDefined, NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
                app.postEvent_atStart_(wake, True)
            elif body.startswith('open:vm:'):
                try:
                    viewer_url = vm_viewer_url(runner, url, body[len('open:vm:'):])
                except ValueError as error:
                    self.web.evaluateJavaScript_completionHandler_('showError(' + json.dumps(str(error)) + ')', None)
                    return
                self.popover.performClose_(None)
                webbrowser.open(viewer_url, new=1)
            elif body in ('open', 'open:config') or (body.startswith('open:#') and body[6:].replace('-', '').replace('=', '').isalnum()):
                self.popover.performClose_(None)
                webbrowser.open(url + '/' + body[5:], new=1 if body == 'open:config' else 0)

        def refresh_(self, timer):
            runs = runner.registry.list()
            active = [r for r in runs if r['status'] in ACTIVE]
            title = status_title(runs, self.seen, time.time())
            self.item.button().setTitle_(' ' + title if title else '')
            self.item.button().setToolTip_('\n'.join(f"{r['status'].title()} · {r['detail'][:160]}" for r in active) or 'EchoPath · no active runs')
            # Publish only geometry, never pixels. The screenshot producer masks this area.
            window = self.item.button().window()
            if window:
                rect = window.frame()
                height = NSScreen.screens()[0].frame().size.height
                geometry = {'pid': os.getpid(), 'rect': [rect.origin.x, height-rect.origin.y-rect.size.height, rect.size.width, rect.size.height]}
                path = runner.registry.root / 'tray.json'
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(geometry))
                temporary.replace(path)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    from .macos_permissions import request_local_desktop_control
    permissions = request_local_desktop_control()
    runner.enable_local_app_host()
    if not all(permissions.values()):
        alert = NSAlert.alloc().init()
        alert.setMessageText_('Enable local desktop control')
        alert.setInformativeText_(
            'EchoPath requested Accessibility and Screen Recording permission. '
            'Approve any macOS prompts, then quit and reopen EchoPath. Keep EchoPath open: '
            'Codex sends local desktop tasks through this authorized app.'
        )
        alert.addButtonWithTitle_('OK')
        alert.runModal()
    delegate = TrayDelegate.alloc().init()
    delegate.setup()
    try:
        app.run()
    finally:
        delegate.timer.invalidate()
        NSStatusBar.systemStatusBar().removeStatusItem_(delegate.item)
        (runner.registry.root / 'tray.json').unlink(missing_ok=True)


def mask_controls(image, region, logical_size):
    """Mask EchoPath's status item and menu without changing pixel coordinates."""
    if sys.platform != 'darwin':
        return image
    from .run_state import current_control
    control = current_control.get()
    if control is None:
        return image
    path = control.registry.root / 'tray.json'
    if not path.exists():
        return image
    from PIL import ImageDraw
    import Quartz
    geometry = json.loads(path.read_text())
    try:
        os.kill(geometry['pid'], 0)
    except ProcessLookupError:
        return image
    rects = [geometry['rect']]
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for window in windows:
        if window.get('kCGWindowOwnerPID') == geometry['pid']:
            bounds = window['kCGWindowBounds']
            rects.append([bounds['X'], bounds['Y'], bounds['Width'], bounds['Height']])
    origin_x, origin_y, width, height = region or (0, 0, logical_size[0], logical_size[1])
    sx, sy = image.width / width, image.height / height
    draw = ImageDraw.Draw(image)
    for x, y, w, h in rects:
        draw.rectangle(((x-origin_x)*sx, (y-origin_y)*sy, (x+w-origin_x)*sx, (y+h-origin_y)*sy), fill='#101724')
    return image
