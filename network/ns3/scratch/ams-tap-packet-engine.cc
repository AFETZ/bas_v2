#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/tap-bridge-module.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <tuple>
#include <unistd.h>
#include <utility>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsTapPacketEngine");

namespace
{

constexpr const char* CONTRACT = "ams.tap_packet_engine/v2";
constexpr const char* EVENT_SCHEMA = "ams.ns3.packet_event/v1";
constexpr uint32_t MAX_UAVS = 5;
constexpr uint32_t MAX_QUEUE_PACKETS = 1000000;
constexpr uint64_t MAX_DURATION_MS = 86400000;
constexpr uint32_t MAX_SIONNA_STATE_CELLS = 64;
constexpr uint32_t MAX_SIONNA_LINE_BYTES = 65536;
constexpr uint32_t MAX_RADIO_LINEAGE_CACHE = 100000;
uint8_t g_controlTos = 184;
uint8_t g_payloadTos = 40;
uint8_t g_additionalDataTos = 0;

class Sha256
{
  public:
    Sha256()
        : m_state{0x6a09e667,
                  0xbb67ae85,
                  0x3c6ef372,
                  0xa54ff53a,
                  0x510e527f,
                  0x9b05688c,
                  0x1f83d9ab,
                  0x5be0cd19}
    {
    }

    void Update(const uint8_t* data, std::size_t length)
    {
        for (std::size_t i = 0; i < length; ++i)
        {
            m_block[m_blockLength++] = data[i];
            if (m_blockLength == 64)
            {
                Transform();
                m_bitLength += 512;
                m_blockLength = 0;
            }
        }
    }

    std::array<uint8_t, 32> Final()
    {
        const uint32_t originalLength = m_blockLength;
        m_block[m_blockLength++] = 0x80;
        if (m_blockLength > 56)
        {
            while (m_blockLength < 64)
            {
                m_block[m_blockLength++] = 0;
            }
            Transform();
            m_blockLength = 0;
        }
        while (m_blockLength < 56)
        {
            m_block[m_blockLength++] = 0;
        }
        m_bitLength += static_cast<uint64_t>(originalLength) * 8;
        for (uint32_t i = 0; i < 8; ++i)
        {
            m_block[63 - i] = static_cast<uint8_t>(m_bitLength >> (i * 8));
        }
        Transform();

        std::array<uint8_t, 32> digest{};
        for (uint32_t i = 0; i < 8; ++i)
        {
            digest[i * 4] = static_cast<uint8_t>(m_state[i] >> 24);
            digest[i * 4 + 1] = static_cast<uint8_t>(m_state[i] >> 16);
            digest[i * 4 + 2] = static_cast<uint8_t>(m_state[i] >> 8);
            digest[i * 4 + 3] = static_cast<uint8_t>(m_state[i]);
        }
        return digest;
    }

  private:
    static uint32_t RotateRight(uint32_t value, uint32_t count)
    {
        return (value >> count) | (value << (32 - count));
    }

    void Transform()
    {
        static constexpr std::array<uint32_t, 64> K = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
            0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
            0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
            0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
            0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
            0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
            0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
            0xc67178f2};
        std::array<uint32_t, 64> words{};
        for (uint32_t i = 0; i < 16; ++i)
        {
            words[i] = (static_cast<uint32_t>(m_block[i * 4]) << 24) |
                       (static_cast<uint32_t>(m_block[i * 4 + 1]) << 16) |
                       (static_cast<uint32_t>(m_block[i * 4 + 2]) << 8) |
                       static_cast<uint32_t>(m_block[i * 4 + 3]);
        }
        for (uint32_t i = 16; i < 64; ++i)
        {
            const uint32_t s0 = RotateRight(words[i - 15], 7) ^ RotateRight(words[i - 15], 18) ^
                                (words[i - 15] >> 3);
            const uint32_t s1 = RotateRight(words[i - 2], 17) ^ RotateRight(words[i - 2], 19) ^
                                (words[i - 2] >> 10);
            words[i] = words[i - 16] + s0 + words[i - 7] + s1;
        }

        uint32_t a = m_state[0];
        uint32_t b = m_state[1];
        uint32_t c = m_state[2];
        uint32_t d = m_state[3];
        uint32_t e = m_state[4];
        uint32_t f = m_state[5];
        uint32_t g = m_state[6];
        uint32_t h = m_state[7];
        for (uint32_t i = 0; i < 64; ++i)
        {
            const uint32_t s1 = RotateRight(e, 6) ^ RotateRight(e, 11) ^ RotateRight(e, 25);
            const uint32_t choice = (e & f) ^ (~e & g);
            const uint32_t temp1 = h + s1 + choice + K[i] + words[i];
            const uint32_t s0 = RotateRight(a, 2) ^ RotateRight(a, 13) ^ RotateRight(a, 22);
            const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t temp2 = s0 + majority;
            h = g;
            g = f;
            f = e;
            e = d + temp1;
            d = c;
            c = b;
            b = a;
            a = temp1 + temp2;
        }
        m_state[0] += a;
        m_state[1] += b;
        m_state[2] += c;
        m_state[3] += d;
        m_state[4] += e;
        m_state[5] += f;
        m_state[6] += g;
        m_state[7] += h;
    }

    std::array<uint8_t, 64> m_block{};
    uint32_t m_blockLength = 0;
    uint64_t m_bitLength = 0;
    std::array<uint32_t, 8> m_state;
};

std::string
Sha256Hex(const uint8_t* data, std::size_t length)
{
    Sha256 sha;
    sha.Update(data, length);
    const auto digest = sha.Final();
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (uint8_t byte : digest)
    {
        out << std::setw(2) << static_cast<uint32_t>(byte);
    }
    return out.str();
}

std::string
PacketSha256(Ptr<const Packet> packet)
{
    std::vector<uint8_t> bytes(packet->GetSize());
    if (!bytes.empty())
    {
        packet->CopyData(bytes.data(), bytes.size());
    }
    return Sha256Hex(bytes.data(), bytes.size());
}

std::string
JsonEscape(const std::string& value)
{
    std::ostringstream out;
    for (unsigned char character : value)
    {
        switch (character)
        {
        case '\\':
            out << "\\\\";
            break;
        case '"':
            out << "\\\"";
            break;
        case '\n':
            out << "\\n";
            break;
        case '\r':
            out << "\\r";
            break;
        case '\t':
            out << "\\t";
            break;
        default:
            if (character < 0x20)
            {
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                    << static_cast<uint32_t>(character) << std::dec;
            }
            else
            {
                out << character;
            }
        }
    }
    return out.str();
}

std::string
MacToString(const Mac48Address& address)
{
    std::ostringstream out;
    out << address;
    return out.str();
}

std::string
Ipv4ToString(const Ipv4Address& address)
{
    std::ostringstream out;
    out << address;
    return out.str();
}

struct FrameMetadata
{
    bool parsedEthernet = false;
    bool ipv4 = false;
    bool p2mp = false;
    bool classMapped = false;
    uint8_t tos = 0;
    int32_t dscp = -1;
    int32_t transportProtocol = -1;
    int32_t sourceUdpPort = -1;
    int32_t destinationUdpPort = -1;
    int64_t transportPayloadSize = -1;
    std::string trafficClass = "unclassified";
    std::string sourceMac = "unknown";
    std::string destinationMac = "unknown";
    std::string sourceIp = "unknown";
    std::string destinationIp = "unknown";
    std::string transportPayloadSha256;
    bool sourceMonotonicValid = false;
    uint64_t sourceMonotonicNs = 0;
    int32_t applicationProfileId = -1;
    int32_t applicationUavId = -1;
};

uint64_t
ReadNetworkU64(const uint8_t* data)
{
    uint64_t value = 0;
    for (uint32_t index = 0; index < 8; ++index)
    {
        value = (value << 8) | data[index];
    }
    return value;
}

void
InspectApplicationHeader(Ptr<const Packet> payload, FrameMetadata& metadata)
{
    // The product packet formats carry their source CLOCK_MONOTONIC timestamp
    // in network byte order.  Reading only the bounded prefix keeps admission
    // independent of payload size and does not parse MAVLink serial bytes.
    std::array<uint8_t, 30> prefix{};
    const uint32_t copied = payload->CopyData(prefix.data(),
                                              std::min<uint32_t>(payload->GetSize(),
                                                                 static_cast<uint32_t>(
                                                                     prefix.size())));
    if (copied >= 20 && prefix[4] == 1 &&
        (std::memcmp(prefix.data(), "BQO1", 4) == 0 ||
         std::memcmp(prefix.data(), "BDP1", 4) == 0))
    {
        metadata.sourceMonotonicNs = ReadNetworkU64(prefix.data() + 12);
        metadata.sourceMonotonicValid = metadata.sourceMonotonicNs > 0;
        if (std::memcmp(prefix.data(), "BQO1", 4) == 0)
        {
            metadata.applicationProfileId = prefix[5];
            metadata.applicationUavId = prefix[7];
        }
        else
        {
            const uint32_t sender = prefix[6];
            const uint32_t receiver = prefix[7];
            metadata.applicationUavId = sender >= 1 && sender <= MAX_UAVS
                                                ? static_cast<int32_t>(sender)
                                                : (receiver >= 1 && receiver <= MAX_UAVS
                                                       ? static_cast<int32_t>(receiver)
                                                       : -1);
        }
        return;
    }
    if (copied >= 30 && prefix[4] == 1 && std::memcmp(prefix.data(), "BSF1", 4) == 0)
    {
        metadata.applicationUavId = prefix[6];
        metadata.sourceMonotonicNs = ReadNetworkU64(prefix.data() + 22);
        metadata.sourceMonotonicValid = metadata.sourceMonotonicNs > 0;
    }
}

FrameMetadata
InspectFrame(Ptr<const Packet> packet, bool hashTransportPayload = true)
{
    FrameMetadata metadata;
    Ptr<Packet> copy = packet->Copy();
    EthernetHeader ethernet(false);
    EthernetTrailer trailer;
    if (copy->GetSize() < ethernet.GetSerializedSize() + trailer.GetSerializedSize())
    {
        return metadata;
    }
    copy->RemoveTrailer(trailer);
    if (copy->RemoveHeader(ethernet) == 0)
    {
        return metadata;
    }
    metadata.parsedEthernet = true;
    metadata.sourceMac = MacToString(ethernet.GetSource());
    metadata.destinationMac = MacToString(ethernet.GetDestination());
    uint16_t etherType = ethernet.GetLengthType();
    if (etherType <= 1500)
    {
        LlcSnapHeader llc;
        if (copy->GetSize() < llc.GetSerializedSize() || copy->RemoveHeader(llc) == 0)
        {
            return metadata;
        }
        etherType = llc.GetType();
    }
    if (etherType == 0x0806)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "control";
        metadata.dscp = 0;
        return metadata;
    }
    if (etherType != 0x0800)
    {
        return metadata;
    }

    Ipv4Header ipv4;
    if (copy->GetSize() < ipv4.GetSerializedSize() || copy->RemoveHeader(ipv4) == 0)
    {
        return metadata;
    }
    metadata.ipv4 = true;
    metadata.sourceIp = Ipv4ToString(ipv4.GetSource());
    metadata.destinationIp = Ipv4ToString(ipv4.GetDestination());
    metadata.p2mp = ipv4.GetDestination().IsMulticast();
    metadata.tos = ipv4.GetTos();
    metadata.dscp = static_cast<int32_t>(metadata.tos >> 2);
    metadata.transportProtocol = ipv4.GetProtocol();
    if (metadata.tos == g_controlTos)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "control";
    }
    else if (metadata.tos == g_payloadTos)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "payload";
    }
    else if (metadata.tos == g_additionalDataTos)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "additional_data";
    }

    if (metadata.transportProtocol == 17)
    {
        UdpHeader udp;
        const uint32_t udpHeaderSize = udp.GetSerializedSize();
        const uint32_t ipv4PayloadSize = ipv4.GetPayloadSize();
        if (ipv4PayloadSize >= udpHeaderSize && copy->GetSize() >= ipv4PayloadSize &&
            copy->RemoveHeader(udp) == udpHeaderSize)
        {
            const uint32_t udpPayloadSize = ipv4PayloadSize - udpHeaderSize;
            if (copy->GetSize() >= udpPayloadSize)
            {
                if (copy->GetSize() > udpPayloadSize)
                {
                    copy->RemoveAtEnd(copy->GetSize() - udpPayloadSize);
                }
                metadata.sourceUdpPort = udp.GetSourcePort();
                metadata.destinationUdpPort = udp.GetDestinationPort();
                metadata.transportPayloadSize = udpPayloadSize;
                if (hashTransportPayload)
                {
                    metadata.transportPayloadSha256 = PacketSha256(copy);
                }
                InspectApplicationHeader(copy, metadata);
            }
        }
    }
    return metadata;
}

uint32_t
ClassIndex(const FrameMetadata& metadata)
{
    if (metadata.trafficClass == "control")
    {
        return 0;
    }
    if (metadata.trafficClass == "payload")
    {
        return 1;
    }
    if (metadata.trafficClass == "additional_data")
    {
        return 2;
    }
    return 3;
}

bool
IsAllowedEndpointUdpPort(int32_t destinationPort)
{
    if (destinationPort == 14900)
    {
        return true;
    }
    for (int32_t base : {14600, 14700, 14800})
    {
        if (destinationPort >= base && destinationPort <= base + static_cast<int32_t>(MAX_UAVS))
        {
            return true;
        }
    }
    return false;
}

uint64_t
SteadyNowNs()
{
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                     std::chrono::steady_clock::now().time_since_epoch())
                                     .count());
}

std::optional<std::string>
JsonStringField(const std::string& line, const std::string& key)
{
    const std::regex pattern("\\\"" + key + "\\\":\\\"([^\\\"]*)\\\"");
    std::smatch match;
    if (!std::regex_search(line, match, pattern))
    {
        return std::nullopt;
    }
    return match[1].str();
}

