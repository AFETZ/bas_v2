#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENDPOINTS_FILE="${1:-$ROOT_DIR/network/config/endpoints.yaml}"
TIMEOUT_S="${NO_BYPASS_TIMEOUT_S:-0.25}"
MOVE_DRONE_FILE="$ROOT_DIR/src/multiagent_simulation/multiagent_simulation/move_drone.py"

if [[ ! -f "$ENDPOINTS_FILE" ]]; then
  printf 'FAIL endpoints file not found: %s\n' "$ENDPOINTS_FILE" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'FAIL python3 is required for the no-bypass smoke check.\n' >&2
  exit 2
fi

python3 - "$ENDPOINTS_FILE" "$TIMEOUT_S" "$MOVE_DRONE_FILE" <<'PY'
import re
import socket
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL PyYAML is required: python3 -m pip install PyYAML", file=sys.stderr)
    sys.exit(2)

endpoints_path = Path(sys.argv[1])
timeout_s = float(sys.argv[2])
move_drone_path = Path(sys.argv[3])
data = yaml.safe_load(endpoints_path.read_text()) or {}
forbidden = data.get("no_bypass", {}).get("forbidden_direct_endpoints", [])

if not forbidden:
    print(f"FAIL no forbidden_direct_endpoints configured in {endpoints_path}", file=sys.stderr)
    sys.exit(2)


def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def udp_port_bound(port: int) -> bool:
    proc_files = (Path("/proc/net/udp"), Path("/proc/net/udp6"))
    for proc_file in proc_files:
        if not proc_file.exists():
            continue
        for line in proc_file.read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or ":" not in parts[1]:
                continue
            _addr_hex, port_hex = parts[1].rsplit(":", 1)
            try:
                if int(port_hex, 16) == port:
                    return True
            except ValueError:
                continue
    return False


failures = []
checked = []
warnings = []

for endpoint in forbidden:
    endpoint_id = endpoint.get("id", "<unnamed>")
    protocol = str(endpoint.get("protocol", "")).lower()
    host = str(endpoint.get("host", "127.0.0.1"))
    port = int(endpoint.get("port"))
    checked.append(f"{endpoint_id} ({protocol} {host}:{port})")

    if protocol == "tcp":
        if tcp_reachable(host, port):
            failures.append(f"{endpoint_id}: TCP {host}:{port} is reachable without ns-3")
    elif protocol == "udp":
        if udp_port_bound(port):
            failures.append(f"{endpoint_id}: UDP port {port} is bound on the host while it is marked forbidden")
    else:
        warnings.append(f"{endpoint_id}: unsupported protocol '{protocol}', skipped")

if not move_drone_path.exists():
    failures.append(f"move_drone.py not found: {move_drone_path}")
else:
    source = move_drone_path.read_text()
    checked.append(f"move_drone source ({move_drone_path})")
    direct_connection = re.search(
        r"mavlink_connection\s*\(\s*['\"](?:udp|udpin|udpout):(?:127\.0\.0\.1|localhost):14550['\"]",
        source,
    )
    if direct_connection:
        failures.append("move_drone.py still opens the legacy udp:127.0.0.1:14550 endpoint directly")

    default_match = re.search(
        r"DEFAULT_MAVLINK_ENDPOINT\s*=\s*['\"](?P<endpoint>[^'\"]+)['\"]",
        source,
    )
    if not default_match:
        failures.append("move_drone.py does not define DEFAULT_MAVLINK_ENDPOINT")
    else:
        default_endpoint = default_match.group("endpoint")
        parsed = re.match(
            r"^(?P<scheme>udp|udpin|udpout|tcp|tcpin|tcpout):(?P<host>[^:]+):(?P<port>[0-9]+)$",
            default_endpoint,
        )
        if parsed:
            protocol = "tcp" if parsed.group("scheme").startswith("tcp") else "udp"
            host = parsed.group("host").lower()
            if host == "localhost":
                host = "127.0.0.1"
            port = int(parsed.group("port"))
            for endpoint in forbidden:
                if (
                    str(endpoint.get("protocol", "")).lower() == protocol
                    and str(endpoint.get("host", "")).lower() == host
                    and int(endpoint.get("port")) == port
                ):
                    failures.append(f"move_drone.py default endpoint is forbidden: {default_endpoint}")
                    break

    if "FORBIDDEN_DIRECT_ENDPOINTS" not in source or "validate_mavlink_endpoint" not in source:
        failures.append("move_drone.py lacks explicit forbidden endpoint validation")

print("No-bypass smoke check")
print(f"Endpoint config: {endpoints_path}")
for item in checked:
    print(f"CHECK {item}")
for item in warnings:
    print(f"WARN {item}")

if failures:
    for item in failures:
        print(f"FAIL {item}")
    print("Direct traffic may bypass the modeled ns-3/radio path. Stop the endpoint or move it behind isolation.")
    sys.exit(1)

print("PASS no configured forbidden direct TCP endpoint is reachable and no configured forbidden UDP port is bound")
print("NOTE full P0 no-bypass proof still requires active endpoints with ns-3 stopped inside the namespace/TAP topology.")
PY
