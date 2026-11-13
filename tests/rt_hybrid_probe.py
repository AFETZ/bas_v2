#!/usr/bin/env python3
"""Matched full-RT/Friis technical probe; preparation never imports GPU modules.

This is a qualification experiment, not a production hybrid selector. Existing
canonical rich-RT flags stay unchanged. The only candidate policy is verified
empty-geometry, iso1x1/V SISO PSD; every nonempty scene selects full RT.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import inspect
import json
import math
from pathlib import Path
import time
import traceback
import xml.etree.ElementTree as ET

PINS = {"sionna-rt": "1.2.0", "mitsuba": "3.7.1", "drjit": "1.2.0"}
SOURCES = {
    "sionna_solver": "https://github.com/NVlabs/sionna-rt/blob/v1.2.0/src/sionna/rt/path_solvers/path_solver.py",
    "sionna_paths": "https://github.com/NVlabs/sionna-rt/blob/v1.2.0/src/sionna/rt/path_solvers/paths.py",
    "sionna_visibility": "https://github.com/NVlabs/sionna-rt/blob/v1.2.0/src/sionna/rt/path_solvers/sb_candidate_generator.py",
    "ns3_friis": "https://gitlab.com/nsnam/ns-3-dev/-/blob/d2add90b452d600cfb4859baed8e9ea633519447/src/spectrum/model/friis-spectrum-propagation-loss.cc",
    "mitsuba": "https://github.com/mitsuba-renderer/mitsuba3/blob/v3.7.1/include/mitsuba/render/scene.h"}
FLAGS = dict(max_depth=2, los=True, specular_reflection=True, diffuse_reflection=False,
             diffraction=True, edge_diffraction=True, refraction=False, synthetic_array=True)
BUDGETS = [dict(samples_per_src=20000 * factor, max_num_paths_per_receiver=1000 * factor)
           for factor in (1, 2, 4)]
CRITERIA = dict(source="selected_project_engineering_defaults_not_industry_acceptance",
    minimum_matched_samples_per_stratum=1000, absolute_bias_db_max=1.0,
    absolute_error_p95_db_max=3.0, absolute_error_p99_db_max=6.0,
    coherent_nmse_max=0.01, state_age_ns_max=100000000,
    rt_reference_convergence=dict(absolute_bias_db_max=0.25,
        absolute_error_p95_db_max=0.5, absolute_error_p99_db_max=1.0),
    convergence_scope="Compare highest two budgets and every listed seed on identical endpoints/flags; no post-hoc best seed.")
C = 299792458.0


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def scene_xml(boxes):
    scene = ET.Element("scene", version="2.1.0")
    if boxes:
        material = ET.SubElement(scene, "bsdf", type="itu-radio-material", id="material-concrete")
        ET.SubElement(material, "string", name="type", value="concrete")
        ET.SubElement(material, "float", name="thickness", value="0.2")
    for index, (lo, hi) in enumerate(boxes):
        shape = ET.SubElement(scene, "shape", type="cube", id=f"mesh-probe-{index}")
        transform = ET.SubElement(shape, "transform", name="to_world")
        ET.SubElement(transform, "scale", **{a:str((h-l)/2) for a,l,h in zip("xyz",lo,hi)})
        ET.SubElement(transform, "translate", **{a:str((h+l)/2) for a,l,h in zip("xyz",lo,hi)})
        ET.SubElement(shape, "ref", id="material-concrete", name="bsdf")
    ET.indent(scene)
    return ET.tostring(scene, encoding="utf-8", xml_declaration=True)


def prepare(output, radio, sample_count):
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    geometry = dict(empty_los=[], ground_reflection=[([-20000,-20000,-2],[20000,20000,0])],
        wall_shadow=[([499,-100,0],[501,100,300])],
        shadow_boundary=[([499,-100,0],[501,100,300])])
    for name, boxes in geometry.items():
        path = output / (name + ".xml")
        path.write_bytes(scene_xml(boxes))
        points = []
        for index in range(sample_count):
            fraction = (index + .5) / sample_count
            if name in ("empty_los", "ground_reflection"):
                radius = 2 * (7000 ** fraction)
                angle = 2 * math.pi * ((index * 0.6180339887498949) % 1)
                position = [radius * math.cos(angle), radius * math.sin(angle),
                            120 + 40 * math.sin(index * .17)]
            else:
                position = [1000., (-300 + 600*fraction) if name == "wall_shadow" else (100000/499 + (fraction-.5)*.2), 120.]
            points.append(dict(sample=index, tx_position_m=[0.,0.,120.], rx_position_m=position,
                tx_velocity_mps=[0.,0.,0.], rx_velocity_mps=[0.,0.,0.],
                tx_orientation_rad=[0.,0.,0.], rx_orientation_rad=[0.,0.,0.],
                host_pose_time_ns=1000000000, native_time_ns=1000000000))
        cases.append(dict(name=name, scene=path.name, scene_sha256=digest(path),
                          shape_count=len(boxes), points=points))
    manifest = dict(schema_version=1, status="prepared_not_executed", pins=PINS,
        source_radio=dict(path=str(radio.resolve()), sha256=digest(radio)),
        probe_sha256=digest(__file__), sources=SOURCES, flags=FLAGS, budgets=BUDGETS,
        seeds=[42,43,44], carrier_hz=2412000000., baseband_bins_hz=[-30000000.,-20000000.,-10000000.,0.,10000000.,20000000.,30000000.],
        batch_receivers=16, sample_count_per_stratum=sample_count, criteria=CRITERIA, cases=cases,
        candidate_policy="empty_verified_geometry_iso1x1_V_unit_weights_SISO_PSD_only",
        selection_rule="Only zero-shape verified LoS and distance>=10lambda can select stock Friis; every other link remains fullRT. One observed path in nonempty geometry is not qualification.",
        coherent_scope="Friis PSD has no phase; a separate analytical LoS CFR diagnostic is evaluated. A PSD-only pass never implies coherent/MIMO qualification.",
        cache_contract=dict(max_age_ns=100000000,
            key_fields=["scene_content_sha256","radio_config_sha256","source_config_sha256","carrier_hz",
                        "spectrum_bins_hz","array_type_pattern_polarization_weights","endpoint_id_direction",
                        "position_velocity_orientation","source_pose_monotonic_ns","native_time_ns","solver_flags_seed"],
            invalidation=["geometry/radio/source change","pose/velocity/orientation change","age>100ms","time reversal"],
            rt_failure="error_invalid_measurement_never_simple_substitution",
            classification_fallback="fullRT_with_explicit_reason"),
        production_status="not_integrated_probe_only")
    write(output / "manifest.json", manifest)
    return manifest


def summary(errors, np):
    values = np.asarray(errors, dtype=float)
    finite = np.isfinite(values)
    return dict(samples=int(values.size), finite_samples=int(finite.sum()),
        nonfinite_samples=int((~finite).sum()),
        bias_db=float(values[finite].mean()) if finite.any() else None,
        p95_absolute_db=float(np.quantile(np.abs(values[finite]), .95)) if finite.any() else None,
        p99_absolute_db=float(np.quantile(np.abs(values[finite]), .99)) if finite.any() else None,
        maximum_absolute_db=float(np.abs(values[finite]).max()) if finite.any() else None)


def execute(input_path, output, smoke=False, radio_override=None):
    manifest = json.loads(input_path.read_text())
    if manifest["probe_sha256"] != digest(__file__):
        raise ValueError("probe source changed since preparation")
    effective_radio = radio_override or Path(manifest["source_radio"]["path"])
    if manifest["source_radio"]["sha256"] != digest(effective_radio):
        raise ValueError("frozen radio changed since preparation")
    for case in manifest["cases"]:
        if digest(input_path.parent/case["scene"]) != case["scene_sha256"]:
            raise ValueError("fixture changed since preparation")
    output.mkdir(parents=True, exist_ok=False)
    status = dict(schema_version=1, status="started", manifest_sha256=digest(input_path),
                  probe_sha256=digest(__file__), smoke=smoke, production_status="not_integrated")
    write(output/"status.json", status)
    try:
        versions = {name:importlib.metadata.version(name) for name in PINS}
        if versions != PINS:
            raise ValueError(f"dependency versions differ: {versions}")
        import numpy as np
        import mitsuba as mi
        mi.set_variant("cuda_ad_mono_polarized")
        import drjit as dr
        from sionna.rt import load_scene, PlanarArray, Transmitter, Receiver, PathSolver, Paths
        from sionna.rt.utils import spawn_ray_to
        from ns import ns
        friis = ns.FriisSpectrumPropagationLossModel()
        source_files = dict(path_solver=digest(inspect.getfile(PathSolver)), paths=digest(inspect.getfile(Paths)),
                            scene=digest(inspect.getfile(load_scene)))
        if source_files["path_solver"] != "0b32228bdde831079cfa4478d092d4634e2e994ecae1b5cbd843404bb2d2afe2" or source_files["paths"] != "8526de9e478100d87c4f08969ade5f8e32f8499d13c21f5c310fdfd1e4b54765":
            raise ValueError("pinned solver/paths source differs despite distribution versions")
        solver = PathSolver()
        carrier = manifest["carrier_hz"]
        offsets = np.asarray(manifest["baseband_bins_hz"])
        rows = []
        convergence = []
        with (output/"matched-samples.jsonl").open("x") as stream:
            for case in manifest["cases"]:
                scene = load_scene(None if not case["shape_count"] else str(input_path.parent/case["scene"]), merge_shapes=True)
                scene.frequency = carrier
                scene.tx_array = PlanarArray(num_rows=1,num_cols=1,pattern="iso",polarization="V")
                scene.rx_array = PlanarArray(num_rows=1,num_cols=1,pattern="iso",polarization="V")
                scene.add(Transmitter(name="probe_tx",position=[0,0,120],orientation=[0,0,0]))
                points = case["points"][:16] if smoke else case["points"]
                for begin in range(0,len(points),manifest["batch_receivers"]):
                    chunk = points[begin:begin+manifest["batch_receivers"]]
                    for n,point in enumerate(chunk):
                        scene.add(Receiver(name=f"probe_rx_{n}",position=point["rx_position_m"],orientation=[0,0,0]))
                    xyz = np.asarray([p["rx_position_m"] for p in chunk])
                    tx = np.asarray(chunk[0]["tx_position_m"])
                    distance = np.linalg.norm(xyz-tx,axis=1)
                    origins = mi.Point3f(*[np.full(len(chunk),a) for a in tx])
                    targets = mi.Point3f(*xyz.T)
                    tick = time.perf_counter_ns()
                    blocked = np.asarray(scene.mi_scene.ray_test(spawn_ray_to(origins,targets)),dtype=bool).reshape(-1)
                    stock_gain = np.asarray([[1. / friis.CalculateLoss(float(carrier+f),float(d)) for f in offsets] for d in distance])
                    candidate_overhead_ns = time.perf_counter_ns()-tick
                    observations = {}
                    budgets = manifest["budgets"][:1] if smoke else manifest["budgets"]
                    seeds = manifest["seeds"][:1] if smoke else manifest["seeds"]
                    for budget_number,budget in enumerate(budgets):
                        for seed in seeds:
                            tick=time.perf_counter_ns()
                            paths=solver(scene, samples_per_src=budget["samples_per_src"],
                                max_num_paths_per_src=budget["max_num_paths_per_receiver"]*len(chunk),
                                seed=seed,**manifest["flags"])
                            # cir and cfr are pinned official APIs; no copied phase solver.
                            h=np.asarray(paths.cfr(frequencies=mi.Float(offsets),normalize_delays=False,
                                               normalize=False,out_type="numpy"))
                            h=h.reshape(len(chunk),-1,len(offsets))
                            if h.shape[1]!=1:raise ValueError("probe requires exactly one antenna/port/time")
                            h=h[:,0,:]
                            valid=np.asarray(paths.valid,dtype=bool).reshape(len(chunk),-1)
                            path_count=valid.sum(axis=1)
                            elapsed=time.perf_counter_ns()-tick
                            observations[budget_number,seed]=h
                            with np.errstate(divide="ignore",invalid="ignore"):
                                error=10*np.log10(stock_gain/(np.abs(h)**2))
                            # Geometry-derived coherent comparison is diagnostic, not an assumed identity.
                            analytical_h=np.sqrt(stock_gain)*np.exp(-2j*np.pi*(carrier+offsets)[None,:]*distance[:,None]/C)
                            for n,point in enumerate(chunk):
                                eligible=(case["shape_count"]==0 and not blocked[n] and distance[n]>=10*C/carrier and path_count[n]==1)
                                nmse=float(np.sum(np.abs(analytical_h[n]-h[n])**2)/np.sum(np.abs(h[n])**2)) if np.any(h[n]) else None
                                row=dict(stratum=case["name"],sample=point["sample"],budget=budget_number,seed=seed,
                                    distance_m=float(distance[n]),verified_los=not bool(blocked[n]),path_count=int(path_count[n]),
                                    selected_model="friis" if eligible else "sionna",
                                    reason="qualified_empty_los_candidate_pending_numeric_gate" if eligible else "unqualified_geometry_or_link",
                                    error_db=[float(x) if math.isfinite(float(x)) else None for x in error[n]],
                                    coherent_nmse=nmse, reference_cfr_real=h[n].real.tolist(), reference_cfr_imag=h[n].imag.tolist(),
                                    stock_friis_gain=stock_gain[n].tolist(),full_rt_solve_ns=elapsed,candidate_batch_overhead_ns=candidate_overhead_ns,
                                    geometry_sha256=case["scene_sha256"],radio_sha256=manifest["source_radio"]["sha256"],
                                    endpoint=point,rt_flags=manifest["flags"])
                                stream.write(json.dumps(row,allow_nan=False)+"\n")
                                rows.append(row)
                    if len(budgets)>1:
                        # Highest two budgets compared at every seed, never selecting the best result.
                        for seed in seeds:
                            a,b=observations[len(budgets)-2,seed],observations[len(budgets)-1,seed]
                            with np.errstate(divide="ignore",invalid="ignore"):
                                err=10*np.log10((np.abs(a)**2)/(np.abs(b)**2))
                            err=np.where((np.abs(a)==0)&(np.abs(b)==0),0.,err)
                            convergence.append(dict(stratum=case["name"],kind="budget",seed=seed,
                                sample_start=begin,errors_db=err.reshape(-1).tolist()))
                        for ia, seed_a in enumerate(seeds):
                            for seed_b in seeds[ia+1:]:
                                a,b=observations[len(budgets)-1,seed_a],observations[len(budgets)-1,seed_b]
                                with np.errstate(divide="ignore",invalid="ignore"):
                                    err=10*np.log10((np.abs(a)**2)/(np.abs(b)**2))
                                err=np.where((np.abs(a)==0)&(np.abs(b)==0),0.,err)
                                convergence.append(dict(stratum=case["name"],kind="seed",seed_pair=[seed_a,seed_b],
                                    sample_start=begin,errors_db=err.reshape(-1).tolist()))
                    for n in range(len(chunk)):scene.remove(f"probe_rx_{n}")
        result=[]
        for case in manifest["cases"]:
            selected=[row for row in rows if row["stratum"]==case["name"] and row["budget"]==(0 if smoke else len(manifest["budgets"])-1)]
            numerical=summary([x if x is not None else float("nan") for row in selected for x in row["error_db"]],np)
            eligible=[row for row in selected if row["selected_model"]=="friis"]
            subset=summary([x if x is not None else float("nan") for row in eligible for x in row["error_db"]],np)
            unique=len({row["sample"] for row in eligible})
            ref=summary([value for item in convergence if item["stratum"]==case["name"] for value in item["errors_db"]],np)
            ref_limits=CRITERIA["rt_reference_convergence"]
            ref_ok=bool(ref["samples"]>0 and ref["nonfinite_samples"]==0 and abs(ref["bias_db"])<=ref_limits["absolute_bias_db_max"] and
                ref["p95_absolute_db"]<=ref_limits["absolute_error_p95_db_max"] and ref["p99_absolute_db"]<=ref_limits["absolute_error_p99_db_max"])
            calls={}
            overhead={}
            for row in rows:
                if row["stratum"]==case["name"]:
                    key=(row["budget"],row["seed"],row["sample"]//manifest["batch_receivers"])
                    calls[key]=row["full_rt_solve_ns"]/1e6
                    overhead[row["sample"]//manifest["batch_receivers"]]=row["candidate_batch_overhead_ns"]/1e6
            errors_ok=bool(unique>=CRITERIA["minimum_matched_samples_per_stratum"] and subset["nonfinite_samples"]==0 and subset["bias_db"] is not None and
                abs(subset["bias_db"])<=1 and subset["p95_absolute_db"]<=3 and subset["p99_absolute_db"]<=6)
            result.append(dict(stratum=case["name"],all_links_friis_diagnostic=numerical,
                candidate_domain=subset,matched_unique_candidate_links=unique,project_error_budget_pass=errors_ok,
                reference_convergence=ref,reference_convergence_pass=ref_ok,
                production_numeric_gate=bool(errors_ok and ref_ok and not smoke),
                timing=dict(rt_calls=len(calls),first_rt_call_ms=next(iter(calls.values()),None),
                    rt_call_median_ms=float(np.median(list(calls.values()))) if calls else None,
                    rt_call_p95_ms=float(np.quantile(list(calls.values()),.95)) if calls else None,
                    candidate_geometry_and_friis_batch_median_ms=float(np.median(list(overhead.values()))) if overhead else None),
                eligibility_scope="empty_scene_SISO_PSD_only",coherent_qualification="not_claimed_separate_diagnostic",
                overhead_scope="Python/native binding and geometry query included; not native production performance"))
        status.update(status="completed",versions=versions,source_files=source_files,strata=result,
            qualification="insufficient_smoke" if smoke else "requires_reference_convergence_review_and_native_boundary_test")
    except BaseException as error:
        status.update(status="failed",error=f"{type(error).__name__}: {error}",traceback=traceback.format_exc())
        write(output/"status.json",status)
        raise
    write(output/"status.json",status)
    return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-only",action="store_true")
    mode.add_argument("--execute",type=Path,metavar="MANIFEST")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--radio",type=Path)
    parser.add_argument("--samples",type=int,default=1000)
    parser.add_argument("--smoke",action="store_true")
    args=parser.parse_args()
    if args.prepare_only:
        if args.radio is None or args.samples<1:parser.error("--radio and positive --samples required")
        result=prepare(args.output,args.radio,args.samples)
    else:result=execute(args.execute,args.output,args.smoke,args.radio)
    print(json.dumps(dict(status=result["status"],output=str(args.output))))
if __name__=="__main__":main()
