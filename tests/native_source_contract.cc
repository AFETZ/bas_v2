// Compile as an ns-3 scratch target against the pinned private runtime.
// No scene solve, network sockets, wall-clock scheduler or GPU is used.
#include "pybind11/embed.h"
#include "ns3/core-module.h"
#include "ns3/spectrum-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "native-spectrum-sources.h"
#include <iostream>
#include <stdexcept>

using namespace ns3;

int main(int argc, char** argv)
{
    if (argc != 2) throw std::runtime_error("expected prepared emitter JSON");
    pybind11::scoped_interpreter interpreter;
    auto reference = CreateObject<SionnaRtSpectrumPropagationLossModel>();
    reference->SetChannelModelAttribute("Frequency", DoubleValue(2412000000.0));
    reference->SetChannelModelAttribute("SamplesPerSource", UintegerValue(17000));
    reference->SetChannelModelAttribute("MaxNumPathsPerSource", UintegerValue(700));
    auto channel = CreateObject<MultiModelSpectrumChannel>();
    SionnaRtChannelModel::RtPathSolverConfig solver;
    auto router = bas::InstallSpectrumSources(reference, channel, argv[1], "not_loaded.xml",
        solver, 90, "sionna", [](const auto&, const auto&, double, const auto&) {});
    bool reused = false, cloned = false;
    if (router->sources.size() != 2) throw std::runtime_error("expected two configured sources");
    for (const auto& [phy, model] : router->sources)
    {
        UintegerValue samples, paths;
        DoubleValue frequency;
        model->GetChannelModelAttribute("SamplesPerSource", samples);
        model->GetChannelModelAttribute("MaxNumPathsPerSource", paths);
        model->GetChannelModelAttribute("Frequency", frequency);
        if (samples.Get() != 17000 || paths.Get() != 700)
            throw std::runtime_error("emitter channel lost the reference solver budget");
        if (frequency.Get() == 2412000000.0)
        {
            if (model != reference) throw std::runtime_error("same-carrier channel was duplicated");
            reused = true;
        }
        else if (frequency.Get() == 2437000000.0)
        {
            if (model == reference) throw std::runtime_error("off-carrier source reused the desired frequency");
            cloned = true;
        }
        else throw std::runtime_error("unexpected source carrier");
        std::cout << "carrier_hz=" << frequency.Get() << " samples_per_src=" << samples.Get()
                  << " max_num_paths_per_src=" << paths.Get() << '\n';
    }
    if (!reused || !cloned) throw std::runtime_error("source carrier routing was not exercised");
    Simulator::Destroy();
    std::cout << "PASS native source carrier and solver budget contract\n";
}
