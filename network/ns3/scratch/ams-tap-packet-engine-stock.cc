// Native ns-3.40 CSMA shared-medium baseline.  This target intentionally uses
// only public ns-3 APIs: CsmaHelper, DropTailQueue, traces, TapBridge, and an
// explicit project ErrorModel that consumes live Sionna packet-loss state.
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/tap-bridge-module.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsTapPacketEngineStock");

namespace
{
constexpr const char* CONTRACT = "ams.tap_packet_engine.stock/v1";
constexpr uint32_t MAX_UAVS = 5;

uint8_t g_controlTos = 184;
uint8_t g_payloadTos = 40;
uint8_t g_additionalDataTos = 0;

uint64_t
SteadyNowNs()
{
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                     std::chrono::steady_clock::now().time_since_epoch())
                                     .count());
}

std::string
JsonEscape(const std::string& value)
{
    std::ostringstream out;
    for (char character : value)
    {
        if (character == '"' || character == '\\')
        {
            out << '\\' << character;
        }
        else if (character == '\n')
        {
            out << "\\n";
        }
        else if (character == '\r')
        {
            out << "\\r";
        }
        else if (character == '\t')
        {
            out << "\\t";
        }
        else
        {
            out << character;
        }
    }
    return out.str();
}

std::string
IpText(const Ipv4Address& address)
{
    std::ostringstream out;
    out << address;
    return out.str();
}

uint64_t
ReadU64(const uint8_t* bytes)
{
    uint64_t value = 0;
    for (uint32_t index = 0; index < 8; ++index)
    {
        value = (value << 8) | bytes[index];
    }
    return value;
}

uint32_t
ReadU32(const uint8_t* bytes)
{
    return (static_cast<uint32_t>(bytes[0]) << 24) |
           (static_cast<uint32_t>(bytes[1]) << 16) |
           (static_cast<uint32_t>(bytes[2]) << 8) | bytes[3];
}

std::string
ProfileName(uint8_t value)
{
    switch (value)
    {
    case 1:
        return "nominal";
    case 2:
        return "contention";
    case 3:
        return "controlled_overload";
    case 4:
        return "meltdown";
    default:
        return "";
    }
}

std::string
ClassName(uint8_t value)
{
    switch (value)
    {
    case 1:
        return "control";
    case 2:
        return "payload";
    case 3:
        return "additional_data";
    default:
        return "";
    }
}

struct PacketMeta
{
    bool ipv4 = false;
    bool p2mp = false;
    int protocol = -1;
    int sourcePort = -1;
    int destinationPort = -1;
    int payloadSize = -1;
    uint8_t tos = 0;
    std::string trafficClass = "unclassified";
    std::string sourceIp = "unknown";
    std::string destinationIp = "unknown";
    uint64_t sourceMonotonicNs = 0;
    std::string packetId;
};

PacketMeta
Inspect(Ptr<const Packet> packet)
{
    PacketMeta meta;
    Ptr<Packet> copy = packet->Copy();
    EthernetHeader ethernet(false);
    EthernetTrailer trailer;
    if (copy->GetSize() < ethernet.GetSerializedSize() + trailer.GetSerializedSize())
    {
        return meta;
    }
    copy->RemoveTrailer(trailer);
    if (copy->RemoveHeader(ethernet) == 0)
    {
        return meta;
    }
    uint16_t etherType = ethernet.GetLengthType();
    if (etherType <= 1500)
    {
        LlcSnapHeader llc;
        if (copy->RemoveHeader(llc) == 0)
        {
            return meta;
        }
        etherType = llc.GetType();
    }
    if (etherType != 0x0800)
    {
        return meta;
    }
    Ipv4Header ipv4;
    if (copy->RemoveHeader(ipv4) == 0)
    {
        return meta;
    }
    meta.ipv4 = true;
    meta.p2mp = ipv4.GetDestination().IsMulticast();
    meta.protocol = ipv4.GetProtocol();
    meta.tos = ipv4.GetTos();
    meta.sourceIp = IpText(ipv4.GetSource());
    meta.destinationIp = IpText(ipv4.GetDestination());
    if (meta.tos == g_controlTos)
    {
        meta.trafficClass = "control";
    }
    else if (meta.tos == g_payloadTos)
    {
        meta.trafficClass = "payload";
    }
    else if (meta.tos == g_additionalDataTos)
    {
        meta.trafficClass = "additional_data";
    }
    if (meta.protocol != 17)
    {
        return meta;
    }
    UdpHeader udp;
    if (copy->RemoveHeader(udp) == 0)
    {
        return meta;
    }
    meta.sourcePort = udp.GetSourcePort();
    meta.destinationPort = udp.GetDestinationPort();
    meta.payloadSize = static_cast<int>(copy->GetSize());
    std::array<uint8_t, 20> prefix{};
    const uint32_t copied = copy->CopyData(prefix.data(), std::min<uint32_t>(copy->GetSize(), prefix.size()));
    if (copied >= prefix.size() && prefix[4] == 1 &&
        std::memcmp(prefix.data(), "BQO1", 4) == 0)
    {
        const std::string profile = ProfileName(prefix[5]);
        const std::string trafficClass = ClassName(prefix[6]);
        const uint32_t sequence = ReadU32(prefix.data() + 8);
        meta.sourceMonotonicNs = ReadU64(prefix.data() + 12);
        if (!profile.empty() && !trafficClass.empty() && prefix[7] >= 1 && prefix[7] <= MAX_UAVS)
        {
            meta.packetId = profile + ":" + trafficClass + ":uav" +
                            std::to_string(prefix[7]) + ":" + std::to_string(sequence);
        }
    }
    return meta;
}

