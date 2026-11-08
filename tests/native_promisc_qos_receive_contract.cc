// CPU regression for the real TapBridge -> promiscuous FEM -> MacRxMiddle path.
// Feed decoded MPDUs through the pinned public receive APIs; no PHY bypass is
// claimed as a system result, and no radio, realtime or hardware SLA is tested.
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/wifi-module.h"
#include "ns3/frame-exchange-manager.h"
#include "ns3/mac-rx-middle.h"
#include "ns3/wifi-mpdu.h"
#include "ns3/wifi-psdu.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

using namespace ns3;
namespace
{
const Mac48Address kSource("02:71:ff:00:00:01");
const Mac48Address kOwn("02:71:02:00:10:10");
const Mac48Address kOther("02:71:01:00:10:10");
std::vector<std::string> g_failures;
struct Received
{
    Mac48Address destination;
    uint16_t sequence;
    bool retry;
    std::string bytes;
};

void Check(bool result, const std::string& reason)
{
    if (!result) g_failures.push_back(reason);
}

struct Receiver
{
    Ptr<MacRxMiddle> middle{Create<MacRxMiddle>()};
    Ptr<FrameExchangeManager> exchange{CreateObject<FrameExchangeManager>()};
    std::vector<Received> received;

    Receiver()
    {
        middle->SetForwardCallback(MakeCallback(&Receiver::Forward, this));
        exchange->SetAddress(kOwn);
        exchange->SetMacRxMiddle(middle);
        exchange->SetPromisc(); // TapBridge enables this public mechanism.
    }
    ~Receiver() { exchange->Dispose(); }
    void Forward(Ptr<const WifiMpdu> mpdu, uint8_t linkId)
    {
        const auto& header = mpdu->GetHeader();
        std::vector<uint8_t> bytes(mpdu->GetPacket()->GetSize());
        mpdu->GetPacket()->CopyData(bytes.data(), bytes.size());
        received.push_back({header.GetAddr1(), header.GetSequenceNumber(), header.IsRetry(),
                            std::string(bytes.begin(), bytes.end())});
        Check(linkId == 0, "receive link ID changed");
    }
    Ptr<WifiMpdu> Frame(Mac48Address dest, uint16_t seq, bool retry,
                        const std::string& bytes, uint8_t tid = 1, bool qos = true,
                        uint8_t fragment = 0, bool more = false)
    {
        WifiMacHeader header;
        header.SetType(qos ? WIFI_MAC_QOSDATA : WIFI_MAC_DATA);
        header.SetAddr1(dest); header.SetAddr2(kSource); header.SetAddr3(kSource);
        header.SetSequenceNumber(seq); header.SetFragmentNumber(fragment);
        if (qos) header.SetQosTid(tid);
        if (retry) header.SetRetry();
        if (more) header.SetMoreFragments();
        return Create<WifiMpdu>(Create<Packet>(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size()), header);
    }
    void Own(Ptr<WifiMpdu> mpdu) { middle->Receive(mpdu, 0); }
    void Overheard(Ptr<WifiMpdu> mpdu)
    {
        Check(mpdu->GetHeader().GetAddr1() != kOwn, "overheard fixture incorrectly addressed to self");
        WifiTxVector vector;
        vector.SetMode(WifiMode("OfdmRate6Mbps"));
        // Empty per-MPDU status denotes already-decoded aggregate member input.
        // The production other-host/promisc branch calls this SAME middle.
        exchange->Receive(Create<WifiPsdu>(mpdu, false), RxSignalInfo{}, vector, {});
    }
};
}

