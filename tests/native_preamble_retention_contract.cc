// Actual SpectrumWifiPhy regression for expired preamble callback retention.
// Run unchanged against the old and patched ns-3 Wi-Fi library; compare trace bytes.
#include "ns3/core-module.h"
#include "ns3/ht-phy.h"
#include "ns3/ht-ppdu.h"
#include "ns3/interference-helper.h"
#include "ns3/multi-model-spectrum-channel.h"
#include "ns3/network-module.h"
#include "ns3/nist-error-rate-model.h"
#include "ns3/phy-entity.h"
#include "ns3/spectrum-signal-parameters.h"
#include "ns3/spectrum-wifi-phy.h"
#include "ns3/threshold-preamble-detection-model.h"
#include "ns3/wifi-net-device.h"
#include "ns3/wifi-phy-state-helper.h"
#include "ns3/wifi-psdu.h"
#include "ns3/wifi-spectrum-signal-parameters.h"
#include "ns3/wifi-spectrum-phy-interface.h"
#include "ns3/wifi-spectrum-value-helper.h"
#include "ns3/wifi-utils.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <vector>

using namespace ns3;

// Access the protected diagnostic vector without changing or substituting the PHY.
// The pointer-to-member is obtained in a derived class and applied to the real object.
struct PreambleInspection : PhyEntity
{
    static std::size_t Retained(const PhyEntity& entity)
    {
        return (entity.*&PreambleInspection::m_endPreambleDetectionEvents).size();
    }
};

