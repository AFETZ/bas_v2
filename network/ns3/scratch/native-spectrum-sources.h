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
    // Known measurement-relative offset; null means the unanchored start is
    // unknown. This is a lower bound after a future anchor, not a native epoch.
    std::optional<Time> preanchorStartOffset;

    bool RequiredAt(Time now, Time lookahead = NanoSeconds(0)) const
    {
        if (now < NanoSeconds(0) || lookahead < NanoSeconds(0))
            throw std::invalid_argument("source requirement times must be nonnegative");
        if (!scheduleCommitted)
        {
            if (preanchorStartOffset && *preanchorStartOffset < NanoSeconds(0))
                throw std::invalid_argument("pre-anchor source offset must be nonnegative");
            // An unobserved measurement anchor will be Simulator::Now() when
            // first observed. A known larger offset cannot enter this horizon.
            // Unknown/immediate starts stay conservative; cold prime is separate.
            return !preanchorStartOffset || *preanchorStartOffset <= lookahead;
        }
        if (!begin || !end || *begin < NanoSeconds(0) || *end <= *begin)
            throw std::logic_error("committed source requires a valid actual interval");
        // Keep the entire [begin,end) interval, including duty-cycle holes. This
        // is a cache requirement, not a statement that the transmitter is on.
        // Subtraction avoids overflowing now+lookahead near the native limit.
        return now < *end &&
               (now >= *begin || *begin - now <= lookahead);
    }
};

