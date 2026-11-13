"""Parsed scene evidence and native counters for qualified SISO PSD hybrid.

No propagation formula is implemented here. The native wrapper delegates to
stock ns-3 Friis and the existing full Sionna RT PSD path.
"""
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import math
from xml.etree import ElementTree

POLICY_ID = "qualified_siso_psd_2412_v2"
LEGACY_POLICY_ID = "empty_los_static_siso_psd_2412_v1"
FLAT_GROUND_SHA256 = "905807072f010dbca786a3842919de50d1d3988656bb08400cb428c1166acdde"


def validate_radio_profile(profile):
    rt, coupling = profile["sionna"], profile["coupling"]
    if (rt["execution_mode"] != "worker_pool"
            or any(type(v) not in (int, float) or not 0 < v <= .1
                   for v in (rt["channel_state_max_age_s"], coupling["state_max_age_s"]))):
        raise ValueError("qualified hybrid requires worker_pool and pose/cache age limits <=100 ms")


def flat_ground_evidence(scene):
    """Actual pinned geometry AND material; matching a bbox alone cannot qualify."""
    from sionna.rt import ITURadioMaterial
    from sionna.rt.radio_materials.itu import itu_material
    shapes = scene.mi_scene.shapes()
    if len(shapes) != 1 or len(scene.objects) != 1:
        return False
    scene.frequency = 2412000000.0
    box = shapes[0].bbox()
    bounds = [[float(v[i]) for i in range(3)] for v in (box.min, box.max)]
    material = next(iter(scene.objects.values())).radio_material
    if not isinstance(material, ITURadioMaterial):
        return False
    eta, sigma = itu_material("medium_dry_ground", scene.frequency)
    scalar = lambda value: float(value[0])
    return (bounds == [[-1000., -1000., -1.], [1000., 1000., 0.]]
            and material.itu_type == "medium_dry_ground"
            and abs(scalar(material.relative_permittivity)-scalar(eta)) < 1e-5
            and abs(scalar(material.conductivity)-scalar(sigma)) < 1e-6
            and abs(scalar(material.thickness)-.2) < 1e-7
            and scalar(material.scattering_coefficient) == 0
            and scalar(material.xpd_coefficient) == 0)


def scene_evidence(path):
    path = Path(path).resolve()
    before = path.read_bytes()
    xml = ElementTree.fromstring(before)
    # Empty eligibility preserves the original prohibition on external or
    # shape/ref declarations. Ground has an exact frozen XML identity instead.
    external = any(node.tag.rsplit("}", 1)[-1] in ("include", "shape", "ref")
                   or node.get("name") == "filename" for node in xml.iter())
    if tuple(version(name) for name in ("sionna-rt", "mitsuba", "drjit")) != (
            "1.2.0", "3.7.1", "1.2.0"):
        raise ValueError("hybrid qualification requires the pinned RT versions")
    from sionna.rt import load_scene
    scene = load_scene(str(path), merge_shapes=True)
    shapes, objects = len(scene.mi_scene.shapes()), len(scene.objects)
    scene_hash = sha256(before).hexdigest()
    qualified_flat = (scene_hash == FLAT_GROUND_SHA256 and shapes == objects == 1
                      and flat_ground_evidence(scene))
    if before != path.read_bytes():
        raise ValueError("hybrid scene changed during preflight")
    return dict(policy_id=POLICY_ID, scene_sha256=scene_hash,
                shape_count=shapes, object_count=objects,
                qualified_empty_scene=(xml.tag == "scene" and shapes == objects == 0 and not external),
                qualified_flat_ground_scene=qualified_flat,
                scope="Parsed immutable input; actual Sionna/Mitsuba geometry and exact flat material/slab identity.")


