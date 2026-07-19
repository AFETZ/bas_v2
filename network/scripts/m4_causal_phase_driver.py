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
from network.validation.m4_runtime import QUERY_DEADLINE_NS
from network.validation.validate_m4_causality import (
    CAUSAL_PIN_MODELS,
    CAUSAL_PIN_PLUGIN_FILENAME,
    CAUSAL_PIN_PLUGIN_NAME,
    CAUSAL_PIN_PUBLISH_PERIOD_NS,
    CAUSAL_PIN_SYSTEM_ADD_SERVICE,
    CAUSAL_PIN_TOPIC_PREFIX,
    CAUSAL_POSE_REFRESH_PERIOD_NS,
    CAUSAL_POSE_VECTOR_MAX_LATENCY_NS,
    CAUSAL_POSE_VECTOR_MODELS,
    CAUSAL_POSE_VECTOR_SERVICE,
    EXPIRY_FAULT_ARM_SETTLE_NS,
    WINDOW_IDS,
    causal_offer_offset_ns,
    causal_pre_window_gap_ns,
    causal_window_plan,
    matrix_link_identity,
)


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
            from gz.msgs10.empty_pb2 import Empty
            from gz.msgs10.entity_plugin_v_pb2 import EntityPlugin_V
            from gz.msgs10.pose_v_pb2 import Pose_V
            from gz.msgs10.scene_pb2 import Scene
            from gz.msgs10.twist_pb2 import Twist
            from gz.transport13 import Node
        except ImportError as exc:
            raise M4ValidationError(
                f"native Gazebo transport is required for causal pose control: {exc}"
            ) from exc
        self.node = Node()
        self.boolean_type = Boolean
        self.empty_type = Empty
        self.entity_plugin_type = EntityPlugin_V
        self.pose_vector_type = Pose_V
        self.scene_type = Scene
        self.twist_type = Twist
        self.pin_publishers: dict[str, Any] = {}
        self.pin_topics = {
            model: f"{CAUSAL_PIN_TOPIC_PREFIX}/{model}/cmd_vel"
            for model in CAUSAL_PIN_MODELS
        }
        self.last_zero_publish_monotonic_ns = 0
        self.zero_publish_count = 0
        self.pins_attached = False

    def _model_entity_ids(self) -> dict[str, int]:
        deadline_ns = time.monotonic_ns() + 5_000_000_000
        while time.monotonic_ns() < deadline_ns:
            ok, scene = self.node.request(
                "/world/map/scene/info",
                self.empty_type(),
                self.empty_type,
                self.scene_type,
                500,
            )
            if ok:
                by_name: dict[str, list[int]] = {}
                for model in scene.model:
                    by_name.setdefault(str(model.name), []).append(int(model.id))
                if all(
                    len(by_name.get(model, [])) == 1
                    and by_name[model][0] > 0
                    for model in CAUSAL_PIN_MODELS
                ):
                    resolved = {
                        model: by_name[model][0] for model in CAUSAL_PIN_MODELS
                    }
                    if len(set(resolved.values())) == len(CAUSAL_PIN_MODELS):
                        return resolved
            time.sleep(0.02)
        raise M4ValidationError("canonical UAV entity IDs are unavailable/ambiguous")

    def publish_zero_velocity(self, *, force: bool = False) -> int:
        now_ns = time.monotonic_ns()
        if (
            not force
            and now_ns - self.last_zero_publish_monotonic_ns
            < CAUSAL_PIN_PUBLISH_PERIOD_NS
        ):
            return self.last_zero_publish_monotonic_ns
        zero = self.twist_type()
        for model in CAUSAL_PIN_MODELS:
            publisher = self.pin_publishers.get(model)
            if publisher is None or not publisher.valid():
                raise M4ValidationError(f"zero-velocity publisher is invalid for {model}")
            publisher.publish(zero)
        published_ns = time.monotonic_ns()
        self.last_zero_publish_monotonic_ns = published_ns
        self.zero_publish_count += len(CAUSAL_PIN_MODELS)
        return published_ns

    def attach_velocity_pins(self) -> dict[str, Any]:
        if self.pins_attached:
            raise M4ValidationError("Gazebo velocity pins may only be attached once")
        entity_ids = self._model_entity_ids()
        for model in CAUSAL_PIN_MODELS:
            publisher = self.node.advertise(
                self.pin_topics[model], self.twist_type
            )
            if not publisher.valid():
                raise M4ValidationError(
                    f"Gazebo rejected zero-velocity topic for {model}"
                )
            self.pin_publishers[model] = publisher
        # The canonical Iris SDF contains no VelocityControl system.  A brief
        # discovery interval proves that these five absolute, unique topics
        # have no pre-existing subscriber before the one-shot live attach.
        preexisting_deadline_ns = time.monotonic_ns() + 250_000_000
        while time.monotonic_ns() < preexisting_deadline_ns:
            if any(
                self.pin_publishers[model].has_connections()
                for model in CAUSAL_PIN_MODELS
            ):
                raise M4ValidationError(
                    "pre-existing Gazebo VelocityControl subscriber differs"
                )
            time.sleep(0.01)
        system_add_request_count = 0
        for model in CAUSAL_PIN_MODELS:
            request = self.entity_plugin_type()
            request.entity.id = entity_ids[model]
            plugin = request.plugins.add()
            plugin.name = CAUSAL_PIN_PLUGIN_NAME
            plugin.filename = CAUSAL_PIN_PLUGIN_FILENAME
            plugin.innerxml = (
                f"<topic>{self.pin_topics[model]}</topic>"
                "<initial_linear>0 0 0</initial_linear>"
                "<initial_angular>0 0 0</initial_angular>"
            )
            # gz-sim8's live system-add request may time out while the system
            # is loaded on the next update.  The normative acknowledgement is
            # the exact plugin subscriber connection below, not this advisory
            # transport return value.
            self.node.request(
                CAUSAL_PIN_SYSTEM_ADD_SERVICE,
                request,
                self.entity_plugin_type,
                self.boolean_type,
                250,
            )
            system_add_request_count += 1
        connection_deadline_ns = time.monotonic_ns() + 5_000_000_000
        while time.monotonic_ns() < connection_deadline_ns:
            self.publish_zero_velocity(force=True)
            if all(
                self.pin_publishers[model].has_connections()
                for model in CAUSAL_PIN_MODELS
            ):
                for _unused in range(3):
                    self.publish_zero_velocity(force=True)
                    time.sleep(0.01)
                attached_ns = time.monotonic_ns()
                self.pins_attached = True
                return {
                    "models": list(CAUSAL_PIN_MODELS),
                    "model_entity_ids": entity_ids,
                    "plugin_name": CAUSAL_PIN_PLUGIN_NAME,
                    "plugin_filename": CAUSAL_PIN_PLUGIN_FILENAME,
                    "system_add_service": CAUSAL_PIN_SYSTEM_ADD_SERVICE,
                    "command_topics": dict(self.pin_topics),
                    "preexisting_subscribers": {
                        model: False for model in CAUSAL_PIN_MODELS
                    },
                    "system_add_request_models": list(CAUSAL_PIN_MODELS),
                    "system_add_request_count": system_add_request_count,
                    "zero_linear_velocity_mps": [0.0, 0.0, 0.0],
                    "zero_angular_velocity_radps": [0.0, 0.0, 0.0],
                    "publish_period_ns": CAUSAL_PIN_PUBLISH_PERIOD_NS,
                    "all_publishers_connected": True,
                    "initial_zero_publish_count": self.zero_publish_count,
                    "attached_monotonic_ns": attached_ns,
                }
            time.sleep(0.01)
        raise M4ValidationError("Gazebo VelocityControl subscribers did not connect")

    def wait_until(self, target_ns: int) -> None:
        while True:
            remaining = target_ns - time.monotonic_ns()
            if remaining <= 0:
                return
            self.publish_zero_velocity()
            time.sleep(min(0.01, remaining / 1_000_000_000))

    def set_pose_vector(
        self, positions: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(positions) != set(CAUSAL_POSE_VECTOR_MODELS):
            raise M4ValidationError("canonical atomic pose-vector entity set differs")
        request = self.pose_vector_type()
        for model in CAUSAL_POSE_VECTOR_MODELS:
            position = positions[model]
            if not isinstance(position, list) or len(position) != 3:
                raise M4ValidationError(f"canonical pose differs for {model}")
            pose = request.pose.add()
            pose.name = model
            pose.position.x, pose.position.y, pose.position.z = map(float, position)
            pose.orientation.w = 1.0
        started_ns = time.monotonic_ns()
        self.publish_zero_velocity(force=True)
        ok, response = self.node.request(
            CAUSAL_POSE_VECTOR_SERVICE,
            request,
            self.pose_vector_type,
            self.boolean_type,
            250,
        )
        completed_ns = time.monotonic_ns()
        if not ok or not bool(getattr(response, "data", False)):
            raise M4ValidationError("Gazebo rejected canonical blocking pose vector")
        latency_ns = completed_ns - started_ns
        if not 0 <= latency_ns <= CAUSAL_POSE_VECTOR_MAX_LATENCY_NS:
            raise M4ValidationError(
                "Gazebo blocking pose vector exceeded its 250-ms transport bound"
            )
        zero_ns = self.publish_zero_velocity(force=True)
        return {
            "pose_vector_service": CAUSAL_POSE_VECTOR_SERVICE,
            "pose_vector_models": list(CAUSAL_POSE_VECTOR_MODELS),
            "pose_vector_size": len(CAUSAL_POSE_VECTOR_MODELS),
            "pose_apply_started_monotonic_ns": started_ns,
            "pose_apply_completed_monotonic_ns": completed_ns,
            "pose_apply_latency_ns": latency_ns,
            "zero_velocity_published_monotonic_ns": zero_ns,
        }


def expected_pose(bundle: Mapping[str, Any], window: Mapping[str, Any]) -> Mapping[str, Any]:
    scenario = str(window["scenario"])
    if scenario in {"terrain_shadow", "building_blocked"}:
        return bundle["causal_scenarios"][scenario]["pose_sets"][window["pose_set"]]
    if scenario == "jammer_off_on_off":
        return bundle["causal_scenarios"][scenario]["pose_set"]
    return bundle["causal_scenarios"]["terrain_shadow"]["pose_sets"]["terrain_good"]


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
    # Resolve eagerly so a missing/escaped run directory still fails before
    # any Gazebo mutation, even though phase events use explicit child paths.
    _run_dir = args.run_dir.resolve(strict=True)
    contract = strict_json(args.contract.resolve(strict=True))
    bundle = strict_json(
        ROOT / "network/config/m4_canonical_scene_bundle.json"
    )
    windows = contract.get("windows")
    if (
        contract.get("contract") != "ams.m4.causality_run/v2"
        or not isinstance(windows, list)
        or [item.get("window_id") for item in windows] != list(WINDOW_IDS)
    ):
        raise M4ValidationError("phase driver contract/window sequence differs")
    client = GazeboPoseClient()
    pin_ready = client.attach_velocity_pins()
    write_exclusive(
        args.ready_file,
        {
            "contract": "ams.m4.causal-phase-driver-ready/v1",
            "run_id": contract["run_id"],
            "runtime_id": contract["runtime_id"],
            "pid": os.getpid(),
            "ready_monotonic_ns": time.monotonic_ns(),
            "velocity_pin": pin_ready,
        },
    )
    event_sequence = 1
    emit_event(
        args.event_dir,
        event_sequence,
        "causal_velocity_pin_ready",
        **pin_ready,
    )
    control_sequence = 0
    previous_id: str | None = None
    previous_end: int | None = None
    pose_fixture_sequence = 0
    expiry_target = matrix_link_identity("uav1.control.downlink")[1]
    for window in windows:
        window_id = str(window["window_id"])
        start_ns = int(window["start_monotonic_ns"])
        end_ns = int(window["end_monotonic_ns"])
        transition_ns = start_ns - causal_pre_window_gap_ns(window_id)
        if previous_end is not None and transition_ns < previous_end:
            raise M4ValidationError(
                "causal stimulus would overlap the previous measurement window"
            )
        client.wait_until(transition_ns)
        pose = expected_pose(bundle, window)
        pose_apply = client.set_pose_vector(pose)
        if (
            int(pose_apply["pose_apply_started_monotonic_ns"]) < transition_ns
            or int(pose_apply["pose_apply_completed_monotonic_ns"])
            > transition_ns + CAUSAL_POSE_VECTOR_MAX_LATENCY_NS
        ):
            raise M4ValidationError(
                "canonical atomic pose vector missed its transition deadline"
            )
        pose_fixture_sequence += 1
        control_sequence += 1
        adapter_control(
            args.adapter_control_dir,
            control_sequence,
            "set_jammer_enabled",
            not_before_monotonic_ns=transition_ns,
            enabled=bool(window["jammer_enabled"]),
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
            **pose_apply,
        )
        drain_ns = start_ns - 50_000_000
        client.wait_until(drain_ns)
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
        client.wait_until(start_ns)
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_measurement_start",
            window_id=window_id,
            target_monotonic_ns=start_ns,
        )
        next_pose_ns = start_ns
        expiry_seed_arm_done = False
        expiry_parallel_arm_done = False
        if window_id == "jammer_off_2":
            positive_plan = causal_window_plan(window_id)
            offered = int(positive_plan["offered_per_uav"])
            # Arm the held seed only after the preceding slot's full provider
            # deadline, then arm the newer query only after the seed slot's
            # full deadline.  Each arm still has an explicit settling margin
            # before its own factual ingress (ordinals N-1 and N).
            expiry_seed_arm_ns = (
                start_ns
                + causal_offer_offset_ns(window_id, offered - 2)
                + QUERY_DEADLINE_NS
                + EXPIRY_FAULT_ARM_SETTLE_NS
            )
            expiry_parallel_arm_ns = (
                start_ns
                + causal_offer_offset_ns(window_id, offered - 1)
                + QUERY_DEADLINE_NS
                + EXPIRY_FAULT_ARM_SETTLE_NS
            )
        else:
            expiry_seed_arm_ns = None
            expiry_parallel_arm_ns = None
        while time.monotonic_ns() < end_ns:
            now = time.monotonic_ns()
            if (
                window_id == "jammer_off_2"
                and not expiry_seed_arm_done
                and expiry_seed_arm_ns is not None
                and now >= expiry_seed_arm_ns
            ):
                # The adapter defers the injector arm until the exact next
                # factual uav1 downlink ingress, then force-submits one seed
                # query.  Refresh cannot consume this role-specific arm.
                control_sequence += 1
                adapter_control(
                    args.adapter_control_dir,
                    control_sequence,
                    "arm_hold_next",
                    not_before_monotonic_ns=expiry_seed_arm_ns,
                    directed_link_id=expiry_target,
                    directed_link="cp>uav1",
                    traffic_class="control",
                )
                expiry_seed_arm_done = True
            if (
                window_id == "jammer_off_2"
                and not expiry_parallel_arm_done
                and expiry_parallel_arm_ns is not None
                and now >= expiry_parallel_arm_ns
            ):
                # apply_control refuses this arm unless the seed's exact real
                # provider result is already held.  The final factual slot then
                # force-submits exactly one newer query with its own deadline.
                control_sequence += 1
                adapter_control(
                    args.adapter_control_dir,
                    control_sequence,
                    "arm_fault_parallel_next",
                    not_before_monotonic_ns=expiry_parallel_arm_ns,
                    directed_link_id=expiry_target,
                    directed_link="cp>uav1",
                    traffic_class="control",
                )
                expiry_parallel_arm_done = True
            pending_fault_arms = tuple(
                arm_ns
                for arm_ns, armed in (
                    (expiry_seed_arm_ns, expiry_seed_arm_done),
                    (expiry_parallel_arm_ns, expiry_parallel_arm_done),
                )
                if arm_ns is not None and not armed
            )
            pose_refresh_guard_ns = (
                CAUSAL_POSE_VECTOR_MAX_LATENCY_NS
                + EXPIRY_FAULT_ARM_SETTLE_NS
            )
            pose_refresh_guarded = any(
                0 <= arm_ns - now <= pose_refresh_guard_ns
                for arm_ns in pending_fault_arms
            )
            if now >= next_pose_ns:
                if not pose_refresh_guarded:
                    client.set_pose_vector(pose)
                # Guarded refresh slots are deliberately skipped, not caught
                # up in a burst after the exact fault arm has been written.
                next_pose_ns += CAUSAL_POSE_REFRESH_PERIOD_NS
            client.publish_zero_velocity()
            time.sleep(0.01)
        if window_id == "jammer_off_2" and not (
            expiry_seed_arm_done and expiry_parallel_arm_done
        ):
            raise M4ValidationError(
                "expiry staged fault arms missed their factual-traffic slots"
            )
        event_sequence += 1
        emit_event(
            args.event_dir,
            event_sequence,
            "window_measurement_end",
            window_id=window_id,
            target_monotonic_ns=end_ns,
        )
        if window_id == "expiry_unavailable":
            # Keep the old query pending until every unavailable-window send
            # has reached its exact three-second timeout.  Release/reorder and
            # restore freshness only inside the predeclared recovery gap.
            control_sequence += 1
            adapter_control(
                args.adapter_control_dir,
                control_sequence,
                "release_held",
                not_before_monotonic_ns=end_ns + 100_000_000,
                directed_link_id=expiry_target,
            )
            control_sequence += 1
            adapter_control(
                args.adapter_control_dir,
                control_sequence,
                "inject_duplicate",
                # The same not-before timestamp makes release+duplicate run
                # in one adapter poll, before the bounded capture ring can
                # evict the formerly-held immutable bytes.
                not_before_monotonic_ns=end_ns + 100_000_000,
                directed_link_id=expiry_target,
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
