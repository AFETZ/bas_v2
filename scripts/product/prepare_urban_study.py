#!/usr/bin/env python3
"""Prepare urban blocks or freeze real inputs; this tool never runs a simulation."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import re
import subprocess
import time

SPEC = importlib.util.spec_from_file_location("urban_study_analysis", Path(__file__).with_name("analyze_urban_experiment.py"))
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)


def design(repeats=10, order_seed=20261028, rng_seed=1):
    rows = [dict(row, cohort="main", scenario_variant="nominal", parameter_variant="nominal")
            for row in A.study_plan(repeats, order_seed, rng_seed)]
    generator = random.Random(order_seed+1)
    pairs = [(parameter, repeat) for parameter in ("low", "high") for repeat in range(1,4)]
    generator.shuffle(pairs)
    for parameter, repeat in pairs:
        policies = ["fixed", "failover"]
        generator.shuffle(policies)
        for policy in policies:
            rows.append(dict(order=len(rows)+1, block_id=f"sensitivity-{parameter}-s{rng_seed}-r{repeat:02d}",
                rng_seed=rng_seed, rng_run=repeat, policy=policy, radio_mode="geometry", cohort="sensitivity",
                scenario_variant="nominal", parameter_variant=parameter,
                status="planned_not_frozen_not_run", order_seed=order_seed+1))
    for scenario in ("short_occlusion", "no_candidate"):
        policies = ["fixed", "failover"]
        generator.shuffle(policies)
        for policy in policies:
            rows.append(dict(order=len(rows)+1, block_id=f"engineering-{scenario}-s{rng_seed}-r01",
                rng_seed=rng_seed, rng_run=1, policy=policy, radio_mode="geometry", cohort="engineering_boundary",
                scenario_variant=scenario, parameter_variant="nominal", order_seed=order_seed+1,
                status="planned_not_frozen_not_run"))
    for row in rows:
        row["case_id"] = f"{row['cohort']}-{row['scenario_variant']}-{row['parameter_variant']}-{row['radio_mode']}-{row['policy']}-r{row['rng_run']:02d}"
    return rows


def artifact(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"required input does not exist: {path}")
    return dict(path=str(path), bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def freeze(plan, *, scenarios, pilot_analyses, reviews, image, horizon_s, output, root, study_id=None):
    """Freeze existing documents, not an approval or proof inferred from labels.

    Scope/parameter decisions and independent reviewer text are supplied by root.
    Pilots and model probes must already exist; freezing cannot manufacture them.
    """
    output, root = Path(output), Path(root)
    if output.exists():
        raise ValueError("freeze output already exists; use a new identity, never overwrite")
    if horizon_s <= 0:
        raise ValueError("positive common model-time observation horizon required")
    required = {"nominal", "los_only", "low", "high", "short_occlusion", "no_candidate"}
    if set(scenarios) != required:
        raise ValueError(f"explicit scenario inputs required: {sorted(required)}")
    if len(pilot_analyses) < 2 or not reviews:
        raise ValueError("at least two recorded pilot analyses and independent review artifact required")
    pilot_rows = []
    for path in pilot_analyses:
        row = A.RELAY.read_json(Path(path))
        if row.get("analysis_version") != 1 or not row.get("observation_window", {}).get("complete"):
            raise ValueError(f"pilot window incomplete or unknown analysis: {path}")
        if row.get("missing_measurements") or row.get("data_quality_errors"):
            raise ValueError(f"pilot inputs incomplete: {path}")
        # Negative/partial mission outcomes remain admissible. A pilot's protocol
        # or recording defect cannot be converted to no-benefit experimental data.
        authority = row.get("authority", {})
        if authority.get("overlapping_issued_grants") or authority.get("unverifiable_admissions"):
            raise ValueError(f"pilot authority unverifiable: {path}")
        audit = authority.get("admission_audit", {})
        if any(value for key,value in audit.items() if key != "valid"):
            raise ValueError(f"pilot UART audit has violations/unknowns: {path}")
        pilot_rows.append(row)
    if not {"fixed", "failover"} <= {r.get("policy") for r in pilot_rows}:
        raise ValueError("fixed and failover pilot analyses required")
    if not any(r.get("trigger", {}).get("status") == "observed" for r in pilot_rows):
        raise ValueError("no pilot observed the primary geometric transition")
    if not any(all((v.get("max_horizontal_displacement_m") or 0) >= 5
                   for v in row.get("mission_progress", {}).values())
               and set(row.get("mission_progress", {})) == {str(i) for i in A.UAVS} for row in pilot_rows):
        raise ValueError("no pilot proves at least5m actual horizontal movement of allfive")
    git = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("tracked source is dirty; commit reviewed implementation before freeze")
    if not image.startswith("sha256:") or len(image) != 71:
        raise ValueError("immutable Docker image ID required")
    # Read-only inspect also proves that the referenced pinned local image exists.
    observed_image = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
    if image != observed_image:
        raise ValueError("image identity mismatch")
    study_id = study_id or output.stem
    if not re.fullmatch(r"[A-Za-z0-9_-]+", study_id):
        raise ValueError("study ID must be a simple basename")
    config_inputs = {key:artifact(path) for key,path in scenarios.items()}
    nominal = A.RELAY.read_json(Path(scenarios["nominal"]))
    control = A.RELAY.read_json(Path(scenarios["los_only"]))
    # This is a radio ablation on the very same mission and physical scene.
    nominal_without_mode = json.loads(json.dumps(nominal))
    control_without_mode = json.loads(json.dumps(control))
    nominal_without_mode["radio"].pop("mode")
    control_without_mode["radio"].pop("mode")
    if nominal_without_mode != control_without_mode:
        raise ValueError("LOS-only control differs from nominal beyond radio.mode")
    cases = []
    for row in plan["rows"]:
        variant = ("los_only" if row["radio_mode"] == "los_only" else row["scenario_variant"]
                   if row["scenario_variant"] != "nominal" else row["parameter_variant"])
        config = Path(scenarios[variant]).resolve()
        config.relative_to(root.resolve())  # must be mounted by the existing launcher
        content = A.RELAY.read_json(config)
        if content.get("radio", {}).get("mode") != row["radio_mode"]:
            raise ValueError(f"radio mode differs from planned case: {row['case_id']}")
        cases.append(dict(row, run_id=f"{study_id}-{row['case_id']}", policy=row["policy"],
            duration_s=horizon_s, rng_run=row["rng_run"], config=str(config),
            config_sha256=config_inputs[variant]["sha256"], status="frozen_not_run"))
    blocks = {r["block_id"]:(r["rng_seed"],r["rng_run"]) for r in cases if r["cohort"]=="main"}
    if len(set(blocks.values())) != len(blocks):
        raise ValueError("independent main blocks reuse an RNG seed/run")
    result = dict(status="frozen_inputs_not_execution_results", schema_version=1,
        frozen_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        frozen_at_host_monotonic_ns=time.monotonic_ns(),
        source_commit=git("rev-parse", "HEAD"), image_id=image,
        parameter_snapshots=True, parameter_workers=5,
        land_timeout_s=60,
        main_block_RNG_pairs=blocks,
        ablation_check="all JSON content identical after removing radio.mode",
        outcome_spec=dict(primary=["fixed-horizon timely/offered", "configured physical waypoint progress"],
            optional_restricted_horizon_host_s=60, physical_hold_source_s=2,
            route_hold_autopilot_s=3, inference_unit="independent whole seed block"),
        base_environment_source=artifact(root/"runs/paper-live-20261026/campaign/freeze.json"),
        base_environment=A.RELAY.read_json(root/"runs/paper-live-20261026/campaign/freeze.json")["base_environment"],
        native_binary=artifact(root/".external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-tap-spike-default"),
        observation_horizon_source_sim_time_s=horizon_s,
        study_id=study_id, rows=plan["rows"], cases=cases, scenario_inputs=config_inputs,
        pilot_analyses=[artifact(path) for path in pilot_analyses],
        independent_reviews=[artifact(path) for path in reviews],
        review_scope="real review artifacts are attached; their scientific judgments are not inferred by this tool",
        model_scope="geometry-conditioned empirical loss; no site calibration inferred",
        censoring="valid nonrecovery at full administrative horizon only; crashes and missing logs remain failed/interrupted",
        statistical_unit="independent seed block/paired runs, never packet/UAV/episode",
        no_replacement="preserve every planned attempt and failure; no stopping on significance")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False, sort_keys=True)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--repeats", type=int, choices=(5,10), default=10)
    plan.add_argument("--order-seed", type=int, default=20261028)
    plan.add_argument("--rng-seed", type=int, default=1)
    plan.add_argument("--output", type=Path, required=True)
    frozen = sub.add_parser("freeze")
    frozen.add_argument("--plan", type=Path, required=True)
    frozen.add_argument("--scenario", action="append", required=True, help="name=immutable_JSON; names nominal,los_only,low,high,short_occlusion,no_candidate")
    frozen.add_argument("--pilot-analysis", type=Path, action="append", required=True)
    frozen.add_argument("--review", type=Path, action="append", required=True)
    frozen.add_argument("--image-id", required=True)
    frozen.add_argument("--horizon-s", type=float, required=True)
    frozen.add_argument("--output", type=Path, required=True)
    frozen.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    frozen.add_argument("--study-id")
    args = parser.parse_args()
    if args.command == "plan":
        if args.output.exists():
            parser.error("plan path exists; choose a new identity")
        A.RELAY.write_json(args.output, dict(status="planned_not_frozen_not_run", repeats=args.repeats,
            order_seed=args.order_seed, rng_seed=args.rng_seed,
            rows=design(args.repeats, args.order_seed, args.rng_seed)))
    else:
        scenarios = dict(value.split("=", 1) for value in args.scenario)
        plan = A.RELAY.read_json(args.plan)
        expected = design(plan.get("repeats", 0), plan.get("order_seed", 0), plan.get("rng_seed", 0))
        if plan.get("status") != "planned_not_frozen_not_run" or plan.get("rows") != expected:
            parser.error("plan does not match declared seed/order/count")
        freeze(plan, scenarios=scenarios, pilot_analyses=args.pilot_analysis, reviews=args.review,
            image=args.image_id, horizon_s=args.horizon_s, output=args.output, root=args.root, study_id=args.study_id)


if __name__ == "__main__":
    main()
