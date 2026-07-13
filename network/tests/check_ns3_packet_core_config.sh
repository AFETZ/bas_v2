#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TOPOLOGY_FILE="$TMP_DIR/topology.txt"
RUN_DIR="$TMP_DIR/run"

python3 "$ROOT_DIR/network/ns3/generate_ns3_topology.py" \
  --run-id config_check \
  --run-dir "$RUN_DIR" \
  --output "$TOPOLOGY_FILE" \
  --allow-mock-sionna >/dev/null

python3 - "$TOPOLOGY_FILE" <<'PY'
import sys
from pathlib import Path

topology = Path(sys.argv[1])
lines = [
    line.strip()
    for line in topology.read_text().splitlines()
    if line.strip() and not line.startswith("#")
]

nodes = [line for line in lines if line.startswith("node ")]
traffic_classes = [line for line in lines if line.startswith("traffic_class ")]
links = [line for line in lines if line.startswith("link ")]
emitters = [line for line in lines if line.startswith("emitter ")]
packet_core = {}
scalar_config = {}
for line in lines:
    if line.startswith("packet_core_"):
        key, value = line.split(maxsplit=1)
        packet_core[key] = value
    elif len(line.split(maxsplit=1)) == 2:
        key, value = line.split(maxsplit=1)
        scalar_config[key] = value

errors = []
if len(nodes) != 6:
    errors.append(f"expected 6 nodes, found {len(nodes)}")
if not any(line.startswith("node cp command_post ") for line in nodes):
    errors.append("missing command-post node")
for name in ["uav1", "uav2", "uav3", "uav4", "uav5"]:
    if not any(line.startswith(f"node {name} uav ") for line in nodes):
        errors.append(f"missing UAV node {name}")

expected_classes = {"control", "payload", "additional_data"}
actual_classes = {line.split()[1] for line in traffic_classes}
if actual_classes != expected_classes:
    errors.append(f"traffic classes mismatch: {sorted(actual_classes)}")

if len(links) != 30:
    errors.append(f"expected 30 directed links, found {len(links)}")
for traffic_class in expected_classes:
    if not any(line == f"link cp uav1 {traffic_class}" for line in links):
        errors.append(f"missing cp->uav1 link for {traffic_class}")
    if not any(line == f"link uav1 cp {traffic_class}" for line in links):
        errors.append(f"missing uav1->cp link for {traffic_class}")

if len(emitters) < 1:
    errors.append("expected at least 1 configured jammer/emitter")
enabled_emitters = [line for line in emitters if len(line.split()) > 2 and line.split()[2] == "1"]
if not enabled_emitters:
    errors.append("expected at least 1 enabled jammer/emitter")

if packet_core.get("packet_core_mode") != "csma_surrogate":
    errors.append(f"packet_core_mode is {packet_core.get('packet_core_mode')!r}, expected csma_surrogate")
if packet_core.get("packet_core_status") != "implemented_current_p0_surrogate":
    errors.append("packet_core_status does not describe the implemented CSMA surrogate")
if packet_core.get("packet_core_runtime_selectable") != "1":
    errors.append("current packet-core mode is not marked runtime-selectable")
if packet_core.get("packet_core_shared_medium_model") != "csma":
    errors.append("packet_core_shared_medium_model is not csma")
if packet_core.get("packet_core_fidelity_note") != "csma_surrogate_not_customer_modem_waveform":
    errors.append("packet_core_fidelity_note does not expose the CSMA surrogate limitation")
if scalar_config.get("bandwidth_hz") != "20000000":
    errors.append(f"bandwidth_hz is {scalar_config.get('bandwidth_hz')!r}, expected 20000000")
if scalar_config.get("channel_rate_bps") != "20000000":
    errors.append(
        f"channel_rate_bps is {scalar_config.get('channel_rate_bps')!r}, expected 20000000"
    )
if scalar_config.get("noise_figure_db") != "6.0":
    errors.append(f"noise_figure_db is {scalar_config.get('noise_figure_db')!r}, expected 6.0")

