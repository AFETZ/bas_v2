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
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-helper.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/uniform-planar-array.h"

#include <chrono>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <optional>
#include <sstream>
#include <string>

using namespace ns3;
namespace py = pybind11;

NS_LOG_COMPONENT_DEFINE("UpstreamSionnaTapSpike");

namespace
{

struct Counters
{
    uint64_t cpMacTx{0};
    uint64_t uavMacTx{0};
    uint64_t cpPhyRxOk{0};
    uint64_t uavPhyRxOk{0};
    uint64_t cpPhyRxError{0};
    uint64_t uavPhyRxError{0};
    uint64_t poseUpdates{0};
};

Counters g_counters;
Ptr<PcapFileWrapper> g_radioPcap;
std::ofstream g_events;
Vector g_cpPosition;
Vector g_uavPosition;
volatile std::sig_atomic_t g_stopRequested = 0;
std::string g_stopReason = "duration";

void
HandleSignal(int)
{
    g_stopRequested = 1;
}

void
LogEvent(const std::string& event, const std::string& node, uint32_t bytes)
{
    g_events << std::fixed << std::setprecision(6) << Simulator::Now().GetSeconds() << ',' << event
             << ',' << node << ',' << bytes << ',' << g_cpPosition.x << ',' << g_cpPosition.y << ','
             << g_cpPosition.z << ',' << g_uavPosition.x << ',' << g_uavPosition.y << ','
             << g_uavPosition.z << '\n';
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

void
CpMacTx(Ptr<const Packet> packet)
{
    ++g_counters.cpMacTx;
    LogEvent("mac_tx", "cp", packet->GetSize());
    WriteRadioPcap(packet);
}

void
UavMacTx(Ptr<const Packet> packet)
{
    ++g_counters.uavMacTx;
    LogEvent("mac_tx", "uav1", packet->GetSize());
    WriteRadioPcap(packet);
}

void
CpPhyRxOk(Ptr<const Packet> packet)
{
    ++g_counters.cpPhyRxOk;
    LogEvent("phy_rx_ok", "cp", packet->GetSize());
    WriteRadioPcap(packet);
}

void
UavPhyRxOk(Ptr<const Packet> packet)
{
    ++g_counters.uavPhyRxOk;
    LogEvent("phy_rx_ok", "uav1", packet->GetSize());
    WriteRadioPcap(packet);
}

void
CpPhyRxError(Ptr<const Packet> packet)
{
    ++g_counters.cpPhyRxError;
    LogEvent("phy_rx_error", "cp", packet->GetSize());
}

void
UavPhyRxError(Ptr<const Packet> packet)
{
    ++g_counters.uavPhyRxError;
    LogEvent("phy_rx_error", "uav1", packet->GetSize());
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

class LivePositionSource
{
  public:
    LivePositionSource(std::string path,
                       Ptr<MobilityModel> cpMobility,
                       Ptr<MobilityModel> uavMobility,
                       Time timeout)
        : m_path(std::move(path)),
          m_cpMobility(cpMobility),
          m_uavMobility(uavMobility),
          m_timeout(timeout),
          m_lastFresh(std::chrono::steady_clock::now())
    {
    }

    void Poll()
    {
        std::error_code error;
        const auto modified = std::filesystem::last_write_time(m_path, error);
        if (!error && (!m_haveTimestamp || modified != m_lastModified))
        {
            std::ifstream input(m_path);
            std::ostringstream buffer;
            buffer << input.rdbuf();
            const std::string json = buffer.str();
            const auto cp = ParsePosition(json, "cp");
            const auto uav = ParsePosition(json, "uav1");
            const bool live = json.find("\"source\": \"ros_odometry\"") != std::string::npos;
            if (input.is_open() && live && cp && uav)
            {
                g_cpPosition = *cp;
                g_uavPosition = *uav;
                m_cpMobility->SetPosition(*cp);
                m_uavMobility->SetPosition(*uav);
                m_lastModified = modified;
                m_haveTimestamp = true;
                m_lastFresh = std::chrono::steady_clock::now();
                ++g_counters.poseUpdates;
                LogEvent("live_pose", "tracker", 0);
            }
        }
        const auto staleFor = std::chrono::steady_clock::now() - m_lastFresh;
        if (staleFor > std::chrono::nanoseconds(m_timeout.GetNanoSeconds()))
        {
            g_stopReason = "position_tracker_stale";
            LogEvent("fail_closed", "tracker", 0);
            Simulator::Stop();
            return;
        }
        Simulator::Schedule(MilliSeconds(100), &LivePositionSource::Poll, this);
    }

  private:
    std::string m_path;
    Ptr<MobilityModel> m_cpMobility;
    Ptr<MobilityModel> m_uavMobility;
    Time m_timeout;
    std::filesystem::file_time_type m_lastModified;
    bool m_haveTimestamp{false};
    std::chrono::steady_clock::time_point m_lastFresh;
};

void
PollSignal()
{
    if (g_stopRequested)
    {
        g_stopReason = "sionna_ns3_process_stopped";
        LogEvent("fail_closed_stop", "sionna_in_process", 0);
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
AddRouterAddress(Ptr<Node> router, Ptr<NetDevice> device, const char* address)
{
    Ptr<Ipv4> ipv4 = router->GetObject<Ipv4>();
    const uint32_t interface = ipv4->AddInterface(device);
    ipv4->AddAddress(interface,
                     Ipv4InterfaceAddress(Ipv4Address(address), Ipv4Mask("255.255.255.0")));
    ipv4->SetMetric(interface, 1);
    ipv4->SetUp(interface);
    ipv4->SetForwarding(interface, true);
    return interface;
}

void
AddPermanentArp(Ptr<Node> router,
                Ptr<NetDevice> device,
                const char* address,
                const Mac48Address& mac)
{
    Ptr<Ipv4L3Protocol> ipv4 = router->GetObject<Ipv4L3Protocol>();
    const int32_t interfaceIndex = ipv4->GetInterfaceForDevice(device);
    NS_ABORT_MSG_IF(interfaceIndex < 0, "radio device has no IPv4 interface");
    Ptr<ArpCache> cache =
        ipv4->GetInterface(static_cast<uint32_t>(interfaceIndex))->GetArpCache();
    NS_ABORT_MSG_IF(!cache, "radio interface has no ARP cache");
    ArpCache::Entry* entry = cache->Lookup(Ipv4Address(address));
    if (!entry)
    {
        entry = cache->Add(Ipv4Address(address));
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
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "{\n"
           << "  \"stop_reason\": \"" << g_stopReason << "\",\n"
           << "  \"pose_updates\": " << g_counters.poseUpdates << ",\n"
           << "  \"cp_mac_tx\": " << g_counters.cpMacTx << ",\n"
           << "  \"uav_mac_tx\": " << g_counters.uavMacTx << ",\n"
           << "  \"cp_phy_rx_ok\": " << g_counters.cpPhyRxOk << ",\n"
           << "  \"uav_phy_rx_ok\": " << g_counters.uavPhyRxOk << ",\n"
           << "  \"cp_phy_rx_error\": " << g_counters.cpPhyRxError << ",\n"
           << "  \"uav_phy_rx_error\": " << g_counters.uavPhyRxError << "\n"
           << "}\n";
}

} // namespace

int
main(int argc, char* argv[])
{
    py::scoped_interpreter python{};

    std::string tapGcs = "tap-gcs";
    std::string tapUav = "tap-uav";
    std::string scene;
    std::string positionFile;
    std::string radioPcap = "upstream-radio.pcap";
    std::string eventCsv = "upstream-radio-events.csv";
    std::string statsFile = "upstream-radio-stats.json";
    std::string readyFile;
    double duration = 120.0;
    double txPowerW = 0.00001;

    CommandLine command(__FILE__);
    command.AddValue("tapGcs", "Existing command-post TAP device", tapGcs);
    command.AddValue("tapUav", "Existing UAV TAP device", tapUav);
    command.AddValue("scene", "Town01 scene.xml path", scene);
    command.AddValue("positionFile", "Live position tracker JSON", positionFile);
    command.AddValue("radioPcap", "Native radio MAC PCAP", radioPcap);
    command.AddValue("eventCsv", "Native radio and position event CSV", eventCsv);
    command.AddValue("statsFile", "Final native radio counters", statsFile);
    command.AddValue("readyFile", "Readiness file", readyFile);
    command.AddValue("duration", "Maximum wall-clock run duration", duration);
    command.AddValue("txPowerW", "Spectrum transmitter power in watts", txPowerW);
    command.Parse(argc, argv);

    NS_ABORT_MSG_IF(scene.empty() || positionFile.empty() || readyFile.empty(),
                    "scene, positionFile, and readyFile are required");
    NS_ABORT_MSG_IF(!std::filesystem::exists(scene), "Town01 scene.xml is missing: " << scene);

    std::signal(SIGTERM, HandleSignal);
    std::signal(SIGINT, HandleSignal);
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod", TimeValue(Seconds(2.0)));

    g_events.open(eventCsv, std::ios::out | std::ios::trunc);
    g_events << "time_s,event,node,bytes,cp_x,cp_y,cp_z,uav_x,uav_y,uav_z\n";
    PcapHelper pcapHelper;
    g_radioPcap = pcapHelper.CreateFile(radioPcap, std::ios::out, PcapHelper::DLT_EN10MB);

    Ptr<Node> ghostGcs = CreateObject<Node>();
    Ptr<Node> commandPost = CreateObject<Node>();
    Ptr<Node> uav = CreateObject<Node>();

    Ptr<ConstantPositionMobilityModel> cpMobility = CreateObject<ConstantPositionMobilityModel>();
    Ptr<ConstantPositionMobilityModel> uavMobility = CreateObject<ConstantPositionMobilityModel>();
    commandPost->AggregateObject(cpMobility);
    uav->AggregateObject(uavMobility);

    CsmaHelper ingress;
    ingress.SetChannelAttribute("DataRate", StringValue("1Gbps"));
    ingress.SetChannelAttribute("Delay", StringValue("10us"));
    NetDeviceContainer ingressDevices = ingress.Install(NodeContainer(ghostGcs, commandPost));

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
    NetDeviceContainer radioDevices = radio.Install(NodeContainer(commandPost, uav));

    Ptr<AlohaNoackNetDevice> cpRadio = DynamicCast<AlohaNoackNetDevice>(radioDevices.Get(0));
    Ptr<AlohaNoackNetDevice> uavRadio = DynamicCast<AlohaNoackNetDevice>(radioDevices.Get(1));
    Ptr<HalfDuplexIdealPhy> cpPhy = cpRadio->GetPhy()->GetObject<HalfDuplexIdealPhy>();
    Ptr<HalfDuplexIdealPhy> uavPhy = uavRadio->GetPhy()->GetObject<HalfDuplexIdealPhy>();
    cpPhy->SetAntenna(CreateAntenna());
    uavPhy->SetAntenna(CreateAntenna());
    cpRadio->TraceConnectWithoutContext("MacTx", MakeCallback(&CpMacTx));
    uavRadio->TraceConnectWithoutContext("MacTx", MakeCallback(&UavMacTx));
    cpPhy->TraceConnectWithoutContext("RxEndOk", MakeCallback(&CpPhyRxOk));
    uavPhy->TraceConnectWithoutContext("RxEndOk", MakeCallback(&UavPhyRxOk));
    cpPhy->TraceConnectWithoutContext("RxEndError", MakeCallback(&CpPhyRxError));
    uavPhy->TraceConnectWithoutContext("RxEndError", MakeCallback(&UavPhyRxError));

    ingressDevices.Get(1)->SetAddress(Mac48Address("02:71:00:00:00:01"));
    radioDevices.Get(0)->SetAddress(Mac48Address("02:71:01:00:00:01"));
    InternetStackHelper internet;
    internet.Install(commandPost);
    AddRouterAddress(commandPost, ingressDevices.Get(1), "10.71.0.1");
    AddRouterAddress(commandPost, radioDevices.Get(0), "10.71.1.1");
    // The existing namespace topology fixes this endpoint MAC.  A permanent
    // neighbor entry avoids the upstream ideal PHY's receive-callback TX
    // reentrancy bug without changing radio propagation or packet outcomes.
    AddPermanentArp(commandPost,
                    radioDevices.Get(0),
                    "10.71.1.10",
                    Mac48Address("02:71:01:00:10:10"));

    TapBridgeHelper tap;
    tap.SetAttribute("Mode", StringValue("UseBridge"));
    tap.SetAttribute("DeviceName", StringValue(tapGcs));
    tap.Install(ghostGcs, ingressDevices.Get(0));
    tap.SetAttribute("DeviceName", StringValue(tapUav));
    tap.Install(uav, radioDevices.Get(1));

    LivePositionSource positions(positionFile, cpMobility, uavMobility, Seconds(1.5));
    Simulator::ScheduleNow(&LivePositionSource::Poll, &positions);
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
