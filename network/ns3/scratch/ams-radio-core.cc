#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/error-model.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/network-module.h"
#include "ns3/traffic-control-module.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <regex>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsRadioCore");

namespace
{

struct NodeSpec
{
  std::string id;
  std::string role;
  double x = 0.0;
  double y = 0.0;
  double z = 0.0;
  std::string antenna = "omni";
};

struct TrafficClassSpec
{
  std::string name;
  uint32_t priority = 2;
  uint64_t targetBps = 1000;
  uint32_t deadlineMs = 1000;
  uint64_t offeredBps = 1000;
  uint32_t tos = 0;
};

struct LinkSpec
{
  std::string tx;
  std::string rx;
  std::string trafficClass;
};

struct EmitterSpec
{
  std::string id;
  bool enabled = false;
  double x = 0.0;
  double y = 0.0;
  double z = 0.0;
  uint64_t centerHz = 2400000000ULL;
  uint64_t bandwidthHz = 1000000ULL;
  double powerDbm = 40.0;
  double dutyCycle = 1.0;
};

struct LinkState
{
  std::string tx;
  std::string rx;
  std::string trafficClass;
  double pathlossDb = 0.0;
  double rssiDbm = -120.0;
  double sinrDb = 0.0;
  double jsDb = -100.0;
  uint64_t serviceTierBps = 1000;
  double perInput = 0.0;
  std::string linkState = "unknown";
  bool stale = false;
  std::string source = "unknown";
};

struct CoreConfig
{
  std::string runId = "manual";
  std::string runDir = "runs/manual";
  double durationS = 20.0;
  uint64_t channelRateBps = 1000000;
  double channelDelayMs = 2.0;
  uint32_t queueMaxPackets = 100;
  std::string packetCoreMode = "csma_surrogate";
  std::string packetCoreStatus = "implemented_current_p0_surrogate";
  bool packetCoreRuntimeSelectable = true;
  std::string packetCoreSharedMediumModel = "csma";
  std::string packetCoreFidelityNote = "csma_surrogate_not_customer_modem_waveform";
  std::string sionnaHost = "127.0.0.1";
  uint16_t sionnaPort = 5090;
  uint32_t sionnaDeadlineMs = 50;
  double sionnaQueryPeriodS = 1.0;
  std::string nodeStateFile;
  bool allowMockSionna = false;
  uint64_t carrierHz = 2400000000ULL;
  uint64_t bandwidthHz = 1000000ULL;
  double txPowerDbm = 33.0;
  double noiseFigureDb = 7.0;
  std::vector<NodeSpec> nodes;
  std::vector<TrafficClassSpec> trafficClasses;
  std::vector<LinkSpec> links;
  std::vector<EmitterSpec> emitters;
};

struct FlowSpec
{
  uint16_t port = 0;
  std::string tx;
  std::string rx;
  std::string trafficClass;
  uint64_t dataRateBps = 1000;
  double perInput = 0.0;
};

struct RadioStats
{
  bool usedMockSionna = false;
  uint32_t sionnaQueries = 0;
  uint32_t staleSionnaQueries = 0;
  double minSinr = std::numeric_limits<double>::infinity();
  double maxJs = -std::numeric_limits<double>::infinity();
};

std::string
Trim(const std::string& input)
{
  const auto begin = input.find_first_not_of(" \t\r\n");
  if (begin == std::string::npos)
    {
      return "";
    }
  const auto end = input.find_last_not_of(" \t\r\n");
  return input.substr(begin, end - begin + 1);
}

bool
StringToBool(const std::string& value)
{
  return value == "1" || value == "true" || value == "True" || value == "yes";
}

std::string
JsonEscape(const std::string& value)
{
  std::ostringstream out;
  for (char c : value)
    {
      switch (c)
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
          out << c;
          break;
        }
    }
  return out.str();
}

std::string
LinkKey(const std::string& tx, const std::string& rx, const std::string& trafficClass)
{
  return tx + ">" + rx + ":" + trafficClass;
}

const NodeSpec*
FindNode(const CoreConfig& config, const std::string& id)
{
  for (const auto& node : config.nodes)
    {
      if (node.id == id)
        {
          return &node;
        }
    }
  return nullptr;
}

const TrafficClassSpec*
FindTrafficClass(const CoreConfig& config, const std::string& name)
{
  for (const auto& trafficClass : config.trafficClasses)
    {
      if (trafficClass.name == name)
        {
          return &trafficClass;
        }
    }
  return nullptr;
}

double
DistanceM(const NodeSpec& a, const NodeSpec& b)
{
  const double dx = a.x - b.x;
  const double dy = a.y - b.y;
  const double dz = a.z - b.z;
  return std::sqrt(dx * dx + dy * dy + dz * dz);
}

double
DistanceM(const EmitterSpec& a, const NodeSpec& b)
{
  const double dx = a.x - b.x;
  const double dy = a.y - b.y;
  const double dz = a.z - b.z;
  return std::sqrt(dx * dx + dy * dy + dz * dz);
}

double
DbmToMw(double dbm)
{
  return std::pow(10.0, dbm / 10.0);
}

double
MwToDbm(double mw)
{
  if (mw <= 0.0)
    {
      return -200.0;
    }
  return 10.0 * std::log10(mw);
}

double
Clamp01(double value)
{
  return std::max(0.0, std::min(1.0, value));
}

