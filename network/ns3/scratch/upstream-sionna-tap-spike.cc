// fmt 8.1.1 (MIT) is installed in the pinned runtime; header-only needs no new link target.
#define FMT_HEADER_ONLY 1
#include <fmt/format.h>
#include "pybind11/embed.h"

#include "ns3/adhoc-aloha-noack-ideal-phy-helper.h"
#include "ns3/aloha-noack-mac-header.h"
#include "ns3/aloha-noack-net-device.h"
#include "ns3/core-module.h"
#include "ns3/config-store.h"
#include "ns3/csma-module.h"
#include "ns3/ethernet-header.h"
#include "ns3/half-duplex-ideal-phy.h"
#include "ns3/internet-module.h"
#include "ns3/isotropic-antenna-model.h"
#include "ns3/ism-spectrum-value-helper.h"
#include "ns3/llc-snap-header.h"
#include "ns3/mobility-module.h"
#include "ns3/multi-model-spectrum-channel.h"
#include "ns3/pcap-file-wrapper.h"
#include "ns3/propagation-delay-model.h"
#include "ns3/propagation-loss-model.h"
#include "ns3/queue.h"
#include "ns3/realtime-simulator-impl.h"
#include "ns3/rng-seed-manager.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-helper.h"
#include "ns3/spectrum-wifi-helper.h"
#include "ns3/spectrum-wifi-phy.h"
#include "ns3/sta-wifi-mac.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/uniform-planar-array.h"
#include "ns3/wifi-module.h"
#include "ns3/wifi-net-device.h"
#include "native-spectrum-sources.h"
#include "native-live-state.h"
#include "native-lockstep.h"
#include "native-geometry-loss.h"
#include "native-rt-pool.h"
#include "native-log-phase.h"
#include <memory>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <ctime>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <limits>
#include <optional>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

using namespace ns3;
namespace py = pybind11;

NS_LOG_COMPONENT_DEFINE("UpstreamSionnaTapSpike");

namespace
{

struct NodeCounters
{
    uint64_t macTx{0};
    uint64_t phyRxOk{0};
    uint64_t phyRxStart{0};
    uint64_t phyRxEnd{0};
    uint64_t phyRxError{0};
    uint64_t phyRxDrop{0};
    uint64_t phyTxStart{0};
    uint64_t phyTxEnd{0};
    uint64_t phyRxAbort{0};
    uint64_t macTxDrop{0};
    uint64_t queueEnqueue{0};
    uint64_t queueDequeue{0};
    uint64_t queueDrop{0};
    uint64_t wifiDataRetries{0};
    uint64_t wifiAssociations{0};
    uint64_t wifiDeassociations{0};
    uint32_t queueMaxDepth{0};
};

std::vector<NodeCounters> g_nodeCounters;
std::vector<std::string> g_nodeNames;
std::vector<Vector> g_positions;
double g_channelStateMaxAgeS{0.5};
double g_updateDistanceThresholdM{0.5};
uint64_t g_poseSnapshots{0};
uint32_t g_posePollPeriodMs{20};
uint64_t g_poseReaderCalls{0}, g_poseReaderWallNs{0}, g_poseReaderMaxWallNs{0};
uint64_t g_spectrumCalls{0}, g_spectrumWallNs{0}, g_spectrumMaxWallNs{0};
int64_t g_spectrumMaxWallNativeNs{0};
size_t g_spectrumMaximumBins{0};
uint64_t g_stalePoseSamples{0};
uint64_t g_pathObservations{0};
uint64_t g_realtimeLagSamples{0};
double g_latestRealtimeLagMs{std::numeric_limits<double>::infinity()};
double g_readinessLagMaxMs{50.0};
uint32_t g_readinessConsecutiveSamples{3};
uint32_t g_healthyLagSamples{0};
Ptr<PcapFileWrapper> g_radioPcap;
std::ofstream g_events;
volatile std::sig_atomic_t g_stopRequested = 0;
std::string g_stopReason = "duration";
std::string g_executionMode = "realtime";
std::unique_ptr<bas::Lockstep> g_lockstep;
uint32_t g_couplingStepMs = 20;
uint64_t g_completedSteps = 0;
double g_stepTimeoutS = 60;
std::string g_phaseFile;
std::unique_ptr<bas::NativeLogPhase> g_logPhase;
std::string g_eventLogging{"batched_trace"};
uint32_t g_flushEveryEvents{256};
uint32_t g_flushMaxDelayMs{25};
uint64_t g_pendingEventWrites{0};
std::chrono::steady_clock::time_point g_lastEventFlush{std::chrono::steady_clock::now()};

// Bounded diagnostics for the native event trace. Nested phase/flush timings
// are INCLUDED in LogEvent, not additional costs to sum with it.
struct TraceTiming
{
    uint64_t calls{0}, wallNs{0}, cpuNs{0}, maxWallNs{0}, maxWallCpuNs{0};
    int64_t maxBeginHostNs{0}, maxEndHostNs{0}, maxNativeNs{0};
};
TraceTiming g_eventTraceTiming, g_phaseReadTiming, g_eventFlushTiming;

class TraceTimingScope
{
  public:
    explicit TraceTimingScope(TraceTiming& target)
        : m_target(target), m_begin(HostNs()), m_cpu(CpuNs()) {}
    ~TraceTimingScope()
    {
        const auto cpu=CpuNs()-m_cpu;
        const auto end=HostNs();
        const auto elapsed=static_cast<uint64_t>(end-m_begin);
        ++m_target.calls; m_target.wallNs+=elapsed; m_target.cpuNs+=cpu;
        if (elapsed>m_target.maxWallNs)
        {
            m_target.maxWallNs=elapsed; m_target.maxWallCpuNs=cpu; m_target.maxBeginHostNs=m_begin;
            m_target.maxEndHostNs=end; m_target.maxNativeNs=Simulator::Now().GetNanoSeconds();
        }
    }
  private:
    static int64_t HostNs()
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    static uint64_t CpuNs()
    {
        timespec value{};
        if (clock_gettime(CLOCK_THREAD_CPUTIME_ID,&value)!=0) std::terminate();
        return static_cast<uint64_t>(value.tv_sec)*1000000000ULL+value.tv_nsec;
    }
    TraceTiming& m_target;
    int64_t m_begin;
    uint64_t m_cpu;
};

void WriteTraceTiming(std::ostream& output, const TraceTiming& timing)
{
    output << "{\"calls\":" << timing.calls << ",\"wall_ns\":" << timing.wallNs
           << ",\"thread_cpu_ns\":" << timing.cpuNs << ",\"max_wall_ns\":" << timing.maxWallNs
           << ",\"max_wall_thread_cpu_ns\":" << timing.maxWallCpuNs
           << ",\"max_begin_host_ns\":" << timing.maxBeginHostNs
           << ",\"max_end_host_ns\":" << timing.maxEndHostNs
           << ",\"max_native_ns\":" << timing.maxNativeNs << '}';
}
std::unordered_map<uint64_t, Time> g_queueEnqueueAt;
std::vector<Ptr<Queue<Packet>>> g_queues;
std::vector<Ptr<StaWifiMac>> g_wifiStations;
std::vector<std::vector<Ptr<WifiMacQueue>>> g_wifiQueues;
std::string g_radioBackend{"aloha"};
std::string g_radioProfile{"generic_native_spectrum_aloha_reference"};
std::string g_neighborDiscoveryMode{"preconfigured_static_neighbors"};
std::string g_radioReason{"upstream_ideal_phy_arp_reentrancy_limit"};
std::string g_solverProfile{"realtime_minimal_solver_profile"};
std::string g_sionnaReceiverBatchMode{"pairwise"};
std::string g_sionnaReceiverBatchStatsJson{"null"};
std::string g_sionnaRtExecutionMode{"in_process"};
std::string g_sionnaRtWorkersJson{"null"};
bool g_technologySpecificModem{false};
std::vector<std::string> g_phasedArraySpectrumPropagationModels;
uint32_t g_sionnaMaxDepth{1};
bool g_sionnaLos{true};
bool g_sionnaSpecularReflection{true};
bool g_sionnaDiffuseReflection{false};
bool g_sionnaDiffraction{false};
bool g_sionnaEdgeDiffraction{false};
bool g_sionnaRefraction{false};
bool g_sionnaSyntheticArray{true};
uint32_t g_sionnaSeed{42};
uint32_t g_sionnaMaxNumberOfPaths{90};
uint32_t g_sionnaSamplesPerSource{1000000};
uint32_t g_sionnaMaxNumPathsPerSource{1000000};
double g_sionnaCacheJitterFraction{0.0};
double g_txPowerW{0.0};
double g_carrierHz{0.0};
uint16_t g_wifiChannelNumber{0};
uint16_t g_wifiChannelWidthMhz{0};
uint16_t g_wifiActualChannelNumber{0};
double g_wifiActualCenterFrequencyHz{0.0};
std::string g_wifiDataMode;
std::string g_wifiControlMode;
std::string g_wifiSsid;
std::string g_wifiTopology{"bss"};
bool g_wifiExplicitBarAfterMissedBlockAck{true};
bool g_wifiAggregationEnabled{false};
std::string g_faultProfile{"none"};
uint32_t g_faultMasterId{1};
double g_faultExtraLossDb{200.0};
bool g_faultActive{false};

class NativeRuntimeSampler;
NativeRuntimeSampler* g_runtimeSampler{nullptr};
std::map<Mac48Address, uint32_t> g_wifiRadioIndices;
std::string g_runtimeHeartbeat;
bool g_radiosIsolated{false};
std::string g_propagationProfile{"sionna"};
bool g_runtimeOperational{false};
Ptr<bas::GeometryLogDistance> g_geometry;
bool g_liveStateValid{false};
int64_t g_radioEpochNs{0};
double g_stateMaxAgeS{0.5};
double g_runtimeLagMaxMs{250.0};

// Time the existing public propagation boundary, including CFR/PSD work after
// GetChannel. This measures wall time (including preemption), not CPU cycles.
// The upstream implementation still owns every RF calculation and result.
class MeasuredSpectrumPropagation : public SionnaRtSpectrumPropagationLossModel
{
  public:
    Ptr<SpectrumSignalParameters> DoCalcRxPowerSpectralDensity(
        Ptr<const SpectrumSignalParameters> params, Ptr<const MobilityModel> a,
        Ptr<const MobilityModel> b, Ptr<const PhasedArrayModel> aa,
        Ptr<const PhasedArrayModel> ba) const override
    {
        struct Timing
        {
            std::chrono::steady_clock::time_point start{std::chrono::steady_clock::now()};
            ~Timing()
            {
                const uint64_t elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now()-start).count();
                ++g_spectrumCalls; g_spectrumWallNs+=elapsed;
                if (elapsed>g_spectrumMaxWallNs)
                {
                    g_spectrumMaxWallNs=elapsed;
                    g_spectrumMaxWallNativeNs=Simulator::Now().GetNanoSeconds();
                }
            }
        } timing;
        if (params->psd) g_spectrumMaximumBins=std::max(g_spectrumMaximumBins,static_cast<size_t>(params->psd->GetValuesN()));
        return SionnaRtSpectrumPropagationLossModel::DoCalcRxPowerSpectralDensity(params,a,b,aa,ba);
    }
};

void ObserveWifiPath(uint32_t receiver, Mac48Address sender, Ptr<const Packet> packet);

void ObserveReceivedPath(uint32_t nodeIndex, Ptr<const Packet> packet);

std::string
ReadPhase()
{
    TraceTimingScope timing(g_phaseReadTiming);
    std::ifstream input(g_phaseFile);
    std::string phase = "unclassified";
    if (input.is_open())
    {
        std::getline(input, phase);
    }
    for (char& value : phase)
    {
        if (value == ',')
        {
            value = '_';
        }
    }
    return phase.empty() ? "unclassified" : phase;
}

void
HandleSignal(int)
{
    g_stopRequested = 1;
}

const std::string&
ReadLogPhase()
{
    if (!g_logPhase)
        g_logPhase = std::make_unique<bas::NativeLogPhase>(g_phaseFile, ReadPhase);
    return g_logPhase->Get();
}

void
LogEvent(const std::string& event,
         const std::string& node,
         const std::string& peer,
         uint32_t bytes,
         double value = std::numeric_limits<double>::quiet_NaN(),
         const std::string& details = "")
{
    const bool packetEvent = event == "mac_tx" || event == "mac_tx_drop" ||
                             event == "phy_tx_start" || event == "phy_tx_end" ||
                             event == "phy_rx_start" || event == "phy_rx_end" ||
                             event == "phy_rx_abort" || event == "phy_rx_ok" ||
                             event == "phy_rx_error" || event == "phy_rx_drop" ||
                             event == "wifi_rx_power_dbm" || event == "wifi_monitor_rx" ||
                             event == "spectrum_signal_arrival" || event == "wifi_data_retry" ||
                             event == "radio_queue_enqueue" || event == "radio_queue_dequeue" ||
                             event == "radio_queue_drop" || event.rfind("wifi_mac_queue_",0)==0;
    if (g_eventLogging == "metrics_only" && packetEvent)
    {
        return;
    }
    TraceTimingScope timing(g_eventTraceTiming);
    Vector position;
    for (std::size_t index = 0; index < g_nodeNames.size(); ++index)
    {
        if (g_nodeNames[index] == node)
        {
            position = g_positions[index];
            break;
        }
    }
    const auto wallNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
                            std::chrono::steady_clock::now().time_since_epoch())
                            .count();
    fmt::memory_buffer line;
    const auto simulationSeconds = Simulator::Now().GetSeconds();
    const auto& phase = ReadLogPhase();
    if (std::isfinite(value))
    {
        fmt::format_to(std::back_inserter(line),
                       "{:.6f},{},{},{},{},{},{},{:.6f},{:.6f},{:.6f},{:.6f},{}\n",
                       simulationSeconds, wallNs, phase, event, node, peer, bytes,
                       position.x, position.y, position.z, value, details);
    }
    else
    {
        fmt::format_to(std::back_inserter(line),
                       "{:.6f},{},{},{},{},{},{},{:.6f},{:.6f},{:.6f},,{}\n",
                       simulationSeconds, wallNs, phase, event, node, peer, bytes,
                       position.x, position.y, position.z, details);
    }
    g_events.write(line.data(), static_cast<std::streamsize>(line.size()));
    ++g_pendingEventWrites;
    const auto now = std::chrono::steady_clock::now();
    if (g_pendingEventWrites >= g_flushEveryEvents ||
        now - g_lastEventFlush >= std::chrono::milliseconds(g_flushMaxDelayMs))
    {
        TraceTimingScope flushTiming(g_eventFlushTiming);
        g_events.flush();
        g_pendingEventWrites = 0;
        g_lastEventFlush = now;
    }
}

void
FlushEvents()
{
    if (g_events.is_open())
    {
        TraceTimingScope flushTiming(g_eventFlushTiming);
        g_events.flush();
        g_pendingEventWrites = 0;
        g_lastEventFlush = std::chrono::steady_clock::now();
    }
}

std::optional<Mac48Address>
RadioMacForAddress(Ipv4Address address)
{
    if (address == Ipv4Address("10.71.0.10"))
    {
        return Mac48Address("02:71:ff:00:00:01");
    }
    for (uint32_t index = 1; index < g_nodeNames.size(); ++index)
    {
        std::ostringstream endpoint;
        endpoint << "10.71." << index << ".10";
        if (address == Ipv4Address(endpoint.str().c_str()))
        {
            std::ostringstream mac;
            mac << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << index
                << ":00:10:10";
            return Mac48Address(mac.str().c_str());
        }
    }
    if (address.IsMulticast())
    {
        return Mac48Address::GetMulticast(address);
    }
    if (address == Ipv4Address("255.255.255.255"))
    {
        return Mac48Address::GetBroadcast();
    }
    return std::nullopt;
}

void
WriteRadioPcap(Ptr<const Packet> packet)
{
    Ptr<Packet> copy = packet->Copy();
    LlcSnapHeader llc;
    Mac48Address source;
    Mac48Address destination;
    if (g_radioBackend == "aloha")
    {
        AlohaNoackMacHeader aloha;
        if (copy->RemoveHeader(aloha) == 0 || copy->RemoveHeader(llc) == 0)
        {
            return;
        }
        source = aloha.GetSource();
        destination = aloha.GetDestination();
    }
    else
    {
        if (copy->RemoveHeader(llc) == 0 || llc.GetType() != Ipv4L3Protocol::PROT_NUMBER)
        {
            return;
        }
        Ipv4Header ipv4;
        if (copy->PeekHeader(ipv4) == 0)
        {
            return;
        }
        const auto sourceMac = RadioMacForAddress(ipv4.GetSource());
        const auto destinationMac = RadioMacForAddress(ipv4.GetDestination());
        if (!sourceMac || !destinationMac)
        {
            return;
        }
        source = *sourceMac;
        destination = *destinationMac;
    }
    if (llc.GetType() != Ipv4L3Protocol::PROT_NUMBER)
    {
        return;
    }
    EthernetHeader ethernet(false);
    ethernet.SetSource(source);
    ethernet.SetDestination(destination);
    ethernet.SetLengthType(llc.GetType());
    copy->AddHeader(ethernet);
    g_radioPcap->Write(Simulator::Now(), copy);
}

