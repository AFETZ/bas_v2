from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts/product/validate_native_wifi_sionna.py"
SPEC = importlib.util.spec_from_file_location("validate_native_wifi_sionna", VALIDATOR_PATH)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def passing_summary() -> dict:
    return {
        "profile": "native_wifi_80211n_spectrum_reference_v1",
        "uav_count": 5,
        "radio": "ns3::SpectrumWifiPhy",
        "propagation": "ns3::SionnaRtSpectrumPropagationLossModel",
        "sionna_enabled": True,
        "scalar_fallback": False,
        "application_bypass": False,
        "control_pdr": 1.0,
        "control_rtt_p95_ms": 15.0,
        "scheduler_lag_profile_p95_ms": 1.0,
        "mean_rtf": 1.0,
        "jain_fairness": 1.0,
        "wifi_mac_tx_drop": 0,
        "no_starvation": True,
        "pass": True,
        "exit_code": 0,
        "per_uav": [
            {"uav": uav, "offered": 10, "acked": 10} for uav in range(1, 6)
        ],
    }


def test_accepts_complete_five_uav_native_reference() -> None:
    assert VALIDATOR.validate(passing_summary()) == []


def test_checked_in_reference_evidence_satisfies_contract() -> None:
    evidence = json.loads(
        (
            ROOT
            / "network/ns3/evidence/native_wifi_80211n_spectrum_reference_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["scope"] == "standalone_ns3_control_reference_not_gazebo_sitl_flight"
    assert VALIDATOR.validate(evidence) == []


def test_rejects_bypass_and_starvation() -> None:
    summary = passing_summary()
    summary["application_bypass"] = True
    summary["per_uav"][3]["acked"] = 0
    errors = VALIDATOR.validate(summary)
    assert any("application_bypass" in error for error in errors)
    assert any("starvation" in error for error in errors)


def test_patch_only_adapts_spectrum_channel_antenna_lookup() -> None:
    patch = (
        ROOT / "network/ns3/patches/mr2608-spectrumwifi-phased-array-adapter.patch"
    ).read_text(encoding="utf-8")
    changed_paths = {
        line.removeprefix("+++ b/")
        for line in patch.splitlines()
        if line.startswith("+++ b/")
    }
    assert changed_paths == {"src/spectrum/model/multi-model-spectrum-channel.cc"}
    assert "GetObject<PhasedArrayModel>()" in patch
    assert "wifi/model" not in patch


def test_scenarios_have_no_scalar_propagation_fallback() -> None:
    for name in ("upstream-sionna-wifi-smoke.cc", "upstream-sionna-wifi-five-uav.cc"):
        source = (ROOT / "network/ns3/scratch" / name).read_text(encoding="utf-8")
        assert "AddPhasedArraySpectrumPropagationLossModel(sionna)" in source
        assert "AddPropagationLossModel" not in source
