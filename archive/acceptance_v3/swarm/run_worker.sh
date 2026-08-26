#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

WORKER="${1:-${WORKER:-foundation}}"
case "$WORKER" in
  foundation|sionna|ns3|bridge|hitl|validation) ;;
  *)
    echo "Unknown worker '$WORKER'" >&2
    echo "Expected one of: foundation, sionna, ns3, bridge, hitl, validation" >&2
    exit 2
    ;;
esac

WORKERS="$WORKER" exec "$ROOT/network/swarm/run_swarm.sh"
