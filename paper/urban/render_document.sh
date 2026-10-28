#!/usr/bin/env bash
set -euo pipefail
# Invoke from integration under /tmp/bas-paper-runtime.lock; do not acquire a
# second lock here or stop any active simulator/container.
task_urban_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
task_urban_args=()
if [[ $# -gt 0 ]]; then
  task_urban_manifest="$(realpath "$1")"
  [[ "$task_urban_manifest" == "$task_urban_repo/"* ]] || {
    printf 'Manifest must be within mounted repository.\n' >&2; exit 2;
  }
  task_urban_args+=(--manifest "/work/${task_urban_manifest#"$task_urban_repo/"}")
  shift
fi
task_urban_args+=("$@")
docker run --rm --user "$(id -u):$(id -g)" --cpus 1 \
  -e XDG_CACHE_HOME=/tmp/bas-urban-doc-cache \
  -v "$task_urban_repo:/work:ro" -v "$task_urban_repo/paper/urban:/work/paper/urban:rw" \
  -v "$task_urban_repo:$task_urban_repo:ro" \
  bas-paper-doc-tools:20261027 \
  bash -eu -c '
python3 /work/paper/urban/build_document.py "$@"
libreoffice -env:UserInstallation=file:///tmp/bas-urban-paper-lo --headless --convert-to pdf \
  --outdir /work/paper/urban/build /work/paper/urban/build/manuscript_anonymous.docx
' urban-doc-render "${task_urban_args[@]}"
