#!/usr/bin/env bash
set -euo pipefail
# Invoke under /tmp/bas-paper-runtime.lock from the integration worktree.
task_paper_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker run --rm --user "$(id -u):$(id -g)" --cpus 1 \
  -e MPLCONFIGDIR=/tmp/bas-paper-mpl \
  -v "$task_paper_root:/work/paper" bas-paper-doc-tools:20261027 \
  bash -eu -c '
python3 /work/paper/tools/make_figures.py
python3 /work/paper/tools/build_docx.py
libreoffice -env:UserInstallation=file:///tmp/bas-paper-lo --headless --convert-to pdf --outdir /work/paper/build /work/paper/build/manuscript_anonymous.docx
'
