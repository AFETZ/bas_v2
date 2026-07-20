#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
NS3_URL="${NS3_URL:-https://www.nsnam.org/releases/ns-allinone-3.40.tar.bz2}"
NS3_ARCHIVE_SHA256="${NS3_ARCHIVE_SHA256:-c0ba395b6fcb084c4d43d6117b28932f716b26aebb54498ce2f44c0c39be3e60}"

if [[ -e "$NS3_DIR" ]]; then
  if [[ -f "$NS3_DIR/VERSION" ]] && [[ "$(tr -d '[:space:]' < "$NS3_DIR/VERSION")" == "3.40" ]]; then
    printf 'ns-3 3.40 source already exists: %s\n' "$NS3_DIR"
    exit 0
  fi
  printf 'FAIL existing ns-3 path is not the pinned 3.40 tree: %s\n' "$NS3_DIR" >&2
  exit 2
fi

mkdir -p "$(dirname "$NS3_DIR")"
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
archive="$temporary_dir/ns-allinone-3.40.tar.bz2"

curl -fsSL --retry 3 "$NS3_URL" -o "$archive"
printf '%s  %s\n' "$NS3_ARCHIVE_SHA256" "$archive" | sha256sum -c -
tar -xjf "$archive" -C "$temporary_dir" ns-allinone-3.40/ns-3.40
mv "$temporary_dir/ns-allinone-3.40/ns-3.40" "$NS3_DIR"
printf 'Installed pinned ns-3 3.40 source: %s\n' "$NS3_DIR"
