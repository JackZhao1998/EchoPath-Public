---
name: echopath
description: "desktop / GUI work on the user's computer or VM → EchoPath runs it as a tracked task, replaying stored procedures when one matches"
---

# EchoPath

EchoPath operates a real desktop (this Mac, or a connected VM) for tasks that need a
GUI: opening apps, clicking through websites, filling forms. It plans with the user's
own model login, replays stored procedures it learned from earlier successful runs, and
tracks every run so the user can pause or stop it from the menu bar or the Control Room
(http://127.0.0.1:8765). The tools come from the `echopath` MCP server; if they are not
available, say so and load the installed `echopath-setup` skill to diagnose the connection. Read
`origin.json` beside this skill to find the installed package and local config.

While this skill is loaded, use EchoPath for desktop and GUI tasks instead of your own
computer-use or browser tools. Work that needs no GUI (files, shell, code) stays yours.

## 1. Check stored procedures first

`echopath_list_procedures` before starting anything. A stored procedure is a recorded,
verified way of doing a task; replaying it is faster and cheaper than planning.

- If an **active** procedure matches, `echopath_get_procedure` for its replay phrase,
  parameters and steps. Start the task with the replay phrase, changing only the values
  that differ (a URL, a search term, a destination) and keeping the rest of the wording.
- Say which procedure you are using. A procedure with `deprecated` or other non-active
  status is not replayed; don't try to force it.
- No match: write the task in plain words, one goal, with a clear stopping point
  ("…and stop at the results page"). EchoPath learns a new procedure if it succeeds.

## 2. Choose where it runs, and get consent for desktop control

`echopath_start_task` takes `target` (`local` or `vm`) and `allow_desktop_control`.

- No VM is connected and the user wants one: use the `echopath-vm-setup` skill first.
- **Always pass `target` explicitly.** Use `vm` when the user mentions a VM or a
  connected VM is available and the task doesn't need their own apps; otherwise ask.
- **`allow_desktop_control` must reflect the user's explicit consent for this task.**
  Without it EchoPath only plans. VM tasks require it. For `local`, tell the user first
  that EchoPath will move their mouse and type on this Mac, and that clicking the
  EchoPath menu-bar icon pauses the run.
- Pass `allow_native_handoff=true` (with desktop control) when replaying a learned
  procedure. If the run stops at `awaiting_native`, `echopath_get_task` shows its
  `native_handoff`: complete only that subgoal on the same desktop, then call
  `echopath_resume_task` with the declared outputs (or `cancel=true` if you cannot).
- Never enter passwords, payment details or personal data through a task, and never
  let a task buy, send, post or delete anything unless the user asked for exactly that.

## 3. Follow the run

`echopath_start_task` returns immediately with the run's id.

- Check progress with `echopath_get_task` every 15–30 seconds, not in a tight loop, and
  give the user a one-line update when the status or step changes.
- `echopath_control_run` with `pause`, `resume` or `stop` when the user asks, or when the
  run is clearly doing the wrong thing. Only `paused` means the desktop is released.
- One run per desktop: if the target is busy, say so and offer to wait, stop the other
  run, or use the other target.

## 4. Report and tidy up

- Report the final status and EchoPath's result text plainly; if it failed, include
  the reason and whether it replayed a procedure or planned live.
- If a stored procedure replayed and did the wrong thing, offer to retire it with
  `echopath_consolidate` (`deprecate_path_id`, plus a short `reason`); do it only when
  the user agrees.
- Don't call `echopath_clear_history` unless the user explicitly asks to wipe history.
- Prefer `echopath_start_task` over `echopath_run_task` for desktop work: it is tracked,
  visible in the Control Room, and can be paused.
