// CPU-only behavioral test of the real state reader and ns-3 MobilityModel.
#include "ns3/core-module.h"
#include "native-live-state.h"
#include "native-lockstep.h"
#include <filesystem>
#include <chrono>
#include <iostream>

int main(int argc, char** argv) {
    pybind11::scoped_interpreter interpreter;
    namespace py=pybind11;
    auto json=py::module_::import("json");
    auto pathlib=py::module_::import("pathlib");
    if (argc==4 && std::string(argv[1])=="--lockstep") {
        bas::Lockstep barrier(argv[2],10,20000000);
        bas::LiveStateReader reader;
        auto mobility=ns3::CreateObject<bas::MeasuredMobility>();
        for (int i=0;i<25;++i) {
            barrier.Begin(ns3::Simulator::Now().GetNanoSeconds(),true);
            auto apply=[&](){
                const auto now=bas::Lockstep::WallNs();
                auto states=reader.Read(argv[3],{"cp","uav1"},now,0,500000000,
                                       barrier.SourceTime(ns3::Simulator::Now().GetSeconds()),10000000000LL);
                mobility->Apply(states.at(1));
            };
            ns3::Simulator::ScheduleNow(apply);
            // Deliberate wall stall tests the barrier, not a synthetic RF result.
            if(i==5) ns3::Simulator::Schedule(ns3::MilliSeconds(10),[](){
                std::this_thread::sleep_for(std::chrono::milliseconds(1200));
            });
            ns3::Simulator::Stop(ns3::MilliSeconds(20));
            ns3::Simulator::Run();
            barrier.Physics(ns3::Simulator::Now().GetNanoSeconds(),true);
        }
        barrier.Clock(ns3::Simulator::Now().GetNanoSeconds(),"stopped",false);
        std::cout << "PASS: stock ns-3 and real Gazebo/ROS advanced 25 barriers / 0.5 model seconds with 1.2s host stall; z="
                  << mobility->GetPosition().z << "\n";
        ns3::Simulator::Destroy();
        return 0;
    }
    if (argc==2) {
        bas::LiveStateReader reader;
        const auto now=std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        auto states=reader.Read(argv[1],{"cp","uav1"},now,0,500000000);
        auto mobility=ns3::CreateObject<bas::MeasuredMobility>();
        mobility->Apply(states.at(1));
        const auto p=mobility->GetPosition(),v=mobility->GetVelocity();
        std::cout << "{\"x\":"<<p.x<<",\"y\":"<<p.y<<",\"z\":"<<p.z
                  <<",\"vx\":"<<v.x<<",\"vy\":"<<v.y<<",\"vz\":"<<v.z<<"}\n";
        return 0;
    }
    // These controlled inputs are unit-test stimuli, not simulated flight evidence.
    auto root=json.attr("loads")(R"({"schema_version":2,"session_id":"test",
      "coordinate_frame":"ENU","source":"ros_odometry","fault":null,
      "published_monotonic_ns":2000000000,"clock_received_monotonic_ns":2000000000,
      "nodes":[{"id":"cp","role":"command_post","position_m":[0,0,20],
      "velocity_enu_mps":[0,0,0],"orientation_quat_xyzw":[0,0,0,1]},
      {"id":"uav1","role":"uav","history":[
      {"sample_monotonic_ns":1900000000,"source_sim_time_s":10.0,"position_m":[1,2,30],
       "velocity_enu_mps":[3,4,5],"orientation_quat_xyzw":[0,0,0.7071067811865475,0.7071067811865475]},
      {"sample_monotonic_ns":1990000000,"source_sim_time_s":10.09,"position_m":[9,9,99],
       "velocity_enu_mps":[0,0,0],"orientation_quat_xyzw":[0,0,0,1]}]}]})").cast<py::dict>();
    const auto path=py::module_::import("tempfile").attr("mktemp")().cast<std::string>();
    const auto save=[&](){pathlib.attr("Path")(path).attr("write_text")(json.attr("dumps")(root));};
    save();
    bas::LiveStateReader reader;
    const auto check=[](bool value,const char* why){if(!value)throw std::runtime_error(why);};
    auto states=reader.Read(path,{"cp","uav1"},1950000000,2000000000,500000000);
    auto mobility=ns3::CreateObject<bas::MeasuredMobility>();
    mobility->Apply(states.at(1));
    ns3::VectorValue orientation; mobility->GetAttribute("Orientation",orientation);
    check(mobility->GetPosition().z==30,"used future pose");
    check(mobility->GetVelocity().z==5,"lost vertical velocity");
    check(std::abs(orientation.Get().x-M_PI/2)<1e-6,"lost attitude");
    auto changed=reader.Read(path,{"cp","uav1"},2000000000,2000000000,500000000);
    mobility->Apply(changed.at(1));
    check(mobility->GetPosition().z==99,"height change not applied");
    check(mobility->GetVelocity().z==0,"velocity change not applied");
    root["simulation_mode"]="lockstep";
    root["published_monotonic_ns"]=60000000000LL;save();
    auto paused=reader.Read(path,{"cp","uav1"},60000000000LL,60000000000LL,500000000,10.05);
    check(paused.at(1).position.z==30,"model-time reader used a future pose during host stall");
    auto delayed=reader.Read(path,{"cp","uav1"},62000000000LL,62000000000LL,500000000,10.05,10000000000LL);
    check(delayed.at(1).position.z==30,"host publication delay consumed model-time source deadline");
    bool tooOld=false;
    try {reader.Read(path,{"cp","uav1"},60000000000LL,60000000000LL,500000000,11.);}
    catch(const std::exception&){tooOld=true;}
    check(tooOld,"model-time reader accepted stale source pose");
    root["published_monotonic_ns"]=2000000000LL;save();
    bool rejected=false;
    try {reader.Read(path,{"cp","uav1"},2600000000,2600000000,500000000);}
    catch (const std::exception&) {rejected=true;}
    check(rejected,"accepted stale source");
    root["session_id"]="restarted";save();rejected=false;
    try {reader.Read(path,{"cp","uav1"},2000000000,2000000000,500000000);}
    catch (const std::exception&) {rejected=true;}
    check(rejected,"accepted tracker reset");
    bas::ClockAlignment clocks;
    clocks.Check(10,2,.5); clocks.Check(11,3,.5);
    rejected=false;
    try {clocks.Check(11.1,4,.5);} catch (const std::exception&) {rejected=true;}
    check(rejected,"accepted slow Gazebo clock despite fresh publication");
    std::filesystem::remove(path);
    std::cout << "PASS: causal pose, velocity, attitude, height change, deadline, session reset, clock drift\n";
}
