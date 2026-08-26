#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/tap-bridge-module.h"

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsTapVerticalSlice");

namespace
{

struct RadioStats
{
    uint64_t macTx{0};
    uint64_t macTxBytes{0};
    uint64_t macTxDrop{0};
    uint64_t macTxBackoff{0};
    uint64_t phyTxDrop{0};
    uint64_t phyRxEnd{0};
};

std::vector<RadioStats> g_radioStats;

void
CountMacTx(uint32_t index, Ptr<const Packet> packet)
{
    g_radioStats.at(index).macTx++;
    g_radioStats.at(index).macTxBytes += packet->GetSize();
}

void
CountMacTxDrop(uint32_t index, Ptr<const Packet>)
{
    g_radioStats.at(index).macTxDrop++;
}

void
CountMacTxBackoff(uint32_t index, Ptr<const Packet>)
{
    g_radioStats.at(index).macTxBackoff++;
}

void
CountPhyTxDrop(uint32_t index, Ptr<const Packet>)
{
    g_radioStats.at(index).phyTxDrop++;
}

void
CountPhyRxEnd(uint32_t index, Ptr<const Packet>)
{
    g_radioStats.at(index).phyRxEnd++;
}

std::vector<std::string>
SplitCsv(const std::string& value)
{
    std::vector<std::string> items;
    std::stringstream stream(value);
    std::string item;
    while (std::getline(stream, item, ','))
    {
        if (!item.empty())
        {
            items.push_back(item);
        }
    }
    return items;
}

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

void
WriteStats(const std::string& statsFile, const std::vector<std::string>& deviceNames)
{
    if (statsFile.empty())
    {
        return;
    }
    uint64_t totalBackoff = 0;
    uint64_t totalDrop = 0;
    std::ofstream output(statsFile, std::ios::out | std::ios::trunc);
    output << "{\n  \"radio_devices\": [\n";
    for (std::size_t index = 0; index < g_radioStats.size(); ++index)
    {
        const auto& stats = g_radioStats[index];
        totalBackoff += stats.macTxBackoff;
        totalDrop += stats.macTxDrop + stats.phyTxDrop;
        output << "    {\"name\": \"" << deviceNames[index] << "\", "
               << "\"mac_tx\": " << stats.macTx << ", "
               << "\"mac_tx_bytes\": " << stats.macTxBytes << ", "
               << "\"mac_tx_drop\": " << stats.macTxDrop << ", "
               << "\"mac_tx_backoff\": " << stats.macTxBackoff << ", "
               << "\"phy_tx_drop\": " << stats.phyTxDrop << ", "
               << "\"phy_rx_end\": " << stats.phyRxEnd << "}"
               << (index + 1 == g_radioStats.size() ? "\n" : ",\n");
    }
    output << "  ],\n  \"backoff_events\": " << totalBackoff
           << ",\n  \"drop_events\": " << totalDrop << "\n}\n";
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
    std::string tapUavs;
    std::string pcapPrefix = "ams-tap";
    std::string readyFile;
    std::string stopFile;
    std::string statsFile;
    double durationSeconds = 3600.0;
    std::string radioRate = "1Mbps";
    std::string radioDelay = "5ms";
    uint32_t queueMaxPackets = 20;

    CommandLine command(__FILE__);
    command.AddValue("tapGcs", "Existing GCS-side TAP device", tapGcs);
    command.AddValue("tapUav", "Existing single UAV TAP device", tapUav);
    command.AddValue("tapUavs", "Comma-separated UAV TAP devices on one shared medium", tapUavs);
    command.AddValue("pcapPrefix", "PCAP output prefix", pcapPrefix);
    command.AddValue("readyFile", "Readiness marker written after device start", readyFile);
    command.AddValue("stopFile", "Stop marker polled in real time", stopFile);
    command.AddValue("statsFile", "JSON packet/medium counters written at shutdown", statsFile);
    command.AddValue("duration", "Maximum real-time duration in seconds", durationSeconds);
    command.AddValue("radioRate", "Modeled radio segment data rate", radioRate);
    command.AddValue("radioDelay", "Modeled radio segment propagation delay", radioDelay);
    command.AddValue("queueMaxPackets", "Per-device shared-medium queue bound", queueMaxPackets);
    command.Parse(argc, argv);

    std::vector<std::string> uavTaps = SplitCsv(tapUavs);
    if (uavTaps.empty())
    {
        uavTaps.push_back(tapUav);
    }

    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    Ptr<Node> ghostGcs = CreateObject<Node>();
    Ptr<Node> router = CreateObject<Node>();
    NodeContainer ghostUavs;
    ghostUavs.Create(uavTaps.size());

    NodeContainer gcsSegment(ghostGcs, router);
    NodeContainer radioSegment;
    radioSegment.Add(router);
    radioSegment.Add(ghostUavs);

    CsmaHelper ingress;
    ingress.SetChannelAttribute("DataRate", StringValue("1Gbps"));
    ingress.SetChannelAttribute("Delay", StringValue("10us"));
    NetDeviceContainer gcsDevices = ingress.Install(gcsSegment);

    CsmaHelper radio;
    radio.SetChannelAttribute("DataRate", StringValue(radioRate));
    radio.SetChannelAttribute("Delay", StringValue(radioDelay));
    radio.SetQueue("ns3::DropTailQueue",
                   "MaxSize",
                   StringValue(std::to_string(queueMaxPackets) + "p"));
    NetDeviceContainer radioDevices = radio.Install(radioSegment);

    gcsDevices.Get(1)->SetAddress(Mac48Address("02:71:00:00:00:01"));
    radioDevices.Get(0)->SetAddress(Mac48Address("02:71:01:00:00:01"));

    InternetStackHelper internet;
    internet.Install(router);
    AddRouterAddress(router, gcsDevices.Get(1), "10.71.0.1");
    AddRouterAddress(router, radioDevices.Get(0), "10.71.1.1");

    TapBridgeHelper tapBridge;
    tapBridge.SetAttribute("Mode", StringValue("UseBridge"));
    tapBridge.SetAttribute("DeviceName", StringValue(tapGcs));
    tapBridge.Install(ghostGcs, gcsDevices.Get(0));
    for (std::size_t index = 0; index < uavTaps.size(); ++index)
    {
        tapBridge.SetAttribute("DeviceName", StringValue(uavTaps[index]));
        tapBridge.Install(ghostUavs.Get(index), radioDevices.Get(index + 1));
    }

    g_radioStats.resize(radioDevices.GetN());
    std::vector<std::string> radioDeviceNames{"router"};
    radioDeviceNames.insert(radioDeviceNames.end(), uavTaps.begin(), uavTaps.end());
    for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
    {
        radioDevices.Get(index)->TraceConnectWithoutContext(
            "MacTx", MakeBoundCallback(&CountMacTx, index));
        radioDevices.Get(index)->TraceConnectWithoutContext(
            "MacTxDrop", MakeBoundCallback(&CountMacTxDrop, index));
        radioDevices.Get(index)->TraceConnectWithoutContext(
            "MacTxBackoff", MakeBoundCallback(&CountMacTxBackoff, index));
        radioDevices.Get(index)->TraceConnectWithoutContext(
            "PhyTxDrop", MakeBoundCallback(&CountPhyTxDrop, index));
        radioDevices.Get(index)->TraceConnectWithoutContext(
            "PhyRxEnd", MakeBoundCallback(&CountPhyRxEnd, index));
    }

    ingress.EnablePcap(pcapPrefix + "-gcs.pcap", gcsDevices.Get(0), true, true);
    ingress.EnablePcap(pcapPrefix + "-router-gcs.pcap", gcsDevices.Get(1), true, true);
    radio.EnablePcap(pcapPrefix + "-radio-router.pcap", radioDevices.Get(0), true, true);
    for (std::size_t index = 0; index < uavTaps.size(); ++index)
    {
        radio.EnablePcap(pcapPrefix + "-radio-uav" + std::to_string(index + 1) + ".pcap",
                         radioDevices.Get(index + 1),
                         true,
                         true);
    }

    Simulator::Schedule(MilliSeconds(100), &WriteReadyFile, readyFile);
    Simulator::Schedule(MilliSeconds(100), &PollStopFile, stopFile);
    Simulator::Stop(Seconds(durationSeconds));
    Simulator::Run();
    WriteStats(statsFile, radioDeviceNames);
    Simulator::Destroy();
    return 0;
}
