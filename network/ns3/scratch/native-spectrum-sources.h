// Configuration adapters only. Propagation and reception stay in upstream ns-3/Sionna.
#pragma once
#include "ns3/waveform-generator.h"
#include "ns3/non-communicating-net-device.h"
#include "ns3/friis-spectrum-propagation-loss.h"
#include "ns3/three-gpp-antenna-model.h"
#include "ns3/pointer.h"
#include "ns3/uinteger.h"
#include "ns3/boolean.h"
#include "ns3/event-id.h"
#include "ns3/simulator.h"
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include <functional>
#include <fstream>
#include <array>
#include <filesystem>
#include <memory>
#include <map>
#include <chrono>
#include <optional>
#include <sstream>
#include <iomanip>
#include <vector>
#include <stdexcept>
#include <utility>
#include <algorithm>
#include <cmath>

namespace bas
{
using namespace ns3;
using EventSink = std::function<void(const std::string&, const std::string&, double,
                                     const std::string&)>;

// A value snapshot of one physical WaveformGenerator/array binding. Read a new
// snapshot after schedule materialization; old copies retain their old state.
struct SourceBinding
{
    std::string id;
    size_t segmentIndex{0};
    double centerHz{0}, bandwidthHz{0}, powerW{0}, feedGainDbi{0};
    std::string pattern;
    Vector positionM, velocityMps, orientationRad; // UPA yaw, downtilt, roll=0.
    Ptr<SpectrumPhy> phy;
    Ptr<PhasedArrayModel> array;
    bool scheduleCommitted{false};
    std::optional<Time> begin, end; // Native absolute ticks; never unresolved offsets.

    bool RequiredAt(Time now, Time lookahead = NanoSeconds(0)) const
    {
        if (now < NanoSeconds(0) || lookahead < NanoSeconds(0))
            throw std::invalid_argument("source requirement times must be nonnegative");
        // Keep the entire [begin,end) interval, including duty-cycle holes. This
        // is a cache requirement, not a statement that the transmitter is on.
        // Subtraction avoids overflowing now+lookahead near the native limit.
        return scheduleCommitted && begin && end && now < *end &&
               (now >= *begin || *begin - now <= lookahead);
    }
};

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

    ~SourcePropagation() override { ClearSourceSchedule(); }

    void ConfigureSourceSchedule(const std::string& config, pybind11::dict resolved,
                                 pybind11::object materialize, pybind11::object json,
                                 EventSink emit, std::function<std::string()> phaseProvider)
    {
        m_config = config;
        m_python = std::make_unique<PythonSchedule>(PythonSchedule{
            std::move(resolved), std::move(materialize), std::move(json)});
        m_emit = std::move(emit);
        m_phaseProvider = std::move(phaseProvider);
    }

    std::vector<SourceBinding> GetSourceBindings() const
    {
        std::vector<SourceBinding> result;
        result.reserve(m_schedule.size());
        for (const auto& source : m_schedule) result.push_back(source.binding);
        return result;
    }

    void AddSourceSchedule(Ptr<WaveformGenerator> generator, const std::string& id,
                           double power, const std::string& details, SourceBinding binding)
    {
        binding.segmentIndex = m_schedule.size();
        m_schedule.push_back({generator, id, power, details, {}, {}, std::move(binding)});
    }

    void ArmSourceSchedule(bool relative)
    {
        // Only this destroy event retains the owner. Normal events carry raw
        // pointers/indices, never Python handles or a self-owning closure.
        Simulator::ScheduleDestroy([owner = Ptr<SourcePropagation>(this)] { owner->Dispose(); });
        if (relative)
            m_phaseEvent = Simulator::ScheduleNow(&SourcePropagation::ObserveSourcePhase, this);
        else
            m_phaseEvent = Simulator::ScheduleNow(&SourcePropagation::CommitSourceSchedule,
                                                 this, std::string("simulation_start"));
    }