std::string
PacketTraceDetails(Ptr<const Packet> packet)
{
    std::ostringstream details;
    details << "packet_uid=" << packet->GetUid();

    Ptr<Packet> copy = packet->Copy();
    LlcSnapHeader llc;
    std::optional<Mac48Address> sourceMac;
    std::optional<Mac48Address> destinationMac;
    if (g_radioBackend == "aloha")
    {
        AlohaNoackMacHeader aloha;
        if (copy->RemoveHeader(aloha) == 0 || copy->RemoveHeader(llc) == 0)
        {
            return details.str();
        }
        sourceMac = aloha.GetSource();
        destinationMac = aloha.GetDestination();
    }
    else if (copy->RemoveHeader(llc) == 0)
    {
        return details.str();
    }
    if (llc.GetType() != Ipv4L3Protocol::PROT_NUMBER)
    {
        return details.str();
    }

    Ipv4Header ipv4;
    if (copy->RemoveHeader(ipv4) == 0)
    {
        return details.str();
    }
    if (g_radioBackend == "wifi")
    {
        sourceMac = RadioMacForAddress(ipv4.GetSource());
        destinationMac = RadioMacForAddress(ipv4.GetDestination());
    }
    if (sourceMac && destinationMac)
    {
        details << ";src_mac=" << *sourceMac << ";dst_mac=" << *destinationMac;
    }
    details << ";src_ip=" << ipv4.GetSource() << ";dst_ip=" << ipv4.GetDestination()
            << ";ip_protocol=" << static_cast<uint32_t>(ipv4.GetProtocol());
    if (ipv4.GetProtocol() != UdpL4Protocol::PROT_NUMBER)
    {
        return details.str();
    }

    UdpHeader udp;
    if (copy->RemoveHeader(udp) == 0)
    {
        return details.str();
    }
    details << ";src_port=" << udp.GetSourcePort() << ";dst_port=" << udp.GetDestinationPort();
    const uint16_t source = udp.GetSourcePort();
    const uint16_t destination = udp.GetDestinationPort();
    if (source == 14800 || (source >= 14801 && source <= 14805) || destination == 14800 ||
        (destination >= 14801 && destination <= 14805) || destination == 14900)
    {
        details << ";flow=additional_data";
    }
    return details.str();
}

void
MacTx(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).macTx;
    if (g_eventLogging != "metrics_only")
    {
        LogEvent("mac_tx",
                 g_nodeNames.at(nodeIndex),
                 "",
                 packet->GetSize(),
                 std::numeric_limits<double>::quiet_NaN(),
                 PacketTraceDetails(packet));
    }
    WriteRadioPcap(packet);
}

