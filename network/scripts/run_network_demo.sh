#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCENARIO_FILE="${SCENARIO_FILE:-$ROOT_DIR/network/config/scenario_5uav.yaml}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/pcap" "$RUN_DIR/flowmon" "$RUN_DIR/heatmaps" "$RUN_DIR/metrics"

printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'scenario=%s\n' "$SCENARIO_FILE"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf unknown)"
  printf 'kernel=%s\n' "$(uname -a)"
} > "$RUN_DIR/environment.txt"

printf 'Run directory: %s\n' "$RUN_DIR"
printf 'Running preflight dependency check...\n'

if ! "$ROOT_DIR/network/scripts/check_deps.sh" 2>&1 | tee "$RUN_DIR/logs/check_deps.log"; then
  {
    printf 'Network demo preflight failed.\n'
    printf 'Missing dependencies are listed in logs/check_deps.log.\n'
    printf 'No partial simulator launch was attempted, to avoid bypassing ns-3/radio isolation.\n'
  } | tee "$RUN_DIR/logs/launch.log"
  exit 2
fi

pending_components=()
for component in network/bridge network/ns3 network/radio_provider network/position_tracker; do
  if [[ ! -d "$ROOT_DIR/$component" ]]; then
    pending_components+=("$component")
  fi
done

if (( ${#pending_components[@]} > 0 )); then
  {
    printf 'Full network demo is not launchable yet.\n'
    printf 'The following integration components are still pending:\n'
    printf '  - %s\n' "${pending_components[@]}"
    printf '\n'
    printf 'Intended base simulation command once the packet path exists:\n'
    printf 'ros2 launch multiagent_simulation multiagent_simulation.launch.py robots_config_file:=%q gui:=false rviz:=false use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false\n' "$SCENARIO_FILE"
    printf '\n'
    printf 'This command intentionally refuses to run a partial demo that could use direct SITL/MAVLink bypass paths.\n'
  } | tee "$RUN_DIR/logs/launch.log"
  exit 3
fi

export RUN_ID RUN_DIR
exec "$ROOT_DIR/network/scripts/run_sim_2_4ghz_loop.sh"
