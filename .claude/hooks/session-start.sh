#!/bin/bash
# SessionStart hook for Claude Code on the web. Installs the app's backend and its test
# dependencies into the app's .venv, so `python -m pytest` works without a manual setup.
# It does nothing on a local machine, which keeps its own environment.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# The app is in the local-agent-workspace folder of this repository. If it is moved to the root, use the root.
APP="$ROOT/local-agent-workspace"
if [ ! -f "$APP/backend/pyproject.toml" ]; then
  APP="$ROOT"
fi
if [ ! -f "$APP/backend/pyproject.toml" ]; then
  echo "session-start: backend/pyproject.toml not found under $ROOT" >&2
  exit 1
fi
cd "$APP"

# backend/pyproject.toml needs Python 3.12 or newer; the container's default python3 may be older.
find_python() {
  for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}
PYTHON="$(find_python)" || {
  echo "session-start: Python 3.12 or newer is required (see backend/pyproject.toml)" >&2
  exit 1
}

# Keep an existing .venv that runs a supported Python. The container state is cached
# after this hook, so later sessions start from the installed packages.
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
  rm -rf .venv
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --quiet --disable-pip-version-check \
  -c constraints.txt -e './backend[test,ui-test]'

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  # Add a line to the session's environment file once, however often the hook runs.
  persist() {
    grep -qxF -- "$1" "$CLAUDE_ENV_FILE" 2>/dev/null || printf '%s\n' "$1" >> "$CLAUDE_ENV_FILE"
  }
  # Make `python` and `pytest` in the session use this environment, as the README's commands expect.
  persist "export VIRTUAL_ENV=\"$APP/.venv\""
  persist "export PATH=\"$APP/.venv/bin:\$PATH\""
  # The frontend tests use the Chromium that the web container provides, so nothing is downloaded.
  browser="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}/chromium"
  if [ -x "$browser" ]; then
    persist "export UI_TEST_CHROMIUM=\"$browser\""
  fi
fi
