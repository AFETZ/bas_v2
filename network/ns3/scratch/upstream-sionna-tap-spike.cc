#include "pybind11/embed.h"

#include "ns3/adhoc-aloha-noack-ideal-phy-helper.h"
#include "ns3/aloha-noack-mac-header.h"
#include "ns3/aloha-noack-net-device.h"
#include "ns3/core-module.h"
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
#include "ns3/queue.h"
#include "ns3/realtime-simulator-impl.h"
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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iomanip>
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
double g_channelStateMaxAgeS{2.0};
double g_updateDistanceThresholdM{1.0};
uint64_t g_poseSnapshots{0};
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
std::string g_phaseFile;
std::string g_eventLogging{"batched_trace"};
uint32_t g_flushEveryEvents{256};
uint32_t g_flushMaxDelayMs{25};
uint64_t g_pendingEventWrites{0};
std::chrono::steady_clock::time_point g_lastEventFlush{std::chrono::steady_clock::now()};
std::unordered_map<uint64_t, Time> g_queueEnqueueAt;
std::vector<Ptr<Queue<Packet>>> g_queues;
std::vector<Ptr<StaWifiMac>> g_wifiStations;
std::string g_radioBackend{"aloha"};
std::string g_radioProfile{"generic_native_spectrum_aloha_reference"};
std::string g_neighborDiscoveryMode{"preconfigured_static_neighbors"};
std::string g_radioReason{"upstream_ideal_phy_arp_reentrancy_limit"};
std::string g_solverProfile{"realtime_minimal_solver_profile"};
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

class NativeRuntimeSampler;
NativeRuntimeSampler* g_runtimeSampler{nullptr};

void ObserveReceivedPath(uint32_t nodeIndex, Ptr<const Packet> packet);

