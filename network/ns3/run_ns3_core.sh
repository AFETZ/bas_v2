#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
SCENARIO_FILE="${SCENARIO_FILE:-$ROOT_DIR/network/config/scenario_5uav.yaml}"
ENDPOINTS_FILE="${ENDPOINTS_FILE:-$ROOT_DIR/network/config/endpoints.yaml}"
SERVICE_TIERS_FILE="${SERVICE_TIERS_FILE:-$ROOT_DIR/network/config/service_tiers.yaml}"
RADIO_FILE="${RADIO_FILE:-$ROOT_DIR/network/config/radio_24ghz.yaml}"
RADIO_BACKEND_FILE="${RADIO_BACKEND_FILE:-$ROOT_DIR/network/config/radio_backend.yaml}"
JAMMERS_FILE="${JAMMERS_FILE:-$ROOT_DIR/network/config/jammers.yaml}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
ALLOW_MOCK_SIONNA=0
DURATION_S=""
NODE_STATE_FILE="${NODE_STATE_FILE:-}"
PACKET_CORE_MODE="${NS3_PACKET_CORE_MODE:-}"
ORIGINAL_ARGS=("$@")
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
NS3_PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-radio-core.cc"
NS3_COPIED_SOURCE="$NS3_DIR/scratch/ams-radio-core.cc"
NS3_REQUIRED_MODULES="applications,core,csma,flow-monitor,internet,mobility,network,traffic-control"

usage() {
  cat <<'EOF'
Usage: network/ns3/run_ns3_core.sh [options]

Options:
  --mock-sionna        Allow deterministic mock link state when the Sionna TCP provider is unavailable.
  --duration SECONDS   Override ns-3 simulation duration.
  --node-state-file P   Read live node positions from this tracker JSON file.
  --packet-core-mode M  Override packet-core mode. Default comes from radio YAML.
  --run-id ID          Override run id. Also supported through RUN_ID.
  -h, --help           Show this help.

Environment:
  NS3_DIR              External ns-3 checkout/build directory. Default: .external/ns-3
  RUN_DIR              Output directory. Default: runs/<run_id>
  NS3_PACKET_CORE_MODE Packet-core mode override. Implemented: csma_surrogate.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mock-sionna)
      ALLOW_MOCK_SIONNA=1
      shift
      ;;
    --duration)
      DURATION_S="${2:?missing duration value}"
      shift 2
      ;;
    --node-state-file)
      NODE_STATE_FILE="${2:?missing node-state-file value}"
      shift 2
      ;;
    --packet-core-mode)
      PACKET_CORE_MODE="${2:?missing packet-core-mode value}"
      shift 2
      ;;
    --run-id)
      RUN_ID="${2:?missing run id value}"
      RUN_DIR="$ROOT_DIR/runs/$RUN_ID"
      shift 2
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

if [[ ! -d "$NS3_DIR" ]]; then
  printf 'FAIL ns-3 directory missing: %s\n' "$NS3_DIR" >&2
  printf 'Install/build ns-3 under .external/ns-3 or set NS3_DIR. Do not vendor it into source.\n' >&2
  exit 2
fi

if [[ ! -x "$NS3_DIR/ns3" ]]; then
  printf 'FAIL pinned ns-3 CMake launcher is missing: %s/ns3\n' "$NS3_DIR" >&2
  exit 2
fi

NS3_VERSION="$(tr -d '[:space:]' < "$NS3_DIR/VERSION")"
if [[ "$NS3_VERSION" != "3.40" ]]; then
  printf 'FAIL ns-3 VERSION must be exactly 3.40, observed: %s\n' "$NS3_VERSION" >&2
  exit 2
fi
NS3_BINARY="$NS3_DIR/build/scratch/ns${NS3_VERSION}-ams-radio-core-default"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/pcap" "$RUN_DIR/flowmon" "$RUN_DIR/metrics" "$RUN_DIR/ns3"

MODE_ARGS=(
  --radio "$RADIO_FILE"
  --radio-backend-config "$RADIO_BACKEND_FILE"
  --endpoints "$ENDPOINTS_FILE"
  --ns3-dir "$NS3_DIR"
  --purpose runtime
  --json-output "$RUN_DIR/metrics/ns3_packet_core_mode.json"
  --print-mode
)

if [[ -n "$PACKET_CORE_MODE" ]]; then
  MODE_ARGS+=(--mode "$PACKET_CORE_MODE")
fi

PACKET_CORE_MODE="$(python3 "$ROOT_DIR/network/ns3/packet_core_modes.py" "${MODE_ARGS[@]}")"

printf '%q ' "$0" "${ORIGINAL_ARGS[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'ns3_dir=%s\n' "$NS3_DIR"
  printf 'scenario=%s\n' "$SCENARIO_FILE"
  printf 'radio=%s\n' "$RADIO_FILE"
  printf 'radio_backend=%s\n' "$RADIO_BACKEND_FILE"
  printf 'packet_core_mode=%s\n' "$PACKET_CORE_MODE"
  printf 'node_state_file=%s\n' "$NODE_STATE_FILE"
  printf 'allow_mock_sionna=%s\n' "$ALLOW_MOCK_SIONNA"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf unknown)"
} > "$RUN_DIR/environment.txt"

TOPOLOGY_FILE="$RUN_DIR/ns3/topology.txt"
GEN_ARGS=(
  --scenario "$SCENARIO_FILE"
  --endpoints "$ENDPOINTS_FILE"
  --service-tiers "$SERVICE_TIERS_FILE"
  --radio "$RADIO_FILE"
  --radio-backend-config "$RADIO_BACKEND_FILE"
  --jammers "$JAMMERS_FILE"
  --run-id "$RUN_ID"
  --run-dir "$RUN_DIR"
  --output "$TOPOLOGY_FILE"
  --packet-core-mode "$PACKET_CORE_MODE"
)

if [[ -n "$DURATION_S" ]]; then
  GEN_ARGS+=(--duration "$DURATION_S")
fi

if [[ -n "$NODE_STATE_FILE" ]]; then
  GEN_ARGS+=(--node-state-file "$NODE_STATE_FILE")
fi

if (( ALLOW_MOCK_SIONNA )); then
  GEN_ARGS+=(--allow-mock-sionna)
fi

python3 "$ROOT_DIR/network/ns3/generate_ns3_topology.py" "${GEN_ARGS[@]}"
NS3_DIR="$NS3_DIR" "$ROOT_DIR/network/ns3/build_ns3_core.sh"

python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" \
  --program ams-radio-core \
  --project-source "$NS3_PROJECT_SOURCE" \
  --copied-source "$NS3_COPIED_SOURCE" \
  --executable "$NS3_BINARY" \
  --required-modules "$NS3_REQUIRED_MODULES" \
  --copy-to "$RUN_DIR/metrics/ns3_core_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt.log"

LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" \
  "$NS3_BINARY" --topology="$TOPOLOGY_FILE" --runDir="$RUN_DIR" \
  2>&1 | tee "$RUN_DIR/logs/ns3.log"

printf 'ns-3 packet-core run complete: %s\n' "$RUN_DIR"