def native_dispatch_valid(stats):
    """Check outputs separately from extra full-RT reference comparisons."""
    hybrid = stats.get("hybrid_selection")
    if not isinstance(hybrid, dict):
        return False
    def count(name):
        value = hybrid.get(name)
        return value if type(value) is int and value >= 0 else None
    rt, friis, guarded = (count(name) for name in (
        "rt_psd_evaluations", "friis_psd_evaluations", "guarded_friis_evaluations"))
    if any(value is None for value in (rt, friis, guarded)):
        return False
    for name in ("state_max_age_s", "channel_state_max_age_s"):
        value = stats.get(name)
        if type(value) not in (int, float) or not 0 < value <= .1:
            return False
    if stats.get("sionna_rt_execution_mode") != "worker_pool":
        return False
    scene_hash = hybrid.get("scene_sha256")
    if (not isinstance(scene_hash, str) or len(scene_hash) != 64
            or any(c not in "0123456789abcdef" for c in scene_hash)):
        return False
    policy_id = hybrid.get("policy_id")
    if policy_id not in (POLICY_ID, LEGACY_POLICY_ID):
        return False
    empty = (hybrid.get("qualified_empty_scene") is True
             and type(hybrid.get("actual_shape_count")) is int and hybrid["actual_shape_count"] == 0
             and type(hybrid.get("actual_object_count")) is int and hybrid["actual_object_count"] == 0)
    ground = (policy_id == POLICY_ID and hybrid.get("qualified_flat_ground_scene") is True
              and scene_hash == FLAT_GROUND_SHA256
              and type(hybrid.get("actual_shape_count")) is int and hybrid["actual_shape_count"] == 1
              and type(hybrid.get("actual_object_count")) is int and hybrid["actual_object_count"] == 1
              and type(hybrid.get("online_guard_limit_db")) in (int, float)
              and hybrid["online_guard_limit_db"] == 1)
    if ((hybrid.get("qualified_empty_scene") is True and not empty)
            or (hybrid.get("qualified_flat_ground_scene") is True and not ground)):
        return False
    if friis is not None and friis > 0 and not (empty or ground):
        return False
    if policy_id == POLICY_ID:
        reference, passes, fallbacks = (hybrid.get(name, 0) for name in (
            "ground_reference_psd_evaluations", "ground_guard_passes", "ground_guard_fallbacks"))
        if (any(type(v) is not int or v < 0 for v in (reference, passes, fallbacks))
                or reference != passes+fallbacks or passes > (friis if friis is not None else -1)
                or (ground and passes != friis)
                or (ground and fallbacks > rt)
                or (not ground and any((reference, passes, fallbacks)))):
            return False
        if passes:
            admitted = hybrid.get("ground_max_admitted_abs_error_db")
            if type(admitted) not in (int, float) or not math.isfinite(admitted) or not 0 <= admitted <= 1:
                return False
    links = hybrid.get("links")
    if not isinstance(links, list) or not links:
        return False
    seen, rt_sum, friis_sum = set(), 0, 0
    reasons = {"unqualified_geometry", "unqualified_antenna", "moving_endpoint",
               "unqualified_distance", "unqualified_carrier", "unqualified_spectrum_bins",
               "reference_not_single_nonzero_path", "unqualified_orientation",
               "unqualified_ground_endpoint_role", "unqualified_ground_pose", "unqualified_ground_velocity",
               "ground_reference_zero", "ground_candidate_invalid", "ground_rss_budget_exceeded",
               "ground_no_positive_input"}
    for link in links:
        if not isinstance(link, dict):
            return False
        tx, rx = link.get("tx_node_id"), link.get("rx_node_id")
        if any(type(x) is not int or x < 0 for x in (tx, rx)) or tx == rx or (tx, rx) in seen:
            return False
        seen.add((tx, rx))
        values = [link.get(key) for key in ("rt_psd_evaluations", "friis_psd_evaluations")]
        if any(type(x) is not int or x < 0 for x in values) or sum(values) == 0:
            return False
        rt_sum += values[0]
        friis_sum += values[1]
        model, reason = link.get("last_model"), link.get("last_reason")
        if not ((model == "friis" and values[1] > 0 and (
                    (reason == "qualified_empty_static_siso_psd" and empty)
                    or (reason == "qualified_flat_ground_online_guard" and ground)))
                or (model == "sionna" and values[0] > 0 and reason in reasons)):
            return False
    return (hybrid.get("rt_producer_retained") is True
            and all(value is not None for value in (rt, friis, guarded))
            and rt + friis > 0 and guarded == friis and (rt, friis) == (rt_sum, friis_sum)
            and count("reference_guard_failures") == 0
            and stats.get("rx_psd_propagation_model") == "bas::QualifiedSionnaFriisSelection"
            and stats.get("selected_propagation_profile") == "hybrid"
            and stats.get("sionna_drives_rx_psd") is (rt > 0))