void
MacTxDrop(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).macTxDrop;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("mac_tx_drop", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyTxStart(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyTxStart;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("phy_tx_start", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyTxEnd(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyTxEnd;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("phy_tx_end", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyRxAbort(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxAbort;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("phy_rx_abort", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
QueueEnqueue(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    NodeCounters& counters = g_nodeCounters.at(nodeIndex);
    ++counters.queueEnqueue;
    const uint32_t depth = g_queues.at(nodeIndex)->GetNPackets();
    counters.queueMaxDepth = std::max(counters.queueMaxDepth, depth);
    g_queueEnqueueAt[packet->GetUid()] = Simulator::Now();
    if (g_eventLogging == "metrics_only") return;
    LogEvent("radio_queue_enqueue", g_nodeNames.at(nodeIndex), "", packet->GetSize(), depth,
             PacketTraceDetails(packet));
}

void
QueueDequeue(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    NodeCounters& counters = g_nodeCounters.at(nodeIndex);
    ++counters.queueDequeue;
    double residenceMs = std::numeric_limits<double>::quiet_NaN();
    const auto found = g_queueEnqueueAt.find(packet->GetUid());
    if (found != g_queueEnqueueAt.end())
    {
        residenceMs = (Simulator::Now() - found->second).GetSeconds() * 1000.0;
        g_queueEnqueueAt.erase(found);
    }
    if (g_eventLogging == "metrics_only") return;
    LogEvent("radio_queue_dequeue", g_nodeNames.at(nodeIndex), "", packet->GetSize(), residenceMs,
             PacketTraceDetails(packet));
}

void
QueueDrop(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).queueDrop;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("radio_queue_drop", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             g_queues.at(nodeIndex)->GetNPackets(), PacketTraceDetails(packet));
}

void
SampleQueues()
{
    for (uint32_t index = 0; index < g_queues.size(); ++index)
    {
        const uint32_t depth = g_queues[index]->GetNPackets();
        g_nodeCounters[index].queueMaxDepth = std::max(g_nodeCounters[index].queueMaxDepth, depth);
        LogEvent("radio_queue_depth", g_nodeNames[index], "", 0, depth, "public_queue_api");
    }
    Simulator::Schedule(Seconds(1), &SampleQueues);
}

void
PhyRxOk(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxOk;
    if (g_eventLogging != "metrics_only")
    {
        LogEvent("phy_rx_ok",
                 g_nodeNames.at(nodeIndex),
                 "",
                 packet->GetSize(),
                 std::numeric_limits<double>::quiet_NaN(),
                 PacketTraceDetails(packet));
    }
    WriteRadioPcap(packet);
}

void
PhyRxStart(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxStart;
    if (g_eventLogging != "metrics_only")
    {
        LogEvent("phy_rx_start",
                 g_nodeNames.at(nodeIndex),
                 "",
                 packet->GetSize(),
                 std::numeric_limits<double>::quiet_NaN(),
                 PacketTraceDetails(packet));
    }
    if (g_radioBackend == "aloha")
    {
        ObserveReceivedPath(nodeIndex, packet);
    }
}

void
PhyRxError(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxError;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("phy_rx_error",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(),
             PacketTraceDetails(packet));
}

void
WifiPhyTxStart(uint32_t nodeIndex, Ptr<const Packet> packet, double)
{
    PhyTxStart(nodeIndex, packet);
}

void
WifiPhyRxStart(uint32_t nodeIndex,
               Ptr<const Packet> packet,
               RxPowerWattPerChannelBand rxPowersW)
{
    PhyRxStart(nodeIndex, packet);
    if (g_eventLogging == "metrics_only") return;

    // SpectrumWifiPhy builds this map after the channel propagation model has
    // transformed the received PSD.  Reproduce SpectrumWifiPhy's total-power
    // integration rule: sum only bands no wider than 20 MHz so nested wider
    // channel views are not counted twice.
    Watt_u totalRxPowerW{0.0};
    uint32_t integratedBandCount{0};
    for (const auto& [band, powerW] : rxPowersW)
    {
        Hz_u bandwidthHz{0.0};
        for (const auto& [startHz, stopHz] : band.frequencies)
        {
            bandwidthHz += stopHz - startHz;
        }
        if (bandwidthHz <= 20e6)
        {
            totalRxPowerW += powerW;
            ++integratedBandCount;
        }
    }

    const double rxPowerDbm = totalRxPowerW > 0.0
                                  ? WToDbm(totalRxPowerW)
                                  : std::numeric_limits<double>::quiet_NaN();
    std::ostringstream details;
    details << PacketTraceDetails(packet) << ";rx_power_w=" << std::setprecision(17)
            << totalRxPowerW << ";integrated_band_count=" << integratedBandCount
            << ";source=PhyRxBegin.RxPowerWattPerChannelBand";
    LogEvent("wifi_rx_power_dbm",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             rxPowerDbm,
             details.str());
}

void
WifiSignalArrival(uint32_t receiver, Ptr<const SpectrumSignalParameters> signal,
                  uint32_t sender, double powerDbm, Time duration)
{
    if (g_eventLogging == "metrics_only") return;
    std::ostringstream details;
    details << "sender_node_id=" << sender << ";duration_s=" << std::setprecision(17)
            << duration.GetSeconds() << ";source=SpectrumWifiPhy.SignalArrival"
            << ";foreign_signal=" << bool(DynamicCast<WaveformGenerator>(signal->txPhy));
    LogEvent("spectrum_signal_arrival", g_nodeNames.at(receiver), std::to_string(sender),
             0, powerDbm, details.str());
}

void
WifiMonitorRx(uint32_t nodeIndex,
              Ptr<const Packet> packet,
              uint16_t frequencyMhz,
              WifiTxVector txVector,
              MpduInfo,
              SignalNoiseDbm signalNoise,
              uint16_t staId)
{
    // Exact ns-3.48 public trace, also used by the standalone reference.
    // This is a successfully decoded MPDU sample, not an outage RSSI probe.
    if (g_eventLogging == "metrics_only") return;
    Ptr<Packet> payload = packet->Copy();
    WifiMacHeader header;
    payload->RemoveHeader(header);
    ObserveWifiPath(nodeIndex, header.GetAddr2(), packet);
    std::ostringstream peer;
    peer << header.GetAddr2();
    std::ostringstream details;
    details << (header.IsData() ? PacketTraceDetails(payload)
                               : "packet_uid=" + std::to_string(packet->GetUid()))
            << std::setprecision(17)
            << ";signal_dbm=" << signalNoise.signal
            << ";noise_interference_dbm=" << signalNoise.noise
            << ";sinr_db=" << signalNoise.signal - signalNoise.noise
            << ";frequency_mhz=" << frequencyMhz
            << ";mode=" << txVector.GetMode(staId)
            << ";mac_sequence=" << header.GetSequenceNumber()
            << ";mac_retry=" << header.IsRetry()
            << ";source=WifiPhy.MonitorSnifferRx.SignalNoiseDbm"
            << ";sampling=successfully_received_mpdu";
    LogEvent("wifi_monitor_rx", g_nodeNames.at(nodeIndex), peer.str(),
             packet->GetSize(), signalNoise.signal, details.str());
}

std::map<std::tuple<uint32_t, uint32_t, uint64_t>, Time> g_queueEntered;
void WifiQueueObservation(uint32_t node, uint32_t ac, Ptr<WifiMacQueue> queue,
                          std::string event, Ptr<const WifiMpdu> mpdu)
{
    NodeCounters& counters = g_nodeCounters.at(node);
    if (event == "enqueue") ++counters.queueEnqueue;
    else if (event == "dequeue") ++counters.queueDequeue;
    else if (event == "drop") ++counters.queueDrop;
    uint32_t totalDepth = 0;
    for (const auto& nativeQueue : g_wifiQueues.at(node))
        totalDepth += nativeQueue->GetNPackets();
    counters.queueMaxDepth = std::max(counters.queueMaxDepth, totalDepth);
    const auto uid=mpdu->GetPacket()->GetUid();
    const auto key=std::make_tuple(node,ac,uid);
    double residenceMs = std::numeric_limits<double>::quiet_NaN();
    if (event=="enqueue") g_queueEntered[key]=Simulator::Now();
    else {
        auto found=g_queueEntered.find(key);
        if (found!=g_queueEntered.end()) {
            residenceMs = (Simulator::Now()-found->second).GetSeconds()*1000;
            g_queueEntered.erase(found);
        }
    }
    if (g_eventLogging == "metrics_only") return;
    std::ostringstream details;
    details << "packet_uid=" << uid << ";ac=" << ac << ";queue_packets=" << queue->GetNPackets()
            << ";queue_bytes=" << queue->GetNBytes() << ";node_all_ac_queue_packets=" << totalDepth
            << ";source=WifiMacQueue." << event;
    if (std::isfinite(residenceMs)) details << ";residence_ms=" << residenceMs;
    LogEvent("wifi_mac_queue_"+event,g_nodeNames.at(node),"",mpdu->GetPacket()->GetSize(),
             queue->GetNPackets(),details.str());
}

void
WifiPhyRxEnd(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxEnd;
    if (g_eventLogging == "metrics_only") return;
    LogEvent("phy_rx_end",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(),
             PacketTraceDetails(packet) + ";verdict=not_yet_available");
}

void
WifiPhyStateRxOk(uint32_t nodeIndex,
                 Ptr<const Packet> packet,
                 double snr,
                 WifiMode mode,
                 WifiPreamble preamble)
{
    ++g_nodeCounters.at(nodeIndex).phyRxOk;
    if (g_eventLogging != "metrics_only")
    {
        std::ostringstream details;
        details << PacketTraceDetails(packet) << ";snr_linear=" << std::setprecision(17) << snr
                << ";mode=" << mode << ";preamble=" << preamble
                << ";source=WifiPhyStateHelper.RxOk";
        LogEvent("phy_rx_ok", g_nodeNames.at(nodeIndex), "", packet->GetSize(), snr, details.str());
    }
    WriteRadioPcap(packet);
}

void
WifiPhyStateRxError(uint32_t nodeIndex, Ptr<const Packet> packet, double snr)
{
    ++g_nodeCounters.at(nodeIndex).phyRxError;
    if (g_eventLogging == "metrics_only") return;
    std::ostringstream details;
    details << PacketTraceDetails(packet) << ";snr_linear=" << std::setprecision(17) << snr
            << ";source=WifiPhyStateHelper.RxError";
    LogEvent("phy_rx_error",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             snr,
             details.str());
}

void
WifiPhyRxDrop(uint32_t nodeIndex, Ptr<const Packet> packet, WifiPhyRxfailureReason reason)
{
    ++g_nodeCounters.at(nodeIndex).phyRxDrop;
    if (g_eventLogging == "metrics_only") return;
    std::ostringstream details;
    details << PacketTraceDetails(packet) << ";reason_code=" << static_cast<uint32_t>(reason)
            << ";source=WifiPhy.PhyRxDrop";
    LogEvent("phy_rx_drop",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             static_cast<double>(reason),
             details.str());
}

void
WifiDataRetry(uint32_t nodeIndex, Mac48Address peer)
{
    ++g_nodeCounters.at(nodeIndex).wifiDataRetries;
    if (g_eventLogging == "metrics_only") return;
    std::ostringstream details;
    details << "peer_mac=" << peer;
    LogEvent("wifi_data_retry", g_nodeNames.at(nodeIndex), "", 0,
             std::numeric_limits<double>::quiet_NaN(), details.str());
}

void
WifiAssociated(uint32_t nodeIndex, Mac48Address ap)
{
    ++g_nodeCounters.at(nodeIndex).wifiAssociations;
    std::ostringstream peer;
    peer << ap;
    LogEvent("wifi_associated", g_nodeNames.at(nodeIndex), peer.str(), 0);
}

void
WifiDeassociated(uint32_t nodeIndex, Mac48Address ap)
{
    ++g_nodeCounters.at(nodeIndex).wifiDeassociations;
    std::ostringstream peer;
    peer << ap;
    LogEvent("wifi_deassociated", g_nodeNames.at(nodeIndex), peer.str(), 0);
}

std::vector<std::string>
Split(const std::string& value, char separator)
{
    std::vector<std::string> result;
    std::istringstream input(value);
    std::string part;
    while (std::getline(input, part, separator))
    {
        if (!part.empty())
        {
            result.push_back(part);
        }
    }
    return result;
}

class LivePositionSource
{
  public:
    LivePositionSource(std::string path, std::vector<Ptr<MobilityModel>> mobility)
        : m_path(std::move(path)), m_mobility(std::move(mobility)) {}

    void SetDeferredApplication(bool deferred)
    {
        if (m_started) throw std::logic_error("Pose application mode must be selected before the first refresh");
        m_deferred = deferred;
    }
    bool DeferredApplication() const { return m_deferred; }
    uint64_t ObservedSnapshots() const { return m_observedSnapshots; }
    uint64_t AppliedSnapshots() const { return m_appliedSnapshots; }
    uint64_t MaximumAppliedPoseAgeNs() const { return m_maximumAppliedAgeNs; }

    void Poll()
    {
        if (!Refresh()) return;
        if (g_executionMode == "realtime")
            Simulator::Schedule(MilliSeconds(g_posePollPeriodMs), &LivePositionSource::Poll, this);
    }

    bool Refresh()
    {
        m_started = true;
        py::gil_scoped_acquire gil;
        const int64_t nowNs = HostNowNs();
        try
        {
            const int64_t eventNs = EventHostNs(nowNs);
            if (!g_lockstep && g_runtimeOperational && (nowNs-eventNs)/1e6 > g_runtimeLagMaxMs)
                throw std::runtime_error("native scheduler exceeded live coupling deadline");
            const auto readerStarted = std::chrono::steady_clock::now();
            auto snapshot = m_reader.Read(m_path, g_nodeNames, eventNs, 0,
                                          static_cast<int64_t>(g_stateMaxAgeS*1e9),
                                          g_lockstep ? g_lockstep->SourceTime(Simulator::Now().GetSeconds()) : -1,
                                          static_cast<int64_t>(g_stepTimeoutS*1e9));
            const uint64_t readerNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - readerStarted).count();
            ++g_poseReaderCalls; g_poseReaderWallNs += readerNs;
            g_poseReaderMaxWallNs = std::max(g_poseReaderMaxWallNs, readerNs);
            if (!g_lockstep && g_runtimeOperational && snapshot.size()>1)
                m_clockAlignment.Check(snapshot[1].sourceTime, Simulator::Now().GetSeconds(), g_stateMaxAgeS);
            // Observation never changes the geometry in deferred mode. The RT
            // owner later commits its ORIGINAL complete measured job frame.
            m_latest = snapshot;
            ++m_observedSnapshots;
            if (m_deferred)
            {
                const int64_t receiptNs = HostNowNs();
                for (size_t i=0; i<snapshot.size(); ++i)
                    LogEvent("live_pose_observed", g_nodeNames[i], "", 0, (receiptNs-snapshot[i].sampleNs)/1e6,
                        "measured_pose_age_ms;source_sim_s="+std::to_string(snapshot[i].sourceTime)+
                        ";sample_host_ns="+std::to_string(snapshot[i].sampleNs)+
                        ";read_publication_host_ns="+std::to_string(m_reader.LastPublicationNs())+
                        ";application_mode=deferred_rt_frame;row_xyz=applied_geometry"+
                        ";observed_x_m="+std::to_string(snapshot[i].position.x)+
                        ";observed_y_m="+std::to_string(snapshot[i].position.y)+
                        ";observed_z_m="+std::to_string(snapshot[i].position.z));
                CheckAppliedFreshness(eventNs,receiptNs);
            }
            else ApplySnapshot(snapshot,eventNs,nowNs);
            g_liveStateValid=true;
        }
        catch (const std::exception& error)
        {
            return RejectSnapshot(error);
        }
        return true;
    }
    void Commit(const std::vector<bas::MeasuredState>& snapshot,
                const std::function<void()>& publishDependents={})
    {
        m_started = true;
        try
        {
            if (!m_deferred) throw std::logic_error("Explicit RT pose commit requires deferred application mode");
            const int64_t receiptNs = HostNowNs(), eventNs = EventHostNs(receiptNs);
            CheckAppliedFreshness(eventNs,receiptNs);
            ApplySnapshot(snapshot,eventNs,receiptNs,publishDependents);
        }
        catch (const std::exception& error)
        {
            RejectSnapshot(error);
            throw;
        }
    }
    std::vector<bas::MeasuredState> Snapshot() const
    {
        if (!g_liveStateValid || m_latest.size() != m_mobility.size())
            throw std::runtime_error("RT producer requires a complete current measured pose cohort");
        return m_latest;
    }
  private:
    static int64_t HostNowNs()
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    int64_t EventHostNs(int64_t wallNs) const
    {
        return bas::MeasuredEventHostNs(m_deferred,g_runtimeOperational,g_radioEpochNs,
                                        Simulator::Now().GetNanoSeconds(),wallNs);
    }
    void CheckAppliedFreshness(int64_t eventNs, int64_t wallNs)
    {
        if (!m_deferred || !g_runtimeOperational) return;
        // Record a known violating age before rejecting it. A fresh observation
        // cannot make an old held geometry frame fresh or invent an application.
        m_maximumAppliedAgeNs = std::max(m_maximumAppliedAgeNs,bas::MaximumMeasuredPoseAgeNs(m_applied,wallNs));
        bas::ValidateMeasuredCohort(m_applied,m_mobility.size(),eventNs,wallNs,
                                   static_cast<int64_t>(g_stateMaxAgeS*1e9));
    }
    void ApplySnapshot(const std::vector<bas::MeasuredState>& snapshot, int64_t eventNs, int64_t wallNs,
                       const std::function<void()>& publishDependents={})
    {
        if (m_deferred)
        {
            bas::CommitMeasuredCohort(snapshot,m_mobility,g_positions,m_applied,eventNs,wallNs,
                static_cast<int64_t>(g_stateMaxAgeS*1e9),true,[&] {
                    if (publishDependents) publishDependents();
                    const int64_t appliedNs = HostNowNs();
                    CheckAppliedFreshness(EventHostNs(appliedNs),appliedNs);
                    RecordApplication(snapshot,appliedNs);
                });
        }
        else
        {
            // Keep the existing immediate/lockstep behavior and clock domain.
            for (size_t i=0; i<snapshot.size(); ++i)
            {
                DynamicCast<bas::MeasuredMobility>(m_mobility[i])->Apply(snapshot[i]);
                g_positions[i]=snapshot[i].position;
            }
            m_applied = snapshot;
            RecordApplication(snapshot,wallNs);
        }
    }
    void RecordApplication(const std::vector<bas::MeasuredState>& snapshot, int64_t wallNs)
    {
        ++m_appliedSnapshots;
        ++g_poseSnapshots;
        if (m_deferred && g_runtimeOperational)
            m_maximumAppliedAgeNs = std::max(m_maximumAppliedAgeNs,bas::MaximumMeasuredPoseAgeNs(m_applied,wallNs));
        for (size_t i=0; i<snapshot.size(); ++i)
        {
            const double ageMs = g_lockstep ? (i==0 ? 0 :
                (g_lockstep->SourceTime(Simulator::Now().GetSeconds())-snapshot[i].sourceTime)*1000)
                : (wallNs-snapshot[i].sampleNs)/1e6;
            const auto origin = m_deferred ? ";sample_host_ns="+std::to_string(snapshot[i].sampleNs)+
                ";application_mode=deferred_rt_frame" :
                ";read_publication_host_ns="+std::to_string(m_reader.LastPublicationNs());
            LogEvent("live_pose",g_nodeNames[i],"",0,ageMs,
                "measured_pose_age_ms;source_sim_s="+std::to_string(snapshot[i].sourceTime)+origin);
        }
        if (g_geometry) g_geometry->ObserveAll();
    }
    bool RejectSnapshot(const std::exception& error)
    {
        g_liveStateValid=false;
        LogEvent("position_snapshot_rejected","tracker","",0,
                 std::numeric_limits<double>::quiet_NaN(),error.what());
        if (g_runtimeOperational || g_lockstep)
        {
            ++g_stalePoseSamples;
            g_stopReason="state_coupling_deadline";
            std::filesystem::remove(g_runtimeHeartbeat);
            LogEvent("fail_closed","tracker","",0);
            Simulator::Stop();
            return false;
        }
        return true;
    }
    std::string m_path;
    std::vector<Ptr<MobilityModel>> m_mobility;
    bas::LiveStateReader m_reader;
    bas::ClockAlignment m_clockAlignment;
    std::vector<bas::MeasuredState> m_latest;
    std::vector<bas::MeasuredState> m_applied;
    bool m_deferred{false}, m_started{false};
    uint64_t m_observedSnapshots{0}, m_appliedSnapshots{0}, m_maximumAppliedAgeNs{0};
};

// Non-owning statistics view; the run closes it before the local pose owner dies.
LivePositionSource* g_poseSourceForStats{nullptr};

class NativeRuntimeSampler
{
  public:
    NativeRuntimeSampler(Ptr<MatrixBasedChannelModel> channelModel,
                         Ptr<MobilityModel> cpMobility,
                         std::vector<Ptr<MobilityModel>> uavMobility)
        : m_channelModel(std::move(channelModel))
    {
        m_mobility.push_back(std::move(cpMobility));
        m_mobility.insert(m_mobility.end(), uavMobility.begin(), uavMobility.end());
    }

    void PollLag()
    {
        Ptr<RealtimeSimulatorImpl> realtime =
            DynamicCast<RealtimeSimulatorImpl>(Simulator::GetImplementation());
        if (realtime)
        {
            const double lagMs = (realtime->RealtimeNow() - Simulator::Now()).GetSeconds() * 1000.0;
            ++g_realtimeLagSamples;
            g_latestRealtimeLagMs = lagMs;
            if (lagMs <= g_readinessLagMaxMs)
            {
                ++g_healthyLagSamples;
            }
            else
            {
                g_healthyLagSamples = 0;
            }
            LogEvent("realtime_lag", "ns3", "", 0, lagMs, "lag_ms");
        }
        if (!g_runtimeHeartbeat.empty() && g_runtimeOperational && g_liveStateValid &&
            (g_lockstep || g_latestRealtimeLagMs <= g_runtimeLagMaxMs))
        {
            const auto stamp = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count();
            std::ofstream heartbeat(g_runtimeHeartbeat + ".tmp");
            heartbeat << "{\"healthy\":true,\"monotonic_ns\":" << stamp
                      << ",\"simulation_s\":" << Simulator::Now().GetSeconds()
                      << ",\"completed_lockstep_steps\":" << g_completedSteps
                      << ",\"radios_isolated\":" << (g_radiosIsolated ? "true" : "false") << "}\n";
            heartbeat.close();
            std::filesystem::rename(g_runtimeHeartbeat + ".tmp", g_runtimeHeartbeat);
        }
        else if (!g_runtimeHeartbeat.empty()) std::filesystem::remove(g_runtimeHeartbeat);
        Simulator::Schedule(MilliSeconds(50), &NativeRuntimeSampler::PollLag, this);
    }

    void PollSionnaPaths()
    {
        if (!m_channelModel) return;
        for (uint32_t uavIndex = 1; uavIndex < m_mobility.size(); ++uavIndex)
        {
            Ptr<const MatrixBasedChannelModel::ChannelParams> params =
                m_channelModel->GetParams(m_mobility.front(), m_mobility.at(uavIndex));
            if (params)
            {
                EmitPathObservation(0,
                                    uavIndex,
                                    params,
                                    "sample=periodic;scope=cp_uav_reciprocal");
            }
        }
        Simulator::Schedule(Seconds(1), &NativeRuntimeSampler::PollSionnaPaths, this);
    }

    void ObserveReceivedPath(uint32_t receiverIndex, Ptr<const Packet> packet)
    {
        if (!m_channelModel || g_eventLogging == "metrics_only") return;
        Ptr<Packet> copy = packet->Copy();
        AlohaNoackMacHeader aloha;
        if (copy->RemoveHeader(aloha) == 0)
        {
            return;
        }
        std::ostringstream sourceValue;
        sourceValue << aloha.GetSource();
        const std::string source = sourceValue.str();
        uint32_t transmitterIndex = g_nodeNames.size();
        if (source == "02:71:ff:00:00:01")
        {
            transmitterIndex = 0;
        }
        else
        {
            for (uint32_t index = 1; index < g_nodeNames.size(); ++index)
            {
                std::ostringstream expected;
                expected << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << index
                         << ":00:10:10";
                if (source == expected.str())
                {
                    transmitterIndex = index;
                    break;
                }
            }
        }
        if (transmitterIndex >= m_mobility.size() || receiverIndex >= m_mobility.size())
        {
            return;
        }
        Ptr<const MatrixBasedChannelModel::ChannelParams> params =
            m_channelModel->GetParams(m_mobility.at(transmitterIndex), m_mobility.at(receiverIndex));
        if (!params)
        {
            return;
        }
        std::ostringstream basis;
        basis << "sample=packet_rx;packet_uid=" << packet->GetUid();
        EmitPathObservation(transmitterIndex, receiverIndex, params, basis.str());
    }

    void ObserveWifiPath(uint32_t receiver, uint32_t transmitter, Ptr<const Packet> packet)
    {
        if (!m_channelModel || receiver == transmitter || g_eventLogging == "metrics_only") return;
        auto params = m_channelModel->GetParams(m_mobility.at(transmitter), m_mobility.at(receiver));
        if (params) EmitPathObservation(transmitter, receiver, params,
            "sample=decoded_wifi_mpdu;packet_uid=" + std::to_string(packet->GetUid()));
    }

  private:
    void EmitPathObservation(
        uint32_t transmitterIndex,
        uint32_t receiverIndex,
        Ptr<const MatrixBasedChannelModel::ChannelParams> params,
        const std::string& basis)
    {
        std::vector<double> delays;
        for (double delay : params->m_delay)
        {
            if (delay >= 0.0 && std::isfinite(delay))
            {
                delays.push_back(delay);
            }
        }
        std::ostringstream details;
        details << basis << ";channel_generation_time_s=" << std::setprecision(12)
                << params->m_generatedTime.GetSeconds() << ";delays_s=";
        for (std::size_t index = 0; index < delays.size(); ++index)
        {
            if (index)
            {
                details << ';';
            }
            details << delays[index];
        }
        ++g_pathObservations;
        LogEvent("sionna_paths",
                 g_nodeNames.at(transmitterIndex),
                 g_nodeNames.at(receiverIndex),
                 0,
                 static_cast<double>(params->m_delay.size()),
                 details.str());
    }

    Ptr<MatrixBasedChannelModel> m_channelModel;
    std::vector<Ptr<MobilityModel>> m_mobility;
};

void
ObserveReceivedPath(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    if (g_runtimeSampler)
    {
        g_runtimeSampler->ObserveReceivedPath(nodeIndex, packet);
    }
}

void ObserveWifiPath(uint32_t receiver, Mac48Address sender, Ptr<const Packet> packet)
{
    const auto tx = g_wifiRadioIndices.find(sender);
    if (g_runtimeSampler && tx != g_wifiRadioIndices.end())
        g_runtimeSampler->ObserveWifiPath(receiver, tx->second, packet);
}

void
PollSignal(NetDeviceContainer devices)
{
    if (g_stopRequested)
    {
        g_stopReason = "sionna_ns3_process_stopped";
        LogEvent("fail_closed_stop", "sionna_in_process", "", 0);
        Simulator::Stop();
        return;
    }
    if (!g_radiosIsolated && ReadPhase() == "no_bypass_radio_off")
    {
        NS_ABORT_MSG_IF(g_radioBackend != "wifi", "Radio isolation requires native Wi-Fi PHYs");
        bool allSleeping = true;
        for (uint32_t i = 0; i < devices.GetN(); ++i)
        {
            auto phy = DynamicCast<WifiNetDevice>(devices.Get(i))->GetPhy();
            if (!phy->IsStateSleep())
            {
                // ns-3.48 SetOffMode during a no-ACK transmission leaves a MAC
                // callback referring to a cleared MPDU. Native sleep waits for
                // TX/RX completion and disables subsequent packet exchange.
                phy->SetSleepMode();
            }
            allSleeping = allSleeping && phy->IsStateSleep();
        }
        if (allSleeping)
        {
            for (uint32_t i = 0; i < devices.GetN(); ++i)
            {
                LogEvent("wifi_radio_sleep", g_nodeNames.at(i), "", 0, 1,
                         "WifiPhy.SetSleepMode; IsStateSleep confirmed");
            }
            g_radiosIsolated = true;
        }
    }
    Simulator::Schedule(MilliSeconds(100), &PollSignal, devices);
}

void
PollFaultMatrix(Ptr<MatrixPropagationLossModel> loss,
                Ptr<MobilityModel> cpMobility,
                std::vector<Ptr<MobilityModel>> uavMobility)
{
    const std::string phase = ReadPhase();
    const bool start = !g_faultActive && phase == "experiment_fault";
    const bool finish = g_faultActive &&
        (phase == "experiment_recovery" || phase == "experiment_landing");
    if (start || finish)
    {
        const double addedLoss = start ? g_faultExtraLossDb : 0.0;
        for (uint32_t i = 0; i < uavMobility.size(); ++i)
        {
            if (g_faultProfile == "total_GCS" || i + 1 == g_faultMasterId)
                loss->SetLoss(cpMobility, uavMobility.at(i), addedLoss, true);
        }
        g_faultActive = start;
        LogEvent(start ? "fault_start" : "fault_end", "cp",
                 g_faultProfile == "total_GCS" ? "all_uavs" : "uav" + std::to_string(g_faultMasterId),
                 0, addedLoss,
                 "provider=ns3::MatrixPropagationLossModel;profile=" + g_faultProfile +
                 ";symmetric=true;default_extra_loss_db=0;base_propagation=friis;"
                 "trigger=phase_file;phase=" + phase);
    }
    Simulator::Schedule(MilliSeconds(50), &PollFaultMatrix, loss, cpMobility, uavMobility);
}

void
WriteReady(const std::string& path)
{
    auto realtime=DynamicCast<RealtimeSimulatorImpl>(Simulator::GetImplementation());
    if (realtime) {
        realtime->SetHardLimit(MilliSeconds(g_runtimeLagMaxMs));
        realtime->SetSynchronizationMode(RealtimeSimulatorImpl::SYNC_HARD_LIMIT);
    }
    g_runtimeOperational = true;
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "ready\n";
}

void
WriteReadyWhenWifiAssociated(const std::string& path)
{
    const bool associated = g_wifiTopology == "adhoc" || (!g_wifiStations.empty() &&
                            std::all_of(g_wifiStations.begin(),
                                        g_wifiStations.end(),
                                        [](const Ptr<StaWifiMac>& mac) {
                                            return mac && mac->IsAssociated();
                                        }));
    const bool realtimeReady = g_realtimeLagSamples > 1 &&
                               g_healthyLagSamples >= g_readinessConsecutiveSamples;
    if (associated && (g_lockstep ? g_completedSteps>0 : realtimeReady) && g_liveStateValid)
    {
        std::ostringstream details;
        details << (g_wifiTopology == "adhoc" ? "native_adhoc;lag_ms=" : "all_stations_associated;lag_ms=")
                << g_latestRealtimeLagMs
                << ";healthy_samples=" << g_healthyLagSamples;
        LogEvent(g_wifiTopology == "adhoc" ? "wifi_adhoc_ready" : "wifi_bss_ready",
                 "cp", "", 0, g_wifiStations.size(), details.str());
        WriteReady(path);
        return;
    }
    Simulator::Schedule(MilliSeconds(100), &WriteReadyWhenWifiAssociated, path);
}

uint32_t
AddRouterInterface(Ptr<Node> router, Ptr<NetDevice> device, const std::string& address)
{
    Ptr<Ipv4> ipv4 = router->GetObject<Ipv4>();
    const uint32_t interface = ipv4->AddInterface(device);
    ipv4->AddAddress(interface,
                     Ipv4InterfaceAddress(Ipv4Address(address.c_str()),
                                          Ipv4Mask("255.255.255.0")));
    ipv4->SetMetric(interface, 1);
    ipv4->SetUp(interface);
    ipv4->SetForwarding(interface, true);
    return interface;
}

void
AddPermanentArp(Ptr<Node> router,
                Ptr<NetDevice> device,
                const std::string& address,
                const Mac48Address& mac)
{
    Ptr<Ipv4L3Protocol> ipv4 = router->GetObject<Ipv4L3Protocol>();
    const int32_t interfaceIndex = ipv4->GetInterfaceForDevice(device);
    NS_ABORT_MSG_IF(interfaceIndex < 0, "radio device has no IPv4 interface");
    Ptr<ArpCache> cache =
        ipv4->GetInterface(static_cast<uint32_t>(interfaceIndex))->GetArpCache();
    NS_ABORT_MSG_IF(!cache, "radio interface has no ARP cache");
    ArpCache::Entry* entry = cache->Lookup(Ipv4Address(address.c_str()));
    if (!entry)
    {
        entry = cache->Add(Ipv4Address(address.c_str()));
    }
    entry->SetMacAddress(mac);
    entry->MarkPermanent();
}

Ptr<UniformPlanarArray>
CreateAntenna()
{
    Ptr<UniformPlanarArray> antenna =
        CreateObjectWithAttributes<UniformPlanarArray>("NumColumns",
                                                       UintegerValue(1),
                                                       "NumRows",
                                                       UintegerValue(1));
    PhasedArrayModel::ComplexVector weights(1);
    weights[0] = std::complex<double>(1.0, 0.0);
    antenna->SetBeamformingVector(weights);
    return antenna;
}

void
AttachWifiAntenna(Ptr<NetDevice> device)
{
    Ptr<WifiNetDevice> wifi = DynamicCast<WifiNetDevice>(device);
    NS_ABORT_MSG_IF(!wifi, "expected WifiNetDevice");
    Ptr<SpectrumWifiPhy> phy = DynamicCast<SpectrumWifiPhy>(wifi->GetPhy());
    NS_ABORT_MSG_IF(!phy, "expected SpectrumWifiPhy");
    Ptr<AntennaModel> antenna = CreateObject<IsotropicAntennaModel>();
    phy->SetAntenna(antenna);
    antenna->AggregateObject(CreateAntenna());
}

void
SetWifiAddress(Ptr<NetDevice> device, const Mac48Address& address)
{
    Ptr<WifiNetDevice> wifi = DynamicCast<WifiNetDevice>(device);
    NS_ABORT_MSG_IF(!wifi, "expected WifiNetDevice");
    wifi->SetAddress(address);
    Ptr<WifiMac> mac = wifi->GetMac();
    for (uint8_t linkId = 0; linkId < mac->GetNLinks(); ++linkId)
    {
        mac->GetFrameExchangeManager(linkId)->SetAddress(address);
    }
}

void
WriteStats(const std::string& path)
{
    uint64_t macTx = 0;
    uint64_t phyRxStart = 0;
    uint64_t phyRxEnd = 0;
    uint64_t phyRxOk = 0;
    uint64_t phyRxError = 0;
    uint64_t phyRxDrop = 0;
    uint64_t phyTxStart = 0;
    uint64_t phyTxEnd = 0;
    uint64_t phyRxAbort = 0;
    uint64_t macTxDrop = 0;
    uint64_t queueEnqueue = 0;
    uint64_t queueDequeue = 0;
    uint64_t queueDrop = 0;
    uint64_t wifiDataRetries = 0;
    uint64_t wifiAssociations = 0;
    uint64_t wifiDeassociations = 0;
    for (const NodeCounters& counters : g_nodeCounters)
    {
        macTx += counters.macTx;
        phyRxStart += counters.phyRxStart;
        phyRxEnd += counters.phyRxEnd;
        phyRxOk += counters.phyRxOk;
        phyRxError += counters.phyRxError;
        phyRxDrop += counters.phyRxDrop;
        phyTxStart += counters.phyTxStart;
        phyTxEnd += counters.phyTxEnd;
        phyRxAbort += counters.phyRxAbort;
        macTxDrop += counters.macTxDrop;
        queueEnqueue += counters.queueEnqueue;
        queueDequeue += counters.queueDequeue;
        queueDrop += counters.queueDrop;
        wifiDataRetries += counters.wifiDataRetries;
        wifiAssociations += counters.wifiAssociations;
        wifiDeassociations += counters.wifiDeassociations;
    }
    const bool sionnaDrivesRxPsd =
        g_propagationProfile == "sionna" && g_phasedArraySpectrumPropagationModels.size() == 1 &&
        g_phasedArraySpectrumPropagationModels.front() ==
            "ns3::SionnaRtSpectrumPropagationLossModel";
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "{\n"
           << "  \"stop_reason\": \"" << g_stopReason << "\",\n"
           << "  \"uav_count\": " << (g_nodeNames.size() - 1) << ",\n"
           << "  \"radio_node_count\": " << g_nodeNames.size() << ",\n"
           << "  \"shared_spectrum_channel_count\": 1,\n"
           << "  \"radio_backend\": \"" << g_radioBackend << "\",\n"
           << "  \"rng_seed\": " << RngSeedManager::GetSeed() << ",\n"
           << "  \"rng_run\": " << RngSeedManager::GetRun() << ",\n"
           << "  \"tx_power_w\": " << g_txPowerW << ",\n"
           << "  \"carrier_hz\": " << g_carrierHz << ",\n"
           << "  \"wifi_channel_number\": " << g_wifiChannelNumber << ",\n"
           << "  \"wifi_channel_width_mhz\": " << g_wifiChannelWidthMhz << ",\n"
           << "  \"wifi_actual_channel_number\": " << g_wifiActualChannelNumber << ",\n"
           << "  \"wifi_actual_center_frequency_hz\": "
           << g_wifiActualCenterFrequencyHz << ",\n"
           << "  \"wifi_data_mode\": \"" << g_wifiDataMode << "\",\n"
           << "  \"wifi_control_mode\": \"" << g_wifiControlMode << "\",\n"
           << "  \"wifi_ssid\": \"" << g_wifiSsid << "\",\n"
           << "  \"wifi_topology\": \"" << g_wifiTopology << "\",\n"
           << "  \"wifi_explicit_bar_after_missed_block_ack\": "
           << (g_wifiExplicitBarAfterMissedBlockAck ? "true" : "false") << ",\n"
           << "  \"wifi_aggregation_enabled\": "
           << (g_wifiAggregationEnabled ? "true" : "false") << ",\n"
           << "  \"fault_profile\": \"" << g_faultProfile << "\",\n"
           << "  \"fault_master_id\": " << g_faultMasterId << ",\n"
           << "  \"fault_extra_loss_db\": " << g_faultExtraLossDb << ",\n"
           << "  \"fault_default_extra_loss_db\": 0,\n"
           << "  \"fault_active_at_stop\": " << (g_faultActive ? "true" : "false") << ",\n"
           << "  \"tap_ingress_segment\": {\"type\": \"local_fast_csma\", \"radio_medium\": false},\n"
           << "  \"cache_policy\": \"" << (g_geometry ? "none_actual_positions_per_packet" :
                "displacement_velocity_orientation_or_time") << "\",\n"
           << "  \"simulation_mode\": \"" << g_executionMode << "\",\n"
           << "  \"coupling_step_ms\": " << g_couplingStepMs << ",\n"
           << "  \"completed_lockstep_steps\": " << g_completedSteps << ",\n"
           << "  \"state_max_age_s\": " << g_stateMaxAgeS << ",\n"
           << "  \"runtime_lag_max_ms\": " << g_runtimeLagMaxMs << ",\n"
           << "  \"velocity_refresh_threshold_mps\": 0.5,\n"
           << "  \"orientation_refresh_threshold_deg\": 5.0,\n"
           << "  \"channel_state_max_age_s\": " << g_channelStateMaxAgeS << ",\n"
           << "  \"endpoint_displacement_threshold_m\": " << g_updateDistanceThresholdM << ",\n"
           << "  \"pose_snapshots\": " << g_poseSnapshots << ",\n"
           << "  \"pose_application\": {\"mode\":\""
           << (g_poseSourceForStats && g_poseSourceForStats->DeferredApplication() ? "coherent_rt" : "independent")
           << "\",\"observed_snapshots\":" << (g_poseSourceForStats ? g_poseSourceForStats->ObservedSnapshots() : 0)
           << ",\"applied_snapshots\":" << (g_poseSourceForStats ? g_poseSourceForStats->AppliedSnapshots() : 0)
           << ",\"maximum_applied_pose_age_ns\":" << (g_poseSourceForStats ? g_poseSourceForStats->MaximumAppliedPoseAgeNs() : 0)
           << ",\"state_max_age_ns\":" << static_cast<int64_t>(g_stateMaxAgeS*1e9) << "},\n"
           << "  \"pose_poll_period_ms\": " << g_posePollPeriodMs << ",\n"
           << "  \"successful_pose_reader_calls\": " << g_poseReaderCalls << ",\n"
           << "  \"successful_pose_reader_wall_ns\": " << g_poseReaderWallNs << ",\n"
           << "  \"successful_pose_reader_max_wall_ns\": " << g_poseReaderMaxWallNs << ",\n"
           << "  \"event_trace_formatter\": {\"backend\":\"fmt_memory_buffer\",\"fmt_version\":" << FMT_VERSION << "},\n"
           << "  \"event_trace_timing\": {\"scope\":\"native_main_thread;phase_reads_include_control_and_log_reload;flush_nested_in_log_event;includes_warmup\",\"log_event\":";
    WriteTraceTiming(output,g_eventTraceTiming);
    output << ",\"read_phase\":";
    WriteTraceTiming(output,g_phaseReadTiming);
    output << ",\"flush\":";
    WriteTraceTiming(output,g_eventFlushTiming);
    output << "},\n";
    const auto phaseStats = g_logPhase ? g_logPhase->GetStats() : bas::NativeLogPhase::Stats{};
    output << "  \"log_phase_observation\": {\"backend\":\"inotify_cached_log_only\","
           << "\"lookups\":" << phaseStats.lookups << ",\"cache_hits\":" << phaseStats.cacheHits
           << ",\"reloads\":" << phaseStats.reloads << ",\"relevant_events\":" << phaseStats.relevantEvents
           << ",\"ignored_events\":" << phaseStats.ignoredEvents << ",\"overflows\":" << phaseStats.overflows
           << ",\"watch_failures\":" << phaseStats.watchFailures << ",\"read_errors\":" << phaseStats.readErrors
           << ",\"fallback_calls\":" << phaseStats.fallbackCalls << ",\"rebinds\":" << phaseStats.rebinds
           << ",\"watch_active\":" << (phaseStats.watchActive ? "true" : "false")
           << ",\"closed\":" << (phaseStats.closed ? "true" : "false")
           << ",\"notification_fd\":" << phaseStats.notificationFd << ",\"last_errno\":" << phaseStats.lastErrno
           << ",\"last_fallback_reason\":" << std::quoted(phaseStats.lastFallbackReason) << "},\n"
           << "  \"desired_spectrum_boundary\": {\"calls\":" << g_spectrumCalls
           << ",\"wall_ns\":" << g_spectrumWallNs << ",\"max_wall_ns\":" << g_spectrumMaxWallNs
           << ",\"max_wall_native_ns\":" << g_spectrumMaxWallNativeNs
           << ",\"maximum_bins\":" << g_spectrumMaximumBins
           << ",\"scope\":\"upstream_CIR_to_RX_PSD_including_cache_lookup;wall_not_cpu_cycles\"},\n"
           << "  \"pose_updates\": " << g_poseSnapshots << ",\n"
           << "  \"stale_pose_samples\": " << g_stalePoseSamples << ",\n"
           << "  \"path_observations\": " << g_pathObservations << ",\n"
           << "  \"path_observation_scope\": \""
           << (g_eventLogging == "metrics_only" ? "periodic_cp_uav" : "periodic_and_decoded_packet_pairs") << "\",\n"
           << "  \"realtime_lag_samples\": " << g_realtimeLagSamples << ",\n"
           << "  \"readiness_lag_max_ms\": " << g_readinessLagMaxMs << ",\n"
           << "  \"readiness_consecutive_samples\": " << g_readinessConsecutiveSamples << ",\n"
           << "  \"mac_tx_total\": " << macTx << ",\n"
           << "  \"phy_rx_start_total\": " << phyRxStart << ",\n"
           << "  \"phy_rx_end_total\": " << phyRxEnd << ",\n"
           << "  \"phy_rx_ok_total\": " << phyRxOk << ",\n"
           << "  \"phy_rx_error_total\": " << phyRxError << ",\n"
           << "  \"phy_rx_drop_total\": " << phyRxDrop << ",\n"
           << "  \"phy_tx_start_total\": " << phyTxStart << ",\n"
           << "  \"phy_tx_end_total\": " << phyTxEnd << ",\n"
           << "  \"phy_rx_abort_total\": " << phyRxAbort << ",\n"
           << "  \"mac_tx_drop_total\": " << macTxDrop << ",\n"
           << "  \"queue_enqueue_total\": " << queueEnqueue << ",\n"
           << "  \"queue_dequeue_total\": " << queueDequeue << ",\n"
           << "  \"queue_drop_total\": " << queueDrop << ",\n"
           << "  \"queue_counter_source\": \""
           << (g_radioBackend == "wifi" ? "ns3::WifiMacQueue.Enqueue/Dequeue/Drop"
                                         : "ns3::Queue<Packet>.Enqueue/Dequeue/Drop") << "\",\n"
           << "  \"queue_max_depth_scope\": \""
           << (g_radioBackend == "wifi"
                   ? "sum_of_four_AC_GetNPackets_at_queue_events_including_inflight_MPDUs"
                   : "single_native_queue_GetNPackets_at_queue_events") << "\",\n"
           << "  \"wifi_data_retry_total\": " << wifiDataRetries << ",\n"
           << "  \"wifi_association_total\": " << wifiAssociations << ",\n"
           << "  \"wifi_deassociation_total\": " << wifiDeassociations << ",\n"
           << "  \"event_logging\": \"" << g_eventLogging << "\",\n"
           << "  \"cp_mac_tx\": " << g_nodeCounters.front().macTx << ",\n"
           << "  \"uav_mac_tx\": " << (macTx - g_nodeCounters.front().macTx) << ",\n"
           << "  \"cp_phy_rx_start\": " << g_nodeCounters.front().phyRxStart << ",\n"
           << "  \"uav_phy_rx_start\": " << (phyRxStart - g_nodeCounters.front().phyRxStart) << ",\n"
           << "  \"cp_phy_rx_end\": " << g_nodeCounters.front().phyRxEnd << ",\n"
           << "  \"uav_phy_rx_end\": " << (phyRxEnd - g_nodeCounters.front().phyRxEnd) << ",\n"
           << "  \"cp_phy_rx_ok\": " << g_nodeCounters.front().phyRxOk << ",\n"
           << "  \"uav_phy_rx_ok\": " << (phyRxOk - g_nodeCounters.front().phyRxOk) << ",\n"
           << "  \"cp_phy_rx_error\": " << g_nodeCounters.front().phyRxError << ",\n"
           << "  \"uav_phy_rx_error\": " << (phyRxError - g_nodeCounters.front().phyRxError) << ",\n"
           << "  \"cp_phy_rx_drop\": " << g_nodeCounters.front().phyRxDrop << ",\n"
           << "  \"uav_phy_rx_drop\": " << (phyRxDrop - g_nodeCounters.front().phyRxDrop) << ",\n"
           << "  \"nodes\": {\n";
    for (std::size_t index = 0; index < g_nodeNames.size(); ++index)
    {
        const NodeCounters& counters = g_nodeCounters[index];
        output << "    \"" << g_nodeNames[index] << "\": {\"mac_tx\": " << counters.macTx
               << ", \"phy_rx_start\": " << counters.phyRxStart << ", \"phy_rx_end\": "
               << counters.phyRxEnd << ", \"phy_rx_ok\": "
               << counters.phyRxOk << ", \"phy_rx_error\": " << counters.phyRxError
               << ", \"phy_rx_drop\": " << counters.phyRxDrop
               << ", \"phy_tx_start\": " << counters.phyTxStart
               << ", \"phy_tx_end\": " << counters.phyTxEnd
               << ", \"phy_rx_abort\": " << counters.phyRxAbort
               << ", \"mac_tx_drop\": " << counters.macTxDrop
               << ", \"queue_enqueue\": " << counters.queueEnqueue
               << ", \"queue_dequeue\": " << counters.queueDequeue
               << ", \"queue_drop\": " << counters.queueDrop
               << ", \"wifi_data_retries\": " << counters.wifiDataRetries
               << ", \"wifi_associations\": " << counters.wifiAssociations
               << ", \"wifi_deassociations\": " << counters.wifiDeassociations
               << ", \"queue_max_depth\": " << counters.queueMaxDepth << "}";
        output << (index + 1 == g_nodeNames.size() ? "\n" : ",\n");
    }
    output << "  },\n"
           << "  \"profile\": \"" << g_radioProfile << "\",\n"
           << "  \"technology_specific_modem\": "
           << (g_technologySpecificModem ? "true" : "false") << ",\n"
           << "  \"native_ns3_phy\": true,\n"
           << "  \"native_ns3_mac\": true,\n"
           << "  \"custom_packet_error_model\": false,\n"
           << "  \"custom_scheduler\": false,\n"
           << "  \"sionna_in_process\": "
           << (((g_propagationProfile == "sionna" || g_propagationProfile == "hybrid") &&
                g_sionnaRtExecutionMode == "in_process") ? "true" : "false") << ",\n"
           << "  \"sionna_solver_enabled\": "
           << ((g_propagationProfile == "sionna" || g_propagationProfile == "hybrid") ? "true" : "false") << ",\n"
           << "  \"selected_propagation_profile\": \"" << g_propagationProfile << "\",\n"
           << "  \"scalar_base_propagation_model_count\": " << (g_geometry ? 1 : 0) << ",\n"
           << "  \"geometry_radio_mode\": \"" << (g_geometry ? g_geometry->mode : "not_enabled") << "\",\n"
           << "  \"geometry_parameter_set\": \"" << (g_geometry ? g_geometry->parameterSet : "not_enabled") << "\",\n"
           << "  \"geometry_packet_evaluations\": " << (g_geometry ? g_geometry->PacketEvaluations() : 0) << ",\n"
           << "  \"geometry_observation_rows\": " << (g_geometry ? g_geometry->RecordedRows() : 0) << ",\n"
           << "  \"phased_array_spectrum_propagation_model_count\": "
           << g_phasedArraySpectrumPropagationModels.size() << ",\n"
           << "  \"rx_psd_propagation_model\": \""
           << (g_phasedArraySpectrumPropagationModels.empty()
                   ? (g_geometry ? "bas::GeometryLogDistance" : "none")
                   : g_phasedArraySpectrumPropagationModels.front())
           << "\",\n"
           << "  \"sionna_drives_rx_psd\": " << (sionnaDrivesRxPsd ? "true" : "false")
           << ",\n"
           << "  \"neighbor_discovery_mode\": \"" << g_neighborDiscoveryMode << "\",\n"
           << "  \"reason\": \"" << g_radioReason << "\",\n"
           << "  \"solver_profile\": \"" << g_solverProfile << "\",\n"
           << "  \"sionna_receiver_batch_mode\": \"" << g_sionnaReceiverBatchMode << "\",\n"
           << "  \"sionna_receiver_batch\": " << g_sionnaReceiverBatchStatsJson << ",\n"
           << "  \"sionna_rt_execution_mode\": \"" << g_sionnaRtExecutionMode << "\",\n"
           << "  \"sionna_rt_workers\": " << g_sionnaRtWorkersJson << ",\n"
           << "  \"sionna_solver\": {\"max_depth\": " << g_sionnaMaxDepth
           << ", \"los\": " << (g_sionnaLos ? "true" : "false")
           << ", \"specular_reflection\": "
           << (g_sionnaSpecularReflection ? "true" : "false")
           << ", \"diffuse_reflection\": "
           << (g_sionnaDiffuseReflection ? "true" : "false")
           << ", \"diffraction\": " << (g_sionnaDiffraction ? "true" : "false")
           << ", \"edge_diffraction\": "
           << (g_sionnaEdgeDiffraction ? "true" : "false")
           << ", \"refraction\": " << (g_sionnaRefraction ? "true" : "false")
           << ", \"synthetic_array\": "
           << (g_sionnaSyntheticArray ? "true" : "false")
           << ", \"seed\": " << g_sionnaSeed
           << ", \"max_number_of_paths\": " << g_sionnaMaxNumberOfPaths
           << ", \"samples_per_src\": " << g_sionnaSamplesPerSource
           << ", \"max_num_paths_per_src\": " << g_sionnaMaxNumPathsPerSource
           << ", \"cache_expiry_jitter_fraction\": " << g_sionnaCacheJitterFraction
           << "},\n"
           << "  \"packet_outcome_affected\": "
           << ((sionnaDrivesRxPsd || g_geometry) ? "true" : "false") << "\n"
           << "}\n";
}

} // namespace

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    // LiveStateReader uses Python stdlib JSON even with native Friis.
    py::scoped_interpreter python{};

    uint32_t uavCount = 1;
    std::string radioBackend = "aloha";
    std::string radioProfile = "generic_native_spectrum_aloha_reference";
    bool technologySpecificModem = false;
    std::string neighborDiscoveryMode = "preconfigured_static_neighbors";
    std::string radioReason = "upstream_ideal_phy_arp_reentrancy_limit";
    std::string ignoredLegacyPacketOutcomeClaim;
    std::string solverProfile = "realtime_minimal_solver_profile";
    std::string sionnaReceiverBatchMode = "pairwise";
    std::string sionnaRtExecutionMode = "in_process";
    uint32_t sionnaWorkerCount = 3;
    std::string projectRoot, sionnaWorkerBinary, sionnaWorkerConfig;
    std::string wifiDataMode = "HtMcs0";
    std::string wifiControlMode = "ErpOfdmRate6Mbps";
    bool wifiAggregation = true;
    std::string sourceConfig;
    std::string geometryConfig, geometryCsv;
    std::string attributeDump;
    std::string wifiSsid = "bas-native-radio";
    uint16_t wifiChannelNumber = 1;
    uint16_t wifiChannelWidthMhz = 20;
    double carrierHz = 2.412e9;
    std::string tapGcs = "tap-gcs";
    std::string tapUav = "tap-uav";
    std::string tapUavs;
    std::string scene;
    std::string positionFile;
    std::string radioPcap = "upstream-radio.pcap";
    std::string eventCsv = "upstream-radio-events.csv";
    std::string statsFile = "upstream-radio-stats.json";
    std::string readyFile;
    std::string phaseFile;
    double duration = 120.0;
    std::string clockFile;
    double txPowerW = 0.00001;
    uint64_t phyRateBps = 1000000;
    double channelStateMaxAgeS = 0.5;
    double updateDistanceThresholdM = 0.5;
    std::string eventLogging = "batched_trace";
    uint32_t flushEveryEvents = 256;
    uint32_t flushMaxDelayMs = 25;
    double readinessLagMaxMs = 50.0;
    uint32_t readinessConsecutiveSamples = 3;
    uint32_t sionnaMaxDepth = 1;
    bool sionnaLos = true;
    bool sionnaSpecularReflection = true;
    bool sionnaDiffuseReflection = false;
    bool sionnaDiffraction = false;
    bool sionnaEdgeDiffraction = false;
    bool sionnaRefraction = false;
    bool sionnaSyntheticArray = true;
    uint32_t sionnaSeed = 42;
    uint32_t sionnaMaxNumberOfPaths = 90;
    uint32_t sionnaSamplesPerSource = 1000000;
    uint32_t sionnaMaxNumPathsPerSource = 1000000;
    double sionnaCacheJitterFraction = 0.0;

    CommandLine command(__FILE__);
    command.AddValue("uavCount", "Number of UAV radio nodes (supported: 1..5)", uavCount);
    command.AddValue("radioBackend", "Native MAC/PHY backend: aloha or wifi", radioBackend);
    command.AddValue("radioProfile", "Product radio profile identifier", radioProfile);
    command.AddValue("technologySpecificModem",
                     "Whether the backend is a technology-specific modem",
                     technologySpecificModem);
    command.AddValue("neighborDiscoveryMode",
                     "Configured neighbor discovery mode",
                     neighborDiscoveryMode);
    command.AddValue("radioReason", "Configured radio implementation rationale", radioReason);
    command.AddValue("packetOutcomeAffected",
                     "Deprecated compatibility input; packet outcome influence is derived from "
                     "the installed spectrum propagation chain",
                     ignoredLegacyPacketOutcomeClaim);
    command.AddValue("solverProfile", "Sionna solver profile identifier", solverProfile);
    command.AddValue("wifiDataMode", "Constant 802.11n data mode", wifiDataMode);
    command.AddValue("geometryConfig", "Explicit building/empirical parameter JSON", geometryConfig);
    command.AddValue("geometryCsv", "Actual geometry/pathloss observation CSV", geometryCsv);
    command.AddValue("sources", "Explicit resolved WaveformGenerator source config", sourceConfig);
    command.AddValue("attributeDump", "Optional native ConfigStore RawText snapshot path", attributeDump);
    command.AddValue("wifiControlMode", "Constant 802.11n control mode", wifiControlMode);
    command.AddValue("wifiSsid", "Native Wi-Fi SSID", wifiSsid);
    command.AddValue("wifiTopology", "Explicit native bss or adhoc MAC topology", g_wifiTopology);
    command.AddValue("wifiExplicitBarAfterMissedBlockAck",
                     "Upstream QosTxop explicit BAR recovery after a missed Block ACK",
                     g_wifiExplicitBarAfterMissedBlockAck);
    command.AddValue("wifiAggregation",
                     "Preserve upstream aggregation defaults or explicitly disable aggregation/BA",
                     wifiAggregation);
    command.AddValue("faultProfile", "Explicit none, selective or total_GCS pair-loss fault", g_faultProfile);
    command.AddValue("faultMasterId", "Initial coordinator UAV affected by selective loss", g_faultMasterId);
    command.AddValue("faultExtraLossDb", "Synthetic additional pair loss in dB during experiment_fault", g_faultExtraLossDb);
    command.AddValue("wifiChannelNumber", "802.11 channel number", wifiChannelNumber);
    command.AddValue("wifiChannelWidthMhz", "802.11n channel width", wifiChannelWidthMhz);
    command.AddValue("carrierHz", "Sionna and Wi-Fi carrier frequency", carrierHz);
    command.AddValue("tapGcs", "Existing command-post TAP device", tapGcs);
    command.AddValue("tapUav", "Backward-compatible single UAV TAP device", tapUav);
    command.AddValue("tapUavs", "Comma-separated UAV TAP devices", tapUavs);
    command.AddValue("scene", "Town01 scene.xml path", scene);
    command.AddValue("positionFile", "Live position tracker JSON", positionFile);
    command.AddValue("radioPcap", "Native radio MAC PCAP", radioPcap);
    command.AddValue("eventCsv", "Native radio and position event CSV", eventCsv);
    command.AddValue("statsFile", "Final native radio counters", statsFile);
    command.AddValue("readyFile", "Readiness file", readyFile);
    command.AddValue("propagationProfile", "Explicit sionna, friis, hybrid or geometry_logdistance", g_propagationProfile);
    command.AddValue("phaseFile", "Current product flight phase file", phaseFile);
    command.AddValue("duration", "Maximum ns-3 simulation duration in seconds", duration);
    command.AddValue("executionMode", "realtime or lockstep", g_executionMode);
    command.AddValue("clockFile", "Lockstep shared clock and Gazebo barrier files", clockFile);
    command.AddValue("couplingStepMs", "Lockstep macrostep in simulation milliseconds", g_couplingStepMs);
    command.AddValue("stepTimeoutS", "Lockstep host wall timeout", g_stepTimeoutS);
    command.AddValue("txPowerW", "Spectrum transmitter power in watts", txPowerW);
    command.AddValue("phyRateBps", "Configured native PHY bit rate", phyRateBps);
    command.AddValue("stateMaxAgeS", "Maximum measured pose/clock host age", g_stateMaxAgeS);
    command.AddValue("runtimeLagMaxMs", "Fail closed beyond live scheduler lag", g_runtimeLagMaxMs);
    command.AddValue("channelStateMaxAgeS", "Maximum age of a live Sionna channel realization", channelStateMaxAgeS);
    command.AddValue("updateDistanceThresholdM",
                     "Endpoint displacement that invalidates a live Sionna channel realization",
                     updateDistanceThresholdM);
    command.AddValue("eventLogging", "metrics_only or batched_trace", eventLogging);
    command.AddValue("flushEveryEvents", "Batched trace flush event limit", flushEveryEvents);
    command.AddValue("flushMaxDelayMs", "Batched trace maximum flush delay", flushMaxDelayMs);
    command.AddValue("readinessLagMaxMs", "Maximum scheduler lag before Wi-Fi readiness", readinessLagMaxMs);
    command.AddValue("readinessConsecutiveSamples",
                     "Consecutive healthy lag samples required before Wi-Fi readiness",
                     readinessConsecutiveSamples);
    command.AddValue("sionnaMaxDepth", "Sionna path solver maximum depth", sionnaMaxDepth);
    command.AddValue("sionnaLos", "Enable Sionna LOS paths", sionnaLos);
    command.AddValue("sionnaSpecularReflection",
                     "Enable Sionna specular reflection paths",
                     sionnaSpecularReflection);
    command.AddValue("sionnaDiffuseReflection",
                     "Enable Sionna diffuse reflection paths",
                     sionnaDiffuseReflection);
    command.AddValue("sionnaDiffraction", "Enable Sionna diffraction paths", sionnaDiffraction);
    command.AddValue("sionnaEdgeDiffraction",
                     "Enable Sionna edge diffraction paths",
                     sionnaEdgeDiffraction);
    command.AddValue("sionnaRefraction", "Enable Sionna refraction paths", sionnaRefraction);
    command.AddValue("sionnaSyntheticArray",
                     "Enable Sionna synthetic arrays",
                     sionnaSyntheticArray);
    command.AddValue("sionnaSeed", "Sionna solver random seed", sionnaSeed);
    command.AddValue("sionnaSamplesPerSource", "Actual PathSolver samples_per_src budget", sionnaSamplesPerSource);
    command.AddValue("sionnaMaxNumPathsPerSource", "Actual PathSolver max_num_paths_per_src budget", sionnaMaxNumPathsPerSource);
    command.AddValue("sionnaReceiverBatchMode", "pairwise, receiver_batch or explicit worker full_mesh", sionnaReceiverBatchMode);
    command.AddValue("sionnaRtExecutionMode", "in_process or explicit worker_pool", sionnaRtExecutionMode);
    command.AddValue("sionnaWorkerCount", "1..3 bounded native RT producer processes", sionnaWorkerCount);
    command.AddValue("projectRoot", "Absolute project root for the native RT worker", projectRoot);
    command.AddValue("sionnaWorkerBinary", "Built native RT producer executable", sionnaWorkerBinary);
    command.AddValue("sionnaWorkerConfig", "New run-owned frozen worker config JSON", sionnaWorkerConfig);
    command.AddValue("sionnaMaxNumberOfPaths",
                     "Maximum Sionna paths retained per link",
                     sionnaMaxNumberOfPaths);
    command.AddValue("sionnaCacheJitterFraction",
                     "Deterministic per-pair cache threshold spread",
                     sionnaCacheJitterFraction);
    command.Parse(argc, argv);
    NS_ABORT_MSG_IF(sionnaSamplesPerSource == 0 || sionnaMaxNumPathsPerSource == 0,
                    "Sionna solver budgets must be positive");
    NS_ABORT_MSG_IF(g_executionMode != "realtime" && g_executionMode != "lockstep", "invalid execution mode");
    NS_ABORT_MSG_IF(g_couplingStepMs<1 || g_couplingStepMs>50 || !std::isfinite(g_stepTimeoutS) || g_stepTimeoutS<=0,
                    "lockstep needs 1..50 ms steps and a positive finite wall timeout");
    GlobalValue::Bind("SimulatorImplementationType", StringValue(g_executionMode=="lockstep"
        ? "ns3::DefaultSimulatorImpl" : "ns3::RealtimeSimulatorImpl"));
    if (g_executionMode=="lockstep") {
        NS_ABORT_MSG_IF(clockFile.empty(), "lockstep requires clockFile");
    }
    g_txPowerW = txPowerW;
    g_carrierHz = carrierHz;
    g_wifiChannelNumber = wifiChannelNumber;
    g_wifiChannelWidthMhz = wifiChannelWidthMhz;
    g_wifiDataMode = wifiDataMode;
    g_wifiControlMode = wifiControlMode;
    g_wifiSsid = wifiSsid;

    NS_ABORT_MSG_IF(uavCount < 1 || uavCount > 5, "uavCount must be in 1..5");
    NS_ABORT_MSG_IF(radioBackend != "aloha" && radioBackend != "wifi",
                    "radioBackend must be aloha or wifi");
    NS_ABORT_MSG_IF(g_wifiTopology != "bss" && g_wifiTopology != "adhoc",
                    "wifiTopology must be bss or adhoc");
    NS_ABORT_MSG_IF(g_wifiTopology == "adhoc" && radioBackend != "wifi",
                    "adhoc topology requires the native Wi-Fi backend");
    NS_ABORT_MSG_IF(radioBackend == "wifi" && wifiChannelWidthMhz != 20,
                    "the product Wi-Fi profile currently requires a 20 MHz channel");
    NS_ABORT_MSG_IF(radioBackend == "wifi" && wifiChannelNumber == 0,
                    "the product Wi-Fi profile requires an explicit channel number");
    NS_ABORT_MSG_IF(wifiSsid.empty(), "wifiSsid must not be empty");
    NS_ABORT_MSG_IF(radioProfile.empty() || neighborDiscoveryMode.empty() || radioReason.empty() ||
                        solverProfile.empty(),
                    "radio profile metadata must not be empty");
    if (tapUavs.empty())
    {
        tapUavs = tapUav;
    }
    const std::vector<std::string> uavTapNames = Split(tapUavs, ',');
    NS_ABORT_MSG_IF(uavTapNames.size() != uavCount,
                    "tapUavs count must match uavCount (got "
                        << uavTapNames.size() << ", expected " << uavCount << ")");
    NS_ABORT_MSG_IF(g_propagationProfile != "sionna" && g_propagationProfile != "friis" &&
                    g_propagationProfile != "hybrid" && g_propagationProfile != "geometry_logdistance",
                    "invalid explicit propagation profile");
    const bool requiresRt = g_propagationProfile == "sionna" || g_propagationProfile == "hybrid";
    const bool geometryProfile = g_propagationProfile == "geometry_logdistance";
    NS_ABORT_MSG_IF(sionnaReceiverBatchMode != "pairwise" && sionnaReceiverBatchMode != "receiver_batch" &&
                    sionnaReceiverBatchMode != "full_mesh",
                    "sionnaReceiverBatchMode must be pairwise, receiver_batch or explicit full_mesh");
    NS_ABORT_MSG_IF(sionnaReceiverBatchMode != "pairwise" &&
                    (g_propagationProfile != "sionna" || radioBackend != "wifi" ||
                     g_executionMode != "realtime" || !sionnaSyntheticArray),
                    "RT batching requires the native Wi-Fi/Sionna realtime iso 1x1 reference");
    NS_ABORT_MSG_IF(sionnaRtExecutionMode != "in_process" && sionnaRtExecutionMode != "worker_pool",
                    "sionnaRtExecutionMode must be in_process or worker_pool");
    const bool useRtWorkers = sionnaRtExecutionMode == "worker_pool";
    NS_ABORT_MSG_IF(sionnaReceiverBatchMode == "full_mesh" && !useRtWorkers,
                    "full_mesh requires the explicit worker_pool execution mode");
    NS_ABORT_MSG_IF(sionnaWorkerCount < 1 || sionnaWorkerCount > 3 ||
                    (!useRtWorkers && sionnaWorkerCount != 3), "invalid or unused RT worker count");
    NS_ABORT_MSG_IF(useRtWorkers && ((sionnaReceiverBatchMode != "receiver_batch" && sionnaReceiverBatchMode != "full_mesh") ||
                    (!sourceConfig.empty() && sionnaReceiverBatchMode != "full_mesh") || projectRoot.empty() || sionnaWorkerBinary.empty() ||
                    sionnaWorkerConfig.empty() || sionnaMaxNumberOfPaths > 90 ||
                    sionnaCacheJitterFraction != 0 || channelStateMaxAgeS > 0.5 ||
                    updateDistanceThresholdM > 0.5),
                    "worker_pool requires receiver_batch or explicit full_mesh, zero expiry jitter, <=90 paths, "
                    "run-owned paths; directed sources require full_mesh coherent bundles");
    NS_ABORT_MSG_IF(geometryProfile && (geometryConfig.empty() || geometryCsv.empty() ||
                    !sourceConfig.empty() || g_faultProfile != "none"),
                    "geometry profile requires explicit geometryConfig/geometryCsv and no injected losses/sources");
    NS_ABORT_MSG_IF(g_faultProfile != "none" && g_faultProfile != "selective" &&
                    g_faultProfile != "total_GCS", "invalid explicit fault profile");
    NS_ABORT_MSG_IF(g_faultMasterId < 1 || g_faultMasterId > uavCount ||
                    !std::isfinite(g_faultExtraLossDb) || g_faultExtraLossDb <= 0.0,
                    "invalid fault master ID or added pair loss");
    NS_ABORT_MSG_IF(g_faultProfile != "none" && (g_propagationProfile != "friis" || radioBackend != "wifi"),
                    "pair-loss experiment currently requires explicit native Friis Wi-Fi");
    NS_ABORT_MSG_IF(positionFile.empty() || readyFile.empty() || phaseFile.empty(),
                    "positionFile, readyFile, and phaseFile are required");
    NS_ABORT_MSG_IF(requiresRt && (scene.empty() || !std::filesystem::exists(scene)),
                    "RT scene.xml is missing: " << scene);

    std::signal(SIGTERM, HandleSignal);
    std::signal(SIGINT, HandleSignal);
    NS_ABORT_MSG_IF(g_stateMaxAgeS <= 0 || g_runtimeLagMaxMs <= 0 || channelStateMaxAgeS <= 0.0 || updateDistanceThresholdM <= 0.0,
                    "channel-state age and displacement threshold must be positive");
    NS_ABORT_MSG_IF(phyRateBps == 0 || (eventLogging != "metrics_only" && eventLogging != "batched_trace") ||
                        flushEveryEvents == 0 || flushMaxDelayMs == 0 || readinessLagMaxMs <= 0.0 ||
                        readinessConsecutiveSamples == 0 || sionnaMaxDepth == 0 ||
                        sionnaMaxNumberOfPaths == 0 || sionnaCacheJitterFraction < 0.0 ||
                        sionnaCacheJitterFraction > 0.9,
                    "invalid PHY rate or event logging configuration");
    g_channelStateMaxAgeS = channelStateMaxAgeS;
    g_updateDistanceThresholdM = updateDistanceThresholdM;
    g_eventLogging = eventLogging;
    g_radioBackend = radioBackend;
    g_radioProfile = radioProfile;
    g_technologySpecificModem = technologySpecificModem;
    g_neighborDiscoveryMode = neighborDiscoveryMode;
    g_radioReason = radioReason;
    g_solverProfile = solverProfile;
    g_sionnaReceiverBatchMode = sionnaReceiverBatchMode;
    g_sionnaRtExecutionMode = sionnaRtExecutionMode;
    g_posePollPeriodMs = useRtWorkers ? 5 : 20;
    g_sionnaMaxDepth = sionnaMaxDepth;
    g_sionnaLos = sionnaLos;
    g_sionnaSpecularReflection = sionnaSpecularReflection;
    g_sionnaDiffuseReflection = sionnaDiffuseReflection;
    g_sionnaDiffraction = sionnaDiffraction;
    g_sionnaEdgeDiffraction = sionnaEdgeDiffraction;
    g_sionnaRefraction = sionnaRefraction;
    g_sionnaSyntheticArray = sionnaSyntheticArray;
    g_sionnaSeed = sionnaSeed;
    g_sionnaMaxNumberOfPaths = sionnaMaxNumberOfPaths;
    g_sionnaSamplesPerSource = sionnaSamplesPerSource;
    g_sionnaMaxNumPathsPerSource = sionnaMaxNumPathsPerSource;
    g_sionnaCacheJitterFraction = sionnaCacheJitterFraction;
    g_flushEveryEvents = flushEveryEvents;
    g_flushMaxDelayMs = flushMaxDelayMs;
    g_readinessLagMaxMs = readinessLagMaxMs;
    g_readinessConsecutiveSamples = readinessConsecutiveSamples;
    if (requiresRt) {
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod",
                       TimeValue(Seconds(channelStateMaxAgeS)));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdateDistanceThreshold",
                       DoubleValue(updateDistanceThresholdM));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdateJitterFraction",
                       DoubleValue(sionnaCacheJitterFraction));
    }

    g_nodeNames.push_back("cp");
    for (uint32_t index = 1; index <= uavCount; ++index)
    {
        g_nodeNames.push_back("uav" + std::to_string(index));
    }
    g_nodeCounters.resize(g_nodeNames.size());
    g_wifiQueues.resize(g_nodeNames.size());
    g_positions.resize(g_nodeNames.size());
    g_events.open(eventCsv, std::ios::out | std::ios::trunc);
    g_phaseFile = phaseFile;
    g_events << "time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details\n";
    PcapHelper pcapHelper;
    g_radioPcap = pcapHelper.CreateFile(radioPcap, std::ios::out, PcapHelper::DLT_EN10MB);

    Ptr<Node> ghostGcs = CreateObject<Node>();
    Ptr<Node> commandPost = CreateObject<Node>();
    NodeContainer uavs;
    uavs.Create(uavCount);
    NodeContainer radioNodes;
    radioNodes.Add(commandPost);
    radioNodes.Add(uavs);

    std::vector<Ptr<MobilityModel>> mobility;
    Ptr<bas::MeasuredMobility> cpMobility = CreateObject<bas::MeasuredMobility>();
    commandPost->AggregateObject(cpMobility);
    mobility.push_back(cpMobility);
    std::vector<Ptr<MobilityModel>> uavMobility;
    for (uint32_t index = 0; index < uavCount; ++index)
    {
        Ptr<bas::MeasuredMobility> model = CreateObject<bas::MeasuredMobility>();
        uavs.Get(index)->AggregateObject(model);
        mobility.push_back(model);
        uavMobility.push_back(model);
    }

    CsmaHelper ingress;
    ingress.SetChannelAttribute("DataRate", StringValue("1Gbps"));
    ingress.SetChannelAttribute("Delay", StringValue("10us"));
    NetDeviceContainer ingressDevices = ingress.Install(NodeContainer(ghostGcs, commandPost));
    Names::Add("tap_ingress_segment", ingressDevices.Get(0)->GetChannel());

    Ptr<MultiModelSpectrumChannel> channel = CreateObject<MultiModelSpectrumChannel>();
    channel->SetPropagationDelayModel(CreateObject<ConstantSpeedPropagationDelayModel>());
    if (g_faultProfile != "none")
    {
        // This contributes only declared extra pair attenuation. Native Friis
        // remains the single base path-loss model for every transmission.
        auto extraLoss = CreateObject<MatrixPropagationLossModel>();
        extraLoss->SetDefaultLoss(0.0);
        channel->AddPropagationLossModel(extraLoss);
        Simulator::ScheduleNow(&PollFaultMatrix, extraLoss, cpMobility, uavMobility);
    }
    Ptr<SionnaRtSpectrumPropagationLossModel> sionna;
    Ptr<bas::SourcePropagation> sourceRouter;
    const bool mergeSionnaShapes = true;
    SionnaRtChannelModel::RtPathSolverConfig solver;
    if (requiresRt)
    {
        sionna = CreateObject<MeasuredSpectrumPropagation>();
        sionna->SetChannelModelAttribute("Frequency", DoubleValue(carrierHz));
        sionna->SetChannelModelAttribute("SamplesPerSource", UintegerValue(sionnaSamplesPerSource));
        sionna->SetChannelModelAttribute("MaxNumPathsPerSource", UintegerValue(sionnaMaxNumPathsPerSource));
        UintegerValue observedSamples, observedPaths;
        sionna->GetChannelModel()->GetAttribute("SamplesPerSource", observedSamples);
        sionna->GetChannelModel()->GetAttribute("MaxNumPathsPerSource", observedPaths);
        g_sionnaSamplesPerSource = observedSamples.Get();
        g_sionnaMaxNumPathsPerSource = observedPaths.Get();
        sionna->SetChannelModelAttribute("Scenario", StringValue(scene));
        sionna->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(mergeSionnaShapes));
        sionna->SetChannelModelAttribute("MaxNumberOfPaths",
                                         DoubleValue(static_cast<double>(sionnaMaxNumberOfPaths)));
        solver.maxDepth = sionnaMaxDepth;
        solver.los = sionnaLos;
        solver.specularReflection = sionnaSpecularReflection;
        solver.diffuseReflection = sionnaDiffuseReflection;
        solver.diffraction = sionnaDiffraction;
        solver.edgeDiffraction = sionnaEdgeDiffraction;
        solver.refraction = sionnaRefraction;
        solver.syntheticArray = sionnaSyntheticArray;
        solver.seed = sionnaSeed;
        sionna->SetRtPathSolverConfig(solver);
    }
    if (geometryProfile)
    {
        g_geometry=CreateObject<bas::GeometryLogDistance>();
        g_geometry->Load(geometryConfig,carrierHz,geometryCsv);
        g_geometry->Bind(mobility);
        channel->AddPropagationLossModel(g_geometry);
        LogEvent("geometry_propagation_binding", "ns3", "", 0, 1,
                 "scalar=bas::GeometryLogDistance;native=ns3::LogDistancePropagationLossModel;"
                 "condition=ns3::BuildingsChannelConditionModel;no_spectrum_base_loss=true");
    }
    else if (sourceConfig.empty() && g_propagationProfile == "sionna")
        channel->AddPhasedArraySpectrumPropagationLossModel(sionna);
    else
    {
        auto& router = sourceRouter;
        if (sourceConfig.empty()) { router=CreateObject<bas::SourcePropagation>(); router->reference=sionna; }
        else router = bas::InstallSpectrumSources(sionna, channel, sourceConfig, scene,
            solver, sionnaMaxNumberOfPaths, g_propagationProfile,
            [](const std::string& event, const std::string& id, double value,
               const std::string& details) { LogEvent(event, id, "", 0, value, details); },
            sionnaReceiverBatchMode != "pairwise", []() { return ReadPhase(); });
        router->profile=g_propagationProfile;
        channel->AddPhasedArraySpectrumPropagationLossModel(router);
        LogEvent("source_propagation_dispatch", "ns3", "", 0, router->sources.size(),
                 "wrapper=bas::SourcePropagation;selected_profile=" + g_propagationProfile + ";one_model_per_signal=true");
    }
    if (!geometryProfile) g_phasedArraySpectrumPropagationModels.push_back(g_propagationProfile == "sionna"
        ? sionna->GetInstanceTypeId().GetName() : (g_propagationProfile == "friis"
            ? "ns3::FriisSpectrumPropagationLossModel" : "bas::ExplicitNativeSionnaFriisSelection"));
    LogEvent("rx_psd_model_binding",
             "ns3",
             "",
             0,
             static_cast<double>(g_phasedArraySpectrumPropagationModels.size()),
             "channel=ns3::MultiModelSpectrumChannel;sole_model=" +
                 (geometryProfile ? std::string("bas::GeometryLogDistance") :
                  g_phasedArraySpectrumPropagationModels.front()) + ";effect=received_psd");

    NetDeviceContainer radioDevices;
    if (radioBackend == "aloha")
    {
        SpectrumValue5MhzFactory spectrum;
        Ptr<SpectrumValue> txPsd = spectrum.CreateTxPowerSpectralDensity(txPowerW, 1);
        Ptr<SpectrumValue> noisePsd = spectrum.CreateConstant(1.381e-23 * 290.0);
        AdhocAlohaNoackIdealPhyHelper radio;
        radio.SetChannel(channel);
        radio.SetTxPowerSpectralDensity(txPsd);
        radio.SetNoisePowerSpectralDensity(noisePsd);
        radio.SetPhyAttribute("Rate", DataRateValue(DataRate(phyRateBps)));
        radioDevices = radio.Install(radioNodes);

        for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
        {
            Ptr<AlohaNoackNetDevice> device =
                DynamicCast<AlohaNoackNetDevice>(radioDevices.Get(index));
            Ptr<HalfDuplexIdealPhy> phy = device->GetPhy()->GetObject<HalfDuplexIdealPhy>();
            PointerValue queueValue;
            device->GetAttribute("Queue", queueValue);
            Ptr<Queue<Packet>> queue = queueValue.Get<Queue<Packet>>();
            NS_ABORT_MSG_IF(!queue,
                            "Aloha device queue is unavailable through its public Queue attribute");
            g_queues.push_back(queue);
            phy->SetAntenna(CreateAntenna());
            device->TraceConnectWithoutContext("MacTx", MakeBoundCallback(&MacTx, index));
            device->TraceConnectWithoutContext("MacTxDrop", MakeBoundCallback(&MacTxDrop, index));
            queue->TraceConnectWithoutContext("Enqueue", MakeBoundCallback(&QueueEnqueue, index));
            queue->TraceConnectWithoutContext("Dequeue", MakeBoundCallback(&QueueDequeue, index));
            queue->TraceConnectWithoutContext("Drop", MakeBoundCallback(&QueueDrop, index));
            phy->TraceConnectWithoutContext("TxStart", MakeBoundCallback(&PhyTxStart, index));
            phy->TraceConnectWithoutContext("TxEnd", MakeBoundCallback(&PhyTxEnd, index));
            phy->TraceConnectWithoutContext("RxEndOk", MakeBoundCallback(&PhyRxOk, index));
            phy->TraceConnectWithoutContext("RxStart", MakeBoundCallback(&PhyRxStart, index));
            phy->TraceConnectWithoutContext("RxAbort", MakeBoundCallback(&PhyRxAbort, index));
            phy->TraceConnectWithoutContext("RxEndError", MakeBoundCallback(&PhyRxError, index));
        }
    }
    else
    {
        SpectrumWifiPhyHelper wifiPhy;
        wifiPhy.SetChannel(channel);
        wifiPhy.SetErrorRateModel("ns3::NistErrorRateModel");
        const double txPowerDbm = 10.0 * std::log10(txPowerW * 1000.0);
        wifiPhy.Set("TxPowerStart", DoubleValue(txPowerDbm));
        wifiPhy.Set("TxPowerEnd", DoubleValue(txPowerDbm));
        wifiPhy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
        std::ostringstream channelSettings;
        channelSettings << "{" << wifiChannelNumber << ", " << wifiChannelWidthMhz
                        << ", BAND_2_4GHZ, 0}";
        wifiPhy.Set("ChannelSettings", StringValue(channelSettings.str()));

        WifiHelper wifi;
        wifi.SetStandard(WIFI_STANDARD_80211n);
        if (!wifiAggregation)
        {
            // Native 802.11n on 2.4 GHz has neither the VHT nor 6 GHz
            // mandatory-BA exceptions in HtFrameExchangeManager::NeedSetupBlockAck.
            // No A-MPDU and a zero threshold prevent BA agreement establishment.
            for (const std::string ac : {"BE", "BK", "VI", "VO"})
            {
                Config::SetDefault("ns3::WifiMac::" + ac + "_MaxAmpduSize", UintegerValue(0));
                Config::SetDefault("ns3::WifiMac::" + ac + "_MaxAmsduSize", UintegerValue(0));
                Config::SetDefault("ns3::WifiMac::" + ac + "_BlockAckThreshold", UintegerValue(0));
            }
        }
        wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                     "DataMode",
                                     StringValue(wifiDataMode),
                                     "ControlMode",
                                     StringValue(wifiControlMode),
                                     "NonUnicastMode",
                                     StringValue(wifiControlMode));
        WifiMacHelper wifiMac;
        const Ssid ssid(wifiSsid);
        if (g_wifiTopology == "adhoc")
        {
            wifiMac.SetType("ns3::AdhocWifiMac", "QosSupported", BooleanValue(true),
                            "Ssid", SsidValue(ssid));
            radioDevices.Add(wifi.Install(wifiPhy, wifiMac, radioNodes));
        }
        else
        {
            wifiMac.SetType("ns3::ApWifiMac", "QosSupported", BooleanValue(true),
                            "Ssid", SsidValue(ssid));
            radioDevices.Add(wifi.Install(wifiPhy, wifiMac, NodeContainer(commandPost)));
            wifiMac.SetType("ns3::StaWifiMac", "QosSupported", BooleanValue(true),
                            "Ssid", SsidValue(ssid));
            radioDevices.Add(wifi.Install(wifiPhy, wifiMac, uavs));
        }
        wifiPhy.EnablePcap(radioPcap + ".radiotap", radioDevices);

        Ptr<WifiNetDevice> referenceDevice = DynamicCast<WifiNetDevice>(radioDevices.Get(0));
        NS_ABORT_MSG_IF(!referenceDevice, "expected reference WifiNetDevice");
        g_wifiActualChannelNumber = referenceDevice->GetPhy()->GetChannelNumber();
        g_wifiActualCenterFrequencyHz = MHzToHz(referenceDevice->GetPhy()->GetFrequency());
        NS_ABORT_MSG_IF(g_wifiActualChannelNumber != wifiChannelNumber,
                        "resolved Wi-Fi channel differs from product configuration");
        NS_ABORT_MSG_IF(std::abs(g_wifiActualCenterFrequencyHz - carrierHz) > 0.5,
                        "Sionna carrier differs from the resolved Wi-Fi center frequency: "
                            << carrierHz << " versus " << g_wifiActualCenterFrequencyHz);

        for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
        {
            AttachWifiAntenna(radioDevices.Get(index));
            Ptr<WifiNetDevice> device = DynamicCast<WifiNetDevice>(radioDevices.Get(index));
            NS_ABORT_MSG_IF(!device, "expected WifiNetDevice");
            for (AcIndex ac : {AC_BE,AC_BK,AC_VI,AC_VO}) {
                auto txop=device->GetMac()->GetQosTxop(ac);
                if (!txop) continue;
                const auto maxAmpdu = device->GetMac()->GetMaxAmpduSize(ac);
                const auto maxAmsdu = device->GetMac()->GetMaxAmsduSize(ac);
                const auto blockAckThreshold = txop->GetBlockAckThreshold();
                g_wifiAggregationEnabled |= maxAmpdu > 0 || maxAmsdu > 0;
                NS_ABORT_MSG_IF(!wifiAggregation &&
                                    (maxAmpdu != 0 || maxAmsdu != 0 || blockAckThreshold != 0 ||
                                     device->GetVhtConfiguration()),
                                "native aggregation/BlockAck disable setting was not applied");
                LogEvent("wifi_aggregation_config", g_nodeNames.at(index), "", 0, uint32_t(ac),
                         "source=ns3::WifiMac+QosTxop;max_ampdu_bytes=" + std::to_string(maxAmpdu) +
                         ";max_amsdu_bytes=" + std::to_string(maxAmsdu) +
                         ";block_ack_threshold_packets=" + std::to_string(blockAckThreshold));
                txop->SetAttribute("UseExplicitBarAfterMissedBlockAck",
                                   BooleanValue(g_wifiExplicitBarAfterMissedBlockAck));
                BooleanValue effectiveExplicitBar;
                txop->GetAttribute("UseExplicitBarAfterMissedBlockAck", effectiveExplicitBar);
                NS_ABORT_MSG_IF(effectiveExplicitBar.Get() != g_wifiExplicitBarAfterMissedBlockAck,
                                "upstream QosTxop recovery setting was not applied");
                LogEvent("wifi_qos_config", g_nodeNames.at(index), "", 0, uint32_t(ac),
                         "source=ns3::QosTxop;attribute=UseExplicitBarAfterMissedBlockAck;effective=" +
                             std::string(effectiveExplicitBar.Get() ? "true" : "false"));
                auto queue=txop->GetWifiMacQueue();
                g_wifiQueues.at(index).push_back(queue);
                for (const auto& trace : {std::pair{"Enqueue","enqueue"}, {"Dequeue","dequeue"}, {"Drop","drop"}})
                    NS_ABORT_MSG_IF(!queue->TraceConnectWithoutContext(trace.first,
                                        MakeBoundCallback(&WifiQueueObservation, index, uint32_t(ac),
                                                          queue, std::string(trace.second))),
                                    "native Wi-Fi queue trace is unavailable");
            }
            NS_ABORT_MSG_IF(g_wifiQueues.at(index).size() != 4,
                            "native Wi-Fi queue observation requires all four ACs");
            device->GetPhy()->TraceConnectWithoutContext(
                "MonitorSnifferRx", MakeBoundCallback(&WifiMonitorRx, index));
            device->GetPhy()->TraceConnectWithoutContext(
                "SignalArrival", MakeBoundCallback(&WifiSignalArrival, index));
            device->GetMac()->TraceConnectWithoutContext("MacTx",
                                                         MakeBoundCallback(&MacTx, index));
            device->GetMac()->TraceConnectWithoutContext("MacTxDrop",
                                                         MakeBoundCallback(&MacTxDrop, index));
            device->GetPhy()->TraceConnectWithoutContext("PhyTxBegin",
                                                         MakeBoundCallback(&WifiPhyTxStart, index));
            device->GetPhy()->TraceConnectWithoutContext("PhyTxEnd",
                                                         MakeBoundCallback(&PhyTxEnd, index));
            device->GetPhy()->TraceConnectWithoutContext("PhyRxBegin",
                                                         MakeBoundCallback(&WifiPhyRxStart, index));
            device->GetPhy()->TraceConnectWithoutContext("PhyRxEnd",
                                                         MakeBoundCallback(&WifiPhyRxEnd, index));
            device->GetPhy()->TraceConnectWithoutContext("PhyRxDrop",
                                                         MakeBoundCallback(&WifiPhyRxDrop, index));
            device->GetPhy()->GetState()->TraceConnectWithoutContext(
                "RxOk",
                MakeBoundCallback(&WifiPhyStateRxOk, index));
            device->GetPhy()->GetState()->TraceConnectWithoutContext(
                "RxError",
                MakeBoundCallback(&WifiPhyStateRxError, index));
            device->GetRemoteStationManager()->TraceConnectWithoutContext(
                "MacTxDataFailed",
                MakeBoundCallback(&WifiDataRetry, index));
            if (index > 0 && g_wifiTopology == "bss")
            {
                Ptr<StaWifiMac> station = DynamicCast<StaWifiMac>(device->GetMac());
                NS_ABORT_MSG_IF(!station, "expected StaWifiMac on UAV radio");
                g_wifiStations.push_back(station);
                station->TraceConnectWithoutContext("Assoc",
                                                    MakeBoundCallback(&WifiAssociated, index));
                station->TraceConnectWithoutContext("DeAssoc",
                                                    MakeBoundCallback(&WifiDeassociated, index));
            }
        }
    }

    ingressDevices.Get(1)->SetAddress(Mac48Address("02:71:00:00:00:01"));
    const Mac48Address cpRadioMac("02:71:ff:00:00:01");
    if (radioBackend == "wifi")
    {
        SetWifiAddress(radioDevices.Get(0), cpRadioMac);
    }
    else
    {
        radioDevices.Get(0)->SetAddress(cpRadioMac);
    }
    InternetStackHelper internet;
    internet.Install(commandPost);
    const uint32_t ingressInterface =
        AddRouterInterface(commandPost, ingressDevices.Get(1), "10.71.0.1");
    const uint32_t radioInterface =
        AddRouterInterface(commandPost, radioDevices.Get(0), "10.71.1.1");
    AddPermanentArp(commandPost,
                    ingressDevices.Get(1),
                    "10.71.0.10",
                    Mac48Address("02:71:00:00:10:10"));
    for (uint32_t index = 1; index <= uavCount; ++index)
    {
        std::ostringstream endpointAddress;
        endpointAddress << "10.71." << index << ".10";
        std::ostringstream endpointMac;
        endpointMac << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << index
                    << ":00:10:10";
        const Mac48Address uavRadioMac(endpointMac.str().c_str());
        if (radioBackend == "wifi")
        {
            SetWifiAddress(radioDevices.Get(index), uavRadioMac);
        }
        else
        {
            radioDevices.Get(index)->SetAddress(uavRadioMac);
        }
        AddPermanentArp(commandPost,
                        radioDevices.Get(0),
                        endpointAddress.str(),
                        Mac48Address(endpointMac.str().c_str()));
    }
    Ipv4StaticRoutingHelper routingHelper;
    Ptr<Ipv4StaticRouting> routing =
        routingHelper.GetStaticRouting(commandPost->GetObject<Ipv4>());
    routing->AddHostRouteTo(Ipv4Address("10.71.0.10"), ingressInterface);
    for (uint32_t index = 1; index <= uavCount; ++index)
    {
        std::ostringstream endpointAddress;
        endpointAddress << "10.71." << index << ".10";
        routing->AddHostRouteTo(Ipv4Address(endpointAddress.str().c_str()), radioInterface);
    }
    routing->AddMulticastRoute(Ipv4Address::GetAny(),
                               Ipv4Address("239.71.0.1"),
                               ingressInterface,
                               std::vector<uint32_t>{radioInterface});

    TapBridgeHelper tap;
    std::vector<Ptr<TapBridge>> tapDevices;
    tap.SetAttribute("SynchronousRead", BooleanValue(g_executionMode == "lockstep"));
    tap.SetAttribute("Mode", StringValue("UseBridge"));
    tap.SetAttribute("DeviceName", StringValue(tapGcs));
    tapDevices.push_back(DynamicCast<TapBridge>(tap.Install(ghostGcs, ingressDevices.Get(0))));
    tap.SetAttribute("Mode", StringValue(radioBackend == "wifi" ? "UseLocal" : "UseBridge"));
    for (uint32_t index = 0; index < uavCount; ++index)
    {
        tap.SetAttribute("DeviceName", StringValue(uavTapNames[index]));
        tapDevices.push_back(DynamicCast<TapBridge>(tap.Install(uavs.Get(index), radioDevices.Get(index + 1))));
    }

    LivePositionSource positions(positionFile, mobility);
    positions.SetDeferredApplication(useRtWorkers && sionnaReceiverBatchMode == "full_mesh");
    g_poseSourceForStats = &positions;
    Ptr<SionnaRtChannelModel> originalRtModel = sionna
        ? DynamicCast<SionnaRtChannelModel>(sionna->GetChannelModel()) : nullptr;
    Ptr<bas::NativeRtCache> rtCache;
    std::unique_ptr<bas::NativeRtPool> rtPool;
    for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
        g_wifiRadioIndices[Mac48Address::ConvertFrom(radioDevices.Get(index)->GetAddress())] = index;
    g_runtimeHeartbeat = readyFile + ".heartbeat";
    if (g_executionMode == "realtime") positions.Refresh();
    if (sionna && g_executionMode == "realtime" && radioBackend == "wifi")
    {
        NS_ABORT_MSG_IF(!g_liveStateValid, "Sionna preparation requires an initial valid pose snapshot");
        std::vector<Ptr<const MobilityModel>> cohort;
        std::vector<Ptr<const PhasedArrayModel>> arrays;
        for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
        {
            cohort.push_back(mobility.at(index));
            auto device = DynamicCast<WifiNetDevice>(radioDevices.Get(index));
            auto phy = DynamicCast<SpectrumWifiPhy>(device->GetPhy());
            arrays.push_back(phy->GetAntenna()->GetObject<PhasedArrayModel>());
        }
        auto model = DynamicCast<SionnaRtChannelModel>(sionna->GetChannelModel());
        if (sionnaReceiverBatchMode != "pairwise")
        {
            model->SetReceiverBatchEndpoints(cohort, arrays);
            model->SetAttribute("ReceiverBatchEnabled", BooleanValue(true));
            NS_ABORT_MSG_IF(!model->GetReceiverBatchEnabled(), "receiver_batch mode was not applied");
            LogEvent("sionna_receiver_batch_config", "ns3", "", 0, cohort.size(),
                     "mode=" + sionnaReceiverBatchMode + ";path_capacity=nonself_receiver_count_times_per_link_cap;samples_per_src_unchanged=true");
        }
        // Readiness must not precede cold PathSolver/JIT work. Populate the
        // actual production cache before the realtime epoch; cache age and
        // displacement checks remain unchanged during the experiment.
        const auto started = std::chrono::steady_clock::now();
        uint64_t links = 0;
        if (useRtWorkers)
        {
            using namespace pybind11::literals;
            auto pathlib = py::module_::import("pathlib");
            auto json = py::module_::import("json");
            auto configPath = pathlib.attr("Path")(sionnaWorkerConfig);
            NS_ABORT_MSG_IF(configPath.attr("exists")().cast<bool>(), "Worker config must be new for this run");
            BooleanValue normalizeDelays;
            model->GetAttribute("NormalizeDelays", normalizeDelays);
            py::list nodeIds;
            std::vector<bas::NativeRtCache::Endpoint> endpoints;
            for (size_t i = 0; i < cohort.size(); ++i)
            {
                nodeIds.append(py::dict("id"_a=g_nodeNames.at(i)));
                endpoints.push_back({cohort[i], arrays[i]});
            }
            const auto sceneBytes = pathlib.attr("Path")(scene).attr("read_bytes")();
            auto config = py::dict("schema"_a=1, "root"_a=projectRoot,
                "scene"_a=py::dict("path"_a=scene, "sha256"_a=py::module_::import("hashlib").attr("sha256")(sceneBytes).attr("hexdigest")()),
                "normalize_delays"_a=normalizeDelays.Get(), "cohort"_a=nodeIds,
                "radio"_a=py::dict("frequency_hz"_a=carrierHz, "samples_per_source"_a=g_sionnaSamplesPerSource,
                    "max_paths_per_source"_a=g_sionnaMaxNumPathsPerSource,
                    "max_number_of_paths"_a=sionnaMaxNumberOfPaths, "merge_shapes"_a=mergeSionnaShapes,
                    "solver"_a=py::dict("max_depth"_a=solver.maxDepth, "seed"_a=solver.seed,
                        "los"_a=solver.los, "specular_reflection"_a=solver.specularReflection,
                        "diffuse_reflection"_a=solver.diffuseReflection, "refraction"_a=solver.refraction,
                        "diffraction"_a=solver.diffraction, "edge_diffraction"_a=solver.edgeDiffraction,
                        "synthetic_array"_a=solver.syntheticArray)));
            if (sionnaReceiverBatchMode == "full_mesh") config["desired_mode"] = "full_mesh";
            const auto sourceBindings = sourceRouter ? sourceRouter->GetSourceBindings()
                : std::vector<bas::SourceBinding>{};
            if (!sourceBindings.empty())
            {
                py::list profiles;
                for (const auto& binding : sourceBindings)
                    profiles.append(py::dict("id"_a="source_segment_"+std::to_string(binding.segmentIndex),
                        "frequency_hz"_a=binding.centerHz,"pattern"_a=binding.pattern));
                config["source_profiles"] = profiles;
            }
            auto contract = py::module_::import("scripts.product.native_rt_workers");
            config = contract.attr("validate_config")(config).cast<py::dict>();
            configPath.attr("write_text")(json.attr("dumps")(config, "indent"_a=2, "allow_nan"_a=false), "encoding"_a="utf-8");
            bas::NativeRtCache::Limits limits;
            limits.maximumAge = Seconds(channelStateMaxAgeS);
            limits.displacementM = updateDistanceThresholdM;
            rtCache = CreateObject<bas::NativeRtCache>(carrierHz, endpoints, limits);
            rtPool = std::make_unique<bas::NativeRtPool>(rtCache, endpoints,
                [&positions]() { return positions.Snapshot(); },
                [](const std::string& event, const std::string& id, double value,
                   const std::string& details) { LogEvent(event, id, "", 0, value, details); },
                sionnaReceiverBatchMode == "full_mesh" ? bas::NativeRtPool::CommitSnapshot(
                    [&positions](const std::vector<bas::MeasuredState>& state, const std::function<void()>& publish) {
                        positions.Commit(state,publish);
                    })
                    : bas::NativeRtPool::CommitSnapshot{});
            for (const auto& binding : sourceBindings)
            {
                // One physical emitter owns one STAR cache and one spectrum
                // wrapper. Sharing wrappers by carrier would overwrite other
                // emitters' cache providers when these bindings are installed.
                const bas::NativeRtCache::Endpoint emitter{binding.phy->GetMobility(),binding.array};
                auto cache=CreateObject<bas::NativeRtCache>(binding.centerHz,endpoints,emitter,limits);
                auto propagation=CreateObject<SionnaRtSpectrumPropagationLossModel>();
                propagation->SetChannelModel(cache);
                sourceRouter->sources.at(binding.phy)=propagation;
                bas::NativeRtPool::SourceBinding source;
                source.id=binding.id+":"+std::to_string(binding.segmentIndex);
                source.profileId="source_segment_"+std::to_string(binding.segmentIndex);
                source.cache=cache;
                source.endpoints=endpoints;
                source.endpoints.push_back(emitter);
                source.emitterStatic.position=binding.positionM;
                source.emitterStatic.velocity=binding.velocityMps;
                source.emitterStatic.orientation=binding.orientationRad;
                source.required=[router=sourceRouter,index=binding.segmentIndex,phy=binding.phy](int64_t lookaheadNs) {
                    const auto current=router->GetSourceBindings();
                    if (index>=current.size() || current[index].phy!=phy)
                        throw std::runtime_error("Native RT source binding disappeared before pool shutdown");
                    return current[index].RequiredAt(Simulator::Now(),NanoSeconds(lookaheadNs));
                };
                rtPool->AddSourceBinding(std::move(source));
            }
            try
            {
                rtPool->Start(projectRoot, sionnaWorkerBinary, sionnaWorkerConfig,
                              (std::filesystem::path(sionnaWorkerConfig).parent_path()/"native_rt_workers").string(),
                              sionnaWorkerCount);
                rtPool->Warm([&positions]() {
                    if (!positions.Refresh() || !g_liveStateValid)
                        throw std::runtime_error("Native RT worker warmup lost the measured pose source");
                });
                sionna->SetChannelModel(rtCache);
            }
            catch (const std::exception& error)
            {
                g_stopReason = "rt_worker_preparation_error";
                std::cerr << error.what() << std::endl;
                try { rtPool->Close(); g_sionnaRtWorkersJson = rtPool->SnapshotJson(); }
                catch (const std::exception& cleanupError) { std::cerr << cleanupError.what() << std::endl; }
                Simulator::Destroy();
                if (g_logPhase) g_logPhase->Close();
                WriteStats(statsFile);
                g_poseSourceForStats = nullptr;
                FlushEvents();
                return 6;
            }
            links = cohort.size() * (cohort.size() - 1) / 2;
        }
        else
            for (size_t tx = 0; tx < cohort.size(); ++tx)
                for (size_t rx = tx + 1; rx < cohort.size(); ++rx)
                {
                    model->GetChannel(cohort[tx], cohort[rx], arrays[tx], arrays[rx]);
                    ++links;
                }
        LogEvent("sionna_radio_warmup", "ns3", "", 0,
                 std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count(),
                 "scope=actual_radio_channels;mode=" + sionnaReceiverBatchMode +
                 ";receiver_links=" + std::to_string(links) +
                 ";clock=host_monotonic_seconds;before_realtime_epoch=true");
        positions.Refresh();
        NS_ABORT_MSG_IF(!g_liveStateValid, "Sionna radio warmup ended without a current pose snapshot");
    }
    if (sourceRouter && !sourceRouter->sources.empty())
    {
        // Prime the actual emitter channels, not a separate diagnostic solver.
        // Cold scene/JIT preparation happens before the wall-clock epoch and
        // readiness. No waveform is transmitted and no interval is shortened.
        NS_ABORT_MSG_IF(g_executionMode != "realtime" || !g_liveStateValid,
                        "source RT warmup requires current realtime poses");
        const auto started = std::chrono::steady_clock::now();
        uint64_t links = 0;
        for (const auto& [sourcePhy, propagation] : sourceRouter->sources)
        {
            auto model = propagation->GetChannelModel();
            auto sourceArray = sourcePhy->GetAntenna()->GetObject<PhasedArrayModel>();
            NS_ABORT_MSG_IF(!sourceArray, "source has no phased array");
            for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
            {
                auto device = DynamicCast<WifiNetDevice>(radioDevices.Get(index));
                NS_ABORT_MSG_IF(!device, "source RT warmup requires the supported Wi-Fi radios");
                auto phy = DynamicCast<SpectrumWifiPhy>(device->GetPhy());
                auto receiverArray = phy->GetAntenna()->GetObject<PhasedArrayModel>();
                NS_ABORT_MSG_IF(!receiverArray, "receiver has no phased array");
                model->GetChannel(sourcePhy->GetMobility(), mobility.at(index), sourceArray, receiverArray);
                ++links;
            }
        }
        const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        LogEvent("source_rt_warmup", "ns3", "", 0, elapsed,
                 "scope=actual_source_channels;receiver_links=" + std::to_string(links) +
                 ";clock=host_monotonic_seconds;before_realtime_epoch=true");
        positions.Refresh(); // A cold solve may outlive the allowed pose age.
        NS_ABORT_MSG_IF(!g_liveStateValid, "source warmup ended without a current pose snapshot");
    }
    NativeRuntimeSampler metrics(sionna ? sionna->GetChannelModel() : nullptr, cpMobility, uavMobility);
    g_runtimeSampler = &metrics;
    // Preparation reads do not schedule the live timer. Observe at native zero
    // before the first RT dispatch: ConfigStore and Run initialization may have
    // outlived the last pre-Run source publication. Preserve measured epochs.
    if (g_executionMode == "realtime") Simulator::ScheduleNow(&LivePositionSource::Poll, &positions);
    if (rtPool) Simulator::ScheduleNow(&bas::NativeRtPool::Poll, rtPool.get());
    Simulator::ScheduleNow(&NativeRuntimeSampler::PollLag, &metrics);
    Simulator::Schedule(Seconds(1), &NativeRuntimeSampler::PollSionnaPaths, &metrics);
    Simulator::ScheduleNow(&SampleQueues);
    Simulator::ScheduleNow(&PollSignal, radioDevices);
    if (radioBackend == "wifi")
    {
        Simulator::ScheduleNow(&WriteReadyWhenWifiAssociated, readyFile);
    }
    else
    {
        Simulator::Schedule(MilliSeconds(250), &WriteReady, readyFile);
    }
    if (g_executionMode == "realtime") Simulator::Stop(Seconds(duration));

    if (!attributeDump.empty())
    {
        Config::SetDefault("ns3::ConfigStore::Filename", StringValue(attributeDump));
        Config::SetDefault("ns3::ConfigStore::Mode", StringValue("Save"));
        Config::SetDefault("ns3::ConfigStore::FileFormat", StringValue("RawText"));
        {
            ConfigStore attributes;
            attributes.ConfigureDefaults(); // Includes registered GlobalValues.
            attributes.ConfigureAttributes();
        } // Close the upstream writer before checking and starting the runtime.
        NS_ABORT_MSG_IF(!std::filesystem::exists(attributeDump) ||
                        std::filesystem::file_size(attributeDump) == 0,
                        "native ConfigStore failed to write " << attributeDump);
        LogEvent("native_config_dump", "ns3", "", 0,
                 static_cast<double>(std::filesystem::file_size(attributeDump)),
                 "provider=ns3::ConfigStore;format=RawText;scope=defaults_globals_attributes;"
                 "phase=topology_constructed_before_run;path=" + attributeDump);
    }

    int result = 0;
    try
    {
        if (g_executionMode == "lockstep")
            g_lockstep = std::make_unique<bas::Lockstep>(clockFile, g_stepTimeoutS, g_couplingStepMs*1000000LL);
        g_radioEpochNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        if (g_lockstep) {
            while (!g_stopRequested && g_stopReason=="duration" && Simulator::Now()<Seconds(duration)) {
                g_lockstep->Begin(Simulator::Now().GetNanoSeconds(), g_runtimeOperational);
                // The completed physics barrier owns this measured boundary.
                // Apply it before events at the next radio interval, never midway.
                positions.Poll();
                if (g_stopReason != "duration") break;
                for (uint32_t i = 0; i < tapDevices.size(); ++i) {
                    const auto frames = tapDevices[i]->ReadTap();
                    if (frames) LogEvent("tap_ingress_batch", g_nodeNames.at(i), "", 0,
                                         frames, "boundary_read_complete");
                }
                Simulator::Stop(MilliSeconds(g_couplingStepMs));
                Simulator::Run();
                if (g_stopRequested || g_stopReason!="duration") break;
                g_lockstep->Physics(Simulator::Now().GetNanoSeconds(), g_runtimeOperational);
                ++g_completedSteps;
            }
        } else if (rtPool) {
            // Receiver threads need the interpreter while the native packet
            // path reads only the C++ CIR cache. Callbacks acquire it locally.
            py::gil_scoped_release release;
            Simulator::Run();
        } else Simulator::Run();
    }
    catch (const py::error_already_set& error)
    {
        g_stopReason = "sionna_python_error";
        std::cerr << error.what() << std::endl;
        result = 3;
    }
    catch (const std::exception& error)
    {
        g_stopReason = "runtime_error";
        std::cerr << error.what() << std::endl;
        result = 4;
    }
    if (g_lockstep) {
        g_lockstep->Clock(Simulator::Now().GetNanoSeconds(), "stopped", false);
        g_lockstep.reset();
    }
    std::filesystem::remove(g_runtimeHeartbeat);
    if (g_geometry) g_geometry->Close();
    if (rtPool) {
        try { rtPool->Close(); }
        catch (const std::exception& error) {
            g_stopReason = "rt_worker_shutdown_error";
            std::cerr << error.what() << std::endl;
            result = 4;
        }
        g_sionnaRtWorkersJson = rtPool->SnapshotJson();
    }
    if (sionna && sionnaReceiverBatchMode != "pairwise")
    {
        const auto batch = originalRtModel->GetReceiverBatchStats();
        std::ostringstream observed;
        observed << "{\"enabled\":" << (batch.enabled ? "true" : "false")
                 << ",\"registered_endpoints\":" << batch.registeredEndpoints
                 << ",\"configured_max_paths_per_source\":" << batch.configuredMaxPathsPerSource
                 << ",\"attempted_calls\":" << batch.attemptedCalls
                 << ",\"completed_calls\":" << batch.completedCalls
                 << ",\"generated_links\":" << batch.generatedLinks
                 << ",\"candidate_capacity_reached_calls\":" << batch.candidateCapacityReachedCalls
                 << ",\"last_receiver_count\":" << batch.lastReceiverCount
                 << ",\"last_allocated_max_paths_per_source\":" << batch.lastAllocatedMaxPathsPerSource
                 << ",\"last_candidate_count\":" << batch.lastCandidateCount
                 << ",\"last_candidate_buffer_size\":" << batch.lastCandidateBufferSize
                 << ",\"get_channel_calls\":" << batch.getChannelCalls
                 << ",\"get_channel_cache_hits\":" << batch.getChannelCacheHits
                 << ",\"get_channel_wall_ns\":" << batch.getChannelWallNs
                 << ",\"max_get_channel_wall_ns\":" << batch.maxGetChannelWallNs
                 << ",\"pair_validation_wall_ns\":" << batch.pairValidationWallNs
                 << ",\"max_pair_validation_wall_ns\":" << batch.maxPairValidationWallNs
                 << ",\"scene_prepare_wall_ns\":" << batch.scenePrepareWallNs
                 << ",\"solver_wall_ns\":" << batch.solverWallNs
                 << ",\"extraction_wall_ns\":" << batch.extractionWallNs
                 << ",\"cache_publish_wall_ns\":" << batch.cachePublishWallNs
                 << ",\"batch_wall_ns\":" << batch.batchWallNs
                 << ",\"max_batch_wall_ns\":" << batch.maxBatchWallNs << "}";
        g_sionnaReceiverBatchStatsJson = observed.str();
    }
    Simulator::Destroy();
    if (g_logPhase) g_logPhase->Close();
    WriteStats(statsFile);
    g_poseSourceForStats = nullptr;
    FlushEvents();
    g_events.close();
    g_radioPcap = nullptr;
    if (g_stopReason == "position_tracker_stale" || g_stopReason == "state_coupling_deadline")
    {
        return 5;
    }
    return result;
}
