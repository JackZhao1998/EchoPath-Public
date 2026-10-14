# Release 0.1.0

Target branch: **`windows-version` (Windows)**. Package version: **0.1.0**.
This document prepares a release; it does not announce a published release.

## Automated verification

From the source root:

```powershell
python packaging/release_check.py --output dist
```

The check parses packaged Python/JSON, validates local documentation targets and
anchors, checks curated memory/catalog/crop consistency, rejects excluded files,
builds and extracts a source ZIP, verifies its manifest/checksum, and compares
the `--destination` copy with the archive contents. Only explicitly approved
memory/media assets are bundled; local user state stays excluded.

GitHub Actions runs these checks on Windows and Linux.
It installs dependencies from the extracted archive and invokes
`packaging/verify.py` against its public config, listing MCP tools and calling
the smoke check without a model request or desktop actions. The workflow saves
the source ZIP and SHA-256 checksum as review artifacts; it does not publish
a GitHub release. A workflow file alone is not evidence that its jobs passed.

## Manual checks before publication

- [ ] Install this branch from a fresh permanent folder on Windows with a
  signed-in model CLI; build/start the native app and restart the coding agent.
- [ ] Run `packaging/setup.py doctor --model`, then call `echopath_smoke_check`
  through the restarted host. Confirm the chosen model login works.
- [ ] Open the Start menu shortcut and confirm tray controls on an unlocked Windows desktop.
- [ ] Run one small authorized desktop task; verify Pause, Resume, Stop, history,
  learning, and replay on the current app versions and screen layout.
- [ ] If distributing VM support, create/connect a guest and confirm visible
  control and manual takeover. Verify the documented platform limitations.
- [ ] Review README video playback and captions while signed out after the
  repository is public. The shared media and branding links require `main` to remain available.
- [ ] Keep provisional savings visibly qualified until reproducible benchmarks
  support them; the recorded demos are not performance measurements.
- [ ] Review the manual guest-agent authentication defaults before release.
  Manual startup permits missing keys; this cleanup does not change that code.
- [ ] Confirm current checks are green, inspect the final diff and package,
  and choose release tags and visibility explicitly.

For separate branch tags, use `v0.1.0` for `main` and `windows-v0.1.0`
for `windows-version`. Both variants declare the same package version; install
one source tree per environment. No tag or visibility change is part of these
preparation commands.