std::optional<uint64_t>
JsonUintField(const std::string& line, const std::string& key)
{
    const std::regex pattern("\\\"" + key + "\\\":([0-9]+)");
    std::smatch match;
    if (!std::regex_search(line, match, pattern))
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
JsonDoubleField(const std::string& line, const std::string& key)
{
    const std::regex pattern("\\\"" + key +
                             "\\\":(-?(?:[0-9]+(?:\\.[0-9]+)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?)");
    std::smatch match;
    if (!std::regex_search(line, match, pattern))
    {
        return std::nullopt;
    }
    try
    {
        const double value = std::stod(match[1].str());
        return std::isfinite(value) ? std::optional<double>(value) : std::nullopt;
    }
    catch (const std::exception&)
    {
        return std::nullopt;
    }
}

bool
VerifyStateLineHash(const std::string& line)
{
    static const std::regex token(",\\\"state_sha256\\\":\\\"([0-9a-f]{64})\\\"");
    std::smatch match;
    if (!std::regex_search(line, match, token))
    {
        return false;
    }
    std::string canonical = line;
    canonical.erase(static_cast<std::size_t>(match.position()),
                    static_cast<std::size_t>(match.length()));
    return Sha256Hex(reinterpret_cast<const uint8_t*>(canonical.data()), canonical.size()) ==
           match[1].str();
}

std::string
CanonicalPacketDevice(const std::string& device)
{
    return device == "gcs" ? "cp" : device;
}

std::string
DeviceFromEndpointIp(const std::string& ip)
{
    for (uint32_t index = 0; index <= MAX_UAVS; ++index)
    {
        const std::string expected = "10.71." + std::to_string(index) + ".10";
        if (ip == expected)
        {
            return index == 0 ? "cp" : "uav" + std::to_string(index);
        }
    }
    return "unknown";
}

std::string
RadioCellForFrame(const FrameMetadata& metadata, const std::string& observedDevice)
{
    if (metadata.p2mp)
    {
        return "unsupported>p2mp";
    }
    const std::string source = CanonicalPacketDevice(observedDevice);
    const std::string destination = DeviceFromEndpointIp(metadata.destinationIp);
    return source + ">" + destination;
}

bool
IsSupportedRadioCell(const std::string& cell, const std::string& trafficClass)
{
    if (trafficClass != "control" && trafficClass != "payload" && trafficClass != "additional_data")
    {
        return false;
    }
    for (uint32_t index = 1; index <= MAX_UAVS; ++index)
    {
        const std::string uav = "uav" + std::to_string(index);
        if (cell == "cp>" + uav || cell == uav + ">cp")
        {
            return true;
        }
    }
    return false;
}

struct RadioState
{
    bool available = false;
    std::string status = "missing";
    uint64_t stateSequence = 0;
    std::string directedLink;
    std::string trafficClass;
    std::string queryId;
    std::string appliedStateId;
    std::string resultWireSha256;
    std::string stateSha256;
    uint64_t validityStartMonotonicNs = 0;
    uint64_t adapterAppliedMonotonicNs = 0;
    uint64_t localExpiresSteadyNs = 0;
    uint64_t propagationDelayNs = 0;
    uint64_t serviceRateBps = 0;
    double lossProbability = 1.0;
    uint64_t mappingSeed = 0;
    std::string mappingVersion;
};

class RadioStateTable
{
  public:
    RadioStateTable(bool enabled,
                    std::string path,
                    uint32_t pollIntervalMs,
                    uint32_t maxUpdatesPerPoll,
                    uint32_t maxStateTtlMs,
                    uint64_t radioCapacityBps)
        : m_enabled(enabled),
          m_path(std::move(path)),
          m_pollIntervalMs(pollIntervalMs),
          m_maxUpdatesPerPoll(maxUpdatesPerPoll),
          m_maxStateTtlNs(static_cast<uint64_t>(maxStateTtlMs) * 1000000ULL),
          m_radioCapacityBps(radioCapacityBps)
    {
    }

    bool Enabled() const
    {
        return m_enabled;
    }

    bool Faulted() const
    {
        return !m_fault.empty();
    }

    const std::string& Fault() const
    {
        return m_fault;
    }

    void Start()
    {
        if (m_enabled)
        {
            Simulator::ScheduleNow(&RadioStateTable::Poll, this);
        }
    }

    RadioState Lookup(const std::string& directedLink, const std::string& trafficClass) const
    {
        RadioState unavailable;
        unavailable.directedLink = directedLink;
        unavailable.trafficClass = trafficClass;
        if (!m_enabled)
        {
            unavailable.status = "disabled";
            return unavailable;
        }
        if (!m_fault.empty())
        {
            unavailable.status = "ipc_fault";
            return unavailable;
        }
        const auto found = m_states.find(directedLink + "|" + trafficClass);
        if (found == m_states.end())
        {
            unavailable.status = "missing";
            return unavailable;
        }
        RadioState state = found->second;
        if (!state.available)
        {
            return state;
        }
        if (SteadyNowNs() >= state.localExpiresSteadyNs)
        {
            state.available = false;
            state.status = "expired";
        }
        return state;
    }

  private:
    void FailClosed(const std::string& reason)
    {
        if (m_fault.empty())
        {
            m_fault = reason;
        }
        m_states.clear();
    }

    void Poll()
    {
        if (!m_enabled)
        {
            return;
        }
        try
        {
            if (std::filesystem::exists(m_path))
            {
                const uint64_t size = std::filesystem::file_size(m_path);
                if (size < m_offset)
                {
                    FailClosed("state_ipc_truncated");
                }
                else
                {
                    std::ifstream input(m_path, std::ios::in | std::ios::binary);
                    if (!input)
                    {
                        FailClosed("state_ipc_open_failed");
                    }
                    else
                    {
                        input.seekg(static_cast<std::streamoff>(m_offset));
                        std::string line;
                        uint32_t processed = 0;
                        char byte = '\0';
                        // The producer appends JSONL concurrently.  EOF without a
                        // newline is therefore an incomplete record, not malformed
                        // input: keep m_offset at the beginning of that tail and
                        // retry it on the next poll.  Complete invalid records still
                        // fail closed below.
                        while (processed < m_maxUpdatesPerPoll && input.get(byte))
                        {
                            if (byte != '\n')
                            {
                                line.push_back(byte);
                                if (line.size() > MAX_SIONNA_LINE_BYTES)
                                {
                                    FailClosed("state_ipc_line_too_large");
                                    break;
                                }
                                continue;
                            }
                            ++processed;
                            if (!ApplyLine(line))
                            {
                                FailClosed("state_ipc_invalid_record");
                                break;
                            }
                            m_offset += line.size() + 1;
                            line.clear();
                        }
                    }
                }
            }
        }
        catch (const std::exception&)
        {
            FailClosed("state_ipc_poll_exception");
        }
        Simulator::Schedule(MilliSeconds(m_pollIntervalMs), &RadioStateTable::Poll, this);
    }

    bool ApplyLine(const std::string& line)
    {
        const auto schema = JsonStringField(line, "schema");
        const auto sequence = JsonUintField(line, "state_sequence");
        const auto availability = JsonStringField(line, "availability");
        const auto directedLink = JsonStringField(line, "directed_link");
        const auto trafficClass = JsonStringField(line, "traffic_class");
        const auto stateHash = JsonStringField(line, "state_sha256");
        if (!schema || *schema != "ams.sionna.packet_state/v1" || !sequence ||
            *sequence <= m_lastSequence || !availability || !directedLink || !trafficClass ||
            !stateHash || !VerifyStateLineHash(line))
        {
            return false;
        }
        if (*trafficClass != "control" && *trafficClass != "payload" &&
            *trafficClass != "additional_data")
        {
            return false;
        }
        m_lastSequence = *sequence;
        const std::string key = *directedLink + "|" + *trafficClass;
        if (*availability == "unavailable")
        {
            RadioState unavailable;
            unavailable.status = "unavailable";
            unavailable.stateSequence = *sequence;
            unavailable.directedLink = *directedLink;
            unavailable.trafficClass = *trafficClass;
            unavailable.stateSha256 = *stateHash;
            m_states[key] = unavailable;
            return true;
        }
        if (*availability != "fresh")
        {
            return false;
        }
        const auto queryId = JsonStringField(line, "query_id");
        const auto appliedStateId = JsonStringField(line, "applied_state_id");
        const auto resultHash = JsonStringField(line, "result_wire_sha256");
        const auto validityStart = JsonUintField(line, "validity_start_monotonic_ns");
        const auto expiry = JsonUintField(line, "expires_monotonic_ns");
        const auto appliedAt = JsonUintField(line, "adapter_applied_monotonic_ns");
        const auto delay = JsonUintField(line, "propagation_delay_ns");
        const auto serviceRate = JsonUintField(line, "service_rate_bps");
        const auto loss = JsonDoubleField(line, "loss_probability");
        const auto mappingSeed = JsonUintField(line, "mapping_seed");
        const auto mappingVersion = JsonStringField(line, "mapping_version");
        if (!queryId || !appliedStateId || !resultHash || !validityStart || !expiry || !appliedAt ||
            !delay || !serviceRate || !loss || !mappingSeed || !mappingVersion ||
            *validityStart > *appliedAt || *expiry <= *appliedAt || *loss < 0.0 || *loss > 1.0 ||
            (*serviceRate != 0 && *serviceRate != 1000 && *serviceRate != 10000 &&
             *serviceRate != 100000 && *serviceRate != 500000 && *serviceRate != 2000000 &&
             *serviceRate != m_radioCapacityBps))
        {
            return false;
        }
        const uint64_t validityTtl = *expiry - *appliedAt;
        if (validityTtl == 0 || validityTtl > m_maxStateTtlNs)
        {
            return false;
        }
        if (m_states.find(key) == m_states.end() && m_states.size() >= MAX_SIONNA_STATE_CELLS)
        {
            return false;
        }
        RadioState state;
        state.available = true;
        state.status = "fresh";
        state.stateSequence = *sequence;
        state.directedLink = *directedLink;
        state.trafficClass = *trafficClass;
        state.queryId = *queryId;
        state.appliedStateId = *appliedStateId;
        state.resultWireSha256 = *resultHash;
        state.stateSha256 = *stateHash;
        state.validityStartMonotonicNs = *validityStart;
        state.adapterAppliedMonotonicNs = *appliedAt;
        // Python time.monotonic_ns() and Linux std::chrono::steady_clock use
        // the same host CLOCK_MONOTONIC domain in the qualified runtime.  Do
        // not add the original TTL again at IPC read time: doing so would
        // silently extend a state beyond the provider-declared expiry.
        state.localExpiresSteadyNs = *expiry;
        state.propagationDelayNs = *delay;
        state.serviceRateBps = *serviceRate;
        state.lossProbability = *loss;
        state.mappingSeed = *mappingSeed;
        state.mappingVersion = *mappingVersion;
        m_states[key] = state;
        return true;
    }

    bool m_enabled;
    std::string m_path;
    uint32_t m_pollIntervalMs;
    uint32_t m_maxUpdatesPerPoll;
    uint64_t m_maxStateTtlNs;
    uint64_t m_radioCapacityBps;
    uint64_t m_offset = 0;
    uint64_t m_lastSequence = 0;
    std::string m_fault;
    std::map<std::string, RadioState> m_states;
};

struct RadioDecision
{
    bool present = false;
    bool drop = false;
    std::string status = "not_decided";
    std::string dropReason;
    RadioState state;
    double lossSample = 0.0;
    std::string delivery = "unavailable";
    std::string intervention = "natural";
    uint64_t rateAppliedAtMonotonicNs = 0;
    uint64_t delayAppliedAtMonotonicNs = 0;
    uint64_t serializationTimeNs = 0;
    uint64_t baseSerializationTimeNs = 0;
    uint64_t servicePaddingNs = 0;
    uint64_t baseChannelDelayNs = 0;
    uint64_t effectiveChannelDelayNs = 0;
    std::string appliedDeviceId;
};

double
DeterministicLossSample(const std::string& packetHash,
                        const std::string& appliedStateId,
                        uint64_t mappingSeed)
{
    std::string material = packetHash;
    material.push_back('\0');
    material += appliedStateId;
    material.push_back('\0');
    material += std::to_string(mappingSeed);
    Sha256 sha;
    sha.Update(reinterpret_cast<const uint8_t*>(material.data()), material.size());
    const auto digest = sha.Final();
    uint64_t prefix = 0;
    for (uint32_t index = 0; index < 8; ++index)
    {
        prefix = (prefix << 8) | digest[index];
    }
    const uint64_t numerator = prefix >> 11;
    return static_cast<double>(numerator) / 9007199254740992.0;
}

class RadioController
{
  public:
    RadioController(bool enabled, RadioStateTable* states, std::string intervention)
        : m_enabled(enabled),
          m_states(states),
          m_intervention(std::move(intervention))
    {
    }

    bool Enabled() const
    {
        return m_enabled;
    }

    void SetChannel(Ptr<CsmaChannel> channel)
    {
        m_channel = channel;
        if (m_channel)
        {
            m_baseChannelDelay = m_channel->GetDelay();
        }
    }

    RadioDecision Decide(const std::string& observedDevice, Ptr<const Packet> packet)
    {
        RadioDecision decision;
        if (!m_enabled)
        {
            decision.status = "disabled";
            return decision;
        }
        const auto cached = DecisionFor(packet);
        if (cached)
        {
            return *cached;
        }
        decision.present = true;
        decision.intervention = m_intervention;
        const FrameMetadata metadata = InspectFrame(packet);
        const std::string cell = RadioCellForFrame(metadata, observedDevice);
        // The v1 adapter publishes exactly the 30 unicast CP<->UAV/class
        // cells.  Multicast and non-IP maintenance traffic retain the locked
        // M3 behavior instead of being confused with missing Sionna state.
        if (!IsSupportedRadioCell(cell, metadata.trafficClass))
        {
            decision.present = false;
            decision.status = "not_applicable";
            return decision;
        }
        decision.state = m_states->Lookup(cell, metadata.trafficClass);
        decision.status = decision.state.status;
        if (!decision.state.available)
        {
            decision.drop = true;
            decision.dropReason = "sionna_state_" + decision.status;
            decision.delivery = "drop";
            return Remember(packet, decision);
        }
        if (decision.state.serviceRateBps == 0)
        {
            decision.drop = true;
            decision.dropReason = "sionna_service_rate_zero";
            decision.delivery = "drop";
            return Remember(packet, decision);
        }
        const std::string packetHash = metadata.transportPayloadSha256.empty()
                                           ? PacketSha256(packet)
                                           : metadata.transportPayloadSha256;
        decision.lossSample = DeterministicLossSample(packetHash,
                                                      decision.state.appliedStateId,
                                                      decision.state.mappingSeed);
        if (m_intervention == "force_drop")
        {
            decision.drop = true;
        }
        else if (m_intervention == "force_deliver")
        {
            decision.drop = false;
        }
        else
        {
            decision.drop = decision.lossSample < decision.state.lossProbability;
        }
        decision.dropReason = decision.drop ? "sionna_loss" : "";
        decision.delivery = decision.drop ? "drop" : "deliver";
        return Remember(packet, decision);
    }

    RadioDecision ApplyForTransmit(const std::string& observedDevice,
                                   Ptr<const Packet> packet,
                                   Ptr<CsmaNetDevice> device)
    {
        RadioDecision decision = Revalidate(observedDevice, packet, "before_transmit");
        if (m_channel)
        {
            // Every successful carrier acquisition starts from the immutable
            // base channel delay.  No not-applicable, late-drop, or next-link
            // packet may inherit the previous UID's service padding.
            m_channel->SetAttribute("Delay", TimeValue(m_baseChannelDelay));
            decision.baseChannelDelayNs =
                static_cast<uint64_t>(m_baseChannelDelay.GetNanoSeconds());
            m_decisions[packet->GetUid()] = decision;
        }
        if (!decision.present || decision.drop || decision.state.serviceRateBps == 0 || !device ||
            !m_channel)
        {
            return decision;
        }
        const DataRate rate(decision.state.serviceRateBps);
        const DataRate baseRate = m_channel->GetDataRate();
        decision.serializationTimeNs =
            static_cast<uint64_t>(rate.CalculateBytesTxTime(packet->GetSize()).GetNanoSeconds());
        decision.baseSerializationTimeNs = static_cast<uint64_t>(
            baseRate.CalculateBytesTxTime(packet->GetSize()).GetNanoSeconds());
        decision.servicePaddingNs =
            decision.serializationTimeNs > decision.baseSerializationTimeNs
                ? decision.serializationTimeNs - decision.baseSerializationTimeNs
                : 0;
        if (decision.servicePaddingNs >
            std::numeric_limits<uint64_t>::max() - decision.state.propagationDelayNs)
        {
            decision.drop = true;
            decision.status = "service_time_overflow";
            decision.dropReason = "sionna_service_time_overflow";
            decision.delivery = "drop";
            m_decisions[packet->GetUid()] = decision;
            return decision;
        }
        // CsmaNetDevice already occupies TRANSMITTING for the base channel
        // serialization time.  Add only the missing packet-scoped padding to
        // CsmaChannel propagation; this gives exact total occupancy of
        // desired serialization + physical propagation without double count.
        decision.effectiveChannelDelayNs =
            decision.state.propagationDelayNs + decision.servicePaddingNs;
        m_channel->SetAttribute("Delay", TimeValue(NanoSeconds(decision.effectiveChannelDelayNs)));
        const uint64_t appliedAt = SteadyNowNs();
        decision.rateAppliedAtMonotonicNs = appliedAt;
        decision.delayAppliedAtMonotonicNs = appliedAt;
        decision.appliedDeviceId = CanonicalPacketDevice(observedDevice) + ".radio";
        m_decisions[packet->GetUid()] = decision;
        return decision;
    }

    RadioDecision RevalidateForDequeue(const std::string& observedDevice, Ptr<const Packet> packet)
    {
        return Revalidate(observedDevice, packet, "in_queue");
    }

    std::optional<RadioDecision> DecisionFor(Ptr<const Packet> packet) const
    {
        const auto found = m_decisions.find(packet->GetUid());
        return found == m_decisions.end() ? std::nullopt
                                          : std::optional<RadioDecision>(found->second);
    }

  private:
    RadioDecision Revalidate(const std::string& observedDevice,
                             Ptr<const Packet> packet,
                             const std::string& boundary)
    {
        RadioDecision decision = Decide(observedDevice, packet);
        if (!decision.present || decision.drop)
        {
            return decision;
        }
        const RadioState current =
            m_states->Lookup(decision.state.directedLink, decision.state.trafficClass);
        const uint64_t now = SteadyNowNs();
        const bool expired = now >= decision.state.localExpiresSteadyNs ||
                             (!current.available && current.status == "expired");
        const bool superseded =
            current.stateSequence > decision.state.stateSequence ||
            (current.available && (current.stateSequence != decision.state.stateSequence ||
                                   current.stateSha256 != decision.state.stateSha256));
        if (expired || superseded || !current.available)
        {
            decision.drop = true;
            if (expired)
            {
                decision.status = "expired_" + boundary;
                decision.dropReason = "sionna_state_expired_" + boundary;
            }
            else if (superseded)
            {
                decision.status = "superseded_" + boundary;
                decision.dropReason = "sionna_state_superseded_" + boundary;
            }
            else
            {
                decision.status = "unavailable_" + boundary;
                decision.dropReason = "sionna_state_unavailable_" + boundary;
            }
            decision.delivery = "drop";
            m_decisions[packet->GetUid()] = decision;
        }
        return decision;
    }

    RadioDecision Remember(Ptr<const Packet> packet, RadioDecision decision)
    {
        if (m_decisions.find(packet->GetUid()) == m_decisions.end() &&
            m_decisions.size() >= MAX_RADIO_LINEAGE_CACHE)
        {
            m_decisions.clear();
            decision.drop = true;
            decision.status = "lineage_cache_overflow";
            decision.dropReason = "sionna_lineage_cache_overflow";
            decision.delivery = "drop";
        }
        m_decisions[packet->GetUid()] = std::move(decision);
        return m_decisions.at(packet->GetUid());
    }

    bool m_enabled;
    RadioStateTable* m_states;
    std::string m_intervention;
    Ptr<CsmaChannel> m_channel;
    Time m_baseChannelDelay = NanoSeconds(0);
    std::map<uint64_t, RadioDecision> m_decisions;
};

class AmsRadioReceiveErrorModel : public ErrorModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("ns3::AmsRadioReceiveErrorModel")
                                .SetParent<ErrorModel>()
                                .SetGroupName("Network")
                                .AddConstructor<AmsRadioReceiveErrorModel>();
        return tid;
    }

    void SetController(RadioController* controller)
    {
        m_controller = controller;
    }

    void SetDeviceId(std::string deviceId)
    {
        m_deviceId = std::move(deviceId);
    }

  private:
    bool DoCorrupt(Ptr<Packet> packet) override
    {
        const auto decision = m_controller ? m_controller->DecisionFor(packet) : std::nullopt;
        if (!decision || !decision->drop)
        {
            return false;
        }
        const std::size_t separator = decision->state.directedLink.find('>');
        return separator != std::string::npos &&
               decision->state.directedLink.substr(separator + 1) == m_deviceId;
    }

    void DoReset() override
    {
    }

    RadioController* m_controller = nullptr;
    std::string m_deviceId;
};

using RadioDecisionSink = std::function<RadioDecision(const std::string&, Ptr<const Packet>)>;

using RadioTransmitSink =
    std::function<RadioDecision(const std::string&, Ptr<const Packet>, Ptr<CsmaNetDevice>)>;

using QueueEventSink = std::function<void(const std::string&,
                                          const std::string&,
                                          Ptr<const Packet>,
                                          int64_t,
                                          int64_t,
                                          uint64_t,
                                          const std::string&)>;

uint64_t
FrameAgeStartNs(const FrameMetadata& metadata, uint64_t nowNs)
{
    return metadata.sourceMonotonicValid && metadata.sourceMonotonicNs <= nowNs
               ? metadata.sourceMonotonicNs
               : nowNs;
}

uint64_t
ElapsedNs(uint64_t nowNs, uint64_t startNs)
{
    return nowNs >= startNs ? nowNs - startNs : 0;
}

uint32_t
ResolveFrameUavId(const FrameMetadata& metadata,
                  const std::string& observedDevice,
                  uint32_t uavCount)
{
    if (metadata.applicationUavId >= 1 &&
        metadata.applicationUavId <= static_cast<int32_t>(uavCount))
    {
        return static_cast<uint32_t>(metadata.applicationUavId);
    }
    for (uint32_t uavId = 1; uavId <= uavCount; ++uavId)
    {
        if (observedDevice == "uav" + std::to_string(uavId))
        {
            return uavId;
        }
    }
    for (uint32_t uavId = 1; uavId <= uavCount; ++uavId)
    {
        const std::string endpointIp = "10.71." + std::to_string(uavId) + ".10";
        if (metadata.sourceIp == endpointIp || metadata.destinationIp == endpointIp)
        {
            return uavId;
        }
    }
    return 0;
}

class TokenBucket
{
  public:
    TokenBucket() = default;

    TokenBucket(long double rateBps, uint64_t burstBytes)
    {
        Configure(rateBps, burstBytes);
    }

    void Configure(long double rateBps, uint64_t burstBytes)
    {
        m_rateBps = rateBps;
        m_burstBytes = static_cast<long double>(burstBytes);
        m_tokensBytes = m_burstBytes;
        m_lastRefillNs = 0;
    }

    bool Available(uint64_t bytes, uint64_t nowNs)
    {
        Refill(nowNs);
        return static_cast<long double>(bytes) <= m_tokensBytes;
    }

    void Consume(uint64_t bytes)
    {
        NS_ASSERT(static_cast<long double>(bytes) <= m_tokensBytes);
        m_tokensBytes -= static_cast<long double>(bytes);
    }

    static bool DeterministicSelfTest()
    {
        // 8 kbps is exactly 1000 bytes/s.  Equality at the bucket edge must
        // admit, while one byte beyond the available balance must reject.
        TokenBucket bucket(8000.0L, 100);
        if (!bucket.Available(80, 1000000))
        {
            return false;
        }
        bucket.Consume(80);
        if (bucket.Available(21, 1000000))
        {
            return false;
        }
        if (!bucket.Available(21, 2000000))
        {
            return false;
        }
        bucket.Consume(21);
        return !bucket.Available(100, 2000000);
    }

  private:
    void Refill(uint64_t nowNs)
    {
        if (m_lastRefillNs == 0)
        {
            m_lastRefillNs = nowNs;
            return;
        }
        if (nowNs <= m_lastRefillNs)
        {
            return;
        }
        const long double elapsedNs = static_cast<long double>(nowNs - m_lastRefillNs);
        const long double addedBytes = elapsedNs * m_rateBps / 8000000000.0L;
        m_tokensBytes = std::min(m_burstBytes, m_tokensBytes + addedBytes);
        m_lastRefillNs = nowNs;
    }

    long double m_rateBps = 0.0L;
    long double m_burstBytes = 0.0L;
    long double m_tokensBytes = 0.0L;
    uint64_t m_lastRefillNs = 0;
};

struct IngressAdmissionDecision
{
    bool admitted = true;
    std::string reason;
    uint64_t ageNs = 0;
    uint32_t uavId = 0;
};

