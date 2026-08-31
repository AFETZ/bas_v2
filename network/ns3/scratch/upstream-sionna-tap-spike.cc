#include "pybind11/embed.h"

#include "ns3/adhoc-aloha-noack-ideal-phy-helper.h"
#include "ns3/aloha-noack-mac-header.h"
#include "ns3/aloha-noack-net-device.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/ethernet-header.h"
#include "ns3/half-duplex-ideal-phy.h"
#include "ns3/internet-module.h"
#include "ns3/ism-spectrum-value-helper.h"
#include "ns3/llc-snap-header.h"
#include "ns3/mobility-module.h"
#include "ns3/multi-model-spectrum-channel.h"
#include "ns3/pcap-file-wrapper.h"
#include "ns3/propagation-delay-model.h"
#include "ns3/realtime-simulator-impl.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-helper.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/uniform-planar-array.h"

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
    uint64_t phyRxError{0};
};

std::vector<NodeCounters> g_nodeCounters;
std::vector<std::string> g_nodeNames;
std::vector<Vector> g_positions;
uint64_t g_poseSnapshots{0};
uint64_t g_stalePoseSamples{0};
uint64_t g_pathObservations{0};
uint64_t g_realtimeLagSamples{0};
Ptr<PcapFileWrapper> g_radioPcap;
std::ofstream g_events;
volatile std::sig_atomic_t g_stopRequested = 0;
std::string g_stopReason = "duration";
std::string g_phaseFile;

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
    g_events.flush();
}

