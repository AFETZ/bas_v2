// A main-thread cache of externally computed native Sionna CIRs. No solver,
// Python, I/O, waiting, extrapolation or propagation formula runs on this path.
#pragma once
#include "ns3/double.h"
#include "ns3/isotropic-antenna-model.h"
#include "ns3/matrix-based-channel-model.h"
#include "ns3/mobility-model.h"
#include "ns3/node.h"
#include "ns3/simulator.h"
#include "ns3/sionna-rt-channel-model.h"
#include "ns3/three-gpp-antenna-model.h"
#include "ns3/uniform-planar-array.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <complex>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace bas
{
using namespace ns3;

class NativeRtCache : public MatrixBasedChannelModel
{
  public:
    struct Endpoint
    {
        Ptr<const MobilityModel> mobility;
        Ptr<const PhasedArrayModel> antenna;
    };
    struct Limits
    {
        Time maximumAge{Seconds(0.5)};
        double displacementM{0.5}, velocityDeltaMps{0.5}, orientationDeltaRad{5 * M_PI / 180};
    };
    struct Pose
    {
        Vector position, velocity, orientation; // ENU m, ENU m/s, yaw/pitch/roll radians.
        int64_t sampleHostNs{0};
    };
    struct Record
    {
        // Direction is the original matrix TX/RX order, not a sorted pair.
        std::array<size_t, 2> direction{0, 0};
        int64_t sampleNativeNs{0}, sampleHostMonotonicNs{0};
        uint64_t sourceRequestId{0};
        std::array<Pose, 2> poses;
        Ptr<const ChannelMatrix> matrix;
        Ptr<const SionnaRtChannelModel::SionnaRtChannelParams> parameters;
    };
    struct Publication
    {
        bool accepted;
        std::string reason;
    };
    struct PairStatus
    {
        size_t first, second;
        bool valid{false}, due{true};
        std::string reason{"missing"};
        uint64_t sourceRequestId{0};
        int64_t sampleNativeNs{-1}, sampleHostMonotonicNs{-1}, nativeAgeNs{-1}, hostAgeNs{-1};
        double displacementM{0}, velocityDeltaMps{0}, orientationDeltaRad{0}, normalizedRisk{1};
    };
    struct Stats
    {
        uint64_t hits{0}, misses{0}, accepted{0}, rejected{0};
        uint64_t getChannelCalls{0}, getChannelWallNs{0}, maxGetChannelWallNs{0};
        int64_t lastHitNativeAgeNs{-1}, lastHitHostAgeNs{-1}, maximumHitNativeAgeNs{0}, maximumHitHostAgeNs{0};
        std::map<std::string, uint64_t> rejectionReasons, missReasons;
    };

    static TypeId GetTypeId()
    {
        // No ObjectFactory constructor: an explicit, fixed cohort is required.
        static TypeId tid = TypeId("bas::NativeRtCache").SetParent<MatrixBasedChannelModel>()
            .AddAttribute("Frequency", "Immutable CIR reference carrier in Hz", TypeId::ATTR_GET,
                          DoubleValue(0), MakeDoubleAccessor(&NativeRtCache::GetFrequency),
                          MakeDoubleChecker<double>());
        return tid;
    }

    NativeRtCache(double frequencyHz, std::vector<Endpoint> endpoints, Limits limits)
        : m_frequencyHz(frequencyHz), m_endpoints(std::move(endpoints)), m_limits(limits), m_sourceStar(false)
    {
        ValidateEndpoints();
    }

    // Receiver indices retain their CP/UAV order. The external emitter is last;
    // only emitter/receiver pairs exist, never an implicit seven-node mesh.
    NativeRtCache(double frequencyHz, std::vector<Endpoint> receivers, Endpoint emitter, Limits limits)
        : m_frequencyHz(frequencyHz), m_endpoints(AppendEmitter(std::move(receivers), std::move(emitter))),
          m_limits(limits), m_sourceStar(true)
    {
        ValidateEndpoints();
    }

    double GetFrequency() const { return m_frequencyHz; }
    const Limits& GetLimits() const { return m_limits; }
    Stats GetStats() const { return m_stats; }
    static int64_t HostNowNs()
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }

    // Called only by the simulation thread after the owner converts a worker
    // result. Publication never changes a sample epoch to its completion time.
    Publication Publish(const Record& record)
    {
        const auto reject = [this](const std::string& reason) {
            ++m_stats.rejected; ++m_stats.rejectionReasons[reason];
            return Publication{false, reason};
        };
        if (m_disposed) return reject("disposed");
        const auto a = record.direction[0], b = record.direction[1];
        if (a >= m_endpoints.size() || b >= m_endpoints.size() || a == b)
            return reject("unknown_pair");
        if (!AllowedPair(a, b)) return reject("unsupported_pair");
        if (record.sourceRequestId == 0 || record.sampleNativeNs < 0 || record.sampleHostMonotonicNs <= 0)
            return reject("invalid_sample_identity");
        for (const auto& pose : record.poses)
            if (!Finite(pose) || pose.sampleHostNs <= 0 || pose.sampleHostNs > record.sampleHostMonotonicNs)
                return reject("invalid_sample_pose");
        const auto shapeError = ShapeError(record);
        if (!shapeError.empty()) return reject(shapeError);
        const auto key = Key(a, b);
        if (auto previous = m_entries.find(key); previous != m_entries.end())
        {
            const auto& old = previous->second.record;
            // Request IDs establish provenance, not a second clock. Worker
            // completion order never supersedes the original sample epochs.
            if (record.sourceRequestId == old.sourceRequestId || record.sampleNativeNs < old.sampleNativeNs ||
                record.sampleHostMonotonicNs < old.sampleHostMonotonicNs ||
                (record.sampleNativeNs == old.sampleNativeNs &&
                 record.sampleHostMonotonicNs == old.sampleHostMonotonicNs))
                return reject("out_of_order");
        }
        const auto status = Evaluate(a, b, record, HostNowNs());
        if (!status.valid) return reject(status.reason);

        // Freeze RF arrays so later producer mutation cannot modify a live CIR.
        Entry entry;
        entry.record = record;
        auto matrix = Create<ChannelMatrix>(*record.matrix);
        matrix->m_channel = Complex3DVector(record.matrix->m_channel);
        matrix->m_nodeIds = {NodeId(a), NodeId(b)};
        matrix->m_antennaPair = {m_endpoints[a].antenna->GetId(), m_endpoints[b].antenna->GetId()};
        auto parameters = Create<SionnaRtChannelModel::SionnaRtChannelParams>(*record.parameters);
        parameters->m_nodeIds = matrix->m_nodeIds;
        // Worker-local spectrum identities must never leak into native caches.
        parameters->m_cachedDelaySincos = ComplexMatrixArray();
        parameters->m_cachedRbWidth = 0;
        parameters->m_cachedSpectrumModelUid = 0;
        parameters->m_cachedCirCarrierHz = 0;
        entry.record.matrix = matrix;
        entry.record.parameters = parameters;
        m_entries[key] = std::move(entry);
        ++m_stats.accepted;
        return {true, "accepted"};
    }

    PairStatus PairState(size_t a, size_t b) const
    {
        if (m_disposed) return Missing(a, b, "disposed");
        CheckPair(a, b);
        auto found = m_entries.find(Key(a, b));
        return found == m_entries.end() ? Missing(a, b, "missing")
                                       : Evaluate(a, b, found->second.record, HostNowNs());
    }

    // Half-budget warnings are scheduling hints, never permission to use an
    // expired result. The caller owns in-flight jobs and the prefetch policy.
    std::vector<PairStatus> Priorities() const
    {
        std::vector<PairStatus> states;
        for (size_t a = 0; a < m_endpoints.size(); ++a)
            for (size_t b = a + 1; b < m_endpoints.size(); ++b)
                if (AllowedPair(a, b)) states.push_back(PairState(a, b));
        std::stable_sort(states.begin(), states.end(), [](const auto& a, const auto& b) {
            if (a.valid != b.valid) return !a.valid;
            return a.normalizedRisk > b.normalizedRisk;
        });
        return states;
    }

    Ptr<const ChannelMatrix> GetChannel(Ptr<const MobilityModel> a, Ptr<const MobilityModel> b,
                                       Ptr<const PhasedArrayModel> aa,
                                       Ptr<const PhasedArrayModel> ba) override
    {
        // Measure the actual packet callback, including refused lookups. This
        // separates cache/guard CPU cost from producer and pose-reader latency.
        const LookupTiming timing(m_stats);
        const auto ai = m_indices.find(PeekPointer(a)), bi = m_indices.find(PeekPointer(b));
        std::string error;
        PairStatus state{};
        if (m_disposed) error = "disposed";
        else if (ai == m_indices.end() || bi == m_indices.end() || ai->second == bi->second)
            error = "unknown_pair";
        else if (!AllowedPair(ai->second, bi->second)) error = "unsupported_pair";
        else if (aa != m_endpoints[ai->second].antenna || ba != m_endpoints[bi->second].antenna)
            error = "unknown_array";
        else
        {
            state = PairState(ai->second, bi->second);
            if (!state.valid) error = state.reason;
        }
        if (!error.empty())
        {
            ++m_stats.misses; ++m_stats.missReasons[error];
            throw std::runtime_error("RT cache unavailable: " + error);
        }
        ++m_stats.hits;
        m_stats.lastHitNativeAgeNs = state.nativeAgeNs;
        m_stats.lastHitHostAgeNs = state.hostAgeNs;
        m_stats.maximumHitNativeAgeNs = std::max(m_stats.maximumHitNativeAgeNs, state.nativeAgeNs);
        m_stats.maximumHitHostAgeNs = std::max(m_stats.maximumHitHostAgeNs, state.hostAgeNs);
        return m_entries.at(Key(ai->second, bi->second)).record.matrix;
    }

    // Metadata lookup matches the SDK contract. Consumers must acquire a valid
    // GetChannel first; a metadata lookup does not freshen or admit a CIR.
    Ptr<const ChannelParams> GetParams(Ptr<const MobilityModel> a, Ptr<const MobilityModel> b) const override
    {
        if (m_disposed) return nullptr;
        const auto ai = m_indices.find(PeekPointer(a)), bi = m_indices.find(PeekPointer(b));
        if (ai == m_indices.end() || bi == m_indices.end() || ai->second == bi->second) return nullptr;
        if (!AllowedPair(ai->second, bi->second)) return nullptr;
        auto found = m_entries.find(Key(ai->second, bi->second));
        return found == m_entries.end() ? nullptr : found->second.record.parameters;
    }

    void DoDispose() override
    {
        m_disposed = true;
        m_entries.clear(); m_indices.clear(); m_endpoints.clear(); m_nodeIds.clear();
        // Endpoints belong to the run, not this provider. Never Dispose them.
        MatrixBasedChannelModel::DoDispose();
    }

  private:
    struct LookupTiming
    {
        Stats& stats;
        const int64_t started{HostNowNs()};
        explicit LookupTiming(Stats& value) : stats(value) { ++stats.getChannelCalls; }
        ~LookupTiming()
        {
            const auto elapsed = static_cast<uint64_t>(HostNowNs() - started);
            stats.getChannelWallNs += elapsed;
            stats.maxGetChannelWallNs = std::max(stats.maxGetChannelWallNs, elapsed);
        }
    };
    struct Entry { Record record; };
    const double m_frequencyHz;
    std::vector<Endpoint> m_endpoints;
    const Limits m_limits;
    const bool m_sourceStar;
    bool m_emitterDirectional{false};
    bool m_disposed{false};
    std::map<const MobilityModel*, size_t> m_indices;
    std::vector<uint32_t> m_nodeIds;
    std::map<uint64_t, Entry> m_entries;
    Stats m_stats;

    static bool Positive(double value) { return std::isfinite(value) && value > 0; }
    static bool Finite(const Vector& v) { return std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z); }
    static bool Finite(const Pose& p) { return Finite(p.position) && Finite(p.velocity) && Finite(p.orientation); }
    static uint64_t Key(size_t a, size_t b) { return MatrixBasedChannelModel::GetKey(a, b); }
    uint32_t NodeId(size_t index) const { return m_nodeIds[index]; }
    static std::vector<Endpoint> AppendEmitter(std::vector<Endpoint> receivers, Endpoint emitter)
    {
        receivers.push_back(std::move(emitter));
        return receivers;
    }
    bool IsEmitter(size_t index) const { return m_sourceStar && index + 1 == m_endpoints.size(); }
    bool AllowedPair(size_t a, size_t b) const { return !m_sourceStar || IsEmitter(a) || IsEmitter(b); }
    void ValidateEndpoints()
    {
        if (!std::isfinite(m_frequencyHz) || m_frequencyHz <= 0 || Time::GetResolution() != Time::NS)
            throw std::invalid_argument("RT cache requires a positive carrier and native nanosecond time");
        const size_t minimum = m_sourceStar ? 3 : 2, maximum = m_sourceStar ? 7 : 6;
        if (m_endpoints.size() < minimum || m_endpoints.size() > maximum || m_limits.maximumAge <= Time(0) ||
            !Positive(m_limits.displacementM) || !Positive(m_limits.velocityDeltaMps) ||
            !Positive(m_limits.orientationDeltaRad))
            throw std::invalid_argument(m_sourceStar
                ? "RT source STAR requires 2..6 receivers and positive finite freshness limits"
                : "RT cache requires 2..6 endpoints and positive finite freshness limits");
        std::map<uint32_t, bool> nodeIds, antennaIds;
        for (size_t i = 0; i < m_endpoints.size(); ++i)
        {
            const auto& endpoint = m_endpoints[i];
            const auto node = endpoint.mobility ? endpoint.mobility->GetObject<Node>() : nullptr;
            if (!node || ArrayError(endpoint.antenna, IsEmitter(i)) != "" ||
                !m_indices.emplace(PeekPointer(endpoint.mobility), i).second ||
                !nodeIds.emplace(node->GetId(), true).second ||
                !antennaIds.emplace(endpoint.antenna->GetId(), true).second)
                throw std::invalid_argument(m_sourceStar
                    ? "RT cache requires unique iso V1x1 receivers and an iso/TR38901 OUTDOOR emitter"
                    : "RT cache requires unique attached iso 1x1 V 0dBi endpoints");
            if (!Finite(CurrentPose(i))) throw std::invalid_argument("RT cache endpoint has a nonfinite pose");
            m_nodeIds.push_back(node->GetId());
        }
        if (m_sourceStar) m_emitterDirectional =
            static_cast<bool>(DynamicCast<const ThreeGppAntennaModel>(m_endpoints.back().antenna->GetAntennaElement()));
    }
    void CheckPair(size_t a, size_t b) const
    {
        if (a >= m_endpoints.size() || b >= m_endpoints.size() || a == b)
            throw std::invalid_argument("RT cache pair is outside its fixed cohort");
        if (!AllowedPair(a, b)) throw std::invalid_argument("Receiver pairs are outside the source STAR");
    }
    static PairStatus Missing(size_t a, size_t b, const std::string& reason)
    {
        PairStatus result{};
        result.first = a; result.second = b; result.reason = reason;
        return result;
    }
    static std::string ArrayError(Ptr<const PhasedArrayModel> value, bool emitter = false)
    {
        auto array = DynamicCast<const UniformPlanarArray>(value);
        if (!array || array->GetNumRows() != 1 || array->GetNumColumns() != 1 ||
            array->GetNumElems() != 1 || array->GetNumPorts() != 1 || array->GetNumPols() != 1 ||
            array->GetPolSlant() != 0)
            return "unsupported_array";
        if (DynamicCast<const IsotropicAntennaModel>(array->GetAntennaElement()))
        {
            DoubleValue gain;
            array->GetAntennaElement()->GetAttribute("Gain", gain);
            if (gain.Get() != 0) return "unsupported_array_gain";
        }
        else
        {
            const auto directional = DynamicCast<const ThreeGppAntennaModel>(array->GetAntennaElement());
            if (!emitter || !directional) return "unsupported_array";
            if (directional->GetRadiationPattern() != ThreeGppAntennaModel::RadiationPattern::OUTDOOR ||
                directional->GetVerticalBeamwidth() != 65 || directional->GetHorizontalBeamwidth() != 65 ||
                directional->GetMaxAttenuation() != 30 || directional->GetSlaV() != 30 ||
                directional->GetAntennaElementGain() != 8)
                return "unsupported_emitter_pattern";
        }
        // As in the native spectrum model, the run must initialize its beam
        // vector before handing the array to a channel provider.
        const auto& weights = array->GetBeamformingVectorRef();
        if (weights.GetSize() != 1 || weights[0] != std::complex<double>(1, 0))
            return "unsupported_beamformer";
        return "";
    }
    Pose CurrentPose(size_t index) const
    {
        const auto& endpoint = m_endpoints[index];
        VectorValue orientation;
        const auto array = DynamicCast<const UniformPlanarArray>(endpoint.antenna);
        return {endpoint.mobility->GetPosition(), endpoint.mobility->GetVelocity(),
                endpoint.mobility->GetAttributeFailSafe("Orientation", orientation) ? orientation.Get()
                    : Vector(array->GetAlpha(), array->GetBeta(), 0), 0};
    }
    static std::string ShapeError(const Record& record)
    {
        if (!record.matrix || !record.parameters) return "missing_cir";
        if (record.matrix->m_generatedTime.GetNanoSeconds() != record.sampleNativeNs ||
            record.parameters->m_generatedTime.GetNanoSeconds() != record.sampleNativeNs)
            return "generation_epoch_mismatch";
        const auto& matrix = record.matrix->m_channel;
        const auto paths = matrix.GetNumPages();
        const auto& parameters = *record.parameters;
        if (matrix.GetNumRows() != 1 || matrix.GetNumCols() != 1 || paths > 90 ||
            parameters.m_delay.size() != paths || parameters.m_doppler.size() != paths ||
            parameters.m_angle.size() != 4)
            return "invalid_cir_shape";
        for (const auto& angles : parameters.m_angle)
            if (angles.size() != paths) return "invalid_angle_shape";
        for (size_t i = 0; i < paths; ++i)
        {
            const auto coefficient = matrix(0, 0, i);
            const auto delay = parameters.m_delay[i];
            if (!std::isfinite(coefficient.real()) || !std::isfinite(coefficient.imag()) ||
                !std::isfinite(delay) || !std::isfinite(parameters.m_doppler[i])) return "nonfinite_cir";
            const bool zero = coefficient == std::complex<double>(0, 0);
            if (delay < 0 && !(zero && delay == -1)) return "invalid_path_delay";
            for (const auto& angles : parameters.m_angle)
                if (!std::isfinite(angles[i])) return "nonfinite_angle";
        }
        return "";
    }
    PairStatus Evaluate(size_t a, size_t b, const Record& record, int64_t hostNow) const
    {
        auto result = Missing(a, b, "");
        result.sourceRequestId = record.sourceRequestId;
        result.sampleNativeNs = record.sampleNativeNs;
        result.sampleHostMonotonicNs = record.sampleHostMonotonicNs;
        const auto nativeNow = Simulator::Now().GetNanoSeconds();
        if (record.sampleNativeNs > nativeNow) { result.reason = "future_native_sample"; return result; }
        if (record.sampleHostMonotonicNs > hostNow) { result.reason = "future_host_sample"; return result; }
        const auto oldestPose = std::min(record.poses[0].sampleHostNs, record.poses[1].sampleHostNs);
        result.nativeAgeNs = nativeNow - record.sampleNativeNs;
        result.hostAgeNs = hostNow - std::min(record.sampleHostMonotonicNs, oldestPose);
        result.normalizedRisk = std::max(double(result.nativeAgeNs), double(result.hostAgeNs)) /
                                m_limits.maximumAge.GetNanoSeconds();
        if (result.nativeAgeNs > m_limits.maximumAge.GetNanoSeconds()) result.reason = "native_sample_expired";
        else if (result.hostAgeNs > m_limits.maximumAge.GetNanoSeconds()) result.reason = "host_sample_expired";
        for (size_t side = 0; side < 2; ++side)
        {
            const auto index = record.direction[side];
            const auto node = m_endpoints[index].mobility->GetObject<Node>();
            if (!node || node->GetId() != m_nodeIds[index])
            { result.reason = "endpoint_identity_changed"; return result; }
            const auto arrayError = ArrayError(m_endpoints[index].antenna, IsEmitter(index));
            if (!arrayError.empty()) { result.reason = arrayError; return result; }
            if (IsEmitter(index) && m_emitterDirectional !=
                static_cast<bool>(DynamicCast<const ThreeGppAntennaModel>(m_endpoints[index].antenna->GetAntennaElement())))
            { result.reason = "emitter_pattern_changed"; return result; }
            const auto current = CurrentPose(index);
            if (!Finite(current)) { result.reason = "nonfinite_current_pose"; return result; }
            const auto& sample = record.poses[side];
            const auto position = current.position - sample.position;
            const auto velocity = current.velocity - sample.velocity;
            const double distance = std::hypot(position.x, position.y, position.z);
            const double speed = std::hypot(velocity.x, velocity.y, velocity.z);
            const auto angle = [](double now, double then) {
                return std::abs(std::remainder(std::remainder(now, 2 * M_PI) -
                                              std::remainder(then, 2 * M_PI), 2 * M_PI));
            };
            const double rotation = std::max({angle(current.orientation.x, sample.orientation.x),
                angle(current.orientation.y, sample.orientation.y), angle(current.orientation.z, sample.orientation.z)});
            if (!std::isfinite(distance) || !std::isfinite(speed))
            { result.reason = "unrepresentable_pose_delta"; return result; }
            result.displacementM = std::max(result.displacementM, distance);
            result.velocityDeltaMps = std::max(result.velocityDeltaMps, speed);
            result.orientationDeltaRad = std::max(result.orientationDeltaRad, rotation);
        }
        result.normalizedRisk = std::max({result.normalizedRisk,
            result.displacementM / m_limits.displacementM,
            result.velocityDeltaMps / m_limits.velocityDeltaMps,
            result.orientationDeltaRad / m_limits.orientationDeltaRad});
        if (result.reason.empty())
        {
            if (result.displacementM > m_limits.displacementM) result.reason = "endpoint_displaced";
            else if (result.velocityDeltaMps >= m_limits.velocityDeltaMps) result.reason = "endpoint_velocity_changed";
            else if (result.orientationDeltaRad >= m_limits.orientationDeltaRad) result.reason = "endpoint_orientation_changed";
        }
        result.valid = result.reason.empty();
        result.due = !result.valid || result.normalizedRisk >= 0.5;
        if (result.valid) result.reason = "valid";
        return result;
    }
};
} // namespace bas
