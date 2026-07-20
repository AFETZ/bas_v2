/*
 * Thin AMS live radio probe for the upstream ns-3 Sionna RT channel model.
 *
 * This intentionally uses SionnaRtChannelModel/SionnaRtSpectrumPropagationLossModel
 * from ns-3 MR2608 instead of the legacy TCP JSONL provider path.
 */

#include "pybind11/embed.h"
#include "pybind11/eval.h"

#include "ns3/antenna-module.h"
#include "ns3/constant-position-mobility-model.h"
#include "ns3/core-module.h"
#include "ns3/lte-spectrum-value-helper.h"
#include "ns3/mobility-model.h"
#include "ns3/net-device.h"
#include "ns3/node-container.h"
#include "ns3/node.h"
#include "ns3/simple-net-device.h"
#include "ns3/sionna-rt-channel-model.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-signal-parameters.h"
#include "ns3/uniform-planar-array.h"

#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

namespace py = pybind11;
using namespace ns3;

NS_LOG_COMPONENT_DEFINE("AmsSionnaRtLive");

namespace
{

struct NodeSample
{
  Vector position;
  std::string source = "static";
  std::string sourceTopic = "";
  bool stale = false;
};

struct SampleParams
{
  std::string nodeStatePath;
  std::string txId;
  std::string rxId;
  Ptr<MobilityModel> txMob;
  Ptr<MobilityModel> rxMob;
  Ptr<NetDevice> txDev;
  Ptr<NetDevice> rxDev;
  Ptr<PhasedArrayModel> txAntenna;
  Ptr<PhasedArrayModel> rxAntenna;
  double txPowerDbm = 33.0;
  double noiseFigureDb = 6.0;
  std::ofstream* csv = nullptr;
};

Ptr<SionnaRtSpectrumPropagationLossModel> g_spectrumLossModel;
uint32_t g_samples = 0;
double g_snrSumDb = 0.0;

void
ConfigureMitsubaVariant()
{
  const char* envVariant = std::getenv("SIONNA_MITSUBA_VARIANT");
  std::string variant = envVariant && std::string(envVariant).size() > 0
                            ? std::string(envVariant)
                            : std::string("llvm_ad_mono_polarized");
  py::module_ mi = py::module_::import("mitsuba");
  py::object current = mi.attr("variant")();
  if (!current.is_none())
    {
      return;
    }
  py::list available = mi.attr("variants")();
  bool found = false;
  for (py::handle item : available)
    {
      if (item.cast<std::string>() == variant)
        {
          found = true;
          break;
        }
    }
  if (!found)
    {
      std::ostringstream msg;
      msg << "requested Mitsuba variant '" << variant << "' is not available";
      throw std::runtime_error(msg.str());
    }
  mi.attr("set_variant")(variant);
  std::cout << "Mitsuba variant: " << variant << std::endl;
}

std::string
CsvEscape(const std::string& value)
{
  if (value.find_first_of(",\"\n\r") == std::string::npos)
    {
      return value;
    }
  std::ostringstream out;
  out << '"';
  for (char c : value)
    {
      if (c == '"')
        {
          out << "\"\"";
        }
      else
        {
          out << c;
        }
    }
  out << '"';
  return out.str();
}

NodeSample
ReadNodeSample(const std::string& path, const std::string& nodeId, const Vector& fallback)
{
  NodeSample sample;
  sample.position = fallback;
  if (path.empty())
    {
      return sample;
    }

  try
    {
      py::dict locals;
      locals["path"] = path;
      locals["node_id"] = nodeId;
      py::exec(R"PY(
import json
from pathlib import Path

try:
    data = json.loads(Path(path).read_text())
    result = None
    for node in data.get("nodes", []):
        if str(node.get("id")) == str(node_id):
            p = node.get("position_m", [])
            if len(p) < 3:
                raise ValueError(f"node {node_id} has invalid position_m={p!r}")
            result = (
                True,
                float(p[0]), float(p[1]), float(p[2]),
                str(data.get("source", "node_state")),
                bool(node.get("stale", False)),
                str(node.get("source_topic", "")),
            )
            break
    if result is None:
        result = (False, f"node {node_id} not found")
except Exception as exc:
    result = (False, str(exc))
)PY",
               py::globals(),
               locals);

      py::tuple result = locals["result"].cast<py::tuple>();
      if (!result[0].cast<bool>())
        {
          sample.source = "node_state_error:" + result[1].cast<std::string>();
          sample.stale = true;
          return sample;
        }
      sample.position = Vector(result[1].cast<double>(),
                               result[2].cast<double>(),
                               result[3].cast<double>());
      sample.source = result[4].cast<std::string>();
      sample.stale = result[5].cast<bool>();
      sample.sourceTopic = result[6].cast<std::string>();
      return sample;
    }
  catch (const std::exception& exc)
    {
      sample.source = std::string("node_state_error:") + exc.what();
      sample.stale = true;
      return sample;
    }
}

