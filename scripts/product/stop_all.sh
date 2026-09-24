#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${BAS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/bas-v2-${UID}}"
CONTAINER_NAME="${BAS_BASE_CONTAINER_NAME:-bas-v2-baseline}"
NETWORK_CONTAINER_NAME="${BAS_NETWORK_CONTAINER_NAME:-bas-v2-network}"
TOWN01_CONTAINER_NAME="${BAS_TOWN01_CONTAINER_NAME:-bas-v2-town01-full-stack}"
NATIVE_FIVE_CONTAINER_NAME="${BAS_NATIVE_FIVE_CONTAINER_NAME:-bas-v2-native-radio-five-uav}"
stopped=0

if [[ ! "$NATIVE_FIVE_CONTAINER_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  printf 'Refusing unsafe native five-UAV container name: %s\n' \
    "$NATIVE_FIVE_CONTAINER_NAME" >&2
  exit 2
fi

if command -v docker >/dev/null 2>&1 \
  && [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
  product_label="$(docker inspect --format '{{index .Config.Labels "bas.product"}}' "$CONTAINER_NAME")"
  if [[ "$product_label" != "base" ]]; then
    printf 'Refusing to stop container %s: missing bas.product=base label.\n' \
      "$CONTAINER_NAME" >&2
    exit 1
  fi
  printf 'Stopping base container %s.\n' "$CONTAINER_NAME"
  docker stop --timeout 15 "$CONTAINER_NAME" >/dev/null
  stopped=$((stopped + 1))
fi

if command -v docker >/dev/null 2>&1 \
  && [[ "$(docker inspect --format '{{.State.Running}}' "$TOWN01_CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
  product_label="$(docker inspect --format '{{index .Config.Labels "bas.product"}}' "$TOWN01_CONTAINER_NAME")"
  if [[ "$product_label" != "town01-full-stack" ]]; then
    printf 'Refusing to stop container %s: missing bas.product=town01-full-stack label.\n' \
      "$TOWN01_CONTAINER_NAME" >&2
    exit 1
  fi
  printf 'Stopping Town01 full-stack container %s.\n' "$TOWN01_CONTAINER_NAME"
  docker stop --timeout 15 "$TOWN01_CONTAINER_NAME" >/dev/null
  stopped=$((stopped + 1))
fi

if command -v docker >/dev/null 2>&1 \
  && [[ "$(docker inspect --format '{{.State.Running}}' "$NATIVE_FIVE_CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
  product_label="$(docker inspect --format '{{index .Config.Labels "bas.product"}}' "$NATIVE_FIVE_CONTAINER_NAME")"
  if [[ "$product_label" != "native-radio-five-uav" ]]; then
    printf 'Refusing to stop container %s: missing bas.product=native-radio-five-uav label.\n' \
      "$NATIVE_FIVE_CONTAINER_NAME" >&2
    exit 1
  fi
  printf 'Stopping native five-UAV demo container %s.\n' "$NATIVE_FIVE_CONTAINER_NAME"
  docker stop --timeout 15 "$NATIVE_FIVE_CONTAINER_NAME" >/dev/null
  stopped=$((stopped + 1))
fi

if command -v docker >/dev/null 2>&1 \
  && [[ "$(docker inspect --format '{{.State.Running}}' "$NETWORK_CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
  product_label="$(docker inspect --format '{{index .Config.Labels "bas.product"}}' "$NETWORK_CONTAINER_NAME")"
  if [[ "$product_label" != "network" ]]; then
    printf 'Refusing to stop container %s: missing bas.product=network label.\n' \
      "$NETWORK_CONTAINER_NAME" >&2
    exit 1
  fi
  printf 'Stopping network container %s.\n' "$NETWORK_CONTAINER_NAME"
  docker stop --timeout 15 "$NETWORK_CONTAINER_NAME" >/dev/null
  stopped=$((stopped + 1))
fi

stop_product_group() {
  local name="$1"
  local expected_pattern="$2"
  local pid_file="$RUNTIME_DIR/$name.pid"

  [[ -f "$pid_file" ]] || return 0
  local pid
  pid="$(<"$pid_file")"
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    printf 'Ignoring invalid PID file %s.\n' "$pid_file" >&2
    rm -f "$pid_file"
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    printf '%s runtime is not running; removing stale PID file.\n' "$name"
    rm -f "$pid_file"
    return 0
  fi

  local command_line
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ ! "$command_line" =~ $expected_pattern ]]; then
    printf 'Refusing to stop PID %s: it does not match the %s product runtime.\n' "$pid" "$name" >&2
    return 1
  fi

  printf 'Stopping %s process group %s.\n' "$name" "$pid"
  kill -TERM -- "-$pid"
  for _ in {1..100}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      stopped=$((stopped + 1))
      return 0
    fi
    sleep 0.1
  done

  printf '%s did not stop after 10 seconds; forcing its validated process group down.\n' "$name" >&2
  kill -KILL -- "-$pid" 2>/dev/null || true
  rm -f "$pid_file"
  stopped=$((stopped + 1))
}

stop_product_group base 'ros2.*launch.*multiagent_simulation.*multiagent_simulation.launch.py'
stop_product_group network 'network/scripts/(run_network_demo|run_sim_2_4ghz_loop).sh'
printf 'Stopped %d product runtime(s).\n' "$stopped"
