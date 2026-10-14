"""Small, validated manual-input surface for the VM viewer; no host desktop access."""
from __future__ import annotations

import math


KEYS = frozenset(
    ['enter', 'tab', 'esc', 'backspace', 'delete', 'insert', 'home', 'end', 'pageup', 'pagedown',
     'up', 'down', 'left', 'right', 'space', 'ctrl', 'alt', 'shift', 'command', 'win',
     'capslock', 'printscreen'] + [f'f{i}' for i in range(1, 13)]
    + list('abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;\',./')
)


def validate_input(value):
    if not isinstance(value, dict):
        raise ValueError('Choose a supported VM input action.')
    action = value.get('action')
    result = {'action': action}
    if action in ('click', 'move', 'scroll', 'drag'):
        names = ('x', 'y', 'to_x', 'to_y') if action == 'drag' else ('x', 'y')
        for name in names:
            coordinate = value.get(name)
            if type(coordinate) not in (int, float) or not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
                raise ValueError('Pointer coordinates must be within the VM image.')
            result[name] = coordinate
        if action in ('click', 'drag'):
            result['button'] = value.get('button', 'left')
            if result['button'] not in ('left', 'middle', 'right'):
                raise ValueError('Unsupported mouse button.')
        if action == 'click':
            count = value.get('clicks', 1)
            if type(count) is not int or count not in (1, 2):
                raise ValueError('Choose a single or double click.')
            result['clicks'] = count
        if action == 'scroll':
            count = value.get('clicks')
            if type(count) is not int or not -10 <= count <= 10 or count == 0:
                raise ValueError('Scroll amount must be between 1 and 10 steps.')
            result['clicks'] = count
    elif action == 'text':
        text = value.get('text')
        if not isinstance(text, str) or not text or len(text) > 4000 or '\x00' in text:
            raise ValueError('Send between 1 and 4,000 characters.')
        result['text'] = text
    elif action == 'key':
        keys = value.get('keys')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 4 or any(not isinstance(k, str) or k not in KEYS for k in keys):
            raise ValueError('Unsupported keyboard shortcut.')
        result['keys'] = keys
    else:
        raise ValueError('Unsupported VM input action.')
    return result


def send_input(controller, value):
    action = value['action']
    if action in ('click', 'move', 'scroll', 'drag'):
        size = controller.screen_size_sync()
        width, height = size['width'], size['height']
        if width < 3 or height < 3:
            raise ValueError('The VM desktop dimensions are unavailable.')
        # Logical guest coordinates, independent of Retina pixels and browser zoom.
        # Avoid PyAutoGUI's emergency corner pixels while preserving the edges.
        point = lambda x, y: (max(1, min(width - 2, round(x * (width - 1)))),
                              max(1, min(height - 2, round(y * (height - 1)))))
        x, y = point(value['x'], value['y'])
        if action == 'click':
            controller.click_sync(x=x, y=y, button=value['button'], clicks=value['clicks'], interval=.1 if value['clicks'] == 2 else 0)
        elif action == 'move':
            controller.move_to_sync(x=x, y=y)
        elif action == 'scroll':
            controller.scroll_sync(x=x, y=y, clicks=value['clicks'])
        else:
            to_x, to_y = point(value['to_x'], value['to_y'])
            controller.drag_sync(x=x, y=y, to_x=to_x, to_y=to_y, button=value['button'], duration=.3)
    elif action == 'text':
        controller.paste_text_sync(text=value['text'])
    elif len(value['keys']) == 1:
        controller.press_sync(key=value['keys'][0])
    else:
        controller.hotkey_sync(keys=value['keys'])