void
DoBeamforming(Ptr<NetDevice> thisDevice,
              Ptr<PhasedArrayModel> thisAntenna,
              Ptr<NetDevice> otherDevice)
{
  Vector aPos = thisDevice->GetNode()->GetObject<MobilityModel>()->GetPosition();
  Vector bPos = otherDevice->GetNode()->GetObject<MobilityModel>()->GetPosition();
  Angles angle(bPos, aPos);
  double h = angle.GetAzimuth();
  double v = angle.GetInclination();
  uint64_t elements = thisAntenna->GetNumElems();
  PhasedArrayModel::ComplexVector weights(elements);
  double power = 1.0 / std::sqrt(static_cast<double>(elements));
  double sinV = std::sin(v);
  double cosV = std::cos(v);
  double sinH = std::sin(h);
  double cosH = std::cos(h);
  for (uint64_t i = 0; i < elements; ++i)
    {
      Vector loc = thisAntenna->GetElementLocation(i);
      double phase = -2 * M_PI * (sinV * cosH * loc.x + sinV * sinH * loc.y + cosV * loc.z);
      weights[i] = std::exp(std::complex<double>(0.0, phase)) * power;
    }
  thisAntenna->SetBeamformingVector(weights);
}

void
SampleLink(SampleParams params)
{
  NodeSample tx = ReadNodeSample(params.nodeStatePath, params.txId, params.txMob->GetPosition());
  NodeSample rx = ReadNodeSample(params.nodeStatePath, params.rxId, params.rxMob->GetPosition());
  params.txMob->SetPosition(tx.position);
  params.rxMob->SetPosition(rx.position);
  DoBeamforming(params.txDev, params.txAntenna, params.rxDev);
  DoBeamforming(params.rxDev, params.rxAntenna, params.txDev);

  std::vector<int> activeRbs(6);
  for (int i = 0; i < 6; ++i)
    {
      activeRbs[i] = i;
    }
  auto txPsd = LteSpectrumValueHelper::CreateTxPowerSpectralDensity(
      2100,
      static_cast<uint16_t>(activeRbs.size()),
      params.txPowerDbm,
      activeRbs);
  auto txParams = Create<SpectrumSignalParameters>();
  txParams->psd = txPsd->Copy();
  auto noisePsd = LteSpectrumValueHelper::CreateNoisePowerSpectralDensity(
      2100,
      static_cast<uint16_t>(activeRbs.size()),
      params.noiseFigureDb);

  auto rxParams = g_spectrumLossModel->CalcRxPowerSpectralDensity(txParams,
                                                                  params.txMob,
                                                                  params.rxMob,
                                                                  params.txAntenna,
                                                                  params.rxAntenna);
  auto rxPsd = rxParams->psd;
  double rxPowerDbm = 10.0 * std::log10(Sum(*rxPsd) * 180e3);
  double snrDb = 10.0 * std::log10(Sum(*rxPsd) / Sum(*noisePsd));
  double t = Simulator::Now().GetSeconds();

  ++g_samples;
  g_snrSumDb += snrDb;

  std::cout << "[t=" << std::fixed << std::setprecision(3) << t << "s] "
            << params.txId << "->" << params.rxId << " SNR=" << std::setprecision(2) << snrDb
            << " dB RSSI=" << rxPowerDbm << " dBm source=" << tx.source << "/"
            << rx.source << std::endl;

  if (params.csv && params.csv->good())
    {
      (*params.csv) << std::fixed << std::setprecision(3) << t << ","
                    << CsvEscape(tx.source == rx.source ? tx.source : tx.source + "+" + rx.source)
                    << "," << CsvEscape(params.txId) << "," << CsvEscape(params.rxId) << ","
                    << tx.position.x << "," << tx.position.y << "," << tx.position.z << ","
                    << rx.position.x << "," << rx.position.y << "," << rx.position.z << ","
                    << std::setprecision(6) << snrDb << "," << rxPowerDbm << ","
                    << (tx.stale ? "true" : "false") << "," << (rx.stale ? "true" : "false")
                    << "," << CsvEscape(tx.sourceTopic) << "," << CsvEscape(rx.sourceTopic)
                    << "\n";
      params.csv->flush();
    }
}

} // namespace

