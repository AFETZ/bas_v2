#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="${AMS_NETWORK_TMP_DIR:-/tmp/ams-network-radio}"

namespaces=(
  ams-gcs
  ams-ns3
  ams-uav1
  ams-uav2
  ams-uav3
  ams-uav4
  ams-uav5
)

links=(
  ams-gcs-veth
  ams-ns3-gcs
  ams-uav1-veth
  ams-ns3-uav1
  ams-uav2-veth
  ams-ns3-uav2
  ams-uav3-veth
  ams-ns3-uav3
  ams-uav4-veth
  ams-ns3-uav4
  ams-uav5-veth
  ams-ns3-uav5
)

printf 'Cleaning network/radio runtime state under %s\n' "$ROOT_DIR"

if command -v ip >/dev/null 2>&1; then
  for ns in "${namespaces[@]}"; do
    if ip netns list 2>/dev/null | awk '{print $1}' | grep -qx "$ns"; then
      printf 'Deleting namespace %s\n' "$ns"
      if ! ip netns delete "$ns"; then
        printf 'WARN could not delete namespace %s; rerun from a privileged shell/container.\n' "$ns" >&2
      fi
    fi
  done

  for link in "${links[@]}"; do
    if ip link show "$link" >/dev/null 2>&1; then
      printf 'Deleting link %s\n' "$link"
      if ! ip link delete "$link"; then
        printf 'WARN could not delete link %s; rerun from a privileged shell/container.\n' "$link" >&2
      fi
    fi
  done
else
  printf 'WARN ip command not found; namespace and link cleanup skipped.\n' >&2
fi

if [[ -d "$TMP_DIR" ]]; then
  printf 'Removing temporary runtime directory %s\n' "$TMP_DIR"
  rm -rf "$TMP_DIR"
fi

printf 'Runtime cleanup complete. Generated runs/ and artifacts/ are preserved.\n'
