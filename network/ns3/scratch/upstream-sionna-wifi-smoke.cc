/*
 * Minimal native SpectrumWifiPhy + Sionna RT integration smoke.
 *
 * There is deliberately no scalar propagation-loss fallback in this topology:
 * every Wi-Fi reception traverses SionnaRtSpectrumPropagationLossModel.
 */

#include "pybind11/pybind11.h"

#include "ns3/boolean.h"
#include "ns3/command-line.h"
#include "ns3/constant-position-mobility-model.h"
#include "ns3/core-module.h"
#include "ns3/internet-stack-helper.h"
#include "ns3/isotropic-antenna-model.h"
#include "ns3/ipv4-address-helper.h"
#include "ns3/multi-model-spectrum-channel.h"
#include "ns3/network-module.h"
#include "ns3/sionna-rt-channel-model.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-wifi-helper.h"
#include "ns3/spectrum-wifi-phy.h"
#include "ns3/ssid.h"
#include "ns3/udp-client-server-helper.h"
#include "ns3/udp-server.h"
#include "ns3/uniform-planar-array.h"
#include "ns3/wifi-module.h"
#include "ns3/wifi-net-device.h"

#include "native-spectrum-sources.h"
#include "native-radio-map.h"
#include "native-cache-study.h"
#include "ns3/constant-velocity-mobility-model.h"
#include <fstream>
#include <iomanip>
#include <iostream>

namespace py = pybind11;
using namespace ns3;

namespace
{

std::ofstream g_measurements;
uint32_t g_monitorSamples = 0;
double g_signalDbmMean = 0.0;
double g_noiseDbmMean = 0.0;

void
MonitorSniffRx(Ptr<const Packet>,
               uint16_t,
               WifiTxVector,
               MpduInfo,
               SignalNoiseDbm signalNoise,
               uint16_t)
{
    g_measurements << Simulator::Now().GetSeconds() << ",decoded_mpdu,aggregate," << signalNoise.signal << "," << signalNoise.noise << "\n";
    ++g_monitorSamples;
    g_signalDbmMean += (signalNoise.signal - g_signalDbmMean) / g_monitorSamples;
    g_noiseDbmMean += (signalNoise.noise - g_noiseDbmMean) / g_monitorSamples;
}

void SignalArrival(std::string rx, Ptr<const SpectrumSignalParameters> signal,
                   uint32_t sender, double power, Time duration)
{
    g_measurements << Simulator::Now().GetSeconds() << ','
        << (DynamicCast<WaveformGenerator>(signal->txPhy) ? "foreign_signal" : "wifi_signal")
        << ',' << rx << ',' << power << ',' << duration.GetSeconds() << ',' << sender << '\n';
}
void ApplicationRx(Ptr<const Packet> packet, const Address&)
{
    g_measurements << Simulator::Now().GetSeconds() << ",application_rx,sta," << packet->GetSize() << '\n';
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
    Ptr<AntennaModel> wifiAntenna = CreateObject<IsotropicAntennaModel>();
    phy->SetAntenna(wifiAntenna);
    wifiAntenna->AggregateObject(CreateArray());
}

} // namespace