class IngressProtectionController
{
  public:
    IngressProtectionController(bool enabled,
                                uint32_t uavCount,
                                uint64_t minimumControlHeadroomBps,
                                uint64_t payloadRateBps,
                                uint64_t additionalDataRateBps,
                                uint32_t burstBytesPerUav)
        : m_enabled(enabled),
          m_uavCount(uavCount),
          m_minimumControlHeadroomBps(minimumControlHeadroomBps)
    {
        const std::array<uint64_t, 2> rates = {payloadRateBps, additionalDataRateBps};
        const uint64_t aggregateBurst =
            static_cast<uint64_t>(burstBytesPerUav) * static_cast<uint64_t>(uavCount);
        for (uint32_t lowerIndex = 0; lowerIndex < rates.size(); ++lowerIndex)
        {
            m_aggregate[lowerIndex].Configure(static_cast<long double>(rates[lowerIndex]),
                                              aggregateBurst);
            m_perUav[lowerIndex].resize(uavCount + 1);
            const long double childRate = static_cast<long double>(rates[lowerIndex]) /
                                          static_cast<long double>(uavCount);
            for (auto& child : m_perUav[lowerIndex])
            {
                child.Configure(childRate, burstBytesPerUav);
            }
        }
    }

    IngressAdmissionDecision Admit(const std::string& observedDevice,
                                    const FrameMetadata& metadata,
                                    uint64_t wireBytes,
                                    uint64_t deadlineNs,
                                    uint64_t nowNs)
    {
        IngressAdmissionDecision decision;
        const uint32_t classIndex = ClassIndex(metadata);
        decision.uavId = ResolveFrameUavId(metadata, observedDevice, m_uavCount);
        decision.ageNs = ElapsedNs(nowNs, FrameAgeStartNs(metadata, nowNs));
        if (deadlineNs > 0 && decision.ageNs >= deadlineNs)
        {
            decision.admitted = false;
            decision.reason = "ingress_deadline_" + metadata.trafficClass;
            return decision;
        }
        if (classIndex == ControlClassIndex())
        {
            return decision;
        }
        if (classIndex < 1 || classIndex > 2)
        {
            return decision;
        }
        if (!m_enabled)
        {
            return decision;
        }

        const uint32_t lowerIndex = classIndex - 1;
        TokenBucket& aggregate = m_aggregate[lowerIndex];
        TokenBucket& child = m_perUav[lowerIndex][decision.uavId];
        if (!aggregate.Available(wireBytes, nowNs) || !child.Available(wireBytes, nowNs))
        {
            decision.admitted = false;
            decision.reason = "ingress_token_bucket_" + metadata.trafficClass;
            return decision;
        }
        aggregate.Consume(wireBytes);
        child.Consume(wireBytes);
        return decision;
    }

    static bool CapacityKeepsMinimumControlHeadroom(uint64_t radioRateBps,
                                                     uint64_t minimumControlHeadroomBps,
                                        uint64_t payloadRateBps,
                                        uint64_t additionalDataRateBps)
    {
        if (payloadRateBps > radioRateBps ||
            additionalDataRateBps > radioRateBps - payloadRateBps)
        {
            return false;
        }
        const uint64_t lowerRateBps = payloadRateBps + additionalDataRateBps;
        return minimumControlHeadroomBps <= radioRateBps - lowerRateBps;
    }

    static bool TokenBucketSelfTest()
    {
        return TokenBucket::DeterministicSelfTest();
    }

    static uint64_t PerUavSustainedAdmissionRateBps(uint64_t aggregateRateBps,
                                                     uint32_t uavCount)
    {
        return uavCount == 0 ? 0 : aggregateRateBps / uavCount;
    }

    static bool AsymmetricPayloadDemandSelfTest()
    {
        // Only uav1 demands payload. The per-UAV bucket remains the limiting
        // rate even though the aggregate bucket retains capacity for uav2..5.
        constexpr uint64_t payloadRateBps = 6500000;
        constexpr uint32_t uavCount = 5;
        const uint64_t perUavBytesPerSecond =
            PerUavSustainedAdmissionRateBps(payloadRateBps, uavCount) / 8;
        IngressProtectionController controller(true,
                                               uavCount,
                                               4000000,
                                               payloadRateBps,
                                               1,
                                               1000000);
        FrameMetadata payload;
        payload.classMapped = true;
        payload.trafficClass = "payload";
        if (!controller.Admit("uav1", payload, 1000000, 0, 1).admitted)
        {
            return false;
        }
        const IngressAdmissionDecision abovePerUavRate = controller.Admit(
            "uav1", payload, perUavBytesPerSecond + 1, 0, 1000000001ULL);
        const IngressAdmissionDecision atPerUavRate = controller.Admit(
            "uav1", payload, perUavBytesPerSecond, 0, 1000000001ULL);
        return !abovePerUavRate.admitted && atPerUavRate.admitted &&
               perUavBytesPerSecond * 8 < payloadRateBps;
    }

    static bool DeadlineDropSelfTest()
    {
        IngressProtectionController controller(true, 1, 4000, 8000, 8000, 100);
        FrameMetadata metadata;
        metadata.classMapped = true;
        metadata.trafficClass = "payload";
        metadata.sourceMonotonicValid = true;
        metadata.sourceMonotonicNs = 1000;
        const IngressAdmissionDecision atDeadline =
            controller.Admit("uav1", metadata, 1, 1000, 2000);
        return !atDeadline.admitted && atDeadline.reason == "ingress_deadline_payload";
    }

    static bool MinimumControlHeadroomSelfTest()
    {
        if (!CapacityKeepsMinimumControlHeadroom(100, 20, 30, 30) ||
            CapacityKeepsMinimumControlHeadroom(100, 50, 30, 30))
        {
            return false;
        }
        IngressProtectionController controller(true, 1, 4000, 8, 8, 1);
        FrameMetadata control;
        control.classMapped = true;
        control.trafficClass = "control";
        return controller.Admit("gcs", control, 1000000, 1000, 1).admitted;
    }

    static bool ProfileIdCannotBypassSelfTest()
    {
        IngressProtectionController controller(true, 1, 1, 8, 8, 1);
        FrameMetadata payload;
        payload.classMapped = true;
        payload.trafficClass = "payload";
        payload.applicationProfileId = 4;
        const IngressAdmissionDecision first =
            controller.Admit("uav1", payload, 1, 0, 1000);
        const IngressAdmissionDecision second =
            controller.Admit("uav1", payload, 1, 0, 1000);
        return first.admitted && !second.admitted &&
               second.reason == "ingress_token_bucket_payload";
    }

    uint64_t MinimumControlHeadroomBps() const
    {
        return m_minimumControlHeadroomBps;
    }

  private:
    // Avoid depending on the scheduler declaration order while retaining the
    // canonical class index used throughout this translation unit.
    static constexpr uint32_t ControlClassIndex()
    {
        return 0;
    }

    bool m_enabled;
    uint32_t m_uavCount;
    uint64_t m_minimumControlHeadroomBps;
    std::array<TokenBucket, 2> m_aggregate;
    std::array<std::vector<TokenBucket>, 2> m_perUav;
};

class PerUavRoundRobin
{
  public:
    void SetUavCount(uint32_t uavCount)
    {
        m_uavCount = uavCount;
        m_nextUavId = 1;
    }

    uint32_t Select(const std::set<uint32_t>& available) const
    {
        if (available.empty())
        {
            return 0;
        }
        for (uint32_t offset = 0; offset < m_uavCount; ++offset)
        {
            const uint32_t candidate = ((m_nextUavId - 1 + offset) % m_uavCount) + 1;
            if (available.count(candidate) > 0)
            {
                return candidate;
            }
        }
        return available.count(0) > 0 ? 0 : *available.begin();
    }

    void Record(uint32_t selectedUavId)
    {
        if (selectedUavId >= 1 && selectedUavId <= m_uavCount)
        {
            m_nextUavId = selectedUavId == m_uavCount ? 1 : selectedUavId + 1;
        }
    }

    static bool DeterministicSelfTest()
    {
        PerUavRoundRobin scheduler;
        scheduler.SetUavCount(MAX_UAVS);
        const std::set<uint32_t> all = {1, 2, 3, 4, 5};
        std::vector<uint32_t> selected;
        for (uint32_t count = 0; count < 10; ++count)
        {
            const uint32_t uavId = scheduler.Select(all);
            selected.push_back(uavId);
            scheduler.Record(uavId);
        }
        return selected == std::vector<uint32_t>({1, 2, 3, 4, 5, 1, 2, 3, 4, 5});
    }

  private:
    uint32_t m_uavCount = 1;
    uint32_t m_nextUavId = 1;
};

struct BackoffGuardDecision
{
    bool requestAbort = false;
    uint32_t retryCount = 0;
    std::string reason;
};

class LowerPacketGuard
{
  public:
    explicit LowerPacketGuard(uint32_t retryLimit)
        : m_retryLimit(retryLimit)
    {
    }

    void Register(Ptr<const Packet> packet,
                  const FrameMetadata& metadata,
                  uint64_t deadlineNs,
                  uint64_t nowNs)
    {
        const uint32_t classIndex = ClassIndex(metadata);
        if (classIndex != 1 && classIndex != 2)
        {
            return;
        }
        PacketState state;
        state.trafficClass = metadata.trafficClass;
        state.startNs = FrameAgeStartNs(metadata, nowNs);
        state.deadlineNs = deadlineNs;
        m_packets[PeekPointer(packet)] = std::move(state);
    }

    BackoffGuardDecision ObserveBackoff(Ptr<const Packet> packet, uint64_t nowNs)
    {
        const auto found = m_packets.find(PeekPointer(packet));
        if (found == m_packets.end())
        {
            return {};
        }
        return Observe(found->second, nowNs);
    }

    std::optional<std::string> PendingDropReason(Ptr<const Packet> packet) const
    {
        const auto found = m_packets.find(PeekPointer(packet));
        if (found == m_packets.end() || found->second.pendingReason.empty())
        {
            return std::nullopt;
        }
        return found->second.pendingReason;
    }

    void Forget(Ptr<const Packet> packet)
    {
        m_packets.erase(PeekPointer(packet));
    }

    static bool DeterministicSelfTest()
    {
        LowerPacketGuard retryGuard(2);
        PacketState retryState;
        retryState.trafficClass = "payload";
        retryState.startNs = 100;
        retryState.deadlineNs = 1000;
        if (retryGuard.Observe(retryState, 100).requestAbort)
        {
            return false;
        }
        const BackoffGuardDecision retryLimit = retryGuard.Observe(retryState, 101);
        if (!retryLimit.requestAbort || retryLimit.retryCount != 2 ||
            retryLimit.reason != "retry_limit_payload")
        {
            return false;
        }

        LowerPacketGuard deadlineGuard(4);
        PacketState deadlineState;
        deadlineState.trafficClass = "additional_data";
        deadlineState.startNs = 100;
        deadlineState.deadlineNs = 10;
        const BackoffGuardDecision deadline = deadlineGuard.Observe(deadlineState, 110);
        return deadline.requestAbort &&
               deadline.reason == "deadline_drop_backoff_additional_data";
    }

  private:
    struct PacketState
    {
        std::string trafficClass;
        uint64_t startNs = 0;
        uint64_t deadlineNs = 0;
        uint32_t retryCount = 0;
        std::string pendingReason;
    };

    BackoffGuardDecision Observe(PacketState& state, uint64_t nowNs)
    {
        ++state.retryCount;
        if (state.pendingReason.empty() && state.deadlineNs > 0 &&
            ElapsedNs(nowNs, state.startNs) >= state.deadlineNs)
        {
            state.pendingReason = "deadline_drop_backoff_" + state.trafficClass;
        }
        if (state.pendingReason.empty() && state.retryCount >= m_retryLimit)
        {
            state.pendingReason = "retry_limit_" + state.trafficClass;
        }
        return {!state.pendingReason.empty(), state.retryCount, state.pendingReason};
    }

    uint32_t m_retryLimit;
    std::map<const Packet*, PacketState> m_packets;
};

class StrictPriorityScheduler
{
  public:
    static constexpr uint32_t CONTROL_CLASS = 0;
    static constexpr uint32_t PAYLOAD_CLASS = 1;
    static constexpr uint32_t ADDITIONAL_DATA_CLASS = 2;
    static constexpr uint32_t NO_CLASS = 3;

    uint32_t Select(const std::array<uint32_t, 3>& counts) const
    {
        if (counts[CONTROL_CLASS] > 0)
        {
            return CONTROL_CLASS;
        }
        const bool lowerWaiting = counts[PAYLOAD_CLASS] > 0 ||
                                  counts[ADDITIONAL_DATA_CLASS] > 0;
        if (lowerWaiting)
        {
            if (counts[m_nextLowerClass] > 0)
            {
                return m_nextLowerClass;
            }
            return m_nextLowerClass == PAYLOAD_CLASS ? ADDITIONAL_DATA_CLASS : PAYLOAD_CLASS;
        }
        return NO_CLASS;
    }

    void Record(uint32_t selectedClass, const std::array<uint32_t, 3>&)
    {
        if (selectedClass == CONTROL_CLASS)
        {
            return;
        }
        if (selectedClass == PAYLOAD_CLASS || selectedClass == ADDITIONAL_DATA_CLASS)
        {
            m_nextLowerClass = selectedClass == PAYLOAD_CLASS ? ADDITIONAL_DATA_CLASS
                                                               : PAYLOAD_CLASS;
        }
    }

    void Reset()
    {
        m_nextLowerClass = PAYLOAD_CLASS;
    }

    static bool DeterministicSelfTest();

  private:
    uint32_t m_nextLowerClass = PAYLOAD_CLASS;
};

bool
StrictPriorityScheduler::DeterministicSelfTest()
{
    const auto drain = [](std::array<uint32_t, 3> counts) {
        StrictPriorityScheduler scheduler;
        std::vector<uint32_t> selections;
        while (true)
        {
            const uint32_t selected = scheduler.Select(counts);
            if (selected == NO_CLASS)
            {
                break;
            }
            if (selected >= counts.size() || counts[selected] == 0)
            {
                return std::vector<uint32_t>{NO_CLASS};
            }
            --counts[selected];
            scheduler.Record(selected, counts);
            selections.push_back(selected);
        }
        return selections;
    };

    if (drain({5, 2, 2}) != std::vector<uint32_t>({0, 0, 0, 0, 0, 1, 2, 1, 2}) ||
        drain({0, 2, 2}) != std::vector<uint32_t>({1, 2, 1, 2}) ||
        drain({2, 0, 2}) != std::vector<uint32_t>({0, 0, 2, 2}) ||
        drain({1, 0, 0}) != std::vector<uint32_t>({0}))
    {
        return false;
    }
    return true;
}

struct GlobalQueueCandidate
{
    uint64_t entryId = 0;
    uint64_t packetUid = 0;
    uint32_t classIndex = StrictPriorityScheduler::NO_CLASS;
    uint32_t uavId = 0;
    uint64_t enqueueSimNs = 0;
    uint32_t ownerIndex = 0;
};

class ExactPacketGrant
{
  public:
    bool Issue(uint64_t entryId, uint64_t generation)
    {
        if (generation == 0 || generation <= m_lastGeneration || m_pending)
        {
            return false;
        }
        m_entryId = entryId;
        m_generation = generation;
        m_pending = true;
        return true;
    }

    std::optional<std::pair<uint64_t, uint64_t>> Consume()
    {
        if (!m_pending)
        {
            return std::nullopt;
        }
        const std::pair<uint64_t, uint64_t> value{m_entryId, m_generation};
        m_lastGeneration = m_generation;
        m_entryId = 0;
        m_generation = 0;
        m_pending = false;
        return value;
    }

    bool Cancel(uint64_t generation)
    {
        if (!m_pending || generation != m_generation)
        {
            return false;
        }
        m_lastGeneration = m_generation;
        m_entryId = 0;
        m_generation = 0;
        m_pending = false;
        return true;
    }

    std::optional<uint64_t> PendingUid() const
    {
        return m_pending ? std::optional<uint64_t>(m_entryId) : std::nullopt;
    }

    void InvalidateEntry(uint64_t entryId)
    {
        if (m_pending && m_entryId == entryId)
        {
            m_lastGeneration = m_generation;
            m_entryId = 0;
            m_generation = 0;
            m_pending = false;
        }
    }

    static bool DeterministicSelfTest()
    {
        ExactPacketGrant grant;
        if (!grant.Issue(0, 1) || grant.Issue(12, 2) || grant.Issue(13, 0))
        {
            return false;
        }
        const auto first = grant.Consume();
        if (!first || first->first != 0 || first->second != 1 || grant.Consume() ||
            grant.Issue(12, 1) || !grant.Issue(12, 2) || !grant.Cancel(2) ||
            grant.Issue(13, 2) || !grant.Issue(13, 3))
        {
            return false;
        }
        grant.InvalidateEntry(13);
        return !grant.PendingUid() && !grant.Issue(14, 3) && grant.Issue(14, 4);
    }

  private:
    uint64_t m_entryId = 0;
    uint64_t m_generation = 0;
    uint64_t m_lastGeneration = 0;
    bool m_pending = false;
};