int main(int argc, char** argv)
{
    if (argc != 2 || std::filesystem::exists(argv[1]))
    {
        std::cerr << "usage: native_promisc_qos_receive_contract NEW_RESULT.json\n";
        return 2;
    }
    {
        Receiver r;
        r.Own(r.Frame(kOwn, 314, false, "own-first"));
        r.Own(r.Frame(kOwn, 314, true, "own-first"));
        Check(r.received.size() == 1, "same-RA genuine retry duplicate was not suppressed");
        r.Own(r.Frame(kOwn, 315, true, "own-new-sequence"));
        Check(r.received.size() == 2 && r.received.back().bytes == "own-new-sequence",
              "new own sequence retry was not delivered");
    }
    {
        Receiver r;
        r.Overheard(r.Frame(kOther, 314, false, "foreign-original"));
        Check(r.received.size() == 1 && r.received[0].destination == kOther,
              "real FrameExchangeManager promiscuous callback did not deliver foreign MPDU");
        // Own original attempt was lost: only its successful retry arrives.
        r.Own(r.Frame(kOwn, 314, true, "own-successful-retry"));
        Check(r.received.size() == 2 && r.received.back().destination == kOwn &&
              r.received.back().bytes == "own-successful-retry",
              "foreign RA poisoned own successful retry with the same source/TID/sequence");
        const auto before = r.received.size();
        r.Own(r.Frame(kOwn, 314, true, "own-successful-retry"));
        r.Overheard(r.Frame(kOther, 314, true, "foreign-original"));
        Check(r.received.size() == before, "duplicate retry suppression did not remain independent for each RA");
        r.Own(r.Frame(kOwn, 314, true, "different-tid", 5));
        Check(r.received.size() == before + 1 && r.received.back().bytes == "different-tid",
              "different QoS TID reused another TID sequence state");
    }
    {
        Receiver r;
        r.Overheard(r.Frame(kOther, 901, false, "A0", 1, true, 0, true));
        r.Own(r.Frame(kOwn, 901, false, "B0", 1, true, 0, true));
        r.Own(r.Frame(kOwn, 901, false, "B1", 1, true, 1));
        r.Overheard(r.Frame(kOther, 901, false, "A1", 1, true, 1));
        Check(r.received.size() == 2 && r.received[0].destination == kOwn && r.received[0].bytes == "B0B1" &&
              r.received[1].destination == kOther && r.received[1].bytes == "A0A1",
              "foreign and own RA fragment contexts merged or lost bytes");
    }
    {
        Receiver r;
        const Mac48Address group1("01:00:5e:00:00:01"), group2("01:00:5e:00:00:02");
        r.Own(r.Frame(group1, 22, false, "group-one"));
        r.Own(r.Frame(group1, 22, true, "group-one"));
        r.Own(r.Frame(group2, 22, true, "group-two"));
        Check(r.received.size() == 2 && r.received[0].bytes == "group-one" && r.received[1].bytes == "group-two",
              "existing QoS group-address duplicate behavior changed");
    }
    {
        Receiver r;
        r.Own(r.Frame(kOwn, 17, false, "legacy", 0, false));
        r.Own(r.Frame(kOwn, 17, true, "legacy", 0, false));
        r.Own(r.Frame(kOwn, 18, true, "legacy-next", 0, false));
        Check(r.received.size() == 2 && r.received[0].bytes == "legacy" && r.received[1].bytes == "legacy-next",
              "existing non-QoS originator duplicate behavior changed");
    }
    Simulator::Destroy();
    std::ofstream result(argv[1]);
    result << "{\"schema\":1,\"passed\":" << (g_failures.empty() ? "true" : "false")
           << ",\"scope\":\"CPU actual public FEM promiscuous and MacRxMiddle receive APIs; decoded MPDU fixture, no PHY, hardware or SLA qualification\""
           << ",\"cases\":[\"same_ra_retry_duplicate\",\"overheard_ra_then_own_successful_retry\",\"qos_tid_isolation\",\"interleaved_ra_fragments\",\"unchanged_groupcast\",\"unchanged_non_qos\"],\"failures\":[";
    for (size_t i=0; i<g_failures.size(); ++i)
    {
        if (i) result << ',';
        result << '"' << g_failures[i] << '"';
        std::cerr << g_failures[i] << '\n';
    }
    result << "]}\n";
    return g_failures.empty() ? 0 : 1;
}
