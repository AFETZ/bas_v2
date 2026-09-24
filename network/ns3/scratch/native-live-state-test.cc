// CPU-only behavioral test of the real state reader and ns-3 MobilityModel.
#include "ns3/core-module.h"
#include "native-live-state.h"
#include <filesystem>
#include <chrono>
#include <iostream>

int main(int argc, char** argv) {
    pybind11::scoped_interpreter interpreter;
    namespace py=pybind11;
    auto json=py::module_::import("json");
    auto pathlib=py::module_::import("pathlib");
    if (argc==2) {
        bas::LiveStateReader reader;
        const auto now=std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        auto states=reader.Read(argv[1],{"cp","uav1"},now,now,500000000);
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
