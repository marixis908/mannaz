#!/bin/bash
# SessionStart: przygotowanie sesji Claude Code w chmurze (claude.ai/code).
# Lokalnie nic nie robi. Tworzy .venv (Python 3.14, uv) i instaluje
# requirements.txt z wersjami z constraints.txt (B-53). Bazy NIE uruchamia:
# w chmurze nie ma danych finansowych, wiec testy z markerem `db` sa
# pomijane (decyzja 2026-10-05).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 14))'; then
  uv venv --quiet --clear --python 3.14 .venv
fi
VIRTUAL_ENV="$PWD/.venv" uv pip install --quiet -r requirements.txt -c constraints.txt

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$PWD/.venv\""
    echo "export PATH=\"$PWD/.venv/bin:\$PATH\""
    echo "export PYTHONPATH=\"$PWD/src\""
  } >> "$CLAUDE_ENV_FILE"
fi
