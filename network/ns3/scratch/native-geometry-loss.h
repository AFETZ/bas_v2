// Adapter only: upstream ns-3 owns intersection, propagation and packet outcomes.
#pragma once
#include "ns3/building.h"
#include "ns3/buildings-channel-condition-model.h"
#include "ns3/mobility-building-info.h"
#include "ns3/propagation-loss-model.h"
#include "ns3/simulator.h"
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <map>
#include <string>
#include <vector>

namespace bas {
using namespace ns3;
namespace py = pybind11;

class GeometryLogDistance : public PropagationLossModel {
public:
    static TypeId GetTypeId() {
        static TypeId tid = TypeId("bas::GeometryLogDistance")
            .SetParent<PropagationLossModel>().AddConstructor<GeometryLogDistance>();
        return tid;
    }
    // JSON parsing uses Python stdlib only, never Sionna or a solver.
    void Load(const std::string& path, double carrierHz, const std::string& csvPath) {
        auto config=py::module_::import("json").attr("loads")(
            py::module_::import("pathlib").attr("Path")(path).attr("read_text")()).cast<py::dict>();
        auto radio=config["radio"].cast<py::dict>();
        parameterSet=radio["parameter_set"].cast<std::string>();
        mode=radio["mode"].cast<std::string>();
        frequencyHz=radio["frequency_hz"].cast<double>();
        NS_ABORT_MSG_IF(parameterSet.empty() || parameterSet.find_first_of(",\n\r\"")!=std::string::npos,
                        "invalid geometry parameter_set");
        NS_ABORT_MSG_IF(mode!="geometry" && mode!="los_only", "invalid geometry radio mode");
        NS_ABORT_MSG_IF(!std::isfinite(frequencyHz) || std::abs(frequencyHz-carrierHz)>.5,
                        "geometry model reference frequency differs from actual carrier");
        for (auto entry : {std::pair{"los", m_los}, std::pair{"nlos", m_nlos}}) {
            auto p=radio[entry.first].cast<py::dict>();
            const double exponent=p["exponent"].cast<double>();
            const double distance=p["reference_distance_m"].cast<double>();
            const double loss=p["reference_loss_db"].cast<double>();
            NS_ABORT_MSG_IF(!std::isfinite(exponent) || exponent<=0 ||
                            !std::isfinite(distance) || distance<=0 || !std::isfinite(loss) || loss<0,
                            "invalid explicit LogDistance parameters");
            entry.second->SetPathLossExponent(exponent);
            entry.second->SetReference(distance,loss);
        }
        for (auto obj : config["buildings"].cast<py::list>()) {
            auto b=obj.cast<py::dict>();
            auto lo=b["min"].cast<std::vector<double>>();
            auto hi=b["max"].cast<std::vector<double>>();
            auto id=b["id"].cast<std::string>();
            NS_ABORT_MSG_IF(lo.size()!=3 || hi.size()!=3 || id.empty() ||
                            id.find_first_of(",;\n\r\"")!=std::string::npos, "invalid building");
            for (size_t k=0;k<3;++k)
                NS_ABORT_MSG_IF(!std::isfinite(lo[k]) || !std::isfinite(hi[k]) || lo[k]>=hi[k],
                                "invalid building box");
            Ptr<Building> building=CreateObject<Building>();
            building->SetBoundaries(Box(lo[0],hi[0],lo[1],hi[1],lo[2],hi[2]));
            m_buildings.emplace_back(id,building);
        }
        NS_ABORT_MSG_IF(m_buildings.empty(), "geometry profile requires explicit buildings");
        m_csv.open(csvPath,std::ios::out|std::ios::trunc);
        NS_ABORT_MSG_IF(!m_csv, "cannot write geometry observations: " << csvPath);
        m_csv << "time_s,wall_monotonic_ns,record_kind,from_id,to_id,geom_los,radio_state,blocker_ids,"
                 "from_x,from_y,from_z,to_x,to_y,to_z,pathloss_db,parameter_set,frequency_hz\n";
        m_csv << std::setprecision(17);
    }
    void Bind(const std::vector<Ptr<MobilityModel>>& models) {
        m_models=models;
        for (uint32_t i=0;i<models.size();++i) {
            if (!models[i]->GetObject<MobilityBuildingInfo>())
                models[i]->AggregateObject(CreateObject<MobilityBuildingInfo>());
            m_ids[PeekPointer(models[i])]=i;
        }
    }
    // Called after each atomic measured-pose update; records transitions and a
    // 100ms diagnostic sample, including links on which no packet is sent.
    void ObserveAll() const {
        for (size_t i=0;i<m_models.size();++i)
            for (size_t j=i+1;j<m_models.size();++j)
                Evaluate(m_models[i],m_models[j],"sample");
    }
    std::string parameterSet, mode;
    double frequencyHz{0};
    uint64_t PacketEvaluations() const { return m_packetEvaluations; }
    uint64_t RecordedRows() const { return m_rows; }
    void Close() { m_csv.flush(); m_csv.close(); }
private:
    double Evaluate(Ptr<MobilityModel> a, Ptr<MobilityModel> b, const std::string& kind) const {
        NS_ABORT_MSG_IF(!m_ids.count(PeekPointer(a)) || !m_ids.count(PeekPointer(b)),
                        "geometry model received unregistered mobility");
        uint32_t ia=m_ids.at(PeekPointer(a)), ib=m_ids.at(PeekPointer(b));
        if (ia>ib) { std::swap(a,b); std::swap(ia,ib); }
        NS_ABORT_MSG_IF(a->GetObject<MobilityBuildingInfo>()->IsIndoor() ||
                        b->GetObject<MobilityBuildingInfo>()->IsIndoor(),
                        "outdoor geometry profile received an indoor antenna");
        const bool los=m_condition->GetChannelCondition(a,b)->IsLos();
        const bool selectLos=mode=="los_only" || los;
        const auto model=selectLos ? m_los : m_nlos;
        const double loss=-model->CalcRxPower(0,a,b);
        NS_ABORT_MSG_IF(!std::isfinite(loss), "nonfinite native geometry pathloss");
        const Vector x=a->GetPosition(), y=b->GetPosition();
        const auto key=std::make_pair(ia,ib);
        auto previous=m_last.find(key);
        const double now=Simulator::Now().GetSeconds();
        const bool initial=previous==m_last.end();
        const bool transition=!initial && previous->second.los!=los;
        const bool periodic=kind=="sample" && !initial && now-previous->second.time>=.1-1e-9;
        if (initial || transition || periodic) {
            std::string blockers;
            for (const auto& building : m_buildings)
                if (building.second->IsIntersect(x,y)) {
                    if (!blockers.empty()) blockers+=';';
                    blockers+=building.first;
                }
            const int64_t wall=std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count();
            m_csv << now << ',' << wall << ',' << (initial ? "initial" : transition ?
                (kind=="packet" ? "packet_transition" : "transition") : "sample") << ','
                << ia << ',' << ib << ',' << (los ? 1 : 0) << ',' << (selectLos ? "LOS" : "NLOS")
                << ',' << blockers << ',' << x.x << ',' << x.y << ',' << x.z << ','
                << y.x << ',' << y.y << ',' << y.z << ',' << loss << ',' << parameterSet << ','
                << frequencyHz << '\n';
            m_last[key]={los,now};
            ++m_rows;
        }
        return loss;
    }
    double DoCalcRxPower(double tx, Ptr<MobilityModel> a, Ptr<MobilityModel> b) const override {
        ++m_packetEvaluations;
        return tx-Evaluate(a,b,"packet");
    }
    int64_t DoAssignStreams(int64_t) override { return 0; } // Deterministic upstream means.
    Ptr<LogDistancePropagationLossModel> m_los{CreateObject<LogDistancePropagationLossModel>()};
    Ptr<LogDistancePropagationLossModel> m_nlos{CreateObject<LogDistancePropagationLossModel>()};
    Ptr<BuildingsChannelConditionModel> m_condition{CreateObject<BuildingsChannelConditionModel>()};
    std::vector<std::pair<std::string,Ptr<Building>>> m_buildings;
    std::vector<Ptr<MobilityModel>> m_models;
    std::map<const MobilityModel*,uint32_t> m_ids;
    struct Last { bool los; double time; };
    mutable std::map<std::pair<uint32_t,uint32_t>,Last> m_last;
    mutable std::ofstream m_csv;
    mutable uint64_t m_packetEvaluations{0}, m_rows{0};
};
} // namespace bas