std::string
ReadPhase()
{
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
                             event == "radio_queue_enqueue" || event == "radio_queue_dequeue" ||
                             event == "radio_queue_drop";
    if (g_eventLogging == "metrics_only" && packetEvent)
    {
        return;
    }
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
    g_events << std::fixed << std::setprecision(6) << Simulator::Now().GetSeconds() << ',' << wallNs
             << ',' << ReadPhase() << ',' << event << ',' << node << ',' << peer << ',' << bytes
             << ',' << position.x << ',' << position.y << ',' << position.z << ',';
    if (std::isfinite(value))
    {
        g_events << value;
    }
    g_events << ',' << details << '\n';
    ++g_pendingEventWrites;
    const auto now = std::chrono::steady_clock::now();
    if (g_pendingEventWrites >= g_flushEveryEvents ||
        now - g_lastEventFlush >= std::chrono::milliseconds(g_flushMaxDelayMs))
    {
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
    LogEvent("mac_tx",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(),
             PacketTraceDetails(packet));
    WriteRadioPcap(packet);
}

void
MacTxDrop(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).macTxDrop;
    LogEvent("mac_tx_drop", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyTxStart(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyTxStart;
    LogEvent("phy_tx_start", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyTxEnd(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyTxEnd;
    LogEvent("phy_tx_end", g_nodeNames.at(nodeIndex), "", packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(), PacketTraceDetails(packet));
}

void
PhyRxAbort(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxAbort;
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
    LogEvent("radio_queue_dequeue", g_nodeNames.at(nodeIndex), "", packet->GetSize(), residenceMs,
             PacketTraceDetails(packet));
}

void
QueueDrop(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).queueDrop;
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
    LogEvent("phy_rx_ok",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(),
             PacketTraceDetails(packet));
    WriteRadioPcap(packet);
}

void
PhyRxStart(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxStart;
    LogEvent("phy_rx_start",
             g_nodeNames.at(nodeIndex),
             "",
             packet->GetSize(),
             std::numeric_limits<double>::quiet_NaN(),
             PacketTraceDetails(packet));
    if (g_radioBackend == "aloha")
    {
        ObserveReceivedPath(nodeIndex, packet);
    }
}

void
PhyRxError(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxError;
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
    Ptr<Packet> payload = packet->Copy();
    WifiMacHeader header;
    payload->RemoveHeader(header);
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

void
WifiPhyRxEnd(uint32_t nodeIndex, Ptr<const Packet> packet)
{
    ++g_nodeCounters.at(nodeIndex).phyRxEnd;
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
    std::ostringstream details;
    details << PacketTraceDetails(packet) << ";snr_linear=" << std::setprecision(17) << snr
            << ";mode=" << mode << ";preamble=" << preamble
            << ";source=WifiPhyStateHelper.RxOk";
    LogEvent("phy_rx_ok", g_nodeNames.at(nodeIndex), "", packet->GetSize(), snr, details.str());
    WriteRadioPcap(packet);
}

void
WifiPhyStateRxError(uint32_t nodeIndex, Ptr<const Packet> packet, double snr)
{
    ++g_nodeCounters.at(nodeIndex).phyRxError;
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

std::optional<Vector>
ParsePosition(const std::string& json, const std::string& nodeId)
{
    const std::string marker = "\"id\": \"" + nodeId + "\"";
    const std::size_t node = json.find(marker);
    if (node == std::string::npos)
    {
        return std::nullopt;
    }
    const std::size_t objectEnd = json.find('}', node);
    const std::size_t positionKey = json.find("\"position_m\": [", node);
    if (objectEnd == std::string::npos || positionKey == std::string::npos || positionKey > objectEnd)
    {
        return std::nullopt;
    }
    const std::size_t begin = json.find('[', positionKey);
    const std::size_t end = json.find(']', begin);
    if (begin == std::string::npos || end == std::string::npos || end > objectEnd)
    {
        return std::nullopt;
    }
    if (json.substr(node, objectEnd - node).find("\"stale\": true") != std::string::npos)
    {
        return std::nullopt;
    }
    std::string values = json.substr(begin + 1, end - begin - 1);
    for (char& value : values)
    {
        if (value == ',')
        {
            value = ' ';
        }
    }
    std::istringstream input(values);
    Vector position;
    if (!(input >> position.x >> position.y >> position.z))
    {
        return std::nullopt;
    }
    return position;
}

std::optional<double>
ParseScalar(const std::string& json, const std::string& key)
{
    const std::string marker = "\"" + key + "\":";
    const std::size_t position = json.find(marker);
    if (position == std::string::npos)
    {
        return std::nullopt;
    }
    std::istringstream input(json.substr(position + marker.size()));
    double value;
    if (!(input >> value))
    {
        return std::nullopt;
    }
    return value;
}

std::string
Join(const std::vector<std::string>& values, char separator)
{
    std::ostringstream output;
    for (std::size_t index = 0; index < values.size(); ++index)
    {
        if (index)
        {
            output << separator;
        }
        output << values[index];
    }
    return output.str();
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
    LivePositionSource(std::string path, std::vector<Ptr<MobilityModel>> mobility, Time timeout)
        : m_path(std::move(path)),
          m_mobility(std::move(mobility)),
          m_timeout(timeout),
          m_lastFresh(std::chrono::steady_clock::now())
    {
    }

    void Poll()
    {
        const auto now = std::chrono::steady_clock::now();
        std::ifstream input(m_path);
        std::ostringstream buffer;
        buffer << input.rdbuf();
        const std::string json = buffer.str();
        const auto sourceTime = ParseScalar(json, "time_s");
        const bool live = json.find("\"source\": \"ros_odometry\"") != std::string::npos;
        const bool sourceAdvanced = sourceTime &&
                                    (!m_lastSourceTime || *sourceTime > *m_lastSourceTime);
        bool invalidSnapshot = !input.is_open();
        if (sourceAdvanced)
        {
            std::vector<Vector> snapshot;
            std::vector<std::string> missing;
            for (const std::string& name : g_nodeNames)
            {
                const auto position = ParsePosition(json, name);
                if (position)
                {
                    snapshot.push_back(*position);
                }
                else
                {
                    missing.push_back(name);
                }
            }
            if (input.is_open() && live && sourceTime && missing.empty() &&
                snapshot.size() == m_mobility.size())
            {
                for (std::size_t index = 0; index < snapshot.size(); ++index)
                {
                    g_positions[index] = snapshot[index];
                    m_mobility[index]->SetPosition(snapshot[index]);
                }
                m_lastSourceTime = sourceTime;
                m_lastFresh = now;
                m_invalidSince.reset();
                ++g_poseSnapshots;
                const double wallSeconds = std::chrono::duration<double>(
                                               std::chrono::system_clock::now().time_since_epoch())
                                               .count();
                const double ageMs = (wallSeconds - *sourceTime) * 1000.0;
                for (const std::string& name : g_nodeNames)
                {
                    LogEvent("live_pose", name, "", 0, ageMs, "atomic_snapshot_age_ms");
                }
            }
            else if (!missing.empty())
            {
                invalidSnapshot = true;
                LogEvent("position_snapshot_rejected", "tracker", "", 0,
                         std::numeric_limits<double>::quiet_NaN(), Join(missing, ';'));
            }
            else
            {
                invalidSnapshot = true;
                LogEvent("position_snapshot_rejected", "tracker", "", 0,
                         std::numeric_limits<double>::quiet_NaN(), "invalid_atomic_snapshot");
            }
        }
        else if (sourceTime && m_lastSourceTime && *sourceTime < *m_lastSourceTime)
        {
            invalidSnapshot = true;
            LogEvent("position_snapshot_rejected", "tracker", "", 0,
                     std::numeric_limits<double>::quiet_NaN(), "non_monotonic_source_time");
        }

        if (invalidSnapshot && !m_invalidSince)
        {
            m_invalidSince = now;
        }
        const auto timeout = std::chrono::nanoseconds(m_timeout.GetNanoSeconds());
        const bool invalidTimedOut = m_invalidSince && now - *m_invalidSince > timeout;
        const bool trackerStopped = m_lastSourceTime && now - m_lastFresh > timeout;
        if (invalidTimedOut || trackerStopped)
        {
            ++g_stalePoseSamples;
            g_stopReason = "position_tracker_stale";
            LogEvent("fail_closed", "tracker", "", 0);
            Simulator::Stop();
            return;
        }
        Simulator::Schedule(MilliSeconds(100), &LivePositionSource::Poll, this);
    }

  private:
    std::string m_path;
    std::vector<Ptr<MobilityModel>> m_mobility;
    Time m_timeout;
    std::optional<double> m_lastSourceTime;
    std::chrono::steady_clock::time_point m_lastFresh;
    std::optional<std::chrono::steady_clock::time_point> m_invalidSince;
};

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
        Simulator::Schedule(MilliSeconds(500), &NativeRuntimeSampler::PollLag, this);
    }

    void PollSionnaPaths()
    {
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

void
PollSignal()
{
    if (g_stopRequested)
    {
        g_stopReason = "sionna_ns3_process_stopped";
        LogEvent("fail_closed_stop", "sionna_in_process", "", 0);
        Simulator::Stop();
        return;
    }
    Simulator::Schedule(MilliSeconds(100), &PollSignal);
}

void
WriteReady(const std::string& path)
{
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "ready\n";
}

void
WriteReadyWhenWifiAssociated(const std::string& path)
{
    const bool associated = !g_wifiStations.empty() &&
                            std::all_of(g_wifiStations.begin(),
                                        g_wifiStations.end(),
                                        [](const Ptr<StaWifiMac>& mac) {
                                            return mac && mac->IsAssociated();
                                        });
    const bool realtimeReady = g_realtimeLagSamples > 1 &&
                               g_healthyLagSamples >= g_readinessConsecutiveSamples;
    if (associated && realtimeReady)
    {
        std::ostringstream details;
        details << "all_stations_associated;lag_ms=" << g_latestRealtimeLagMs
                << ";healthy_samples=" << g_healthyLagSamples;
        LogEvent("wifi_bss_ready", "cp", "", 0, g_wifiStations.size(), details.str());
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
        g_phasedArraySpectrumPropagationModels.size() == 1 &&
        g_phasedArraySpectrumPropagationModels.front() ==
            "ns3::SionnaRtSpectrumPropagationLossModel";
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "{\n"
           << "  \"stop_reason\": \"" << g_stopReason << "\",\n"
           << "  \"uav_count\": " << (g_nodeNames.size() - 1) << ",\n"
           << "  \"radio_node_count\": " << g_nodeNames.size() << ",\n"
           << "  \"shared_spectrum_channel_count\": 1,\n"
           << "  \"radio_backend\": \"" << g_radioBackend << "\",\n"
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
           << "  \"tap_ingress_segment\": {\"type\": \"local_fast_csma\", \"radio_medium\": false},\n"
           << "  \"cache_policy\": \"displacement_or_time\",\n"
           << "  \"channel_state_max_age_s\": " << g_channelStateMaxAgeS << ",\n"
           << "  \"endpoint_displacement_threshold_m\": " << g_updateDistanceThresholdM << ",\n"
           << "  \"pose_snapshots\": " << g_poseSnapshots << ",\n"
           << "  \"pose_updates\": " << g_poseSnapshots << ",\n"
           << "  \"stale_pose_samples\": " << g_stalePoseSamples << ",\n"
           << "  \"path_observations\": " << g_pathObservations << ",\n"
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
           << "  \"sionna_in_process\": true,\n"
           << "  \"phased_array_spectrum_propagation_model_count\": "
           << g_phasedArraySpectrumPropagationModels.size() << ",\n"
           << "  \"rx_psd_propagation_model\": \""
           << (g_phasedArraySpectrumPropagationModels.empty()
                   ? "none"
                   : g_phasedArraySpectrumPropagationModels.front())
           << "\",\n"
           << "  \"sionna_drives_rx_psd\": " << (sionnaDrivesRxPsd ? "true" : "false")
           << ",\n"
           << "  \"neighbor_discovery_mode\": \"" << g_neighborDiscoveryMode << "\",\n"
           << "  \"reason\": \"" << g_radioReason << "\",\n"
           << "  \"solver_profile\": \"" << g_solverProfile << "\",\n"
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
           << ", \"cache_expiry_jitter_fraction\": " << g_sionnaCacheJitterFraction
           << "},\n"
           << "  \"packet_outcome_affected\": "
           << (sionnaDrivesRxPsd ? "true" : "false") << "\n"
           << "}\n";
}

} // namespace

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    py::scoped_interpreter python{};

    uint32_t uavCount = 1;
    std::string radioBackend = "aloha";
    std::string radioProfile = "generic_native_spectrum_aloha_reference";
    bool technologySpecificModem = false;
    std::string neighborDiscoveryMode = "preconfigured_static_neighbors";
    std::string radioReason = "upstream_ideal_phy_arp_reentrancy_limit";
    std::string ignoredLegacyPacketOutcomeClaim;
    std::string solverProfile = "realtime_minimal_solver_profile";
    std::string wifiDataMode = "HtMcs0";
    std::string wifiControlMode = "ErpOfdmRate6Mbps";
    std::string sourceConfig;
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
    double txPowerW = 0.00001;
    uint64_t phyRateBps = 1000000;
    double channelStateMaxAgeS = 2.0;
    double updateDistanceThresholdM = 1.0;
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
    double sionnaCacheJitterFraction = 0.0;

    CommandLine command(__FILE__);
    command.AddValue("uavCount", "Number of UAV radio nodes (supported: 1 or 5)", uavCount);
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
    command.AddValue("sources", "Explicit resolved WaveformGenerator source config", sourceConfig);
    command.AddValue("wifiControlMode", "Constant 802.11n control mode", wifiControlMode);
    command.AddValue("wifiSsid", "Infrastructure BSS SSID", wifiSsid);
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
    command.AddValue("phaseFile", "Current product flight phase file", phaseFile);
    command.AddValue("duration", "Maximum wall-clock run duration", duration);
    command.AddValue("txPowerW", "Spectrum transmitter power in watts", txPowerW);
    command.AddValue("phyRateBps", "Configured native PHY bit rate", phyRateBps);
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
    command.AddValue("sionnaMaxNumberOfPaths",
                     "Maximum Sionna paths retained per link",
                     sionnaMaxNumberOfPaths);
    command.AddValue("sionnaCacheJitterFraction",
                     "Deterministic per-pair cache threshold spread",
                     sionnaCacheJitterFraction);
    command.Parse(argc, argv);
    g_txPowerW = txPowerW;
    g_carrierHz = carrierHz;
    g_wifiChannelNumber = wifiChannelNumber;
    g_wifiChannelWidthMhz = wifiChannelWidthMhz;
    g_wifiDataMode = wifiDataMode;
    g_wifiControlMode = wifiControlMode;
    g_wifiSsid = wifiSsid;

    NS_ABORT_MSG_IF(uavCount != 1 && uavCount != 5, "uavCount must be 1 or 5");
    NS_ABORT_MSG_IF(radioBackend != "aloha" && radioBackend != "wifi",
                    "radioBackend must be aloha or wifi");
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
    NS_ABORT_MSG_IF(scene.empty() || positionFile.empty() || readyFile.empty() || phaseFile.empty(),
                    "scene, positionFile, readyFile, and phaseFile are required");
    NS_ABORT_MSG_IF(!std::filesystem::exists(scene), "Town01 scene.xml is missing: " << scene);

    std::signal(SIGTERM, HandleSignal);
    std::signal(SIGINT, HandleSignal);
    NS_ABORT_MSG_IF(channelStateMaxAgeS <= 0.0 || updateDistanceThresholdM <= 0.0,
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
    g_sionnaCacheJitterFraction = sionnaCacheJitterFraction;
    g_flushEveryEvents = flushEveryEvents;
    g_flushMaxDelayMs = flushMaxDelayMs;
    g_readinessLagMaxMs = readinessLagMaxMs;
    g_readinessConsecutiveSamples = readinessConsecutiveSamples;
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod",
                       TimeValue(Seconds(channelStateMaxAgeS)));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdateDistanceThreshold",
                       DoubleValue(updateDistanceThresholdM));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdateJitterFraction",
                       DoubleValue(sionnaCacheJitterFraction));

    g_nodeNames.push_back("cp");
    for (uint32_t index = 1; index <= uavCount; ++index)
    {
        g_nodeNames.push_back("uav" + std::to_string(index));
    }
    g_nodeCounters.resize(g_nodeNames.size());
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
    Ptr<ConstantPositionMobilityModel> cpMobility = CreateObject<ConstantPositionMobilityModel>();
    commandPost->AggregateObject(cpMobility);
    mobility.push_back(cpMobility);
    std::vector<Ptr<MobilityModel>> uavMobility;
    for (uint32_t index = 0; index < uavCount; ++index)
    {
        Ptr<ConstantPositionMobilityModel> model = CreateObject<ConstantPositionMobilityModel>();
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
    Ptr<SionnaRtSpectrumPropagationLossModel> sionna =
        CreateObject<SionnaRtSpectrumPropagationLossModel>();
    sionna->SetChannelModelAttribute("Frequency", DoubleValue(carrierHz));
    sionna->SetChannelModelAttribute("Scenario", StringValue(scene));
    sionna->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(true));
    sionna->SetChannelModelAttribute("MaxNumberOfPaths",
                                     DoubleValue(static_cast<double>(sionnaMaxNumberOfPaths)));
    SionnaRtChannelModel::RtPathSolverConfig solver;
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
    if (sourceConfig.empty())
        channel->AddPhasedArraySpectrumPropagationLossModel(sionna);
    else
    {
        auto router = bas::InstallSpectrumSources(sionna, channel, sourceConfig, scene,
            solver, sionnaMaxNumberOfPaths,
            [](const std::string& event, const std::string& id, double value,
               const std::string& details) { LogEvent(event, id, "", 0, value, details); });
        channel->AddPhasedArraySpectrumPropagationLossModel(router);
        LogEvent("source_propagation_dispatch", "ns3", "", 0, router->sources.size(),
                 "wrapper=bas::SourcePropagation;all_delegates=ns3::SionnaRtSpectrumPropagationLossModel;one_model_per_signal=true");
    }
    g_phasedArraySpectrumPropagationModels.push_back(sionna->GetInstanceTypeId().GetName());
    LogEvent("rx_psd_model_binding",
             "ns3",
             "",
             0,
             static_cast<double>(g_phasedArraySpectrumPropagationModels.size()),
             "channel=ns3::MultiModelSpectrumChannel;sole_model=" +
                 g_phasedArraySpectrumPropagationModels.front() + ";effect=received_psd");

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
        wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                     "DataMode",
                                     StringValue(wifiDataMode),
                                     "ControlMode",
                                     StringValue(wifiControlMode),
                                     "NonUnicastMode",
                                     StringValue(wifiControlMode));
        WifiMacHelper wifiMac;
        const Ssid ssid(wifiSsid);
        wifiMac.SetType("ns3::ApWifiMac",
                        "QosSupported",
                        BooleanValue(true),
                        "Ssid",
                        SsidValue(ssid));
        radioDevices.Add(wifi.Install(wifiPhy, wifiMac, NodeContainer(commandPost)));
        wifiMac.SetType("ns3::StaWifiMac",
                        "QosSupported",
                        BooleanValue(true),
                        "Ssid",
                        SsidValue(ssid));
        radioDevices.Add(wifi.Install(wifiPhy, wifiMac, uavs));
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
            if (index > 0)
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
    tap.SetAttribute("Mode", StringValue("UseBridge"));
    tap.SetAttribute("DeviceName", StringValue(tapGcs));
    tap.Install(ghostGcs, ingressDevices.Get(0));
    tap.SetAttribute("Mode", StringValue(radioBackend == "wifi" ? "UseLocal" : "UseBridge"));
    for (uint32_t index = 0; index < uavCount; ++index)
    {
        tap.SetAttribute("DeviceName", StringValue(uavTapNames[index]));
        tap.Install(uavs.Get(index), radioDevices.Get(index + 1));
    }

    LivePositionSource positions(positionFile, mobility, Seconds(1.5));
    NativeRuntimeSampler metrics(sionna->GetChannelModel(), cpMobility, uavMobility);
    g_runtimeSampler = &metrics;
    if (radioBackend == "wifi")
    {
        positions.Poll();
    }
    else
    {
        Simulator::ScheduleNow(&LivePositionSource::Poll, &positions);
    }
    Simulator::ScheduleNow(&NativeRuntimeSampler::PollLag, &metrics);
    Simulator::Schedule(Seconds(1), &NativeRuntimeSampler::PollSionnaPaths, &metrics);
    Simulator::ScheduleNow(&SampleQueues);
    Simulator::ScheduleNow(&PollSignal);
    if (radioBackend == "wifi")
    {
        Simulator::ScheduleNow(&WriteReadyWhenWifiAssociated, readyFile);
    }
    else
    {
        Simulator::Schedule(MilliSeconds(250), &WriteReady, readyFile);
    }
    Simulator::Stop(Seconds(duration));

    int result = 0;
    try
    {
        Simulator::Run();
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
    Simulator::Destroy();
    WriteStats(statsFile);
    FlushEvents();
    g_events.close();
    g_radioPcap = nullptr;
    if (g_stopReason == "position_tracker_stale")
    {
        return 5;
    }
    return result;
}
