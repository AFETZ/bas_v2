// Configuration adapters only. Propagation and reception stay in upstream ns-3/Sionna.
#pragma once
#include "ns3/waveform-generator.h"
#include "ns3/non-communicating-net-device.h"
#include "ns3/friis-spectrum-propagation-loss.h"
#include "ns3/three-gpp-antenna-model.h"
#include "ns3/pointer.h"
#include "pybind11/stl.h"
#include <functional>
#include <array>
#include <filesystem>
#include <memory>
#include <map>

namespace bas
{
using namespace ns3;
using EventSink = std::function<void(const std::string&, const std::string&, double,
                                     const std::string&)>;

// Select exactly one upstream propagation model per transmission. No loss formula,
// packet-error decision, or fallback is implemented in this dispatcher.
class SourcePropagation : public PhasedArraySpectrumPropagationLossModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("bas::SourcePropagation")
            .SetParent<PhasedArraySpectrumPropagationLossModel>()
            .AddConstructor<SourcePropagation>();
        return tid;
    }
    Ptr<SionnaRtSpectrumPropagationLossModel> reference;
    std::string profile{"sionna"};
    Ptr<FriisSpectrumPropagationLossModel> freeSpace{CreateObject<FriisSpectrumPropagationLossModel>()};
    std::map<Ptr<SpectrumPhy>, Ptr<SionnaRtSpectrumPropagationLossModel>> sources;
    std::map<Ptr<SpectrumPhy>, Ptr<SpectrumValue>> sourcePsds;
    std::map<Ptr<SpectrumPhy>, std::array<double, 4>> sourceTimes;
  private:
    int64_t DoAssignStreams(int64_t) override { return 0; }
    Ptr<SpectrumSignalParameters> DoCalcRxPowerSpectralDensity(
        Ptr<const SpectrumSignalParameters> params, Ptr<const MobilityModel> a,
        Ptr<const MobilityModel> b, Ptr<const PhasedArrayModel> aa,
        Ptr<const PhasedArrayModel> ba) const override
    {
        const auto x = a->GetPosition();
        const auto y = b->GetPosition();
        const bool farField = x.x > 2000 && y.x > 2000 && x.z >= 200 && y.z >= 200 &&
                              CalculateDistance(x, y) >= 500;
        if (profile == "friis" || (profile == "hybrid" && farField))
        {
            NS_ABORT_MSG_IF(!DynamicCast<const IsotropicAntennaModel>(aa->GetAntennaElement()) ||
                            !DynamicCast<const IsotropicAntennaModel>(ba->GetAntennaElement()),
                            "explicit free-space profile supports isotropic elements only");
            auto result = params->Copy();
            result->psd = freeSpace->CalcRxPowerSpectralDensity(params, a, b);
            return result;
        }
        auto entry = sources.find(params->txPhy);
        auto selected = entry == sources.end() ? reference : entry->second;
        return selected->CalcRxPowerSpectralDensity(params, a, b, aa, ba);
    }
    void DoDispose() override
    {
        sources.clear();
        sourcePsds.clear();
        sourceTimes.clear();
        reference = nullptr;
        PhasedArraySpectrumPropagationLossModel::DoDispose();
    }
};

