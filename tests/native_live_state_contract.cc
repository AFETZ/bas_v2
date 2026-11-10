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

struct CommitObserver
{
    const std::vector<Ptr<MobilityModel>>& mobility;
    const std::vector<Vector>& positions;
    const std::vector<bas::MeasuredState>& applied;
    const std::vector<bas::MeasuredState>* expected;
    const size_t& dependentMarker;
    size_t expectedMarker;
    size_t notifications{0};
    void Changed(Ptr<const MobilityModel>)
    {
        ++notifications;
        Require(dependentMarker==expectedMarker,"CourseChange ran before the matching dependent CIR frame was published");
        Require(applied.size()==expected->size(),"CourseChange observed incomplete applied metadata");
        for(size_t i=0;i<expected->size();++i)
        {
            VectorValue orientation;mobility[i]->GetAttribute("Orientation",orientation);
            Require(mobility[i]->GetPosition()==(*expected)[i].position && positions[i]==(*expected)[i].position &&
                mobility[i]->GetVelocity()==(*expected)[i].velocity && orientation.Get()==(*expected)[i].orientation &&
                applied[i].sampleNs==(*expected)[i].sampleNs && applied[i].sourceTime==(*expected)[i].sourceTime,
                "CourseChange observed a partially committed cohort or changed source epoch");
        }
    }
};
void CohortCommit()
{
    std::vector<Ptr<MobilityModel>> mobility;
    std::vector<Vector> positions(3);
    std::vector<bas::MeasuredState> applied,states(3);
    for(size_t i=0;i<3;++i)
    {
        mobility.push_back(CreateObject<bas::MeasuredMobility>());
        states[i].position={double(i+1),double(i+2),20.+i};
        states[i].velocity={double(i),.2*double(i),-.1*double(i)};
        states[i].orientation={.1*double(i),.01*double(i),-.02*double(i)};
        states[i].sampleNs=i ? base-20*ms+10*ms*(i-1) : base;
        states[i].sourceTime=i ? 1.+.02*(i-1) : 0.;
    }
    size_t dependentMarker=0,publishCalls=0,expectedNotifications=0;
    CommitObserver observer{mobility,positions,applied,&states,dependentMarker,1};
    const auto publish=[&] {
        ++publishCalls;
        Require(observer.notifications==expectedNotifications,"Notification preceded synchronous dependent publication");
        for(size_t i=0;i<states.size();++i)
            Require(mobility[i]->GetPosition()==(*observer.expected)[i].position && positions[i]==(*observer.expected)[i].position,
                    "Dependent publication did not see the whole new measured geometry");
        dependentMarker=observer.expectedMarker;
    };
    for(auto model:mobility)
        Require(model->TraceConnectWithoutContext("CourseChange",MakeCallback(&CommitObserver::Changed,&observer)),
                "Cannot observe actual cohort CourseChange notifications");
    bas::CommitMeasuredCohort(states,mobility,positions,applied,base+5*ms,base+5*ms,100*ms,true,publish);
    Require(observer.notifications==3,"A complete commit did not notify every actual mobility");
    Require(applied[1].sampleNs==base-20*ms && applied[1].sourceTime==1.,"Commit freshened a measured pose epoch");
    bas::ValidateMeasuredCohort(applied,3,base+80*ms,base+80*ms,100*ms);
    Require(bas::MaximumMeasuredPoseAgeNs(applied,base+80*ms)==100*ms,"Exact100ms applied age changed");
    Reject([&]{bas::ValidateMeasuredCohort(applied,3,base+80*ms+1,base+80*ms+1,100*ms);},
           "Applied frame silently survived beyond its100ms budget");
    auto observed=states;
    for(auto& state:observed){state.sampleNs+=100*ms; if(state.sourceTime)state.sourceTime+=.1;}
    bas::ValidateMeasuredCohort(observed,3,base+110*ms,base+110*ms,100*ms);
    Require(bas::MaximumMeasuredPoseAgeNs(applied,base+110*ms)==130*ms &&
        bas::MaximumMeasuredPoseAgeNs(observed,base+110*ms)==30*ms,"Fresh observed frame changed applied age");
    Reject([&]{bas::ValidateMeasuredCohort(applied,3,base+110*ms,base+110*ms,100*ms);},
           "Fresh observations hid stale held geometry");
    const auto before=observer.notifications;
    const auto rejectCommit=[&](const std::vector<bas::MeasuredState>& bad) {
        Reject([&]{bas::CommitMeasuredCohort(bad,mobility,positions,applied,base+5*ms,base+5*ms,100*ms,true,publish);},
               "Invalid cohort reached a geometry commit");
        Require(observer.notifications==before && publishCalls==1 && applied[1].sampleNs==states[1].sampleNs &&
            positions[0]==states[0].position && mobility[2]->GetPosition()==states[2].position,
            "Failed validation partially mutated the cohort");
    };
    auto bad=states;bad.pop_back();rejectCommit(bad);
    bad=states;bad[2].orientation.y=std::numeric_limits<double>::quiet_NaN();rejectCommit(bad);
    bad=states;bad[1].sampleNs=base+5*ms+1;rejectCommit(bad);
    bad=states;bad[1].sampleNs=base-100*ms;rejectCommit(bad);
    bad=states;bad[1].sourceTime=-1;rejectCommit(bad);
    bad=states;bad[1].sourceTime=1e20;rejectCommit(bad);
    bad=states;bad[2].sourceTime=states[1].sourceTime+.100000001;rejectCommit(bad);
    bad=states;--bad[1].sampleNs;rejectCommit(bad);
    bad=states;bad[2].sourceTime-=.01;rejectCommit(bad);
    bas::ValidateMeasuredCohort(bad,3,base+5*ms,base+5*ms,100*ms); // No previous-frame guard in immediate mode.
    auto invalidModels=mobility;invalidModels[2]=CreateObject<ConstantPositionMobilityModel>();
    Reject([&]{bas::CommitMeasuredCohort(states,invalidModels,positions,applied,base+5*ms,base+5*ms,100*ms,true,publish);},
           "Non-measured last node allowed a partial first-node application");
    Require(observer.notifications==before,"Incompatible mobility emitted a partial update");
    auto skewBoundary=states;skewBoundary[2].sourceTime=skewBoundary[1].sourceTime+.1;
    bas::ValidateMeasuredCohort(skewBoundary,3,base+5*ms,base+5*ms,100*ms);
    observer.expected=&observed;
    observer.expectedMarker=2;expectedNotifications=before;
    bas::CommitMeasuredCohort(observed,mobility,positions,applied,base+110*ms,base+110*ms,100*ms,true,publish);
    Require(observer.notifications==before+3 && applied[1].sampleNs==observed[1].sampleNs,
            "Fresh complete frame did not replace all original epochs");
    const auto completed=observer.notifications;
    Reject([&]{bas::CommitMeasuredCohort(observed,mobility,positions,applied,base+110*ms,base+110*ms,100*ms,true,
        []{throw std::runtime_error("injected dependent publication failure");});},
        "Dependent publication failure was hidden");
    Require(observer.notifications==completed,"An uncommitted dependent graph emitted CourseChange");
}

