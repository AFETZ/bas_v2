#!/usr/bin/bash
set -euo pipefail

if (($# != 0)); then
  printf 'FAIL run_five_uav_capacity.sh accepts no positional arguments\n' >&2
  exit 2
fi
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ "$SCRIPT_DIR" == "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR=.
fi
export AMS_FLIGHT_RUN_PROFILE=flight_capacity_prerequisite
exec "$SCRIPT_DIR/run_five_uav_health.sh"
