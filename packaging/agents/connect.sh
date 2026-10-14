#!/bin/bash
# Compatibility entry point for an installed source folder.
# Prefer: python3 packaging/setup.py connect --agent codex|claude|both
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || { echo 'Install EchoPath first; see INSTALL.md.' >&2; exit 1; }
if [ "$#" -eq 0 ]; then
  AGENT="${ECHOPATH_AGENT:-}"
  if [ -z "$AGENT" ]; then
    HAS_CODEX=0; HAS_CLAUDE=0
    if command -v codex >/dev/null 2>&1 || [ -x /Applications/Codex.app/Contents/Resources/codex ] || [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then HAS_CODEX=1; fi
    if command -v claude >/dev/null 2>&1; then HAS_CLAUDE=1; fi
    if [ "$HAS_CODEX$HAS_CLAUDE" = 11 ]; then AGENT=both
    elif [ "$HAS_CODEX" = 1 ]; then AGENT=codex
    elif [ "$HAS_CLAUDE" = 1 ]; then AGENT=claude
    else echo 'No coding-agent CLI found. See INSTALL.md.' >&2; exit 1; fi
  fi
  set -- --agent "$AGENT"
fi
exec "$PYTHON" "$ROOT/packaging/setup.py" connect --config "${ECHOPATH_CONFIG:-$ROOT/echopath.local.json}" "$@"
