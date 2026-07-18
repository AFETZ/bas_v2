#!/usr/bin/env python3
"""Apply the frozen M4 pose/jammer/expiry sequence on the host monotonic clock."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.validation.m4_common import M4ValidationError, strict_json
from network.validation.validate_m4_causality import WINDOW_IDS, matrix_link_identity


def write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o664)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


class GazeboPoseClient:
    def __init__(self) -> None:
        try:
            from gz.msgs10.boolean_pb2 import Boolean
            from gz.msgs10.pose_pb2 import Pose
            from gz.transport13 import Node
        except ImportError as exc:
            raise M4ValidationError(
                f"native Gazebo transport is required for causal pose control: {exc}"
            ) from exc
        self.node = Node()
        self.pose_type = Pose
        self.boolean_type = Boolean

    def set_pose(self, model: str, position: list[float]) -> None:
        request = self.pose_type()
        request.name = model
        request.position.x, request.position.y, request.position.z = map(float, position)
        request.orientation.w = 1.0
        ok, response = self.node.request(
            "/world/map/set_pose",
            request,
            self.pose_type,
            self.boolean_type,
            1_000,
        )
        if not ok or not bool(getattr(response, "data", False)):
            raise M4ValidationError(f"Gazebo rejected canonical pose for {model}")


def expected_pose(bundle: Mapping[str, Any], window: Mapping[str, Any]) -> Mapping[str, Any]:
    scenario = str(window["scenario"])
    if scenario in {"terrain_shadow", "building_blocked"}:
        return bundle["causal_scenarios"][scenario]["pose_sets"][window["pose_set"]]
    if scenario == "jammer_off_on_off":
        return bundle["causal_scenarios"][scenario]["pose_set"]
    return bundle["causal_scenarios"]["terrain_shadow"]["pose_sets"]["terrain_good"]


def wait_until(target_ns: int) -> None:
    while True:
        remaining = target_ns - time.monotonic_ns()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining / 1_000_000_000))


def emit_event(event_dir: Path, sequence: int, event: str, **fields: Any) -> None:
    write_exclusive(
        event_dir / f"{sequence:04d}-{event}.json",
        {"event": event, **fields},
    )


def adapter_control(directory: Path, sequence: int, action: str, **fields: Any) -> None:
    write_exclusive(
        directory / f"{sequence:04d}-{action}.json",
        {"action": action, **fields},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--event-dir", type=Path, required=True)
    parser.add_argument("--adapter-control-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--done-file", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve(strict=True)
    contract = strict_json(args.contract.resolve(strict=True))
    bundle = strict_json(
        ROOT / "network/config/m4_canonical_scene_bundle.json"
    )
    windows = contract.get("windows")
    if (
        contract.get("contract") != "ams.m4.causality_run/v1"
        or not isinstance(windows, list)
        or [item.get("window_id") for item in windows] != list(WINDOW_IDS)
    ):
        raise M4ValidationError("phase driver contract/window sequence differs")
    client = GazeboPoseClient()
    write_exclusive(
        args.ready_file,
        {
            "contract": "ams.m4.causal-phase-driver-ready/v1",
            "run_id": contract["run_id"],
            "runtime_id": contract["runtime_id"],
            "pid": os.getpid(),
            "ready_monotonic_ns": time.monotonic_ns(),
        },
    )
    event_sequence = 0
    control_sequence = 0
    previous_id: str | None = None
    previous_end: int | None = None
    pose_fixture_sequence = 0
    expiry_target = matrix_link_identity("uav1.control.downlink")[1]
    for window in windows:
        window_id = str(window["window_id"])
        start_ns = int(window["start_monotonic_ns"])
        end_ns = int(window["end_monotonic_ns"])
        transition_ns = (
            start_ns - 90_000_000
            if previous_end is None or start_ns == previous_end
            else previous_end + 100_000_000
        )
        wait_until(transition_ns)
        pose = expected_pose(bundle, window)
        for model in ("cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"):
            client.set_pose(model, list(pose[model]))
        pose_fixture_sequence += 1
        control_sequence += 1
        adapter_control(
            args.adapter_control_dir,
            control_sequence,
            "set_jammer_enabled",
            not_before_monotonic_ns=transition_ns,
            enabled=bool(window["jammer_enabled"]),
        )
        if window_id == "expiry_unavailable":
            control_sequence += 1
            adapter_control(
                args.adapter_control_dir,
                control_sequence,
                "arm_hold_next",
                not_before_monotonic_ns=transition_ns,
                directed_link_id=expiry_target,
            )
            control_sequence += 1
            adapter_control(
                args.adapter_control_dir,
                control_sequence,
                "arm_fault_parallel_next",
                not_before_monotonic_ns=transition_ns,
                directed_link="cp>uav1",
                traffic_class="control",
            )
        predicate = {
            "good": "fresh_state_applied",
            "down": "fresh_physical_down_state_applied",
            "recovery": "fresh_state_applied",
            "off-1": "fresh_state_applied",
            "on": "fresh_jammer_state_applied",
            "off-2": "fresh_state_applied",
            "unavailable": "state_expired",
        }[str(window["phase"])]
        if window_id == "expiry_recovery":
            predicate = "fresh_state_applied_after_fault_removed"
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_stimulus_applied",
            window_id=window_id,
            state_predicate=predicate,
            target_packet_link=window["target_link"],
            pose_fixture_sequence=pose_fixture_sequence,
        )
        drain_ns = start_ns - 50_000_000
        wait_until(drain_ns)
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_drain_complete",
            next_window_id=window_id,
            prior_window_id=previous_id,
            terminal_outcomes_complete=True,
            queue_depths={
                "userspace": 0,
                "ns3": 0,
                "qdisc": 0,
                "capture_pending": 0,
            },
        )
        wait_until(start_ns)
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_measurement_start",
            window_id=window_id,
            target_monotonic_ns=start_ns,
        )
        next_pose_ns = start_ns
        expiry_release_done = False
        expiry_duplicate_done = False
        while time.monotonic_ns() < end_ns:
            now = time.monotonic_ns()
            if now >= next_pose_ns:
                for model in ("cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"):
                    client.set_pose(model, list(pose[model]))
                next_pose_ns += 500_000_000
            if window_id == "expiry_unavailable" and not expiry_release_done and now >= start_ns + 5_000_000_000:
                control_sequence += 1
                adapter_control(
                    args.adapter_control_dir,
                    control_sequence,
                    "release_held",
                    not_before_monotonic_ns=now,
                    directed_link_id=expiry_target,
                )
                expiry_release_done = True
            if window_id == "expiry_unavailable" and not expiry_duplicate_done and now >= start_ns + 7_000_000_000:
                control_sequence += 1
                adapter_control(
                    args.adapter_control_dir,
                    control_sequence,
                    "inject_duplicate",
                    not_before_monotonic_ns=now,
                    directed_link_id=expiry_target,
                )
                expiry_duplicate_done = True
            time.sleep(0.01)
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_measurement_end",
            window_id=window_id,
            target_monotonic_ns=end_ns,
        )
        previous_id = window_id
        previous_end = end_ns
    write_exclusive(
        args.done_file,
        {
            "contract": "ams.m4.causal-phase-driver-done/v1",
            "run_id": contract["run_id"],
            "runtime_id": contract["runtime_id"],
            "window_count": len(windows),
            "completed_monotonic_ns": time.monotonic_ns(),
        },
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (M4ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL M4 causal phase driver: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
