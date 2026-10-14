#!/bin/bash
# EchoPath installer for macOS.
#
# Builds EchoPath.app on this Mac rather than shipping a prebuilt binary: the
# launcher is compiled and signed locally, so macOS attributes Accessibility and
# Screen Recording to EchoPath itself, and Gatekeeper has nothing to block.
#
# Run from a checkout to install that checkout, or on its own to fetch one.
#
# Environment overrides:
#   ECHOPATH_SRC_DIR     source location when fetching  (default ~/.echopath/src)
#   ECHOPATH_REPO        GitHub owner/name               (default JackZhao1998/EchoPath)
#   ECHOPATH_REF         branch or tag                   (default main)
#   ECHOPATH_APP_DEST    where EchoPath.app goes         (default /Applications)
#   ECHOPATH_BACKEND     codex or claude (auto-detected when omitted)
#   ECHOPATH_AGENT       codex, claude, both or none (default none)
#   ECHOPATH_SKIP_LAUNCH=1  build but do not open the app

set -euo pipefail

REPO="${ECHOPATH_REPO:-JackZhao1998/EchoPath}"
REF="${ECHOPATH_REF:-main}"
APP_DEST="${ECHOPATH_APP_DEST:-/Applications}"

step() { printf '\n\033[1;34m◉ %s\033[0m\n' "$*"; }
note() { printf '\033[0;36m   %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m ✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "The EchoPath app is macOS-only. Use the CLI and web Control Room elsewhere."

# ---------------------------------------------------------------- clang
if ! xcode-select -p >/dev/null 2>&1; then
  warn "Xcode Command Line Tools are needed to build EchoPath's launcher."
  warn "A dialog should appear now; rerun this installer when it finishes."
  xcode-select --install >/dev/null 2>&1 || true
  fail "Command Line Tools missing."
fi

# ---------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (manages Python and packages)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installation failed. Install it from https://docs.astral.sh/uv and rerun."
fi

# ---------------------------------------------------------------- source
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/../../pyproject.toml" ] && grep -q 'name = "echopath-core"' "$HERE/../../pyproject.toml"; then
  SRC_DIR="$(cd "$HERE/../.." && pwd)"
  step "Installing from this checkout"
  note "$SRC_DIR"
else
  SRC_DIR="${ECHOPATH_SRC_DIR:-$HOME/.echopath/src}"
  step "Getting the EchoPath source"
  mkdir -p "$(dirname "$SRC_DIR")"
  if [ -d "$SRC_DIR/.git" ]; then
    note "keeping the existing checkout at $SRC_DIR (no branch changes or automatic pull)"
  else
    git clone --quiet --depth 1 -b "$REF" "https://github.com/$REPO.git" "$SRC_DIR"
    note "cloned to $SRC_DIR"
  fi
fi
cd "$SRC_DIR"

# ---------------------------------------------------------------- python env
step "Setting up Python"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 --seed .venv --quiet
fi
# ---------------------------------------------------------------- shared setup
BACKEND="${ECHOPATH_BACKEND:-}"
if [ -z "$BACKEND" ] && [ -f echopath.local.json ]; then
  BACKEND="$(.venv/bin/python - <<'PYTHON'
import json
from pathlib import Path
try:
    command = json.loads(Path('echopath.local.json').read_text()).get('external_command', {}).get('command', [])
    script = Path(command[1]).name if len(command) == 2 else ''
    print({'run_codex_cli.py': 'codex', 'run_claude_code.py': 'claude'}.get(script, ''))
except (ValueError, TypeError):
    print('')
PYTHON
)"
fi
if [ -z "$BACKEND" ]; then
  if command -v codex >/dev/null 2>&1 || [ -x /Applications/Codex.app/Contents/Resources/codex ] || [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then BACKEND=codex
  elif command -v claude >/dev/null 2>&1; then BACKEND=claude
  else fail "Install and sign in to Codex or Claude Code first; see INSTALL.md."; fi
fi
step "Installing EchoPath and verifying MCP"
.venv/bin/python packaging/setup.py install --backend "$BACKEND" --agent "${ECHOPATH_AGENT:-none}" --app --app-destination "$APP_DEST"

if [ "${ECHOPATH_SKIP_LAUNCH:-0}" != "1" ]; then
  open "$APP_DEST/EchoPath.app"
fi

step "Installed. Open EchoPath.app when you are ready."
cat <<'TXT'
   Before a task can use the desktop, allow EchoPath in
   System Settings → Privacy & Security:

     1. Accessibility     — so it can click and type
     2. Screen Recording  — so it can see the screen

   EchoPath appears in each list by name. After allowing them, quit
   EchoPath from its menu-bar popover and open it again once.

   Control Room:  http://127.0.0.1:8765
   Logs:          ~/.echopath/app.log

   To use EchoPath from Claude Code or Codex (/echopath):
     .venv/bin/python packaging/setup.py connect --agent codex
   Choose --agent claude or --agent both when appropriate.
   Verify the model login: .venv/bin/python packaging/setup.py doctor --model
TXT
