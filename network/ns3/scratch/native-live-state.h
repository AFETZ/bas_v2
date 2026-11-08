// A measured-state adapter, not another motion or propagation simulator.
#pragma once
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include "ns3/mobility-model.h"
#include "ns3/vector.h"
#include <algorithm>
#include <cmath>
#include <chrono>
#include <fstream>
#include <functional>
#include <map>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace bas {
using namespace ns3;
namespace py = pybind11;

struct MeasuredState {
    Vector position, velocity, orientation; // ENU m, ENU m/s, yaw/pitch/roll rad
    int64_t sampleNs{0};
    double sourceTime{0};
};

class MeasuredMobility : public MobilityModel {
public:
    static TypeId GetTypeId() {
        static TypeId tid = TypeId("bas::MeasuredMobility").SetParent<MobilityModel>()
            .AddConstructor<MeasuredMobility>()
            .AddAttribute("Orientation", "Measured body yaw/pitch/roll in ENU radians",
                VectorValue(), MakeVectorAccessor(&MeasuredMobility::m_orientation), MakeVectorChecker());
        return tid;
    }
    void Apply(const MeasuredState& state, bool notify=true) {
        m_position=state.position; m_velocity=state.velocity; m_orientation=state.orientation;
        if (notify) NotifyCourseChange();
    }
    void NotifyApplied() { NotifyCourseChange(); }
    Ptr<MobilityModel> Copy() const override { return CreateObject<MeasuredMobility>(*this); }
private:
    Vector DoGetPosition() const override { return m_position; }
    Vector DoGetVelocity() const override { return m_velocity; }
    void DoSetPosition(const Vector& p) override { m_position=p; NotifyCourseChange(); }
    Vector m_position, m_velocity, m_orientation;
};

// Host cutoff of the native event, independently from the readiness flag.
// Deferred RT frames use the native epoch as soon as Run starts. Warmup still
// uses the current host clock; the default immediate path keeps its old cutoff.
inline int64_t MeasuredEventHostNs(bool deferred, bool operational, int64_t radioEpochNs,
                                  int64_t nativeNowNs, int64_t wallNowNs) {
    if (!(deferred ? radioEpochNs>0 : operational)) return wallNowNs;
    if (radioEpochNs<0 || nativeNowNs<0 || radioEpochNs>std::numeric_limits<int64_t>::max()-nativeNowNs)
        throw std::runtime_error("invalid native/host pose clock mapping");
    return radioEpochNs+nativeNowNs;
}

inline uint64_t MaximumMeasuredPoseAgeNs(const std::vector<MeasuredState>& states, int64_t wallNs) {
    uint64_t maximum=0;
    for (const auto& state:states)
        if (state.sampleNs>=0 && state.sampleNs<=wallNs)
            maximum=std::max(maximum,static_cast<uint64_t>(wallNs-state.sampleNs));
    return maximum;
}

inline void ValidateMeasuredCohort(const std::vector<MeasuredState>& states, size_t expected,
                                 int64_t eventNs, int64_t wallNs, int64_t maxAgeNs,
                                 const std::vector<MeasuredState>* previous=nullptr) {
    if (!expected || states.size()!=expected || eventNs<0 || wallNs<0 || maxAgeNs<=0 ||
        (previous && !previous->empty() && previous->size()!=expected))
        throw std::runtime_error("incomplete measured pose cohort or invalid age domain");
    int64_t oldest=std::numeric_limits<int64_t>::max(), newest=0;
    for (size_t i=0;i<states.size();++i) {
        const auto& state=states[i];
        for (const auto& value:{state.position,state.velocity,state.orientation})
            if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
                throw std::runtime_error("nonfinite measured pose cohort");
        if (state.sampleNs<0 || state.sampleNs>eventNs || state.sampleNs>wallNs || wallNs-state.sampleNs>maxAgeNs)
            throw std::runtime_error("applied pose is stale or future to the native event/receipt");
        if (!std::isfinite(state.sourceTime) || state.sourceTime<0 ||
            state.sourceTime>=static_cast<double>(std::numeric_limits<int64_t>::max())/1e9)
            throw std::runtime_error("invalid measured source clock");
        if (previous && !previous->empty() && (state.sampleNs<(*previous)[i].sampleNs ||
                                               state.sourceTime<(*previous)[i].sourceTime))
            throw std::runtime_error("deferred measured frame would roll back an applied source epoch");
        if (i) {
            const auto sourceNs=std::llround(state.sourceTime*1e9);
            oldest=std::min(oldest,sourceNs); newest=std::max(newest,sourceNs);
        }
    }
    if (states.size()>1 && newest-oldest>100000000LL)
        throw std::runtime_error("measured cohort source skew exceeds100ms");
}