double
FreeSpacePathlossDb(uint64_t carrierHz, double distanceM)
{
  const double distanceKm = std::max(distanceM / 1000.0, 0.001);
  const double frequencyMhz = static_cast<double>(carrierHz) / 1000000.0;
  return 32.44 + 20.0 * std::log10(frequencyMhz) + 20.0 * std::log10(distanceKm);
}

CoreConfig
ReadTopology(const std::string& path)
{
  std::ifstream in(path);
  if (!in)
    {
      NS_FATAL_ERROR("Unable to open ns-3 topology file: " << path);
    }

  CoreConfig config;
  std::string line;
  while (std::getline(in, line))
    {
      line = Trim(line);
      if (line.empty() || line[0] == '#')
        {
          continue;
        }

      std::istringstream iss(line);
      std::string key;
      iss >> key;

      if (key == "run_id")
        {
          iss >> config.runId;
        }
      else if (key == "run_dir")
        {
          iss >> config.runDir;
        }
      else if (key == "duration_s")
        {
          iss >> config.durationS;
        }
      else if (key == "channel_rate_bps")
        {
          iss >> config.channelRateBps;
        }
      else if (key == "channel_delay_ms")
        {
          iss >> config.channelDelayMs;
        }
      else if (key == "queue_max_packets")
        {
          iss >> config.queueMaxPackets;
        }
      else if (key == "packet_core_mode")
        {
          iss >> config.packetCoreMode;
        }
      else if (key == "packet_core_status")
        {
          iss >> config.packetCoreStatus;
        }
      else if (key == "packet_core_runtime_selectable")
        {
          std::string value;
          iss >> value;
          config.packetCoreRuntimeSelectable = StringToBool(value);
        }
      else if (key == "packet_core_shared_medium_model")
        {
          iss >> config.packetCoreSharedMediumModel;
        }
      else if (key == "packet_core_fidelity_note")
        {
          iss >> config.packetCoreFidelityNote;
        }
      else if (key == "sionna_host")
        {
          iss >> config.sionnaHost;
        }
      else if (key == "sionna_port")
        {
          iss >> config.sionnaPort;
        }
      else if (key == "sionna_deadline_ms")
        {
          iss >> config.sionnaDeadlineMs;
        }
      else if (key == "sionna_query_period_s")
        {
          iss >> config.sionnaQueryPeriodS;
        }
      else if (key == "node_state_file")
        {
          iss >> config.nodeStateFile;
        }
      else if (key == "allow_mock_sionna")
        {
          std::string value;
          iss >> value;
          config.allowMockSionna = StringToBool(value);
        }
      else if (key == "carrier_hz")
        {
          iss >> config.carrierHz;
        }
      else if (key == "bandwidth_hz")
        {
          iss >> config.bandwidthHz;
        }
      else if (key == "tx_power_dbm")
        {
          iss >> config.txPowerDbm;
        }
      else if (key == "noise_figure_db")
        {
          iss >> config.noiseFigureDb;
        }
      else if (key == "node")
        {
          NodeSpec node;
          iss >> node.id >> node.role >> node.x >> node.y >> node.z >> node.antenna;
          config.nodes.push_back(node);
        }
      else if (key == "traffic_class")
        {
          TrafficClassSpec trafficClass;
          iss >> trafficClass.name >> trafficClass.priority >> trafficClass.targetBps >>
              trafficClass.deadlineMs >> trafficClass.offeredBps >> trafficClass.tos;
          config.trafficClasses.push_back(trafficClass);
        }
      else if (key == "link")
        {
          LinkSpec link;
          iss >> link.tx >> link.rx >> link.trafficClass;
          config.links.push_back(link);
        }
      else if (key == "emitter")
        {
          EmitterSpec emitter;
          std::string enabled;
          iss >> emitter.id >> enabled >> emitter.x >> emitter.y >> emitter.z >> emitter.centerHz >>
              emitter.bandwidthHz >> emitter.powerDbm >> emitter.dutyCycle;
          emitter.enabled = StringToBool(enabled);
          config.emitters.push_back(emitter);
        }
      else
        {
          NS_LOG_WARN("Ignoring unknown topology directive: " << key);
        }
    }

  if (config.nodes.size() != 6)
    {
      NS_FATAL_ERROR("Expected 6 nodes in topology, found " << config.nodes.size());
    }
  if (config.links.empty())
    {
      NS_FATAL_ERROR("Topology has no links");
    }
  return config;
}

