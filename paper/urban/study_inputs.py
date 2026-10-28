"""Pure presentation transforms for saved urban_study/per-run exports.

No bootstrap, simulation, imputation or favourable-run selection occurs here.
Missing values remain None; estimates and intervals come from the study export.
"""
from collections import defaultdict
import statistics

PRIMARY_METRICS = ("timely_per_offered", "completed_waypoint_fraction")
METRIC_LABELS = {
    "timely_per_offered": "Timely/offered, п.п.",
    "completed_waypoint_fraction": "Точки/план, п.п.",
    "restricted_post_NLOS_UART_60s": "Ограниченное отсутствие UART, с",
}


def validate_closed_study(manifest, freeze, study, controllers, freeze_sha256):
    """A final manuscript may retain failures but cannot precede attempt closure.

    Controllers are saved launcher records, not run-success flags. A nonzero
    return code is a closed failed attempt, never a reason to remove its row.
    """
    if study is None or manifest.get("kind") != "frozen_series":
        raise ValueError("--final requires study_json and kind=frozen_series")
    if not manifest.get("source_commit") or manifest["source_commit"] != freeze.get("source_commit"):
        raise ValueError("frozen and manuscript runtime versions differ")
    cases = freeze.get("cases", [])
    expected = {r["run_id"]: r for r in cases}
    records = study.get("runs", [])
    if (not cases or len(expected) != len(cases) or len(records) != len(expected)
            or {r["run_id"] for r in records} != set(expected)
            or study.get("planned_attempts") != len(expected)
            or study.get("observed_attempts") != len(expected)):
        raise ValueError("final document requires the complete planned attempt roster")
    if study.get("freeze_sha256") != freeze_sha256:
        raise ValueError("aggregate and freeze differ")
    for record in records:
        rid = record["run_id"]
        controller = controllers.get(rid)
        if controller is None or record.get("controller_recorded") is not True:
            raise ValueError(f"{rid}: missing attempt controller")
        code = controller.get("returncode")
        start, end = controller.get("started_host_monotonic_ns"), controller.get("ended_host_monotonic_ns")
        if (not isinstance(code, int) or isinstance(code, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start):
            raise ValueError(f"{rid}: attempt was started but its controller is not closed")
        if record.get("controller_returncode") != code:
            raise ValueError(f"{rid}: aggregate/controller completion differs; re-export")
        if (controller.get("run_id") != rid or controller.get("source_commit") != freeze["source_commit"]
                or controller.get("config_sha256") != expected[rid].get("config_sha256")):
            raise ValueError(f"{rid}: controller identity/source/config differs from freeze")


def cell_key(row):
    return tuple(row.get(key, "main" if key == "cohort" else "nominal") for key in
                 ("cohort", "scenario_variant", "parameter_variant")) + (row["radio_mode"], row["policy"])


def cells(study):
    groups = defaultdict(list)
    for run in study.get("runs", []):
        groups[cell_key(run)].append(run)
    output = []
    for key, rows in sorted(groups.items()):
        eligible = [r for r in rows if r.get("primary_eligible") is True]
        values = {}
        for metric in PRIMARY_METRICS + ("new_relay_selections", "integral_RTF"):
            observed = [r.get("outcomes", {}).get(metric) for r in eligible
                        if r.get("outcomes", {}).get(metric) is not None]
            values[metric] = statistics.fmean(observed) if observed else None
            values[metric + "_n"] = len(observed)
        output.append(dict(zip(("cohort", "scenario_variant", "parameter_variant", "radio_mode", "policy"), key),
            planned=len(rows), attempted=sum(r.get("attempted") is True for r in rows),
            failed=sum(r.get("attempt_failed") is True for r in rows), eligible=len(eligible), **values))
    return output


def contrasts(study, metrics=PRIMARY_METRICS):
    output = []
    for record in study.get("paired_contrasts", []):
        if record.get("metric") not in metrics:
            continue
        estimate = record.get("failover_minus_fixed", {})
        scale = 100 if record["metric"] in PRIMARY_METRICS else 1
        limits = estimate.get("interval_95")
        bounds = record.get("missing_outcome_sensitivity") or {}
        range_bounds = bounds.get("mean_difference_bounds")
        output.append(dict(cohort=record.get("cohort"), scenario_variant=record.get("scenario_variant"),
            parameter_variant=record.get("parameter_variant"), radio_mode=record.get("radio_mode"),
            metric=record["metric"], metric_label=METRIC_LABELS[record["metric"]],
            planned_pairs=record.get("planned_pairs"), complete_pairs=record.get("complete_pairs"),
            mean=scale*estimate["mean"] if estimate.get("mean") is not None else None,
            interval_95=[scale*x for x in limits] if limits is not None else None,
            missing_bounds=[scale*x for x in range_bounds] if range_bounds is not None else None,
            missing_bounds_status=bounds.get("status"),
            missing_pair_ids=record.get("missing_pair_ids", []),
            method=estimate.get("method")))
    return output


def interactions(study):
    output = []
    for record in study.get("policy_geometry_interactions", []):
        if record.get("metric") not in PRIMARY_METRICS:
            continue
        estimate = record.get("estimate", {})
        limits = estimate.get("interval_95")
        bounds = record.get("missing_outcome_sensitivity") or {}
        range_bounds = bounds.get("mean_interaction_bounds")
        output.append(dict(metric=record["metric"], metric_label=METRIC_LABELS[record["metric"]],
            planned_blocks=record.get("planned_blocks"), complete_blocks=record.get("complete_four_case_blocks"),
            mean=100*estimate["mean"] if estimate.get("mean") is not None else None,
            interval_95=[100*x for x in limits] if limits is not None else None,
            missing_bounds=[100*x for x in range_bounds] if range_bounds is not None else None))
    return output


def stage_counts(study, analyses):
    """Sum named stages only over complete eligible windows with every count.

    analyses is keyed by true run_id. No application receipt is inferred from
    UART or physical completion, and missing receipt exports are not zeros.
    """
    keys = ("target_CIDs_created_in_window", "master_receive_CIDs",
            "follower_application_receive_CIDs", "UART_write_end_CIDs",
            "audited_valid_UART_CIDs", "independently_verified_unique_inspection_goals")
    groups = defaultdict(list)
    for record in study.get("runs", []):
        groups[cell_key(record)].append(record)
    output = []
    for key, records in sorted(groups.items()):
        eligible = [r for r in records if r.get("primary_eligible") is True]
        complete, absent = [], []
        for row in eligible:
            actual = analyses.get(row["run_id"], {})
            stages = actual.get("target_stage_counts", {})
            offered = actual.get("rates", {}).get("all_five", {}).get("offered")
            if offered is None or any(stages.get(name) is None for name in keys):
                absent.append(row["run_id"])
            else:
                complete.append((offered, stages))
        totals = {name: sum(s[name] for _, s in complete) if complete else None for name in keys}
        output.append(dict(zip(("cohort", "scenario_variant", "parameter_variant", "radio_mode", "policy"), key),
            eligible_runs=len(eligible), complete_stage_runs=len(complete), missing_stage_run_ids=absent,
            offered=sum(n for n, _ in complete) if complete else None, **totals))
    return output
