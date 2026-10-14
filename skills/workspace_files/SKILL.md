---
name: workspace-files
description: Use when an agent in EchoPath needs to read, write, append, list, or edit files inside the current workspace.
---

# Workspace Files

Use this skill when the task needs structured file operations inside the repo.

## Expected setup

- Register this skill with `build_workspace_files_skill()`.
- Equip the agent with `workspace-files` before calling `context.use_skill(...)`.
- Paths are resolved relative to the agent working directory and must stay inside that workspace.

## Payload shape

```python
{
    "actions": [
        {"action": "read", "path": "README.md", "result_key": "readme"},
        {"action": "write", "path": "notes/plan.txt", "content": "draft", "result_key": "note"},
        {
            "action": "replace_text",
            "path": "src/example.py",
            "old_text": "old value",
            "new_text": "new value",
            "replace_all": False,
            "result_key": "edit",
        },
    ]
}
```

## Supported actions

- `read`
- `write`
- `append`
- `replace_text`
- `list_dir`

## Operating guidance

1. Read the file before editing it when the exact contents matter.
2. Use `replace_text` for deterministic edits instead of overwriting full files unnecessarily.
3. Keep paths relative to the workspace root unless an absolute path inside the workspace is required.