inline Ptr<SourcePropagation> InstallSpectrumSources(
    Ptr<SionnaRtSpectrumPropagationLossModel> reference,
    Ptr<MultiModelSpectrumChannel> channel, const std::string& config,
    const std::string& scene, const SionnaRtChannelModel::RtPathSolverConfig& solver,
    double maxPaths, EventSink emit)
{
    namespace py = pybind11;
    auto router = CreateObject<SourcePropagation>();
    router->reference = reference;
    auto text = py::module_::import("pathlib").attr("Path")(config).attr("read_text")();
    py::list entries = py::module_::import("json").attr("loads")(text)["segments"];
    auto active = std::make_shared<unsigned>(0);
    auto state = [config, active](bool start) {
        if (start) ++*active;
        else if (*active) --*active;
        std::ofstream output(config + ".state.tmp");
        output << "{\"enabled_sources\":" << *active << ",\"sim_time_s\":"
               << Simulator::Now().GetSeconds() << "}";
        output.close();
        std::filesystem::rename(config + ".state.tmp", config + ".state");
    };
    std::map<double, Ptr<SionnaRtSpectrumPropagationLossModel>> frequencies;
    DoubleValue referenceFrequency;
    reference->GetChannelModelAttribute("Frequency", referenceFrequency);
    frequencies[referenceFrequency.Get()] = reference;
    for (auto item : entries)
    {
        auto entry = py::reinterpret_borrow<py::dict>(item);
        const auto id = entry["id"].cast<std::string>();
        const auto xyz = entry["position_m"].cast<std::vector<double>>();
        const auto angles = entry["orientation_rad"].cast<std::vector<double>>();
        const double hz = entry["center_hz"].cast<double>();
        const double bandwidth = entry["bandwidth_hz"].cast<double>();
        const double power = entry["power_w"].cast<double>();
        const double gain = entry["gain_dbi"].cast<double>();
        const double start = entry["start_s"].cast<double>();
        const double stop = entry["stop_s"].cast<double>();
        const double period = entry["period_s"].cast<double>();
        const double duty = entry["duty_cycle"].cast<double>();
        const auto pattern = entry["pattern"].cast<std::string>();
        NS_ABORT_MSG_IF(xyz.size() != 3 || angles.size() != 2 || bandwidth <= 0 ||
            hz <= bandwidth / 2 || power <= 0 || start < 0 || stop <= start ||
            period <= 0 || duty <= 0 || duty > 1 ||
            (pattern != "iso" && pattern != "tr38901"), "invalid source segment");
        // The envelope gain is the explicitly configured feed gain. The pattern
        // is supplied only to Sionna: the outer AntennaModel stays 0 dBi, avoiding
        // double application by MultiModelSpectrumChannel.
        auto array = CreateObjectWithAttributes<UniformPlanarArray>(
            "NumColumns", UintegerValue(1), "NumRows", UintegerValue(1),
            "BearingAngle", DoubleValue(angles[0]), "DowntiltAngle", DoubleValue(angles[1]));
        array->SetAntennaElement(pattern == "tr38901"
            ? StaticCast<AntennaModel>(CreateObject<ThreeGppAntennaModel>())
            : StaticCast<AntennaModel>(CreateObject<IsotropicAntennaModel>()));
        PhasedArrayModel::ComplexVector weights(1);
        weights[0] = {1.0, 0.0};
        array->SetBeamformingVector(weights);
        auto antenna = CreateObject<IsotropicAntennaModel>();
        antenna->AggregateObject(array);
        auto node = CreateObject<Node>();
        auto mobility = CreateObject<ConstantPositionMobilityModel>();
        mobility->SetPosition({xyz[0], xyz[1], xyz[2]});
        node->AggregateObject(mobility);
        auto device = CreateObject<NonCommunicatingNetDevice>();
        node->AddDevice(device);
        auto generator = CreateObject<WaveformGenerator>();
        device->SetPhy(generator);
        generator->SetDevice(device);
        generator->SetMobility(mobility);
        generator->SetAntenna(antenna);
        generator->SetChannel(channel);
        generator->SetPeriod(Seconds(period));
        generator->SetDutyCycle(duty);
        Bands bands{{hz - bandwidth / 2, hz, hz + bandwidth / 2}};
        auto psd = Create<SpectrumValue>(Create<SpectrumModel>(bands));
        *psd = power * std::pow(10.0, gain / 10.0) / bandwidth;
        generator->SetTxPowerSpectralDensity(psd);
        if (!frequencies.count(hz))
        {
            auto model = CreateObject<SionnaRtSpectrumPropagationLossModel>();
            model->SetChannelModelAttribute("Frequency", DoubleValue(hz));
            model->SetChannelModelAttribute("Scenario", StringValue(scene));
            model->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(true));
            model->SetChannelModelAttribute("MaxNumberOfPaths", DoubleValue(maxPaths));
            model->SetRtPathSolverConfig(solver);
            frequencies[hz] = model;
        }
        router->sources[generator] = frequencies.at(hz);
        router->sourcePsds[generator] = psd;
        router->sourceTimes[generator] = {start, stop, period, duty};
        const auto details = "source=ns3::WaveformGenerator;center_hz=" + std::to_string(hz) +
            ";bandwidth_hz=" + std::to_string(bandwidth) + ";power_w=" + std::to_string(power) +
            ";gain_dbi=" + std::to_string(gain) + ";pattern=" + pattern +
            ";azimuth_rad=" + std::to_string(angles[0]) + ";downtilt_rad=" + std::to_string(angles[1]) +
            ";duty_cycle=" + std::to_string(duty) + ";period_s=" + std::to_string(period);
        Simulator::Schedule(Seconds(start), [generator, emit, id, power, details, state]() {
            state(true);
            emit("jammer_on", id, power, details);
            generator->Start();
        });
        Simulator::Schedule(Seconds(stop), [generator, emit, id, details, state]() {
            state(false);
            generator->Stop();
            emit("jammer_off", id, 0.0, details);
        });
    }
    return router;
}
} // namespace bas
