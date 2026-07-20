#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/tap-bridge-module.h"

#include <algorithm>
#include <array>
#include <cerrno>
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
#include <system_error>
#include <sys/socket.h>
#include <sys/un.h>
#include <fcntl.h>
#include <unistd.h>
#include <utility>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsTapPacketEngine");

namespace
{

constexpr const char* CONTRACT = "ams.tap_packet_engine/v1";
constexpr const char* EVENT_SCHEMA = "ams.ns3.packet_event/v1";
constexpr const char* LIFECYCLE_SCHEMA = "ams.ns3.lifecycle/v1";
constexpr uint32_t MAX_UAVS = 5;
constexpr uint32_t MAX_QUEUE_PACKETS = 1000000;
constexpr uint64_t MAX_DURATION_MS = 86400000;
constexpr uint32_t MAX_SIONNA_STATE_CELLS = 64;
constexpr uint32_t MAX_SIONNA_LINE_BYTES = 65536;
constexpr uint32_t MAX_RADIO_LINEAGE_CACHE = 100000;
constexpr uint8_t CONTROL_TOS = 184;
constexpr uint8_t PAYLOAD_TOS = 40;
constexpr uint8_t ADDITIONAL_DATA_TOS = 0;

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
};

FrameMetadata
InspectFrame(Ptr<const Packet> packet)
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
    if (metadata.tos == CONTROL_TOS)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "control";
    }
    else if (metadata.tos == PAYLOAD_TOS)
    {
        metadata.classMapped = true;
        metadata.trafficClass = "payload";
    }
    else if (metadata.tos == ADDITIONAL_DATA_TOS)
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
                metadata.transportPayloadSha256 = PacketSha256(copy);
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
                    uint32_t maxStateTtlMs)
        : m_enabled(enabled),
          m_path(std::move(path)),
          m_pollIntervalMs(pollIntervalMs),
          m_maxUpdatesPerPoll(maxUpdatesPerPoll),
          m_maxStateTtlNs(static_cast<uint64_t>(maxStateTtlMs) * 1000000ULL)
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
                    std::ifstream input(m_path, std::ios::in);
                    if (!input)
                    {
                        FailClosed("state_ipc_open_failed");
                    }
                    else
                    {
                        input.seekg(static_cast<std::streamoff>(m_offset));
                        std::string line;
                        uint32_t processed = 0;
                        while (processed < m_maxUpdatesPerPoll && std::getline(input, line))
                        {
                            m_offset += line.size() + 1;
                            ++processed;
                            if (line.size() > MAX_SIONNA_LINE_BYTES)
                            {
                                FailClosed("state_ipc_line_too_large");
                                break;
                            }
                            if (!ApplyLine(line))
                            {
                                FailClosed("state_ipc_invalid_record");
                                break;
                            }
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
             *serviceRate != 20000000))
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
                                          const std::string&)>;

class BoundedPriorityScheduler
{
  public:
    static constexpr uint32_t CONTROL_CLASS = 0;
    static constexpr uint32_t PAYLOAD_CLASS = 1;
    static constexpr uint32_t ADDITIONAL_DATA_CLASS = 2;
    static constexpr uint32_t NO_CLASS = 3;

    // Eight packets preserve a strong control preference without permitting
    // an always-backlogged control stream to starve the other two classes.
    // Under three-class saturation one lower-class packet is selected after
    // every bounded control burst, and the two lower classes alternate.
    static constexpr uint32_t CONTROL_BURST_LIMIT = 8;

    uint32_t Select(const std::array<uint32_t, 3>& counts) const
    {
        const bool lowerWaiting = counts[PAYLOAD_CLASS] > 0 ||
                                  counts[ADDITIONAL_DATA_CLASS] > 0;
        if (counts[CONTROL_CLASS] > 0 &&
            (!lowerWaiting || m_controlBurst < CONTROL_BURST_LIMIT))
        {
            return CONTROL_CLASS;
        }
        if (lowerWaiting)
        {
            if (counts[m_nextLowerClass] > 0)
            {
                return m_nextLowerClass;
            }
            return m_nextLowerClass == PAYLOAD_CLASS ? ADDITIONAL_DATA_CLASS : PAYLOAD_CLASS;
        }
        return counts[CONTROL_CLASS] > 0 ? CONTROL_CLASS : NO_CLASS;
    }

