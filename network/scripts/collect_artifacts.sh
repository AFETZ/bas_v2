#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FORCE=0
RUN_DIR=""

usage() {
  cat <<'EOF'
Usage: network/scripts/collect_artifacts.sh [--force] runs/<run_id>

Creates:
  artifacts/customer_delivery_<run_id>/
  artifacts/customer_delivery_<run_id>.tar.gz

The bundle records missing proof explicitly. It does not convert an incomplete
validation run into customer-ready status.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -n "$RUN_DIR" ]]; then
        printf 'FAIL only one run directory may be provided\n' >&2
        usage >&2
        exit 2
      fi
      RUN_DIR="$1"
      shift
      ;;
  esac
done

if [[ -z "$RUN_DIR" ]]; then
  printf 'FAIL run directory is required\n' >&2
  usage >&2
  exit 2
fi

case "$RUN_DIR" in
  /*) ;;
  *) RUN_DIR="$ROOT_DIR/$RUN_DIR" ;;
esac

if [[ ! -d "$RUN_DIR" ]]; then
  printf 'FAIL run directory not found: %s\n' "$RUN_DIR" >&2
  exit 2
fi

RUN_ID="$(basename "$RUN_DIR")"
ARTIFACTS_DIR="$ROOT_DIR/artifacts"
BUNDLE_NAME="customer_delivery_$RUN_ID"
BUNDLE_DIR="$ARTIFACTS_DIR/$BUNDLE_NAME"
ARCHIVE_PATH="$ARTIFACTS_DIR/$BUNDLE_NAME.tar.gz"

if [[ -e "$BUNDLE_DIR" || -e "$ARCHIVE_PATH" ]]; then
  if (( FORCE == 1 )); then
    rm -rf "$BUNDLE_DIR" "$ARCHIVE_PATH"
  else
    printf 'FAIL bundle already exists: %s\n' "$BUNDLE_DIR" >&2
    printf 'Use --force to replace it.\n' >&2
    exit 2
  fi
fi

mkdir -p "$BUNDLE_DIR/selected_logs" "$BUNDLE_DIR/selected_pcap" "$BUNDLE_DIR/heatmaps" "$BUNDLE_DIR/metrics"

copy_if_present() {
  local source_rel="$1"
  local dest_rel="$2"
  local source="$RUN_DIR/$source_rel"
  local dest="$BUNDLE_DIR/$dest_rel"

  if [[ -e "$source" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$source" "$dest"
    printf 'present\t%s\n' "$source_rel" >> "$BUNDLE_DIR/manifest.tsv"
  else
    printf 'missing\t%s\n' "$source_rel" >> "$BUNDLE_DIR/manifest.tsv"
  fi
}

: > "$BUNDLE_DIR/manifest.tsv"

copy_if_present "validation_report.md" "validation_report.md"
copy_if_present "metrics/summary.json" "metrics/summary.json"
copy_if_present "metrics/queues.csv" "metrics/queues.csv"
copy_if_present "metrics/links.csv" "metrics/links.csv"
copy_if_present "metrics/live_sinr.csv" "metrics/live_sinr.csv"
copy_if_present "metrics/live_sinr_summary.json" "metrics/live_sinr_summary.json"
copy_if_present "metrics/ns3_link_states.csv" "metrics/ns3_link_states.csv"
copy_if_present "metrics/ns3_flow_rates.csv" "metrics/ns3_flow_rates.csv"
copy_if_present "metrics/traffic_classes.csv" "metrics/traffic_classes.csv"
copy_if_present "logs/long_run_status.json" "selected_logs/long_run_status.json"
copy_if_present "logs/long_run_resume_command.sh" "selected_logs/long_run_resume_command.sh"
copy_if_present "logs/launch.log" "selected_logs/launch.log"
copy_if_present "logs/validation.log" "selected_logs/validation.log"
copy_if_present "logs/check_deps.log" "selected_logs/check_deps.log"
copy_if_present "logs/no_bypass.log" "selected_logs/no_bypass.log"
copy_if_present "logs/no_bypass_active.log" "selected_logs/no_bypass_active.log"
copy_if_present "logs/bridge.jsonl" "selected_logs/bridge.jsonl"
copy_if_present "logs/ns3.log" "selected_logs/ns3.log"
copy_if_present "logs/sionna_link_queries.jsonl" "selected_logs/sionna_link_queries.jsonl"
copy_if_present "logs/live_sinr_queries.jsonl" "selected_logs/live_sinr_queries.jsonl"
copy_if_present "logs/live_sinr_monitor.log" "selected_logs/live_sinr_monitor.log"
copy_if_present "logs/timing.jsonl" "selected_logs/timing.jsonl"
copy_if_present "pcap/control.pcap" "selected_pcap/control.pcap"
copy_if_present "pcap/payload.pcap" "selected_pcap/payload.pcap"
copy_if_present "pcap/additional_data.pcap" "selected_pcap/additional_data.pcap"
copy_if_present "flowmon/flowmon.xml" "metrics/flowmon.xml"
copy_if_present "heatmaps/rss.png" "heatmaps/rss.png"
copy_if_present "heatmaps/sinr.png" "heatmaps/sinr.png"
copy_if_present "heatmaps/js.png" "heatmaps/js.png"
copy_if_present "heatmaps/degradation_zone.png" "heatmaps/degradation_zone.png"
copy_if_present "heatmaps/service_tier.png" "heatmaps/service_tier.png"
copy_if_present "plots/live_sinr.png" "plots/live_sinr.png"

CUSTOMER_READY="false"
if [[ -f "$RUN_DIR/metrics/summary.json" ]] && command -v python3 >/dev/null 2>&1; then
  CUSTOMER_READY="$(python3 - "$RUN_DIR/metrics/summary.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    print("false")
else:
    print("true" if data.get("customer_ready") is True and data.get("p0_passed") is True else "false")
PY
)"
fi

{
  printf '# Customer Delivery Bundle\n\n'
  printf 'Run ID: `%s`\n\n' "$RUN_ID"
  printf 'Customer-ready status: **%s**.\n\n' "$([[ "$CUSTOMER_READY" == "true" ]] && printf ready || printf 'not ready')"
  printf 'This bundle was generated from `%s` by `network/scripts/collect_artifacts.sh`.\n\n' "${RUN_DIR#$ROOT_DIR/}"
  printf 'Start with `validation_report.md` and `metrics/summary.json`. Missing files are listed in `manifest.tsv` and `known_limitations.md`.\n'
} > "$BUNDLE_DIR/README.md"

{
  printf '# Run Instructions\n\n'
  printf 'From the repository root:\n\n'
  printf '```bash\n'
  printf './network/scripts/check_deps.sh\n'
  printf './network/scripts/run_network_demo.sh\n'
  printf './network/scripts/run_validation.sh --run-dir runs/%s\n' "$RUN_ID"
  printf './network/scripts/collect_artifacts.sh runs/%s\n' "$RUN_ID"
  printf '```\n\n'
  printf 'For the optional P1 long-run gate, use the generated template when present:\n\n'
  printf '```bash\n'
  printf 'bash artifacts/customer_delivery_%s/selected_logs/long_run_resume_command.sh\n' "$RUN_ID"
  printf '```\n\n'
  printf 'That template runs the 30-minute gate and re-validates with `--long-run required`.\n\n'
  printf 'The demo must not be represented as customer-ready unless `metrics/summary.json` has both `p0_passed: true` and `customer_ready: true`.\n'
} > "$BUNDLE_DIR/run_instructions.md"

{
  printf '# Architecture\n\n'
  printf 'The intended packet-in-the-loop path is:\n\n'
  printf '```text\n'
  printf 'Ground control namespace -> radio endpoint bridge -> ns-3 real-time packet core -> online Sionna RT provider -> UAV endpoint bridge -> ArduPilot SITL/HITL\n'
  printf '```\n\n'
  printf 'The current acceptance backend is `sim_2_4ghz`. The future `real_modem_2_4ghz` backend must reuse the same endpoint, traffic-class, metrics, and artifact contracts.\n'
} > "$BUNDLE_DIR/architecture.md"

{
  printf '# Known Limitations\n\n'
  if [[ "$CUSTOMER_READY" == "true" ]]; then
    printf '%s\n' '- No P0 limitation is recorded in `metrics/summary.json`; review P1/P2 statuses in `validation_report.md`.'
  else
    printf '%s\n' '- Customer-ready status is false. One or more P0 gates failed, were partial, or were not run.'
    printf '%s\n' '- Missing bundle inputs from the source run:'
    awk -F '\t' '$1 == "missing" { printf "  - `%s`\n", $2 }' "$BUNDLE_DIR/manifest.tsv"
  fi
} > "$BUNDLE_DIR/known_limitations.md"

if [[ ! -f "$BUNDLE_DIR/validation_report.md" ]]; then
  {
    printf '# Network/Radio Validation Report\n\n'
    printf 'Customer-ready status: **not ready**.\n\n'
    printf 'No validation_report.md was present in the source run `%s`.\n' "${RUN_DIR#$ROOT_DIR/}"
  } > "$BUNDLE_DIR/validation_report.md"
fi

tar -czf "$ARCHIVE_PATH" -C "$ARTIFACTS_DIR" "$BUNDLE_NAME"

printf 'Bundle directory: %s\n' "$BUNDLE_DIR"
printf 'Bundle archive: %s\n' "$ARCHIVE_PATH"
if [[ "$CUSTOMER_READY" != "true" ]]; then
  printf 'NOTE customer-ready status remains false; see known_limitations.md.\n'
fi