std::string
BuildSionnaRequest(const CoreConfig& config)
{
  std::ostringstream out;
  out << std::fixed << std::setprecision(6);
  out << "{\"type\":\"link_query\",";
  out << "\"time_s\":" << Simulator::Now().GetSeconds() << ",";
  out << "\"deadline_ms\":" << config.sionnaDeadlineMs << ",";
  out << "\"radio\":{\"carrier_hz\":" << config.carrierHz << ",\"bandwidth_hz\":"
      << config.bandwidthHz << ",\"tx_power_dbm\":" << config.txPowerDbm << "},";

  out << "\"nodes\":[";
  for (size_t i = 0; i < config.nodes.size(); ++i)
    {
      const auto& node = config.nodes[i];
      if (i > 0)
        {
          out << ",";
        }
      out << "{\"id\":\"" << JsonEscape(node.id) << "\",\"role\":\"" << JsonEscape(node.role)
          << "\",\"position_m\":[" << node.x << "," << node.y << "," << node.z
          << "],\"orientation_quat_xyzw\":[0.0,0.0,0.0,1.0],\"antenna\":\""
          << JsonEscape(node.antenna) << "\"}";
    }
  out << "],";

  out << "\"emitters\":[";
  bool firstEmitter = true;
  for (const auto& emitter : config.emitters)
    {
      if (!emitter.enabled)
        {
          continue;
        }
      if (!firstEmitter)
        {
          out << ",";
        }
      firstEmitter = false;
      out << "{\"id\":\"" << JsonEscape(emitter.id) << "\",\"position_m\":[" << emitter.x
          << "," << emitter.y << "," << emitter.z << "],\"center_hz\":" << emitter.centerHz
          << ",\"bandwidth_hz\":" << emitter.bandwidthHz << ",\"power_dbm\":"
          << emitter.powerDbm << ",\"duty_cycle\":" << emitter.dutyCycle << "}";
    }
  out << "],";

  out << "\"links\":[";
  for (size_t i = 0; i < config.links.size(); ++i)
    {
      const auto& link = config.links[i];
      if (i > 0)
        {
          out << ",";
        }
      out << "{\"tx\":\"" << JsonEscape(link.tx) << "\",\"rx\":\"" << JsonEscape(link.rx)
          << "\",\"traffic_class\":\"" << JsonEscape(link.trafficClass) << "\"}";
    }
  out << "]}";
  return out.str();
}

bool
TcpJsonLineQuery(const CoreConfig& config, const std::string& request, std::string* response, std::string* error)
{
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    {
      *error = "socket() failed";
      return false;
    }

  struct timeval timeout;
  timeout.tv_sec = config.sionnaDeadlineMs / 1000;
  timeout.tv_usec = static_cast<suseconds_t>((config.sionnaDeadlineMs % 1000) * 1000);
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

  sockaddr_in addr {};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(config.sionnaPort);
  if (inet_pton(AF_INET, config.sionnaHost.c_str(), &addr.sin_addr) != 1)
    {
      *error = "sionna_host must be an IPv4 address for this adapter: " + config.sionnaHost;
      close(fd);
      return false;
    }

  if (connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0)
    {
      *error = "connect() failed to " + config.sionnaHost + ":" + std::to_string(config.sionnaPort);
      close(fd);
      return false;
    }

  const std::string line = request + "\n";
  ssize_t sent = send(fd, line.data(), line.size(), 0);
  if (sent < 0 || static_cast<size_t>(sent) != line.size())
    {
      *error = "send() failed or wrote a partial request";
      close(fd);
      return false;
    }

  response->clear();
  char ch = '\0';
  while (true)
    {
      ssize_t n = recv(fd, &ch, 1, 0);
      if (n <= 0)
        {
          *error = "recv() ended before a JSON line response";
          close(fd);
          return false;
        }
      if (ch == '\n')
        {
          break;
        }
      response->push_back(ch);
      if (response->size() > 1024 * 1024)
        {
          *error = "Sionna response exceeded 1 MiB";
          close(fd);
          return false;
        }
    }

  close(fd);
  return true;
}