namespace
{
constexpr unsigned kReceivers = 6;
constexpr unsigned kPackets = 20000;
std::ofstream g_trace;
std::vector<Ptr<SpectrumWifiPhy>> g_phys;
std::size_t g_maxRetained = 0;
uint64_t g_failed = 0;
uint64_t g_begun = 0;
uint64_t g_ended = 0;
std::chrono::steady_clock::time_point g_recoveryStart;
int64_t g_recoveryWallNs = 0;

void RxDrop(unsigned receiver, Ptr<const Packet> packet, WifiPhyRxfailureReason reason)
{
    g_trace << "drop," << Simulator::Now().GetNanoSeconds() << ',' << receiver << ','
            << packet->GetUid() << ',' << reason << '\n';
    if (reason == PREAMBLE_DETECT_FAILURE)
    {
        ++g_failed;
    }
}

void RxBegin(unsigned receiver, Ptr<const Packet> packet, RxPowerWattPerChannelBand powers)
{
    ++g_begun;
    g_trace << "begin," << Simulator::Now().GetNanoSeconds() << ',' << receiver << ','
            << packet->GetUid();
    for (const auto& [band, power] : powers)
    {
        g_trace << ',' << power;
    }
    g_trace << '\n';
}

void RxEnd(unsigned receiver, Ptr<const Packet> packet)
{
    ++g_ended;
    g_trace << "end," << Simulator::Now().GetNanoSeconds() << ',' << receiver << ','
            << packet->GetUid() << '\n';
}

void State(unsigned receiver, Time start, Time duration, WifiPhyState state)
{
    g_trace << "state," << Simulator::Now().GetNanoSeconds() << ',' << receiver << ','
            << start.GetNanoSeconds() << ',' << duration.GetNanoSeconds() << ',' << state << '\n';
}

Ptr<SpectrumValue> Psd(dBm_u power)
{
    return WifiSpectrumValueHelper::CreateHtOfdmTxPowerSpectralDensity(
        {MHz_u{5180}}, MHz_u{20}, DbmToW(power), MHz_u{20});
}

void ForeignSlice()
{
    for (const auto& phy : g_phys)
    {
        auto params = Create<SpectrumSignalParameters>();
        params->duration = MilliSeconds(10);
        params->psd = Psd(dBm_u{-10});
        phy->StartRx(params, nullptr);
    }
}

void PacketArrival(uint64_t index)
{
    for (unsigned receiver = 0; receiver < g_phys.size(); ++receiver)
    {
        const auto& phy = g_phys[receiver];
        WifiTxVector vector(HtPhy::GetHtMcs(0), WIFI_MIN_TX_PWR_LEVEL,
                            WIFI_PREAMBLE_HT_MF, NanoSeconds(800), 1, 1, 0, MHz_u{20}, false);
        WifiMacHeader header;
        header.SetType(WIFI_MAC_QOSDATA);
        header.SetQosTid(0);
        auto psdu = Create<WifiPsdu>(Create<Packet>(200), header);
        auto duration = SpectrumWifiPhy::CalculateTxDuration(psdu->GetSize(), vector,
                                                            phy->GetPhyBand());
        auto params = Create<WifiSpectrumSignalParameters>();
        params->duration = duration;
        params->psd = Psd(dBm_u{-50});
        params->ppdu = Create<HtPpdu>(psdu, vector, phy->GetOperatingChannel(), duration,
                                     index * kReceivers + receiver);
        phy->StartRx(params, nullptr);
        g_maxRetained = std::max(g_maxRetained,
            PreambleInspection::Retained(*phy->GetPhyEntity(WIFI_MOD_CLASS_HT)));
    }
}

void Recovery()
{
    g_recoveryStart = std::chrono::steady_clock::now();
    PacketArrival(kPackets);
    Simulator::Schedule(MicroSeconds(5), [] {
        g_recoveryWallNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - g_recoveryStart).count();
    });
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 3)
    {
        std::cerr << "usage: native_preamble_retention_contract RESULT_JSON TRACE_CSV\n";
        return 64;
    }
    RngSeedManager::SetSeed(1);
    RngSeedManager::SetRun(1);
    g_trace.open(argv[2]);
    g_trace << std::setprecision(17);
    if (!g_trace)
    {
        throw std::runtime_error("cannot create PHY trace");
    }
    for (unsigned i = 0; i < kReceivers; ++i)
    {
        auto channel = CreateObject<MultiModelSpectrumChannel>();
        auto node = CreateObject<Node>();
        auto device = CreateObject<WifiNetDevice>();
        auto phy = CreateObject<SpectrumWifiPhy>();
        phy->SetInterferenceHelper(CreateObject<InterferenceHelper>());
        phy->SetErrorRateModel(CreateObject<NistErrorRateModel>());
        phy->SetPreambleDetectionModel(CreateObject<ThresholdPreambleDetectionModel>());
        phy->SetDevice(device);
        phy->AddChannel(channel);
        phy->SetOperatingChannel(WifiPhy::ChannelTuple{36, 0, WIFI_PHY_BAND_5GHZ, 0});
        phy->ConfigureStandard(WIFI_STANDARD_80211n);
        device->SetPhy(phy);
        node->AddDevice(device);
        phy->AssignStreams(100 + i);
        phy->TraceConnectWithoutContext("PhyRxDrop", MakeBoundCallback(&RxDrop, i));
        phy->TraceConnectWithoutContext("PhyRxBegin", MakeBoundCallback(&RxBegin, i));
        phy->TraceConnectWithoutContext("PhyRxEnd", MakeBoundCallback(&RxEnd, i));
        PointerValue state;
        phy->GetAttribute("State", state);
        state.Get<WifiPhyStateHelper>()->TraceConnectWithoutContext(
            "State", MakeBoundCallback(&State, i));
        g_phys.push_back(phy);
    }
    // Preserve independent 10 ms foreign signals, matching the runtime's moving-source cadence.
    for (unsigned i = 0; i < 2000; ++i)
    {
        Simulator::Schedule(MilliSeconds(100 + 10 * i), &ForeignSlice);
    }
    for (unsigned i = 0; i < kPackets; ++i)
    {
        Simulator::Schedule(MilliSeconds(100 + i) + MicroSeconds(100), &PacketArrival, i);
    }
    Simulator::Schedule(MilliSeconds(20101), &Recovery);
    Simulator::Stop(MilliSeconds(20110));
    Simulator::Run();
    const bool phyOk = g_failed == uint64_t{kPackets} * kReceivers &&
                       g_begun == kReceivers && g_ended == kReceivers;
    const bool bounded = g_maxRetained <= 1;
    std::ofstream result(argv[1]);
    result << "{\n  \"phy_contract_passed\":" << (phyOk ? "true" : "false")
           << ",\n  \"retention_bounded\":" << (bounded ? "true" : "false")
           << ",\n  \"receivers\":" << kReceivers
           << ",\n  \"failed_preambles\":" << g_failed
           << ",\n  \"rx_begin\":" << g_begun << ",\n  \"rx_end\":" << g_ended
           << ",\n  \"max_retained_per_phy\":" << g_maxRetained
           << ",\n  \"recovery_wall_ns\":" << g_recoveryWallNs << "\n}\n";
    g_trace.close();
    Simulator::Destroy();
    for (auto& phy : g_phys)
    {
        phy->Dispose();
    }
    g_phys.clear();
    return phyOk ? (bounded ? 0 : 2) : 1;
}