void
WriteRadioPcap(Ptr<const Packet> packet)
{
    Ptr<Packet> copy = packet->Copy();
    AlohaNoackMacHeader aloha;
    LlcSnapHeader llc;
    if (copy->RemoveHeader(aloha) == 0 || copy->RemoveHeader(llc) == 0)
    {
        return;
    }
    EthernetHeader ethernet(false);
    ethernet.SetSource(aloha.GetSource());
    ethernet.SetDestination(aloha.GetDestination());
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
    AlohaNoackMacHeader aloha;
    LlcSnapHeader llc;
    if (copy->RemoveHeader(aloha) == 0 || copy->RemoveHeader(llc) == 0 ||
        llc.GetType() != Ipv4L3Protocol::PROT_NUMBER)
    {
        return details.str();
    }

    Ipv4Header ipv4;
    if (copy->RemoveHeader(ipv4) == 0)
    {
        return details.str();
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
        std::error_code error;
        const auto modified = std::filesystem::last_write_time(m_path, error);
        bool invalidSnapshot = error.value() != 0;
        if (!error && (!m_haveTimestamp || modified != m_lastModified))
        {
            std::ifstream input(m_path);
            std::ostringstream buffer;
            buffer << input.rdbuf();
            const std::string json = buffer.str();
            const auto sourceTime = ParseScalar(json, "time_s");
            const bool live = json.find("\"source\": \"ros_odometry\"") != std::string::npos;
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
                m_lastModified = modified;
                m_haveTimestamp = true;
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

        if (invalidSnapshot && !m_invalidSince)
        {
            m_invalidSince = now;
        }
        const auto timeout = std::chrono::nanoseconds(m_timeout.GetNanoSeconds());
        const bool invalidTimedOut = m_invalidSince && now - *m_invalidSince > timeout;
        const bool trackerStopped = !error && m_haveTimestamp && modified == m_lastModified &&
                                    now - m_lastFresh > timeout;
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
    std::filesystem::file_time_type m_lastModified;
    bool m_haveTimestamp{false};
    std::chrono::steady_clock::time_point m_lastFresh;
    std::optional<std::chrono::steady_clock::time_point> m_invalidSince;
};

class NativeRuntimeSampler
{
  public:
    NativeRuntimeSampler(Ptr<MatrixBasedChannelModel> channelModel,
                         Ptr<MobilityModel> cpMobility,
                         std::vector<Ptr<MobilityModel>> uavMobility)
        : m_channelModel(std::move(channelModel)),
          m_cpMobility(std::move(cpMobility)),
          m_uavMobility(std::move(uavMobility))
    {
    }

    void PollLag()
    {
        Ptr<RealtimeSimulatorImpl> realtime =
            DynamicCast<RealtimeSimulatorImpl>(Simulator::GetImplementation());
        if (realtime)
        {
            const double lagMs = (realtime->RealtimeNow() - Simulator::Now()).GetSeconds() * 1000.0;
            ++g_realtimeLagSamples;
            LogEvent("realtime_lag", "ns3", "", 0, lagMs, "lag_ms");
        }
        Simulator::Schedule(MilliSeconds(500), &NativeRuntimeSampler::PollLag, this);
    }

    void PollPaths()
    {
        for (std::size_t index = 0; index < m_uavMobility.size(); ++index)
        {
            Ptr<const MatrixBasedChannelModel::ChannelParams> params =
                m_channelModel->GetParams(m_cpMobility, m_uavMobility[index]);
            if (params)
            {
                std::ostringstream delays;
                delays << std::setprecision(12);
                for (std::size_t path = 0; path < params->m_delay.size(); ++path)
                {
                    if (path)
                    {
                        delays << ';';
                    }
                    delays << params->m_delay[path];
                }
                ++g_pathObservations;
                LogEvent("sionna_paths", "cp", g_nodeNames.at(index + 1), 0,
                         static_cast<double>(params->m_delay.size()), delays.str());
            }
        }
    }

  private:
    Ptr<MatrixBasedChannelModel> m_channelModel;
    Ptr<MobilityModel> m_cpMobility;
    std::vector<Ptr<MobilityModel>> m_uavMobility;
};

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
WriteStats(const std::string& path)
{
    uint64_t macTx = 0;
    uint64_t phyRxStart = 0;
    uint64_t phyRxOk = 0;
    uint64_t phyRxError = 0;
    for (const NodeCounters& counters : g_nodeCounters)
    {
        macTx += counters.macTx;
        phyRxStart += counters.phyRxStart;
        phyRxOk += counters.phyRxOk;
        phyRxError += counters.phyRxError;
    }
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "{\n"
           << "  \"stop_reason\": \"" << g_stopReason << "\",\n"
           << "  \"uav_count\": " << (g_nodeNames.size() - 1) << ",\n"
           << "  \"radio_node_count\": " << g_nodeNames.size() << ",\n"
           << "  \"shared_spectrum_channel_count\": 1,\n"
           << "  \"tap_ingress_segment\": {\"type\": \"local_fast_csma\", \"radio_medium\": false},\n"
           << "  \"pose_snapshots\": " << g_poseSnapshots << ",\n"
           << "  \"pose_updates\": " << g_poseSnapshots << ",\n"
           << "  \"stale_pose_samples\": " << g_stalePoseSamples << ",\n"
           << "  \"path_observations\": " << g_pathObservations << ",\n"
           << "  \"realtime_lag_samples\": " << g_realtimeLagSamples << ",\n"
           << "  \"mac_tx_total\": " << macTx << ",\n"
           << "  \"phy_rx_start_total\": " << phyRxStart << ",\n"
           << "  \"phy_rx_ok_total\": " << phyRxOk << ",\n"
           << "  \"phy_rx_error_total\": " << phyRxError << ",\n"
           << "  \"cp_mac_tx\": " << g_nodeCounters.front().macTx << ",\n"
           << "  \"uav_mac_tx\": " << (macTx - g_nodeCounters.front().macTx) << ",\n"
           << "  \"cp_phy_rx_start\": " << g_nodeCounters.front().phyRxStart << ",\n"
           << "  \"uav_phy_rx_start\": " << (phyRxStart - g_nodeCounters.front().phyRxStart) << ",\n"
           << "  \"cp_phy_rx_ok\": " << g_nodeCounters.front().phyRxOk << ",\n"
           << "  \"uav_phy_rx_ok\": " << (phyRxOk - g_nodeCounters.front().phyRxOk) << ",\n"
           << "  \"cp_phy_rx_error\": " << g_nodeCounters.front().phyRxError << ",\n"
           << "  \"uav_phy_rx_error\": " << (phyRxError - g_nodeCounters.front().phyRxError) << ",\n"
           << "  \"nodes\": {\n";
    for (std::size_t index = 0; index < g_nodeNames.size(); ++index)
    {
        const NodeCounters& counters = g_nodeCounters[index];
        output << "    \"" << g_nodeNames[index] << "\": {\"mac_tx\": " << counters.macTx
               << ", \"phy_rx_start\": " << counters.phyRxStart << ", \"phy_rx_ok\": "
               << counters.phyRxOk << ", \"phy_rx_error\": " << counters.phyRxError << "}";
        output << (index + 1 == g_nodeNames.size() ? "\n" : ",\n");
    }
    output << "  },\n"
           << "  \"profile\": \"generic_native_spectrum_aloha_reference\",\n"
           << "  \"technology_specific_modem\": false,\n"
           << "  \"native_ns3_phy\": true,\n"
           << "  \"native_ns3_mac\": true,\n"
           << "  \"custom_packet_error_model\": false,\n"
           << "  \"custom_scheduler\": false,\n"
           << "  \"sionna_in_process\": true,\n"
           << "  \"neighbor_discovery_mode\": \"preconfigured_static_neighbors\",\n"
           << "  \"reason\": \"upstream_ideal_phy_arp_reentrancy_limit\",\n"
           << "  \"packet_outcome_affected\": false\n"
           << "}\n";
}

} // namespace

int
main(int argc, char* argv[])
{
    py::scoped_interpreter python{};

    uint32_t uavCount = 1;
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

    CommandLine command(__FILE__);
    command.AddValue("uavCount", "Number of UAV radio nodes (supported: 1 or 5)", uavCount);
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
    command.Parse(argc, argv);

    NS_ABORT_MSG_IF(uavCount != 1 && uavCount != 5, "uavCount must be 1 or 5");
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
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod", TimeValue(Seconds(2.0)));

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
    sionna->SetChannelModelAttribute("Frequency", DoubleValue(2.4e9));
    sionna->SetChannelModelAttribute("Scenario", StringValue(scene));
    sionna->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(true));
    sionna->SetChannelModelAttribute("MaxNumberOfPaths", DoubleValue(90));
    SionnaRtChannelModel::RtPathSolverConfig solver;
    solver.maxDepth = 1;
    solver.los = true;
    solver.specularReflection = true;
    solver.diffuseReflection = false;
    solver.diffraction = false;
    solver.edgeDiffraction = false;
    solver.refraction = false;
    solver.syntheticArray = true;
    solver.seed = 42;
    sionna->SetRtPathSolverConfig(solver);
    channel->AddPhasedArraySpectrumPropagationLossModel(sionna);

    SpectrumValue5MhzFactory spectrum;
    Ptr<SpectrumValue> txPsd = spectrum.CreateTxPowerSpectralDensity(txPowerW, 1);
    Ptr<SpectrumValue> noisePsd = spectrum.CreateConstant(1.381e-23 * 290.0);
    AdhocAlohaNoackIdealPhyHelper radio;
    radio.SetChannel(channel);
    radio.SetTxPowerSpectralDensity(txPsd);
    radio.SetNoisePowerSpectralDensity(noisePsd);
    radio.SetPhyAttribute("Rate", DataRateValue(DataRate("1Mbps")));
    NetDeviceContainer radioDevices = radio.Install(radioNodes);

    for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
    {
        Ptr<AlohaNoackNetDevice> device = DynamicCast<AlohaNoackNetDevice>(radioDevices.Get(index));
        Ptr<HalfDuplexIdealPhy> phy = device->GetPhy()->GetObject<HalfDuplexIdealPhy>();
        phy->SetAntenna(CreateAntenna());
        device->TraceConnectWithoutContext("MacTx", MakeBoundCallback(&MacTx, index));
        phy->TraceConnectWithoutContext("RxEndOk", MakeBoundCallback(&PhyRxOk, index));
        phy->TraceConnectWithoutContext("RxStart", MakeBoundCallback(&PhyRxStart, index));
        phy->TraceConnectWithoutContext("RxEndError", MakeBoundCallback(&PhyRxError, index));
    }

    ingressDevices.Get(1)->SetAddress(Mac48Address("02:71:00:00:00:01"));
    const Mac48Address cpRadioMac("02:71:ff:00:00:01");
    radioDevices.Get(0)->SetAddress(cpRadioMac);
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
        radioDevices.Get(index)->SetAddress(Mac48Address(endpointMac.str().c_str()));
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
    for (uint32_t index = 0; index < uavCount; ++index)
    {
        tap.SetAttribute("DeviceName", StringValue(uavTapNames[index]));
        tap.Install(uavs.Get(index), radioDevices.Get(index + 1));
    }

    LivePositionSource positions(positionFile, mobility, Seconds(1.5));
    NativeRuntimeSampler metrics(sionna->GetChannelModel(), cpMobility, uavMobility);
    Simulator::ScheduleNow(&LivePositionSource::Poll, &positions);
    Simulator::ScheduleNow(&NativeRuntimeSampler::PollLag, &metrics);
    // Allow one startup cache sample; it can be empty before the first packet.
    // Periodic report-only GetParams calls are intentionally avoided: packet
    // transmissions are the causal source of subsequent channel solves.
    Simulator::ScheduleNow(&NativeRuntimeSampler::PollPaths, &metrics);
    Simulator::ScheduleNow(&PollSignal);
    Simulator::Schedule(MilliSeconds(250), &WriteReady, readyFile);
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
    g_events.close();
    g_radioPcap = nullptr;
    if (g_stopReason == "position_tracker_stale")
    {
        return 5;
    }
    return result;
}