std::string
ExtractString(const std::string& object, const std::string& key, const std::string& fallback = "")
{
  const std::regex pattern("\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
  std::smatch match;
  if (std::regex_search(object, match, pattern))
    {
      return match[1].str();
    }
  return fallback;
}

double
ExtractDouble(const std::string& object, const std::string& key, double fallback)
{
  const std::regex pattern("\"" + key + "\"\\s*:\\s*(-?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)");
  std::smatch match;
  if (std::regex_search(object, match, pattern))
    {
      return std::stod(match[1].str());
    }
  return fallback;
}

uint64_t
ExtractUint64(const std::string& object, const std::string& key, uint64_t fallback)
{
  const std::regex pattern("\"" + key + "\"\\s*:\\s*([0-9]+)");
  std::smatch match;
  if (std::regex_search(object, match, pattern))
    {
      return static_cast<uint64_t>(std::stoull(match[1].str()));
    }
  return fallback;
}

bool
ExtractBool(const std::string& object, const std::string& key, bool fallback)
{
  const std::regex pattern("\"" + key + "\"\\s*:\\s*(true|false)");
  std::smatch match;
  if (std::regex_search(object, match, pattern))
    {
      return match[1].str() == "true";
    }
  return fallback;
}

std::vector<std::string>
ExtractObjectsFromArray(const std::string& response, const std::string& arrayName)
{
  std::vector<std::string> objects;
  const auto arrayNamePos = response.find("\"" + arrayName + "\"");
  if (arrayNamePos == std::string::npos)
    {
      return objects;
    }
  const auto arrayStart = response.find('[', arrayNamePos);
  if (arrayStart == std::string::npos)
    {
      return objects;
    }

  bool inString = false;
  bool escaped = false;
  int depth = 0;
  size_t objectStart = std::string::npos;
  for (size_t i = arrayStart + 1; i < response.size(); ++i)
    {
      const char c = response[i];
      if (escaped)
        {
          escaped = false;
          continue;
        }
      if (c == '\\' && inString)
        {
          escaped = true;
          continue;
        }
      if (c == '"')
        {
          inString = !inString;
          continue;
        }
      if (inString)
        {
          continue;
        }
      if (c == '{')
        {
          if (depth == 0)
            {
              objectStart = i;
            }
          ++depth;
        }
      else if (c == '}')
        {
          --depth;
          if (depth == 0 && objectStart != std::string::npos)
            {
              objects.push_back(response.substr(objectStart, i - objectStart + 1));
              objectStart = std::string::npos;
            }
        }
      else if (c == ']' && depth == 0)
        {
          break;
        }
    }
  return objects;
}

std::vector<std::string>
ExtractLinkObjects(const std::string& response)
{
  return ExtractObjectsFromArray(response, "links");
}

bool
ExtractPositionM(const std::string& object, double* x, double* y, double* z)
{
  const std::string number = "(-?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)";
  const std::regex pattern("\"position_m\"\\s*:\\s*\\[\\s*" + number + "\\s*,\\s*" +
                           number + "\\s*,\\s*" + number + "\\s*\\]");
  std::smatch match;
  if (!std::regex_search(object, match, pattern))
    {
      return false;
    }
  *x = std::stod(match[1].str());
  *y = std::stod(match[2].str());
  *z = std::stod(match[3].str());
  return true;
}

bool
RefreshNodePositionsFromStateFile(CoreConfig& config)
{
  if (config.nodeStateFile.empty())
    {
      return false;
    }

  std::ifstream in(config.nodeStateFile);
  if (!in)
    {
      NS_LOG_WARN("Unable to read node-state file for live Sionna update: " << config.nodeStateFile);
      return false;
    }

  std::ostringstream buffer;
  buffer << in.rdbuf();
  const std::string content = buffer.str();
  uint32_t updated = 0;
  for (const auto& object : ExtractObjectsFromArray(content, "nodes"))
    {
      const std::string id = ExtractString(object, "id");
      if (id.empty())
        {
          continue;
        }
      double x = 0.0;
      double y = 0.0;
      double z = 0.0;
      if (!ExtractPositionM(object, &x, &y, &z))
        {
          continue;
        }
      for (auto& node : config.nodes)
        {
          if (node.id == id)
            {
              node.x = x;
              node.y = y;
              node.z = z;
              ++updated;
              break;
            }
        }
    }

  if (updated > 0)
    {
      NS_LOG_INFO("Updated " << updated << " node positions from " << config.nodeStateFile);
    }
  return updated > 0;
}

std::map<std::string, LinkState>
ParseSionnaResponse(const CoreConfig& config, const std::string& response)
{
  std::map<std::string, LinkState> states;
  for (const auto& object : ExtractLinkObjects(response))
    {
      LinkState state;
      state.tx = ExtractString(object, "tx");
      state.rx = ExtractString(object, "rx");
      state.trafficClass = ExtractString(object, "traffic_class");
      if (state.tx.empty() || state.rx.empty() || state.trafficClass.empty())
        {
          continue;
        }
      const auto* trafficClass = FindTrafficClass(config, state.trafficClass);
      state.pathlossDb = ExtractDouble(object, "pathloss_db", 0.0);
      state.rssiDbm = ExtractDouble(object, "rssi_dbm", -120.0);
      state.sinrDb = ExtractDouble(object, "sinr_db", 0.0);
      state.jsDb = ExtractDouble(object, "js_db", -100.0);
      state.serviceTierBps =
          ExtractUint64(object, "service_tier_bps", trafficClass ? trafficClass->targetBps : 1000);
      state.perInput = Clamp01(ExtractDouble(object, "per_input", 0.0));
      state.linkState = ExtractString(object, "link_state", "unknown");
      state.stale = ExtractBool(object, "stale", false);
      state.source = "sionna";
      states[LinkKey(state.tx, state.rx, state.trafficClass)] = state;
    }
  return states;
}

uint64_t
ServiceTierFromSinr(double sinrDb)
{
  if (sinrDb >= 25.0)
    {
      return 20000000ULL;
    }
  if (sinrDb >= 18.0)
    {
      return 2000000ULL;
    }
  if (sinrDb >= 12.0)
    {
      return 500000ULL;
    }
  if (sinrDb >= 7.0)
    {
      return 100000ULL;
    }
  if (sinrDb >= 2.0)
    {
      return 10000ULL;
    }
  return 1000ULL;
}

double
PerFromSinr(double sinrDb)
{
  if (sinrDb >= 20.0)
    {
      return 0.001;
    }
  if (sinrDb >= 12.0)
    {
      return 0.01;
    }
  if (sinrDb >= 7.0)
    {
      return 0.05;
    }
  if (sinrDb >= 2.0)
    {
      return 0.15;
    }
  return 0.35;
}

std::map<std::string, LinkState>
BuildMockStates(const CoreConfig& config)
{
  std::map<std::string, LinkState> states;
  const double noiseDbm =
      -174.0 + 10.0 * std::log10(static_cast<double>(config.bandwidthHz)) + config.noiseFigureDb;

  for (const auto& link : config.links)
    {
      const auto* tx = FindNode(config, link.tx);
      const auto* rx = FindNode(config, link.rx);
      if (!tx || !rx)
        {
          continue;
        }

      LinkState state;
      state.tx = link.tx;
      state.rx = link.rx;
      state.trafficClass = link.trafficClass;
      state.pathlossDb = FreeSpacePathlossDb(config.carrierHz, DistanceM(*tx, *rx));
      state.rssiDbm = config.txPowerDbm - state.pathlossDb;

      double interferenceMw = DbmToMw(noiseDbm);
      double strongestJammerDbm = -200.0;
      for (const auto& emitter : config.emitters)
        {
          if (!emitter.enabled || emitter.dutyCycle <= 0.0)
            {
              continue;
            }
          const double pathloss = FreeSpacePathlossDb(emitter.centerHz, DistanceM(emitter, *rx));
          const double jammerDbm = emitter.powerDbm - pathloss + 10.0 * std::log10(emitter.dutyCycle);
          strongestJammerDbm = std::max(strongestJammerDbm, jammerDbm);
          interferenceMw += DbmToMw(jammerDbm);
        }

      const double interferenceDbm = MwToDbm(interferenceMw);
      state.sinrDb = state.rssiDbm - interferenceDbm;
      state.jsDb = strongestJammerDbm - state.rssiDbm;
      state.serviceTierBps = ServiceTierFromSinr(state.sinrDb);
      state.perInput = PerFromSinr(state.sinrDb);
      state.linkState = state.sinrDb >= 7.0 ? "good" : (state.sinrDb >= 2.0 ? "degraded" : "poor");
      state.stale = false;
      state.source = "mock";
      states[LinkKey(link.tx, link.rx, link.trafficClass)] = state;
    }

  return states;
}

std::map<std::string, LinkState>
QueryLinkState(CoreConfig& config)
{
  RefreshNodePositionsFromStateFile(config);
  const std::string request = BuildSionnaRequest(config);
  const std::string clientLogPath = config.runDir + "/logs/ns3_sionna_client.jsonl";
  std::ofstream clientLog(clientLogPath, std::ios::app);
  clientLog << "{\"event\":\"request\",\"time_s\":" << Simulator::Now().GetSeconds()
            << ",\"payload\":" << request << "}\n";

  std::string response;
  std::string error;
  if (TcpJsonLineQuery(config, request, &response, &error))
    {
      clientLog << "{\"event\":\"response\",\"time_s\":" << Simulator::Now().GetSeconds()
                << ",\"payload\":" << response << "}\n";
      auto states = ParseSionnaResponse(config, response);
      if (states.size() == config.links.size())
        {
          return states;
        }
      error = "response contained " + std::to_string(states.size()) + " parseable link objects; expected " +
              std::to_string(config.links.size());
    }

  clientLog << "{\"event\":\"error\",\"time_s\":" << Simulator::Now().GetSeconds()
            << ",\"message\":\"" << JsonEscape(error) << "\"}\n";

  if (!config.allowMockSionna)
    {
      NS_FATAL_ERROR("Sionna link-state query failed and mock mode is disabled: " << error);
    }

  clientLog << "{\"event\":\"mock_link_state\",\"time_s\":" << Simulator::Now().GetSeconds()
            << ",\"message\":\"using deterministic mock link state for dependency smoke testing only\"}\n";
  return BuildMockStates(config);
}

LinkState
StateForLink(const CoreConfig& config,
             const std::map<std::string, LinkState>& states,
             const LinkSpec& link)
{
  const auto key = LinkKey(link.tx, link.rx, link.trafficClass);
  const auto found = states.find(key);
  if (found != states.end())
    {
      return found->second;
    }

  const auto* trafficClass = FindTrafficClass(config, link.trafficClass);
  LinkState fallback;
  fallback.tx = link.tx;
  fallback.rx = link.rx;
  fallback.trafficClass = link.trafficClass;
  fallback.serviceTierBps = trafficClass ? trafficClass->targetBps : 1000;
  fallback.perInput = 0.0;
  fallback.linkState = "missing";
  fallback.source = "fallback";
  fallback.stale = true;
  return fallback;
}

void
WriteLinkStatesCsv(const CoreConfig& config, const std::map<std::string, LinkState>& states, bool append)
{
  std::ofstream out(config.runDir + "/metrics/ns3_link_states.csv",
                    append ? std::ios::app : std::ios::trunc);
  if (!append)
    {
      out << "time_s,tx,rx,traffic_class,pathloss_db,rssi_dbm,sinr_db,js_db,service_tier_bps,per_input,link_state,stale,source\n";
    }
  for (const auto& link : config.links)
    {
      const auto state = StateForLink(config, states, link);
      out << Simulator::Now().GetSeconds() << "," << state.tx << "," << state.rx << ","
          << state.trafficClass << "," << state.pathlossDb << "," << state.rssiDbm << ","
          << state.sinrDb << "," << state.jsDb << "," << state.serviceTierBps << ","
          << state.perInput << "," << state.linkState << "," << (state.stale ? "true" : "false")
          << "," << state.source << "\n";
    }
}

uint64_t
DataRateBpsForLink(const CoreConfig& config, const LinkState& state, const LinkSpec& link)
{
  const auto* trafficClass = FindTrafficClass(config, link.trafficClass);
  const uint64_t offeredBps = trafficClass ? trafficClass->offeredBps : 1000;
  return std::max<uint64_t>(1000, std::min<uint64_t>(offeredBps, state.serviceTierBps));
}

void
WriteFlowRatesCsv(const CoreConfig& config, const std::vector<FlowSpec>& flows, bool append)
{
  std::ofstream out(config.runDir + "/metrics/ns3_flow_rates.csv",
                    append ? std::ios::app : std::ios::trunc);
  if (!append)
    {
      out << "time_s,port,tx,rx,traffic_class,data_rate_bps,per_input\n";
    }
  for (const auto& flow : flows)
    {
      out << Simulator::Now().GetSeconds() << "," << flow.port << "," << flow.tx << ","
          << flow.rx << "," << flow.trafficClass << "," << flow.dataRateBps << ","
          << flow.perInput << "\n";
    }
}

void
AccumulateRadioStats(RadioStats& stats, const std::map<std::string, LinkState>& states)
{
  ++stats.sionnaQueries;
  bool staleThisQuery = false;
  for (const auto& item : states)
    {
      stats.usedMockSionna = stats.usedMockSionna || item.second.source == "mock";
      staleThisQuery = staleThisQuery || item.second.stale;
      stats.minSinr = std::min(stats.minSinr, item.second.sinrDb);
      stats.maxJs = std::max(stats.maxJs, item.second.jsDb);
    }
  if (staleThisQuery)
    {
      ++stats.staleSionnaQueries;
    }
}

void
ApplyReceiverErrorModels(const CoreConfig& config,
                         const std::map<std::string, LinkState>& states,
                         const std::vector<Ptr<RateErrorModel>>& receiverErrorModels)
{
  for (size_t i = 0; i < config.nodes.size(); ++i)
    {
      double receiverPer = 0.0;
      for (const auto& link : config.links)
        {
          if (link.rx == config.nodes[i].id)
            {
              receiverPer = std::max(receiverPer, StateForLink(config, states, link).perInput);
            }
        }
      if (i < receiverErrorModels.size() && receiverErrorModels[i])
        {
          receiverErrorModels[i]->SetAttribute("ErrorRate", DoubleValue(Clamp01(receiverPer)));
        }
    }
}

void
ApplyFlowRates(const CoreConfig& config,
               const std::map<std::string, LinkState>& states,
               std::vector<FlowSpec>& flows,
               const std::vector<Ptr<Application>>& sourceApps)
{
  for (size_t i = 0; i < flows.size(); ++i)
    {
      LinkSpec link;
      link.tx = flows[i].tx;
      link.rx = flows[i].rx;
      link.trafficClass = flows[i].trafficClass;
      const auto state = StateForLink(config, states, link);
      const uint64_t dataRateBps = DataRateBpsForLink(config, state, link);
      flows[i].dataRateBps = dataRateBps;
      flows[i].perInput = state.perInput;
      if (i < sourceApps.size() && sourceApps[i])
        {
          Ptr<OnOffApplication> app = DynamicCast<OnOffApplication>(sourceApps[i]);
          if (app)
            {
              app->SetAttribute("DataRate", DataRateValue(DataRate(dataRateBps)));
            }
        }
    }
}

void
WriteTrafficClassesCsv(const CoreConfig& config)
{
  std::ofstream out(config.runDir + "/metrics/traffic_classes.csv");
  out << "traffic_class,priority,target_bps,deadline_ms,offered_bps,tos\n";
  for (const auto& trafficClass : config.trafficClasses)
    {
      out << trafficClass.name << "," << trafficClass.priority << "," << trafficClass.targetBps
          << "," << trafficClass.deadlineMs << "," << trafficClass.offeredBps << ","
          << trafficClass.tos << "\n";
    }
}

void
WriteFlowManifest(const CoreConfig& config, const std::vector<FlowSpec>& flows)
{
  std::ofstream out(config.runDir + "/metrics/ns3_flows.csv");
  out << "port,tx,rx,traffic_class,data_rate_bps,per_input\n";
  for (const auto& flow : flows)
    {
      out << flow.port << "," << flow.tx << "," << flow.rx << "," << flow.trafficClass << ","
          << flow.dataRateBps << "," << flow.perInput << "\n";
    }
}

void
RefreshLiveLinkState(CoreConfig* config,
                     std::map<std::string, LinkState>* linkStates,
                     std::vector<Ptr<RateErrorModel>>* receiverErrorModels,
                     std::vector<FlowSpec>* flowManifest,
                     std::vector<Ptr<Application>>* sourceApps,
                     RadioStats* radioStats)
{
  if (!config || !linkStates || !receiverErrorModels || !flowManifest || !sourceApps || !radioStats)
    {
      return;
    }

  *linkStates = QueryLinkState(*config);
  AccumulateRadioStats(*radioStats, *linkStates);
  WriteLinkStatesCsv(*config, *linkStates, true);
  ApplyReceiverErrorModels(*config, *linkStates, *receiverErrorModels);
  ApplyFlowRates(*config, *linkStates, *flowManifest, *sourceApps);
  WriteFlowRatesCsv(*config, *flowManifest, true);

  const double nextTime = Simulator::Now().GetSeconds() + config->sionnaQueryPeriodS;
  if (config->sionnaQueryPeriodS > 0.0 && nextTime <= config->durationS)
    {
      Simulator::Schedule(Seconds(config->sionnaQueryPeriodS),
                          &RefreshLiveLinkState,
                          config,
                          linkStates,
                          receiverErrorModels,
                          flowManifest,
                          sourceApps,
                          radioStats);
    }
}

} // namespace

