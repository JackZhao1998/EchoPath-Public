---
name: command-line
description: Use when an agent in EchoPath needs to run shell commands inside the workspace and capture stdout, stderr, exit codes, and working directories.
---

# Command Line

Use this skill when the task needs terminal execution inside the current workspace.

## Expected setup

- Register this skill with `build_command_line_skill()`.
- Equip the agent with `command-line` before calling `context.use_skill(...)`.
- Commands run through `/bin/zsh` by default unless a different shell is specified.

## Payload shape

```python
{
    "commands": [
        {
            "command": "python3 -m unittest discover -s tests",
            "cwd": ".",
            "timeout_seconds": 120,
            "check": False,
            "result_key": "tests",
        }
    ]
}
```

## Operating guidance

1. Keep commands short, explicit, and workspace-relative where possible.
2. Use `cwd` to scope the command instead of chaining many `cd` calls.
3. Set `check=true` only when a non-zero exit code should fail the whole task.
4. Inspect `stdout`, `stderr`, and `exit_code` in the returned values before deciding the next step.
