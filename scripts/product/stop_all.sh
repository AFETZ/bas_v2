#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${BAS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/bas-v2-${UID}}"
stopped=0

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