class AmsThreeClassQueue : public Queue<Packet>
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("ns3::AmsThreeClassQueue")
                                .SetParent<Queue<Packet>>()
                                .SetGroupName("Network")
                                .AddConstructor<AmsThreeClassQueue>();
        return tid;
    }

    AmsThreeClassQueue()
    {
        SetLimits(256, 128, 128);
    }

    void SetLimits(uint32_t control, uint32_t payload, uint32_t additionalData)
    {
        SetQos(control,
               payload,
               additionalData,
               250,
               1000,
               2000,
               200,
               750,
               1500,
               1);
    }

    void SetQos(uint32_t control,
                uint32_t payload,
                uint32_t additionalData,
                uint32_t controlDeadlineMs,
                uint32_t payloadDeadlineMs,
                uint32_t additionalDeadlineMs,
                uint32_t controlMaxAgeMs,
                uint32_t payloadMaxAgeMs,
                uint32_t additionalMaxAgeMs,
                uint32_t uavCount)
    {
        m_limits = {control, payload, additionalData};
        m_deadlinesNs = {static_cast<uint64_t>(controlDeadlineMs) * 1000000ULL,
                         static_cast<uint64_t>(payloadDeadlineMs) * 1000000ULL,
                         static_cast<uint64_t>(additionalDeadlineMs) * 1000000ULL};
        m_maxAgesNs = {static_cast<uint64_t>(controlMaxAgeMs) * 1000000ULL,
                       static_cast<uint64_t>(payloadMaxAgeMs) * 1000000ULL,
                       static_cast<uint64_t>(additionalMaxAgeMs) * 1000000ULL};
        m_scheduler.Reset();
        for (auto& scheduler : m_perUavSchedulers)
        {
            scheduler.SetUavCount(uavCount);
        }
        m_uavCount = uavCount;
        SetMaxSize(QueueSize(std::to_string(control + payload + additionalData) + "p"));
    }

    void SetIdentity(std::string deviceId, QueueEventSink sink)
    {
        m_deviceId = std::move(deviceId);
        m_sink = std::move(sink);
    }

    void SetRadioDecisionSink(RadioDecisionSink sink)
    {
        m_radioDecisionSink = std::move(sink);
    }

    void SetIngressProtection(IngressProtectionController* controller)
    {
        m_ingressProtection = controller;
    }

    void SetPacketGuard(LowerPacketGuard* guard)
    {
        m_packetGuard = guard;
    }

    void SetRealtimeDeadlineClock(bool enabled)
    {
        m_realtimeDeadlineClock = enabled;
    }

    void SetRadioTransmitSink(RadioTransmitSink sink, Ptr<CsmaNetDevice> device)
    {
        m_radioTransmitSink = std::move(sink);
        m_radioDevice = std::move(device);
    }

    void SetRadioDevice(Ptr<CsmaNetDevice> device)
    {
        m_radioDevice = std::move(device);
    }

    void EnableGlobalScheduling()
    {
        NS_ASSERT_MSG(GetContainer().empty(),
                      "global scheduling must be enabled before queue ingress");
        m_globalScheduling = true;
    }

    std::vector<GlobalQueueCandidate> CandidateSnapshot() const
    {
        std::vector<GlobalQueueCandidate> candidates;
        candidates.reserve(GetContainer().size());
        for (auto position = GetContainer().begin(); position != GetContainer().end(); ++position)
        {
            const Ptr<const Packet> packet = *position;
            const CachedFrame cached = Cached(packet);
            const uint32_t classIndex = ClassIndex(cached.metadata);
            if (classIndex < 3)
            {
                candidates.push_back(
                    {cached.entryId,
                     packet->GetUid(),
                     classIndex,
                     cached.uavId,
                     cached.enqueueSimNs,
                     0});
            }
        }
        return candidates;
    }

    bool GrantExactPacket(uint64_t entryId, uint64_t generation)
    {
        if (!m_globalScheduling || m_entryPackets.count(entryId) == 0)
        {
            return false;
        }
        return m_exactGrant.Issue(entryId, generation);
    }

    bool CancelExactGrant(uint64_t generation)
    {
        return m_exactGrant.Cancel(generation);
    }

    bool Enqueue(Ptr<Packet> packet) override
    {
        // Admission only needs bounded headers.  Payload SHA-256 is computed
        // later by the event logger, and never while scanning the queue.
        const FrameMetadata metadata = InspectFrame(packet, false);
        const uint32_t classIndex = ClassIndex(metadata);
        if (classIndex >= m_limits.size())
        {
            DropBeforeEnqueue(packet);
            Emit("drop", packet, 0, 0, "unmapped_dscp_or_ether_type");
            return false;
        }
        if (metadata.transportProtocol == 17 &&
            !IsAllowedEndpointUdpPort(metadata.destinationUdpPort))
        {
            DropBeforeEnqueue(packet);
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 "udp_destination_port_not_in_endpoint_matrix");
            return false;
        }

        const uint64_t nowNs = SteadyNowNs();
        const uint64_t ageLimitNs = AgeLimitNs(classIndex);
        IngressAdmissionDecision admission;
        if (m_ingressProtection)
        {
            admission = m_ingressProtection->Admit(
                m_deviceId, metadata, packet->GetSize(), ageLimitNs, nowNs);
        }
        else
        {
            admission.uavId = ResolveFrameUavId(metadata, m_deviceId, m_uavCount);
            admission.ageNs = ElapsedNs(nowNs, FrameAgeStartNs(metadata, nowNs));
            if (ageLimitNs > 0 && admission.ageNs >= ageLimitNs)
            {
                admission.admitted = false;
                admission.reason = "ingress_deadline_" + metadata.trafficClass;
            }
        }
        if (!admission.admitted)
        {
            DropBeforeEnqueue(packet);
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 admission.reason,
                 admission.ageNs);
            return false;
        }

        // This is the exact policy-admission point.  It deliberately precedes
        // Sionna state lookup and queue/medium outcomes.
        Emit("admit",
             packet,
             m_counts[classIndex],
             m_limits[classIndex],
             "",
             admission.ageNs);
        if (m_counts[classIndex] >= m_limits[classIndex])
        {
            DropBeforeEnqueue(packet);
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 "queue_limit_" + metadata.trafficClass);
            return false;
        }
        if (m_radioDecisionSink)
        {
            const RadioDecision decision = m_radioDecisionSink(m_deviceId, packet);
            if (decision.drop)
            {
                DropBeforeEnqueue(packet);
                Emit("drop",
                     packet,
                     m_counts[classIndex],
                     m_limits[classIndex],
                     decision.dropReason);
                return false;
            }
        }

        if (!DoEnqueue(GetContainer().end(), packet))
        {
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 "aggregate_queue_limit");
            return false;
        }
        ++m_counts[classIndex];
        CachedFrame cached;
        cached.entryId = ++m_nextQueueEntryId;
        if (cached.entryId == 0)
        {
            throw std::runtime_error("queue entry identity exhausted");
        }
        cached.metadata = metadata;
        cached.ageStartNs = FrameAgeStartNs(metadata, nowNs);
        cached.enqueueSimNs = Simulator::Now().GetNanoSeconds();
        cached.uavId = admission.uavId;
        const Packet* packetIdentity = PeekPointer(packet);
        m_entryPackets[cached.entryId] = packetIdentity;
        m_cachedFrames[packetIdentity] = std::move(cached);
        if (m_packetGuard)
        {
            m_packetGuard->Register(packet, metadata, ageLimitNs, nowNs);
        }
        Emit("enqueue", packet, m_counts[classIndex], m_limits[classIndex], "");
        return true;
    }

    Ptr<Packet> Dequeue() override
    {
        if (m_globalScheduling)
        {
            const auto grant = m_exactGrant.Consume();
            if (!grant)
            {
                return nullptr;
            }
            const auto selected = FindEntry(grant->first);
            if (selected == GetContainer().end())
            {
                // A stale grant is fail-closed.  Never substitute the local
                // class head; the global scheduler must take a fresh snapshot.
                return nullptr;
            }
            const DequeueResult result = DequeueAt(selected, false);
            return result.transmit ? result.packet : nullptr;
        }

        while (!GetContainer().empty())
        {
            const auto selected = SelectNextIterator();
            NS_ASSERT(selected != GetContainer().end());
            const DequeueResult result = DequeueAt(selected, true);
            if (result.transmit || !result.packet)
            {
                return result.packet;
            }
            if (GetContainer().empty() && m_radioDevice)
            {
                // Legacy callers assert that IsEmpty()==false implies a packet
                // return. Product queues use exact global grants and never take
                // this compatibility sentinel path.
                m_radioDevice->SetSendEnable(false);
                Simulator::ScheduleNow(&AmsThreeClassQueue::EnsureSendEnabled, m_radioDevice);
                return result.packet;
            }
        }
        return nullptr;
    }

    Ptr<Packet> Remove() override
    {
        if (GetContainer().empty())
        {
            return nullptr;
        }
        const auto pendingEntry = m_exactGrant.PendingUid();
        const auto selected = m_globalScheduling && pendingEntry ? FindEntry(*pendingEntry)
                                                                 : SelectNextIterator();
        if (selected == GetContainer().end())
        {
            return nullptr;
        }
        NS_ASSERT(selected != GetContainer().end());
        Ptr<const Packet> candidate = *selected;
        const CachedFrame cached = Cached(candidate);
        const uint32_t classIndex = ClassIndex(cached.metadata);
        const uint64_t queueAgeNs = QueueAgeNs(candidate);
        Ptr<Packet> packet = DoRemove(selected);
        if (packet && classIndex < m_counts.size())
        {
            NS_ASSERT(m_counts[classIndex] > 0);
            --m_counts[classIndex];
            if (!m_globalScheduling)
            {
                m_scheduler.Record(classIndex, m_counts);
                RecordUavSelection(classIndex, cached.uavId);
            }
            m_cachedFrames.erase(PeekPointer(packet));
            m_entryPackets.erase(cached.entryId);
            m_exactGrant.InvalidateEntry(cached.entryId);
            ForgetGuard(packet);
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 "queue_flush",
                 queueAgeNs);
        }
        return packet;
    }

    Ptr<const Packet> Peek() const override
    {
        if (m_globalScheduling)
        {
            const auto entryId = m_exactGrant.PendingUid();
            if (!entryId)
            {
                return nullptr;
            }
            const auto granted = FindEntry(*entryId);
            return granted == GetContainer().end() ? nullptr : DoPeek(granted);
        }
        const auto selected = SelectNextIterator();
        return selected == GetContainer().end() ? nullptr : DoPeek(selected);
    }

  private:
    struct CachedFrame
    {
        uint64_t entryId = 0;
        FrameMetadata metadata;
        uint64_t ageStartNs = 0;
        uint64_t enqueueSimNs = 0;
        uint32_t uavId = 0;
    };

    struct DequeueResult
    {
        Ptr<Packet> packet;
        bool transmit = false;
    };

    ConstIterator FindEntry(uint64_t entryId) const
    {
        const auto known = m_entryPackets.find(entryId);
        if (known == m_entryPackets.end())
        {
            return GetContainer().end();
        }
        const Packet* packetIdentity = known->second;
        return std::find_if(GetContainer().begin(),
                            GetContainer().end(),
                            [packetIdentity](Ptr<const Packet> packet) {
                                return PeekPointer(packet) == packetIdentity;
                            });
    }

    DequeueResult DequeueAt(ConstIterator selected, bool recordLocalSelection)
    {
        Ptr<const Packet> candidate = *selected;
        const CachedFrame cached = Cached(candidate);
        const FrameMetadata metadata = cached.metadata;
        const uint32_t classIndex = ClassIndex(metadata);
        const uint64_t queueAgeNs = QueueAgeNs(candidate);
        const uint64_t ageLimitNs = AgeLimitNs(classIndex);
        const bool deadlineExpired = ageLimitNs > 0 && queueAgeNs >= ageLimitNs;
        RadioDecision transmitDecision;
        if (!deadlineExpired && m_radioTransmitSink)
        {
            // The exact owner revalidates its causal Sionna decision only after
            // the global scheduler grants this packet and before native events.
            transmitDecision = m_radioTransmitSink(m_deviceId, candidate, m_radioDevice);
        }

        Ptr<Packet> packet = DoDequeue(selected);
        if (!packet || classIndex >= m_counts.size())
        {
            return {packet, false};
        }
        NS_ASSERT(m_counts[classIndex] > 0);
        --m_counts[classIndex];
        if (recordLocalSelection)
        {
            m_scheduler.Record(classIndex, m_counts);
            RecordUavSelection(classIndex, cached.uavId);
        }
        m_cachedFrames.erase(PeekPointer(packet));
        m_entryPackets.erase(cached.entryId);
        m_exactGrant.InvalidateEntry(cached.entryId);

        if (deadlineExpired || transmitDecision.drop)
        {
            ForgetGuard(packet);
            DropAfterDequeue(packet);
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 deadlineExpired ? "deadline_drop_" + metadata.trafficClass
                                 : transmitDecision.dropReason,
                 queueAgeNs);
            return {packet, false};
        }

        Emit("dequeue", packet, m_counts[classIndex], m_limits[classIndex], "", queueAgeNs);
        return {packet, true};
    }

    ConstIterator SelectNextIterator() const
    {
        const uint32_t selectedClass = m_scheduler.Select(m_counts);
        if (selectedClass == StrictPriorityScheduler::NO_CLASS)
        {
            return GetContainer().end();
        }
        uint32_t selectedUavId = 0;
        if (selectedClass == StrictPriorityScheduler::PAYLOAD_CLASS ||
            selectedClass == StrictPriorityScheduler::ADDITIONAL_DATA_CLASS)
        {
            std::set<uint32_t> available;
            for (auto position = GetContainer().begin(); position != GetContainer().end();
                 ++position)
            {
                const CachedFrame cached = Cached(*position);
                if (ClassIndex(cached.metadata) == selectedClass)
                {
                    available.insert(cached.uavId);
                }
            }
            selectedUavId = m_perUavSchedulers[selectedClass - 1].Select(available);
        }
        for (auto position = GetContainer().begin(); position != GetContainer().end(); ++position)
        {
            const CachedFrame cached = Cached(*position);
            if (ClassIndex(cached.metadata) == selectedClass &&
                (selectedClass == StrictPriorityScheduler::CONTROL_CLASS ||
                 cached.uavId == selectedUavId))
            {
                return position;
            }
        }
        NS_ASSERT_MSG(false, "scheduler selected an empty traffic class");
        return GetContainer().end();
    }

    CachedFrame Cached(Ptr<const Packet> packet) const
    {
        const auto found = m_cachedFrames.find(PeekPointer(packet));
        if (found != m_cachedFrames.end())
        {
            return found->second;
        }
        CachedFrame fallback;
        fallback.metadata = InspectFrame(packet, false);
        const uint64_t nowNs = SteadyNowNs();
        fallback.ageStartNs = FrameAgeStartNs(fallback.metadata, nowNs);
        fallback.enqueueSimNs = Simulator::Now().GetNanoSeconds();
        fallback.uavId = ResolveFrameUavId(fallback.metadata, m_deviceId, m_uavCount);
        return fallback;
    }

    uint64_t AgeLimitNs(uint32_t classIndex) const
    {
        return classIndex < m_deadlinesNs.size()
                   ? std::min(m_deadlinesNs[classIndex], m_maxAgesNs[classIndex])
                   : 0;
    }

    void RecordUavSelection(uint32_t classIndex, uint32_t uavId)
    {
        if (classIndex == StrictPriorityScheduler::PAYLOAD_CLASS ||
            classIndex == StrictPriorityScheduler::ADDITIONAL_DATA_CLASS)
        {
            m_perUavSchedulers[classIndex - 1].Record(uavId);
        }
    }

    void ForgetGuard(Ptr<const Packet> packet)
    {
        if (m_packetGuard)
        {
            m_packetGuard->Forget(packet);
        }
    }

    static void EnsureSendEnabled(Ptr<CsmaNetDevice> device)
    {
        if (device && !device->IsSendEnabled())
        {
            device->SetSendEnable(true);
        }
    }

    uint64_t QueueAgeNs(Ptr<const Packet> packet) const
    {
        const auto found = m_cachedFrames.find(PeekPointer(packet));
        if (found == m_cachedFrames.end())
        {
            return 0;
        }
        if (!m_realtimeDeadlineClock)
        {
            const uint64_t nowSimNs = Simulator::Now().GetNanoSeconds();
            return ElapsedNs(nowSimNs, found->second.enqueueSimNs);
        }
        return ElapsedNs(SteadyNowNs(), found->second.ageStartNs);
    }

    void Emit(const std::string& event,
              Ptr<const Packet> packet,
              int64_t depth,
              int64_t limit,
              const std::string& reason,
              uint64_t queueAgeNs = 0)
    {
        if (m_sink)
        {
            m_sink(event, m_deviceId, packet, depth, limit, queueAgeNs, reason);
        }
    }

    using Queue<Packet>::DoDequeue;
    using Queue<Packet>::DoEnqueue;
    using Queue<Packet>::DoPeek;
    using Queue<Packet>::DoRemove;
    using Queue<Packet>::DropAfterDequeue;
    using Queue<Packet>::DropBeforeEnqueue;
    using Queue<Packet>::GetContainer;

    std::array<uint32_t, 3> m_limits{};
    std::array<uint32_t, 3> m_counts{};
    std::array<uint64_t, 3> m_deadlinesNs{};
    std::array<uint64_t, 3> m_maxAgesNs{};
    std::map<const Packet*, CachedFrame> m_cachedFrames;
    std::map<uint64_t, const Packet*> m_entryPackets;
    uint64_t m_nextQueueEntryId = 0;
    ExactPacketGrant m_exactGrant;
    StrictPriorityScheduler m_scheduler;
    std::array<PerUavRoundRobin, 2> m_perUavSchedulers;
    uint32_t m_uavCount = 1;
    std::string m_deviceId;
    QueueEventSink m_sink;
    IngressProtectionController* m_ingressProtection = nullptr;
    LowerPacketGuard* m_packetGuard = nullptr;
    bool m_realtimeDeadlineClock = true;
    bool m_globalScheduling = false;
    RadioDecisionSink m_radioDecisionSink;
    RadioTransmitSink m_radioTransmitSink;
    Ptr<CsmaNetDevice> m_radioDevice;
};

class GlobalRadioScheduler
{
  public:
    GlobalRadioScheduler(Ptr<CsmaChannel> channel, uint32_t uavCount)
        : m_channel(std::move(channel))
    {
        for (auto& scheduler : m_perUavSchedulers)
        {
            scheduler.SetUavCount(uavCount);
        }
    }

    void InstallChannelCallback()
    {
        if (!m_channel)
        {
            throw std::runtime_error("global radio scheduler requires CsmaChannel");
        }
        m_channel->SetIdleCallback(MakeCallback(&GlobalRadioScheduler::RequestDispatch, this));
    }

    void RegisterOwner(const std::string& deviceId,
                       Ptr<CsmaNetDevice> device,
                       Ptr<AmsThreeClassQueue> queue)
    {
        if (!device || !queue || m_owners.size() >= MAX_UAVS + 1)
        {
            throw std::runtime_error("invalid global radio scheduler owner registration");
        }
        queue->EnableGlobalScheduling();
        device->SetQueueReadyCallback(
            MakeCallback(&GlobalRadioScheduler::RequestDispatch, this));
        m_owners.push_back({deviceId, std::move(device), std::move(queue)});
    }

    void RequestDispatch()
    {
        if (m_dispatchScheduled)
        {
            return;
        }
        m_dispatchScheduled = true;
        Simulator::ScheduleNow(&GlobalRadioScheduler::Dispatch, this);
    }

    static bool DeterministicSelfTest()
    {
        StrictPriorityScheduler classScheduler;
        std::array<PerUavRoundRobin, 2> perUav;
        for (auto& scheduler : perUav)
        {
            scheduler.SetUavCount(MAX_UAVS);
        }

        // A control packet in the sixth owner must block a lower packet in
        // the first owner and retain its exact owner identity.
        const std::vector<GlobalQueueCandidate> crossOwner = {
            {1, 10, StrictPriorityScheduler::PAYLOAD_CLASS, 1, 1, 0},
            {2, 11, StrictPriorityScheduler::ADDITIONAL_DATA_CLASS, 2, 2, 1},
            {3, 12, StrictPriorityScheduler::PAYLOAD_CLASS, 3, 3, 2},
            {4, 13, StrictPriorityScheduler::ADDITIONAL_DATA_CLASS, 4, 4, 3},
            {5, 14, StrictPriorityScheduler::PAYLOAD_CLASS, 5, 5, 4},
            {6, 20, StrictPriorityScheduler::CONTROL_CLASS, 0, 6, 5},
        };
        auto selected = SelectFrom(crossOwner, classScheduler, perUav);
        if (!selected || selected->packetUid != 20 || selected->ownerIndex != 5)
        {
            return false;
        }

        classScheduler.Reset();
        for (auto& scheduler : perUav)
        {
            scheduler.SetUavCount(MAX_UAVS);
        }
        const std::vector<GlobalQueueCandidate> lowerClasses = {
            {3, 31, StrictPriorityScheduler::PAYLOAD_CLASS, 1, 1, 1},
            {4, 32, StrictPriorityScheduler::ADDITIONAL_DATA_CLASS, 1, 2, 1},
        };
        selected = SelectFrom(lowerClasses, classScheduler, perUav);
        if (!selected || selected->classIndex != StrictPriorityScheduler::PAYLOAD_CLASS)
        {
            return false;
        }
        RecordSelection(*selected, classScheduler, perUav);
        selected = SelectFrom(lowerClasses, classScheduler, perUav);
        if (!selected ||
            selected->classIndex != StrictPriorityScheduler::ADDITIONAL_DATA_CLASS)
        {
            return false;
        }

        classScheduler.Reset();
        for (auto& scheduler : perUav)
        {
            scheduler.SetUavCount(MAX_UAVS);
        }
        std::vector<GlobalQueueCandidate> fiveUavs;
        for (uint32_t uavId = 1; uavId <= MAX_UAVS; ++uavId)
        {
            fiveUavs.push_back(
                {uavId,
                 100 + uavId,
                 StrictPriorityScheduler::PAYLOAD_CLASS,
                 uavId,
                 uavId,
                 uavId});
        }
        for (uint32_t expectedUav = 1; expectedUav <= MAX_UAVS; ++expectedUav)
        {
            selected = SelectFrom(fiveUavs, classScheduler, perUav);
            if (!selected || selected->uavId != expectedUav ||
                selected->ownerIndex != expectedUav)
            {
                return false;
            }
            RecordSelection(*selected, classScheduler, perUav);
            // Keep the class on payload for this independent RR proof.
            classScheduler.Reset();
        }
        return true;
    }

  private:
    struct Owner
    {
        std::string deviceId;
        Ptr<CsmaNetDevice> device;
        Ptr<AmsThreeClassQueue> queue;
    };