// Select exactly one upstream propagation model per transmission. No loss formula,
// packet-error decision is implemented here. Hybrid uses a fixed qualified domain
// and an explicit RT fallback; it retains the existing RT cache/pose guards.
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
    // Set by the existing run owner from its immutable CP/UAV roster.
    // Coordinates alone never assign an endpoint the command-post role.
    Ptr<const MobilityModel> hybridCommandPost;
    Ptr<FriisSpectrumPropagationLossModel> freeSpace{CreateObject<FriisSpectrumPropagationLossModel>()};
    std::map<Ptr<SpectrumPhy>, Ptr<SionnaRtSpectrumPropagationLossModel>> sources;
    std::map<Ptr<SpectrumPhy>, Ptr<SpectrumValue>> sourcePsds;
    std::map<Ptr<SpectrumPhy>, std::array<double, 4>> sourceTimes;

    static void ValidateProfile(const std::string& value)
    {
        if (value != "sionna" && value != "friis" && value != "hybrid")
            throw std::invalid_argument("source propagation profile must be sionna, friis or hybrid");
    }


    // Fixed policy, loaded once before Simulator::Run. It is never inferred
    // from endpoint coordinates or from one under-sampled RT path.
    void ConfigureHybridScene(const std::string& scene, EventSink emit = {})
    {
        if (m_hybridConfigured) throw std::logic_error("hybrid scene is immutable for one run");
        pybind11::gil_scoped_acquire gil;
        auto evidence = pybind11::module_::import("scripts.product.hybrid_policy")
            .attr("scene_evidence")(scene).cast<pybind11::dict>();
        if (evidence["policy_id"].cast<std::string>() != "qualified_siso_psd_2412_v2")
            throw std::runtime_error("unknown hybrid qualification policy");
        m_hybridSceneSha = evidence["scene_sha256"].cast<std::string>();
        m_hybridShapeCount = evidence["shape_count"].cast<size_t>();
        m_hybridObjectCount = evidence["object_count"].cast<size_t>();
        m_hybridEmpty = evidence["qualified_empty_scene"].cast<bool>();
        m_hybridFlat = evidence["qualified_flat_ground_scene"].cast<bool>();
        if (m_hybridSceneSha.size() != 64 ||
            m_hybridSceneSha.find_first_not_of("0123456789abcdef") != std::string::npos ||
            (m_hybridEmpty && (m_hybridShapeCount || m_hybridObjectCount)) ||
            (m_hybridFlat && (m_hybridEmpty || m_hybridShapeCount!=1 || m_hybridObjectCount!=1 ||
                m_hybridSceneSha!="905807072f010dbca786a3842919de50d1d3988656bb08400cb428c1166acdde")))
            throw std::runtime_error("invalid parsed hybrid geometry evidence");
        m_hybridEmit = std::move(emit);
        m_hybridConfigured = true;
    }

    uint64_t HybridRtEvaluations() const { return m_hybridRt; }
    uint64_t HybridFriisEvaluations() const { return m_hybridFriis; }
    std::string HybridSnapshotJson() const
    {
        if (!m_hybridConfigured) return "null";
        std::ostringstream out;
        out << "{\"policy_id\":\"qualified_siso_psd_2412_v2\","
            << "\"scope\":\"SISO_PSD_only;empty_static_or_exact_flat_ground_online_guard;RT_producer_and_freshness_retained;no_speedup_claim\","
            << "\"selection_cache\":\"none;inspect_current_pair_antennas_spectrum_velocity_each_signal\","
            << "\"scene_sha256\":\"" << m_hybridSceneSha << "\","
            << "\"actual_shape_count\":" << m_hybridShapeCount << ",\"actual_object_count\":" << m_hybridObjectCount
            << ",\"qualified_empty_scene\":" << (m_hybridEmpty ? "true" : "false")
            << ",\"qualified_flat_ground_scene\":" << (m_hybridFlat ? "true" : "false")
            << ",\"online_guard_limit_db\":1.0,\"ground_reference_psd_evaluations\":" << m_hybridGroundReferences
            << ",\"ground_guard_passes\":" << m_hybridGroundPasses
            << ",\"ground_guard_fallbacks\":" << m_hybridGroundFallbacks
            << ",\"ground_max_observed_abs_error_db\":" << m_hybridGroundMaxErrorDb
            << ",\"ground_max_admitted_abs_error_db\":" << m_hybridGroundMaxAdmittedErrorDb
            << ",\"rt_producer_retained\":true,\"rt_psd_evaluations\":" << m_hybridRt
            << ",\"friis_psd_evaluations\":" << m_hybridFriis
            << ",\"guarded_friis_evaluations\":" << m_hybridGuarded
            << ",\"reference_guard_failures\":" << m_hybridFailures << ",\"links\":[";
        bool first = true;
        for (const auto& [key, link] : m_hybridLinks)
        {
            if (!first) out << ',';
            first = false;
            out << "{\"tx_node_id\":" << key.first << ",\"rx_node_id\":" << key.second
                << ",\"rt_psd_evaluations\":" << link.rt << ",\"friis_psd_evaluations\":" << link.friis
                << ",\"selection_transitions\":" << link.transitions << ",\"last_native_ns\":" << link.lastNs
                << ",\"last_model\":\"" << link.model << "\",\"last_reason\":\"" << link.reason << "\"}";
        }
        out << "]}"; return out.str();
    }

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
            m_hybridEmit = {};
        }
        m_schedule.clear();
    }

  private:

    struct HybridLink
    {
        uint64_t rt{0}, friis{0}, transitions{0};
        int64_t lastNs{0};
        std::string model, reason;
    };
    bool m_hybridConfigured{false}, m_hybridEmpty{false}, m_hybridFlat{false};
    size_t m_hybridShapeCount{0}, m_hybridObjectCount{0};
    std::string m_hybridSceneSha;
    EventSink m_hybridEmit;
    mutable uint64_t m_hybridRt{0}, m_hybridFriis{0}, m_hybridGuarded{0}, m_hybridFailures{0};
    mutable std::map<std::pair<uint32_t,uint32_t>,HybridLink> m_hybridLinks;
    mutable uint64_t m_hybridGroundReferences{0}, m_hybridGroundPasses{0}, m_hybridGroundFallbacks{0};
    mutable double m_hybridGroundMaxErrorDb{0}, m_hybridGroundMaxAdmittedErrorDb{0};

    static bool QualifiedHybridArray(Ptr<const PhasedArrayModel> array)
    {
        auto upa = DynamicCast<const UniformPlanarArray>(array);
        auto iso = upa ? DynamicCast<const IsotropicAntennaModel>(upa->GetAntennaElement()) : nullptr;
        if (!upa || !iso || upa->GetNumRows()!=1 || upa->GetNumColumns()!=1 ||
            upa->GetNumPorts()!=1 || upa->GetNumElems()!=1 || upa->GetNumPols()!=1 ||
            upa->GetPolSlant()!=0 || upa->GetAlpha()!=0 || upa->GetBeta()!=0) return false;
        DoubleValue gain; iso->GetAttribute("Gain",gain);
        if (gain.Get()!=0) return false;
        const auto& weights = upa->GetBeamformingVectorRef();
        return weights.GetSize()==1 && weights[0]==std::complex<double>(1.0,0.0);
    }
    std::string HybridReason(Ptr<const SpectrumSignalParameters> params,
        Ptr<const MobilityModel> a, Ptr<const MobilityModel> b,
        Ptr<const PhasedArrayModel> aa, Ptr<const PhasedArrayModel> ba,
        Ptr<SionnaRtSpectrumPropagationLossModel> selected) const
    {
        if (!m_hybridConfigured) throw std::logic_error("hybrid scene qualification was not configured");
        if (!selected || !selected->GetChannelModel())
            throw std::runtime_error("hybrid requires an available explicit RT reference");
        if (!m_hybridEmpty && !m_hybridFlat) return "unqualified_geometry";
        if (m_hybridFlat)
        {
            if (!QualifiedHybridArray(aa) || !QualifiedHybridArray(ba)) return "unqualified_antenna";
            DoubleValue carrier; selected->GetChannelModel()->GetAttribute("Frequency",carrier);
            if (carrier.Get()!=2412000000.0) return "unqualified_carrier";
            for (auto band=params->psd->ConstBandsBegin(); band!=params->psd->ConstBandsEnd(); ++band)
                if (!std::isfinite(band->fc) || std::abs(band->fc-carrier.Get())>30000000.0)
                    return "unqualified_spectrum_bins";
            // External source STAR endpoints are not the run's UAV cohort.
            if (sources.find(params->txPhy)!=sources.end()) return "unqualified_ground_endpoint_role";
            if (!hybridCommandPost || (a!=hybridCommandPost && b!=hybridCommandPost))
                return "unqualified_ground_endpoint_role";
            const auto cp=a==hybridCommandPost ? a : b;
            const auto uav=a==hybridCommandPost ? b : a;
            const auto orientation=[](Ptr<const MobilityModel> endpoint) {
                VectorValue value;
                return endpoint->GetAttributeFailSafe("Orientation",value) ? value.Get() : Vector{};
            };
            const auto c=cp->GetPosition(), u=uav->GetPosition();
            const auto co=orientation(cp), uo=orientation(uav);
            const auto finite=[](const Vector& v) {
                return std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z);
            };
            if (!finite(c) || !finite(u) || !finite(co) || !finite(uo) ||
                c!=Vector(0,0,2) || co!=Vector{} || u.z<19.5 || u.z>20.5 ||
                std::abs(uo.y)>2*M_PI/180 || std::abs(uo.z)>2*M_PI/180)
                return "unqualified_ground_pose";
            if (!finite(cp->GetVelocity()) || !finite(uav->GetVelocity()) ||
                cp->GetVelocity()!=Vector{} || uav->GetVelocity().GetLength()>3)
                return "unqualified_ground_velocity";
            const double theta=std::atan2(std::hypot(u.x,u.y),c.z+u.z)*180/M_PI;
            if (theta<73.90006776148272 || theta>75.90006776148272)
                return "unqualified_ground_pose";
            // Current pose is sampled, not proof of continuous attitude or
            // angular speed. Actual RT PSD must additionally admit every signal.
            return "ground_candidate_online_guard";
        }
        // The production pool/cache uses a measured body Orientation attribute
        // before the UPA bearing/downtilt fallback. Do not silently substitute
        // co-polar Friis for a rolled/pitched or otherwise unqualified RT array.
        for (const auto& endpoint : {a,b})
        {
            VectorValue orientation;
            if (endpoint->GetAttributeFailSafe("Orientation",orientation))
            {
                const auto value=orientation.Get();
                if (value.x!=0 || value.y!=0 || value.z!=0) return "unqualified_orientation";
            }
        }
        if (!QualifiedHybridArray(aa) || !QualifiedHybridArray(ba)) return "unqualified_antenna";
        if (a->GetVelocity().GetLength()!=0 || b->GetVelocity().GetLength()!=0) return "moving_endpoint";
        const double distance = CalculateDistance(a->GetPosition(),b->GetPosition());
        if (!std::isfinite(distance) || distance<2 || distance>10000) return "unqualified_distance";
        DoubleValue carrier; selected->GetChannelModel()->GetAttribute("Frequency",carrier);
        if (carrier.Get()!=2412000000.0) return "unqualified_carrier";
        for (auto band=params->psd->ConstBandsBegin(); band!=params->psd->ConstBandsEnd(); ++band)
            if (!std::isfinite(band->fc) || std::abs(band->fc-carrier.Get())>30000000.0)
                return "unqualified_spectrum_bins";
        // Query the existing model/cache before the approximation. Missing,
        // aged or mismatched CIR/pose epochs fail exactly as in the RT path.
        Ptr<const MatrixBasedChannelModel::ChannelMatrix> matrix;
        try { matrix=selected->GetChannelModel()->GetChannel(a,b,aa,ba); }
        catch (...) { ++m_hybridFailures; throw; }
        if (!matrix) { ++m_hybridFailures; throw std::runtime_error("hybrid RT reference returned no matrix"); }
        if (matrix->m_channel.GetNumRows()!=1 || matrix->m_channel.GetNumCols()!=1 ||
            matrix->m_channel.GetNumPages()!=1 || matrix->m_channel(0,0,0)==std::complex<double>(0,0))
            return "reference_not_single_nonzero_path";
        return "qualified_empty_static_siso_psd";
    }
    Ptr<SpectrumSignalParameters> GroundHybridPsd(
        Ptr<const SpectrumSignalParameters> params, Ptr<const MobilityModel> a,
        Ptr<const MobilityModel> b, Ptr<const PhasedArrayModel> aa,
        Ptr<const PhasedArrayModel> ba, Ptr<SionnaRtSpectrumPropagationLossModel> selected) const
    {
        // Reuse native RT's coherent paths, Doppler, cached delay phases and
        // original epoch guards. No CIR formula or second attenuation is added.
        Ptr<SpectrumSignalParameters> rt;
        ++m_hybridGroundReferences;
        try { rt=selected->CalcRxPowerSpectralDensity(params,a,b,aa,ba); }
        catch (...) { ++m_hybridFailures; throw; }
        auto candidate=params->Copy();
        candidate->psd=freeSpace->CalcRxPowerSpectralDensity(params,a,b);
        const auto invalidReference=[this](const std::string& why) {
            ++m_hybridFailures;
            throw std::runtime_error("hybrid RT PSD unavailable: "+why);
        };
        if (!rt || !rt->psd || !candidate->psd ||
            rt->psd->GetSpectrumModel()!=params->psd->GetSpectrumModel() ||
            candidate->psd->GetSpectrumModel()!=params->psd->GetSpectrumModel())
            invalidReference("missing_or_mismatched_spectrum");
        auto r=rt->psd->ConstValuesBegin(), f=candidate->psd->ConstValuesBegin();
        bool positive=false;
        double comparisonMaximumDb=0;
        std::string fallback;
        for (auto input=params->psd->ConstValuesBegin();input!=params->psd->ConstValuesEnd();++input,++r,++f)
        {
            if (!std::isfinite(*input) || *input<0 || !std::isfinite(*r) || *r<0)
                invalidReference("nonfinite_or_negative_power");
            if (!std::isfinite(*f) || *f<0)
            { if (fallback.empty()) fallback="ground_candidate_invalid"; continue; }
            if (*input==0) continue;
            positive=true;
            if (*r==0) { if (fallback.empty()) fallback="ground_reference_zero"; continue; }
            if (*f==0) { if (fallback.empty()) fallback="ground_candidate_invalid"; continue; }
            // Separate logs avoid ratio overflow while keeping the same dB test.
            const double difference=10*std::abs(std::log10(*f)-std::log10(*r));
            m_hybridGroundMaxErrorDb=std::max(m_hybridGroundMaxErrorDb,difference);
            comparisonMaximumDb=std::max(comparisonMaximumDb,difference);
            if (difference>1 && fallback.empty()) fallback="ground_rss_budget_exceeded";
        }
        if (!positive && fallback.empty()) fallback="ground_no_positive_input";
        if (!fallback.empty())
        {
            ++m_hybridGroundFallbacks;
            RecordHybrid(a,b,"sionna",fallback);
            return rt;
        }
        ++m_hybridGroundPasses;
        m_hybridGroundMaxAdmittedErrorDb=std::max(m_hybridGroundMaxAdmittedErrorDb,comparisonMaximumDb);
        RecordHybrid(a,b,"friis","qualified_flat_ground_online_guard");
        return candidate;
    }
    void RecordHybrid(Ptr<const MobilityModel> a, Ptr<const MobilityModel> b,
                      const std::string& model, const std::string& reason) const
    {
        const auto tx=a->GetObject<Node>(), rx=b->GetObject<Node>();
        if (!tx || !rx) throw std::runtime_error("hybrid links require actual registered ns-3 nodes");
        auto& link=m_hybridLinks[{tx->GetId(),rx->GetId()}];
        if (link.model!=model || link.reason!=reason)
        {
            ++link.transitions;
            if (m_hybridEmit) m_hybridEmit("hybrid_model_selection",
                "tx"+std::to_string(tx->GetId())+"_rx"+std::to_string(rx->GetId()),0,
                "selected_model="+model+";reason="+reason+";policy=qualified_siso_psd_2412_v2");
        }
        link.model=model; link.reason=reason; link.lastNs=Simulator::Now().GetNanoSeconds();
        if (model=="friis") { ++link.friis; ++m_hybridFriis; ++m_hybridGuarded; }
        else { ++link.rt; ++m_hybridRt; }
    }

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
        // Validate every dispatch as profile is also assigned directly by the
        // low-level runner. An unavailable/unknown model must never select Friis.
        ValidateProfile(profile);
        if (profile == "friis")
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
        if (profile == "hybrid")
        {
            const auto reason = HybridReason(params,a,b,aa,ba,selected);
            if (reason == "ground_candidate_online_guard")
                return GroundHybridPsd(params,a,b,aa,ba,selected);
            if (reason == "qualified_empty_static_siso_psd")
            {
                auto result = params->Copy();
                result->psd = freeSpace->CalcRxPowerSpectralDensity(params,a,b);
                RecordHybrid(a,b,"friis",reason);
                return result;
            }
            auto result = selected->CalcRxPowerSpectralDensity(params,a,b,aa,ba);
            RecordHybrid(a,b,"sionna",reason);
            return result;
        }
        if (!selected) throw std::runtime_error("explicit RT propagation model is unavailable");
        return selected->CalcRxPowerSpectralDensity(params, a, b, aa, ba);
    }
    void DoDispose() override
    {
        ClearSourceSchedule();
        sources.clear();
        sourcePsds.clear();
        sourceTimes.clear();
        reference = nullptr;
        hybridCommandPost = nullptr;
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
    // Reject before reading source files, allocating devices or configuring RT.
    SourcePropagation::ValidateProfile(profile);
    namespace py = pybind11;
    auto router = CreateObject<SourcePropagation>();
    router->reference = reference;
    router->profile = profile;
    if (profile == "hybrid") router->ConfigureHybridScene(scene,emit);
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
        if (relative)
        {
            // Same integer-ns rounding as prepare_native_sources.materialize,
            // without inventing an anchor or materializing an actual schedule.
            const double ticks = std::floor(start * 1e9 + .5);
            if (!std::isfinite(ticks) || ticks < 0 || ticks >= std::ldexp(1., 63))
                throw std::invalid_argument("source offset exceeds native signed-64-bit nanoseconds");
            binding.preanchorStartOffset = NanoSeconds(static_cast<int64_t>(ticks));
        }
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
