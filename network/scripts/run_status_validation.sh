#!/usr/bin/bash
set -euo pipefail

# Canonical host launcher for live milestone authority.  Resolve the project
# root with shell builtins before replacing the environment; the Python/YAML
# import set is independently bound by m0_execution_policy.
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ "$SCRIPT_DIR" == "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR=.
fi
cd -- "$SCRIPT_DIR/../.."
ROOT_DIR="$PWD"
exec /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  LANG=C.UTF-8 \
  LC_ALL=C \
  HOME=/nonexistent \
  PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$ROOT_DIR:/usr/local/lib/python3.10/dist-packages:/usr/lib/python3/dist-packages" \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
  GIT_NO_REPLACE_OBJECTS=1 \
  GIT_OPTIONAL_LOCKS=0 \
  /usr/bin/python3.10 -S network/scripts/validate_status_documents.py "$@"