if errors:
    print("FAIL ns-3 packet-core generated topology is invalid")
    for error in errors:
        print(f"  - {error}")
    sys.exit(1)

print("PASS ns-3 packet-core config generated 6 nodes, 3 traffic classes, 30 links, jammer metadata, and explicit CSMA-surrogate mode")
PY

MODE_REPORT="$TMP_DIR/ns3_packet_core_mode.json"
python3 "$ROOT_DIR/network/ns3/packet_core_modes.py" \
  --purpose evaluation \
  --json-output "$MODE_REPORT" \
  --print-mode > "$TMP_DIR/resolved_mode.txt"

python3 - "$MODE_REPORT" "$TMP_DIR/resolved_mode.txt" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
resolved_mode = Path(sys.argv[2]).read_text().strip()
errors = []

if resolved_mode != "csma_surrogate":
    errors.append(f"resolved mode is {resolved_mode!r}, expected csma_surrogate")
if report.get("mode") != "csma_surrogate":
    errors.append("mode report did not resolve csma_surrogate")
if report.get("runtime_selectable") is not True:
    errors.append("csma_surrogate is not runtime-selectable in the mode report")
if report.get("fail_closed") is not False:
    errors.append("csma_surrogate should not be marked fail-closed")
if "csma_surrogate_not_customer_modem_waveform" != report.get("fidelity_note_id"):
    errors.append("mode report does not expose the CSMA surrogate fidelity note")

if errors:
    print("FAIL ns-3 packet-core mode report is invalid")
    for error in errors:
        print(f"  - {error}")
    sys.exit(1)

print("PASS ns-3 packet-core mode report resolves the current CSMA surrogate explicitly")
PY

FUTURE_REPORT="$TMP_DIR/tap_bridge_external_report.json"
python3 "$ROOT_DIR/network/ns3/packet_core_modes.py" \
  --mode tap_bridge_external \
  --purpose evaluation \
  --skip-host-checks \
  --json-output "$FUTURE_REPORT" >/dev/null

python3 - "$FUTURE_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
errors = []
if report.get("mode") != "tap_bridge_external":
    errors.append("tap_bridge_external evaluation report resolved the wrong mode")
if report.get("runtime_selectable") is not False:
    errors.append("tap_bridge_external must remain non-runtime-selectable")
if report.get("fail_closed") is not True:
    errors.append("tap_bridge_external must be marked fail-closed")
if report.get("status") != "implemented_m2_diagnostic_fail_closed":
    errors.append("tap_bridge_external status does not describe the implemented diagnostic path")
if "ns3::TapBridge" not in report.get("upstream_interfaces", []):
    errors.append("tap_bridge_external report does not name ns3::TapBridge")
if "ns3::FdNetDevice" in report.get("upstream_interfaces", []):
    errors.append("tap_bridge_external must not claim serial FdNetDevice wiring")

if errors:
    print("FAIL tap_bridge_external evaluation scaffold report is invalid")
    for error in errors:
        print(f"  - {error}")
    sys.exit(1)

print("PASS tap_bridge_external evaluation scaffold is reportable and fail-closed")
PY

set +e
python3 "$ROOT_DIR/network/ns3/generate_ns3_topology.py" \
  --run-id tap_bridge_must_fail \
  --run-dir "$RUN_DIR/future_mode" \
  --output "$TMP_DIR/future_topology.txt" \
  --packet-core-mode tap_bridge_external \
  --allow-mock-sionna >"$TMP_DIR/future_stdout.txt" 2>"$TMP_DIR/future_stderr.txt"
future_status=$?
set -e

if [[ "$future_status" -eq 0 ]]; then
  printf 'FAIL tap_bridge_external topology generation unexpectedly succeeded\n' >&2
  exit 1
fi

if ! grep -q 'fail-closed' "$TMP_DIR/future_stderr.txt"; then
  printf 'FAIL tap_bridge_external runtime guard did not mention fail-closed\n' >&2
  cat "$TMP_DIR/future_stderr.txt" >&2
  exit 1
fi

printf 'PASS tap_bridge_external runtime selection fails closed before ns-3 launch\n'
