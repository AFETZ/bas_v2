#!/usr/bin/env bash
set -euo pipefail

GCS_NS="${GCS_NS:-ams-gcs}"
NS3_NS="${NS3_NS:-ams-ns3}"
UAV_NS="${UAV_NS:-ams-uav1}"
TAIL_ROOT="${TAIL_ROOT:-ams-tail0}"

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -Fxq "$1"
}

down() {
  sudo ip netns del "$GCS_NS" 2>/dev/null || true
  sudo ip netns del "$NS3_NS" 2>/dev/null || true
  sudo ip netns del "$UAV_NS" 2>/dev/null || true
  sudo ip link del "$TAIL_ROOT" 2>/dev/null || true
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

  sudo ip netns add "$GCS_NS"
  sudo ip netns add "$NS3_NS"
  sudo ip netns add "$UAV_NS"
  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    sudo ip netns exec "$namespace" sysctl -qw net.ipv6.conf.all.disable_ipv6=1
    sudo ip netns exec "$namespace" sysctl -qw net.ipv6.conf.default.disable_ipv6=1
  done

  sudo ip link add v-gcs type veth peer name v-gcs-ns3
  sudo ip link add v-uav type veth peer name v-uav-ns3
  sudo ip link add "$TAIL_ROOT" type veth peer name v-tail-uav

  sudo ip link set v-gcs netns "$GCS_NS"
  sudo ip link set v-gcs-ns3 netns "$NS3_NS"
  sudo ip link set v-uav netns "$UAV_NS"
  sudo ip link set v-uav-ns3 netns "$NS3_NS"
  sudo ip link set v-tail-uav netns "$UAV_NS"

  sudo ip -n "$GCS_NS" link set v-gcs name eth0
  sudo ip -n "$UAV_NS" link set v-uav name eth0
  sudo ip -n "$UAV_NS" link set v-tail-uav name tail0

  sudo ip -n "$NS3_NS" link add br-gcs type bridge
  sudo ip -n "$NS3_NS" link add br-uav type bridge
  sudo ip netns exec "$NS3_NS" ip tuntap add dev tap-gcs mode tap user "$(id -u)"
  sudo ip netns exec "$NS3_NS" ip tuntap add dev tap-uav mode tap user "$(id -u)"
  sudo ip -n "$NS3_NS" link set v-gcs-ns3 master br-gcs
  sudo ip -n "$NS3_NS" link set tap-gcs master br-gcs
  sudo ip -n "$NS3_NS" link set v-uav-ns3 master br-uav
  sudo ip -n "$NS3_NS" link set tap-uav master br-uav

  sudo ip -n "$GCS_NS" link set eth0 address 02:71:00:00:10:10
  sudo ip -n "$UAV_NS" link set eth0 address 02:71:01:00:10:10
  sudo ip -n "$GCS_NS" address add 10.71.0.10/24 dev eth0
  sudo ip -n "$UAV_NS" address add 10.71.1.10/24 dev eth0
  sudo ip address add 10.72.1.1/30 dev "$TAIL_ROOT"
  sudo ip -n "$UAV_NS" address add 10.72.1.2/30 dev tail0

  for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
    sudo ip -n "$namespace" link set lo up
  done
  sudo ip -n "$GCS_NS" link set eth0 up
  sudo ip -n "$UAV_NS" link set eth0 up
  sudo ip -n "$UAV_NS" link set tail0 up
  sudo ip link set "$TAIL_ROOT" up
  for interface in v-gcs-ns3 tap-gcs br-gcs v-uav-ns3 tap-uav br-uav; do
    sudo ip -n "$NS3_NS" link set "$interface" up
  done

  sudo ip -n "$GCS_NS" route add default via 10.71.0.1 dev eth0
  sudo ip -n "$UAV_NS" route add default via 10.71.1.1 dev eth0
  sudo ip netns exec "$UAV_NS" sysctl -qw net.ipv4.ip_forward=0
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
  sudo ip -n "$GCS_NS" -brief address
  sudo ip -n "$NS3_NS" -brief address
  sudo ip -n "$UAV_NS" -brief address
  for bridge in br-gcs br-uav; do
    if sudo ip -n "$NS3_NS" -o address show dev "$bridge" | grep -q .; then
      printf 'FAIL ns-3 Linux bridge owns an IP address: %s\n' "$bridge" >&2
      exit 1
    fi
  done
  if [[ "$(sudo ip netns exec "$UAV_NS" sysctl -n net.ipv4.ip_forward)" != "0" ]]; then
    printf 'FAIL UAV namespace IP forwarding is enabled\n' >&2
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