    void ObserveTransmissions(Ptr<MultiModelSpectrumChannel> channel)
    {
        if (m_traceChannel || !channel || m_scheduleCleared)
            throw std::logic_error("source transmission observer must be installed once before Run");
        if (!channel->TraceConnectWithoutContext("TxSigParams",MakeCallback(&SourcePropagation::ObserveTransmission,this)))
            throw std::runtime_error("native SpectrumChannel TxSigParams trace is unavailable");
        m_traceChannel=channel;
    }

    void ClearSourceSchedule()
    {
        if (m_scheduleCleared) return;
        m_scheduleCleared = true;
        if (m_traceChannel)
        {
            m_traceChannel->TraceDisconnectWithoutContext("TxSigParams",MakeCallback(&SourcePropagation::ObserveTransmission,this));
            m_traceChannel=nullptr;
        }
        if (m_phaseEvent.PeekEventImpl()) m_phaseEvent.Cancel();
        for (auto& source : m_schedule)
        {
            if (source.begin.PeekEventImpl()) source.begin.Cancel();
            if (source.end.PeekEventImpl()) source.end.Cancel();
            source.generator->Stop();
        }
        if (m_activeSources)
        {
            m_activeSources = 0;
            PublishSourceState();
        }
        // Both explicit Dispose and Simulator::Destroy may run with the GIL
        // released. Clear handles AND Python-backed callback storage here;
        // their ordinary C++ destructors must have nothing left to decref.
        NS_ABORT_MSG_IF(m_python && !Py_IsInitialized(),
                        "source schedule must be cleared before Python finalization");
        if (Py_IsInitialized())
        {
            pybind11::gil_scoped_acquire gil;
            m_python.reset();
            m_phaseProvider = {};
            m_emit = {};
        }
        m_schedule.clear();
    }

  private:
    struct PythonSchedule
    {
        pybind11::dict resolved;
        pybind11::object materialize;
        pybind11::object json;
    };
    struct ScheduledSource
    {
        Ptr<WaveformGenerator> generator;
        std::string id;
        double power;
        std::string details;
        EventId begin;
        EventId end;
        SourceBinding binding;
    };
    std::unique_ptr<PythonSchedule> m_python;
    std::vector<ScheduledSource> m_schedule;
    EventSink m_emit;
    std::function<std::string()> m_phaseProvider;
    std::string m_config;
    EventId m_phaseEvent;
    Ptr<MultiModelSpectrumChannel> m_traceChannel; // Disconnected/released by ClearSourceSchedule.
    unsigned m_activeSources{0};
    bool m_scheduleCleared{false};

    void ObserveTransmission(Ptr<SpectrumSignalParameters> params)
    {
        const auto found=std::find_if(m_schedule.begin(),m_schedule.end(),
            [&](const auto& source){return source.generator==params->txPhy;});
        if (found==m_schedule.end()) return;
        if (!params->psd || params->psd->GetValuesN()!=1 || params->duration<=NanoSeconds(0))
            throw std::runtime_error("source waveform does not have the supported rectangular PSD and positive duration");
        const auto band=params->psd->ConstBandsBegin();
        const double power=Integral(*params->psd);
        if (!std::isfinite(power) || power<=0 || !std::isfinite(band->fc) || band->fh<=band->fl)
            throw std::runtime_error("invalid actual source waveform spectrum");
        const auto position=params->txPhy->GetMobility()->GetPosition();
        std::ostringstream details;
        details << std::setprecision(17)
            << "observer=ns3::SpectrumChannel::TxSigParams;stage=before_propagation;segment_index=" << found->binding.segmentIndex
            << ";spectrum_uid=" << params->psd->GetSpectrumModelUid()
            << ";low_hz=" << band->fl << ";center_hz=" << band->fc << ";high_hz=" << band->fh
            << ";bandwidth_hz=" << band->fh-band->fl << ";psd_w_hz=" << (*params->psd)[0]
            << ";psd_integral_w=" << power << ";duration_ns=" << params->duration.GetNanoSeconds()
            << ";position_x_m=" << position.x << ";position_y_m=" << position.y << ";position_z_m=" << position.z;
        m_emit("source_waveform_tx",found->id,power,details.str());
    }

