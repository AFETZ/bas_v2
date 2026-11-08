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

namespace
{
void Require(bool value, const std::string& reason)
{
    if (!value) throw std::runtime_error(reason);
}

template <typename Call> void RejectsHybrid(Call call)
{
    bool rejected = false;
    try { call(); }
    catch (const std::invalid_argument& error)
    {
        rejected = std::string(error.what()).find("hybrid propagation is not qualified") != std::string::npos;
    }
    Require(rejected, "unqualified hybrid dispatch did not fail closed");
}

void CheckPropagationProfiles()
{
    auto a = CreateObject<ConstantPositionMobilityModel>();
    auto b = CreateObject<ConstantPositionMobilityModel>();
    auto aa = CreateObject<UniformPlanarArray>();
    auto ba = CreateObject<UniformPlanarArray>();
    aa->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
    ba->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
    Bands bands{{2401000000, 2402000000, 2403000000},
                {2411000000, 2412000000, 2413000000},
                {2421000000, 2422000000, 2423000000}};
    auto signal = Create<SpectrumSignalParameters>();
    signal->psd = Create<SpectrumValue>(Create<SpectrumModel>(bands));
    (*signal->psd)[0] = 1e-9;
    (*signal->psd)[1] = 0;
    (*signal->psd)[2] = 2e-9;
    auto originalPsd = Copy<SpectrumValue>(signal->psd);
    auto router = CreateObject<bas::SourcePropagation>();
    router->profile = "hybrid";
    // The first pair satisfied the removed coordinate-only gate. The second
    // used its RT branch: neither branch may run under an unqualified profile.
    for (const auto& positions : {std::pair{Vector(3000, 3000, 250), Vector(3600, 3000, 250)},
                                  std::pair{Vector(0, 0, 10), Vector(10, 0, 10)}})
    {
        a->SetPosition(positions.first); b->SetPosition(positions.second);
        RejectsHybrid([&] { router->CalcRxPowerSpectralDensity(signal, a, b, aa, ba); });
    }
    // Installer validation precedes JSON reads, device allocation and RT setup.
    RejectsHybrid([&] {
        bas::InstallSpectrumSources(nullptr, nullptr, "nonexistent-hybrid-sources.json", "not_loaded.xml",
            SionnaRtChannelModel::RtPathSolverConfig{}, 90, "hybrid", {});
    });
    router->profile = "misspelled_model";
    bool unknownRejected = false;
    try { router->CalcRxPowerSpectralDensity(signal, a, b, aa, ba); }
    catch (const std::invalid_argument&) { unknownRejected = true; }
    Require(unknownRejected, "unknown profile silently selected RT");

    // The supported simple path must still delegate once to stock spectrum
    // Friis at each actual band, preserving the caller's PSD and signal object.
    router->profile = "friis";
    auto stock = CreateObject<FriisSpectrumPropagationLossModel>();
    auto expected = stock->CalcRxPowerSpectralDensity(signal, a, b);
    auto actual = router->CalcRxPowerSpectralDensity(signal, a, b, aa, ba);
    Require(actual != signal && actual->psd != signal->psd, "Friis dispatch mutated its input");
    for (size_t index = 0; index < bands.size(); ++index)
    {
        Require((*actual->psd)[index] == (*expected)[index], "Friis dispatch changed stock per-band loss");
        Require((*signal->psd)[index] == (*originalPsd)[index], "dispatcher changed the transmit PSD");
    }
    router->Dispose();
    std::cout << "PASS hybrid and unknown profile rejection; stock Friis PSD preserved\n";
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2) throw std::runtime_error("expected prepared emitter JSON");
    pybind11::scoped_interpreter interpreter;
    CheckPropagationProfiles();
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
    auto batchSources = bas::InstallSpectrumSources(reference, channel, argv[1], "not_loaded.xml",
        solver, 90, "sionna", [](const auto&, const auto&, double, const auto&) {}, true);
    for (const auto& [phy, model] : batchSources->sources)
    {
        if (model == reference) throw std::runtime_error("external source reused the registered radio cohort");
        auto sourceChannel = DynamicCast<SionnaRtChannelModel>(model->GetChannelModel());
        if (sourceChannel->GetReceiverBatchEnabled())
            throw std::runtime_error("external source unexpectedly entered receiver batch");
        UintegerValue samples;
        model->GetChannelModelAttribute("SamplesPerSource", samples);
        if (samples.Get() != 17000) throw std::runtime_error("standalone source lost its bounded budget");
    }
    Simulator::Destroy();
    std::cout << "PASS native source carrier and solver budget contract\n";
}
