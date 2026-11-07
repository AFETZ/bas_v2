// CPU integration regression for the production TAP -> Wi-Fi QoS boundary.
// Compile as an ns-3 scratch target against the pinned private runtime, then run:
//   sudo unshare --net -- <absolute binary> <new output directory>
// The isolated namespace owns one non-persistent TAP. No GPU, SITL, physical
// UART, modem, realtime scheduler or customer latency criterion is exercised.
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/ethernet-header.h"
#include "ns3/llc-snap-header.h"
#include "ns3/mobility-module.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/wifi-module.h"
#include <arpa/inet.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <vector>

using namespace ns3;
namespace fs = std::filesystem;

namespace
{
constexpr uint8_t kControlTos = 184, kPayloadTos = 40;
constexpr uint16_t kControlPort = 14600, kPayloadPort = 14700;
const Ipv4Address kGcsIp("10.249.254.1"), kUavIp("10.249.254.2");
std::ofstream g_trace;
std::vector<std::string> g_failures;
std::set<std::string> g_failureSet;
std::map<std::vector<uint8_t>, unsigned> g_exact;
std::map<std::pair<uint32_t, uint64_t>, Time> g_entered;
std::array<std::array<uint64_t, 2>, 2> g_enqueued{}; // node, control/payload
std::array<uint64_t, 2> g_uplinkReceived{}, g_downlinkReceived{};
std::array<uint32_t, 2> g_maxPayloadDepth{};
std::array<uint64_t, 2> g_payloadDrops{};
std::array<Ptr<Socket>, 2> g_downlink;
std::array<Ptr<Socket>, 2> g_uplink;
std::array<int, 2> g_linuxReceivers{-1, -1};
int g_raw = -1;
unsigned g_interface = 0;
Mac48Address g_gcsMac, g_uavMac;
Ptr<TapBridge> g_tap;
uint16_t g_identification = 1;

void Check(bool ok, const std::string& reason)
{
    if (!ok && g_failureSet.insert(reason).second) g_failures.push_back(reason);
}

std::vector<uint8_t> Bytes(Ptr<const Packet> packet)
{
    std::vector<uint8_t> bytes(packet->GetSize());
    packet->CopyData(bytes.data(), bytes.size());
    return bytes;
}

void ObserveQueue(uint32_t node, AcIndex ac, Ptr<WifiMacQueue> queue,
                  std::string event, Ptr<const WifiMpdu> mpdu)
{
    auto packet = mpdu->GetPacket()->Copy();
    LlcSnapHeader llc;
    if (packet->GetSize() < llc.GetSerializedSize()) return;
    packet->RemoveHeader(llc);
    const auto bytes = Bytes(packet);
    const auto key = std::make_pair(node, mpdu->GetPacket()->GetUid());
    const auto tid = mpdu->GetHeader().IsQosData() ? mpdu->GetHeader().GetQosTid() : 255;
    int tos = -1, port = 0;
    if (llc.GetType() == 0x0800 && packet->GetSize() >= 20)
    {
        Ipv4Header ip;
        packet->RemoveHeader(ip);
        tos = ip.GetTos();
        if (ip.GetProtocol() == 17 && packet->GetSize() >= 8)
        {
            UdpHeader udp;
            packet->RemoveHeader(udp);
            port = udp.GetDestinationPort();
        }
    }
    else if (llc.GetType() == 0x86dd && packet->GetSize() >= 40)
    {
        Ipv6Header ip;
        packet->RemoveHeader(ip);
        tos = ip.GetTrafficClass();
    }
    int64_t residenceNs = -1;
    if (event == "enqueue")
    {
        g_entered[key] = Simulator::Now();
        if (auto match = g_exact.find(bytes); match != g_exact.end()) ++match->second;
        if (tos >= 0 && ((tos & 0xfc) == kControlTos || (tos & 0xfc) == kPayloadTos))
        {
            const unsigned flow = (tos & 0xfc) == kControlTos ? 0 : 1;
            Check(tid == (flow == 0 ? 5 : 1), "DS field did not reach the expected native QoS TID");
            Check(ac == (flow == 0 ? AC_VI : AC_BK), "DS field entered the wrong native AC queue");
            ++g_enqueued[node][flow];
        }
        if (llc.GetType() == 0x0806)
            Check(tid == 0 && ac == AC_BE, "ARP classification changed");
    }
    else if (auto found = g_entered.find(key); found != g_entered.end())
    {
        residenceNs = (Simulator::Now() - found->second).GetNanoSeconds();
        g_entered.erase(found);
    }
    if (ac == AC_BK)
    {
        g_maxPayloadDepth[node] = std::max(g_maxPayloadDepth[node], queue->GetNPackets());
        if (event == "drop") ++g_payloadDrops[node];
    }
    g_trace << Simulator::Now().GetNanoSeconds() << ',' << node << ',' << event << ','
            << unsigned(ac) << ',' << unsigned(tid) << ',' << tos << ',' << port << ','
            << mpdu->GetPacket()->GetUid() << ',' << queue->GetNPackets() << ','
            << residenceNs << '\n';
}

void ReceiveUplink(unsigned flow, Ptr<Socket> socket)
{
    while (socket->Recv()) ++g_uplinkReceived[flow];
}

void DrainDownlink()
{
    std::array<uint8_t, 4096> buffer;
    for (unsigned flow = 0; flow < 2; ++flow)
        while (recv(g_linuxReceivers[flow], buffer.data(), buffer.size(), MSG_DONTWAIT) >= 0)
            ++g_downlinkReceived[flow];
}

void Inject(Ptr<Packet> networkPacket, uint16_t protocol, bool exact)
{
    if (exact) g_exact.emplace(Bytes(networkPacket), 0);
    EthernetHeader ethernet(false);
    ethernet.SetSource(g_uavMac);
    ethernet.SetDestination(g_gcsMac);
    ethernet.SetLengthType(protocol);
    networkPacket->AddHeader(ethernet);
    const auto frame = Bytes(networkPacket);
    sockaddr_ll address{};
    address.sll_family = AF_PACKET;
    address.sll_protocol = htons(protocol);
    address.sll_ifindex = g_interface;
    address.sll_halen = 6;
    g_gcsMac.CopyTo(address.sll_addr);
    if (sendto(g_raw, frame.data(), frame.size(), 0,
               reinterpret_cast<sockaddr*>(&address), sizeof(address)) != ssize_t(frame.size()))
        throw std::runtime_error("Linux TAP packet injection failed");
}

void InjectIpv4(uint8_t tos, unsigned flow, bool exact)
{
    auto packet = Create<Packet>(flow == 0 ? 64 : 192);
    UdpHeader udp;
    udp.SetSourcePort(flow == 0 ? kControlPort + 10 : kPayloadPort + 10);
    udp.SetDestinationPort(flow == 0 ? kControlPort : kPayloadPort);
    udp.EnableChecksums();
    udp.InitializeChecksum(kUavIp, kGcsIp, 17);
    packet->AddHeader(udp);
    Ipv4Header ip;
    ip.SetSource(kUavIp); ip.SetDestination(kGcsIp); ip.SetProtocol(17);
    ip.SetPayloadSize(packet->GetSize()); ip.SetTtl(64); ip.SetTos(tos);
    ip.SetIdentification(g_identification++); ip.EnableChecksum();
    packet->AddHeader(ip);
    Inject(packet, 0x0800, exact);
}

void SetupHost(const std::string& tapName)
{
    // ConfigureLocal owns the temporary TAP; use the production ingress mode
    // after creation, before any test frames are read.
    g_tap->SetMode(TapBridge::USE_LOCAL);
    g_interface = if_nametoindex(tapName.c_str());
    if (!g_interface) throw std::runtime_error("Owned TAP was not created");
    g_raw = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (g_raw < 0) throw std::runtime_error("CAP_NET_RAW is required in the isolated test namespace");
    for (unsigned flow = 0; flow < 2; ++flow)
    {
        g_linuxReceivers[flow] = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
        sockaddr_in address{};
        // Native downlink is subnet broadcast, so receive on all addresses of
        // this isolated namespace rather than binding a unicast-only socket.
        address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_ANY);
        address.sin_port = htons(flow == 0 ? kControlPort + 10 : kPayloadPort + 10);
        if (g_linuxReceivers[flow] < 0 ||
            bind(g_linuxReceivers[flow], reinterpret_cast<sockaddr*>(&address), sizeof(address)))
            throw std::runtime_error("Cannot bind the owned TAP receiver");
    }
    // All ECN values must preserve the same DSCP classification and exact bytes.
    for (uint8_t ecn = 0; ecn < 4; ++ecn)
    {
        InjectIpv4(kControlTos | ecn, 0, true);
        InjectIpv4(kPayloadTos | ecn, 1, true);
    }
    for (uint8_t tos : {kControlTos, kPayloadTos})
    {
        auto packet = Create<Packet>(32);
        Ipv6Header ip;
        ip.SetSource(Ipv6Address("2001:db8::2"));
        ip.SetDestination(Ipv6Address("2001:db8::1"));
        ip.SetTrafficClass(tos); ip.SetNextHeader(59); ip.SetHopLimit(64);
        ip.SetPayloadLength(packet->GetSize()); packet->AddHeader(ip);
        Inject(packet, 0x86dd, true);
    }
    auto arpPacket = Create<Packet>();
    ArpHeader arp;
    arp.SetRequest(g_uavMac, kUavIp, g_gcsMac, kGcsIp);
    arpPacket->AddHeader(arp);
    Inject(arpPacket, 0x0806, true);
    Check(g_tap->ReadTap() >= 11, "TAP did not read all baseline Ethernet frames");
}

