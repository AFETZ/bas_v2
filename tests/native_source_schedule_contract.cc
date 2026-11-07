// CPU-only embedded-Python/ns-3 lifetime regression, not RF/HIL qualification.
// Stage native-spectrum-sources.h beside this scratch source; argument is a NEW
// result.json path. Real prepare/materialize and WaveformGenerator are used.
#define PYBIND11_ASSERT_GIL_HELD_INCREF_DECREF
#include "pybind11/embed.h"
#include "ns3/core-module.h"
#include "ns3/spectrum-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "native-spectrum-sources.h"
#include <filesystem>
#include <fstream>
#include <iostream>

using namespace ns3;
namespace py = pybind11;
using namespace pybind11::literals;
namespace
{
size_t checks = 0, destroyed = 0, destroyedWithoutGil = 0;
void Require(bool condition, const std::string& reason)
{
    ++checks;
    if (!condition) throw std::runtime_error(reason);
}
void LifetimeDestroyed(void* value)
{
    ++destroyed;
    if (!PyGILState_Check()) ++destroyedWithoutGil;
    delete static_cast<int*>(value);
}
struct Observation
{
    std::vector<int64_t> waves;
    std::vector<std::pair<std::string, int64_t>> events;
    unsigned polls{0};
    bool pollHadGil{false};
};
Observation* current = nullptr;
void WaveformStarted(Ptr<const Packet>) { current->waves.push_back(Simulator::Now().GetNanoSeconds()); }

std::filesystem::path ActualPath(std::filesystem::path input)
{
    input.replace_filename(input.stem().string() + "_actual.json");
    return input;
}
py::dict ReadJson(const std::filesystem::path& path)
{
    std::ifstream stream(path);
    const std::string text((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    return py::module_::import("json").attr("loads")(text).cast<py::dict>();
}
void WriteInput(const std::filesystem::path& path, bool relative)
{
    Require(!std::filesystem::exists(path) && !std::filesystem::exists(ActualPath(path)),
            "Preserve the previous source input and actual schedule");
    auto source = py::dict("id"_a="cpu_source", "mode"_a="continuous", "spectral_shape"_a="rectangular",
        "position_m"_a=std::vector<double>{0,0,10}, "orientation_rad"_a=std::vector<double>{0,0},
        "pattern"_a="iso", "gain_dbi"_a=0., "power_w"_a=1., "bandwidth_hz"_a=20000000.,
        "center_hz"_a=2412000000., "start_s"_a=.01, "stop_s"_a=.04,
        "period_s"_a=.01, "duty_cycle"_a=1.);
    py::list sources; sources.append(source);
    auto input = py::dict("time_reference"_a=relative ? "measurement_start" : "simulation_start", "sources"_a=sources);
    const auto resolved = py::module_::import("scripts.product.prepare_native_sources").attr("prepare")(input);
    std::ofstream output(path);
    output << py::module_::import("json").attr("dumps")(resolved).cast<std::string>() << '\n';
    output.close();
    Require(static_cast<bool>(output), "Cannot write prepared source input");
}

// Watch the ACTUAL resolved dict owned by production InstallSpectrumSources.
// An instance attribute is not a config field and is not copied into actual JSON.
// Restoring json.loads before Run leaves only the source owner holding this
// object. Forced pybind11 refcount guards also catch copies outside the GIL.
struct WatchedLoads
{
    py::object json = py::module_::import("json");
    py::object original = json.attr("loads");
    WatchedLoads()
    {
        py::dict scope;
        py::exec("class WatchedSchedule(dict): pass", scope);
        auto type = scope["WatchedSchedule"].cast<py::object>();
        json.attr("loads") = py::cpp_function([load = original, type](const std::string& text) {
            auto value = type(load(text));
            value.attr("_schedule_lifetime") = py::capsule(new int(1), &LifetimeDestroyed);
            return value;
        });
    }
    ~WatchedLoads() { json.attr("loads") = original; }
};

struct Fixture
{
    Observation observation;
    Ptr<MultiModelSpectrumChannel> channel{CreateObject<MultiModelSpectrumChannel>()};
    Ptr<bas::SourcePropagation> owner;
    std::filesystem::path input;
    size_t initialDestroyed{destroyed};
    Fixture(const std::filesystem::path& path, bool relative, bool reachPhase = true) : input(path)
    {
        current = &observation;
        WriteInput(path, relative);
        {
            WatchedLoads watch;
            SionnaRtChannelModel::RtPathSolverConfig solver;
            owner = bas::InstallSpectrumSources(nullptr, channel, path.string(), "not_loaded.xml", solver, 90,
                "friis", [this](const auto& event, const auto&, double, const auto&) {
                    observation.events.emplace_back(event, Simulator::Now().GetNanoSeconds());
                    if (event == "source_time_anchor")
                        Require(PyGILState_Check(), "Commit did not acquire the GIL");
                }, false, [this, reachPhase] {
                    ++observation.polls;
                    observation.pollHadGil |= PyGILState_Check() != 0;
                    return reachPhase && Simulator::Now() >= MilliSeconds(45)
                        ? std::string("experiment_normal") : std::string("warming");
                });
        }
        channel->AddPhasedArraySpectrumPropagationLossModel(owner);
        Require(owner->sourceTimes.size() == 1, "Production source generator is missing");
        const auto generator = DynamicCast<WaveformGenerator>(owner->sourceTimes.begin()->first);
        Require(generator && generator->TraceConnectWithoutContext("TxStart", MakeCallback(&WaveformStarted)),
                "Cannot observe real WaveformGenerator transmissions");
    }
    void Run(Time stop)
    {
        Simulator::Stop(stop);
        py::gil_scoped_release release;
        Simulator::Run();
    }
    void Destroy()
    {
        {
            py::gil_scoped_release release;
            Simulator::Destroy();
            channel = nullptr;
            if (owner)
                Require(owner->GetReferenceCount() == 1, "Disposed source retains a self/channel ownership cycle");
            owner = nullptr;
        }
        Require(destroyed == initialDestroyed + 1 && destroyedWithoutGil == 0,
                "Resolved Python object was leaked or released without the GIL");
    }
};

void ObservedMeasurement(const std::filesystem::path& path)
{
    Fixture fixture(path, true);
    fixture.Run(MilliSeconds(125));
    Require(fixture.observation.polls == 4 && !fixture.observation.pollHadGil,
            "Phase polling did not execute at 0/20/40/60ms with the GIL released");
    Require(fixture.observation.waves == std::vector<int64_t>{70000000,80000000,90000000},
            "Materialized offsets changed the real waveform ticks");
    Require(fixture.observation.events == std::vector<std::pair<std::string,int64_t>>{
                {"source_time_anchor",60000000},{"jammer_on",70000000},{"jammer_off",100000000}},
            "Anchor/on/off events changed or the source committed more than once");
    auto actual = ReadJson(ActualPath(path));
    auto anchor = actual["anchor"].cast<py::dict>();
    auto segment = actual["segments"].cast<py::list>()[0].cast<py::dict>();
    Require(anchor["simulation_ns"].cast<int64_t>() == 60000000 &&
            anchor["host_monotonic_ns"].cast<int64_t>() > 0 &&
            anchor["phase"].cast<std::string>() == "experiment_normal" &&
            segment["start_ns"].cast<int64_t>() == 70000000 && segment["stop_ns"].cast<int64_t>() == 100000000,
            "Actual JSON does not retain the observed anchor and native interval ticks");
    Require(ReadJson(path.string()+".state")["enabled_sources"].cast<unsigned>() == 0,
            "Existing enabled_sources counter did not return to zero");
    Require(destroyed == fixture.initialDestroyed, "Python schedule was destroyed before Simulator::Destroy");
    fixture.Destroy();
}

void CancelActive(const std::filesystem::path& path)
{
    Fixture fixture(path, false);
    Simulator::Schedule(MilliSeconds(25), [pointer = PeekPointer(fixture.owner)] { pointer->Dispose(); });
    fixture.Run(MilliSeconds(80));
    Require(fixture.observation.polls == 0 && fixture.observation.waves == std::vector<int64_t>{10000000,20000000},
            "Dispose did not stop active waveform/future intervals or absolute clock changed");
    Require(fixture.observation.events == std::vector<std::pair<std::string,int64_t>>{
                {"source_time_anchor",0},{"jammer_on",10000000}}, "Cancelled source emitted an uncancelled stop callback");
    Require(ReadJson(ActualPath(path))["anchor"]["simulation_ns"].cast<int64_t>() == 0,
            "Default simulation_start no longer anchors at native zero");
    Require(destroyed == fixture.initialDestroyed + 1, "Explicit Dispose did not clear Python storage");
    Require(ReadJson(path.string()+".state")["enabled_sources"].cast<unsigned>() == 0,
            "Cancelled waveform remains active in the published source state");
    { py::gil_scoped_release release; fixture.owner->Dispose(); }
    fixture.Destroy();
}

void UnreachedOrCancelled(const std::filesystem::path& path, bool cancel)
{
    Fixture fixture(path, true, false);
    if (cancel)
        Simulator::Schedule(MilliSeconds(25), [pointer = PeekPointer(fixture.owner)] { pointer->Dispose(); });
    fixture.Run(MilliSeconds(55));
    Require(fixture.observation.polls == (cancel ? 2u : 3u) && !fixture.observation.pollHadGil,
            "Cancelled phase poll survived or pending phase did not poll normally");
    Require(fixture.observation.events.empty() && fixture.observation.waves.empty() &&
            !std::filesystem::exists(ActualPath(path)), "Unobserved phase fabricated an anchor or transmitted");
    Require(destroyed == fixture.initialDestroyed + (cancel ? 1u : 0u), "Unexpected pre-Destroy Python lifetime");
    fixture.Destroy();
}
void DestroyBeforeRun(const std::filesystem::path& path)
{
    Fixture fixture(path, true);
    fixture.owner = nullptr; // Only native ownership remains; no Python closure retains it.
    fixture.Destroy();
    Require(!std::filesystem::exists(ActualPath(path)), "Destroy before Run fabricated an actual schedule");
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2 || std::filesystem::exists(argv[1])) throw std::runtime_error("Expected a new result.json path");
    py::scoped_interpreter python;
    std::string error;
    try
    {
        ObservedMeasurement(std::string(argv[1])+".measurement.json");
        CancelActive(std::string(argv[1])+".cancel-active.json");
        UnreachedOrCancelled(std::string(argv[1])+".unreached.json", false);
        UnreachedOrCancelled(std::string(argv[1])+".cancel-poll.json", true);
        DestroyBeforeRun(std::string(argv[1])+".before-run.json");
    }
    catch (const std::exception& failure) { error = failure.what(); }
    { py::gil_scoped_release release; Simulator::Destroy(); }
    std::ofstream report(argv[1]);
    report << py::module_::import("json").attr("dumps")(py::dict("schema"_a=1, "passed"_a=error.empty(),
        "checks"_a=checks, "error"_a=error, "python_lifetimes_destroyed"_a=destroyed,
        "destructors_without_gil"_a=destroyedWithoutGil,
        "scope"_a="CPU production source schedule/phase/commit/cancel/Destroy under released GIL; no RT/RF/HIL qualification"))
        .cast<std::string>() << '\n';
    if (!error.empty()) std::cerr << error << '\n';
    return error.empty() ? 0 : 1;
}
