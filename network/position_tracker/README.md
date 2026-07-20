# Position Tracker

This adapter preserves the existing ROS/Gazebo launch model and subscribes to
the per-UAV odometry topics already bridged by the base simulator:

```text
/uav1/odometry
/uav2/odometry
/uav3/odometry
/uav4/odometry
/uav5/odometry
```

Runtime command:

```bash
./network/scripts/run_position_tracker.sh
```

The tracker writes normalized radio node state to:

```text
runs/<run_id>/logs/node_state.json
runs/<run_id>/logs/node_state.jsonl
```

The stream includes command-post and jammer positions from config, UAV
position/orientation from ROS odometry, source topic names, and missing/stale
node lists. It does not change the flight-control path.

Offline smoke-test helper:

```bash
python3 network/position_tracker/tracker.py --from-config-once
```

That helper emits nominal configured positions only; it is not proof that
Gazebo motion is feeding Sionna.
