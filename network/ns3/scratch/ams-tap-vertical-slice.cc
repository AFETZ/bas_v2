#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/tap-bridge-module.h"

#include <fstream>
#include <string>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsTapVerticalSlice");

namespace
{

void
PollStopFile(const std::string& stopFile)
{
    if (!stopFile.empty() && std::ifstream(stopFile).good())
    {
        Simulator::Stop();
        return;
    }
    Simulator::Schedule(MilliSeconds(100), &PollStopFile, stopFile);
}

void
WriteReadyFile(const std::string& readyFile)
{
    if (readyFile.empty())
    {
        return;
    }
    std::ofstream output(readyFile, std::ios::out | std::ios::trunc);
    output << "ready\n";
}

uint32_t
AddRouterAddress(Ptr<Node> router, Ptr<NetDevice> device, const char* address)
{
    Ptr<Ipv4> ipv4 = router->GetObject<Ipv4>();
    uint32_t interface = ipv4->AddInterface(device);
    ipv4->AddAddress(interface,
                     Ipv4InterfaceAddress(Ipv4Address(address), Ipv4Mask("255.255.255.0")));
    ipv4->SetMetric(interface, 1);
    ipv4->SetUp(interface);
    ipv4->SetForwarding(interface, true);
    return interface;
}

} // namespace

int
main(int argc, char* argv[])
{
    std::string tapGcs = "tap-gcs";
    std::string tapUav = "tap-uav";
    std::string pcapPrefix = "ams-tap";
    std::string readyFile;
    std::string stopFile;
    double durationSeconds = 3600.0;
    std::string radioRate = "1Mbps";
    std::string radioDelay = "5ms";

    CommandLine command(__FILE__);
    command.AddValue("tapGcs", "Existing GCS-side TAP device", tapGcs);
    command.AddValue("tapUav", "Existing UAV-side TAP device", tapUav);
    command.AddValue("pcapPrefix", "PCAP output prefix", pcapPrefix);
    command.AddValue("readyFile", "Readiness marker written after device start", readyFile);
    command.AddValue("stopFile", "Stop marker polled in real time", stopFile);
    command.AddValue("duration", "Maximum real-time duration in seconds", durationSeconds);
    command.AddValue("radioRate", "Modeled radio segment data rate", radioRate);
    command.AddValue("radioDelay", "Modeled radio segment propagation delay", radioDelay);
    command.Parse(argc, argv);

    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    Ptr<Node> ghostGcs = CreateObject<Node>();
    Ptr<Node> router = CreateObject<Node>();
    Ptr<Node> ghostUav = CreateObject<Node>();

    NodeContainer gcsSegment(ghostGcs, router);
    NodeContainer uavSegment(router, ghostUav);

    CsmaHelper ingress;
    ingress.SetChannelAttribute("DataRate", StringValue("1Gbps"));
    ingress.SetChannelAttribute("Delay", StringValue("10us"));
    NetDeviceContainer gcsDevices = ingress.Install(gcsSegment);

    CsmaHelper radio;
    radio.SetChannelAttribute("DataRate", StringValue(radioRate));
    radio.SetChannelAttribute("Delay", StringValue(radioDelay));
    NetDeviceContainer uavDevices = radio.Install(uavSegment);

    gcsDevices.Get(1)->SetAddress(Mac48Address("02:71:00:00:00:01"));
    uavDevices.Get(0)->SetAddress(Mac48Address("02:71:01:00:00:01"));

    InternetStackHelper internet;
    internet.Install(router);
    AddRouterAddress(router, gcsDevices.Get(1), "10.71.0.1");
    AddRouterAddress(router, uavDevices.Get(0), "10.71.1.1");

    TapBridgeHelper tapBridge;
    tapBridge.SetAttribute("Mode", StringValue("UseBridge"));
    tapBridge.SetAttribute("DeviceName", StringValue(tapGcs));
    tapBridge.Install(ghostGcs, gcsDevices.Get(0));
    tapBridge.SetAttribute("DeviceName", StringValue(tapUav));
    tapBridge.Install(ghostUav, uavDevices.Get(1));

    ingress.EnablePcap(pcapPrefix + "-gcs.pcap", gcsDevices.Get(0), true, true);
    ingress.EnablePcap(pcapPrefix + "-router-gcs.pcap", gcsDevices.Get(1), true, true);
    radio.EnablePcap(pcapPrefix + "-router-uav.pcap", uavDevices.Get(0), true, true);
    radio.EnablePcap(pcapPrefix + "-uav.pcap", uavDevices.Get(1), true, true);

    Simulator::Schedule(MilliSeconds(100), &WriteReadyFile, readyFile);
    Simulator::Schedule(MilliSeconds(100), &PollStopFile, stopFile);
    Simulator::Stop(Seconds(durationSeconds));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