    void ObserveSourcePhase()
    {
        const auto phase = m_phaseProvider();
        if (phase == "experiment_normal")
        {
            CommitSourceSchedule(phase); // One observed anchor; no further polls.
            return;
        }
        m_phaseEvent = Simulator::Schedule(MilliSeconds(20), &SourcePropagation::ObserveSourcePhase, this);
    }

    void SourceState(bool start)
    {
        if (start) ++m_activeSources;
        else if (m_activeSources) --m_activeSources;
        PublishSourceState();
    }

    void PublishSourceState()
    {
        std::ofstream output(m_config + ".state.tmp");
        output << "{\"enabled_sources\":" << m_activeSources << ",\"sim_time_s\":"
               << Simulator::Now().GetSeconds() << "}";
        output.close();
        std::filesystem::rename(m_config + ".state.tmp", m_config + ".state");
    }

    void StartSource(size_t index)
    {
        auto& source = m_schedule.at(index);
        SourceState(true);
        m_emit("jammer_on", source.id, source.power, source.details);
        source.generator->Start();
    }

    void StopSource(size_t index)
    {
        auto& source = m_schedule.at(index);
        SourceState(false);
        source.generator->Stop();
        m_emit("jammer_off", source.id, 0.0, source.details);
    }

    void CommitSourceSchedule(const std::string& phase)
    {
        namespace py = pybind11;
        py::gil_scoped_acquire gil;
        const auto now = Simulator::Now();
        const auto host = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        py::dict actual = m_python->materialize(m_python->resolved, now.GetNanoSeconds(), host, phase);
        py::list intervals = actual["segments"];
        NS_ABORT_MSG_IF(intervals.size() != m_schedule.size(), "source materialization changed segment count");
        auto destination = std::filesystem::path(m_config);
        destination.replace_filename(destination.stem().string() + "_actual.json");
        const auto temporary = destination.string() + ".tmp";
        std::ofstream output(temporary);
        output << m_python->json.attr("dumps")(actual, py::arg("indent") = 2,
            py::arg("allow_nan") = false).cast<std::string>() << '\n';
        output.close();
        if (!output) throw std::runtime_error("cannot publish actual source schedule");
        std::filesystem::rename(temporary, destination);
        m_emit("source_time_anchor", "ns3", now.GetSeconds(),
             "phase=" + phase + ";clock=ns3_simulation_seconds;actual=" + destination.string());
        for (size_t i = 0; i < m_schedule.size(); ++i)
        {
            auto interval = py::reinterpret_borrow<py::dict>(intervals[i]);
            const auto begin = NanoSeconds(interval["start_ns"].cast<int64_t>());
            const auto end = NanoSeconds(interval["stop_ns"].cast<int64_t>());
            NS_ABORT_MSG_IF(begin < Simulator::Now() || end <= begin, "invalid actual source interval");
            auto& source = m_schedule[i];
            source.generator->SetPeriod(NanoSeconds(interval["period_ns"].cast<int64_t>()));
            sourceTimes[source.generator][0] = begin.GetSeconds();
            sourceTimes[source.generator][1] = end.GetSeconds();
            source.binding.begin = begin;
            source.binding.end = end;
            source.binding.scheduleCommitted = true;
            source.begin = Simulator::Schedule(begin - Simulator::Now(), &SourcePropagation::StartSource, this, i);
            source.end = Simulator::Schedule(end - Simulator::Now(), &SourcePropagation::StopSource, this, i);
        }
    }

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
        ClearSourceSchedule();
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
    double maxPaths, const std::string& profile, EventSink emit,
    bool referenceUsesReceiverBatch = false,
    std::function<std::string()> phaseProvider = {})
{
    namespace py = pybind11;
    auto router = CreateObject<SourcePropagation>();
    router->reference = reference;
    router->profile = profile;
    auto text = py::module_::import("pathlib").attr("Path")(config).attr("read_text")();
    auto json = py::module_::import("json");
    py::dict resolved = json.attr("loads")(text);
    py::list entries = resolved["segments"];
    const auto clock = resolved["clock"].cast<std::string>();
    const bool relative = clock == "measurement_elapsed_seconds";
    NS_ABORT_MSG_IF(clock != "ns3_simulation_seconds" && !relative, "unsupported source clock");
    NS_ABORT_MSG_IF(relative && !phaseProvider, "measurement sources require a phase observer");
    NS_ABORT_MSG_IF(Time::GetResolution() != Time::NS, "source schedule requires native 1 ns resolution");
    // Preload project validation/materialization before Run/HardLimit. The
    // runner supplies ROOT in PYTHONPATH; no cold PyYAML import at measurement.
    py::object materialize = py::module_::import("scripts.product.prepare_native_sources").attr("materialize");
    router->ConfigureSourceSchedule(config, resolved, materialize, json, emit, std::move(phaseProvider));
    std::map<double, Ptr<SionnaRtSpectrumPropagationLossModel>> frequencies;
    if (reference && !referenceUsesReceiverBatch)
    {
        DoubleValue referenceFrequency;
        reference->GetChannelModelAttribute("Frequency", referenceFrequency);
        frequencies[referenceFrequency.Get()] = reference;
    }
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
        NS_ABORT_MSG_IF(profile == "friis" && pattern != "iso",
                        "native Friis source profile requires an isotropic element");
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
        if (profile != "friis" && !frequencies.count(hz))
        {
            NS_ABORT_MSG_IF(!reference, "RT sources require an explicit reference channel model");
            auto model = CreateObject<SionnaRtSpectrumPropagationLossModel>();
            // Emitters are outside the registered CP/UAV cohort. Their exact
            // propagation stays pairwise, including cochannel sources.
            model->SetChannelModelAttribute("ReceiverBatchEnabled", BooleanValue(false));
            model->SetChannelModelAttribute("Frequency", DoubleValue(hz));
            // A different carrier needs its own RT channel, with the same
            // bounded solver allocation as the configured desired link.
            for (const auto* attribute : {"SamplesPerSource", "MaxNumPathsPerSource"})
            {
                UintegerValue budget;
                reference->GetChannelModelAttribute(attribute, budget);
                model->SetChannelModelAttribute(attribute, budget);
            }
            model->SetChannelModelAttribute("Scenario", StringValue(scene));
            model->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(true));
            model->SetChannelModelAttribute("MaxNumberOfPaths", DoubleValue(maxPaths));
            model->SetRtPathSolverConfig(solver);
            frequencies[hz] = model;
        }
        if (profile != "friis")
        {
            auto selected = frequencies.at(hz);
            router->sources[generator] = selected;
            UintegerValue samples, paths;
            selected->GetChannelModelAttribute("SamplesPerSource", samples);
            selected->GetChannelModelAttribute("MaxNumPathsPerSource", paths);
            emit("source_rt_model_config", id, hz,
                 "samples_per_src=" + std::to_string(samples.Get()) +
                 ";max_num_paths_per_src=" + std::to_string(paths.Get()));
        }
        router->sourcePsds[generator] = psd;
        router->sourceTimes[generator] = {start, stop, period, duty};
        const auto details = "source=ns3::WaveformGenerator;center_hz=" + std::to_string(hz) +
            ";bandwidth_hz=" + std::to_string(bandwidth) + ";power_w=" + std::to_string(power) +
            ";gain_dbi=" + std::to_string(gain) + ";pattern=" + pattern +
            ";azimuth_rad=" + std::to_string(angles[0]) + ";downtilt_rad=" + std::to_string(angles[1]) +
            ";duty_cycle=" + std::to_string(duty) + ";period_s=" + std::to_string(period);
        SourceBinding binding;
        binding.id = id;
        binding.centerHz = hz;
        binding.bandwidthHz = bandwidth;
        binding.powerW = power;
        binding.feedGainDbi = gain;
        binding.pattern = pattern;
        binding.positionM = mobility->GetPosition();
        binding.velocityMps = mobility->GetVelocity();
        binding.orientationRad = {angles[0], angles[1], 0.0};
        binding.phy = generator;
        binding.array = array;
        router->AddSourceSchedule(generator, id, power, details, std::move(binding));
    }
    router->ObserveTransmissions(channel);
    router->ArmSourceSchedule(relative);
    return router;
}
} // namespace bas
