#!/usr/bin/env bash
set -euo pipefail

GCS_NS="${GCS_NS:-ams-gcs}"
NS3_NS="${NS3_NS:-ams-ns3}"
UAV_NS="${UAV_NS:-ams-uav1}"
TAIL_ROOT="${TAIL_ROOT:-ams-tail0}"

if ((EUID != 0)); then
  printf 'FAIL M2 namespace setup requires an already capability-bounded root process\n' >&2
  exit 2
fi

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -Fxq "$1"
}

set_namespace_sysctls() {
  local namespace="$1"
  # Docker mounts the container's /proc/sys read-only.  A short-lived private
  # mount namespace can mount proc for the selected network namespace, change
  # only its namespaced sysctls, and then disappear without a host mount.
  ip netns exec "$namespace" unshare --mount --propagation private sh -c '
    set -e
    mount -t proc proc /proc
    sysctl -qw net.ipv4.ip_forward=0
    sysctl -qw net.ipv6.conf.all.disable_ipv6=1
    sysctl -qw net.ipv6.conf.default.disable_ipv6=1
  '
}

down() {
  ip netns del "$GCS_NS" 2>/dev/null || true
  ip netns del "$NS3_NS" 2>/dev/null || true
  ip netns del "$UAV_NS" 2>/dev/null || true
  ip link del "$TAIL_ROOT" 2>/dev/null || true
}

up() {
  trap down ERR
  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    if namespace_exists "$namespace"; then
      printf 'FAIL namespace already exists: %s\n' "$namespace" >&2
      exit 1
    fi
  done
  if ip link show "$TAIL_ROOT" >/dev/null 2>&1; then
    printf 'FAIL tail interface already exists: %s\n' "$TAIL_ROOT" >&2
    exit 1
  fi

  ip netns add "$GCS_NS"
  ip netns add "$NS3_NS"
  ip netns add "$UAV_NS"
  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    set_namespace_sysctls "$namespace"
  done

  ip link add v-gcs type veth peer name v-gcs-ns3
  ip link add v-uav type veth peer name v-uav-ns3
  ip link add "$TAIL_ROOT" type veth peer name v-tail-uav

  ip link set v-gcs netns "$GCS_NS"
  ip link set v-gcs-ns3 netns "$NS3_NS"
  ip link set v-uav netns "$UAV_NS"
  ip link set v-uav-ns3 netns "$NS3_NS"
  ip link set v-tail-uav netns "$UAV_NS"

  ip -n "$GCS_NS" link set v-gcs name eth0
  ip -n "$UAV_NS" link set v-uav name eth0
  ip -n "$UAV_NS" link set v-tail-uav name tail0

  ip -n "$NS3_NS" link add br-gcs type bridge
  ip -n "$NS3_NS" link add br-uav type bridge
  ip netns exec "$NS3_NS" ip tuntap add dev tap-gcs mode tap user "$(id -u)"
  ip netns exec "$NS3_NS" ip tuntap add dev tap-uav mode tap user "$(id -u)"
  ip -n "$NS3_NS" link set v-gcs-ns3 master br-gcs
  ip -n "$NS3_NS" link set tap-gcs master br-gcs
  ip -n "$NS3_NS" link set v-uav-ns3 master br-uav
  ip -n "$NS3_NS" link set tap-uav master br-uav

  # Docker deliberately exposes /proc/sys read-only to this non-privileged
  # capability profile.  Prevent automatic IPv6 link-local addresses through
  # rtnetlink itself instead of relying on a mutable sysctl filesystem.
  ip -n "$GCS_NS" link set eth0 addrgenmode none
  ip -n "$UAV_NS" link set eth0 addrgenmode none
  ip -n "$UAV_NS" link set tail0 addrgenmode none
  for interface in v-gcs-ns3 tap-gcs br-gcs v-uav-ns3 tap-uav br-uav; do
    ip -n "$NS3_NS" link set "$interface" addrgenmode none
  done

  ip -n "$GCS_NS" link set eth0 address 02:71:00:00:10:10
  ip -n "$UAV_NS" link set eth0 address 02:71:01:00:10:10
  ip -n "$GCS_NS" address add 10.71.0.10/24 dev eth0
  ip -n "$UAV_NS" address add 10.71.1.10/24 dev eth0
  ip address add 10.72.1.1/30 dev "$TAIL_ROOT"
  ip -n "$UAV_NS" address add 10.72.1.2/30 dev tail0

  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    ip -n "$namespace" link set lo up
  done
  ip -n "$GCS_NS" link set eth0 up
  ip -n "$UAV_NS" link set eth0 up
  ip -n "$UAV_NS" link set tail0 up
  ip link set "$TAIL_ROOT" up
  for interface in v-gcs-ns3 tap-gcs br-gcs v-uav-ns3 tap-uav br-uav; do
    ip -n "$NS3_NS" link set "$interface" up
  done

  # The external veth/bridge remains present while the ns-3 packet engine is
  # stopped.  Pin its deterministic L2 next hop so every stopped-phase offer
  # reaches that bridge and is observable in raw captures; the absent
  # TapBridge reader still prevents any radio/UAV delivery.
  ip -n "$GCS_NS" neigh replace 10.71.0.1 \
    lladdr 02:71:00:00:00:01 nud permanent dev eth0
  ip -n "$UAV_NS" neigh replace 10.71.1.1 \
    lladdr 02:71:01:00:00:01 nud permanent dev eth0
  ip -n "$GCS_NS" route add default via 10.71.0.1 dev eth0
  ip -n "$UAV_NS" route add default via 10.71.1.1 dev eth0
  trap - ERR
  printf 'Created M2 namespaces: %s %s %s\n' "$GCS_NS" "$NS3_NS" "$UAV_NS"
}

status() {
  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    if ! namespace_exists "$namespace"; then
      printf 'FAIL namespace missing: %s\n' "$namespace" >&2
      exit 1
    fi
  done
  ip -n "$GCS_NS" -brief address
  ip -n "$NS3_NS" -brief address
  ip -n "$UAV_NS" -brief address
  for bridge in br-gcs br-uav; do
    if ip -n "$NS3_NS" -o address show dev "$bridge" | grep -q .; then
      printf 'FAIL ns-3 Linux bridge owns an IP address: %s\n' "$bridge" >&2
      exit 1
    fi
  done
  if [[ "$(ip netns exec "$UAV_NS" sysctl -n net.ipv4.ip_forward)" != "0" ]]; then
    printf 'FAIL UAV namespace IP forwarding is enabled\n' >&2
    exit 1
  fi
  local gcs_neighbor uav_neighbor
  gcs_neighbor="$(ip -n "$GCS_NS" neigh show to 10.71.0.1 dev eth0)"
  uav_neighbor="$(ip -n "$UAV_NS" neigh show to 10.71.1.1 dev eth0)"
  if [[ "$gcs_neighbor" != *"02:71:00:00:00:01"* || "$gcs_neighbor" != *"PERMANENT"* ]]; then
    printf 'FAIL GCS namespace lacks the permanent ns-3 external next hop\n' >&2
    exit 1
  fi
  if [[ "$uav_neighbor" != *"02:71:01:00:00:01"* || "$uav_neighbor" != *"PERMANENT"* ]]; then
    printf 'FAIL UAV namespace lacks the permanent ns-3 external next hop\n' >&2
    exit 1
  fi
  printf 'M2 namespace topology is present\n'
}

case "${1:-}" in
  up) up ;;
  status) status ;;
  down) down ;;
  *) printf 'Usage: %s up|status|down\n' "$0" >&2; exit 2 ;;
esac
