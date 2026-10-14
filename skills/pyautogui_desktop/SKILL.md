---
name: pyautogui-desktop
description: Use when a EchoPath agent needs desktop-level GUI observation and control through pyautogui, including screenshots, mouse movement, clicking, typing, key presses, and scrolling.
---

# PyAutoGUI Desktop Control

Use this skill when the task needs to observe the current desktop and perform GUI actions through the visible screen, mouse, and keyboard.

## Expected setup

- Register a `PyAutoGUIController` instance as a service, usually under the name `desktop`.
- Register this skill with `build_pyautogui_desktop_skill()`.
- Equip the agent with `pyautogui-desktop` before calling `context.use_skill(...)`.
- On macOS, grant Accessibility and Screen Recording permissions to the Python process before running live GUI control.

## Payload shape

Pass an `actions` list and optionally a `service_name`.

Example payload:

```python
{
    "service_name": "desktop",
    "actions": [
        {"action": "activate_app", "app_name": "Finder"},
        {"action": "observe", "path": "local_data/desktop_observations/page.png"},
        {"action": "move", "x": 640, "y": 320, "duration": 0.2},
        {"action": "click", "x": 640, "y": 320},
        {"action": "type", "text": "asthma"},
        {"action": "press", "key": "enter"},
    ],
}
```

Supported actions:

- `observe`
- `activate_app`
- `move`
- `click`
- `type`
- `press`
- `hotkey`
- `scroll`
- `wait`
- `position`
- `screen_size`

## Operating guidance

1. Capture an observation before the first GUI action.
2. Keep action batches small so the agent can re-evaluate after each step.
3. Prefer explicit coordinates only when the target window is frontmost and stable.
4. Use `wait` sparingly and prefer a new observation over blind delays.
5. Treat this as a high-impact capability because it controls the real mouse and keyboard.
