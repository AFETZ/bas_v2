// A component probe, not a flight: real native Spectrum Wi-Fi/UDP traffic at
// explicitly supplied static positions through the same upstream loss adapter.
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/spectrum-module.h"
#include "ns3/spectrum-wifi-helper.h"
#include "ns3/wifi-module.h"
#include "native-geometry-loss.h"
#include <set>
#include <sstream>

using namespace ns3;
namespace {
std::set<uint32_t> directReceived, relayReceived, forwarded;
Ptr<Socket> relayOut;
Address relayDestination;

Vector ParseVector(const std::string& text) {
    std::istringstream stream(text);
    Vector out;
    char a,b;
    NS_ABORT_MSG_IF(!(stream>>out.x>>a>>out.y>>b>>out.z) || a!=',' || b!=',' ||
                    !std::isfinite(out.x) || !std::isfinite(out.y) || !std::isfinite(out.z),
                    "invalid explicit static position");
    return out;
}
uint32_t Sequence(Ptr<Packet> packet) {
    uint8_t bytes[4];
    NS_ABORT_MSG_IF(packet->GetSize()!=64, "unexpected probe payload");
    packet->CopyData(bytes,4);
    return (uint32_t(bytes[0])<<24)|(uint32_t(bytes[1])<<16)|
           (uint32_t(bytes[2])<<8)|uint32_t(bytes[3]);
}
void Receive(bool viaRelay, Ptr<Socket> socket) {
    while (auto packet=socket->Recv())
        (viaRelay ? relayReceived : directReceived).insert(Sequence(packet));
}
void Forward(Ptr<Socket> socket) {
    while (auto packet=socket->Recv()) {
        forwarded.insert(Sequence(packet));
        relayOut->SendTo(packet,0,relayDestination);
    }
}
void Send(Ptr<Socket> socket, Address direct, Address relay, uint32_t sequence) {
    uint8_t bytes[64]{};
    bytes[0]=sequence>>24; bytes[1]=sequence>>16; bytes[2]=sequence>>8; bytes[3]=sequence;
    socket->SendTo(Create<Packet>(bytes,64),0,direct);
    socket->SendTo(Create<Packet>(bytes,64),0,relay);
}
void Sample(Ptr<bas::GeometryLogDistance> loss) {
    loss->ObserveAll();
    Simulator::Schedule(MilliSeconds(20),&Sample,loss);
}
}
int main(int argc,char* argv[]) {
    pybind11::scoped_interpreter python{};
    std::string config, outputPrefix, from="0,0,2", to="85,80,12", relay="15,90,16";
    double carrierHz=2.412e9, txPowerDbm=-20, interval=.05;
    uint32_t count=100;
    CommandLine command(__FILE__);
    command.AddValue("geometryConfig","Explicit same-format urban geometry/model JSON",config);
    command.AddValue("outputPrefix","New output path prefix",outputPrefix);
    command.AddValue("from","Static CP antenna ENU x,y,z",from);
    command.AddValue("to","Static destination antenna ENU x,y,z",to);
    command.AddValue("relay","Static relay antenna ENU x,y,z",relay);
    command.AddValue("carrierHz","Actual Wi-Fi reference frequency",carrierHz);
    command.AddValue("txPowerDbm","Native Wi-Fi Tx power",txPowerDbm);
    command.AddValue("count","Offered UDP datagrams per direct/relay path",count);
    command.AddValue("interval","Interval between sequence numbers in model seconds",interval);
    command.Parse(argc,argv);
    NS_ABORT_MSG_IF(config.empty() || outputPrefix.empty() || count==0 || interval<=0,
                    "explicit inputs are required");
    NodeContainer nodes;
    nodes.Create(3);
    std::vector<Ptr<MobilityModel>> mobility;
    for (auto text : {from,to,relay}) {
        auto model=CreateObject<ConstantPositionMobilityModel>();
        model->SetPosition(ParseVector(text));
        nodes.Get(mobility.size())->AggregateObject(model);
        mobility.push_back(model);
    }
    auto loss=CreateObject<bas::GeometryLogDistance>();
    loss->Load(config,carrierHz,outputPrefix+"-geometry.csv");
    loss->Bind(mobility);
    auto channel=CreateObject<MultiModelSpectrumChannel>();
    channel->SetPropagationDelayModel(CreateObject<ConstantSpeedPropagationDelayModel>());
    channel->AddPropagationLossModel(loss); // Exactly one base loss, no spectrum Friis.
    SpectrumWifiPhyHelper phy;
    phy.SetChannel(channel);
    phy.SetErrorRateModel("ns3::NistErrorRateModel");
    phy.Set("TxPowerStart",DoubleValue(txPowerDbm));
    phy.Set("TxPowerEnd",DoubleValue(txPowerDbm));
    phy.Set("ChannelSettings",StringValue("{1,20,BAND_2_4GHZ,0}"));
    phy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
    WifiHelper wifi;
    wifi.SetStandard(WIFI_STANDARD_80211g);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
        "DataMode",StringValue("ErpOfdmRate6Mbps"),"ControlMode",StringValue("ErpOfdmRate6Mbps"));
    WifiMacHelper mac;
    mac.SetType("ns3::AdhocWifiMac");
    auto devices=wifi.Install(phy,mac,nodes);
    NS_ABORT_MSG_IF(std::abs(MHzToHz(DynamicCast<WifiNetDevice>(devices.Get(0))->
                       GetPhy()->GetFrequency())-carrierHz)>.5,"probe carrier/channel mismatch");
    phy.EnablePcap(outputPrefix,devices);
    InternetStackHelper internet;
    internet.Install(nodes);
    Ipv4AddressHelper addresses;
    addresses.SetBase("10.42.0.0","255.255.255.0");
    auto ip=addresses.Assign(devices);
    auto sender=Socket::CreateSocket(nodes.Get(0),UdpSocketFactory::GetTypeId());
    auto direct=Socket::CreateSocket(nodes.Get(1),UdpSocketFactory::GetTypeId());
    direct->Bind(InetSocketAddress(Ipv4Address::GetAny(),9000));
    direct->SetRecvCallback(MakeBoundCallback(&Receive,false));
    auto receiver=Socket::CreateSocket(nodes.Get(1),UdpSocketFactory::GetTypeId());
    receiver->Bind(InetSocketAddress(Ipv4Address::GetAny(),9002));
    receiver->SetRecvCallback(MakeBoundCallback(&Receive,true));
    auto relayIn=Socket::CreateSocket(nodes.Get(2),UdpSocketFactory::GetTypeId());
    relayIn->Bind(InetSocketAddress(Ipv4Address::GetAny(),9001));
    relayIn->SetRecvCallback(MakeCallback(&Forward));
    relayOut=Socket::CreateSocket(nodes.Get(2),UdpSocketFactory::GetTypeId());
    relayDestination=InetSocketAddress(ip.GetAddress(1),9002);
    for (uint32_t i=0;i<count;++i)
        Simulator::Schedule(Seconds(1+i*interval),&Send,sender,
            Address(InetSocketAddress(ip.GetAddress(1),9000)),
            Address(InetSocketAddress(ip.GetAddress(2),9001)),i);
    Sample(loss);
    Simulator::Stop(Seconds(3+count*interval));
    Simulator::Run();
    std::ofstream out(outputPrefix+"-packets.json");
    out << "{\"probe_only\":true,\"nodes\":3,\"native_phy\":\"SpectrumWifiPhy\","
        << "\"native_mac\":\"AdhocWifiMac\",\"wifi_standard\":\"802.11g\","
        << "\"data_mode\":\"ErpOfdmRate6Mbps\",\"tx_power_dbm\":" << txPowerDbm
        << ",\"offered_each_path\":" << count << ",\"direct_unique_received\":"
        << directReceived.size() << ",\"relay_unique_received_first_hop\":" << forwarded.size()
        << ",\"relay_unique_received_second_hop\":" << relayReceived.size()
        << ",\"packet_model_evaluations\":" << loss->PacketEvaluations() << "}\n";
    loss->Close();
    Simulator::Destroy();
    return 0;
}
