#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_SIONNA_DIR="${NS3_SIONNA_DIR:-$ROOT_DIR/.external/ns-3-sionna}"
RUN_ID="${RUN_ID:-ns3_sionna_rt_live_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
SCENE_XML="${SCENE_XML:-$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/sionna_scene.xml}"
NODE_STATE_FILE="${NODE_STATE_FILE:-}"
TX="${LIVE_RSSI_TX:-uav1}"
RX="${LIVE_RSSI_RX:-uav2}"
DURATION_S="${DURATION_S:-20}"
PERIOD_S="${PERIOD_S:-1}"
FREQUENCY_HZ="${FREQUENCY_HZ:-2400000000}"
TX_POWER_DBM="${TX_POWER_DBM:-33}"
NOISE_FIGURE_DB="${NOISE_FIGURE_DB:-6}"
MAX_DEPTH="${MAX_DEPTH:-0}"
WITH_SETUP="${WITH_SETUP:-1}"
ORIGINAL_ARGS=("$@")

usage() {
  cat <<'EOF'
Usage: network/ns3/run_ns3_sionna_rt_live.sh [options]

Runs an ns-3 scratch program using upstream SionnaRtChannelModel via pybind11.
No TCP Sionna provider is used.

Options:
  --scene PATH              Mitsuba/Sionna XML scene. Default: rock_demo sionna_scene.xml
  --node-state PATH         Live position_tracker node_state.json
  --tx NODE                 Transmitter node id. Default: uav1
  --rx NODE                 Receiver node id. Default: uav2
  --duration SECONDS        Duration. Default: 20
  --period SECONDS          Sionna channel sample/update period. Default: 1
  --frequency HZ            Carrier frequency. Default: 2400000000
  --tx-power-dbm DBM        Tx power. Default: 33
  --noise-figure-db DB      Receiver noise figure. Default: 6
  --max-depth N             Sionna path max depth. Default: 0
  --no-setup                Do not configure/check the ns-3 Sionna checkout first
  -h, --help                Show this help

Environment:
  NS3_SIONNA_DIR, RUN_ID, RUN_DIR, SCENE_XML, NODE_STATE_FILE,
  LIVE_RSSI_TX, LIVE_RSSI_RX, DURATION_S, PERIOD_S
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scene)
      SCENE_XML="${2:?missing scene path}"
      shift 2
      ;;
    --node-state)
      NODE_STATE_FILE="${2:?missing node-state path}"
      shift 2
      ;;
    --tx)
      TX="${2:?missing tx}"
      shift 2
      ;;
    --rx)
      RX="${2:?missing rx}"
      shift 2
      ;;
    --duration)
      DURATION_S="${2:?missing duration}"
      shift 2
      ;;
    --period)
      PERIOD_S="${2:?missing period}"
      shift 2
      ;;
    --frequency)
      FREQUENCY_HZ="${2:?missing frequency}"
      shift 2
      ;;
    --tx-power-dbm)
      TX_POWER_DBM="${2:?missing tx power}"
      shift 2
      ;;
    --noise-figure-db)
      NOISE_FIGURE_DB="${2:?missing noise figure}"
      shift 2
      ;;
    --max-depth)
      MAX_DEPTH="${2:?missing max depth}"
      shift 2
      ;;
    --no-setup)
      WITH_SETUP=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

abspath_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    printf 'FAIL %s file missing: %s\n' "$label" "$path" >&2
    exit 2
  fi
  printf '%s/%s\n' "$(cd "$(dirname "$path")" && pwd -P)" "$(basename "$path")"
}

SCENE_XML="$(abspath_file "$SCENE_XML" "scene")"
if [[ -n "$NODE_STATE_FILE" ]]; then
  NODE_STATE_FILE="$(abspath_file "$NODE_STATE_FILE" "node state")"
fi

mkdir -p "$RUN_DIR"/{logs,metrics,plots}
printf '%q ' "$0" "${ORIGINAL_ARGS[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'ns3_sionna_dir=%s\n' "$NS3_SIONNA_DIR"
  printf 'scene_xml=%s\n' "$SCENE_XML"
  printf 'node_state_file=%s\n' "$NODE_STATE_FILE"
  printf 'tx=%s\n' "$TX"
  printf 'rx=%s\n' "$RX"
  printf 'duration_s=%s\n' "$DURATION_S"
  printf 'period_s=%s\n' "$PERIOD_S"
  printf 'frequency_hz=%s\n' "$FREQUENCY_HZ"
  printf 'tx_power_dbm=%s\n' "$TX_POWER_DBM"
  printf 'noise_figure_db=%s\n' "$NOISE_FIGURE_DB"
  printf 'max_depth=%s\n' "$MAX_DEPTH"
} > "$RUN_DIR/environment.txt"

if (( WITH_SETUP )); then
  "$ROOT_DIR/network/ns3/setup_ns3_sionna_rt.sh" > "$RUN_DIR/logs/setup_ns3_sionna_rt.log" 2>&1
fi

if [[ ! -x "$NS3_SIONNA_DIR/ns3" ]]; then
  printf 'FAIL no ns3 launcher under %s\n' "$NS3_SIONNA_DIR" >&2
  exit 2
fi

cp "$ROOT_DIR/network/ns3/scratch/ams-sionna-rt-live.cc" "$NS3_SIONNA_DIR/scratch/ams-sionna-rt-live.cc"

PY_SITE="$(python3 - <<'PY'
import site
print(site.getusersitepackages())
PY
)"
export PYTHONPATH="$PY_SITE:${PYTHONPATH:-}"
export SIONNA_MITSUBA_VARIANT="${SIONNA_MITSUBA_VARIANT:-llvm_ad_mono_polarized}"

(
  cd "$NS3_SIONNA_DIR"
  ./ns3 build scratch/ams-sionna-rt-live
) > "$RUN_DIR/logs/ns3_sionna_rt_build.log" 2>&1

ARGS=(
  "scratch/ams-sionna-rt-live"
  "--scene=$SCENE_XML"
  "--runDir=$RUN_DIR"
  "--tx=$TX"
  "--rx=$RX"
  "--duration=$DURATION_S"
  "--period=$PERIOD_S"
  "--frequency=$FREQUENCY_HZ"
  "--txPowerDbm=$TX_POWER_DBM"
  "--noiseFigureDb=$NOISE_FIGURE_DB"
  "--maxDepth=$MAX_DEPTH"
)
if [[ -n "$NODE_STATE_FILE" ]]; then
  ARGS+=("--nodeState=$NODE_STATE_FILE")
fi

(
  cd "$NS3_SIONNA_DIR"
  ./ns3 run "${ARGS[*]}"
) 2>&1 | tee "$RUN_DIR/logs/ns3_sionna_rt_live.log"

python3 "$ROOT_DIR/network/scripts/plot_live_radio_csv.py" \
  --input "$RUN_DIR/metrics/ns3_sionna_rt_live.csv" \
  --output "$RUN_DIR/plots/ns3_sionna_rt_live.png" \
  --title "ns-3 pybind11 Sionna RT $TX->$RX"

printf 'ns-3 pybind11 Sionna RT live run complete: %s\n' "$RUN_DIR"