    static bool Older(const GlobalQueueCandidate& left, const GlobalQueueCandidate& right)
    {
        return std::tie(left.enqueueSimNs, left.packetUid, left.entryId, left.ownerIndex) <
               std::tie(right.enqueueSimNs, right.packetUid, right.entryId, right.ownerIndex);
    }

    static std::optional<GlobalQueueCandidate>
    SelectFrom(const std::vector<GlobalQueueCandidate>& candidates,
               const StrictPriorityScheduler& classScheduler,
               const std::array<PerUavRoundRobin, 2>& perUav)
    {
        std::array<uint32_t, 3> counts{};
        for (const auto& candidate : candidates)
        {
            if (candidate.classIndex < counts.size())
            {
                ++counts[candidate.classIndex];
            }
        }
        const uint32_t classIndex = classScheduler.Select(counts);
        if (classIndex == StrictPriorityScheduler::NO_CLASS)
        {
            return std::nullopt;
        }

        uint32_t uavId = 0;
        if (classIndex == StrictPriorityScheduler::PAYLOAD_CLASS ||
            classIndex == StrictPriorityScheduler::ADDITIONAL_DATA_CLASS)
        {
            std::set<uint32_t> available;
            for (const auto& candidate : candidates)
            {
                if (candidate.classIndex == classIndex)
                {
                    available.insert(candidate.uavId);
                }
            }
            uavId = perUav[classIndex - 1].Select(available);
        }

        std::optional<GlobalQueueCandidate> selected;
        for (const auto& candidate : candidates)
        {
            if (candidate.classIndex != classIndex ||
                (classIndex != StrictPriorityScheduler::CONTROL_CLASS &&
                 candidate.uavId != uavId))
            {
                continue;
            }
            if (!selected || Older(candidate, *selected))
            {
                selected = candidate;
            }
        }
        return selected;
    }

    static void RecordSelection(const GlobalQueueCandidate& selected,
                                StrictPriorityScheduler& classScheduler,
                                std::array<PerUavRoundRobin, 2>& perUav)
    {
        classScheduler.Record(selected.classIndex, {});
        if (selected.classIndex == StrictPriorityScheduler::PAYLOAD_CLASS ||
            selected.classIndex == StrictPriorityScheduler::ADDITIONAL_DATA_CLASS)
        {
            perUav[selected.classIndex - 1].Record(selected.uavId);
        }
    }

    std::vector<GlobalQueueCandidate> Snapshot() const
    {
        std::vector<GlobalQueueCandidate> candidates;
        for (uint32_t ownerIndex = 0; ownerIndex < m_owners.size(); ++ownerIndex)
        {
            std::vector<GlobalQueueCandidate> ownerCandidates =
                m_owners[ownerIndex].queue->CandidateSnapshot();
            for (auto& candidate : ownerCandidates)
            {
                candidate.ownerIndex = ownerIndex;
                candidates.push_back(candidate);
            }
        }
        return candidates;
    }

    void Dispatch()
    {
        m_dispatchScheduled = false;
        if (!m_channel || m_channel->GetState() != IDLE)
        {
            // PropagationCompleteEvent is the authoritative next wakeup.
            return;
        }

        const auto selected = SelectFrom(Snapshot(), m_classScheduler, m_perUavSchedulers);
        if (!selected || selected->ownerIndex >= m_owners.size())
        {
            return;
        }
        Owner& owner = m_owners[selected->ownerIndex];
        if (!owner.device->IsTransmitReady())
        {
            // Strict priority is fail-closed: never substitute a lower packet
            // merely because the selected control owner is still in its IFG.
            return;
        }

        const uint64_t generation = ++m_nextGrantGeneration;
        if (!owner.queue->GrantExactPacket(selected->entryId, generation))
        {
            RequestDispatch();
            return;
        }
        if (!owner.device->StartOneQueuedPacket())
        {
            owner.queue->CancelExactGrant(generation);
            RequestDispatch();
            return;
        }
        RecordSelection(*selected, m_classScheduler, m_perUavSchedulers);
    }

    Ptr<CsmaChannel> m_channel;
    std::vector<Owner> m_owners;
    StrictPriorityScheduler m_classScheduler;
    std::array<PerUavRoundRobin, 2> m_perUavSchedulers;
    uint64_t m_nextGrantGeneration = 0;
    bool m_dispatchScheduled = false;
};

struct EngineConfig
{
    uint32_t uavCount = 1;
    std::string tapGcs = "tap-gcs";
    std::string tapUavs;
    uint64_t durationMs = 3600000;
    // Product launchers must pass the capacity from the radio YAML.  This
    // deliberately unusable fallback prevents a second capacity authority.
    std::string radioRate = "1bps";
    std::string radioDelay = "2ms";
    uint32_t queueControlMaxPackets = 256;
    uint32_t queuePayloadMaxPackets = 128;
    uint32_t queueAdditionalDataMaxPackets = 128;
    uint32_t queueControlDeadlineMs = 250;
    uint32_t queuePayloadDeadlineMs = 1000;
    uint32_t queueAdditionalDataDeadlineMs = 2000;
    uint32_t queueControlMaxAgeMs = 200;
    uint32_t queuePayloadMaxAgeMs = 750;
    uint32_t queueAdditionalDataMaxAgeMs = 1500;
    // Product QoS/protection values have no executable defaults: every launch
    // must resolve them from communication_qos.yaml and pass them explicitly.
    bool strictControlPriority = false;
    bool fairLowerClassesPerUav = false;
    bool ingressProtectionEnabled = false;
    bool shapingEnabled = false;
    uint64_t minimumControlHeadroomBps = 0;
    uint64_t payloadAdmissionRateBps = 0;
    uint64_t additionalDataAdmissionRateBps = 0;
    uint32_t tokenBucketBurstBytesPerUav = 0;
    uint32_t lowerRetryLimit = 0;
    uint32_t macRetryLimit = 0;
    uint32_t eventLogFlushEvery = 0;
    uint32_t eventLogFlushMaxDelayMs = 0;
    uint32_t controlPriority = 0;
    uint32_t payloadPriority = 0;
    uint32_t additionalDataPriority = 0;
    uint32_t controlTos = 0;
    uint32_t payloadTos = 0;
    uint32_t additionalDataTos = 0;
    uint32_t seed = 42;
    uint64_t run = 1;
    uint64_t eventEpoch = 1;
    bool selfTest = false;
    uint32_t selfTestBurst = 1;
    bool selfTestUnknownTos = false;
    std::string configHash;
    bool printConfigHash = false;
    std::string eventsFile = "ams-tap-packet-events.jsonl";
    std::string pcapPrefix;
    std::string readyFile;
    std::string stopFile;
    bool sionnaIpcEnabled = false;
    std::string sionnaStateFile;
    uint32_t sionnaPollIntervalMs = 1;
    uint32_t sionnaMaxUpdatesPerPoll = 64;
    uint32_t sionnaMaxStateTtlMs = 1000;
    std::string sionnaIntervention = "natural";
    std::string clockDatagramSocket;
};

std::vector<std::string>
SplitCsv(const std::string& value)
{
    std::vector<std::string> result;
    std::stringstream stream(value);
    std::string item;
    while (std::getline(stream, item, ','))
    {
        result.push_back(item);
    }
    return result;
}

std::string
JoinCsv(const std::vector<std::string>& values)
{
    std::ostringstream out;
    for (std::size_t i = 0; i < values.size(); ++i)
    {
        if (i > 0)
        {
            out << ',';
        }
        out << values[i];
    }
    return out.str();
}

std::vector<std::string>
ResolveTapUavs(const EngineConfig& config)
{
    if (!config.tapUavs.empty())
    {
        return SplitCsv(config.tapUavs);
    }
    if (config.uavCount == 1)
    {
        return {"tap-uav"};
    }
    std::vector<std::string> names;
    for (uint32_t i = 1; i <= config.uavCount; ++i)
    {
        names.push_back("tap-uav" + std::to_string(i));
    }
    return names;
}

std::string
CanonicalConfig(const EngineConfig& config, const std::vector<std::string>& tapUavs)
{
    std::ostringstream out;
    out << "contract=" << CONTRACT << '\n'
        << "uav_count=" << config.uavCount << '\n'
        << "tap_gcs=" << config.tapGcs << '\n'
        << "tap_uavs=" << JoinCsv(tapUavs) << '\n'
        << "duration_ms=" << config.durationMs << '\n'
        << "radio_rate=" << config.radioRate << '\n'
        << "radio_delay=" << config.radioDelay << '\n'
        << "queue_control_max_packets=" << config.queueControlMaxPackets << '\n'
        << "queue_payload_max_packets=" << config.queuePayloadMaxPackets << '\n'
        << "queue_additional_data_max_packets=" << config.queueAdditionalDataMaxPackets << '\n'
        << "queue_control_deadline_ms=" << config.queueControlDeadlineMs << '\n'
        << "queue_payload_deadline_ms=" << config.queuePayloadDeadlineMs << '\n'
        << "queue_additional_data_deadline_ms=" << config.queueAdditionalDataDeadlineMs << '\n'
        << "queue_control_max_age_ms=" << config.queueControlMaxAgeMs << '\n'
        << "queue_payload_max_age_ms=" << config.queuePayloadMaxAgeMs << '\n'
        << "queue_additional_data_max_age_ms=" << config.queueAdditionalDataMaxAgeMs << '\n'
        << "strict_control_priority=" << (config.strictControlPriority ? 1 : 0) << '\n'
        << "fair_lower_classes_per_uav=" << (config.fairLowerClassesPerUav ? 1 : 0) << '\n'
        << "ingress_protection_enabled=" << (config.ingressProtectionEnabled ? 1 : 0) << '\n'
        << "shaping_enabled=" << (config.shapingEnabled ? 1 : 0) << '\n'
        << "minimum_control_headroom_bps=" << config.minimumControlHeadroomBps << '\n'
        << "payload_admission_rate_bps=" << config.payloadAdmissionRateBps << '\n'
        << "additional_data_admission_rate_bps=" << config.additionalDataAdmissionRateBps << '\n'
        << "token_bucket_burst_bytes_per_uav=" << config.tokenBucketBurstBytesPerUav << '\n'
        << "lower_retry_limit=" << config.lowerRetryLimit << '\n'
        << "mac_retry_limit=" << config.macRetryLimit << '\n'
        << "event_log_flush_every=" << config.eventLogFlushEvery << '\n'
        << "event_log_flush_max_delay_ms=" << config.eventLogFlushMaxDelayMs << '\n'
        << "control_priority=" << config.controlPriority << '\n'
        << "payload_priority=" << config.payloadPriority << '\n'
        << "additional_data_priority=" << config.additionalDataPriority << '\n'
        << "control_tos=" << config.controlTos << '\n'
        << "payload_tos=" << config.payloadTos << '\n'
        << "additional_data_tos=" << config.additionalDataTos << '\n'
        << "seed=" << config.seed << '\n'
        << "run=" << config.run << '\n'
        << "event_epoch=" << config.eventEpoch << '\n'
        << "self_test=" << (config.selfTest ? 1 : 0) << '\n'
        << "self_test_burst=" << config.selfTestBurst << '\n'
        << "self_test_unknown_tos=" << (config.selfTestUnknownTos ? 1 : 0) << '\n';
    if (config.sionnaIpcEnabled)
    {
        out << "sionna_ipc_enabled=1\n"
            << "sionna_state_file=" << config.sionnaStateFile << '\n'
            << "sionna_poll_interval_ms=" << config.sionnaPollIntervalMs << '\n'
            << "sionna_max_updates_per_poll=" << config.sionnaMaxUpdatesPerPoll << '\n'
            << "sionna_max_state_ttl_ms=" << config.sionnaMaxStateTtlMs << '\n'
            << "sionna_intervention=" << config.sionnaIntervention << '\n';
        if (!config.clockDatagramSocket.empty())
        {
            out << "clock_datagram_socket=" << config.clockDatagramSocket << '\n';
        }
    }
    return out.str();
}

bool
IsValidInterfaceName(const std::string& value)
{
    static const std::regex pattern("^[A-Za-z0-9_.-]{1,15}$");
    return std::regex_match(value, pattern);
}

std::optional<uint64_t>
ParseIntegralDataRateBps(const std::string& value)
{
    uint64_t multiplier = 1;
    std::size_t suffixLength = 3;
    if (value.size() >= 4 && value.compare(value.size() - 4, 4, "Kbps") == 0)
    {
        multiplier = 1000ULL;
        suffixLength = 4;
    }
    else if (value.size() >= 4 && value.compare(value.size() - 4, 4, "Mbps") == 0)
    {
        multiplier = 1000000ULL;
        suffixLength = 4;
    }
    else if (value.size() >= 4 && value.compare(value.size() - 4, 4, "Gbps") == 0)
    {
        multiplier = 1000000000ULL;
        suffixLength = 4;
    }
    else if (value.size() < 3 || value.compare(value.size() - 3, 3, "bps") != 0)
    {
        return std::nullopt;
    }
    try
    {
        const uint64_t integral = std::stoull(value.substr(0, value.size() - suffixLength));
        if (integral == 0 || integral > std::numeric_limits<uint64_t>::max() / multiplier)
        {
            return std::nullopt;
        }
        return integral * multiplier;
    }
    catch (const std::exception&)
    {
        return std::nullopt;
    }
}

std::string
ValidateConfig(const EngineConfig& config, const std::vector<std::string>& tapUavs)
{
    if (config.uavCount < 1 || config.uavCount > MAX_UAVS)
    {
        return "uavCount must be in 1..5";
    }
    if (tapUavs.size() != config.uavCount)
    {
        return "tapUavs must contain exactly uavCount names";
    }
    std::set<std::string> uniqueTaps;
    if (!IsValidInterfaceName(config.tapGcs))
    {
        return "tapGcs is not a valid Linux interface name";
    }
    uniqueTaps.insert(config.tapGcs);
    for (const auto& name : tapUavs)
    {
        if (!IsValidInterfaceName(name))
        {
            return "tapUavs contains an invalid Linux interface name";
        }
        if (!uniqueTaps.insert(name).second)
        {
            return "TAP interface names must be unique";
        }
    }
    if (config.durationMs < 1 || config.durationMs > MAX_DURATION_MS)
    {
        return "durationMs must be in 1..86400000";
    }
    static const std::regex ratePattern("^[1-9][0-9]*(bps|Kbps|Mbps|Gbps)$");
    static const std::regex delayPattern("^[1-9][0-9]*(ns|us|ms|s)$");
    if (!std::regex_match(config.radioRate, ratePattern))
    {
        return "radioRate must be a positive integral ns-3 data rate";
    }
    const auto radioRateBps = ParseIntegralDataRateBps(config.radioRate);
    if (!radioRateBps)
    {
        return "radioRate is outside the supported integral bps range";
    }
    if (!std::regex_match(config.radioDelay, delayPattern))
    {
        return "radioDelay must be a positive integral ns-3 time";
    }
    const std::array<uint32_t, 3> limits = {config.queueControlMaxPackets,
                                            config.queuePayloadMaxPackets,
                                            config.queueAdditionalDataMaxPackets};
    if (std::any_of(limits.begin(), limits.end(), [](uint32_t value) {
            return value < 1 || value > MAX_QUEUE_PACKETS;
        }))
    {
        return "every queue bound must be in 1..1000000";
    }
    const std::array<uint32_t, 3> deadlines = {config.queueControlDeadlineMs,
                                               config.queuePayloadDeadlineMs,
                                               config.queueAdditionalDataDeadlineMs};
    const std::array<uint32_t, 3> maxAges = {config.queueControlMaxAgeMs,
                                             config.queuePayloadMaxAgeMs,
                                             config.queueAdditionalDataMaxAgeMs};
    for (uint32_t index = 0; index < deadlines.size(); ++index)
    {
        if (deadlines[index] < 1 || deadlines[index] > 60000 || maxAges[index] < 1 ||
            maxAges[index] > deadlines[index])
        {
            return "queue deadlines/max ages must satisfy 1 <= maxAge <= deadline <= 60000";
        }
    }
    if (!config.strictControlPriority)
    {
        return "strictControlPriority must be true for the protected product path";
    }
    if (!config.fairLowerClassesPerUav)
    {
        return "fairLowerClassesPerUav must be true for the protected product path";
    }
    if (!config.ingressProtectionEnabled)
    {
        return "ingressProtectionEnabled must be true for the protected product path";
    }
    if (config.minimumControlHeadroomBps == 0 || config.payloadAdmissionRateBps == 0 ||
        config.additionalDataAdmissionRateBps == 0)
    {
        return "minimum control headroom and lower admission rates must be positive";
    }
    if (!IngressProtectionController::CapacityKeepsMinimumControlHeadroom(
            *radioRateBps,
            config.minimumControlHeadroomBps,
            config.payloadAdmissionRateBps,
            config.additionalDataAdmissionRateBps))
    {
        return "payload/additional admission rates plus minimum control headroom exceed radioRate";
    }
    if (config.tokenBucketBurstBytesPerUav < 1 ||
        config.tokenBucketBurstBytesPerUav > 1000000)
    {
        return "tokenBucketBurstBytesPerUav must be in 1..1000000";
    }
    if (config.lowerRetryLimit < 1 || config.macRetryLimit < config.lowerRetryLimit ||
        config.macRetryLimit > 1000000)
    {
        return "retry limits must satisfy 1 <= lowerRetryLimit <= macRetryLimit <= 1000000";
    }
    if (config.eventLogFlushEvery < 1 || config.eventLogFlushEvery > 65536)
    {
        return "eventLogFlushEvery must be in 1..65536";
    }
    if (config.eventLogFlushMaxDelayMs < 1 || config.eventLogFlushMaxDelayMs > 1000)
    {
        return "eventLogFlushMaxDelayMs must be in 1..1000";
    }
    if (!(config.controlPriority < config.payloadPriority &&
          config.payloadPriority < config.additionalDataPriority))
    {
        return "priorities must satisfy control < payload < additionalData";
    }
    const std::set<uint32_t> tosValues = {config.controlTos,
                                          config.payloadTos,
                                          config.additionalDataTos};
    if (tosValues.size() != 3 || config.controlTos > 255 || config.payloadTos > 255 ||
        config.additionalDataTos > 255)
    {
        return "class TOS values must be unique bytes";
    }
    if (config.seed == 0)
    {
        return "seed must be non-zero";
    }
    if (config.run == 0)
    {
        return "run must be non-zero";
    }
    if (config.eventEpoch == 0)
    {
        return "eventEpoch must be non-zero";
    }
    if (config.selfTestBurst < 1 || config.selfTestBurst > 100000)
    {
        return "selfTestBurst must be in 1..100000";
    }
    if (!config.selfTest && config.selfTestUnknownTos)
    {
        return "selfTestUnknownTos requires selfTest";
    }
    if (config.sionnaIpcEnabled)
    {
        if (config.sionnaStateFile.empty())
        {
            return "sionnaStateFile is required when sionnaIpcEnabled";
        }
        if (config.sionnaPollIntervalMs < 1 || config.sionnaPollIntervalMs > 1000)
        {
            return "sionnaPollIntervalMs must be in 1..1000";
        }
        if (config.sionnaMaxUpdatesPerPoll < 1 || config.sionnaMaxUpdatesPerPoll > 4096)
        {
            return "sionnaMaxUpdatesPerPoll must be in 1..4096";
        }
        if (config.sionnaMaxStateTtlMs < 1 || config.sionnaMaxStateTtlMs > 60000)
        {
            return "sionnaMaxStateTtlMs must be in 1..60000";
        }
        if (config.sionnaIntervention != "natural" && config.sionnaIntervention != "force_drop" &&
            config.sionnaIntervention != "force_deliver")
        {
            return "sionnaIntervention must be natural, force_drop, or force_deliver";
        }
        if (!config.clockDatagramSocket.empty() &&
            (config.clockDatagramSocket.front() != '/' ||
             config.clockDatagramSocket.size() >= 100 ||
             config.clockDatagramSocket.find_first_of("\r\n") != std::string::npos))
        {
            return "clockDatagramSocket must be a short absolute AF_UNIX path";
        }
    }
    return "";
}