int
main(int argc, char* argv[])
{
  py::scoped_interpreter guard{};
  ConfigureMitsubaVariant();

  std::string scene = "simple_street_canyon_with_cars";
  std::string nodeStatePath;
  std::string runDir = ".";
  std::string outputCsv = "";
  std::string txId = "uav1";
  std::string rxId = "uav2";
  double durationS = 20.0;
  double periodS = 1.0;
  double frequencyHz = 2.4e9;
  double txPowerDbm = 33.0;
  double noiseFigureDb = 6.0;
  double txX = -400.0;
  double txY = 0.0;
  double txZ = 80.0;
  double rxX = -300.0;
  double rxY = 0.0;
  double rxZ = 80.0;
  bool realtime = true;

  SionnaRtChannelModel::RtPathSolverConfig solver;
  solver.maxDepth = 0;
  solver.los = true;
  solver.specularReflection = false;
  solver.diffuseReflection = false;
  solver.diffraction = false;
  solver.edgeDiffraction = false;
  solver.refraction = false;
  solver.syntheticArray = true;
  solver.seed = 42;

  CommandLine cmd;
  cmd.AddValue("scene", "Sionna built-in scene name or absolute/relative Mitsuba XML path", scene);
  cmd.AddValue("nodeState", "Live position_tracker node_state.json path", nodeStatePath);
  cmd.AddValue("runDir", "Run directory for metrics output", runDir);
  cmd.AddValue("outputCsv", "CSV output path; defaults to <runDir>/metrics/ns3_sionna_rt_live.csv", outputCsv);
  cmd.AddValue("tx", "Transmitter node id in node_state", txId);
  cmd.AddValue("rx", "Receiver node id in node_state", rxId);
  cmd.AddValue("duration", "Simulation duration in seconds", durationS);
  cmd.AddValue("period", "Sionna RT channel update/sample period in seconds", periodS);
  cmd.AddValue("frequency", "Carrier frequency in Hz", frequencyHz);
  cmd.AddValue("txPowerDbm", "Transmit power in dBm", txPowerDbm);
  cmd.AddValue("noiseFigureDb", "Receiver noise figure in dB", noiseFigureDb);
  cmd.AddValue("txX", "Fallback/static tx x", txX);
  cmd.AddValue("txY", "Fallback/static tx y", txY);
  cmd.AddValue("txZ", "Fallback/static tx z", txZ);
  cmd.AddValue("rxX", "Fallback/static rx x", rxX);
  cmd.AddValue("rxY", "Fallback/static rx y", rxY);
  cmd.AddValue("rxZ", "Fallback/static rx z", rxZ);
  cmd.AddValue("realtime", "Use ns-3 RealtimeSimulatorImpl for wall-clock live node-state reads", realtime);
  cmd.AddValue("maxDepth", "Sionna RT max path depth", solver.maxDepth);
  cmd.AddValue("los", "Enable LOS path", solver.los);
  cmd.AddValue("specularReflection", "Enable specular reflections", solver.specularReflection);
  cmd.AddValue("diffuseReflection", "Enable diffuse reflections", solver.diffuseReflection);
  cmd.AddValue("refraction", "Enable refraction", solver.refraction);
  cmd.AddValue("diffraction", "Enable diffraction", solver.diffraction);
  cmd.AddValue("edgeDiffraction", "Enable edge diffraction", solver.edgeDiffraction);
  cmd.AddValue("syntheticArray", "Enable synthetic array", solver.syntheticArray);
  cmd.AddValue("seed", "Sionna RT seed", solver.seed);
  cmd.Parse(argc, argv);

  if (realtime)
    {
      GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    }

  if (outputCsv.empty())
    {
      outputCsv = runDir + "/metrics/ns3_sionna_rt_live.csv";
    }
  std::filesystem::create_directories(std::filesystem::path(outputCsv).parent_path());

  Config::SetDefault("ns3::SionnaRtChannelModel::UpdatePeriod",
                     TimeValue(MilliSeconds(1)));

  g_spectrumLossModel = CreateObject<SionnaRtSpectrumPropagationLossModel>();
  g_spectrumLossModel->SetChannelModelAttribute("Frequency", DoubleValue(frequencyHz));
  g_spectrumLossModel->SetChannelModelAttribute("Scenario", StringValue(scene));
  g_spectrumLossModel->SetChannelModelAttribute("IsImageRenderedEnabled", BooleanValue(false));
  g_spectrumLossModel->SetRtPathSolverConfig(solver);

  NodeContainer nodes;
  nodes.Create(2);
  Ptr<SimpleNetDevice> txDev = CreateObject<SimpleNetDevice>();
  Ptr<SimpleNetDevice> rxDev = CreateObject<SimpleNetDevice>();
  nodes.Get(0)->AddDevice(txDev);
  txDev->SetNode(nodes.Get(0));
  nodes.Get(1)->AddDevice(rxDev);
  rxDev->SetNode(nodes.Get(1));

  Ptr<MobilityModel> txMob = CreateObject<ConstantPositionMobilityModel>();
  Ptr<MobilityModel> rxMob = CreateObject<ConstantPositionMobilityModel>();
  txMob->SetPosition(Vector(txX, txY, txZ));
  rxMob->SetPosition(Vector(rxX, rxY, rxZ));
  nodes.Get(0)->AggregateObject(txMob);
  nodes.Get(1)->AggregateObject(rxMob);

  Ptr<PhasedArrayModel> txAntenna =
      CreateObjectWithAttributes<UniformPlanarArray>("NumColumns", UintegerValue(1), "NumRows", UintegerValue(1));
  Ptr<PhasedArrayModel> rxAntenna =
      CreateObjectWithAttributes<UniformPlanarArray>("NumColumns", UintegerValue(1), "NumRows", UintegerValue(1));
  txAntenna->SetAttribute("IsDualPolarized", BooleanValue(false));
  rxAntenna->SetAttribute("IsDualPolarized", BooleanValue(false));

  std::ofstream csv(outputCsv, std::ios::out);
  csv << "time_s,source,tx,rx,tx_x,tx_y,tx_z,rx_x,rx_y,rx_z,snr_db,rssi_dbm,tx_stale,rx_stale,tx_source_topic,rx_source_topic\n";
  csv.flush();

  SampleParams params;
  params.nodeStatePath = nodeStatePath;
  params.txId = txId;
  params.rxId = rxId;
  params.txMob = txMob;
  params.rxMob = rxMob;
  params.txDev = txDev;
  params.rxDev = rxDev;
  params.txAntenna = txAntenna;
  params.rxAntenna = rxAntenna;
  params.txPowerDbm = txPowerDbm;
  params.noiseFigureDb = noiseFigureDb;
  params.csv = &csv;

  uint32_t count = static_cast<uint32_t>(std::floor(durationS / std::max(periodS, 0.001))) + 1;
  for (uint32_t i = 0; i < count; ++i)
    {
      Simulator::Schedule(Seconds(i * periodS), &SampleLink, params);
    }
  Simulator::Stop(Seconds(durationS + 0.001));

  try
    {
      Simulator::Run();
      Simulator::Destroy();
    }
  catch (const py::error_already_set& exc)
    {
      std::cerr << "[FAILURE] Sionna/Python error: " << exc.what() << std::endl;
      return 1;
    }
  catch (const std::exception& exc)
    {
      std::cerr << "[FAILURE] ns-3/Sionna RT error: " << exc.what() << std::endl;
      return 1;
    }

  if (g_samples == 0)
    {
      std::cerr << "[FAILURE] no samples were produced" << std::endl;
      return 1;
    }

  std::cout << "[SUCCESS] wrote " << g_samples << " samples to " << outputCsv
            << "; average SNR=" << std::fixed << std::setprecision(2)
            << (g_snrSumDb / static_cast<double>(g_samples)) << " dB" << std::endl;
  return 0;
}