// Validate and allocate before mutating; notify only after every mobility,
// positions entry, original sample/clock epoch and synchronous dependents
// describe the same frame. A dependent failure propagates before notification.
inline void CommitMeasuredCohort(const std::vector<MeasuredState>& states,
    const std::vector<Ptr<MobilityModel>>& mobility, std::vector<Vector>& positions,
    std::vector<MeasuredState>& applied, int64_t eventNs, int64_t wallNs,
    int64_t maxAgeNs, bool deferred, const std::function<void()>& publishDependents={}) {
    ValidateMeasuredCohort(states,mobility.size(),eventNs,wallNs,maxAgeNs,deferred ? &applied : nullptr);
    if (positions.size()!=mobility.size()) throw std::runtime_error("measured position roster differs from mobility");
    std::vector<Ptr<MeasuredMobility>> receivers;
    for (const auto& model:mobility) {
        auto measured=DynamicCast<MeasuredMobility>(model);
        if (!measured) throw std::runtime_error("pose cohort contains a non-measured mobility model");
        receivers.push_back(measured);
    }
    auto original=states;
    for (size_t i=0;i<states.size();++i) {
        receivers[i]->Apply(states[i],false); positions[i]=states[i].position;
    }
    applied.swap(original);
    if (publishDependents) publishDependents();
    for (const auto& model:receivers) model->NotifyApplied();
}

inline Vector ReadVector(py::handle object, const char* key) {
    auto v = object[key].cast<std::vector<double>>();
    if (v.size()!=3 || !std::all_of(v.begin(),v.end(),[](double x){return std::isfinite(x);}))
        throw std::runtime_error(std::string("invalid vector: ")+key);
    return {v[0],v[1],v[2]};
}

inline MeasuredState DecodeState(py::handle object) {
    MeasuredState state;
    state.position=ReadVector(object,"position_m");
    state.velocity=ReadVector(object,"velocity_enu_mps");
    auto q=object["orientation_quat_xyzw"].cast<std::vector<double>>();
    if (q.size()!=4 || !std::all_of(q.begin(),q.end(),[](double x){return std::isfinite(x);}))
        throw std::runtime_error("invalid orientation");
    double norm=0;
    for (auto x:q) norm+=x*x;
    if (std::abs(norm-1)>1e-3) throw std::runtime_error("orientation is not normalized");
    const double x=q[0],y=q[1],z=q[2],w=q[3];
    state.orientation={std::atan2(2*(w*z+x*y),1-2*(y*y+z*z)),
        std::asin(std::clamp(2*(w*y-z*x),-1.,1.)),
        std::atan2(2*(w*x+y*z),1-2*(x*x+y*y))};
    return state;
}

class ClockAlignment {
public:
    void Check(double sourceS, double eventS, double maxDriftS) {
        if (!m_anchored) {
            m_anchored=true; m_sourceOrigin=sourceS; m_eventOrigin=eventS;
        }
        if (!std::isfinite(sourceS) || !std::isfinite(eventS) ||
            std::abs((sourceS-m_sourceOrigin)-(eventS-m_eventOrigin))>maxDriftS)
            throw std::runtime_error("Gazebo/ns-3 clock drift exceeds state budget");
    }
private:
    bool m_anchored{false};
    double m_sourceOrigin{0}, m_eventOrigin{0};
};