std::string
HexMac(const std::string& prefix, uint32_t deviceIndex)
{
    std::ostringstream out;
    out << prefix << std::hex << std::setfill('0') << std::setw(2) << (deviceIndex + 1);
    return out.str();
}

std::string
EndpointMac(uint32_t deviceIndex)
{
    std::ostringstream out;
    out << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << deviceIndex << ":00:10:10";
    return out.str();
}

std::string
RouterExternalMac(uint32_t deviceIndex)
{
    std::ostringstream out;
    out << "02:71:" << std::hex << std::setfill('0') << std::setw(2) << deviceIndex << ":00:00:01";
    return out.str();
}

std::string
RadioMac(uint32_t deviceIndex)
{
    return HexMac("02:72:00:00:00:", deviceIndex);
}

std::string
EndpointIp(uint32_t deviceIndex)
{
    return "10.71." + std::to_string(deviceIndex) + ".10";
}

std::string
RouterExternalIp(uint32_t deviceIndex)
{
    return "10.71." + std::to_string(deviceIndex) + ".1";
}

std::string
RadioIp(uint32_t deviceIndex)
{
    return "10.72.0." + std::to_string(deviceIndex + 1);
}

uint32_t
EndpointIndexFromDevice(const std::string& device)
{
    if (device == "gcs" || device == "cp")
    {
        return 0;
    }
    for (uint32_t index = 1; index <= MAX_UAVS; ++index)
    {
        if (device == "uav" + std::to_string(index))
        {
            return index;
        }
    }
    return MAX_UAVS + 1;
}

uint32_t
EndpointIndexFromIp(const Ipv4Address& address)
{
    const std::string value = Ipv4ToString(address);
    for (uint32_t index = 0; index <= MAX_UAVS; ++index)
    {
        if (value == EndpointIp(index))
        {
            return index;
        }
    }
    return MAX_UAVS + 1;
}

Ptr<Packet>
ReconstructIpv4EthernetFrame(const Ipv4Header& header,
                             Ptr<const Packet> ipv4Payload,
                             const std::string& observedDevice,
                             bool externalIngress)
{
    Ptr<Packet> frame = ipv4Payload->Copy();
    Ipv4Header reconstructedHeader = header;
    reconstructedHeader.SetPayloadSize(frame->GetSize());
    if (Node::ChecksumEnabled())
    {
        reconstructedHeader.EnableChecksum();
    }
    frame->AddHeader(reconstructedHeader);

    // CsmaNetDevice's DIX framing pads the IPv4 datagram to Ethernet's
    // minimum 46-byte payload before adding the header and trailer.
    if (frame->GetSize() < 46)
    {
        uint8_t padding[46]{};
        frame->AddAtEnd(Create<Packet>(padding, 46 - frame->GetSize()));
    }

    const uint32_t observedIndex = EndpointIndexFromDevice(observedDevice);
    const uint32_t sourceIndex = EndpointIndexFromIp(header.GetSource());
    if (observedIndex > MAX_UAVS)
    {
        throw std::runtime_error("cannot reconstruct IPv4 drop for unknown router identity");
    }
    const Mac48Address source = sourceIndex <= MAX_UAVS
                                    ? Mac48Address((externalIngress ? EndpointMac(sourceIndex)
                                                                  : RadioMac(sourceIndex))
                                                       .c_str())
                                    : Mac48Address("00:00:00:00:00:00");
    const Mac48Address destination(
        (externalIngress ? RouterExternalMac(observedIndex) : RadioMac(observedIndex)).c_str());
    EthernetHeader ethernet(false);
    ethernet.SetSource(source);
    ethernet.SetDestination(destination);
    ethernet.SetLengthType(0x0800);
    frame->AddHeader(ethernet);

    EthernetTrailer trailer;
    if (Node::ChecksumEnabled())
    {
        trailer.EnableFcs(true);
    }
    trailer.CalcFcs(frame);
    frame->AddTrailer(trailer);
    return frame;
}

class PacketEventLogger
{
  public:
    PacketEventLogger(const std::string& path,
                      uint64_t eventEpoch,
                      std::string configHash,
                      uint32_t seed,
                      uint64_t run,
                      uint32_t uavCount,
                      RadioController* radioController,
                      bool realtimeObservability,
                      uint32_t flushEvery,
                      uint32_t flushMaxDelayMs)
        : m_output(path, std::ios::out | std::ios::trunc),
          m_eventEpoch(eventEpoch),
          m_configHash(std::move(configHash)),
          m_seed(seed),
          m_run(run),
          m_uavCount(uavCount),
          m_radioController(radioController),
          m_realtimeObservability(realtimeObservability),
          m_flushEvery(flushEvery),
          m_flushMaxDelayMs(flushMaxDelayMs),
          m_hostStartNs(SteadyNowNs())
    {
        if (!m_output)
        {
            throw std::runtime_error("cannot open packet event JSONL: " + path);
        }
    }

    void Log(const std::string& event,
             const std::string& observedDevice,
             Ptr<const Packet> packet,
             int64_t queueDepth = -1,
             int64_t queueLimit = -1,
             const std::string& reason = "",
             uint64_t queueAgeNs = 0)
    {
        const FrameMetadata metadata = InspectFrame(packet);
        const uint64_t hostMonotonicNs = m_realtimeObservability ? SteadyNowNs() : 0;
        const int64_t schedulerLagNs =
            m_realtimeObservability
                ? static_cast<int64_t>(hostMonotonicNs - m_hostStartNs) -
                      Simulator::Now().GetNanoSeconds()
                : 0;
        const std::string sourceDevice =
            CanonicalDevice(ResolveSourceDevice(metadata, observedDevice, event));
        const std::string destinationDevice =
            CanonicalDevice(ResolveDestinationDevice(metadata, observedDevice, event));
        const std::string directedLink = sourceDevice + ">" + destinationDevice;
        const uint32_t classIndex = ClassIndex(metadata);
        const std::string queueId = directedLink + "." + metadata.trafficClass + ".q" +
                                    (classIndex < 3 ? std::to_string(classIndex) : "unmapped");
        const std::string canonicalObserved = CanonicalDevice(observedDevice);
        std::string physicalDeviceId = canonicalObserved + ".radio";
        if (event == "ingress")
        {
            physicalDeviceId = canonicalObserved + ".tap.ingress";
        }
        else if (event == "egress")
        {
            physicalDeviceId = canonicalObserved + ".tap.egress";
        }
        const std::string wireHash = PacketSha256(packet);
        ++m_sequence;

        m_output << '{' << "\"schema\":\"" << EVENT_SCHEMA << "\","
                 << "\"event_epoch\":" << m_eventEpoch << ',' << "\"event_sequence\":" << m_sequence
                 << ',' << "\"sim_time_ns\":" << Simulator::Now().GetNanoSeconds() << ','
                 << "\"host_monotonic_ns\":" << hostMonotonicNs << ','
                 << "\"host_clock_domain\":\"host-monotonic\","
                 << "\"scheduler_lag_ns\":" << schedulerLagNs << ','
                 << "\"event\":\"" << JsonEscape(event) << "\","
                 << "\"packet_wire_hash_algorithm\":\"sha256\","
                 << "\"packet_wire_hash\":\"" << wireHash << "\","
                 << "\"packet_wire_size\":" << packet->GetSize() << ','
                 << "\"packet_uid\":" << packet->GetUid() << ','
                 << "\"tos\":" << static_cast<uint32_t>(metadata.tos) << ','
                 << "\"dscp\":" << metadata.dscp << ',' << "\"traffic_class\":\""
                 << metadata.trafficClass << "\","
                 << "\"directed_link\":\"" << directedLink << "\","
                 << "\"queue_id\":\"" << queueId << "\","
                 << "\"device_id\":\"" << JsonEscape(physicalDeviceId) << "\","
                 << "\"source_mac\":\"" << metadata.sourceMac << "\","
                 << "\"destination_mac\":\"" << metadata.destinationMac << "\","
                 << "\"source_ip\":\"" << metadata.sourceIp << "\","
                 << "\"destination_ip\":\"" << metadata.destinationIp << "\","
                 << "\"transport_protocol\":";
        if (metadata.transportProtocol < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.transportProtocol;
        }
        m_output << ",\"source_udp_port\":";
        if (metadata.sourceUdpPort < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.sourceUdpPort;
        }
        m_output << ",\"destination_udp_port\":";
        if (metadata.destinationUdpPort < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.destinationUdpPort;
        }
        m_output << ",\"transport_payload_sha256\":";
        if (metadata.transportPayloadSha256.empty())
        {
            m_output << "null";
        }
        else
        {
            m_output << '"' << metadata.transportPayloadSha256 << '"';
        }
        m_output << ",\"transport_payload_size\":";
        if (metadata.transportPayloadSize < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.transportPayloadSize;
        }
        m_output << ",\"source_monotonic_ns\":";
        if (!metadata.sourceMonotonicValid)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.sourceMonotonicNs;
        }
        m_output << ",\"application_profile_id\":";
        if (metadata.applicationProfileId < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.applicationProfileId;
        }
        m_output << ",\"application_uav_id\":";
        if (metadata.applicationUavId < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << metadata.applicationUavId;
        }
        m_output << ',' << "\"p2mp\":" << (metadata.p2mp ? "true" : "false") << ','
                 << "\"root_transmission\":"
                 << ((metadata.p2mp && event == "channel") ? "true" : "false") << ','
                 << "\"queue_depth_packets\":";
        if (queueDepth < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << queueDepth;
        }
        m_output << ",\"queue_limit_packets\":";
        if (queueLimit < 0)
        {
            m_output << "null";
        }
        else
        {
            m_output << queueLimit;
        }
        m_output << ",\"queue_age_ns\":" << queueAgeNs;
        m_output << ",\"drop_reason\":";
        if (reason.empty())
        {
            m_output << "null";
        }
        else
        {
            m_output << '"' << JsonEscape(reason) << '"';
        }
        if (m_radioController && m_radioController->Enabled())
        {
            const auto decision = m_radioController->DecisionFor(packet);
            m_output << ",\"radio_state_status\":\""
                     << JsonEscape(decision ? decision->status : "not_decided") << '"';
            auto writeString = [this](const std::string& key, const std::string& value) {
                m_output << ",\"" << key << "\":";
                if (value.empty())
                {
                    m_output << "null";
                }
                else
                {
                    m_output << '"' << JsonEscape(value) << '"';
                }
            };
            if (decision && decision->state.available)
            {
                m_output << ",\"radio_state_sequence\":" << decision->state.stateSequence;
            }
            else
            {
                m_output << ",\"radio_state_sequence\":null";
            }
            writeString("radio_state_sha256", decision ? decision->state.stateSha256 : "");
            writeString("radio_query_id", decision ? decision->state.queryId : "");
            writeString("radio_applied_state_id", decision ? decision->state.appliedStateId : "");
            writeString("radio_result_wire_sha256",
                        decision ? decision->state.resultWireSha256 : "");
            writeString("radio_mapping_version", decision ? decision->state.mappingVersion : "");
            if (decision && decision->state.available)
            {
                m_output << ",\"radio_mapping_seed\":" << decision->state.mappingSeed
                         << ",\"radio_delay_ns\":" << decision->state.propagationDelayNs
                         << ",\"radio_service_rate_bps\":" << decision->state.serviceRateBps
                         << ",\"radio_serialization_time_ns\":" << decision->serializationTimeNs
                         << ",\"radio_base_serialization_time_ns\":"
                         << decision->baseSerializationTimeNs
                         << ",\"radio_service_padding_ns\":" << decision->servicePaddingNs
                         << ",\"radio_base_channel_delay_ns\":" << decision->baseChannelDelayNs
                         << ",\"radio_effective_channel_delay_ns\":"
                         << decision->effectiveChannelDelayNs
                         << ",\"radio_rate_applied_at_monotonic_ns\":";
                if (decision->rateAppliedAtMonotonicNs == 0)
                {
                    m_output << "null";
                }
                else
                {
                    m_output << decision->rateAppliedAtMonotonicNs;
                }
                m_output << ",\"radio_delay_applied_at_monotonic_ns\":";
                if (decision->delayAppliedAtMonotonicNs == 0)
                {
                    m_output << "null";
                }
                else
                {
                    m_output << decision->delayAppliedAtMonotonicNs;
                }
                m_output << ",\"radio_applied_device_id\":";
                if (decision->appliedDeviceId.empty())
                {
                    m_output << "null";
                }
                else
                {
                    m_output << '"' << JsonEscape(decision->appliedDeviceId) << '"';
                }
                m_output << ",\"radio_validity_start_monotonic_ns\":"
                         << decision->state.validityStartMonotonicNs
                         << ",\"radio_adapter_applied_monotonic_ns\":"
                         << decision->state.adapterAppliedMonotonicNs
                         << ",\"radio_expires_monotonic_ns\":"
                         << decision->state.localExpiresSteadyNs << ",\"radio_state_age_ns\":"
                         << (hostMonotonicNs >= decision->state.validityStartMonotonicNs
                                 ? hostMonotonicNs - decision->state.validityStartMonotonicNs
                                 : 0)
                         << ",\"radio_loss_probability\":" << std::setprecision(17)
                         << decision->state.lossProbability
                         << ",\"radio_loss_sample\":" << std::setprecision(17)
                         << decision->lossSample;
            }
            else
            {
                m_output << ",\"radio_mapping_seed\":null,\"radio_delay_ns\":null,"
                         << "\"radio_service_rate_bps\":null,"
                         << "\"radio_serialization_time_ns\":null,"
                         << "\"radio_base_serialization_time_ns\":null,"
                         << "\"radio_service_padding_ns\":null,"
                         << "\"radio_base_channel_delay_ns\":null,"
                         << "\"radio_effective_channel_delay_ns\":null,"
                         << "\"radio_rate_applied_at_monotonic_ns\":null,"
                         << "\"radio_delay_applied_at_monotonic_ns\":null,"
                         << "\"radio_applied_device_id\":null,"
                         << "\"radio_validity_start_monotonic_ns\":null,"
                         << "\"radio_adapter_applied_monotonic_ns\":null,"
                         << "\"radio_expires_monotonic_ns\":null,"
                         << "\"radio_state_age_ns\":null,"
                         << "\"radio_loss_probability\":null,\"radio_loss_sample\":null";
            }
            writeString("radio_delivery", decision ? decision->delivery : "");
            writeString("radio_intervention", decision ? decision->intervention : "");
        }
        m_output << ",\"config_sha256\":\"" << m_configHash << "\","
                 << "\"seed\":" << m_seed << ',' << "\"run\":" << m_run << "}\n";
        ++m_eventsSinceFlush;
        if (m_eventsSinceFlush >= m_flushEvery)
        {
            Flush();
        }
        else
        {
            ScheduleTimedFlush();
        }

        if (metadata.p2mp && event == "channel")
        {
            ++m_p2mpRootTransmissions;
        }
        if (metadata.p2mp && event == "egress" && observedDevice.rfind("uav", 0) == 0)
        {
            m_p2mpEgressDevices.insert(observedDevice);
        }
        ++m_eventCounts[event];
    }

    void Flush()
    {
        m_output.flush();
        m_eventsSinceFlush = 0;
    }

    uint64_t P2mpRootTransmissions() const
    {
        return m_p2mpRootTransmissions;
    }

    uint32_t P2mpEgressCount() const
    {
        return m_p2mpEgressDevices.size();
    }

    uint64_t EventCount(const std::string& event) const
    {
        auto found = m_eventCounts.find(event);
        return found == m_eventCounts.end() ? 0 : found->second;
    }

  private:
    void ScheduleTimedFlush()
    {
        if (m_timedFlushScheduled)
        {
            return;
        }
        m_timedFlushScheduled = true;
        Simulator::Schedule(MilliSeconds(m_flushMaxDelayMs),
                            &PacketEventLogger::TimedFlush,
                            this);
    }

    void TimedFlush()
    {
        m_timedFlushScheduled = false;
        if (m_eventsSinceFlush > 0)
        {
            Flush();
        }
    }

    static std::string CanonicalDevice(const std::string& device)
    {
        return device == "gcs" ? "cp" : device;
    }

    std::string DeviceFromMacOrIp(const std::string& mac, const std::string& ip) const
    {
        for (uint32_t index = 0; index <= m_uavCount; ++index)
        {
            const std::string device = index == 0 ? "gcs" : "uav" + std::to_string(index);
            if (ip == EndpointIp(index))
            {
                return device;
            }
        }
        for (uint32_t index = 0; index <= m_uavCount; ++index)
        {
            const std::string device = index == 0 ? "gcs" : "uav" + std::to_string(index);
            if (mac == EndpointMac(index) || mac == RouterExternalMac(index) ||
                mac == RadioMac(index))
            {
                return device;
            }
        }
        return "unknown";
    }

    std::string ResolveSourceDevice(const FrameMetadata& metadata,
                                    const std::string& observed,
                                    const std::string& event) const
    {
        std::string source = DeviceFromMacOrIp(metadata.sourceMac, metadata.sourceIp);
        if (source == "unknown" && event != "egress" && event != "phy_rx_drop")
        {
            source = observed;
        }
        return source;
    }

    std::string ResolveDestinationDevice(const FrameMetadata& metadata,
                                         const std::string& observed,
                                         const std::string& event) const
    {
        if (metadata.p2mp)
        {
            return "p2mp";
        }
        // For routed IPv4 traffic the L3 destination is authoritative.  The
        // ingress Ethernet destination is merely this router's local MAC and
        // must not turn an unreachable address into a false cp>cp link.
        std::string destination = metadata.ipv4
                                      ? DeviceFromMacOrIp("", metadata.destinationIp)
                                      : DeviceFromMacOrIp(metadata.destinationMac,
                                                          metadata.destinationIp);
        if (destination == "unknown" && (event == "egress" || event == "phy_rx_drop"))
        {
            destination = observed;
        }
        return destination;
    }

    std::ofstream m_output;
    uint64_t m_eventEpoch;
    std::string m_configHash;
    uint32_t m_seed;
    uint64_t m_run;
    uint32_t m_uavCount;
    RadioController* m_radioController;
    bool m_realtimeObservability;
    uint32_t m_flushEvery;
    uint32_t m_flushMaxDelayMs;
    uint64_t m_hostStartNs;
    uint32_t m_eventsSinceFlush = 0;
    bool m_timedFlushScheduled = false;
    uint64_t m_sequence = 0;
    uint64_t m_p2mpRootTransmissions = 0;
    std::set<std::string> m_p2mpEgressDevices;
    std::map<std::string, uint64_t> m_eventCounts;
};

void
TraceIngress(PacketEventLogger* logger, std::string context, Ptr<const Packet> packet)
{
    // Every source-timestamped product packet (BQO1, BSF1, or BDP1) gets its
    // first rich record from queue admission (admit or ingress drop). Avoid
    // hashing/serializing it before the cheap token/deadline decision.
    const FrameMetadata metadata = InspectFrame(packet, false);
    if (metadata.sourceMonotonicValid)
    {
        return;
    }
    logger->Log("ingress", context, packet);
}

void
TraceIpv4Drop(PacketEventLogger* logger,
              Ptr<NetDevice> externalDevice,
              std::string context,
              const Ipv4Header& header,
              Ptr<const Packet> ipv4Payload,
              Ipv4L3Protocol::DropReason reason,
              Ptr<Ipv4> ipv4,
              uint32_t interface)
{
    if (reason != Ipv4L3Protocol::DROP_NO_ROUTE)
    {
        return;
    }
    const bool externalIngress = ipv4 && interface < ipv4->GetNInterfaces() &&
                                 ipv4->GetNetDevice(interface) == externalDevice;
    Ptr<Packet> frame =
        ReconstructIpv4EthernetFrame(header, ipv4Payload, context, externalIngress);
    logger->Log("drop", context, frame, -1, -1, "ipv4_no_route");
}