std::string
EndpointDevice(const std::string& address)
{
    if (address == "10.71.0.10")
    {
        return "cp";
    }
    for (uint32_t index = 1; index <= MAX_UAVS; ++index)
    {
        if (address == "10.71." + std::to_string(index) + ".10")
        {
            return "uav" + std::to_string(index);
        }
    }
    return "unknown";
}

std::optional<std::string>
JsonString(const std::string& line, const std::string& key)
{
    const std::regex expression("\\\"" + key + "\\\":\\\"([^\\\"]*)\\\"");
    std::smatch match;
    return std::regex_search(line, match, expression) ? std::optional<std::string>(match[1].str())
                                                       : std::nullopt;
}

std::optional<uint64_t>
JsonUint(const std::string& line, const std::string& key)
{
    const std::regex expression("\\\"" + key + "\\\":([0-9]+)");
    std::smatch match;
    if (!std::regex_search(line, match, expression))
    {
        return std::nullopt;
    }
    try
    {
        return std::stoull(match[1].str());
    }
    catch (const std::exception&)
    {
        return std::nullopt;
    }
}

std::optional<double>
JsonDouble(const std::string& line, const std::string& key)
{
    const std::regex expression("\\\"" + key + "\\\":([0-9]+(?:\\.[0-9]+)?)");
    std::smatch match;
    if (!std::regex_search(line, match, expression))
    {
        return std::nullopt;
    }
    try
    {
        return std::stod(match[1].str());
    }
    catch (const std::exception&)
    {
        return std::nullopt;
    }
}

struct SionnaState
{
    bool available = false;
    std::string status = "missing";
    uint64_t sequence = 0;
    uint64_t expiresNs = 0;
    double lossProbability = 1.0;
    std::string appliedStateId;
    std::string mappingVersion;
};

class SionnaStateTable
{
  public:
    SionnaStateTable(bool enabled, std::string path, uint32_t periodMs, uint32_t maximumAgeMs)
        : m_enabled(enabled),
          m_path(std::move(path)),
          m_periodMs(periodMs),
          m_maximumAgeNs(static_cast<uint64_t>(maximumAgeMs) * 1000000ULL)
    {
    }

    void Start()
    {
        if (m_enabled)
        {
            Simulator::ScheduleNow(&SionnaStateTable::Poll, this);
        }
    }

    SionnaState Lookup(const std::string& link, const std::string& trafficClass) const
    {
        if (!m_enabled)
        {
            return {true, "disabled", 0, 0, 0.0, "", ""};
        }
        const auto found = m_states.find(link + "|" + trafficClass);
        if (found == m_states.end())
        {
            return {};
        }
        SionnaState state = found->second;
        if (!state.available || SteadyNowNs() >= state.expiresNs)
        {
            state.available = false;
            state.status = state.status == "fresh" ? "expired" : state.status;
        }
        return state;
    }

