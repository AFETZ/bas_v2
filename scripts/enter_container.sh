#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-multiagent_simulation}"
CONTAINER_ID="${CONTAINER_ID:-}"

usage() {
  cat <<'EOF'
Usage: scripts/enter_container.sh [--container ID]

Open a shell inside an already-running multiagent_simulation container.
Use this for a second terminal while Gazebo/SITL is running; do not start a
second container with run_container.sh for live ROS tools.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      CONTAINER_ID="${2:?missing container id}"
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

if [[ -z "$CONTAINER_ID" ]]; then
  mapfile -t candidates < <(docker ps --filter "ancestor=$IMAGE" --format '{{.ID}}')
  if [[ "${#candidates[@]}" -eq 0 ]]; then
    printf 'FAIL no running %s container. Start one with ./scripts/run_container.sh first.\n' "$IMAGE" >&2
    exit 2
  fi

  for candidate in "${candidates[@]}"; do
    if docker exec "$candidate" bash -lc \
      "pgrep -f 'ros2 launch.*multiagent_simulation.launch.py|gz sim|arducopter' >/dev/null 2>&1"; then
      CONTAINER_ID="$candidate"
      break
    fi
  done
  CONTAINER_ID="${CONTAINER_ID:-${candidates[0]}}"
fi

printf 'Entering container %s\n' "$CONTAINER_ID" >&2
exec docker exec -it "$CONTAINER_ID" bash -lc '
  set -eo pipefail
  source /opt/ros/humble/setup.bash
  source /workspace/ardu_ws/install/setup.bash
  if [[ -f /workspace/multiagent_simulation/install/setup.bash ]]; then
    source /workspace/multiagent_simulation/install/setup.bash
  fi
  cd /workspace/multiagent_simulation
  exec bash
'