    void Record(uint32_t selectedClass, const std::array<uint32_t, 3>& countsAfter)
    {
        if (selectedClass == CONTROL_CLASS)
        {
            const bool lowerWaiting = countsAfter[PAYLOAD_CLASS] > 0 ||
                                      countsAfter[ADDITIONAL_DATA_CLASS] > 0;
            m_controlBurst = lowerWaiting ? m_controlBurst + 1 : 0;
            return;
        }
        if (selectedClass == PAYLOAD_CLASS || selectedClass == ADDITIONAL_DATA_CLASS)
        {
            m_controlBurst = 0;
            m_nextLowerClass = selectedClass == PAYLOAD_CLASS ? ADDITIONAL_DATA_CLASS
                                                               : PAYLOAD_CLASS;
        }
    }

    void Reset()
    {
        m_controlBurst = 0;
        m_nextLowerClass = PAYLOAD_CLASS;
    }

    static bool DeterministicSelfTest();

  private:
    uint32_t m_controlBurst = 0;
    uint32_t m_nextLowerClass = PAYLOAD_CLASS;
};

bool
BoundedPriorityScheduler::DeterministicSelfTest()
{
    const auto drain = [](std::array<uint32_t, 3> counts) {
        BoundedPriorityScheduler scheduler;
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

    const std::vector<uint32_t> saturatedExpected = {
        0, 0, 0, 0, 0, 0, 0, 0, 1,
        0, 0, 0, 0, 0, 0, 0, 0, 2,
        0, 1, 2,
    };
    if (drain({17, 2, 2}) != saturatedExpected ||
        drain({0, 2, 2}) != std::vector<uint32_t>({1, 2, 1, 2}) ||
        drain({9, 0, 2}) !=
            std::vector<uint32_t>({0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 2}) ||
        drain({1, 0, 0}) != std::vector<uint32_t>({0}))
    {
        return false;
    }

    // Uncontended control traffic does not consume the contested burst: when
    // a lower-class packet arrives later, control still receives immediate
    // priority before the newly bounded burst begins.
    BoundedPriorityScheduler scheduler;
    std::array<uint32_t, 3> counts = {2, 0, 0};
    if (scheduler.Select(counts) != CONTROL_CLASS)
    {
        return false;
    }
    --counts[CONTROL_CLASS];
    scheduler.Record(CONTROL_CLASS, counts);
    counts[PAYLOAD_CLASS] = 1;
    return scheduler.Select(counts) == CONTROL_CLASS;
}

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
        m_limits = {control, payload, additionalData};
        m_scheduler.Reset();
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

    void SetRadioTransmitSink(RadioTransmitSink sink, Ptr<CsmaNetDevice> device)
    {
        m_radioTransmitSink = std::move(sink);
        m_radioDevice = std::move(device);
    }

    struct Depths
    {
        uint32_t control = 0;
        uint32_t payload = 0;
        uint32_t additionalData = 0;
        uint64_t total = 0;
    };

    struct FlushResult
    {
        Depths before;
        Depths after;
        uint64_t flushedPackets = 0;
    };

    Depths ActualDepths() const
    {
        const uint64_t total = static_cast<uint64_t>(m_counts[BoundedPriorityScheduler::CONTROL_CLASS]) +
                               static_cast<uint64_t>(m_counts[BoundedPriorityScheduler::PAYLOAD_CLASS]) +
                               static_cast<uint64_t>(
                                   m_counts[BoundedPriorityScheduler::ADDITIONAL_DATA_CLASS]);
        if (GetContainer().size() != total)
        {
            throw std::runtime_error("three-class queue depth accounting diverged");
        }
        return {m_counts[BoundedPriorityScheduler::CONTROL_CLASS],
                m_counts[BoundedPriorityScheduler::PAYLOAD_CLASS],
                m_counts[BoundedPriorityScheduler::ADDITIONAL_DATA_CLASS],
                total};
    }

    FlushResult FlushForLifecycleStop()
    {
        FlushResult result;
        result.before = ActualDepths();
        // Queue::Flush() deliberately dispatches through our virtual Remove(),
        // so each dropped packet preserves the queue_flush packet-event trail
        // and all three class counters are decremented by the real queue code.
        Flush();
        result.after = ActualDepths();
        result.flushedPackets = result.before.total - result.after.total;
        if (result.after.total != 0 || result.flushedPackets != result.before.total)
        {
            throw std::runtime_error("three-class queue did not reach an empty terminal state");
        }
        return result;
    }

    bool Enqueue(Ptr<Packet> packet) override
    {
        const FrameMetadata metadata = InspectFrame(packet);
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

        auto position = GetContainer().begin();
        while (position != GetContainer().end())
        {
            if (ClassIndex(InspectFrame(*position)) > classIndex)
            {
                break;
            }
            ++position;
        }
        if (!DoEnqueue(position, packet))
        {
            Emit("drop",
                 packet,
                 m_counts[classIndex],
                 m_limits[classIndex],
                 "aggregate_queue_limit");
            return false;
        }
        ++m_counts[classIndex];
        Emit("enqueue", packet, m_counts[classIndex], m_limits[classIndex], "");
        return true;
    }

    Ptr<Packet> Dequeue() override
    {
        while (!GetContainer().empty())
        {
            const auto selected = SelectNextIterator();
            NS_ASSERT(selected != GetContainer().end());
            Ptr<const Packet> candidate = *selected;
            const uint32_t classIndex = ClassIndex(InspectFrame(candidate));
            RadioDecision transmitDecision;
            if (m_radioTransmitSink)
            {
                // Revalidate the enqueue-time packet decision immediately before
                // transmission.  A queued packet can never hold an expired state
                // or inherit a newer cell state that was not its causal decision.
                transmitDecision = m_radioTransmitSink(m_deviceId, candidate, m_radioDevice);
            }
            Ptr<Packet> packet = DoDequeue(selected);
            if (!packet || classIndex >= m_counts.size())
            {
                return packet;
            }
            NS_ASSERT(m_counts[classIndex] > 0);
            --m_counts[classIndex];
            m_scheduler.Record(classIndex, m_counts);
            if (transmitDecision.drop)
            {
                DropAfterDequeue(packet);
                Emit("drop",
                     packet,
                     m_counts[classIndex],
                     m_limits[classIndex],
                     transmitDecision.dropReason);
                if (GetContainer().empty() && m_radioDevice)
                {
                    // Queue::Dequeue must return a packet after the caller saw
                    // IsEmpty()==false.  Return the final already-accounted
                    // drop as a sentinel while disabling the send side for
                    // this call stack; CsmaNetDevice emits PhyTxDrop and never
                    // invokes CsmaChannel::TransmitStart.  Re-enable at the
                    // next simulator event before any future ingress.
                    m_radioDevice->SetSendEnable(false);
                    Simulator::ScheduleNow(&AmsThreeClassQueue::EnsureSendEnabled, m_radioDevice);
                    return packet;
                }
                continue;
            }
            Emit("dequeue", packet, m_counts[classIndex], m_limits[classIndex], "");
            return packet;
        }
        return nullptr;
    }

    Ptr<Packet> Remove() override
    {
        if (GetContainer().empty())
        {
            return nullptr;
        }
        const auto selected = SelectNextIterator();
        NS_ASSERT(selected != GetContainer().end());
        Ptr<const Packet> candidate = *selected;
        const uint32_t classIndex = ClassIndex(InspectFrame(candidate));
        Ptr<Packet> packet = DoRemove(selected);
        if (packet && classIndex < m_counts.size())
        {
            NS_ASSERT(m_counts[classIndex] > 0);
            --m_counts[classIndex];
            m_scheduler.Record(classIndex, m_counts);
            Emit("drop", packet, m_counts[classIndex], m_limits[classIndex], "queue_flush");
        }
        return packet;
    }

    Ptr<const Packet> Peek() const override
    {
        const auto selected = SelectNextIterator();
        return selected == GetContainer().end() ? nullptr : DoPeek(selected);
    }

  private:
    ConstIterator SelectNextIterator() const
    {
        const uint32_t selectedClass = m_scheduler.Select(m_counts);
        if (selectedClass == BoundedPriorityScheduler::NO_CLASS)
        {
            return GetContainer().end();
        }
        for (auto position = GetContainer().begin(); position != GetContainer().end(); ++position)
        {
            if (ClassIndex(InspectFrame(*position)) == selectedClass)
            {
                return position;
            }
        }
        NS_ASSERT_MSG(false, "scheduler selected an empty traffic class");
        return GetContainer().end();
    }

    static void EnsureSendEnabled(Ptr<CsmaNetDevice> device)
    {
        if (device && !device->IsSendEnabled())
        {
            device->SetSendEnable(true);
        }
    }

    void Emit(const std::string& event,
              Ptr<const Packet> packet,
              int64_t depth,
              int64_t limit,
              const std::string& reason)
    {
        if (m_sink)
        {
            m_sink(event, m_deviceId, packet, depth, limit, reason);
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
    BoundedPriorityScheduler m_scheduler;
    std::string m_deviceId;
    QueueEventSink m_sink;
    RadioDecisionSink m_radioDecisionSink;
    RadioTransmitSink m_radioTransmitSink;
    Ptr<CsmaNetDevice> m_radioDevice;
};

struct EngineConfig
{
    uint32_t uavCount = 1;
    std::string tapGcs = "tap-gcs";
    std::string tapUavs;
    uint64_t durationMs = 3600000;
    std::string radioRate = "20000000bps";
    std::string radioDelay = "2ms";
    uint32_t queueControlMaxPackets = 256;
    uint32_t queuePayloadMaxPackets = 128;
    uint32_t queueAdditionalDataMaxPackets = 128;
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
    // This is a runtime evidence sink, deliberately excluded from CanonicalConfig:
    // changing its pathname must not change simulated packet behavior.
    std::string lifecycleFile;
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
                      RadioController* radioController)
        : m_output(path, std::ios::out | std::ios::trunc),
          m_eventEpoch(eventEpoch),
          m_configHash(std::move(configHash)),
          m_seed(seed),
          m_run(run),
          m_uavCount(uavCount),
          m_radioController(radioController)
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
             const std::string& reason = "")
    {
        const FrameMetadata metadata = InspectFrame(packet);
        const uint64_t hostMonotonicNs = SteadyNowNs();
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
            m_output << ",\"host_monotonic_ns\":" << hostMonotonicNs
                     << ",\"host_clock_domain\":\"host-monotonic\"";
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
        m_output.flush();

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
    uint64_t m_sequence = 0;
    uint64_t m_p2mpRootTransmissions = 0;
    std::set<std::string> m_p2mpEgressDevices;
    std::map<std::string, uint64_t> m_eventCounts;
};

void
TraceIngress(PacketEventLogger* logger, std::string context, Ptr<const Packet> packet)
{
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
TracePhyTxDrop(RadioController* radioController,
               Ptr<CsmaNetDevice> sourceDevice,
               PacketEventLogger* logger,
               std::string context,
               Ptr<const Packet> packet)
{
    const auto decision = radioController ? radioController->DecisionFor(packet) : std::nullopt;
    if (decision && decision->drop && sourceDevice && !sourceDevice->IsSendEnabled())
    {
        // This is the synchronous confirmation of the queue sentinel drop.
        // The queue already emitted the single causal terminal event.
        sourceDevice->SetSendEnable(true);
        return;
    }
    logger->Log("drop", context, packet, -1, -1, "phy_tx_drop");
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
    if (frame.tos == CONTROL_TOS)
    {
        base = 14600;
    }
    else if (frame.tos == PAYLOAD_TOS)
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
    const std::array<uint8_t, 3> tosValues = {CONTROL_TOS, PAYLOAD_TOS, ADDITIONAL_DATA_TOS};
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
                                      ADDITIONAL_DATA_TOS,
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
                                      CONTROL_TOS,
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
                                      ADDITIONAL_DATA_TOS,
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
                                          PAYLOAD_TOS,
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
WriteAllOrThrow(int descriptor, const std::string& payload)
{
    std::size_t offset = 0;
    while (offset < payload.size())
    {
        const ssize_t written =
            ::write(descriptor, payload.data() + offset, payload.size() - offset);
        if (written < 0 && errno == EINTR)
        {
            continue;
        }
        if (written <= 0)
        {
            throw std::runtime_error("cannot append lifecycle JSONL: " +
                                     std::string(std::strerror(errno)));
        }
        offset += static_cast<std::size_t>(written);
    }
}

class LifecycleLogger
{
  public:
    LifecycleLogger(const std::string& path, uint64_t eventEpoch, std::string configHash)
        : m_eventEpoch(eventEpoch),
          m_configHash(std::move(configHash))
    {
        if (path.empty())
        {
            throw std::runtime_error("lifecycleFile must not be empty");
        }
        const std::filesystem::path outputPath(path);
        const std::filesystem::path parent =
            outputPath.parent_path().empty() ? std::filesystem::path(".") : outputPath.parent_path();
        std::error_code directoryError;
        std::filesystem::create_directories(parent, directoryError);
        if (directoryError)
        {
            throw std::runtime_error("cannot create lifecycle directory " + parent.string() + ": " +
                                     directoryError.message());
        }

        int flags = O_WRONLY | O_CREAT | O_EXCL | O_APPEND | O_CLOEXEC;
#ifdef O_NOFOLLOW
        flags |= O_NOFOLLOW;
#endif
        const int descriptor = ::open(outputPath.c_str(), flags, 0640);
        if (descriptor < 0)
        {
            throw std::runtime_error("cannot exclusively create lifecycle JSONL " + path + ": " +
                                     std::string(std::strerror(errno)));
        }
        const int directoryDescriptor =
            ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (directoryDescriptor < 0)
        {
            const int error = errno;
            ::close(descriptor);
            throw std::runtime_error("cannot open lifecycle directory " + parent.string() + ": " +
                                     std::string(std::strerror(error)));
        }
        if (::fsync(directoryDescriptor) != 0)
        {
            const int error = errno;
            ::close(directoryDescriptor);
            ::close(descriptor);
            throw std::runtime_error("cannot fsync lifecycle directory " + parent.string() + ": " +
                                     std::string(std::strerror(error)));
        }
        ::close(directoryDescriptor);
        m_descriptor = descriptor;
    }

    ~LifecycleLogger()
    {
        if (m_descriptor >= 0)
        {
            ::close(m_descriptor);
        }
    }

    LifecycleLogger(const LifecycleLogger&) = delete;
    LifecycleLogger& operator=(const LifecycleLogger&) = delete;

    void Emit(const std::string& event, const std::string& details = "")
    {
        if (event.empty())
        {
            throw std::runtime_error("lifecycle event must not be empty");
        }
        std::ostringstream record;
        record << "{\"schema\":\"" << LIFECYCLE_SCHEMA << "\",\"event\":\""
               << JsonEscape(event) << "\",\"event_sequence\":" << ++m_sequence
               << ",\"event_epoch\":" << m_eventEpoch << ",\"config_sha256\":\""
               << m_configHash << "\",\"host_monotonic_ns\":" << SteadyNowNs()
               << ",\"sim_time_ns\":" << Simulator::Now().GetNanoSeconds();
        if (!details.empty())
        {
            record << ',' << details;
        }
        record << "}\n";
        WriteAllOrThrow(m_descriptor, record.str());
        Sync();
    }

    void Sync() const
    {
        if (m_descriptor < 0 || ::fsync(m_descriptor) != 0)
        {
            throw std::runtime_error("cannot fsync lifecycle JSONL: " +
                                     std::string(std::strerror(errno)));
        }
    }

  private:
    int m_descriptor = -1;
    uint64_t m_eventEpoch;
    std::string m_configHash;
    uint64_t m_sequence = 0;
};

struct RegisteredQueue
{
    std::string deviceId;
    Ptr<AmsThreeClassQueue> queue;
};

std::string
JsonDepths(const AmsThreeClassQueue::Depths& depths)
{
    std::ostringstream out;
    out << "{\"control_packets\":" << depths.control << ",\"payload_packets\":"
        << depths.payload << ",\"additional_data_packets\":" << depths.additionalData
        << ",\"total_packets\":" << depths.total << '}';
    return out.str();
}

class LifecycleController
{
  public:
    LifecycleController(LifecycleLogger& logger, const std::vector<RegisteredQueue>& queues)
        : m_logger(logger),
          m_queues(queues)
    {
        if (m_queues.empty())
        {
            throw std::runtime_error("lifecycle queue registry must not be empty");
        }
    }

    void MarkReady()
    {
        if (m_ready)
        {
            throw std::runtime_error("duplicate lifecycle ready transition");
        }
        m_ready = true;
        m_logger.Emit("ready",
                      "\"registered_queue_count\":" + std::to_string(m_queues.size()));
    }

    void ObserveStop(const std::string& reason)
    {
        if (m_stopping)
        {
            return;
        }
        if (!m_ready)
        {
            throw std::runtime_error("lifecycle stop observed before ready");
        }
        m_stopping = true;
        m_logger.Emit("stop_observed", "\"stop_reason\":\"" + JsonEscape(reason) + "\"");

        std::ostringstream terminal;
        terminal << "\"stop_reason\":\"" << JsonEscape(reason)
                 << "\",\"queues\":[";
        bool allQueuesEmpty = true;
        for (std::size_t index = 0; index < m_queues.size(); ++index)
        {
            const RegisteredQueue& registration = m_queues[index];
            if (!registration.queue)
            {
                throw std::runtime_error("lifecycle queue registry contains a null queue");
            }
            const AmsThreeClassQueue::FlushResult result = registration.queue->FlushForLifecycleStop();
            allQueuesEmpty = allQueuesEmpty && result.after.total == 0;
            if (index > 0)
            {
                terminal << ',';
            }
            terminal << "{\"device_id\":\"" << JsonEscape(registration.deviceId)
                     << "\",\"before_depths\":" << JsonDepths(result.before)
                     << ",\"after_depths\":" << JsonDepths(result.after)
                     << ",\"flushed_packets\":" << result.flushedPackets << '}';
        }
        terminal << "],\"all_queues_empty\":" << (allQueuesEmpty ? "true" : "false");
        m_logger.Emit("queues_terminal", terminal.str());
        if (!allQueuesEmpty)
        {
            throw std::runtime_error("lifecycle queues did not reach an empty terminal state");
        }
        // Every lifecycle record is fsynced by Emit.  Keep this explicit at
        // the stop boundary: durable terminal evidence precedes Simulator::Stop.
        m_logger.Sync();
        Simulator::Stop();
        m_logger.Emit("stopped", "\"stop_reason\":\"" + JsonEscape(reason) + "\"");
        m_logger.Sync();
    }

  private:
    LifecycleLogger& m_logger;
    const std::vector<RegisteredQueue>& m_queues;
    bool m_ready = false;
    bool m_stopping = false;
};

void
PollStopFile(const std::string& stopFile, LifecycleController* lifecycle)
{
    if (!stopFile.empty() && std::ifstream(stopFile).good())
    {
        lifecycle->ObserveStop("stop_file");
        return;
    }
    Simulator::Schedule(MilliSeconds(100), &PollStopFile, stopFile, lifecycle);
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
           << "}\n";
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
    command.AddValue("lifecycleFile",
                     "Required append-only raw lifecycle JSONL evidence output",
                     config.lifecycleFile);
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
    if (config.lifecycleFile.empty())
    {
        std::cerr << "FAIL lifecycleFile must not be empty\n";
        return 2;
    }

    try
    {
        if (config.selfTest && !BoundedPriorityScheduler::DeterministicSelfTest())
        {
            throw std::runtime_error("bounded-priority scheduler self-test failed");
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
                                    config.sionnaMaxStateTtlMs);
        RadioController radioController(config.sionnaIpcEnabled,
                                        &radioStates,
                                        config.sionnaIntervention);
        LifecycleLogger lifecycleLogger(config.lifecycleFile, config.eventEpoch, resolvedHash);
        PacketEventLogger logger(config.eventsFile,
                                 config.eventEpoch,
                                 resolvedHash,
                                 config.seed,
                                 config.run,
                                 config.uavCount,
                                 &radioController);
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
        std::vector<RegisteredQueue> queueRegistry;
        queueRegistry.reserve(radioDevices.GetN());
        if (radioDevices.GetN() > 0)
        {
            radioController.SetChannel(DynamicCast<CsmaChannel>(radioDevices.Get(0)->GetChannel()));
        }

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
            queue->SetLimits(config.queueControlMaxPackets,
                             config.queuePayloadMaxPackets,
                             config.queueAdditionalDataMaxPackets);
            queue->SetIdentity(deviceId,
                               [&logger](const std::string& event,
                                         const std::string& observedDevice,
                                         Ptr<const Packet> packet,
                                         int64_t depth,
                                         int64_t limit,
                                         const std::string& reason) {
                                   logger.Log(event, observedDevice, packet, depth, limit, reason);
                               });
            if (config.sionnaIpcEnabled)
            {
                // Service-rate padding can keep the shared medium busy much
                // longer than the base 20 Mbps frame time.  A bounded packet
                // must wait for that factual occupancy instead of being lost
                // to the legacy retry cap before its queue decision is reached.
                device->SetBackoffParams(MicroSeconds(1), 1, 1000, 10, 1000000);
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
            queueRegistry.push_back({deviceId, queue});
            device->TraceConnect(
                "PhyTxBegin",
                deviceId,
                MakeBoundCallback(&TraceChannel, &radioController, device, &logger));
            device->TraceConnect(
                "PhyTxDrop",
                deviceId,
                MakeBoundCallback(&TracePhyTxDrop, &radioController, device, &logger));
            device->TraceConnect("PhyRxDrop",
                                 deviceId,
                                 MakeBoundCallback(&TracePhyRxDrop, &radioController, &logger));

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
        LifecycleController lifecycle(lifecycleLogger, queueRegistry);

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
        }

        if (!config.stopFile.empty())
        {
            Simulator::Schedule(MilliSeconds(100),
                                &PollStopFile,
                                config.stopFile,
                                &lifecycle);
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
        // The raw lifecycle ready record is durable before the compatibility
        // ready marker can let an external orchestration phase proceed.
        Simulator::Schedule(MilliSeconds(1), &LifecycleController::MarkReady, &lifecycle);
        Simulator::Schedule(MilliSeconds(1),
                            &WriteReadyFile,
                            config.readyFile,
                            resolvedHash,
                            config.eventEpoch,
                            config.uavCount);
        Simulator::Schedule(MilliSeconds(config.durationMs),
                            &LifecycleController::ObserveStop,
                            &lifecycle,
                            std::string("duration"));
        Simulator::Run();

        bool selfTestPassed = true;
        if (config.selfTest)
        {
            selfTestPassed = logger.P2mpRootTransmissions() == 1 &&
                             logger.P2mpEgressCount() == config.uavCount &&
                             logger.EventCount("ingress") > 0 && logger.EventCount("enqueue") > 0 &&
                             logger.EventCount("dequeue") > 0 && logger.EventCount("channel") > 0 &&
                             logger.EventCount("egress") > 0;
        }
        std::cout << "{\"status\":\"" << (selfTestPassed ? "passed" : "failed")
                  << "\",\"contract\":\"" << CONTRACT << "\",\"config_sha256\":\"" << resolvedHash
                  << "\",\"uav_count\":" << config.uavCount << ",\"seed\":" << config.seed
                  << ",\"run\":" << config.run << ",\"event_epoch\":" << config.eventEpoch
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