void EpochTransition(const std::filesystem::path& input)
{
    Require(!std::filesystem::exists(input),"Preserve existing transition input");
    std::ofstream(input)<<py::module_::import("json").attr("dumps")(Fixture()).cast<std::string>();
    bas::LiveStateReader reader;
    const int64_t epoch=base+10*ms,native=5*ms,wall=base+30*ms;
    const auto warm=bas::MeasuredEventHostNs(true,false,0,0,base+10*ms);
    const auto preReady=bas::MeasuredEventHostNs(true,false,epoch,native,wall);
    const auto ready=bas::MeasuredEventHostNs(true,true,epoch,native,wall);
    Require(warm==base+10*ms && preReady==base+15*ms && preReady==ready,
            "Deferred cutoff changed when readiness flipped or native epoch was ignored");
    Require(bas::MeasuredEventHostNs(false,false,epoch,native,wall)==wall &&
        bas::MeasuredEventHostNs(false,true,epoch,native,wall)==epoch+native,
        "Default immediate cutoff changed");
    std::vector<bas::MeasuredState> applied;
    for(const auto event:{warm,preReady,ready})
    {
        auto observed=reader.Read(input.string(),{"cp","uav1"},event,wall,100*ms);
        Require(observed[1].sourceTime==1. && observed[1].sampleNs==base-20*ms,
                "Pre-ready observation admitted a future native pose or rolled back on readiness");
        bas::ValidateMeasuredCohort(observed,2,event,wall,100*ms,&applied);
        applied=observed;
    }
    auto immediate=reader.Read(input.string(),{"cp","uav1"},wall,wall,100*ms);
    Require(immediate[1].sourceTime==1.04,"Controlled immediate history no longer reaches its old host cutoff");
    Reject([&]{bas::MeasuredEventHostNs(true,true,std::numeric_limits<int64_t>::max(),1,wall);},
           "Native/host epoch addition overflowed silently");
}
void AgeBoundedHistory(const std::filesystem::path& input)
{
    const auto fullPath=input.string()+".full.json", prunedPath=input.string()+".pruned.json";
    for (const bool prune:{false,true})
    {
        const auto& path=prune ? prunedPath : fullPath;
        Require(!std::filesystem::exists(path),"Preserve existing bounded-history fixture");
        auto root=Fixture();
        root["published_monotonic_ns"]=base;
        auto nodes=root["nodes"].cast<py::list>();
        auto node=nodes[1].cast<py::dict>();
        py::list history;
        for (int i=31;i>=0;--i)
        {
            if (prune && i>5) continue; // Publication at base, 100ms inclusive budget.
            auto pose=Pose("uav",i);
            pose["source_sim_time_s"]=10.-i*.02;
            pose["sample_monotonic_ns"]=base-i*20*ms;
            history.append(pose);
        }
        node["history"]=history;
        std::ofstream(path)<<py::module_::import("json").attr("dumps")(root).cast<std::string>();
    }
    bas::LiveStateReader full,pruned;
    for (const int delay:{0,1,20,99,100,101})
        for (int cutoff=-120;cutoff<=20;cutoff+=5)
        {
            std::vector<bas::MeasuredState> a,b;
            bool acceptedA=true,acceptedB=true;
            try {a=full.Read(fullPath,{"cp","uav1"},base+cutoff*ms,base+delay*ms,100*ms);}
            catch(const std::exception&) {acceptedA=false;}
            try {b=pruned.Read(prunedPath,{"cp","uav1"},base+cutoff*ms,base+delay*ms,100*ms);}
            catch(const std::exception&) {acceptedB=false;}
            Require(acceptedA==acceptedB,"Age pruning changed causal reader acceptance");
            if (acceptedA)
                Require(a.size()==b.size() && a[1].sourceTime==b[1].sourceTime &&
                        a[1].sampleNs==b[1].sampleNs && a[1].position==b[1].position,
                        "Age pruning selected a different measured state");
        }
}
} // namespace
int main(int argc,char** argv)
{
    if(argc!=2 || std::filesystem::exists(argv[1])) throw std::runtime_error("Expected new result.json path");
    py::scoped_interpreter python;
    const auto input=std::string(argv[1])+".snapshot.json";
    if(std::filesystem::exists(input)) throw std::runtime_error("Preserve the previous reader input");
    std::string error;
    try { Test(input); CohortCommit(); EpochTransition(input+".transition.json"); AgeBoundedHistory(input); }
    catch(const std::exception& failure) { error=failure.what(); }
    std::ofstream report(argv[1]);
    report << py::module_::import("json").attr("dumps")(py::dict("schema"_a=1,"passed"_a=error.empty(),
        "checks"_a=checks,"error"_a=error,
        "scope"_a="CPU production reader/whole-cohort commit/age/causal/readiness guards; no full-stack or hardware acceptance")).cast<std::string>() << '\n';
    if(!error.empty()) std::cerr << error << '\n';
    return error.empty()?0:1;
}
