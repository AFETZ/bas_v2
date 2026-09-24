/*
 * Five-UAV native Wi-Fi/Sionna control-plane reference.
 *
 * One 802.11n AP sends simultaneous control requests to five associated STAs.
 * Every STA returns the original SeqTsHeader, giving per-UAV RTT/PDR and fairness.
 * The MultiModelSpectrumChannel contains only Sionna RT propagation: there is no
 * scalar propagation-loss fallback or application-level bypass.
 */

#include "pybind11/pybind11.h"

#include "ns3/boolean.h"
#include "ns3/command-line.h"
#include "ns3/constant-position-mobility-model.h"
#include "ns3/core-module.h"
#include "ns3/internet-stack-helper.h"
#include "ns3/ipv4-address-helper.h"
#include "ns3/isotropic-antenna-model.h"
#include "ns3/multi-model-spectrum-channel.h"
#include "ns3/network-module.h"
#include "ns3/realtime-simulator-impl.h"
#include "ns3/seq-ts-header.h"
#include "ns3/sionna-rt-channel-model.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-wifi-helper.h"
#include "ns3/spectrum-wifi-phy.h"
#include "ns3/ssid.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uniform-planar-array.h"
#include "ns3/wifi-module.h"
#include "ns3/wifi-net-device.h"

#include <sys/resource.h>

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <set>
#include <string>
#include <vector>

namespace py = pybind11;
using namespace ns3;

