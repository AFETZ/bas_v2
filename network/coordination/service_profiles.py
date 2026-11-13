"""Versioned engineering service limits over the existing audited payload lineage.

This reducer never parses packets or substitutes PHY predictions for delivery.
Current TUNNEL load records are transport proxies; application semantics and
object integrity need their own measured endpoints.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import hashlib
import json
import math
from pathlib import Path
import re

from network.coordination.payload import LOAD_CLASSES, normalize_payload_load

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = ROOT / "network/config/service_profiles_v1.json"
UNITS = dict(useful_bitrate_bps="useful application bit/s, decimal",
             deadline_ms="ms from unique creation to UART OS-write completion",
             timely_delivery_ratio_min="fraction of unique offered records")


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{label} requires exactly {sorted(expected)}")


def _finite(value, label, low, high):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite number in [{low}, {high}]")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate service profile field: " + key)
        result[key] = value
    return result


def normalize_service_profiles(value):
    """Validate explicit values; no undocumented defaults or unknown fields."""
    _fields(value, {"schema_version", "profile_id", "scope", "units", "minimum_observation_s",
        "rolling_window_s", "rolling_step_s", "minimum_offered_records", "minimum_independent_replications",
        "minimum_records_per_stream", "record_boundary", "profiles", "evidence_policy", "sources"}, "service profile")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("service profile schema_version must be integer 1")
    if not isinstance(value["profile_id"], str) or not re.fullmatch("[a-z][a-z0-9_]*", value["profile_id"]):
        raise ValueError("service profile_id must be a stable lowercase identifier")
    if value["scope"] != "project_engineering_defaults_not_external_certification":
        raise ValueError("service scope must distinguish engineering defaults from certification")
    if value["units"] != UNITS or value["record_boundary"] != "application_creation_to_uart_os_write_end":
        raise ValueError("unsupported service units or measurement boundary")
    _finite(value["minimum_observation_s"], "minimum_observation_s", 60, 86400)
    _finite(value["rolling_window_s"], "rolling_window_s", 60, value["minimum_observation_s"])
    _finite(value["rolling_step_s"], "rolling_step_s", 1, value["rolling_window_s"])
    for name, minimum in [("minimum_offered_records", 1), ("minimum_independent_replications", 2),
                          ("minimum_records_per_stream", 1)]:
        if type(value[name]) is not int or not minimum <= value[name] <= 1_000_000_000:
            raise ValueError(name + " must be a bounded positive integer")
    _fields(value["profiles"], LOAD_CLASSES, "service classes")
    for name, selected in value["profiles"].items():
        _fields(selected, {"useful_bitrate_bps", "deadline_ms", "timely_delivery_ratio_min", "application"}, name)
        if type(selected["useful_bitrate_bps"]) is not int or selected["useful_bitrate_bps"] != LOAD_CLASSES[name][0]:
            raise ValueError(name + " must use the existing decimal payload class rate")
        _finite(selected["deadline_ms"], name + ".deadline_ms", .001, 60000)
        _finite(selected["timely_delivery_ratio_min"], name + ".timely_delivery_ratio_min", .000001, 1)
        if not isinstance(selected["application"], str) or not selected["application"]:
            raise ValueError(name + ".application must be nonempty")
    if not isinstance(value["evidence_policy"], str) or not value["evidence_policy"]:
        raise ValueError("service evidence_policy is required")
    if not isinstance(value["sources"], list) or not value["sources"]:
        raise ValueError("service provenance sources are required")
    for source in value["sources"]:
        _fields(source, {"url", "basis"}, "service source")
        if (not isinstance(source["url"], str) or not source["url"].startswith("https://")
                or not isinstance(source["basis"], str) or not source["basis"]):
            raise ValueError("service sources require HTTPS URL and explicit basis")
    return copy.deepcopy(value)


def load_service_profiles(path=None):
    path = DEFAULT_PROFILE if path is None else Path(path)
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique)
    try:
        source = path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        source = str(path)
    return dict(config=normalize_service_profiles(value), path=source,
                sha256=hashlib.sha256(raw).hexdigest())


def _combined_status(statuses):
    for status in ("invalid", "measured_fail", "insufficient_evidence"):
        if status in statuses:
            return status
    return "measured_pass" if statuses and all(s == "measured_pass" for s in statuses) else "not_measured"


def qualify_payload_services(lineage, profiles=None, *, run_metadata=None):
    """Consume analyze_payload_lineage output; keep all offered records.

    Existing outcomes establish identity/receipt provenance. Class deadlines are
    recomputed from the preserved timestamps, not copied from legacy250ms labels.
    """
    bundle = load_service_profiles() if profiles is None else profiles
    config = normalize_service_profiles(bundle["config"])
    if not isinstance(bundle.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", bundle["sha256"]):
        raise ValueError("service profile SHA-256 is required")
    profile_path = Path(bundle["path"])
    original = load_service_profiles(profile_path if profile_path.is_absolute() else ROOT / profile_path)
    if original["sha256"] != bundle["sha256"] or original["config"] != config:
        raise ValueError("service profile content/path/SHA-256 binding disagrees")
    metadata = {} if run_metadata is None else run_metadata
    if not isinstance(metadata, dict):
        raise ValueError("run_metadata must be an object")
    seed, run = metadata.get("rng_seed"), metadata.get("rng_run")
    pair_valid = (type(seed) is int and 1 <= seed <= 2**32 - 1
                  and type(run) is int and 0 <= run <= 2**64 - 1)
    replication = dict(
        status="insufficient_replications",
        observed_independent_replications=1 if pair_valid else 0,
        minimum_independent_replications=config["minimum_independent_replications"],
        minimum_records_per_stream=config["minimum_records_per_stream"],
        identity_status="recorded_conditional_ns3_identity" if pair_valid else "missing_or_invalid_rng_identity",
        identity=dict(run_id=metadata.get("run_id"), rng_seed=seed, rng_run=run,
                      key=[seed, run] if pair_valid else None,
                      rt_solver_seed=metadata.get("rt_solver_seed"),
                      source_commit=metadata.get("source_commit")),
        independence_scope="Configuredns3substreams conditional on the same fixedinputs and streamassignment; RTsampling independence is not implied.",
        counting_rule="Count a fixedseed with distinctRngRun substreams; repeated(seed,run) is execution repeatability, not another independent replication.",
        planning_basis="Ten replications and10000 records perstream are selected project engineering planning defaults, not3GPPnorms.")
    digest = hashlib.sha256()
    for row in lineage.get("records", []):
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
    bindings = dict(
        service_profile=dict(path=bundle["path"], sha256=bundle["sha256"]),
        raw_lineage_files=copy.deepcopy(lineage.get("trace_files", [])),
        normalized_records_sha256=digest.hexdigest(),
        offered_config_sha256=lineage.get("offered_load", {}).get("config_sha256"),
        offered_config_file=dict(path="metrics/relay_experiment_config.json",
                                 sha256=lineage.get("offered_load", {}).get("config_sha256")),
        runtime_metadata_files=copy.deepcopy(metadata.get("source_files", [])),
        runtime_source_commit=metadata.get("source_commit"),
        runtime_source_dirty_paths=metadata.get("source_dirty_paths"),
        run_id=metadata.get("run_id"),
        note="Upstream trace provenance is a necessary validity gate; supplied pass/status labels never determine measured service limits.")
    result = dict(schema_version=1, profile_id=config["profile_id"],
        service_profile_sha256=bundle["sha256"], service_profile_path=bundle["path"],
        evidence_kind="measured", qualification_scope="observed_single_run_limits_only",
        semantic_scope="synthetic_transport_load_proxy_not_object_or_critical_command_semantics",
        record_boundary=config["record_boundary"], independent_series_status="insufficient_evidence",
        replication_qualification=replication, source_bindings=bindings,
        series_requirements=dict(minimum_independent_replications=config["minimum_independent_replications"],
                                 minimum_records_per_stream=config["minimum_records_per_stream"]),
        classes={name: dict(status="not_measured", streams=[], limits=copy.deepcopy(limits))
                 for name, limits in config["profiles"].items()})
    load = lineage.get("offered_load", {})
    profile = load.get("profile")
    if not isinstance(profile, dict) or profile.get("class") not in LOAD_CLASSES:
        result["status"] = "invalid" if load.get("status") == "unverifiable" else "not_measured"
        return result
    name = profile["class"]
    selected = result["classes"][name]
    rows = [row for row in lineage.get("records", []) if row.get("load_id") is not None]
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("uav_id"), row.get("direction"), row.get("process_epoch")].append(row)
    window = load.get("load_window", {})
    start, stop = window.get("start_host_monotonic_ns"), window.get("end_host_monotonic_ns")
    interval_valid = type(start) is int and type(stop) is int and 0 <= start < stop
    invalid_reasons = []
    trace_files = lineage.get("trace_files")
    if not isinstance(trace_files, list) or not trace_files:
        invalid_reasons.append("missing_raw_lineage_bindings")
    else:
        for entry in trace_files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if (not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts
                    or entry.get("status") != "observed"
                    or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch("[0-9a-f]{64}", entry["sha256"])
                    or type(entry.get("bytes")) is not int or entry["bytes"] < 0):
                invalid_reasons.append("invalid_or_changing_raw_lineage_binding")
                break
    if (not isinstance(load.get("config_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", load["config_sha256"])):
        invalid_reasons.append("missing_offered_configuration_binding")
    try:
        if normalize_payload_load(profile, tuple(range(1, 6))) != profile:
            invalid_reasons.append("frozen_load_not_normalized")
    except (ValueError, TypeError, KeyError):
        invalid_reasons.append("frozen_load_not_normalized")
    if lineage.get("measurement_eligible") is not True:
        invalid_reasons.append("underlying_lineage_or_runtime_measurement_not_eligible")
    if load.get("status") != "recorded" or load.get("generator_eligible") is not True:
        invalid_reasons.append("offered_load_generator_not_qualified")
    if not interval_valid:
        invalid_reasons.append("missing_exact_offered_window")
    if any(count != 1 for count in Counter(row.get("frame_uid") for row in rows).values()):
        invalid_reasons.append("duplicate_offered_record_identity")
    expected = {(uav, profile.get("direction")) for uav in profile.get("uav_ids", [])}
    if {(uav, direction) for uav, direction, _epoch in groups} != expected:
        invalid_reasons.append("stream_roster_disagrees_with_frozen_load")
    limits = config["profiles"][name]
    length_ns = round(config["rolling_window_s"] * 1e9)
    step_ns = round(config["rolling_step_s"] * 1e9)
    deadline_ns = round(limits["deadline_ms"] * 1e6)
    requested_bps = profile.get("useful_bitrate_bps")
    if (type(requested_bps) is not int or requested_bps < limits["useful_bitrate_bps"]
            or name != "20mbps_plus" and requested_bps != limits["useful_bitrate_bps"]):
        invalid_reasons.append("requested_rate_disagrees_with_class")
    for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
        stream = dict(uav_id=key[0], direction=key[1], process_epoch=key[2], windows=[],
                      invalid_reasons=list(invalid_reasons))
        if invalid_reasons:
            stream["status"] = "invalid"
        elif stop - start < round(config["minimum_observation_s"] * 1e9):
            stream.update(status="insufficient_evidence", reason="observation_shorter_than_minimum")
        else:
            ordered = sorted(members, key=lambda row: row["created_host_monotonic_ns"])
            low = high = 0
            counts = Counter()
            useful = 0

            def outcome(row):
                created, accepted = row["created_host_monotonic_ns"], row.get("accepted_host_monotonic_ns")
                horizon = row.get("target_observation_end_host_monotonic_ns")
                if row.get("outcome") == "unverifiable":
                    return "unverifiable"
                if accepted is not None:
                    if type(accepted) is not int or accepted < created:
                        return "unverifiable"
                    return "timely" if accepted <= created + deadline_ns else "late"
                return ("right_censored" if type(horizon) is not int or horizon < created + deadline_ns
                        else "missing")

            window_starts = list(range(start, stop - length_ns + 1, step_ns))
            if not window_starts or window_starts[-1] != stop - length_ns:
                window_starts.append(stop - length_ns)  # Cover the final sub-step tail too.
            for left in window_starts:
                right = left + length_ns
                # Each row enters/leaves at most once, even in long rolling series.
                while low < high and ordered[low]["created_host_monotonic_ns"] < left:
                    row = ordered[low]
                    category = outcome(row)
                    counts[category] -= 1
                    useful -= row["application_useful_bytes"] if category == "timely" else 0
                    low += 1
                while high < len(ordered) and ordered[high]["created_host_monotonic_ns"] < right:
                    row = ordered[high]
                    category = outcome(row)
                    counts[category] += 1
                    useful += row["application_useful_bytes"] if category == "timely" else 0
                    high += 1
                count = high - low
                ratio = counts["timely"] / count if count else None
                bps = useful * 8 / config["rolling_window_s"]
                checks = dict(timely_delivery_ratio=ratio >= limits["timely_delivery_ratio_min"] if ratio is not None else None,
                              deadline_qualified_useful_rate=bps >= requested_bps * limits["timely_delivery_ratio_min"])
                status = ("invalid" if counts["unverifiable"] else "insufficient_evidence"
                          if count < config["minimum_offered_records"] or counts["right_censored"] else
                          "measured_pass" if all(checks.values()) else "measured_fail")
                stream["windows"].append(dict(start_host_monotonic_ns=left, end_host_monotonic_ns=right,
                    status=status, offered=count, timely=counts["timely"], late=counts["late"],
                    missing=counts["missing"], right_censored=counts["right_censored"],
                    unverifiable=counts["unverifiable"], timely_per_offered=ratio,
                    deadline_qualified_useful_bps=bps, requested_useful_bps=requested_bps, checks=checks))
            stream["status"] = _combined_status([item["status"] for item in stream["windows"]])
        selected["streams"].append(stream)
    selected["status"] = "invalid" if invalid_reasons else _combined_status([s["status"] for s in selected["streams"]])
    selected["invalid_reasons"] = invalid_reasons
    result["status"] = selected["status"]
    return result
