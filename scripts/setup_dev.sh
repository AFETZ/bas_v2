#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3.10}"
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 10), "Python 3.10 required"'
VENV="$PWD/.external/dev-venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 10), "Python 3.10 required"'
"$VENV/bin/python" -m pip install --disable-pip-version-check -r .devcontainer/requirements-dev.lock
"$VENV/bin/python" -m pip check
printf 'Ready: source .external/dev-venv/bin/activate\n'