namespace
{

constexpr uint32_t kUavCount = 5;
constexpr uint16_t kControlPort = 9400;

std::vector<Ipv4Address> g_uavAddresses;
std::vector<uint32_t> g_offered(kUavCount, 0);
std::vector<uint32_t> g_acked(kUavCount, 0);
std::vector<std::vector<double>> g_rttMs(kUavCount);
std::set<uint32_t> g_seenAcks;
std::vector<double> g_allLagMs;
std::vector<double> g_profileLagMs;
double g_profileStartS = 0.0;
double g_simulationSeconds = 0.0;
uint32_t g_packetSize = 0;
uint64_t g_macTx = 0;
uint64_t g_macTxDrop = 0;
uint64_t g_phyTx = 0;
uint64_t g_macDataRetry = 0;
uint64_t g_monitorSamples = 0;
double g_signalDbmMean = 0.0;
double g_noiseDbmMean = 0.0;

double
Percentile95(std::vector<double> values)
{
    if (values.empty())
    {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const std::size_t index =
        std::min(values.size() - 1,
                 static_cast<std::size_t>(std::ceil(0.95 * values.size())) - 1);
    return values[index];
}

double
CpuSeconds(const rusage& value)
{
    return value.ru_utime.tv_sec + value.ru_utime.tv_usec / 1e6 + value.ru_stime.tv_sec +
           value.ru_stime.tv_usec / 1e6;
}

Ptr<UniformPlanarArray>
CreateArray()
{
    Ptr<UniformPlanarArray> array =
        CreateObjectWithAttributes<UniformPlanarArray>("NumColumns",
                                                       UintegerValue(1),
                                                       "NumRows",
                                                       UintegerValue(1));
    PhasedArrayModel::ComplexVector weights(1);
    weights[0] = std::complex<double>(1.0, 0.0);
    array->SetBeamformingVector(weights);
    return array;
}

void
AttachArray(Ptr<NetDevice> device)
{
    Ptr<WifiNetDevice> wifiDevice = DynamicCast<WifiNetDevice>(device);
    NS_ABORT_MSG_IF(!wifiDevice, "expected WifiNetDevice");
    Ptr<SpectrumWifiPhy> phy = DynamicCast<SpectrumWifiPhy>(wifiDevice->GetPhy());
    NS_ABORT_MSG_IF(!phy, "expected SpectrumWifiPhy");
    Ptr<AntennaModel> antenna = CreateObject<IsotropicAntennaModel>();
    phy->SetAntenna(antenna);
    antenna->AggregateObject(CreateArray());
}

void
MacTx(Ptr<const Packet>)
{
    ++g_macTx;
}

void
MacTxDrop(Ptr<const Packet>)
{
    ++g_macTxDrop;
}

void
PhyTx(Ptr<const Packet>, double)
{
    ++g_phyTx;
}

void
MacDataRetry(Mac48Address)
{
    ++g_macDataRetry;
}

void
MonitorSniffRx(Ptr<const Packet>,
               uint16_t,
               WifiTxVector,
               MpduInfo,
               SignalNoiseDbm signalNoise,
               uint16_t)
{
    ++g_monitorSamples;
    g_signalDbmMean += (signalNoise.signal - g_signalDbmMean) / g_monitorSamples;
    g_noiseDbmMean += (signalNoise.noise - g_noiseDbmMean) / g_monitorSamples;
}

void
SampleLag()
{
    Ptr<RealtimeSimulatorImpl> realtime =
        DynamicCast<RealtimeSimulatorImpl>(Simulator::GetImplementation());
    if (realtime)
    {
        const double lagMs =
            std::max(0.0, (realtime->RealtimeNow() - Simulator::Now()).GetSeconds() * 1000.0);
        g_allLagMs.push_back(lagMs);
        if (Simulator::Now().GetSeconds() >= g_profileStartS)
        {
            g_profileLagMs.push_back(lagMs);
        }
    }
    if (Simulator::Now().GetSeconds() + 0.05 < g_simulationSeconds)
    {
        Simulator::Schedule(MilliSeconds(50), &SampleLag);
    }
}

void
UavReceive(uint32_t, Ptr<Socket> socket)
{
    Address from;
    while (Ptr<Packet> packet = socket->RecvFrom(from))
    {
        socket->SendTo(packet, 0, from);
    }
}

void
GcsReceive(Ptr<Socket> socket)
{
    Address from;
    while (Ptr<Packet> packet = socket->RecvFrom(from))
    {
        SeqTsHeader header;
        if (packet->RemoveHeader(header) == 0)
        {
            continue;
        }
        const uint32_t encoded = header.GetSeq();
        const uint32_t uavIndex = encoded >> 24;
        if (uavIndex >= kUavCount || !g_seenAcks.insert(encoded).second)
        {
            continue;
        }
        ++g_acked[uavIndex];
        g_rttMs[uavIndex].push_back((Simulator::Now() - header.GetTs()).GetSeconds() * 1000.0);
    }
}

void
SendRound(Ptr<Socket> socket, uint32_t round)
{
    for (uint32_t uavIndex = 0; uavIndex < kUavCount; ++uavIndex)
    {
        SeqTsHeader header;
        header.SetSeq((uavIndex << 24) | round);
        Ptr<Packet> packet = Create<Packet>(g_packetSize - header.GetSerializedSize());
        packet->AddHeader(header);
        socket->SendTo(packet, 0, InetSocketAddress(g_uavAddresses[uavIndex], kControlPort));
        ++g_offered[uavIndex];
    }
}

} // namespace

int
main(int argc, char* argv[])
{
    py::scoped_interpreter python{};
    // Bind before constructing SeqTsHeader: its constructor reads Simulator::Now().
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));

    std::string scene = "simple_street_canyon_with_cars";
    std::string output = "sionna-wifi-five-uav.json";
    double trafficStartS = 12.0;
    double simulationSeconds = 16.0;
    uint32_t rounds = 10;
    uint32_t packetSize = 96;
    double roundIntervalMs = 100.0;

    CommandLine command(__FILE__);
    command.AddValue("scene", "Sionna RT built-in scene or scene.xml path", scene);
    command.AddValue("output", "JSON result path", output);
    command.AddValue("trafficStartS", "Control traffic start after BSS/cache warmup", trafficStartS);
    command.AddValue("simulationSeconds", "Simulation duration including drain", simulationSeconds);
    command.AddValue("rounds", "Simultaneous five-UAV control request rounds", rounds);
    command.AddValue("packetSize", "Control UDP payload bytes including SeqTsHeader", packetSize);
    command.AddValue("roundIntervalMs", "Interval between control rounds", roundIntervalMs);
    command.Parse(argc, argv);

    const double trafficEndS = trafficStartS + rounds * roundIntervalMs / 1000.0;
    NS_ABORT_MSG_IF(packetSize < SeqTsHeader().GetSerializedSize() || rounds == 0,
                    "packet size must fit SeqTsHeader and rounds must be positive");
    NS_ABORT_MSG_IF(trafficStartS < 2.0 || trafficEndS + 1.0 >= simulationSeconds,
                    "scenario requires association warmup and at least one second of drain");

    g_profileStartS = trafficStartS;
    g_simulationSeconds = simulationSeconds;
    g_packetSize = packetSize;
    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod", TimeValue(Seconds(30)));

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

    NodeContainer gcs;
    gcs.Create(1);
    NodeContainer uavs;
    uavs.Create(kUavCount);
    Ptr<ConstantPositionMobilityModel> gcsMobility =
        CreateObject<ConstantPositionMobilityModel>();
    gcsMobility->SetPosition(Vector(-20.0, 0.0, 1.5));
    gcs.Get(0)->AggregateObject(gcsMobility);
    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        Ptr<ConstantPositionMobilityModel> mobility =
            CreateObject<ConstantPositionMobilityModel>();
        mobility->SetPosition(Vector(-10.0 + 2.0 * index, 0.0, 1.5));
        uavs.Get(index)->AggregateObject(mobility);
    }

    SpectrumWifiPhyHelper phy;
    phy.SetChannel(channel);
    phy.SetErrorRateModel("ns3::NistErrorRateModel");
    phy.Set("TxPowerStart", DoubleValue(20.0));
    phy.Set("TxPowerEnd", DoubleValue(20.0));
    phy.Set("ChannelSettings", StringValue("{0, 20, BAND_2_4GHZ, 0}"));
    WifiHelper wifi;
    wifi.SetStandard(WIFI_STANDARD_80211n);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                 "DataMode",
                                 StringValue("HtMcs0"),
                                 "ControlMode",
                                 StringValue("HtMcs0"));
    WifiMacHelper mac;
    Ssid ssid("sionna-five-uav");
    mac.SetType("ns3::StaWifiMac",
                "QosSupported",
                BooleanValue(true),
                "Ssid",
                SsidValue(ssid));
    NetDeviceContainer uavDevices = wifi.Install(phy, mac, uavs);
    mac.SetType("ns3::ApWifiMac",
                "QosSupported",
                BooleanValue(true),
                "Ssid",
                SsidValue(ssid));
    NetDeviceContainer gcsDevice = wifi.Install(phy, mac, gcs);
    for (uint32_t index = 0; index < uavDevices.GetN(); ++index)
    {
        AttachArray(uavDevices.Get(index));
    }
    AttachArray(gcsDevice.Get(0));

    InternetStackHelper stack;
    stack.Install(gcs);
    stack.Install(uavs);
    Ipv4AddressHelper addresses;
    addresses.SetBase("10.94.0.0", "255.255.255.0");
    Ipv4InterfaceContainer uavInterfaces = addresses.Assign(uavDevices);
    addresses.Assign(gcsDevice);
    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        g_uavAddresses.push_back(uavInterfaces.GetAddress(index));
    }

    Ptr<Socket> gcsSocket = Socket::CreateSocket(gcs.Get(0), UdpSocketFactory::GetTypeId());
    NS_ABORT_MSG_IF(gcsSocket->Bind(InetSocketAddress(Ipv4Address::GetAny(), kControlPort)) != 0,
                    "failed to bind GCS control socket");
    gcsSocket->SetIpTos(0xc0);
    gcsSocket->SetRecvCallback(MakeCallback(&GcsReceive));
    std::vector<Ptr<Socket>> uavSockets;
    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        Ptr<Socket> socket = Socket::CreateSocket(uavs.Get(index), UdpSocketFactory::GetTypeId());
        NS_ABORT_MSG_IF(socket->Bind(InetSocketAddress(Ipv4Address::GetAny(), kControlPort)) != 0,
                        "failed to bind UAV control socket");
        socket->SetIpTos(0xc0);
        socket->SetRecvCallback(MakeBoundCallback(&UavReceive, index));
        uavSockets.push_back(socket);
    }

    for (uint32_t round = 0; round < rounds; ++round)
    {
        Simulator::Schedule(Seconds(trafficStartS + round * roundIntervalMs / 1000.0),
                            &SendRound,
                            gcsSocket,
                            round);
    }

    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        Ptr<WifiNetDevice> device = DynamicCast<WifiNetDevice>(uavDevices.Get(index));
        device->GetMac()->TraceConnectWithoutContext("MacTx", MakeCallback(&MacTx));
        device->GetMac()->TraceConnectWithoutContext("MacTxDrop", MakeCallback(&MacTxDrop));
        device->GetPhy()->TraceConnectWithoutContext("PhyTxBegin", MakeCallback(&PhyTx));
        device->GetRemoteStationManager()->TraceConnectWithoutContext("MacTxDataFailed",
                                                                       MakeCallback(&MacDataRetry));
    }
    Ptr<WifiNetDevice> gcsWifi = DynamicCast<WifiNetDevice>(gcsDevice.Get(0));
    gcsWifi->GetMac()->TraceConnectWithoutContext("MacTx", MakeCallback(&MacTx));
    gcsWifi->GetMac()->TraceConnectWithoutContext("MacTxDrop", MakeCallback(&MacTxDrop));
    gcsWifi->GetPhy()->TraceConnectWithoutContext("PhyTxBegin", MakeCallback(&PhyTx));
    gcsWifi->GetRemoteStationManager()->TraceConnectWithoutContext("MacTxDataFailed",
                                                                    MakeCallback(&MacDataRetry));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/MonitorSnifferRx",
                                  MakeCallback(&MonitorSniffRx));

    rusage usageBefore{};
    rusage usageAfter{};
    getrusage(RUSAGE_SELF, &usageBefore);
    const auto wallStart = std::chrono::steady_clock::now();
    int result = 0;
    std::string error;
    try
    {
        Simulator::ScheduleNow(&SampleLag);
        Simulator::Stop(Seconds(simulationSeconds));
        Simulator::Run();
    }
    catch (const py::error_already_set& exception)
    {
        result = 2;
        error = exception.what();
    }
    catch (const std::exception& exception)
    {
        result = 3;
        error = exception.what();
    }
    const auto wallEnd = std::chrono::steady_clock::now();
    getrusage(RUSAGE_SELF, &usageAfter);
    const uint64_t eventCount = Simulator::GetEventCount();
    Simulator::Destroy();

    std::vector<double> allRtt;
    uint64_t offeredTotal = 0;
    uint64_t ackedTotal = 0;
    double fairnessSum = 0.0;
    double fairnessSquareSum = 0.0;
    bool noStarvation = true;
    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        offeredTotal += g_offered[index];
        ackedTotal += g_acked[index];
        fairnessSum += g_acked[index];
        fairnessSquareSum += static_cast<double>(g_acked[index]) * g_acked[index];
        noStarvation = noStarvation && g_acked[index] > 0;
        allRtt.insert(allRtt.end(), g_rttMs[index].begin(), g_rttMs[index].end());
    }
    const double pdr = offeredTotal == 0 ? 0.0 : static_cast<double>(ackedTotal) / offeredTotal;
    const double fairness = fairnessSquareSum == 0.0
                                ? 0.0
                                : fairnessSum * fairnessSum /
                                      (kUavCount * fairnessSquareSum);
    const double rttP95Ms = Percentile95(allRtt);
    const double lagP95Ms = Percentile95(g_profileLagMs);
    const double startupLagP95Ms = Percentile95(g_allLagMs);
    const double wallSeconds = std::chrono::duration<double>(wallEnd - wallStart).count();
    const double cpuSeconds = CpuSeconds(usageAfter) - CpuSeconds(usageBefore);
    const double cpuPercent = wallSeconds == 0.0 ? 0.0 : 100.0 * cpuSeconds / wallSeconds;
    const double meanRtf = wallSeconds == 0.0 ? 0.0 : simulationSeconds / wallSeconds;
    const double eventsPerAck = ackedTotal == 0 ? 0.0 : static_cast<double>(eventCount) / ackedTotal;
    const bool pass = result == 0 && pdr >= 0.99 && rttP95Ms <= 250.0 && lagP95Ms <= 50.0 &&
                      noStarvation && fairness >= 0.99 && meanRtf >= 0.95 && g_macTxDrop == 0;

    std::ofstream json(output, std::ios::out | std::ios::trunc);
    if (!json.is_open())
    {
        std::cerr << "failed to open JSON output: " << output << std::endl;
        return 5;
    }
    json << std::fixed << std::setprecision(3);
    json << "{\n"
         << "  \"schema_version\": 1,\n"
         << "  \"profile\": \"native_wifi_80211n_spectrum_reference_v1\",\n"
         << "  \"uav_count\": 5,\n"
         << "  \"radio\": \"ns3::SpectrumWifiPhy\",\n"
         << "  \"mac\": \"802.11n infrastructure BSS with QoS\",\n"
         << "  \"control_ip_tos\": 192,\n"
         << "  \"propagation\": \"ns3::SionnaRtSpectrumPropagationLossModel\",\n"
         << "  \"sionna_enabled\": true,\n"
         << "  \"scalar_fallback\": false,\n"
         << "  \"application_bypass\": false,\n"
         << "  \"scene\": \"" << scene << "\",\n"
         << "  \"warmup_s\": " << trafficStartS << ",\n"
         << "  \"drain_s\": " << simulationSeconds - trafficEndS << ",\n"
         << "  \"offered_packets\": " << offeredTotal << ",\n"
         << "  \"acked_packets\": " << ackedTotal << ",\n"
         << "  \"control_pdr\": " << pdr << ",\n"
         << "  \"control_rtt_p95_ms\": " << rttP95Ms << ",\n"
         << "  \"scheduler_lag_profile_p95_ms\": " << lagP95Ms << ",\n"
         << "  \"scheduler_lag_including_warmup_p95_ms\": " << startupLagP95Ms << ",\n"
         << "  \"wall_runtime_s\": " << wallSeconds << ",\n"
         << "  \"mean_rtf\": " << meanRtf << ",\n"
         << "  \"cpu_seconds\": " << cpuSeconds << ",\n"
         << "  \"cpu_percent_one_core\": " << cpuPercent << ",\n"
         << "  \"ns3_events\": " << eventCount << ",\n"
         << "  \"ns3_events_per_acked_logical_packet\": " << eventsPerAck << ",\n"
         << "  \"wifi_mac_tx\": " << g_macTx << ",\n"
         << "  \"wifi_mac_tx_drop\": " << g_macTxDrop << ",\n"
         << "  \"wifi_phy_tx_frames\": " << g_phyTx << ",\n"
         << "  \"wifi_mac_data_retry_events\": " << g_macDataRetry << ",\n"
         << "  \"wifi_monitor_rx_samples\": " << g_monitorSamples << ",\n"
         << "  \"mean_signal_dbm\": " << g_signalDbmMean << ",\n"
         << "  \"mean_noise_dbm\": " << g_noiseDbmMean << ",\n"
         << "  \"jain_fairness\": " << fairness << ",\n"
         << "  \"no_starvation\": " << (noStarvation ? "true" : "false") << ",\n"
         << "  \"per_uav\": [\n";
    for (uint32_t index = 0; index < kUavCount; ++index)
    {
        json << "    {\"uav\": " << index + 1 << ", \"offered\": " << g_offered[index]
             << ", \"acked\": " << g_acked[index] << ", \"rtt_p95_ms\": "
             << Percentile95(g_rttMs[index]) << "}";
        json << (index + 1 == kUavCount ? "\n" : ",\n");
    }
    json << "  ],\n"
         << "  \"exit_code\": " << result << ",\n"
         << "  \"pass\": " << (pass ? "true" : "false");
    if (!error.empty())
    {
        json << ",\n  \"error\": \"simulation exception; see stderr\"";
    }
    json << "\n}\n";
    json.close();

    if (!error.empty())
    {
        std::cerr << error << std::endl;
    }
    std::cout << "SIONNA_WIFI_FIVE_UAV offered=" << offeredTotal << " acked=" << ackedTotal
              << " pdr=" << pdr << " rtt_p95_ms=" << rttP95Ms
              << " lag_p95_ms=" << lagP95Ms << " fairness=" << fairness
              << " mean_rtf=" << meanRtf << " pass=" << (pass ? "true" : "false")
              << std::endl;
    return pass ? 0 : (result == 0 ? 4 : result);
}