  private:
    void Poll()
    {
        try
        {
            if (std::filesystem::exists(m_path))
            {
                const uint64_t size = std::filesystem::file_size(m_path);
                if (size < m_offset)
                {
                    m_fault = "state_ipc_truncated";
                    m_states.clear();
                }
                else
                {
                    std::ifstream input(m_path);
                    input.seekg(static_cast<std::streamoff>(m_offset));
                    std::string line;
                    while (std::getline(input, line))
                    {
                        m_offset += line.size() + 1;
                        Apply(line);
                    }
                }
            }
        }
        catch (const std::exception&)
        {
            m_fault = "state_ipc_read_failed";
            m_states.clear();
        }
        Simulator::Schedule(MilliSeconds(m_periodMs), &SionnaStateTable::Poll, this);
    }

    void Apply(const std::string& line)
    {
        const auto schema = JsonString(line, "schema");
        const auto sequence = JsonUint(line, "state_sequence");
        const auto availability = JsonString(line, "availability");
        const auto link = JsonString(line, "directed_link");
        const auto trafficClass = JsonString(line, "traffic_class");
        if (!schema || *schema != "ams.sionna.packet_state/v1" || !sequence || !availability || !link ||
            !trafficClass || *sequence <= m_lastSequence)
        {
            m_fault = "state_ipc_invalid_record";
            m_states.clear();
            return;
        }
        m_lastSequence = *sequence;
        SionnaState state;
        state.sequence = *sequence;
        if (*availability == "fresh")
        {
            const auto expiry = JsonUint(line, "expires_monotonic_ns");
            const auto loss = JsonDouble(line, "loss_probability");
            const auto applied = JsonString(line, "applied_state_id");
            const auto mapping = JsonString(line, "mapping_version");
            if (!expiry || !loss || !applied || !mapping || *loss < 0.0 || *loss > 1.0 ||
                *expiry <= SteadyNowNs() || *expiry - SteadyNowNs() > m_maximumAgeNs)
            {
                m_fault = "state_ipc_invalid_fresh_record";
                m_states.clear();
                return;
            }
            state.available = true;
            state.status = "fresh";
            state.expiresNs = *expiry;
            state.lossProbability = *loss;
            state.appliedStateId = *applied;
            state.mappingVersion = *mapping;
        }
        else if (*availability == "unavailable")
        {
            state.status = "unavailable";
        }
        else
        {
            m_fault = "state_ipc_invalid_availability";
            m_states.clear();
            return;
        }
        m_states[*link + "|" + *trafficClass] = state;
    }

    bool m_enabled;
    std::string m_path;
    uint32_t m_periodMs;
    uint64_t m_maximumAgeNs;
    uint64_t m_offset = 0;
    uint64_t m_lastSequence = 0;
    std::string m_fault;
    std::map<std::string, SionnaState> m_states;
};

class AmsStockSionnaPacketErrorModel : public ErrorModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId type = TypeId("ns3::AmsStockSionnaPacketErrorModel")
                                 .SetParent<ErrorModel>()
                                 .SetGroupName("Network")
                                 .AddConstructor<AmsStockSionnaPacketErrorModel>();
        return type;
    }

    void Configure(SionnaStateTable* states, std::string receiver)
    {
        m_states = states;
        m_receiver = std::move(receiver);
    }

  private:
    bool DoCorrupt(Ptr<Packet> packet) override
    {
        const PacketMeta meta = Inspect(packet);
        const std::string sender = EndpointDevice(meta.sourceIp);
        if (!m_states || !meta.ipv4 || meta.p2mp || sender == "unknown" || sender == m_receiver)
        {
            return false;
        }
        const SionnaState state = m_states->Lookup(sender + ">" + m_receiver, meta.trafficClass);
        if (!state.available)
        {
            return true;
        }
        return m_uniform->GetValue() < state.lossProbability;
    }

    void DoReset() override {}

    SionnaStateTable* m_states = nullptr;
    std::string m_receiver;
    Ptr<UniformRandomVariable> m_uniform = CreateObject<UniformRandomVariable>();
};

class EventLog
{
  public:
    EventLog(std::string path, uint64_t epoch, uint32_t queueLimit, SionnaStateTable* states)
        : m_output(path, std::ios::out | std::ios::trunc),
          m_epoch(epoch),
          m_queueLimit(queueLimit),
          m_states(states)
    {
        if (!m_output)
        {
            throw std::runtime_error("cannot open stock event log");
        }
    }

