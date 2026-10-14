# Local GUI Memory Pack

This folder contains local desktop tasks for demonstrating EchoPath replay on a user's own device.
The packaged examples use visible macOS apps, so users can watch each workflow happen on their
screen without preparing starter files. Each task quits its app at the end so another replay can
begin from a clean app state.

## Contents

- `TASKS.md`: the exact prompts and preparation notes.
- `ARTIFACTS.md`: notes about whether starter files are needed.
- `../../memory/consolidated/records/consolidated-local-demo-*.json`: default replay memories.
- `../../memory/consolidated/records/consolidated-local-demo-direct-*.json`: direct replay options.
- `../../memory/consolidated/crops/local-demo-*`: crop assets used by visual GUI replay.

## Suggested Use

1. Pick a task from `TASKS.md`.
2. Make sure the listed app is available locally.
3. Send the exact prompt to EchoPath.
4. Watch the GUI replay and verify the visible result.

For default replay, click targets are visually re-aimed from packaged crops when the task has a
pointer step. The Clock timer example also adapts the hour, minute, second, and wait duration from
the current request. The Weather example adapts the requested location and uses the top Weather
search result without adding it to saved locations. Direct coordinate replay remains available for
comparison by prefixing the exact prompt with `Direct replay: `.
