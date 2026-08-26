#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSPECTOR="$REPO_ROOT/scripts/product/inspect_cavise_map_bundle.py"
ROI_CONFIG="$REPO_ROOT/network/config/customer_map_roi.yaml"
PREPARED_BASE="$REPO_ROOT/.external/cavise_maps"
TOWN13_ARCHIVE="CAVISE_SIONNA_Town13_EditorLOD0_Full_Official_20260731.zip"

mode=""
allow_large_extract=false
verify_all=false
bundle_filter=""

usage() {
  printf '%s\n' \
    "Usage: $0 --metadata-only [--bundle FILE] [--verify-all]" \
    "       $0 --prepare-selected [--allow-large-extract] [--verify-all]" \
    "" \
    "CAVISE_MAPS_DIR must point to the external directory containing CAVISE ZIPs" \
    "or already extracted Town directories. Large assets remain outside Git."
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --metadata-only|--prepare-selected)
      [[ -z "$mode" ]] || fail "choose exactly one mode"
      mode="$1"
      ;;
    --allow-large-extract)
      allow_large_extract=true
      ;;
    --verify-all)
      verify_all=true
      ;;
    --bundle)
      shift
      (($#)) || fail "--bundle requires a canonical ZIP filename"
      bundle_filter="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
  shift
done

[[ -n "$mode" ]] || {
  usage >&2
  exit 2
}
[[ -n "${CAVISE_MAPS_DIR-}" ]] || fail \
  "CAVISE_MAPS_DIR is not set; place $TOWN13_ARCHIVE there and export CAVISE_MAPS_DIR"
[[ -d "$CAVISE_MAPS_DIR" ]] || fail "CAVISE_MAPS_DIR is not a directory: $CAVISE_MAPS_DIR"

maps_root="$(cd "$CAVISE_MAPS_DIR" && pwd -P)"
[[ "$maps_root" != / ]] || fail "CAVISE_MAPS_DIR must not be the filesystem root"
mkdir -p "$PREPARED_BASE"

find_archives() {
  local pattern='CAVISE_SIONNA_Town*_EditorLOD0_Full_Official_*.zip'
  if [[ -n "$bundle_filter" ]]; then
    [[ "$bundle_filter" != */* ]] || fail "--bundle accepts a filename, not a path"
    pattern="$bundle_filter"
  fi
  find "$maps_root" -maxdepth 3 -type f -name "$pattern" -print0 | sort -z
}

verify_archive() {
  local archive="$1"
  python3 - "$archive" <<'PY'
import hashlib
import re
import stat
import sys
import zipfile
from pathlib import PurePosixPath

archive = sys.argv[1]
with zipfile.ZipFile(archive, "r", allowZip64=True) as source:
    infos = [info for info in source.infolist() if not info.is_dir()]
    indexed = {info.filename.lstrip("/"): info for info in infos}
    sums = [info for info in infos if PurePosixPath(info.filename).name == "SHA256SUMS"]
    if not sums:
        raise SystemExit("SHA256SUMS is absent; cannot run --verify-all")
    sums_info = min(sums, key=lambda info: (info.filename.count("/"), len(info.filename)))
    if sums_info.file_size > 16 * 1024 * 1024:
        raise SystemExit("SHA256SUMS exceeds the 16 MiB safety limit")
    base = str(PurePosixPath(sums_info.filename).parent)
    base = "" if base == "." else base.rstrip("/") + "/"
    checks = []
    for line in source.read(sums_info).decode("utf-8-sig").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *](.+)", line.strip())
        if match:
            checks.append((match.group(1).lower(), match.group(2).lstrip("./")))
    if not checks:
        raise SystemExit("SHA256SUMS contains no recognized entries")
    for expected, name in checks:
        candidates = [name, base + name]
        info = next((indexed[item] for item in candidates if item in indexed), None)
        if info is None:
            raise SystemExit(f"hash target is absent from archive: {name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SystemExit(f"refusing symlink hash target: {info.filename}")
        digest = hashlib.sha256()
        with source.open(info) as payload:
            for chunk in iter(lambda: payload.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise SystemExit(f"SHA-256 mismatch: {info.filename}")
        print(f"SHA256 OK {info.filename}")
print(f"Verified {len(checks)} archive members.")
PY
}

if [[ "$mode" == --metadata-only ]]; then
  mapfile -d '' -t archives < <(find_archives)
  ((${#archives[@]} > 0)) || fail \
    "no canonical CAVISE ZIP found under CAVISE_MAPS_DIR; place $TOWN13_ARCHIVE there for candidate inspection"
  for archive in "${archives[@]}"; do
    python3 "$INSPECTOR" "$archive"
    if $verify_all; then
      verify_archive "$archive"
    fi
  done
  exit 0
fi

[[ -z "$bundle_filter" ]] || fail "--bundle is only valid with --metadata-only"
[[ -f "$ROI_CONFIG" ]] || fail \
  "no selected ROI exists; place $TOWN13_ARCHIVE in CAVISE_MAPS_DIR, run --metadata-only, then select only from measured metadata"

mapfile -t selected < <(
  python3 - "$ROI_CONFIG" <<'PY'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as source:
    config = yaml.safe_load(source) or {}
selection = config.get("source", {})
fields = (
    selection.get("town"),
    selection.get("bundle_filename"),
    selection.get("scene_xml_relative"),
    selection.get("blend_relative"),
    selection.get("transforms_relative"),
)
if not all(isinstance(value, str) and value for value in fields):
    raise SystemExit("customer_map_roi.yaml has incomplete source paths")
print(*fields, sep="\n")
PY
)
((${#selected[@]} == 5)) || fail "could not resolve selected CAVISE source from $ROI_CONFIG"
town="${selected[0]}"
bundle_filename="${selected[1]}"
scene_relative="${selected[2]}"
blend_relative="${selected[3]}"
transforms_relative="${selected[4]}"

for relative in "$scene_relative" "$blend_relative" "$transforms_relative"; do
  [[ "$relative" != /* && "$relative" != *../* && "$relative" != */.. ]] || \
    fail "selected source path must stay inside the bundle: $relative"
done

scene_in_bundle="${scene_relative#"$town"/}"
blend_in_bundle="${blend_relative#"$town"/}"
transforms_in_bundle="${transforms_relative#"$town"/}"

validate_bundle() {
  local root="$1"
  [[ -f "$root/$scene_in_bundle" ]] || return 1
  [[ -f "$root/$transforms_in_bundle" ]] || return 1
  [[ -f "$root/$blend_in_bundle" ]] || return 1
}

verify_extracted() {
  local root="$1"
  python3 - "$root" <<'PY'
import hashlib
import re
import sys
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve()
manifest = root / "SHA256SUMS"
if not manifest.is_file():
    raise SystemExit(f"SHA256SUMS is absent from extracted bundle: {root}")
if manifest.stat().st_size > 16 * 1024 * 1024:
    raise SystemExit("SHA256SUMS exceeds the 16 MiB safety limit")
checks = []
for line in manifest.read_text(encoding="utf-8-sig").splitlines():
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *](.+)", line.strip())
    if match:
        checks.append((match.group(1).lower(), PurePosixPath(match.group(2).lstrip("./"))))
if not checks:
    raise SystemExit("SHA256SUMS contains no recognized entries")
for expected, relative in checks:
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"unsafe hash target: {relative}")
    candidates = [root.joinpath(*relative.parts)]
    if relative.parts and relative.parts[0] == root.name:
        candidates.append(root.joinpath(*relative.parts[1:]))
    target = next((item.resolve() for item in candidates if item.is_file()), None)
    if target is None or (target != root and root not in target.parents):
        raise SystemExit(f"hash target is absent or escapes the bundle: {relative}")
    digest = hashlib.sha256()
    with target.open("rb") as payload:
        for chunk in iter(lambda: payload.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit(f"SHA-256 mismatch: {relative}")
    print(f"SHA256 OK {relative}")
print(f"Verified {len(checks)} extracted files.")
PY
}

prepared_root="$PREPARED_BASE/$town"
if validate_bundle "$prepared_root"; then
  printf 'Selected bundle is already prepared: %s\n' "$prepared_root"
  $verify_all && verify_extracted "$prepared_root"
  exit 0
fi

while IFS= read -r -d '' candidate; do
  if validate_bundle "$candidate"; then
    printf 'Selected extracted bundle is ready outside Git: %s\n' "$candidate"
    $verify_all && verify_extracted "$candidate"
    exit 0
  fi
done < <(find "$maps_root" -maxdepth 3 -type d -name "$town" -print0)

archive=""
while IFS= read -r -d '' candidate; do
  archive="$candidate"
  break
done < <(find "$maps_root" -maxdepth 3 -type f -name "$bundle_filename" -print0)
[[ -n "$archive" ]] || fail \
  "selected ZIP or extracted bundle is absent: $bundle_filename under CAVISE_MAPS_DIR"

python3 "$INSPECTOR" "$archive"
archive_bytes="$(stat -c '%s' "$archive")"
if command -v numfmt >/dev/null 2>&1; then
  archive_human="$(numfmt --to=iec-i --suffix=B "$archive_bytes")"
else
  archive_human="$archive_bytes bytes"
fi
printf 'Selected archive: %s\nArchive size: %s (%s bytes)\n' "$archive" "$archive_human" "$archive_bytes"
$allow_large_extract || fail \
  "full extraction requires explicit --allow-large-extract; no archive content was extracted"
$verify_all && verify_archive "$archive"
[[ ! -e "$prepared_root" ]] || fail \
  "prepared destination exists but is incomplete; inspect it manually: $prepared_root"

temporary_root="$(mktemp -d "$PREPARED_BASE/.extract-$town.XXXXXX")"
cleanup() {
  [[ -n "${temporary_root-}" && -d "$temporary_root" ]] && rm -rf -- "$temporary_root"
}
trap cleanup EXIT
mkdir -p "$temporary_root/content"

python3 - "$archive" "$temporary_root/content" <<'PY'
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive, destination_raw = sys.argv[1:]
destination = Path(destination_raw).resolve()
with zipfile.ZipFile(archive, "r", allowZip64=True) as source:
    for info in source.infolist():
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe ZIP member path: {info.filename}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SystemExit(f"refusing ZIP symlink: {info.filename}")
        target = destination.joinpath(*relative.parts).resolve()
        if target != destination and destination not in target.parents:
            raise SystemExit(f"ZIP member escapes destination: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open(info) as incoming, target.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=8 * 1024 * 1024)
PY

extracted_root="$temporary_root/content/$town"
if ! validate_bundle "$extracted_root"; then
  extracted_root="$temporary_root/content"
fi
validate_bundle "$extracted_root" || fail \
  "extracted archive lacks selected scene.xml, transforms.xml, or Blender artifact"
mv "$extracted_root" "$prepared_root"
validate_bundle "$prepared_root" || fail "prepared bundle validation failed: $prepared_root"
printf 'Prepared selected bundle outside Git: %s\n' "$prepared_root"