void
TraceChannel(RadioController* radioController,
             Ptr<CsmaNetDevice> sourceDevice,
             PacketEventLogger* logger,
             std::string context,
             Ptr<const Packet> packet)
{
    if (radioController && radioController->Enabled())
    {
        radioController->ApplyForTransmit(context, packet, sourceDevice);
    }
    logger->Log("channel", context, packet);
}

void
TraceEgress(PacketEventLogger* logger, std::string context, Ptr<const Packet> packet)
{
    logger->Log("egress", context, packet);
}

void
TraceBackoff(LowerPacketGuard* packetGuard,
             Ptr<CsmaNetDevice> sourceDevice,
             PacketEventLogger* logger,
             std::string context,
             Ptr<const Packet> packet)
{
    logger->Log("backoff", context, packet);
    if (!packetGuard || !sourceDevice)
    {
        return;
    }
    const BackoffGuardDecision guard = packetGuard->ObserveBackoff(packet, SteadyNowNs());
    if (guard.requestAbort)
    {
        // The tracked ns-3 hook is consumed at the next busy check and lets
        // native TransmitAbort restore READY and wake the global scheduler.
        sourceDevice->RequestCurrentPacketAbortOnNextBusy();
    }
}

void
TracePhyTxEnd(LowerPacketGuard* packetGuard,
              Ptr<CsmaNetDevice> sourceDevice,
              uint32_t macRetryLimit,
              PacketEventLogger* logger,
              std::string context,
              Ptr<const Packet> packet)
{
    if (sourceDevice)
    {
        sourceDevice->SetBackoffParams(MicroSeconds(1), 1, 1000, macRetryLimit, 10);
    }
    if (packetGuard)
    {
        packetGuard->Forget(packet);
    }
    logger->Log("phy_tx_end", context, packet);
}

void
TracePhyTxDrop(LowerPacketGuard* packetGuard,
               Ptr<CsmaNetDevice> sourceDevice,
               uint32_t macRetryLimit,
               PacketEventLogger* logger,
               std::string context,
               Ptr<const Packet> packet)
{
    if (sourceDevice)
    {
        sourceDevice->SetBackoffParams(MicroSeconds(1), 1, 1000, macRetryLimit, 10);
    }
    if (sourceDevice && !sourceDevice->IsSendEnabled())
    {
        // This is the synchronous confirmation of the queue sentinel drop.
        // The queue already emitted the single causal terminal event.
        sourceDevice->SetSendEnable(true);
        if (packetGuard)
        {
            packetGuard->Forget(packet);
        }
        return;
    }
    const auto guardedReason = packetGuard ? packetGuard->PendingDropReason(packet) : std::nullopt;
    logger->Log("drop",
                context,
                packet,
                -1,
                -1,
                guardedReason ? *guardedReason : "phy_tx_drop");
    if (packetGuard)
    {
        packetGuard->Forget(packet);
    }
}

void
TracePhyRxDrop(RadioController* radioController,
               PacketEventLogger* logger,
               std::string context,
               Ptr<const Packet> packet)
{
    const auto decision = radioController ? radioController->DecisionFor(packet) : std::nullopt;
    logger->Log("drop",
                context,
                packet,
                -1,
                -1,
                decision && decision->drop ? decision->dropReason : "phy_rx_drop");
}

bool
DiscardReceive(Ptr<NetDevice>, Ptr<const Packet>, uint16_t, const Address&)
{
    return true;
}

bool
DiscardPromiscReceive(Ptr<NetDevice>,
                      Ptr<const Packet>,
                      uint16_t,
                      const Address&,
                      const Address&,
                      NetDevice::PacketType)
{
    return true;
}

Mac48Address
EndpointMacAddress(uint32_t deviceIndex)
{
    return Mac48Address(EndpointMac(deviceIndex).c_str());
}

Mac48Address
RouterExternalMacAddress(uint32_t deviceIndex)
{
    return Mac48Address(RouterExternalMac(deviceIndex).c_str());
}

Mac48Address
RadioMacAddress(uint32_t deviceIndex)
{
    return Mac48Address(RadioMac(deviceIndex).c_str());
}

struct SelfTestFrame
{
    Ptr<CsmaNetDevice> device;
    Mac48Address sourceMac;
    Mac48Address destinationMac;
    Ipv4Address sourceIp;
    Ipv4Address destinationIp;
    uint8_t tos = 0;
    uint32_t sequence = 0;
    uint16_t destinationPort = 0;
};

uint16_t
SelfTestDestinationPort(const SelfTestFrame& frame)
{
    if (frame.destinationPort != 0)
    {
        return frame.destinationPort;
    }
    if (frame.destinationIp.IsMulticast())
    {
        return 14900;
    }
    uint16_t base = 14800;
    if (frame.tos == g_controlTos)
    {
        base = 14600;
    }
    else if (frame.tos == g_payloadTos)
    {
        base = 14700;
    }
    const std::string destination = DeviceFromEndpointIp(Ipv4ToString(frame.destinationIp));
    if (destination == "cp")
    {
        return base;
    }
    for (uint16_t index = 1; index <= MAX_UAVS; ++index)
    {
        if (destination == "uav" + std::to_string(index))
        {
            return static_cast<uint16_t>(base + index);
        }
    }
    return 14550;
}

void
SendSelfTestFrame(SelfTestFrame frame)
{
    std::ostringstream marker;
    marker << "ams-self-test:" << frame.sequence;
    const std::string payloadBytes = marker.str();
    Ptr<Packet> packet =
        Create<Packet>(reinterpret_cast<const uint8_t*>(payloadBytes.data()), payloadBytes.size());
    UdpHeader udp;
    udp.SetSourcePort(static_cast<uint16_t>(20000 + (frame.sequence % 10000)));
    udp.SetDestinationPort(SelfTestDestinationPort(frame));
    udp.InitializeChecksum(frame.sourceIp, frame.destinationIp, 17);
    udp.EnableChecksums();
    udp.ForcePayloadSize(packet->GetSize());
    packet->AddHeader(udp);
    Ipv4Header ipv4;
    ipv4.SetSource(frame.sourceIp);
    ipv4.SetDestination(frame.destinationIp);
    ipv4.SetProtocol(17);
    ipv4.SetTtl(64);
    ipv4.SetTos(frame.tos);
    ipv4.SetIdentification(static_cast<uint16_t>(frame.sequence));
    ipv4.SetPayloadSize(packet->GetSize());
    ipv4.EnableChecksum();
    packet->AddHeader(ipv4);
    if (!frame.device->SendFrom(packet, frame.sourceMac, frame.destinationMac, 0x0800))
    {
        NS_LOG_WARN("self-test frame rejected at ingress sequence=" << frame.sequence);
    }
}

void
ScheduleSelfTest(const NetDeviceContainer& endpointDevices,
                 uint32_t uavCount,
                 uint32_t burst,
                 bool unknownTos)
{
    uint32_t sequence = 1;
    uint64_t nextUs = 1000;
    const std::array<uint8_t, 3> tosValues = {g_controlTos, g_payloadTos, g_additionalDataTos};
    for (uint32_t uav = 1; uav <= uavCount; ++uav)
    {
        Ptr<CsmaNetDevice> gcs = DynamicCast<CsmaNetDevice>(endpointDevices.Get(0));
        Ptr<CsmaNetDevice> remote = DynamicCast<CsmaNetDevice>(endpointDevices.Get(uav));
        for (uint8_t tos : tosValues)
        {
            Simulator::Schedule(MicroSeconds(nextUs),
                                &SendSelfTestFrame,
                                SelfTestFrame{gcs,
                                              EndpointMacAddress(0),
                                              RouterExternalMacAddress(0),
                                              Ipv4Address(EndpointIp(0).c_str()),
                                              Ipv4Address(EndpointIp(uav).c_str()),
                                              tos,
                                              sequence++});
            nextUs += 5000;
            Simulator::Schedule(MicroSeconds(nextUs),
                                &SendSelfTestFrame,
                                SelfTestFrame{remote,
                                              EndpointMacAddress(uav),
                                              RouterExternalMacAddress(uav),
                                              Ipv4Address(EndpointIp(uav).c_str()),
                                              Ipv4Address(EndpointIp(0).c_str()),
                                              tos,
                                              sequence++});
            nextUs += 5000;
        }
    }

    Ptr<CsmaNetDevice> gcs = DynamicCast<CsmaNetDevice>(endpointDevices.Get(0));
    const Ipv4Address multicastIp("239.71.0.1");
    Simulator::Schedule(MicroSeconds(nextUs),
                        &SendSelfTestFrame,
                        SelfTestFrame{gcs,
                                      EndpointMacAddress(0),
                                      Mac48Address::GetMulticast(multicastIp),
                                      Ipv4Address(EndpointIp(0).c_str()),
                                      multicastIp,
                                      g_additionalDataTos,
                                      sequence++});
    nextUs += 5000;

    // The legacy ArduPilot listener port is deliberately active in external
    // M3 tests but is not a declared endpoint-matrix cell.  Exercise the same
    // default-on engine rejection in every compiled self-test.
    Simulator::Schedule(MicroSeconds(nextUs),
                        &SendSelfTestFrame,
                        SelfTestFrame{gcs,
                                      EndpointMacAddress(0),
                                      RouterExternalMacAddress(0),
                                      Ipv4Address(EndpointIp(0).c_str()),
                                      Ipv4Address(EndpointIp(1).c_str()),
                                      g_controlTos,
                                      sequence++,
                                      14550});
    nextUs += 5000;

    // This destination is outside every configured endpoint and radio subnet.
    // It must produce one independent Ipv4L3Protocol DROP_NO_ROUTE event; its
    // allowed-looking UDP identity must not be relabelled as a queue rejection.
    Simulator::Schedule(MicroSeconds(nextUs),
                        &SendSelfTestFrame,
                        SelfTestFrame{gcs,
                                      EndpointMacAddress(0),
                                      RouterExternalMacAddress(0),
                                      Ipv4Address(EndpointIp(0).c_str()),
                                      Ipv4Address("198.18.0.1"),
                                      g_additionalDataTos,
                                      sequence++,
                                      15300});
    nextUs += 5000;

    for (uint32_t index = 0; index < burst; ++index)
    {
        Simulator::Schedule(MicroSeconds(nextUs),
                            &SendSelfTestFrame,
                            SelfTestFrame{gcs,
                                          EndpointMacAddress(0),
                                          RouterExternalMacAddress(0),
                                          Ipv4Address(EndpointIp(0).c_str()),
                                          Ipv4Address(EndpointIp(1).c_str()),
                                          g_payloadTos,
                                          sequence++});
    }
    nextUs += 5000;
    if (unknownTos)
    {
        Simulator::Schedule(MicroSeconds(nextUs),
                            &SendSelfTestFrame,
                            SelfTestFrame{gcs,
                                          EndpointMacAddress(0),
                                          RouterExternalMacAddress(0),
                                          Ipv4Address(EndpointIp(0).c_str()),
                                          Ipv4Address(EndpointIp(1).c_str()),
                                          4,
                                          sequence++});
    }
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

uint32_t
AddRouterInterface(Ptr<Node> router,
                   Ptr<NetDevice> device,
                   const std::string& address,
                   const std::string& mask)
{
    Ptr<Ipv4> ipv4 = router->GetObject<Ipv4>();
    if (!ipv4)
    {
        throw std::runtime_error("router has no ns-3 IPv4 stack");
    }
    const uint32_t interface = ipv4->AddInterface(device);
    if (!ipv4->AddAddress(
            interface,
            Ipv4InterfaceAddress(Ipv4Address(address.c_str()), Ipv4Mask(mask.c_str()))))
    {
        throw std::runtime_error("failed to assign router address " + address);
    }
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
    if (!ipv4)
    {
        throw std::runtime_error("router has no ns-3 Ipv4L3Protocol");
    }
    const int32_t interfaceIndex = ipv4->GetInterfaceForDevice(device);
    if (interfaceIndex < 0)
    {
        throw std::runtime_error("router device has no IPv4 interface");
    }
    Ptr<Ipv4Interface> interface = ipv4->GetInterface(static_cast<uint32_t>(interfaceIndex));
    Ptr<ArpCache> cache = interface->GetArpCache();
    if (!cache)
    {
        throw std::runtime_error("router IPv4 interface has no ARP cache");
    }
    ArpCache::Entry* entry = cache->Lookup(Ipv4Address(address.c_str()));
    if (!entry)
    {
        entry = cache->Add(Ipv4Address(address.c_str()));
    }
    entry->SetMacAddress(mac);
    entry->MarkPermanent();
}

void
WriteReadyFile(const std::string& readyFile,
               const std::string& configHash,
               uint64_t eventEpoch,
               uint32_t uavCount)
{
    if (readyFile.empty())
    {
        return;
    }
    std::ofstream output(readyFile, std::ios::out | std::ios::trunc);
    if (!output)
    {
        NS_FATAL_ERROR("cannot write readiness file: " << readyFile);
    }
    output << "{\"status\":\"ready\",\"contract\":\"" << CONTRACT << "\",\"config_sha256\":\""
           << configHash << "\",\"event_epoch\":" << eventEpoch << ",\"uav_count\":" << uavCount
           << ",\"pid\":" << static_cast<uint64_t>(::getpid()) << "}\n";
}

class ClockDatagramProducer
{
  public:
    explicit ClockDatagramProducer(const std::string& path)
        : m_enabled(!path.empty())
    {
        if (!m_enabled)
        {
            return;
        }
        m_socket = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (m_socket < 0)
        {
            throw std::runtime_error("cannot create M4 clock datagram socket");
        }
        std::memset(&m_address, 0, sizeof(m_address));
        m_address.sun_family = AF_UNIX;
        std::memcpy(m_address.sun_path, path.c_str(), path.size() + 1);
    }

    ~ClockDatagramProducer()
    {
        if (m_socket >= 0)
        {
            ::close(m_socket);
        }
    }

    void Start()
    {
        if (m_enabled)
        {
            Simulator::ScheduleNow(&ClockDatagramProducer::Emit, this);
        }
    }

  private:
    void Emit()
    {
        const uint64_t now = SteadyNowNs();
        std::ostringstream stream;
        stream << "{\"producer\":\"ns3_packet_engine\",\"producer_monotonic_ns\":" << now
               << ",\"producer_pid\":" << static_cast<uint64_t>(::getpid())
               << ",\"sample_index\":" << m_sequence << ",\"schema\":\"ams.m4.clock_datagram/v1\"}";
        const std::string payload = stream.str();
        const ssize_t sent = ::sendto(m_socket,
                                      payload.data(),
                                      payload.size(),
                                      MSG_DONTWAIT,
                                      reinterpret_cast<const sockaddr*>(&m_address),
                                      sizeof(m_address));
        const bool accepted = sent == static_cast<ssize_t>(payload.size());
        if (accepted)
        {
            ++m_sequence;
        }
        Simulator::Schedule(accepted ? Seconds(1) : MilliSeconds(100),
                            &ClockDatagramProducer::Emit,
                            this);
    }

    bool m_enabled = false;
    int m_socket = -1;
    sockaddr_un m_address{};
    uint64_t m_sequence = 0;
};

} // namespace