    void Log(const std::string& event,
             const std::string& device,
             Ptr<const Packet> packet,
             int64_t depth = -1,
             const std::string& reason = "")
    {
        const PacketMeta meta = Inspect(packet);
        const uint64_t now = SteadyNowNs();
        uint64_t age = 0;
        if (meta.sourceMonotonicNs > 0 && now >= meta.sourceMonotonicNs)
        {
            age = now - meta.sourceMonotonicNs;
        }
        const std::string source = EndpointDevice(meta.sourceIp);
        const std::string receiver = EndpointDevice(meta.destinationIp);
        SionnaState state;
        if (m_states && source != "unknown" && receiver != "unknown" && !meta.p2mp)
        {
            state = m_states->Lookup(source + ">" + receiver, meta.trafficClass);
        }
        m_output << "{\"schema\":\"ams.ns3.stock_packet_event/v1\",\"event\":\""
                 << JsonEscape(event) << "\",\"host_monotonic_ns\":" << now
                 << ",\"simulator_ns\":" << Simulator::Now().GetNanoSeconds()
                 << ",\"event_epoch\":" << m_epoch << ",\"device_id\":\""
                 << JsonEscape(device) << ".radio\",\"queue_id\":\""
                 << JsonEscape(device) << ".radio.tx_queue\",\"traffic_class\":\""
                 << JsonEscape(meta.trafficClass) << "\",\"transport_protocol\":";
        if (meta.protocol < 0) m_output << "null"; else m_output << meta.protocol;
        m_output << ",\"source_udp_port\":";
        if (meta.sourcePort < 0) m_output << "null"; else m_output << meta.sourcePort;
        m_output << ",\"destination_udp_port\":";
        if (meta.destinationPort < 0) m_output << "null"; else m_output << meta.destinationPort;
        m_output << ",\"transport_payload_size\":";
        if (meta.payloadSize < 0) m_output << "null"; else m_output << meta.payloadSize;
        m_output << ",\"transport_payload_sha256\":null,\"packet_id\":";
        if (meta.packetId.empty()) m_output << "null"; else m_output << '\"' << JsonEscape(meta.packetId) << '\"';
        m_output << ",\"source_monotonic_ns\":";
        if (meta.sourceMonotonicNs == 0) m_output << "null"; else m_output << meta.sourceMonotonicNs;
        m_output << ",\"p2mp\":" << (meta.p2mp ? "true" : "false")
                 << ",\"root_transmission\":" << ((meta.p2mp && event == "channel") ? "true" : "false")
                 << ",\"queue_depth_packets\":";
        if (depth < 0) m_output << "null"; else m_output << depth;
        m_output << ",\"queue_limit_packets\":" << m_queueLimit
                 << ",\"queue_age_ns\":" << age << ",\"drop_reason\":";
        if (reason.empty()) m_output << "null"; else m_output << '\"' << JsonEscape(reason) << '\"';
        m_output << ",\"radio_state_status\":\"" << JsonEscape(state.status)
                 << "\",\"radio_state_sequence\":";
        if (state.sequence == 0) m_output << "null"; else m_output << state.sequence;
        m_output << ",\"radio_mapping_version\":";
        if (state.mappingVersion.empty()) m_output << "null"; else m_output << '\"' << JsonEscape(state.mappingVersion) << '\"';
        m_output << ",\"radio_loss_probability\":" << state.lossProbability << "}\n";
        m_output.flush();
        ++m_counts[event];
    }

    uint64_t Count(const std::string& event) const { return m_counts.count(event) ? m_counts.at(event) : 0; }

  private:
    std::ofstream m_output;
    uint64_t m_epoch;
    uint32_t m_queueLimit;
    SionnaStateTable* m_states;
    std::map<std::string, uint64_t> m_counts;
};

