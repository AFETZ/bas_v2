#!/usr/bin/env python3
"""Source contracts for native Wi-Fi/Sionna runtime observability."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT / "network/ns3/scratch/upstream-sionna-tap-spike.cc"
).read_text(encoding="utf-8")


def test_packet_outcome_influence_is_derived_from_the_installed_chain() -> None:
    assert "g_packetOutcomeAffected" not in SOURCE
    assert "g_phasedArraySpectrumPropagationModels.push_back(" in SOURCE
    assert '"ns3::SionnaRtSpectrumPropagationLossModel"' in SOURCE
    assert "g_phasedArraySpectrumPropagationModels.size() == 1" in SOURCE
    assert "sionna_drives_rx_psd" in SOURCE
    assert "packet_outcome_affected" in SOURCE
    assert '(sionnaDrivesRxPsd ? "true" : "false")' in SOURCE
    assert "Deprecated compatibility input" in SOURCE
    for runtime_setting in (
        "tx_power_w",
        "carrier_hz",
        "wifi_channel_number",
        "wifi_channel_width_mhz",
        "wifi_actual_channel_number",
        "wifi_actual_center_frequency_hz",
        "wifi_data_mode",
        "wifi_control_mode",
        "wifi_ssid",
    ):
        assert f'\\"{runtime_setting}\\"' in SOURCE


def test_wifi_and_sionna_use_the_same_explicit_center_frequency() -> None:
    assert 'command.AddValue("wifiChannelNumber"' in SOURCE
    assert 'channelSettings << "{" << wifiChannelNumber' in SOURCE
    assert "GetPhy()->GetChannelNumber()" in SOURCE
    assert "MHzToHz(referenceDevice->GetPhy()->GetFrequency())" in SOURCE
    assert "g_wifiActualCenterFrequencyHz - carrierHz" in SOURCE


def test_wifi_rx_power_comes_from_post_propagation_band_powers() -> None:
    callback = SOURCE.split("WifiPhyRxStart", 1)[1].split("WifiPhyRxEnd", 1)[0]
    assert "RxPowerWattPerChannelBand rxPowersW" in callback
    assert "for (const auto& [band, powerW] : rxPowersW)" in callback
    assert "bandwidthHz <= 20e6" in callback
    assert "totalRxPowerW += powerW" in callback
    assert "WToDbm(totalRxPowerW)" in callback
    assert 'LogEvent("wifi_rx_power_dbm"' in callback
    assert "source=PhyRxBegin.RxPowerWattPerChannelBand" in callback


def test_wifi_decode_verdicts_do_not_come_from_phy_rx_end() -> None:
    assert re.search(
        r'TraceConnectWithoutContext\("PhyRxEnd",\s*'
        r'MakeBoundCallback\(&WifiPhyRxEnd, index\)\)',
        SOURCE,
    )
    assert not re.search(
        r'TraceConnectWithoutContext\("PhyRxEnd",\s*'
        r'MakeBoundCallback\(&PhyRxOk, index\)\)',
        SOURCE,
    )
    assert re.search(
        r'GetState\(\)->TraceConnectWithoutContext\(\s*"RxOk",\s*'
        r'MakeBoundCallback\(&WifiPhyStateRxOk, index\)\)',
        SOURCE,
    )
    assert re.search(
        r'GetState\(\)->TraceConnectWithoutContext\(\s*"RxError",\s*'
        r'MakeBoundCallback\(&WifiPhyStateRxError, index\)\)',
        SOURCE,
    )
    assert ";verdict=not_yet_available" in SOURCE
    assert ";source=WifiPhyStateHelper.RxOk" in SOURCE
    assert ";source=WifiPhyStateHelper.RxError" in SOURCE


def test_cp_uav_path_counts_are_sampled_from_channel_params_periodically() -> None:
    assert "void PollSionnaPaths()" in SOURCE
    assert "m_channelModel->GetParams(m_mobility.front(), m_mobility.at(uavIndex))" in SOURCE
    assert '"sample=periodic;scope=cp_uav_reciprocal"' in SOURCE
    assert "static_cast<double>(params->m_delay.size())" in SOURCE
    assert "params->m_generatedTime.GetSeconds()" in SOURCE
    assert re.search(
        r"Simulator::Schedule\(Seconds\(1\),\s*"
        r"&NativeRuntimeSampler::PollSionnaPaths,\s*&metrics\)",
        SOURCE,
    )
