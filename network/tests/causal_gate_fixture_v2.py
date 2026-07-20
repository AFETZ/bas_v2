"""Raw-event fixtures shared by strict causal-gate unit tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from network.validation.evidence import P0_GATE_IDS


SOURCE_HASH = "a" * 64
GIT_COMMIT = "b" * 40
RUNTIME_ID = "runtime-parent-0001"

SUMMARY_FILES = {
    "sionna_causality": "metrics/sionna_causality.json",
    "link_locality": "metrics/link_locality.json",
    "shared_medium": "metrics/contention_experiment.json",
    "priority": "metrics/priority_experiment.json",
    "jamming": "metrics/jammer_experiment.json",
    "time_coherence": "metrics/time_coherence.json",
    "scene_alignment": "metrics/scene_alignment.json",
    "repeatability": "metrics/repeatability.json",
}

RAW_FILES = {
    "sionna_causality": "logs/sionna_causality_events.jsonl",
    "link_locality": "logs/link_locality_events.jsonl",
    "shared_medium": "logs/contention_experiment_events.jsonl",
    "priority": "logs/priority_experiment_events.jsonl",
    "jamming": "logs/jammer_experiment_events.jsonl",
    "time_coherence": "logs/time_coherence_events.jsonl",
    "scene_alignment": "logs/scene_alignment_events.jsonl",
    "repeatability": "logs/repeatability_events.jsonl",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def delivery(event: str, tx: int, rx: int, latency: float, **extra: Any) -> dict[str, Any]:
    return {
        "event": event,
        "tx_packets": tx,
        "rx_packets": rx,
        "latency_ms": [latency] * rx,
        **extra,
    }


def positive_events(profile: str) -> list[dict[str, Any]]:
    if profile == "sionna_causality":
        result: list[dict[str, Any]] = []
        for index in (1, 2):
            correlation = f"correlation-{index}"
            query = f"query-{index}"
            result.extend(
                [
                    {
                        "event": "node_state",
                        "correlation_id": correlation,
                        "node_state_seq": index,
                        "node_id": "uav-1",
                        "position_m": [float(index * 10), 0.0, 10.0],
                    },
                    {
                        "event": "sionna_query",
                        "correlation_id": correlation,
                        "node_state_seq": index,
                        "node_id": "uav-1",
                        "query_id": query,
                        "provider_id": "tcp_jsonl_real_sionna",
                    },
                    {
                        "event": "link_state_applied",
                        "correlation_id": correlation,
                        "query_id": query,
                        "link_id": "uav-1/uav-2",
                        "provider_id": "tcp_jsonl_real_sionna",
                        "baseline_per": 0.01,
                        "applied_per": 0.30,
                    },
                    delivery(
                        "packet_outcome_baseline",
                        20,
                        20,
                        10.0,
                        correlation_id=correlation,
                        query_id=query,
                        link_id="uav-1/uav-2",
                    ),
                    delivery(
                        "packet_outcome_impaired",
                        20,
                        14,
                        30.0,
                        correlation_id=correlation,
                        query_id=query,
                        link_id="uav-1/uav-2",
                    ),
                ]
            )
        return result
    if profile == "link_locality":
        return [
            delivery("target_link_baseline", 100, 98, 20.0, link_id="uav-1/uav-2"),
            delivery("target_link_impaired", 100, 70, 60.0, link_id="uav-1/uav-2"),
            delivery("control_link_baseline", 100, 98, 20.0, link_id="uav-4/uav-5"),
            delivery("control_link_impaired", 100, 97, 22.0, link_id="uav-4/uav-5"),
        ]
    if profile == "shared_medium":
        common = {
            "medium_id": "wifi-medium-0",
            "capacity_bps": 1_000_000.0,
            "packet_size_bytes": 500,
            "duration_s": 1.0,
        }
        return [
            delivery(
                "single_flow_sample",
                100,
                100,
                10.0,
                flow_id="flow-a",
                offered_bps=400_000.0,
                **common,
            ),
            delivery(
                "concurrent_flow_sample",
                100,
                80,
                30.0,
                flow_id="flow-a",
                offered_bps=800_000.0,
                **common,
            ),
            delivery(
                "concurrent_flow_sample",
                100,
                80,
                30.0,
                flow_id="flow-b",
                offered_bps=800_000.0,
                **common,
            ),
            {"event": "queue_sample", "medium_id": "wifi-medium-0", "queue_depth_packets": 20, "queue_limit_packets": 100},
            {"event": "queue_sample", "medium_id": "wifi-medium-0", "queue_depth_packets": 40, "queue_limit_packets": 100},
        ]
    if profile == "priority":
        return [
            {"event": "overload_offer", "medium_id": "wifi-medium-0", "traffic_class": "control", "offered_bps": 800_000.0, "capacity_bps": 1_000_000.0},
            {"event": "overload_offer", "medium_id": "wifi-medium-0", "traffic_class": "payload", "offered_bps": 1_400_000.0, "capacity_bps": 1_000_000.0},
            delivery("control_delivery", 100, 98, 20.0, medium_id="wifi-medium-0", traffic_class="control"),
            delivery("payload_delivery", 100, 60, 100.0, medium_id="wifi-medium-0", traffic_class="payload"),
            {"event": "queue_sample", "medium_id": "wifi-medium-0", "queue_depth_packets": 30, "queue_limit_packets": 100},
            {"event": "queue_sample", "medium_id": "wifi-medium-0", "queue_depth_packets": 80, "queue_limit_packets": 100},
            *[
                {
                    "event": "scheduler_decision",
                    "medium_id": "wifi-medium-0",
                    "queue_owner": "ns3",
                    "control_backlog_packets": 2,
                    "payload_backlog_packets": 20,
                    "selected_class": "control",
                }
                for _ in range(3)
            ],
        ]
    if profile == "jamming":
        result = []
        for _ in range(2):
            result.append({"event": "jammer_off_before", "link_id": "uav-1/uav-2", "jammer_id": "jammer-1", "sinr_db": 20.0, "js_db": -10.0})
        result.append(delivery("packet_outcome", 100, 98, 20.0, phase="off_before", link_id="uav-1/uav-2"))
        for _ in range(2):
            result.append({"event": "jammer_on", "link_id": "uav-1/uav-2", "jammer_id": "jammer-1", "sinr_db": 8.0, "js_db": 5.0})
        result.append(delivery("packet_outcome", 100, 60, 100.0, phase="on", link_id="uav-1/uav-2"))
        for _ in range(2):
            result.append({"event": "jammer_off_after", "link_id": "uav-1/uav-2", "jammer_id": "jammer-1", "sinr_db": 19.5, "js_db": -9.0})
        result.append(delivery("packet_outcome", 100, 97, 22.0, phase="off_after", link_id="uav-1/uav-2"))
        return result
    if profile == "time_coherence":
        return [
            {"event": "realtime_sample", "sim_time_ns": 0, "_at_ns": 100_000_000},
            {"event": "pose_sample", "pose_seq": 1, "_at_ns": 200_000_000},
            {"event": "sionna_update", "update_id": "update-1", "pose_seq": 1, "_at_ns": 220_000_000},
            {"event": "packet_decision", "packet_id": "packet-1", "update_id": "update-1", "state_ttl_ns": 100_000_000, "_at_ns": 230_000_000},
            {"event": "pose_sample", "pose_seq": 2, "_at_ns": 400_000_000},
            {"event": "sionna_update", "update_id": "update-2", "pose_seq": 2, "_at_ns": 420_000_000},
            {"event": "packet_decision", "packet_id": "packet-2", "update_id": "update-2", "state_ttl_ns": 100_000_000, "_at_ns": 430_000_000},
            {"event": "realtime_sample", "sim_time_ns": 1_000_000_000, "_at_ns": 1_100_000_000},
            {"event": "realtime_sample", "sim_time_ns": 2_000_000_000, "_at_ns": 2_100_000_000},
        ]
    if profile == "scene_alignment":
        scene_hash = "c" * 64
        return [
            *[
                {"event": "scene_hash", "component": component, "scene_sha256": scene_hash}
                for component in ("gazebo", "sionna", "network")
            ],
            {
                "event": "frame_contract",
                "contract_id": "gazebo_to_sionna",
                "source_frame": "gazebo_enu",
                "target_frame": "sionna_enu",
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            {
                "event": "frame_contract",
                "contract_id": "gazebo_enu_to_ardupilot_ned",
                "source_frame": "gazebo_enu",
                "target_frame": "ardupilot_ned",
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.70710678, 0.70710678, 0.0, 0.0],
            },
            *[
                {
                    "event": "landmark_measurement",
                    "landmark_id": f"landmark-{index}",
                    "gazebo_xyz_m": [float(index), float(index * 2), 10.0],
                    "sionna_xyz_m": [float(index) + 0.2, float(index * 2), 10.0],
                }
                for index in (1, 2, 3)
            ],
        ]
    if profile == "repeatability":
        return []
    raise ValueError(profile)


def _write_repeatability_children(run_dir: Path, *, dirty_child: bool) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    child_gate_ids = set(P0_GATE_IDS) - {"repeatability"}
    for index in (1, 2):
        child_run_id = f"clean-clone-{index}"
        child_runtime_id = f"runtime-child-{index:04d}"
        child_dir = run_dir / "repeatability" / child_run_id
        child_dir.mkdir(parents=True, exist_ok=True)
        validation_path = child_dir / "validation.json"
        provenance_path = child_dir / "provenance.json"
        validation = {
            "schema_version": 2,
            "validation_engine": "network.validation.evidence",
            "run_id": child_run_id,
            "p0_passed": False,
            "gates": {
                "p0": {gate_id: {"status": "passed"} for gate_id in sorted(child_gate_ids)}
            },
        }
        provenance = {
            "schema_version": 2,
            "run_id": child_run_id,
            "runtime_id": child_runtime_id,
            "git_commit": GIT_COMMIT,
            "source_hash": SOURCE_HASH,
            "git_dirty": dirty_child and index == 2,
            "git_status": ["M forged"] if dirty_child and index == 2 else [],
        }
        validation_path.write_text(json.dumps(validation, sort_keys=True), encoding="utf-8")
        provenance_path.write_text(json.dumps(provenance, sort_keys=True), encoding="utf-8")
        events.append(
            {
                "event": "clean_clone_run",
                "child_run_id": child_run_id,
                "child_runtime_id": child_runtime_id,
                "validation_artifact": str(validation_path.relative_to(run_dir)),
                "validation_sha256": _sha256(validation_path),
                "provenance_artifact": str(provenance_path.relative_to(run_dir)),
                "provenance_sha256": _sha256(provenance_path),
            }
        )
    return events


def write_profile(
    root: Path,
    profile: str,
    *,
    events: list[dict[str, Any]] | None = None,
    dirty_repeatability_child: bool = False,
) -> Path:
    run_dir = root / f"run-{profile}"
    (run_dir / "metrics").mkdir(parents=True)
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "metrics" / "provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "source_hash": SOURCE_HASH,
                "git_commit": GIT_COMMIT,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics" / "joint_runtime.json").write_text(
        json.dumps({"schema_version": 2, "run_id": run_dir.name, "runtime_id": RUNTIME_ID}),
        encoding="utf-8",
    )
    payload = list(events if events is not None else positive_events(profile))
    if profile == "repeatability" and events is None:
        payload = _write_repeatability_children(
            run_dir, dirty_child=dirty_repeatability_child
        )
    ordered: list[dict[str, Any]] = []
    for index, record in enumerate(payload, start=1):
        item = dict(record)
        item.setdefault("_at_ns", index * 10_000_000)
        ordered.append(item)
    ordered.sort(key=lambda record: record["_at_ns"])
    raw_records = [{"event": "experiment_start", "_at_ns": 0}, *ordered]
    completion_time = max((record["_at_ns"] for record in raw_records), default=0) + 10_000_000
    raw_records.append({"event": "experiment_complete", "errors": [], "_at_ns": completion_time})
    envelope_records: list[dict[str, Any]] = []
    for event_seq, record in enumerate(raw_records):
        item = dict(record)
        monotonic_ns = item.pop("_at_ns")
        envelope_records.append(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": RUNTIME_ID,
                "source_hash": SOURCE_HASH,
                "experiment": profile,
                "event_seq": event_seq,
                "monotonic_ns": monotonic_ns,
                **item,
            }
        )
    raw_path = run_dir / RAW_FILES[profile]
    raw_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in envelope_records),
        encoding="utf-8",
    )
    summary_path = run_dir / SUMMARY_FILES[profile]
    summary = {
        "schema_version": 2,
        "run_id": run_dir.name,
        "runtime_id": RUNTIME_ID,
        "source_hash": SOURCE_HASH,
        "raw_event_log": str(raw_path.relative_to(run_dir)),
        "raw_event_sha256": _sha256(raw_path),
        "errors": [],
        # Deliberately forged/irrelevant: strict profiles must ignore these.
        "passed": True,
        "packet_outcome_changed": True,
        "both_runs_passed": True,
        "control_loss_rate": 0.0,
        "control_p95_ms": 0.0,
    }
    if profile == "scene_alignment":
        summary["scene_hash"] = "c" * 64
    summary_path.write_text(
        json.dumps(summary, sort_keys=True),
        encoding="utf-8",
    )
    return run_dir


def rewrite_raw(run_dir: Path, profile: str, mutate: Any) -> None:
    summary_path = run_dir / SUMMARY_FILES[profile]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    raw_path = run_dir / summary["raw_event_log"]
    records = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    mutate(records)
    raw_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    summary["raw_event_sha256"] = _sha256(raw_path)
    summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