class LiveStateReader {
public:
    // The owner keeps this reader inside the interpreter lifetime and holds
    // the GIL during Read and destruction, as for the existing decoder calls.
    // Retain session identity: a tracker/clock reset requires a whole-run restart.
    std::vector<MeasuredState> Read(const std::string& path, const std::vector<std::string>& names,
                                   int64_t eventNs, int64_t wallNs, int64_t maxAgeNs,
                                   double sourceEventS = -1, int64_t publicationMaxAgeNs = 0) {
        const bool lockstep = sourceEventS >= 0;
        std::ifstream stream(path, std::ios::binary | std::ios::ate);
        const auto size=stream.tellg();
        if (size<=0 || size>1024*1024) throw std::runtime_error("missing/oversized state snapshot");
        stream.seekg(0);
        std::string json(static_cast<size_t>(size),'\0');
        if (!stream.read(json.data(),static_cast<std::streamsize>(json.size())) ||
            stream.peek()!=std::char_traits<char>::eof())
            throw std::runtime_error("incomplete/changing state snapshot");
        // Runtime callers sample receipt AFTER reading: the atomic publisher can
        // otherwise replace the file between a caller's clock sample and open().
        // A nonzero wallNs is reserved for controlled clock fixtures.
        if (wallNs==0) wallNs=std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        if (!m_parsed || json!=m_json) {
            auto parsed=py::module_::import("json").attr("loads")(json).cast<py::dict>();
            m_parsed=std::move(parsed);
            m_json=std::move(json);
        }
        // Reuse parsing only. Clock/session/schema validation and causal history
        // selection below run on every call, including an unchanged publication.
        auto root=py::reinterpret_borrow<py::dict>(m_parsed);
        const auto sourceKind=root["source"].cast<std::string>();
        if (root["schema_version"].cast<int>()!=2 || (sourceKind!="ros_odometry" && sourceKind!="gazebo_odometry")
            || root["coordinate_frame"].cast<std::string>()!="ENU" || !root["fault"].is_none())
            throw std::runtime_error("invalid state contract or source clock reset");
        if (lockstep && (!root.contains("simulation_mode") || root["simulation_mode"].cast<std::string>()!="lockstep"))
            throw std::runtime_error("tracker is using a different clock domain");
        const auto session=root["session_id"].cast<std::string>();
        if (!m_session.empty() && m_session!=session) throw std::runtime_error("tracker restarted");
        for (auto field:{"published_monotonic_ns","clock_received_monotonic_ns"}) {
            if (lockstep && std::string(field)=="clock_received_monotonic_ns") continue;
            auto stamp=root[field].cast<int64_t>();
            const auto hostBudget=lockstep && publicationMaxAgeNs>0 ? publicationMaxAgeNs : maxAgeNs;
            if (stamp>wallNs || wallNs-stamp>hostBudget) throw std::runtime_error(std::string("stale clock: ")+field);
        }
        std::map<std::string,py::dict> nodes;
        for (auto item:root["nodes"]) {
            auto node=py::reinterpret_borrow<py::dict>(item);
            if (!nodes.emplace(node["id"].cast<std::string>(),node).second)
                throw std::runtime_error("duplicate node identity");
        }
        std::vector<MeasuredState> states;
        // Choose a measured slice shared by independently delivered ROS streams.
        // If a history has a gap, move the window back until every source fits.
        int64_t sourceLimitNs=std::numeric_limits<int64_t>::max();
        for (;;) {
        states.clear();
        double oldest=1e100,newest=-1e100;
        for (const auto& name:names) {
            auto found=nodes.find(name);
            if (found==nodes.end()) throw std::runtime_error("missing node: "+name);
            auto node=found->second;
            if (node["role"].cast<std::string>()=="command_post") {
                auto state=DecodeState(node); state.sampleNs=eventNs; states.push_back(state); continue;
            }
            auto history=node["history"].cast<py::list>();
            if (history.size()>32) throw std::runtime_error("unbounded pose history");
            py::dict selected;
            int64_t selectedNs=-1;
            double selectedSource=-1;
            for (auto sample:history) {
                auto stamp=sample["sample_monotonic_ns"].cast<int64_t>();
                auto source=sample["source_sim_time_s"].cast<double>();
                if (!std::isfinite(source) || source<0) throw std::runtime_error("invalid source time");
                if (std::llround(source*1e9)>sourceLimitNs) continue;
                if ((lockstep ? std::llround(source*1e9)<=std::llround(sourceEventS*1e9)
                              : stamp<=eventNs) && source>selectedSource) {
                    selected=py::reinterpret_borrow<py::dict>(sample); selectedNs=stamp;
                    selectedSource=source;
                }
            }
            if (selectedNs<0 || (lockstep ? std::llround(sourceEventS*1e9)-std::llround(selectedSource*1e9)>maxAgeNs
                                         : wallNs-selectedNs>maxAgeNs))
                throw std::runtime_error("no timely causal pose for "+name);
            auto state=DecodeState(selected);
            state.sampleNs=selectedNs; state.sourceTime=selected["source_sim_time_s"].cast<double>();
            if (!std::isfinite(state.sourceTime)) throw std::runtime_error("invalid source time");
            oldest=std::min(oldest,state.sourceTime); newest=std::max(newest,state.sourceTime);
            states.push_back(state);
        }
        // Source stamps originate as integer ROS nanoseconds. Compare in that
        // domain so an exact 100 ms difference is not rejected by double rounding.
        if (states.size() && newest>=0 && std::llround(newest*1e9)-std::llround(oldest*1e9)>100000000LL) {
            sourceLimitNs=std::llround(oldest*1e9)+100000000LL;
            continue;
        }
        break;
        }
        m_session=session;
        m_lastPublicationNs=root["published_monotonic_ns"].cast<int64_t>();
        return states;
    }
    int64_t LastPublicationNs() const { return m_lastPublicationNs; }
private:
    std::string m_session;
    std::string m_json;
    py::object m_parsed;
    int64_t m_lastPublicationNs{0};
};
} // namespace bas