int
main(int argc, char* argv[])
{
  std::string topologyPath;
  std::string runDirOverride;

  CommandLine cmd(__FILE__);
  cmd.AddValue("topology", "Path to generated ns-3 topology file", topologyPath);
  cmd.AddValue("runDir", "Run output directory override", runDirOverride);
  cmd.Parse(argc, argv);

  if (topologyPath.empty())
    {
      NS_FATAL_ERROR("Missing --topology argument");
    }

  GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
  GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

  CoreConfig config = ReadTopology(topologyPath);
  if (!runDirOverride.empty())
    {
      config.runDir = runDirOverride;
    }

  auto linkStates = QueryLinkState(config);
  RadioStats radioStats;
  AccumulateRadioStats(radioStats, linkStates);
  WriteLinkStatesCsv(config, linkStates, false);
  WriteTrafficClassesCsv(config);

  NodeContainer nodes;
  nodes.Create(config.nodes.size());

  MobilityHelper mobility;
  mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mobility.Install(nodes);
  for (size_t i = 0; i < config.nodes.size(); ++i)
    {
      Ptr<MobilityModel> mobilityModel = nodes.Get(i)->GetObject<MobilityModel>();
      mobilityModel->SetPosition(Vector(config.nodes[i].x, config.nodes[i].y, config.nodes[i].z));
    }

  CsmaHelper csma;
  csma.SetChannelAttribute("DataRate", DataRateValue(DataRate(config.channelRateBps)));
  csma.SetChannelAttribute("Delay", TimeValue(MilliSeconds(config.channelDelayMs)));
  csma.SetQueue("ns3::DropTailQueue", "MaxSize",
                QueueSizeValue(QueueSize(std::to_string(config.queueMaxPackets) + "p")));

  NetDeviceContainer devices = csma.Install(nodes);

  InternetStackHelper internet;
  internet.Install(nodes);

  TrafficControlHelper trafficControl;
  trafficControl.SetRootQueueDisc("ns3::PfifoFastQueueDisc");
  trafficControl.Install(devices);

  Ipv4AddressHelper ipv4;
  ipv4.SetBase("10.71.100.0", "255.255.255.0");
  Ipv4InterfaceContainer interfaces = ipv4.Assign(devices);

  std::map<std::string, uint32_t> nodeIndex;
  for (size_t i = 0; i < config.nodes.size(); ++i)
    {
      nodeIndex[config.nodes[i].id] = static_cast<uint32_t>(i);
    }

  std::vector<Ptr<RateErrorModel>> receiverErrorModels;
  for (size_t i = 0; i < config.nodes.size(); ++i)
    {
      Ptr<RateErrorModel> errorModel = CreateObject<RateErrorModel>();
      errorModel->SetAttribute("ErrorRate", DoubleValue(0.0));
      errorModel->SetAttribute("ErrorUnit", StringValue("ERROR_UNIT_PACKET"));
      receiverErrorModels.push_back(errorModel);
      Ptr<CsmaNetDevice> device = DynamicCast<CsmaNetDevice>(devices.Get(i));
      if (device)
        {
          device->SetReceiveErrorModel(errorModel);
        }
    }
  ApplyReceiverErrorModels(config, linkStates, receiverErrorModels);

  ApplicationContainer sinks;
  ApplicationContainer sources;
  std::vector<FlowSpec> flowManifest;
  std::vector<Ptr<Application>> sourceApps;
  uint16_t port = 16000;

  for (size_t i = 0; i < config.links.size(); ++i)
    {
      const auto& link = config.links[i];
      const auto txIndex = nodeIndex.at(link.tx);
      const auto rxIndex = nodeIndex.at(link.rx);
      const auto* trafficClass = FindTrafficClass(config, link.trafficClass);
      const auto state = StateForLink(config, linkStates, link);
      const uint64_t dataRateBps = DataRateBpsForLink(config, state, link);

      PacketSinkHelper sink("ns3::UdpSocketFactory",
                            InetSocketAddress(Ipv4Address::GetAny(), port));
      sinks.Add(sink.Install(nodes.Get(rxIndex)));

      InetSocketAddress remote(interfaces.GetAddress(rxIndex), port);
      if (trafficClass)
        {
          remote.SetTos(static_cast<uint8_t>(trafficClass->tos));
        }

      OnOffHelper onoff("ns3::UdpSocketFactory", Address(remote));
      onoff.SetAttribute("DataRate", DataRateValue(DataRate(dataRateBps)));
      onoff.SetAttribute("PacketSize", UintegerValue(link.trafficClass == "control" ? 96 : 512));
      onoff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
      onoff.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0]"));
      auto apps = onoff.Install(nodes.Get(txIndex));
      apps.Start(Seconds(1.0 + 0.01 * static_cast<double>(i)));
      apps.Stop(Seconds(config.durationS));
      sources.Add(apps);
      sourceApps.push_back(apps.Get(0));

      FlowSpec flow;
      flow.port = port;
      flow.tx = link.tx;
      flow.rx = link.rx;
      flow.trafficClass = link.trafficClass;
      flow.dataRateBps = dataRateBps;
      flow.perInput = state.perInput;
      flowManifest.push_back(flow);
      ++port;
    }

  sinks.Start(Seconds(0.0));
  sinks.Stop(Seconds(config.durationS + 1.0));
  WriteFlowManifest(config, flowManifest);
  WriteFlowRatesCsv(config, flowManifest, false);

  const std::string pcapPrefix = config.runDir + "/pcap/ns3-p2mp";
  csma.EnablePcapAll(pcapPrefix, true);

  FlowMonitorHelper flowmon;
  Ptr<FlowMonitor> monitor = flowmon.InstallAll();
  Ptr<Ipv4FlowClassifier> classifier = DynamicCast<Ipv4FlowClassifier>(flowmon.GetClassifier());

  if (config.sionnaQueryPeriodS > 0.0 && config.sionnaQueryPeriodS <= config.durationS)
    {
      Simulator::Schedule(Seconds(config.sionnaQueryPeriodS),
                          &RefreshLiveLinkState,
                          &config,
                          &linkStates,
                          &receiverErrorModels,
                          &flowManifest,
                          &sourceApps,
                          &radioStats);
    }

  Simulator::Stop(Seconds(config.durationS + 1.0));
  Simulator::Run();

  monitor->CheckForLostPackets();
  monitor->SerializeToXmlFile(config.runDir + "/flowmon/flowmon.xml", true, true);

  std::map<uint16_t, FlowSpec> flowsByPort;
  for (const auto& flow : flowManifest)
    {
      flowsByPort[flow.port] = flow;
    }

  std::map<std::string, uint64_t> txPacketsByClass;
  std::map<std::string, uint64_t> rxPacketsByClass;
  std::map<std::string, uint64_t> lostPacketsByClass;
  for (const auto& item : monitor->GetFlowStats())
    {
      Ipv4FlowClassifier::FiveTuple tuple = classifier->FindFlow(item.first);
      const auto flow = flowsByPort.find(tuple.destinationPort);
      if (flow == flowsByPort.end())
        {
          continue;
        }
      const std::string& trafficClass = flow->second.trafficClass;
      txPacketsByClass[trafficClass] += item.second.txPackets;
      rxPacketsByClass[trafficClass] += item.second.rxPackets;
      lostPacketsByClass[trafficClass] += item.second.lostPackets;
    }

  double minSinr = radioStats.minSinr;
  double maxJs = radioStats.maxJs;
  if (!std::isfinite(minSinr))
    {
      minSinr = 0.0;
    }
  if (!std::isfinite(maxJs))
    {
      maxJs = 0.0;
    }

  std::ofstream summary(config.runDir + "/metrics/summary.json");
  summary << "{\n";
  summary << "  \"run_id\": \"" << JsonEscape(config.runId) << "\",\n";
  summary << "  \"scenario\": \"scenario_5uav\",\n";
  summary << "  \"p0_passed\": false,\n";
  summary << "  \"duration_s\": " << config.durationS << ",\n";
  summary << "  \"uav_count\": 5,\n";
  summary << "  \"traffic_classes\": [\"control\", \"payload\", \"additional_data\"],\n";
  summary << "  \"packet_core\": {\n";
  summary << "    \"mode\": \"" << JsonEscape(config.packetCoreMode) << "\",\n";
  summary << "    \"status\": \"" << JsonEscape(config.packetCoreStatus) << "\",\n";
  summary << "    \"runtime_selectable\": "
          << (config.packetCoreRuntimeSelectable ? "true" : "false") << ",\n";
  summary << "    \"shared_medium_model\": \"" << JsonEscape(config.packetCoreSharedMediumModel)
          << "\",\n";
  summary << "    \"fidelity_note\": \"" << JsonEscape(config.packetCoreFidelityNote) << "\"\n";
  summary << "  },\n";
  summary << "  \"packets\": {\n";
  summary << "    \"control_tx\": " << txPacketsByClass["control"] << ",\n";
  summary << "    \"control_rx\": " << rxPacketsByClass["control"] << ",\n";
  summary << "    \"payload_tx\": " << txPacketsByClass["payload"] << ",\n";
  summary << "    \"payload_rx\": " << rxPacketsByClass["payload"] << ",\n";
  summary << "    \"additional_tx\": " << txPacketsByClass["additional_data"] << ",\n";
  summary << "    \"additional_rx\": " << rxPacketsByClass["additional_data"] << "\n";
  summary << "  },\n";
  summary << "  \"radio\": {\n";
  summary << "    \"min_sinr_db\": " << minSinr << ",\n";
  summary << "    \"max_js_db\": " << maxJs << ",\n";
  summary << "    \"sionna_queries\": " << radioStats.sionnaQueries << ",\n";
  summary << "    \"late_sionna_queries\": " << radioStats.staleSionnaQueries << ",\n";
  summary << "    \"used_mock_sionna\": " << (radioStats.usedMockSionna ? "true" : "false") << "\n";
  summary << "  },\n";
  summary << "  \"validation\": {\n";
  summary << "    \"packet_core_ran\": true,\n";
  summary << "    \"online_sionna\": " << (radioStats.usedMockSionna ? "false" : "true") << ",\n";
  summary << "    \"live_sionna_updates\": " << (radioStats.sionnaQueries > 1 ? "true" : "false") << ",\n";
  summary << "    \"shared_medium_model\": \"" << JsonEscape(config.packetCoreSharedMediumModel)
          << "\",\n";
  summary << "    \"pcap_prefix\": \"pcap/ns3-p2mp\",\n";
  summary << "    \"flowmon\": \"flowmon/flowmon.xml\"\n";
  summary << "  }\n";
  summary << "}\n";

  Simulator::Destroy();
  return 0;
}
