// A measured-state adapter, not another motion or propagation simulator.
#pragma once
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include "ns3/mobility-model.h"
#include "ns3/vector.h"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
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
    void Apply(const MeasuredState& state) {
        m_position=state.position; m_velocity=state.velocity; m_orientation=state.orientation;
        NotifyCourseChange();
    }
    Ptr<MobilityModel> Copy() const override { return CreateObject<MeasuredMobility>(*this); }
private:
    Vector DoGetPosition() const override { return m_position; }
    Vector DoGetVelocity() const override { return m_velocity; }
    void DoSetPosition(const Vector& p) override { m_position=p; NotifyCourseChange(); }
    Vector m_position, m_velocity, m_orientation;
};

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
    // Retain session identity: a tracker/clock reset requires a whole-run restart.
    std::vector<MeasuredState> Read(const std::string& path, const std::vector<std::string>& names,
                                   int64_t eventNs, int64_t wallNs, int64_t maxAgeNs) {
        std::ifstream stream(path, std::ios::binary | std::ios::ate);
        const auto size=stream.tellg();
        if (size<=0 || size>1024*1024) throw std::runtime_error("missing/oversized state snapshot");
        stream.seekg(0);
        std::string json((std::istreambuf_iterator<char>(stream)),{});
        if (json.empty() || json.size()>1024*1024) throw std::runtime_error("missing/oversized state snapshot");
        auto root=py::module_::import("json").attr("loads")(json).cast<py::dict>();
        if (root["schema_version"].cast<int>()!=2 || root["source"].cast<std::string>()!="ros_odometry"
            || root["coordinate_frame"].cast<std::string>()!="ENU" || !root["fault"].is_none())
            throw std::runtime_error("invalid state contract or source clock reset");
        const auto session=root["session_id"].cast<std::string>();
        if (!m_session.empty() && m_session!=session) throw std::runtime_error("tracker restarted");
        for (auto field:{"published_monotonic_ns","clock_received_monotonic_ns"}) {
            auto stamp=root[field].cast<int64_t>();
            if (stamp>wallNs || wallNs-stamp>maxAgeNs) throw std::runtime_error(std::string("stale clock: ")+field);
        }
        std::map<std::string,py::dict> nodes;
        for (auto item:root["nodes"]) {
            auto node=py::reinterpret_borrow<py::dict>(item);
            if (!nodes.emplace(node["id"].cast<std::string>(),node).second)
                throw std::runtime_error("duplicate node identity");
        }
        std::vector<MeasuredState> states;
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
            for (auto sample:history) {
                auto stamp=sample["sample_monotonic_ns"].cast<int64_t>();
                if (stamp<=eventNs && stamp>selectedNs) {
                    selected=py::reinterpret_borrow<py::dict>(sample); selectedNs=stamp;
                }
            }
            if (selectedNs<0 || wallNs-selectedNs>maxAgeNs)
                throw std::runtime_error("no timely causal pose for "+name);
            auto state=DecodeState(selected);
            state.sampleNs=selectedNs; state.sourceTime=selected["source_sim_time_s"].cast<double>();
            if (!std::isfinite(state.sourceTime)) throw std::runtime_error("invalid source time");
            oldest=std::min(oldest,state.sourceTime); newest=std::max(newest,state.sourceTime);
            states.push_back(state);
        }
        if (newest-oldest>.1) throw std::runtime_error("cross-UAV source time skew exceeds 100 ms");
        m_session=session;
        return states;
    }
private:
    std::string m_session;
};
} // namespace bas