void TraceIngress(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("admit", device, packet); }
void TraceQueueEnqueue(EventLog* log, std::string device, Ptr<Queue<Packet>> queue, Ptr<const Packet> packet) { log->Log("enqueue", device, packet, queue->GetNPackets()); }
void TraceQueueDequeue(EventLog* log, std::string device, Ptr<Queue<Packet>> queue, Ptr<const Packet> packet) { log->Log("dequeue", device, packet, queue->GetNPackets()); }
void TraceQueueDrop(EventLog* log, std::string device, Ptr<Queue<Packet>> queue, Ptr<const Packet> packet) { log->Log("drop", device, packet, queue->GetNPackets(), "queue_limit_stock"); }
void TraceChannel(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("channel", device, packet); }
void TraceBackoff(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("backoff", device, packet); }
void TracePhyTxEnd(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("phy_tx_end", device, packet); }
void TracePhyRxDrop(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("drop", device, packet, -1, "sionna_error_model_or_native_rx_drop"); }
void TraceEgress(EventLog* log, std::string device, Ptr<const Packet> packet) { log->Log("egress", device, packet); }

std::vector<std::string>
SplitCsv(const std::string& value)
{
    std::vector<std::string> result;
    std::stringstream stream(value);
    std::string item;
    while (std::getline(stream, item, ',')) if (!item.empty()) result.push_back(item);
    return result;
}

std::string EndpointIp(uint32_t index) { return "10.71." + std::to_string(index) + ".10"; }
std::string RouterIp(uint32_t index) { return "10.71." + std::to_string(index) + ".1"; }
std::string RadioIp(uint32_t index) { return "10.72.0." + std::to_string(index + 1); }

Mac48Address EndpointMac(uint32_t index)
{
    std::ostringstream out; out << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << index << ":00:10:10";
    return Mac48Address(out.str().c_str());
}
Mac48Address RouterMac(uint32_t index)
{
    std::ostringstream out; out << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << index << ":00:00:01";
    return Mac48Address(out.str().c_str());
}
Mac48Address RadioMac(uint32_t index)
{
    std::ostringstream out; out << "02:72:00:00:00:" << std::hex << std::setfill('0') << std::setw(2) << index + 1;
    return Mac48Address(out.str().c_str());
}

void AddInterface(Ptr<Node> node, Ptr<NetDevice> device, const std::string& address)
{
    Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
    const uint32_t interface = ipv4->AddInterface(device);
    ipv4->AddAddress(interface, Ipv4InterfaceAddress(Ipv4Address(address.c_str()), Ipv4Mask("255.255.255.0")));
    ipv4->SetMetric(interface, 1); ipv4->SetUp(interface); ipv4->SetForwarding(interface, true);
}

void AddPermanentArp(Ptr<Node> node, Ptr<NetDevice> device, const std::string& address, const Mac48Address& mac)
{
    Ptr<Ipv4L3Protocol> ipv4 = node->GetObject<Ipv4L3Protocol>();
    const int32_t interface = ipv4->GetInterfaceForDevice(device);
    Ptr<ArpCache> cache = ipv4->GetInterface(static_cast<uint32_t>(interface))->GetArpCache();
    ArpCache::Entry* entry = cache->Lookup(Ipv4Address(address.c_str()));
    if (!entry) entry = cache->Add(Ipv4Address(address.c_str()));
    entry->SetMacAddress(mac); entry->MarkPermanent();
}

void PollStopFile(const std::string& path)
{
    if (!path.empty() && std::ifstream(path).good()) { Simulator::Stop(); return; }
    Simulator::Schedule(MilliSeconds(100), &PollStopFile, path);
}

void WriteReady(const std::string& path, const std::string& hash, uint64_t epoch, uint32_t uavCount)
{
    if (path.empty()) return;
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    output << "{\"status\":\"ready\",\"contract\":\"" << CONTRACT
           << "\",\"config_sha256\":\"" << hash << "\",\"event_epoch\":" << epoch
           << ",\"uav_count\":" << uavCount << ",\"pid\":" << getpid() << "}\n";
}
} // namespace