void LoadTick(unsigned tick)
{
    // 4 * 192 bytes per ms in each direction: 6.144 Mbit/s useful offered
    // payload per sender, sharing a stock HtMcs0 channel. Measure backlog;
    // do not infer it merely from this offered rate.
    if (tick % 50 == 0)
    {
        InjectIpv4(kControlTos, 0, false);
        Check(g_downlink[0]->Send(Create<Packet>(64)) == 64, "Downlink control send failed");
    }
    for (unsigned i = 0; i < 4; ++i)
    {
        InjectIpv4(kPayloadTos, 1, false);
        // Nonblocking Linux TAP ingress is intentionally exercised separately
        // from the native socket / TrafficControlLayer downlink.
        g_downlink[1]->Send(Create<Packet>(192));
    }
    g_tap->ReadTap();
    DrainDownlink();
    if (tick < 999) Simulator::Schedule(MilliSeconds(1), &LoadTick, tick + 1);
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2) throw std::runtime_error("Expected a new result directory");
    const fs::path output = fs::absolute(argv[1]);
    if (fs::exists(output)) throw std::runtime_error("Preserve previous attempts: result already exists");
    fs::create_directories(output);
    g_trace.open(output / "wifi-queues.csv");
    g_trace << "simulation_ns,node,event,ac,tid,tos,destination_port,packet_uid,queue_packets,residence_ns\n";
    RngSeedManager::SetSeed(1); RngSeedManager::SetRun(1);
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    NodeContainer nodes; nodes.Create(2);
    MobilityHelper mobility; mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(nodes);
    nodes.Get(1)->GetObject<MobilityModel>()->SetPosition(Vector(1, 0, 0));
    for (const std::string ac : {"BE", "BK", "VI", "VO"})
    {
        Config::SetDefault("ns3::WifiMac::" + ac + "_MaxAmpduSize", UintegerValue(0));
        Config::SetDefault("ns3::WifiMac::" + ac + "_MaxAmsduSize", UintegerValue(0));
        Config::SetDefault("ns3::WifiMac::" + ac + "_BlockAckThreshold", UintegerValue(0));
    }
    YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
    YansWifiPhyHelper phy; phy.SetChannel(channel.Create());
    phy.Set("ChannelSettings", StringValue("{1, 20, BAND_2_4GHZ, 0}"));
    WifiHelper wifi; wifi.SetStandard(WIFI_STANDARD_80211n);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager", "DataMode", StringValue("HtMcs0"),
                                 "ControlMode", StringValue("ErpOfdmRate6Mbps"),
                                 "NonUnicastMode", StringValue("ErpOfdmRate6Mbps"));
    WifiMacHelper mac; mac.SetType("ns3::AdhocWifiMac", "QosSupported", BooleanValue(true));
    auto devices = wifi.Install(phy, mac, nodes);
    wifi.AssignStreams(devices, 10);
    g_gcsMac = Mac48Address::ConvertFrom(devices.Get(0)->GetAddress());
    g_uavMac = Mac48Address::ConvertFrom(devices.Get(1)->GetAddress());
    for (unsigned node = 0; node < 2; ++node)
    {
        auto native = DynamicCast<WifiNetDevice>(devices.Get(node));
        for (const auto ac : {AC_BE, AC_BK, AC_VI, AC_VO})
        {
            auto queue = native->GetMac()->GetQosTxop(ac)->GetWifiMacQueue();
            for (const std::string event : {"Enqueue", "Dequeue", "Drop"})
            {
                std::string lower = event;
                std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
                Check(queue->TraceConnectWithoutContext(event,
                    MakeBoundCallback(&ObserveQueue, node, ac, queue, lower)), "Native queue trace is unavailable");
            }
        }
    }
    InternetStackHelper internet; internet.Install(nodes.Get(0));
    Ipv4AddressHelper addresses; addresses.SetBase("10.249.254.0", "255.255.255.0");
    addresses.Assign(NetDeviceContainer(devices.Get(0)));
    for (unsigned flow = 0; flow < 2; ++flow)
    {
        auto receiver = Socket::CreateSocket(nodes.Get(0), UdpSocketFactory::GetTypeId());
        receiver->Bind(InetSocketAddress(Ipv4Address::GetAny(), flow == 0 ? kControlPort : kPayloadPort));
        receiver->SetRecvCallback(MakeBoundCallback(&ReceiveUplink, flow));
        g_uplink[flow] = receiver;
        auto sender = Socket::CreateSocket(nodes.Get(0), UdpSocketFactory::GetTypeId());
        sender->SetIpTos(flow == 0 ? kControlTos : kPayloadTos);
        sender->SetAllowBroadcast(true);
        sender->Connect(InetSocketAddress(Ipv4Address("10.249.254.255"),
                                         flow == 0 ? kControlPort + 10 : kPayloadPort + 10));
        g_downlink[flow] = sender;
    }
    const auto tapName = "bq" + std::to_string(getpid());
    if (if_nametoindex(tapName.c_str())) throw std::runtime_error("Refuse to use an existing TAP");
    TapBridgeHelper tap;
    tap.SetAttribute("Mode", StringValue("ConfigureLocal"));
    tap.SetAttribute("DeviceName", StringValue(tapName));
    tap.SetAttribute("IpAddress", Ipv4AddressValue(kUavIp));
    tap.SetAttribute("Netmask", Ipv4MaskValue(Ipv4Mask("255.255.255.0")));
    tap.SetAttribute("SynchronousRead", BooleanValue(true));
    g_tap = DynamicCast<TapBridge>(tap.Install(nodes.Get(1), devices.Get(1)));
    Simulator::Schedule(MilliSeconds(1), &SetupHost, tapName);
    Simulator::Schedule(MilliSeconds(100), &LoadTick, 0);
    Simulator::Stop(Seconds(1.8));
    Simulator::Run();
    DrainDownlink();
    for (const auto& [bytes, observed] : g_exact)
        Check(observed == 1, "An injected IP/ARP frame was missing, duplicated or changed at native enqueue");
    for (unsigned node = 0; node < 2; ++node)
    {
        Check(g_enqueued[node][0] > 0 && g_enqueued[node][1] > 0, "Both flows did not enter native queues");
        Check(g_maxPayloadDepth[node] > 1, "Payload backlog was not observed; saturation is unconfirmed");
    }
    Check(g_uplinkReceived[0] > 0 && g_uplinkReceived[1] > 0, "No control or payload uplink delivery");
    Check(g_downlinkReceived[0] > 0 && g_downlinkReceived[1] > 0, "No control or payload downlink delivery");
    Simulator::Destroy();
    if (g_raw >= 0) close(g_raw);
    for (auto fd : g_linuxReceivers) if (fd >= 0) close(fd);
    g_trace.close();
    std::ofstream report(output / "result.json");
    report << "{\"schema\":1,\"passed\":" << (g_failures.empty() ? "true" : "false")
           << ",\"scope\":\"CPU TAP and stock YansWifi EDCA regression; no realtime or hardware qualification\""
           << ",\"seed\":1,\"run\":1,\"data_mode\":\"HtMcs0\",\"non_unicast_mode\":\"ErpOfdmRate6Mbps\",\"exact_frames\":" << g_exact.size()
           << ",\"control_tos\":184,\"payload_tos\":40,\"control_expected_tid\":5,\"payload_expected_tid\":1"
           << ",\"payload_useful_offered_bps_per_direction\":6144000,\"nodes\":[";
    for (unsigned node = 0; node < 2; ++node)
    {
        if (node) report << ',';
        report << "{\"id\":" << node << ",\"control_enqueues\":" << g_enqueued[node][0]
               << ",\"payload_enqueues\":" << g_enqueued[node][1]
               << ",\"maximum_payload_queue_packets\":" << g_maxPayloadDepth[node]
               << ",\"payload_queue_drops\":" << g_payloadDrops[node] << '}';
    }
    report << "],\"uplink_received\":[" << g_uplinkReceived[0] << ',' << g_uplinkReceived[1]
           << "],\"downlink_received\":[" << g_downlinkReceived[0] << ',' << g_downlinkReceived[1]
           << "],\"failures\":[";
    for (unsigned i = 0; i < g_failures.size(); ++i)
    {
        if (i) report << ',';
        report << '"' << g_failures[i] << '"';
        std::cerr << g_failures[i] << '\n';
    }
    report << "]}\n";
    return g_failures.empty() ? 0 : 1;
}
