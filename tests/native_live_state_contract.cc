// CPU-only production pose reader regression. No Gazebo, worker, RF or GPU.
// Stage with native-live-state.h as an ns-3 scratch target; link Python embedding.
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "native-live-state.h"
#include <filesystem>
#include <functional>
#include <iostream>

using namespace ns3;
namespace py = pybind11;
using namespace pybind11::literals;
namespace
{
size_t checks=0;
void Require(bool value,const std::string& reason)
{ ++checks; if(!value) throw std::runtime_error(reason); }
void Reject(std::function<void()> call,const std::string& reason)
{
    bool rejected=false;
    try { call(); } catch(const std::exception&) { rejected=true; }
    Require(rejected,reason);
}
constexpr int64_t base=1000000000000LL, ms=1000000;
py::dict Pose(const char* role,double x)
{
    return py::dict("role"_a=role,"position_m"_a=std::vector<double>{x,0,20},
        "velocity_enu_mps"_a=std::vector<double>{0,0,0},
        "orientation_quat_xyzw"_a=std::vector<double>{0,0,0,1});
}
py::dict Fixture()
{
    auto cp=Pose("command_post",0); cp["id"]="cp";
    auto uav=Pose("uav",1); uav["id"]="uav1";
    auto old=Pose("uav",1); old["sample_monotonic_ns"]=base-20*ms; old["source_sim_time_s"]=1.;
    auto next=Pose("uav",2); next["sample_monotonic_ns"]=base+20*ms; next["source_sim_time_s"]=1.04;
    py::list history; history.append(old); history.append(next); uav["history"]=history;
    py::list nodes; nodes.append(cp); nodes.append(uav);
    return py::dict("schema_version"_a=2,"source"_a="gazebo_odometry","coordinate_frame"_a="ENU",
        "simulation_mode"_a="realtime","fault"_a=py::none(),"session_id"_a="same",
        "published_monotonic_ns"_a=base+10*ms,"clock_received_monotonic_ns"_a=base,
        "nodes"_a=nodes);
}
void Test(const std::filesystem::path& input)
{
    auto json=py::module_::import("json");
    py::object original=json.attr("loads");
    size_t parses=0;
    json.attr("loads")=py::cpp_function([&](const std::string& value) {
        ++parses; return original(value);
    });
    // Restore the original loader before the wrapper's captured locals die.
    struct Restore { py::module_ json; py::object original; ~Restore(){json.attr("loads")=original;} } restore{json,original};
    bas::LiveStateReader reader;
    auto fixture=Fixture();
    const auto write=[&] {
        std::ofstream file(input,std::ios::binary|std::ios::trunc);
        file << json.attr("dumps")(fixture).cast<std::string>(); file.close();
        Require(static_cast<bool>(file),"Cannot write controlled reader input");
    };
    const std::vector<std::string> names{"cp","uav1"};
    const auto read=[&](int64_t event,int64_t wall) { return reader.Read(input.string(),names,event,wall,500*ms); };
    write();
    auto states=read(base,base+40*ms);
    Require(parses==1 && states[1].sourceTime==1. && states[1].sampleNs==base-20*ms,
            "Reader admitted a future history sample");
    states=read(base+25*ms,base+40*ms);
    Require(parses==1 && states[1].sourceTime==1.04 && states[1].sampleNs==base+20*ms,
            "Unchanged publication was reparsed or its causal selection was cached");
    Require(reader.LastPublicationNs()==base+10*ms,"Reader changed the original publication epoch");
    Reject([&]{read(base+25*ms,base+600*ms);},"Cached publication bypassed stale-clock validation");
    Require(parses==1,"Age validation reloaded an unchanged publication");
    auto history=fixture["nodes"].cast<py::list>()[1]["history"].cast<py::list>();
    history[1]["position_m"]=std::vector<double>{7,0,20};
    write();
    states=read(base+25*ms,base+40*ms);
    Require(parses==2 && states[1].position.x==7,"Same-length changed bytes reused an old decoded publication");
    fixture["session_id"]="other"; write();
    Reject([&]{read(base+25*ms,base+40*ms);},"Reader ignored a changed tracker session");
    fixture["session_id"]="same"; fixture["fault"]="clock_reset"; write();
    Reject([&]{read(base+25*ms,base+40*ms);},"Reader ignored a published source fault");
    fixture["fault"]=py::none(); fixture["source"]="invented_odometry"; write();
    Reject([&]{read(base+25*ms,base+40*ms);},"Reader ignored a changed source contract");
    fixture["source"]="gazebo_odometry"; fixture["schema_version"]=99; write();
    Reject([&]{read(base+25*ms,base+40*ms);},"Reader ignored a changed schema");
    fixture["schema_version"]=2; write();
    Reject([&]{read(base-100*ms,base+40*ms);},"Reader cached a pose past a backwards causal cutoff");
    auto third=Pose("uav",9); third["sample_monotonic_ns"]=base-15*ms; third["source_sim_time_s"]=1.2;
    history.append(third); write();
    states=read(base,base+40*ms);
    Require(states[1].sourceTime==1.2 && parses>=8,"Changed history was not applied");
    auto peer=Pose("uav",10); peer["id"]="uav2";
    auto peerSample=Pose("uav",10); peerSample["sample_monotonic_ns"]=base-10*ms;
    peerSample["source_sim_time_s"]=1.;
    py::list peerHistory; peerHistory.append(peerSample); peer["history"]=peerHistory;
    fixture["nodes"].cast<py::list>().append(peer); write();
    states=reader.Read(input.string(),{"cp","uav1","uav2"},base,base+40*ms,500*ms);
    Require(states[1].sourceTime==1. && states[2].sourceTime==1.,
            "Cached parsing bypassed the100ms cross-node source-skew window");
    std::ofstream bad(input,std::ios::binary|std::ios::trunc); bad << "{"; bad.close();
    Reject([&]{read(base,base+40*ms);},"Malformed publication reused the prior valid root");
}
} // namespace
int main(int argc,char** argv)
{
    if(argc!=2 || std::filesystem::exists(argv[1])) throw std::runtime_error("Expected new result.json path");
    py::scoped_interpreter python;
    const auto input=std::string(argv[1])+".snapshot.json";
    if(std::filesystem::exists(input)) throw std::runtime_error("Preserve the previous reader input");
    std::string error;
    try { Test(input); } catch(const std::exception& failure) { error=failure.what(); }
    std::ofstream report(argv[1]);
    report << py::module_::import("json").attr("dumps")(py::dict("schema"_a=1,"passed"_a=error.empty(),
        "checks"_a=checks,"error"_a=error,
        "scope"_a="CPU production pose reader: parsing reuse and causal/schema/clock/session guards; no simulator hardware acceptance")).cast<std::string>() << '\n';
    if(!error.empty()) std::cerr << error << '\n';
    return error.empty()?0:1;
}