int
main(int argc, char* argv[])
{
    uint32_t uavCount = 1, queueMaxPackets = 100, controlTos = 184, payloadTos = 40, additionalDataTos = 0, seed = 42, selfTestBurst = 1;
    uint64_t durationMs = 3600000, run = 1, eventEpoch = 1;
    bool selfTest = false, sionnaIpcEnabled = false;
    std::string tapGcs = "tap-gcs", tapUavs, radioRate = "20000000bps", radioDelay = "2ms", configHash, eventsFile, pcapPrefix, readyFile, stopFile, sionnaStateFile;
    uint32_t sionnaPollIntervalMs = 1, sionnaMaxStateTtlMs = 20000;
    CommandLine command(__FILE__);
    command.AddValue("uavCount", "Number of UAV TAP endpoints", uavCount); command.AddValue("tapGcs", "GCS TAP", tapGcs); command.AddValue("tapUavs", "Comma-separated UAV TAPs", tapUavs);
    command.AddValue("durationMs", "Maximum realtime duration", durationMs); command.AddValue("radioRate", "CSMA data rate", radioRate); command.AddValue("radioDelay", "CSMA delay", radioDelay); command.AddValue("queueMaxPackets", "Required upstream default CSMA queue bound", queueMaxPackets);
    command.AddValue("controlTos", "Control TOS", controlTos); command.AddValue("payloadTos", "Payload TOS", payloadTos); command.AddValue("additionalDataTos", "Additional-data TOS", additionalDataTos);
    command.AddValue("seed", "ns-3 seed", seed); command.AddValue("run", "ns-3 run", run); command.AddValue("eventEpoch", "Product event epoch", eventEpoch); command.AddValue("selfTest", "Run without TAP bridges", selfTest); command.AddValue("selfTestBurst", "Smoke burst size", selfTestBurst);
    command.AddValue("sionnaIpcEnabled", "Apply live Sionna packet error state", sionnaIpcEnabled); command.AddValue("sionnaStateFile", "Sionna state JSONL", sionnaStateFile); command.AddValue("sionnaPollIntervalMs", "Sionna state polling period", sionnaPollIntervalMs); command.AddValue("sionnaMaxStateTtlMs", "Sionna maximum state age", sionnaMaxStateTtlMs);
    command.AddValue("configHash", "Resolved stock config hash", configHash); command.AddValue("eventsFile", "JSONL trace output", eventsFile); command.AddValue("pcapPrefix", "PCAP prefix", pcapPrefix); command.AddValue("readyFile", "Readiness JSON", readyFile); command.AddValue("stopFile", "Stop marker", stopFile); command.Parse(argc, argv);
    if (uavCount < 1 || uavCount > MAX_UAVS || queueMaxPackets != 100 || eventsFile.empty() || configHash.size() != 64) { std::cerr << "invalid stock packet engine configuration\n"; return 2; }
    const std::vector<std::string> taps = SplitCsv(tapUavs);
    if (taps.size() != uavCount) { std::cerr << "tapUavs must match uavCount\n"; return 2; }
    g_controlTos = static_cast<uint8_t>(controlTos); g_payloadTos = static_cast<uint8_t>(payloadTos); g_additionalDataTos = static_cast<uint8_t>(additionalDataTos);
    RngSeedManager::SetSeed(seed); RngSeedManager::SetRun(run);
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl")); GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));
    SionnaStateTable states(sionnaIpcEnabled, sionnaStateFile, sionnaPollIntervalMs, sionnaMaxStateTtlMs);
    EventLog log(eventsFile, eventEpoch, queueMaxPackets, &states);
    NodeContainer routers, ghosts; routers.Create(uavCount + 1); ghosts.Create(uavCount + 1); InternetStackHelper internet; internet.Install(routers);
    CsmaHelper external; external.SetChannelAttribute("DataRate", StringValue("1Gbps")); external.SetChannelAttribute("Delay", StringValue("10us"));
    NetDeviceContainer endpointDevices, routerExternal;
    for (uint32_t index = 0; index <= uavCount; ++index) { NodeContainer segment(ghosts.Get(index), routers.Get(index)); NetDeviceContainer devices = external.Install(segment); devices.Get(0)->SetAddress(EndpointMac(index)); devices.Get(1)->SetAddress(RouterMac(index)); endpointDevices.Add(devices.Get(0)); routerExternal.Add(devices.Get(1)); AddInterface(routers.Get(index), devices.Get(1), RouterIp(index)); }
    CsmaHelper radio; radio.SetChannelAttribute("DataRate", StringValue(radioRate)); radio.SetChannelAttribute("Delay", StringValue(radioDelay));
    NetDeviceContainer radioDevices = radio.Install(routers);
    for (uint32_t index = 0; index < radioDevices.GetN(); ++index) {
        const std::string name = index == 0 ? "cp" : "uav" + std::to_string(index);
        radioDevices.Get(index)->SetAddress(RadioMac(index)); AddInterface(routers.Get(index), radioDevices.Get(index), RadioIp(index));
        Ptr<CsmaNetDevice> device = DynamicCast<CsmaNetDevice>(radioDevices.Get(index)); Ptr<Queue<Packet>> queue = device->GetQueue();
        queue->TraceConnectWithoutContext("Enqueue", MakeBoundCallback(&TraceQueueEnqueue, &log, name, queue)); queue->TraceConnectWithoutContext("Dequeue", MakeBoundCallback(&TraceQueueDequeue, &log, name, queue)); queue->TraceConnectWithoutContext("Drop", MakeBoundCallback(&TraceQueueDrop, &log, name, queue));
        device->TraceConnectWithoutContext("PhyTxBegin", MakeBoundCallback(&TraceChannel, &log, name)); device->TraceConnectWithoutContext("MacTxBackoff", MakeBoundCallback(&TraceBackoff, &log, name)); device->TraceConnectWithoutContext("PhyTxEnd", MakeBoundCallback(&TracePhyTxEnd, &log, name)); device->TraceConnectWithoutContext("PhyRxDrop", MakeBoundCallback(&TracePhyRxDrop, &log, name));
        routerExternal.Get(index)->TraceConnectWithoutContext("MacRx", MakeBoundCallback(&TraceIngress, &log, name)); endpointDevices.Get(index)->TraceConnectWithoutContext("MacRx", MakeBoundCallback(&TraceEgress, &log, name));
        if (sionnaIpcEnabled) { Ptr<AmsStockSionnaPacketErrorModel> error = CreateObject<AmsStockSionnaPacketErrorModel>(); error->Configure(&states, name); device->SetReceiveErrorModel(error); }
    }
    for (uint32_t index = 0; index <= uavCount; ++index) { AddPermanentArp(routers.Get(index), routerExternal.Get(index), EndpointIp(index), EndpointMac(index)); for (uint32_t peer = 0; peer <= uavCount; ++peer) if (peer != index) AddPermanentArp(routers.Get(index), radioDevices.Get(index), RadioIp(peer), RadioMac(peer)); }
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();
    const Ipv4Address multicastSource(EndpointIp(0).c_str()), multicastGroup("239.71.0.1"); Ipv4StaticRoutingHelper multicast; NetDeviceContainer gcsOut; gcsOut.Add(radioDevices.Get(0)); multicast.AddMulticastRoute(routers.Get(0), multicastSource, multicastGroup, routerExternal.Get(0), gcsOut);
    for (uint32_t index = 1; index <= uavCount; ++index) { NetDeviceContainer output; output.Add(routerExternal.Get(index)); multicast.AddMulticastRoute(routers.Get(index), multicastSource, multicastGroup, radioDevices.Get(index), output); }
    if (!selfTest) { TapBridgeHelper bridge; bridge.SetAttribute("Mode", StringValue("UseBridge")); bridge.SetAttribute("DeviceName", StringValue(tapGcs)); bridge.Install(ghosts.Get(0), endpointDevices.Get(0)); for (uint32_t index = 1; index <= uavCount; ++index) { bridge.SetAttribute("DeviceName", StringValue(taps[index - 1])); bridge.Install(ghosts.Get(index), endpointDevices.Get(index)); } Simulator::Schedule(MilliSeconds(100), &PollStopFile, stopFile); }
    if (!pcapPrefix.empty()) for (uint32_t index = 0; index < radioDevices.GetN(); ++index) radio.EnablePcap(pcapPrefix + "-radio-" + (index == 0 ? "cp" : "uav" + std::to_string(index)) + ".pcap", radioDevices.Get(index), true, true);
    states.Start(); Simulator::Schedule(MilliSeconds(1), &WriteReady, readyFile, configHash, eventEpoch, uavCount); Simulator::Stop(MilliSeconds(selfTest ? std::min<uint64_t>(durationMs, 200) : durationMs)); Simulator::Run(); Simulator::Destroy();
    std::cout << "{\"status\":\"passed\",\"contract\":\"" << CONTRACT << "\",\"native_backoff_events\":" << log.Count("backoff") << ",\"native_retries\":" << log.Count("backoff") << ",\"self_test_burst\":" << selfTestBurst << "}\n";
    return 0;
}