int
main(int argc, char* argv[])
{
    EngineConfig config;
    CommandLine command(__FILE__);
    command.AddValue("uavCount", "Number of external UAV TAP endpoints (1..5)", config.uavCount);
    command.AddValue("tapGcs", "Existing GCS-side TAP device", config.tapGcs);
    command.AddValue("tapUavs", "Comma-separated existing UAV TAP devices", config.tapUavs);
    command.AddValue("durationMs",
                     "Maximum simulation/realtime duration in milliseconds",
                     config.durationMs);
    command.AddValue("radioRate", "Shared CSMA radio-medium data rate", config.radioRate);
    command.AddValue("radioDelay", "Shared CSMA radio-medium propagation delay", config.radioDelay);
    command.AddValue("queueControlMaxPackets",
                     "Bound for the control transmit queue",
                     config.queueControlMaxPackets);
    command.AddValue("queuePayloadMaxPackets",
                     "Bound for the payload transmit queue",
                     config.queuePayloadMaxPackets);
    command.AddValue("queueAdditionalDataMaxPackets",
                     "Bound for the additional-data transmit queue",
                     config.queueAdditionalDataMaxPackets);
    command.AddValue("queueControlDeadlineMs",
                     "Control queue deadline in milliseconds",
                     config.queueControlDeadlineMs);
    command.AddValue("queuePayloadDeadlineMs",
                     "Payload queue deadline in milliseconds",
                     config.queuePayloadDeadlineMs);
    command.AddValue("queueAdditionalDataDeadlineMs",
                     "Additional-data queue deadline in milliseconds",
                     config.queueAdditionalDataDeadlineMs);
    command.AddValue("queueControlMaxAgeMs",
                     "Maximum control queue age in milliseconds",
                     config.queueControlMaxAgeMs);
    command.AddValue("queuePayloadMaxAgeMs",
                     "Maximum payload queue age in milliseconds",
                     config.queuePayloadMaxAgeMs);
    command.AddValue("queueAdditionalDataMaxAgeMs",
                     "Maximum additional-data queue age in milliseconds",
                     config.queueAdditionalDataMaxAgeMs);
    command.AddValue("strictControlPriority",
                     "Serve all queued control before either lower-priority class",
                     config.strictControlPriority);
    command.AddValue("fairLowerClassesPerUav",
                     "Enable per-UAV round-robin within both lower-priority classes",
                     config.fairLowerClassesPerUav);
    command.AddValue("ingressProtectionEnabled",
                     "Enable payload/additional ingress token buckets",
                     config.ingressProtectionEnabled);
    command.AddValue("shapingEnabled",
                     "Startup-authorized lower-class shaping mode",
                     config.shapingEnabled);
    command.AddValue("minimumControlHeadroomBps",
                     "Minimum static control headroom required by lower-class admission validation",
                     config.minimumControlHeadroomBps);
    command.AddValue("payloadAdmissionRateBps",
                     "Aggregate payload token-bucket rate",
                     config.payloadAdmissionRateBps);
    command.AddValue("additionalDataAdmissionRateBps",
                     "Aggregate additional-data token-bucket rate",
                     config.additionalDataAdmissionRateBps);
    command.AddValue("tokenBucketBurstBytesPerUav",
                     "Per-UAV lower-class token-bucket burst in wire bytes",
                     config.tokenBucketBurstBytesPerUav);
    command.AddValue("lowerRetryLimit",
                     "Backoff bound for payload and additional-data packets",
                     config.lowerRetryLimit);
    command.AddValue("macRetryLimit",
                     "Hard native CSMA retry bound for every device",
                     config.macRetryLimit);
    command.AddValue("eventLogFlushEvery",
                     "Flush packet JSONL after this many events",
                     config.eventLogFlushEvery);
    command.AddValue("eventLogFlushMaxDelayMs",
                     "Maximum simulator delay before flushing pending packet JSONL events",
                     config.eventLogFlushMaxDelayMs);
    command.AddValue("controlPriority", "Configured control priority", config.controlPriority);
    command.AddValue("payloadPriority", "Configured payload priority", config.payloadPriority);
    command.AddValue("additionalDataPriority",
                     "Configured additional-data priority",
                     config.additionalDataPriority);
    command.AddValue("controlTos", "Control IPv4 TOS byte", config.controlTos);
    command.AddValue("payloadTos", "Payload IPv4 TOS byte", config.payloadTos);
    command.AddValue("additionalDataTos",
                     "Additional-data IPv4 TOS byte",
                     config.additionalDataTos);
    command.AddValue("seed", "Deterministic ns-3 RNG seed", config.seed);
    command.AddValue("run", "Deterministic ns-3 RNG run/substream", config.run);
    command.AddValue("eventEpoch",
                     "Non-zero lifecycle epoch copied into every event",
                     config.eventEpoch);
    command.AddValue("selfTest",
                     "Run without TAP/root and inject frames directly at NetDevice",
                     config.selfTest);
    command.AddValue("selfTestBurst",
                     "Same-time payload frames used to exercise queue bounds",
                     config.selfTestBurst);
    command.AddValue("selfTestUnknownTos",
                     "Inject one fail-closed unmapped-TOS frame",
                     config.selfTestUnknownTos);
    command.AddValue("configHash", "Expected canonical CLI SHA-256", config.configHash);
    command.AddValue("printConfigHash",
                     "Print resolved canonical CLI SHA-256 and exit",
                     config.printConfigHash);
    command.AddValue("eventsFile", "Packet event JSONL output", config.eventsFile);
    command.AddValue("pcapPrefix", "Optional shared-medium PCAP prefix", config.pcapPrefix);
    command.AddValue("readyFile", "Optional readiness JSON marker", config.readyFile);
    command.AddValue("stopFile", "Optional realtime stop marker", config.stopFile);
    command.AddValue("sionnaIpcEnabled",
                     "Enable fail-closed asynchronous Sionna state IPC",
                     config.sionnaIpcEnabled);
    command.AddValue("sionnaStateFile",
                     "Append-only applied-state JSONL produced by the Sionna adapter",
                     config.sionnaStateFile);
    command.AddValue("sionnaPollIntervalMs",
                     "Bounded applied-state IPC polling interval",
                     config.sionnaPollIntervalMs);
    command.AddValue("sionnaMaxUpdatesPerPoll",
                     "Maximum state records consumed per simulator poll",
                     config.sionnaMaxUpdatesPerPoll);
    command.AddValue("sionnaMaxStateTtlMs",
                     "Maximum accepted remaining state validity",
                     config.sionnaMaxStateTtlMs);
    command.AddValue("sionnaIntervention",
                     "Causal decision mode: natural, force_drop, or force_deliver",
                     config.sionnaIntervention);
    command.AddValue("clockDatagramSocket",
                     "Optional bounded AF_UNIX clock collector socket (M4 only)",
                     config.clockDatagramSocket);
    command.Parse(argc, argv);

    const std::vector<std::string> tapUavs = ResolveTapUavs(config);
    const std::string validationError = ValidateConfig(config, tapUavs);
    if (!validationError.empty())
    {
        std::cerr << "FAIL " << validationError << '\n';
        return 2;
    }
    const uint64_t radioCapacityBps = *ParseIntegralDataRateBps(config.radioRate);
    g_controlTos = static_cast<uint8_t>(config.controlTos);
    g_payloadTos = static_cast<uint8_t>(config.payloadTos);
    g_additionalDataTos = static_cast<uint8_t>(config.additionalDataTos);
    const std::string canonicalConfig = CanonicalConfig(config, tapUavs);
    const std::string resolvedHash =
        Sha256Hex(reinterpret_cast<const uint8_t*>(canonicalConfig.data()), canonicalConfig.size());
    if (config.printConfigHash)
    {
        std::cout << resolvedHash << '\n';
        return 0;
    }
    static const std::regex hashPattern("^[0-9a-f]{64}$");
    if (!std::regex_match(config.configHash, hashPattern))
    {
        std::cerr << "FAIL configHash must be exactly 64 lowercase hexadecimal characters\n";
        return 2;
    }
    if (config.configHash != resolvedHash)
    {
        std::cerr << "FAIL configHash mismatch: expected=" << config.configHash
                  << " resolved=" << resolvedHash << '\n';
        return 2;
    }
    if (config.eventsFile.empty())
    {
        std::cerr << "FAIL eventsFile must not be empty\n";
        return 2;
    }

    try
    {
        const bool tokenBucketSelfTest = IngressProtectionController::TokenBucketSelfTest();
        const bool deadlineDropSelfTest = IngressProtectionController::DeadlineDropSelfTest();
        const bool minimumControlHeadroomSelfTest =
            IngressProtectionController::MinimumControlHeadroomSelfTest();
        const bool asymmetricPayloadDemandSelfTest =
            IngressProtectionController::AsymmetricPayloadDemandSelfTest();
        const bool profileIdNoBypassSelfTest =
            IngressProtectionController::ProfileIdCannotBypassSelfTest();
        const bool perUavFairnessSelfTest = PerUavRoundRobin::DeterministicSelfTest();
        const bool retryBoundSelfTest = LowerPacketGuard::DeterministicSelfTest();
        const bool strictPrioritySelfTest = StrictPriorityScheduler::DeterministicSelfTest();
        const bool globalRadioSchedulerSelfTest = GlobalRadioScheduler::DeterministicSelfTest();
        const bool staleGrantSelfTest = ExactPacketGrant::DeterministicSelfTest();
        if (config.selfTest &&
            !(tokenBucketSelfTest && deadlineDropSelfTest && minimumControlHeadroomSelfTest &&
              asymmetricPayloadDemandSelfTest &&
              profileIdNoBypassSelfTest &&
              perUavFairnessSelfTest && retryBoundSelfTest && strictPrioritySelfTest &&
              globalRadioSchedulerSelfTest && staleGrantSelfTest))
        {
            throw std::runtime_error("control-plane protection self-test failed");
        }
        RngSeedManager::SetSeed(config.seed);
        RngSeedManager::SetRun(config.run);
        if (!config.selfTest || config.sionnaIpcEnabled)
        {
            // Sionna state validity is defined in host CLOCK_MONOTONIC.  The
            // feature-flagged compiled self-test therefore uses the same
            // realtime simulator as production, so expiry/queue races are
            // exercised against the authoritative clock instead of a fast
            // synthetic simulation timeline.
            GlobalValue::Bind("SimulatorImplementationType",
                              StringValue("ns3::RealtimeSimulatorImpl"));
        }
        GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

        RadioStateTable radioStates(config.sionnaIpcEnabled,
                                    config.sionnaStateFile,
                                    config.sionnaPollIntervalMs,
                                    config.sionnaMaxUpdatesPerPoll,
                                    config.sionnaMaxStateTtlMs,
                                    radioCapacityBps);
        RadioController radioController(config.sionnaIpcEnabled,
                                        &radioStates,
                                        config.sionnaIntervention);
        IngressProtectionController ingressProtection(config.ingressProtectionEnabled &&
                                                           config.shapingEnabled,
                                                       config.uavCount,
                                                       config.minimumControlHeadroomBps,
                                                       config.payloadAdmissionRateBps,
                                                       config.additionalDataAdmissionRateBps,
                                                       config.tokenBucketBurstBytesPerUav);
        LowerPacketGuard packetGuard(config.lowerRetryLimit);
        PacketEventLogger logger(config.eventsFile,
                                 config.eventEpoch,
                                 resolvedHash,
                                 config.seed,
                                 config.run,
                                 config.uavCount,
                                 &radioController,
                                 !config.selfTest || config.sionnaIpcEnabled,
                                 config.eventLogFlushEvery,
                                 config.eventLogFlushMaxDelayMs);
        ClockDatagramProducer clockDatagrams(config.clockDatagramSocket);
        NodeContainer routers;
        NodeContainer endpointGhosts;
        routers.Create(config.uavCount + 1);
        endpointGhosts.Create(config.uavCount + 1);
        InternetStackHelper internet;
        internet.Install(routers);

        CsmaHelper external;
        external.SetChannelAttribute("DataRate", StringValue("1Gbps"));
        external.SetChannelAttribute("Delay", StringValue("10us"));
        NetDeviceContainer endpointDevices;
        NetDeviceContainer routerExternalDevices;
        for (uint32_t index = 0; index <= config.uavCount; ++index)
        {
            NodeContainer segment(endpointGhosts.Get(index), routers.Get(index));
            NetDeviceContainer segmentDevices = external.Install(segment);
            segmentDevices.Get(0)->SetAddress(EndpointMacAddress(index));
            segmentDevices.Get(1)->SetAddress(RouterExternalMacAddress(index));
            endpointDevices.Add(segmentDevices.Get(0));
            routerExternalDevices.Add(segmentDevices.Get(1));
            AddRouterInterface(routers.Get(index),
                               segmentDevices.Get(1),
                               RouterExternalIp(index),
                               "255.255.255.0");
        }

        CsmaHelper radio;
        radio.SetChannelAttribute("DataRate", StringValue(config.radioRate));
        radio.SetChannelAttribute("Delay", StringValue(config.radioDelay));
        NetDeviceContainer radioDevices = radio.Install(routers);
        Ptr<CsmaChannel> radioChannel;
        if (radioDevices.GetN() > 0)
        {
            radioChannel = DynamicCast<CsmaChannel>(radioDevices.Get(0)->GetChannel());
            radioController.SetChannel(radioChannel);
        }
        GlobalRadioScheduler globalRadioScheduler(radioChannel, config.uavCount);
        globalRadioScheduler.InstallChannelCallback();

        for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
        {
            Ptr<CsmaNetDevice> device = DynamicCast<CsmaNetDevice>(radioDevices.Get(index));
            if (!device)
            {
                throw std::runtime_error("CsmaHelper did not create CsmaNetDevice");
            }
            const std::string deviceId = index == 0 ? "gcs" : "uav" + std::to_string(index);
            device->SetAddress(RadioMacAddress(index));
            AddRouterInterface(routers.Get(index), device, RadioIp(index), "255.255.255.0");
            Ptr<AmsThreeClassQueue> queue = CreateObject<AmsThreeClassQueue>();
            queue->SetQos(config.queueControlMaxPackets,
                          config.queuePayloadMaxPackets,
                          config.queueAdditionalDataMaxPackets,
                          config.queueControlDeadlineMs,
                          config.queuePayloadDeadlineMs,
                          config.queueAdditionalDataDeadlineMs,
                          config.queueControlMaxAgeMs,
                          config.queuePayloadMaxAgeMs,
                          config.queueAdditionalDataMaxAgeMs,
                          config.uavCount);
            queue->SetIngressProtection(&ingressProtection);
            queue->SetPacketGuard(&packetGuard);
            queue->SetRadioDevice(device);
            queue->SetRealtimeDeadlineClock(!config.selfTest || config.sionnaIpcEnabled);
            queue->SetIdentity(deviceId,
                               [&logger](const std::string& event,
                                         const std::string& observedDevice,
                                         Ptr<const Packet> packet,
                                         int64_t depth,
                                         int64_t limit,
                                         uint64_t queueAgeNs,
                                         const std::string& reason) {
                                   logger.Log(
                                       event, observedDevice, packet, depth, limit, reason, queueAgeNs);
                               });
            // Keep the stock ns-3.40 public API order: maxRetries, then ceiling.
            device->SetBackoffParams(
                MicroSeconds(1), 1, 1000, config.macRetryLimit, 10);
            if (config.sionnaIpcEnabled)
            {
                Ptr<AmsRadioReceiveErrorModel> receiveError =
                    CreateObject<AmsRadioReceiveErrorModel>();
                receiveError->SetController(&radioController);
                receiveError->SetDeviceId(CanonicalPacketDevice(deviceId));
                device->SetReceiveErrorModel(receiveError);
                queue->SetRadioDecisionSink([&radioController](const std::string& observedDevice,
                                                               Ptr<const Packet> packet) {
                    return radioController.Decide(observedDevice, packet);
                });
                queue->SetRadioTransmitSink(
                    [&radioController](const std::string& observedDevice,
                                       Ptr<const Packet> packet,
                                       Ptr<CsmaNetDevice>) {
                        return radioController.RevalidateForDequeue(observedDevice, packet);
                    },
                    device);
            }
            device->SetQueue(queue);
            globalRadioScheduler.RegisterOwner(deviceId, device, queue);
            device->TraceConnect(
                "PhyTxBegin",
                deviceId,
                MakeBoundCallback(&TraceChannel, &radioController, device, &logger));
            device->TraceConnect(
                "PhyTxDrop",
                deviceId,
                MakeBoundCallback(&TracePhyTxDrop,
                                  &packetGuard,
                                  device,
                                  config.macRetryLimit,
                                  &logger));
            device->TraceConnect("PhyRxDrop",
                                 deviceId,
                                 MakeBoundCallback(&TracePhyRxDrop, &radioController, &logger));
            device->TraceConnect("MacTxBackoff",
                                 deviceId,
                                 MakeBoundCallback(&TraceBackoff, &packetGuard, device, &logger));
            device->TraceConnect("PhyTxEnd",
                                 deviceId,
                                 MakeBoundCallback(&TracePhyTxEnd,
                                                   &packetGuard,
                                                   device,
                                                   config.macRetryLimit,
                                                   &logger));

            routerExternalDevices.Get(index)->TraceConnect(
                "MacRx",
                deviceId,
                MakeBoundCallback(&TraceIngress, &logger));
            Ptr<Ipv4L3Protocol> routerIpv4 = routers.Get(index)->GetObject<Ipv4L3Protocol>();
            if (!routerIpv4)
            {
                throw std::runtime_error("router has no ns-3 Ipv4L3Protocol drop trace");
            }
            routerIpv4->TraceConnect(
                "Drop",
                deviceId,
                MakeBoundCallback(&TraceIpv4Drop, &logger, routerExternalDevices.Get(index)));
            endpointDevices.Get(index)->TraceConnect("MacPromiscRx",
                                                     deviceId,
                                                     MakeBoundCallback(&TraceEgress, &logger));
        }

        for (uint32_t index = 0; index <= config.uavCount; ++index)
        {
            AddPermanentArp(routers.Get(index),
                            routerExternalDevices.Get(index),
                            EndpointIp(index),
                            EndpointMacAddress(index));
            for (uint32_t peer = 0; peer <= config.uavCount; ++peer)
            {
                if (peer != index)
                {
                    AddPermanentArp(routers.Get(index),
                                    radioDevices.Get(index),
                                    RadioIp(peer),
                                    RadioMacAddress(peer));
                }
            }
        }
        Ipv4GlobalRoutingHelper::PopulateRoutingTables();
        radioStates.Start();
        clockDatagrams.Start();

        const Ipv4Address multicastSource(EndpointIp(0).c_str());
        const Ipv4Address multicastGroup("239.71.0.1");
        Ipv4StaticRoutingHelper multicastRouting;
        NetDeviceContainer gcsMulticastOutput;
        gcsMulticastOutput.Add(radioDevices.Get(0));
        multicastRouting.AddMulticastRoute(routers.Get(0),
                                           multicastSource,
                                           multicastGroup,
                                           routerExternalDevices.Get(0),
                                           gcsMulticastOutput);
        for (uint32_t index = 1; index <= config.uavCount; ++index)
        {
            NetDeviceContainer uavMulticastOutput;
            uavMulticastOutput.Add(routerExternalDevices.Get(index));
            multicastRouting.AddMulticastRoute(routers.Get(index),
                                               multicastSource,
                                               multicastGroup,
                                               radioDevices.Get(index),
                                               uavMulticastOutput);
        }

        if (config.selfTest)
        {
            for (uint32_t index = 0; index < endpointDevices.GetN(); ++index)
            {
                endpointDevices.Get(index)->SetReceiveCallback(MakeCallback(&DiscardReceive));
                endpointDevices.Get(index)->SetPromiscReceiveCallback(
                    MakeCallback(&DiscardPromiscReceive));
            }
            ScheduleSelfTest(endpointDevices,
                             config.uavCount,
                             config.selfTestBurst,
                             config.selfTestUnknownTos);
        }
        else
        {
            TapBridgeHelper tapBridge;
            tapBridge.SetAttribute("Mode", StringValue("UseBridge"));
            tapBridge.SetAttribute("DeviceName", StringValue(config.tapGcs));
            tapBridge.Install(endpointGhosts.Get(0), endpointDevices.Get(0));
            for (uint32_t index = 1; index <= config.uavCount; ++index)
            {
                tapBridge.SetAttribute("DeviceName", StringValue(tapUavs[index - 1]));
                tapBridge.Install(endpointGhosts.Get(index), endpointDevices.Get(index));
            }
            Simulator::Schedule(MilliSeconds(100), &PollStopFile, config.stopFile);
        }

        if (!config.pcapPrefix.empty())
        {
            for (uint32_t index = 0; index < radioDevices.GetN(); ++index)
            {
                radio.EnablePcap(config.pcapPrefix + "-radio-" +
                                     (index == 0 ? "gcs" : "uav" + std::to_string(index)) + ".pcap",
                                 radioDevices.Get(index),
                                 true,
                                 true);
            }
        }
        Simulator::Schedule(MilliSeconds(1),
                            &WriteReadyFile,
                            config.readyFile,
                            resolvedHash,
                            config.eventEpoch,
                            config.uavCount);
        Simulator::Stop(MilliSeconds(config.durationMs));
        Simulator::Run();
        logger.Flush();

        bool selfTestPassed = true;
        if (config.selfTest)
        {
            selfTestPassed = tokenBucketSelfTest && deadlineDropSelfTest &&
                             minimumControlHeadroomSelfTest && asymmetricPayloadDemandSelfTest &&
                             profileIdNoBypassSelfTest &&
                             perUavFairnessSelfTest &&
                             retryBoundSelfTest && strictPrioritySelfTest &&
                             globalRadioSchedulerSelfTest && staleGrantSelfTest &&
                             logger.P2mpRootTransmissions() == 1 &&
                             logger.P2mpEgressCount() == config.uavCount &&
                             logger.EventCount("ingress") > 0 && logger.EventCount("admit") > 0 &&
                             logger.EventCount("enqueue") > 0 &&
                             logger.EventCount("dequeue") > 0 && logger.EventCount("channel") > 0 &&
                             logger.EventCount("egress") > 0;
        }
        std::cout << "{\"status\":\"" << (selfTestPassed ? "passed" : "failed")
                  << "\",\"contract\":\"" << CONTRACT << "\",\"config_sha256\":\"" << resolvedHash
                  << "\",\"uav_count\":" << config.uavCount << ",\"seed\":" << config.seed
                  << ",\"run\":" << config.run << ",\"event_epoch\":" << config.eventEpoch
                  << ",\"token_bucket_self_test\":"
                  << (tokenBucketSelfTest ? "true" : "false")
                  << ",\"deadline_drop_self_test\":"
                  << (deadlineDropSelfTest ? "true" : "false")
                  << ",\"minimum_control_headroom_self_test\":"
                  << (minimumControlHeadroomSelfTest ? "true" : "false")
                  << ",\"asymmetric_payload_demand_self_test\":"
                  << (asymmetricPayloadDemandSelfTest ? "true" : "false")
                  << ",\"asymmetric_payload_uav1_max_sustained_admitted_bps\":"
                  << IngressProtectionController::PerUavSustainedAdmissionRateBps(
                         config.payloadAdmissionRateBps,
                         config.uavCount)
                  << ",\"work_conserving_across_idle_uavs\":false"
                  << ",\"profile_id_no_bypass_self_test\":"
                  << (profileIdNoBypassSelfTest ? "true" : "false")
                  << ",\"per_uav_fairness_self_test\":"
                  << (perUavFairnessSelfTest ? "true" : "false")
                  << ",\"retry_bound_self_test\":"
                  << (retryBoundSelfTest ? "true" : "false")
                  << ",\"strict_control_priority_self_test\":"
                  << (strictPrioritySelfTest ? "true" : "false")
                  << ",\"global_radio_scheduler_self_test\":"
                  << (globalRadioSchedulerSelfTest ? "true" : "false")
                  << ",\"stale_grant_self_test\":"
                  << (staleGrantSelfTest ? "true" : "false")
                  << ",\"p2mp_root_transmissions\":" << logger.P2mpRootTransmissions()
                  << ",\"p2mp_egress_devices\":" << logger.P2mpEgressCount() << "}\n";
        Simulator::Destroy();
        return selfTestPassed ? 0 : 3;
    }
    catch (const std::exception& error)
    {
        std::cerr << "FAIL " << error.what() << '\n';
        Simulator::Destroy();
        return 2;
    }
}