int
main(int argc, char* argv[])
{
    py::scoped_interpreter python{};

    std::string scene = "simple_street_canyon_with_cars";
    std::string output = "sionna-wifi-smoke.json";
    std::string sources;
    std::string heatmapCsv;
    std::string cacheStudyCsv;
    std::string propagationProfile="sionna";
    uint32_t staCount=1;
    double velocityY=0, updatePeriodS=20, updateDistanceM=10, altitudeM=2, apX=5;
    double heatmapTimeS = 4.0;
    double simulationSeconds = 4.0;
    uint32_t offeredPackets = 20;
    uint32_t packetSize = 128;
    double distanceM = 20.0;

    CommandLine command(__FILE__);
    command.AddValue("cacheStudyCsv", "Short obstacle-motion cache comparison", cacheStudyCsv);
    command.AddValue("propagationProfile", "Explicit sionna, friis or hybrid", propagationProfile);
    command.AddValue("staCount", "Radio-only STAs, not SITL", staCount);
    command.AddValue("velocityY", "STA velocity on y, m/s", velocityY);
    command.AddValue("updatePeriodS", "Declared channel cache period", updatePeriodS);
    command.AddValue("updateDistanceM", "Declared channel cache displacement", updateDistanceM);
    command.AddValue("altitudeM", "AP and STA altitude", altitudeM);
    command.AddValue("apX", "AP x position", apX);
    command.AddValue("heatmapCsv", "Offline native PSD map CSV instead of packets", heatmapCsv);
    command.AddValue("heatmapTimeS", "Instantaneous source time for map", heatmapTimeS);
    command.AddValue("sources", "Resolved native waveform source JSON", sources);
    command.AddValue("scene", "Sionna RT built-in scene", scene);
    command.AddValue("output", "JSON result path", output);
    command.AddValue("simulationSeconds", "Simulation duration", simulationSeconds);
    command.AddValue("offeredPackets", "UDP packets offered after association", offeredPackets);
    command.AddValue("packetSize", "UDP payload bytes", packetSize);
    command.AddValue("distanceM", "AP to STA distance", distanceM);
    command.Parse(argc, argv);
    NS_ABORT_MSG_IF(staCount<1 || staCount>32 || (propagationProfile!="sionna" && propagationProfile!="friis" && propagationProfile!="hybrid"), "invalid explicit experiment profile");
    g_measurements.open(output + ".events.csv");
    g_measurements << "time_s,event,receiver,value,noise_or_duration,sender\n";

    NS_ABORT_MSG_IF(simulationSeconds <= 2.5 || offeredPackets == 0,
                    "simulationSeconds must exceed 2.5 and offeredPackets must be positive");

    Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod", TimeValue(Seconds(updatePeriodS)));

    Ptr<MultiModelSpectrumChannel> channel = CreateObject<MultiModelSpectrumChannel>();
    channel->SetPropagationDelayModel(CreateObject<ConstantSpeedPropagationDelayModel>());
    Ptr<SionnaRtSpectrumPropagationLossModel> sionna =
        CreateObject<SionnaRtSpectrumPropagationLossModel>();
    sionna->SetChannelModelAttribute("UpdateDistanceThreshold", DoubleValue(updateDistanceM));
    sionna->SetChannelModelAttribute("Frequency", DoubleValue(2.412e9));
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
    Ptr<bas::SourcePropagation> sourceRouter;
    if (!cacheStudyCsv.empty()) { bas::CacheStudy(cacheStudyCsv,scene,solver); return 0; }
    if (sources.empty() && propagationProfile=="sionna")
        channel->AddPhasedArraySpectrumPropagationLossModel(sionna);
    else
    {
        if (sources.empty()) { sourceRouter=CreateObject<bas::SourcePropagation>(); sourceRouter->reference=sionna; }
        else sourceRouter = bas::InstallSpectrumSources(
            sionna, channel, sources, scene, solver, 90,
            [](const std::string& event, const std::string& id, double power, const std::string& detail) {
                std::cout << Simulator::Now().GetSeconds() << ',' << event << ',' << id << ','
                          << power << ',' << detail << std::endl;
            });
        sourceRouter->profile=propagationProfile;
        channel->AddPhasedArraySpectrumPropagationLossModel(sourceRouter);
    }

    NodeContainer apNode;
    apNode.Create(1);
    NodeContainer staNode;
    staNode.Create(staCount);

    Ptr<ConstantPositionMobilityModel> apMobility =
        CreateObject<ConstantPositionMobilityModel>();
    apMobility->SetPosition(Vector(apX, 0.0, altitudeM));
    apNode.Get(0)->AggregateObject(apMobility);
    for (uint32_t i=0;i<staCount;++i) {
        auto mobility=CreateObject<ConstantVelocityMobilityModel>();
        mobility->SetPosition(Vector(apX+distanceM+i*5,0,altitudeM));
        mobility->SetVelocity(Vector(0,velocityY,0)); staNode.Get(i)->AggregateObject(mobility);
    }

    SpectrumWifiPhyHelper phy;
    phy.SetChannel(channel);
    phy.SetErrorRateModel("ns3::NistErrorRateModel");
    phy.Set("TxPowerStart", DoubleValue(10.0));
    phy.Set("TxPowerEnd", DoubleValue(10.0));
    phy.Set("ChannelSettings", StringValue("{1, 20, BAND_2_4GHZ, 0}"));

    WifiHelper wifi;
    wifi.SetStandard(WIFI_STANDARD_80211n);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                 "DataMode",
                                 StringValue("HtMcs0"),
                                 "ControlMode",
                                 StringValue("ErpOfdmRate6Mbps"));
    WifiMacHelper mac;
    Ssid ssid("sionna-native-wifi");
    mac.SetType("ns3::StaWifiMac", "Ssid", SsidValue(ssid));
    NetDeviceContainer staDevice = wifi.Install(phy, mac, staNode);
    mac.SetType("ns3::ApWifiMac", "Ssid", SsidValue(ssid));
    NetDeviceContainer apDevice = wifi.Install(phy, mac, apNode);
    for (uint32_t i=0;i<staCount;++i) AttachArray(staDevice.Get(i));
    AttachArray(apDevice.Get(0));
    DynamicCast<WifiNetDevice>(apDevice.Get(0))->GetPhy()->TraceConnectWithoutContext(
        "SignalArrival", MakeBoundCallback(&SignalArrival, std::string("ap")));
    DynamicCast<WifiNetDevice>(staDevice.Get(0))->GetPhy()->TraceConnectWithoutContext(
        "SignalArrival", MakeBoundCallback(&SignalArrival, std::string("sta")));

    if (!heatmapCsv.empty())
    {
        auto antenna = DynamicCast<SpectrumWifiPhy>(DynamicCast<WifiNetDevice>(apDevice.Get(0))->GetPhy())->GetAntenna()->GetObject<PhasedArrayModel>();
        bas::WriteRadioMap(heatmapCsv, sionna, sourceRouter, apMobility, antenna, heatmapTimeS);
        Simulator::Destroy();
        return 0;
    }

    InternetStackHelper stack;
    stack.Install(apNode);
    stack.Install(staNode);
    Ipv4AddressHelper addresses;
    addresses.SetBase("10.88.0.0", "255.255.255.0");
    Ipv4InterfaceContainer staInterface = addresses.Assign(staDevice);
    addresses.Assign(apDevice);

    constexpr uint16_t port = 9088;
    UdpServerHelper server(port);
    ApplicationContainer serverApp = server.Install(staNode);
    serverApp.Get(0)->TraceConnectWithoutContext("Rx", MakeCallback(&ApplicationRx));
    serverApp.Start(Seconds(0.5));
    serverApp.Stop(Seconds(simulationSeconds));
    for (uint32_t i=0;i<staCount;++i) {
    UdpClientHelper client(staInterface.GetAddress(i), port);
    client.SetAttribute("MaxPackets", UintegerValue(offeredPackets));
    const double sendWindowSeconds = simulationSeconds - 2.25;
    client.SetAttribute("Interval", TimeValue(Seconds(sendWindowSeconds / offeredPackets)));
    client.SetAttribute("PacketSize", UintegerValue(packetSize));
    ApplicationContainer clientApp = client.Install(apNode.Get(0));
    clientApp.Start(Seconds(2.0));
    clientApp.Stop(Seconds(simulationSeconds - 0.1));
    }

    phy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
    phy.EnablePcap(output + ".radio", apDevice);
    phy.EnablePcap(output + ".radio", staDevice);
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/MonitorSnifferRx",
                                  MakeCallback(&MonitorSniffRx));

    int result = 0;
    std::string error;
    try
    {
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

    uint64_t deliveredPackets = 0;
    for (uint32_t i=0;i<staCount;++i) deliveredPackets+=DynamicCast<UdpServer>(serverApp.Get(i))->GetReceived();
    Simulator::Destroy();

    std::ofstream json(output, std::ios::out | std::ios::trunc);
    json << "{\n"
         << "  \"selected_profile\": \"" << propagationProfile << "\",\n"
         << "  \"radio_only_sta_count\": " << staCount << ",\n"
         << "  \"velocity_y_m_s\": " << velocityY << ",\n"
         << "  \"schema_version\": 1,\n"
         << "  \"radio\": \"ns3::SpectrumWifiPhy\",\n"
         << "  \"propagation\": \"" << (propagationProfile=="sionna" ? "ns3::SionnaRtSpectrumPropagationLossModel" : "bas::ExplicitNativeSionnaFriisSelection") << "\",\n"
         << "  \"scalar_fallback\": false,\n"
         << "  \"scene\": \"" << scene << "\",\n"
         << "  \"offered_packets\": " << offeredPackets*staCount << ",\n"
         << "  \"delivered_packets\": " << deliveredPackets << ",\n"
         << "  \"monitor_rx_samples\": " << g_monitorSamples << ",\n"
         << std::fixed << std::setprecision(3)
         << "  \"mean_signal_dbm\": " << (g_monitorSamples ? std::to_string(g_signalDbmMean) : "null") << ",\n"
         << "  \"mean_noise_dbm\": " << (g_monitorSamples ? std::to_string(g_noiseDbmMean) : "null") << ",\n"
         << "  \"exit_code\": " << result;
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
    if (result == 0 && deliveredPackets == 0)
    {
        std::cerr << "SpectrumWifiPhy + Sionna ran but delivered no UDP packets" << std::endl;
        return 4;
    }
    std::cout << "SIONNA_WIFI_SMOKE offered=" << offeredPackets
              << " delivered=" << deliveredPackets << " monitor_rx=" << g_monitorSamples
              << " scalar_fallback=false" << std::endl;
    return result;
}
