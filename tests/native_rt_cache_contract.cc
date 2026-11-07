// Compile as an ns-3 scratch target with native-rt-cache.h beside this source.
// CPU only: no Python interpreter, worker, files on the packet path or RT solve.
// Argument: a new result.json path. Uses the pinned patched Sionna spectrum API.
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "ns3/spectrum-signal-parameters.h"
#include "ns3/spectrum-value.h"
#include "native-rt-cache.h"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <thread>

using namespace ns3;
using Cache = bas::NativeRtCache;
namespace
{
constexpr double kCarrier = 2412000000.0;
size_t g_checks = 0;
void Require(bool condition, const std::string& reason)
{
    ++g_checks;
    if (!condition) throw std::runtime_error(reason);
}
template <typename Function> void Throws(Function call, const std::string& reason)
{
    bool threw = false;
    try { call(); } catch (const std::exception&) { threw = true; }
    Require(threw, reason);
}

// Keep the stock constant-velocity implementation; add the same measured Euler
// attribute used by the live mobility adapter so orientation guards are tested.
class OrientedMobility : public ConstantVelocityMobilityModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("bas::CacheContractMobility").SetParent<ConstantVelocityMobilityModel>()
            .AddConstructor<OrientedMobility>()
            .AddAttribute("Orientation", "ENU yaw/pitch/roll radians", VectorValue(),
                          MakeVectorAccessor(&OrientedMobility::orientation), MakeVectorChecker());
        return tid;
    }
    Vector orientation;
};

// Direct access to the original production spectrum boundary is the reference,
// not another propagation implementation or a mocked channel result.
class DirectSpectrum : public SionnaRtSpectrumPropagationLossModel
{
  public:
    using SionnaRtSpectrumPropagationLossModel::CalcBeamformingGain;
    using SionnaRtSpectrumPropagationLossModel::CalcLongTerm;
};

struct Cohort
{
    std::vector<Ptr<OrientedMobility>> mobility;
    std::vector<Ptr<UniformPlanarArray>> antennas;
    std::vector<Cache::Endpoint> endpoints;
    explicit Cohort(size_t count = 3)
    {
        for (size_t index = 0; index < count; ++index)
        {
            auto node = CreateObject<Node>();
            auto pose = CreateObject<OrientedMobility>();
            pose->SetPosition(Vector(10 * index, 0, 20));
            node->AggregateObject(pose);
            auto array = CreateObject<UniformPlanarArray>();
            array->SetNumRows(1); array->SetNumColumns(1);
            array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1); weights[0] = {1.0, 0.0};
            array->SetBeamformingVector(weights);
            mobility.push_back(pose); antennas.push_back(array); endpoints.push_back({pose, array});
        }
    }
    Ptr<Cache> New(Cache::Limits limits = {}) { return CreateObject<Cache>(kCarrier, endpoints, limits); }
    Cache::Record Sample(uint64_t request = 1, size_t a = 0, size_t b = 1, int64_t nativeNs = -1)
    {
        Cache::Record record;
        record.direction = {a, b};
        record.sourceRequestId = request;
        record.sampleNativeNs = nativeNs < 0 ? Simulator::Now().GetNanoSeconds() : nativeNs;
        record.sampleHostMonotonicNs = Cache::HostNowNs();
        for (size_t side = 0; side < 2; ++side)
        {
            const auto index = record.direction[side];
            record.poses[side] = {mobility[index]->GetPosition(), mobility[index]->GetVelocity(),
                                  mobility[index]->orientation, record.sampleHostMonotonicNs};
        }
        auto matrix = Create<Cache::ChannelMatrix>();
        matrix->m_channel = Cache::Complex3DVector(1, 1, 2);
        matrix->m_channel(0, 0, 0) = {1, 0}; matrix->m_channel(0, 0, 1) = {0, -0.5};
        matrix->m_nodeIds = {900, 901}; matrix->m_antennaPair = {800, 801};
        matrix->m_generatedTime = NanoSeconds(record.sampleNativeNs);
        auto parameters = Create<SionnaRtChannelModel::SionnaRtChannelParams>();
        parameters->m_generatedTime = matrix->m_generatedTime;
        parameters->m_nodeIds = {902, 903};
        parameters->m_delay = {0, 100e-9}; parameters->m_doppler = {0, 25};
        parameters->m_angle.assign(4, std::vector<double>(2, 0));
        record.matrix = matrix; record.parameters = parameters;
        return record;
    }
};

void Advance(Time duration)
{
    Simulator::Schedule(duration, [] {});
    Simulator::Run();
}

Ptr<SpectrumSignalParameters> Signal()
{
    Bands bands;
    for (double offset : {-2500000.0, 0.0, 2500000.0})
        bands.push_back({kCarrier + offset - 500000, kCarrier + offset, kCarrier + offset + 500000});
    auto signal = Create<SpectrumSignalParameters>();
    signal->psd = Create<SpectrumValue>(Create<SpectrumModel>(bands));
    (*signal->psd)[0] = 4; (*signal->psd)[1] = 1; (*signal->psd)[2] = 9;
    return signal;
}

Ptr<SpectrumSignalParameters> CachedPsd(Ptr<SionnaRtSpectrumPropagationLossModel> spectrum,
                                      Cohort& cohort, size_t a = 0, size_t b = 1)
{
    return spectrum->DoCalcRxPowerSpectralDensity(Signal(), cohort.mobility[a], cohort.mobility[b],
                                                 cohort.antennas[a], cohort.antennas[b]);
}

void CompareDirect(const Cache::Record& record, Ptr<SpectrumSignalParameters> actual,
                   Cohort& cohort)
{
    auto reference = CreateObject<DirectSpectrum>();
    reference->SetChannelModelAttribute("Frequency", DoubleValue(kCarrier));
    auto matrix = Create<Cache::ChannelMatrix>(*record.matrix);
    const auto a = record.direction[0], b = record.direction[1];
    auto parameters = Create<SionnaRtChannelModel::SionnaRtChannelParams>(*record.parameters);
    const auto longTerm = reference->CalcLongTerm(matrix, cohort.antennas[a], cohort.antennas[b]);
    const auto direct = reference->CalcBeamformingGain(Signal(), longTerm, matrix, parameters,
        cohort.mobility[a]->GetVelocity(), cohort.mobility[b]->GetVelocity(), 1, 1, false);
    for (size_t bin = 0; bin < 3; ++bin)
    {
        Require(std::abs((*actual->psd)[bin] - (*direct->psd)[bin]) < 1e-10,
                "Cached PSD differs from original Sionna spectrum calculation");
        Require(std::abs((*actual->spectrumChannelMatrix)(0, 0, bin) -
                         (*direct->spectrumChannelMatrix)(0, 0, bin)) < 1e-10,
                "Cache changed the complex coherent spectrum channel");
    }
    reference->Dispose();
}

void RfAndIdentity()
{
    Cohort cohort;
    auto cache = cohort.New();
    DoubleValue frequency;
    cache->GetAttribute("Frequency", frequency);
    Require(frequency.Get() == kCarrier, "Spectrum cannot read the fixed cache carrier");
    Require(!cache->SetAttributeFailSafe("Frequency", DoubleValue(5180000000)), "Cache carrier was mutable");
    Require(!cache->GetParams(cohort.mobility[0], cohort.mobility[1]), "Missing pair had parameters");
    Throws([&] { cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]); },
           "Missing CIR was silently admitted");
    auto record = cohort.Sample();
    Advance(MilliSeconds(10)); // Publication completes after its original sample.
    Require(cache->Publish(record).accepted, "Fresh delayed publication was rejected");
    const auto channel = cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]);
    const auto params = cache->GetParams(cohort.mobility[0], cohort.mobility[1]);
    Require(channel->m_generatedTime == NanoSeconds(record.sampleNativeNs) &&
            params->m_generatedTime == channel->m_generatedTime, "Completion time replaced the sample epoch");
    Require(channel->m_nodeIds == std::make_pair(cohort.mobility[0]->GetObject<Node>()->GetId(),
                cohort.mobility[1]->GetObject<Node>()->GetId()) && params->m_nodeIds == channel->m_nodeIds,
            "Worker-local node IDs leaked into the native channel");
    Require(!channel->IsReverse(cohort.antennas[0]->GetId(), cohort.antennas[1]->GetId()) &&
             channel->IsReverse(cohort.antennas[1]->GetId(), cohort.antennas[0]->GetId()),
            "Cache antenna identity or reciprocity is wrong");
    Require(cache->GetChannel(cohort.mobility[1], cohort.mobility[0], cohort.antennas[1], cohort.antennas[0]) == channel,
            "Reciprocal lookup did not return the same CIR object");
    auto spectrum = CreateObject<SionnaRtSpectrumPropagationLossModel>();
    spectrum->SetChannelModel(cache);
    auto actual = CachedPsd(spectrum, cohort);
    CompareDirect(record, actual, cohort);
    const std::array<double, 3> golden{5, 2.25, 11.25}; // 25 Hz * 10 ms = +pi/2.
    for (size_t bin = 0; bin < golden.size(); ++bin)
        Require(std::abs((*actual->psd)[bin] - golden[bin]) < 1e-10,
                "Cache retimestamped or changed the analytic two-path Doppler PSD");
    CompareDirect(record, CachedPsd(spectrum, cohort, 1, 0), cohort);

    // Producer mutation after acceptance must not change a live coherent CIR.
    auto mutableMatrix = ConstCast<Cache::ChannelMatrix>(record.matrix);
    auto mutableParams = ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(record.parameters);
    mutableMatrix->m_channel(0, 0, 0) = {77, 0}; mutableParams->m_doppler[1] = 999;
    auto frozen = CachedPsd(spectrum, cohort);
    Require(std::abs((*frozen->psd)[1] - 2.25) < 1e-10, "Publisher mutation changed an accepted CIR");

    // Same generation timestamp, a different coherent result. Native long-term
    // cache must key the matrix identity, not just its timestamp.
    auto replacement = cohort.Sample(2, 1, 0, record.sampleNativeNs);
    auto changed = ConstCast<Cache::ChannelMatrix>(replacement.matrix);
    changed->m_channel(0, 0, 0) = {0.7, 0.2};
    auto changedParams = ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(replacement.parameters);
    changedParams->m_doppler = {-11, 37};
    Require(cache->Publish(replacement).accepted, "New same-epoch coherent result was rejected");
    CompareDirect(replacement, CachedPsd(spectrum, cohort, 1, 0), cohort);
    const auto after = cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]);
    Require(after != channel && after->IsReverse(cohort.antennas[0]->GetId(), cohort.antennas[1]->GetId()),
            "Replacement did not retain its actual source direction");
    Require(!cache->Publish(record).accepted && cache->GetChannel(cohort.mobility[0], cohort.mobility[1],
                cohort.antennas[0], cohort.antennas[1]) == after, "Out-of-order result replaced the live cache");
    Require(cache->GetStats().accepted == 2 && cache->GetStats().rejected == 1 &&
            cache->GetStats().hits > 0 && cache->GetStats().misses == 1,
            "Cache hit/publication statistics do not describe actual calls");
    spectrum->Dispose(); cache->Dispose();
    Require(cohort.mobility[0]->GetObject<Node>() != nullptr, "Cache disposed a run-owned endpoint");
    cohort.mobility[0]->SetPosition(Vector(2, 3, 4));
    Require(cohort.mobility[0]->GetPosition().x == 2, "Cache disposal damaged run-owned mobility");
    Require(!cache->GetParams(cohort.mobility[0], cohort.mobility[1]), "Disposed cache returned parameters");
    Throws([&] { cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]); },
           "Disposed cache was admitted");
    const auto measured = cache->GetStats();
    Require(measured.getChannelCalls == measured.hits + measured.misses &&
            measured.misses == 2 && measured.getChannelWallNs >= measured.maxGetChannelWallNs,
            "Lookup timing did not cover both successful and exception paths");
    Simulator::Destroy();
}

void Guards()
{
    Cohort cohort;
    auto cache = cohort.New();
    auto record = cohort.Sample();
    Require(cache->Publish(record).accepted, "Initial guard CIR was rejected");
    auto read = [&] { cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]); };
    cohort.mobility[1]->SetPosition(Vector(10.25, 0, 20));
    Require(cache->PairState(0, 1).valid && cache->PairState(0, 1).due, "Half-displacement prefetch warning missing");
    cohort.mobility[1]->SetPosition(Vector(10.5, 0, 20));
    Require(cache->PairState(0, 1).valid, "Exact allowed displacement was rejected");
    cohort.mobility[1]->SetPosition(Vector(10.500001, 0, 20));
    Throws(read, "Displaced endpoint was admitted");
    cohort.mobility[1]->SetPosition(Vector(10, 0, 20));
    cohort.mobility[1]->SetVelocity(Vector(0.25, 0, 0));
    Require(cache->PairState(0, 1).valid && cache->PairState(0, 1).due, "Half-velocity warning missing");
    cohort.mobility[1]->SetVelocity(Vector(0.5, 0, 0));
    Throws(read, "Exact forbidden velocity delta was admitted");
    cohort.mobility[1]->SetVelocity(Vector());
    cohort.mobility[1]->orientation.x = 2.5 * M_PI / 180;
    Require(cache->PairState(0, 1).valid && cache->PairState(0, 1).due, "Half-orientation warning missing");
    cohort.mobility[1]->orientation.x = 5 * M_PI / 180;
    Throws(read, "Exact forbidden orientation delta was admitted");
    cohort.mobility[1]->orientation.x = M_PI - 0.01;
    auto wrapped = cohort.Sample(2);
    Require(cache->Publish(wrapped).accepted, "Fresh wrap-angle sample was rejected");
    cohort.mobility[1]->orientation.x = -M_PI + 0.01;
    Require(cache->PairState(0, 1).valid, "Euler wrap was mistaken for a large body rotation");
    cohort.mobility[1]->orientation.x = M_PI - 0.01;
    Advance(MilliSeconds(250));
    Require(cache->PairState(0, 1).valid && cache->PairState(0, 1).due, "Half-age warning missing");
    Advance(MilliSeconds(250));
    Require(cache->PairState(0, 1).valid, "Exact allowed native age was rejected");
    Advance(NanoSeconds(1));
    Require(cache->PairState(0, 1).reason == "native_sample_expired", "Cache age used completion instead of sample");
    Throws(read, "Expired native sample was admitted");
    Require(!cache->Publish(record).accepted, "Old result was accepted again");
    cache->Dispose(); Simulator::Destroy();

    Cohort host;
    Cache::Limits limits; limits.maximumAge = MilliSeconds(150);
    auto hostCache = host.New(limits);
    auto delayed = host.Sample();
    delayed.sampleHostMonotonicNs -= 100000000;
    for (auto& pose : delayed.poses) pose.sampleHostNs = delayed.sampleHostMonotonicNs;
    Require(hostCache->Publish(delayed).accepted, "Timely delayed host sample was rejected");
    Require(hostCache->PairState(0, 1).due, "Half-host-age warning missing");
    std::this_thread::sleep_for(std::chrono::milliseconds(70));
    Require(hostCache->PairState(0, 1).reason == "host_sample_expired", "Host age was reset on completion");
    Throws([&] { hostCache->GetChannel(host.mobility[0], host.mobility[1], host.antennas[0], host.antennas[1]); },
           "Expired host sample was admitted");
    hostCache->Dispose(); Simulator::Destroy();
}

void RejectInputs()
{
    Cohort cohort;
    auto cache = cohort.New();
    auto reject = [&](Cache::Record record, const std::string& reason) {
        const auto result = cache->Publish(record);
        Require(!result.accepted && result.reason == reason, "Invalid result was not rejected: " + reason);
    };
    auto sample = cohort.Sample();
    ConstCast<Cache::ChannelMatrix>(sample.matrix)->m_generatedTime += NanoSeconds(1);
    reject(sample, "generation_epoch_mismatch");
    sample = cohort.Sample();
    ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(sample.parameters)->m_generatedTime += NanoSeconds(1);
    reject(sample, "generation_epoch_mismatch");
    sample = cohort.Sample(); sample.sampleNativeNs += 1;
    reject(sample, "generation_epoch_mismatch");
    sample = cohort.Sample(1, 0, 1, Simulator::Now().GetNanoSeconds() + 1);
    reject(sample, "future_native_sample");
    sample = cohort.Sample(); sample.sampleHostMonotonicNs += 1000000000;
    reject(sample, "future_host_sample");
    sample = cohort.Sample(); sample.poses[0].sampleHostNs -= 600000000;
    reject(sample, "host_sample_expired");
    sample = cohort.Sample(); sample.direction[1] = 6; reject(sample, "unknown_pair");
    sample = cohort.Sample(); sample.poses[0].position.x = std::numeric_limits<double>::quiet_NaN();
    reject(sample, "invalid_sample_pose");
    sample = cohort.Sample(); ConstCast<Cache::ChannelMatrix>(sample.matrix)->m_channel = Cache::Complex3DVector(2, 1, 2);
    reject(sample, "invalid_cir_shape");
    sample = cohort.Sample(); ConstCast<Cache::ChannelMatrix>(sample.matrix)->m_channel = Cache::Complex3DVector(1, 1, 91);
    reject(sample, "invalid_cir_shape");
    sample = cohort.Sample(); ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(sample.parameters)->m_angle[1].pop_back();
    reject(sample, "invalid_angle_shape");
    sample = cohort.Sample(); ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(sample.parameters)->m_delay[1] = -1;
    reject(sample, "invalid_path_delay");
    sample = cohort.Sample(); ConstCast<Cache::ChannelMatrix>(sample.matrix)->m_channel(0, 0, 1) = {0, 0};
    ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(sample.parameters)->m_delay[1] = -1;
    Require(cache->Publish(sample).accepted, "Sionna zero-amplitude padding with tau=-1 was rejected");
    auto invalid = cohort.Sample(2);
    ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(invalid.parameters)->m_doppler[0] = std::numeric_limits<double>::infinity();
    reject(invalid, "nonfinite_cir");
    invalid = cohort.Sample(2); ConstCast<SionnaRtChannelModel::SionnaRtChannelParams>(invalid.parameters)->m_angle[0][0] = std::numeric_limits<double>::quiet_NaN();
    reject(invalid, "nonfinite_angle");
    invalid = cohort.Sample(2); ConstCast<Cache::ChannelMatrix>(invalid.matrix)->m_channel(0, 0, 0) = {std::numeric_limits<double>::infinity(), 0};
    reject(invalid, "nonfinite_cir");
    auto foreign = CreateObject<ConstantPositionMobilityModel>();
    Throws([&] { cache->GetChannel(foreign, cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]); },
           "Unknown roster mobility was admitted");
    auto array = CreateObject<UniformPlanarArray>();
    Throws([&] { cache->GetChannel(cohort.mobility[0], cohort.mobility[1], array, cohort.antennas[1]); },
           "Unknown array identity was admitted");
    Require(!cache->GetParams(foreign, cohort.mobility[1]), "Unknown pair returned parameters");
    ConstCast<AntennaModel>(cohort.antennas[0]->GetAntennaElement())->SetAttribute("Gain", DoubleValue(3));
    Throws([&] { cache->GetChannel(cohort.mobility[0], cohort.mobility[1], cohort.antennas[0], cohort.antennas[1]); },
           "Changed array gain was admitted");
    cache->Dispose(); Simulator::Destroy();

    Cohort full(6);
    auto complete = full.New();
    const auto priorities = complete->Priorities();
    Require(priorities.size() == 15 && !priorities[0].valid && priorities[0].due,
            "Full-cohort priorities did not expose all 15 missing pairs");
    auto duplicate = full.endpoints; duplicate[1] = duplicate[0];
    Throws([&] { CreateObject<Cache>(kCarrier, duplicate, Cache::Limits{}); }, "Duplicate roster was accepted");
    Throws([&] { CreateObject<Cache>(kCarrier, std::vector<Cache::Endpoint>{full.endpoints[0]}, Cache::Limits{}); },
           "Single-endpoint roster was accepted");
    Throws([&] { CreateObject<Cache>(0, full.endpoints, Cache::Limits{}); }, "Unknown carrier was accepted");
    complete->Dispose(); Simulator::Destroy();
}

std::string Quote(const std::string& value)
{
    std::string result = "\"";
    for (char c : value)
    {
        if (c == '"' || c == '\\') result += '\\';
        if (c == '\n') result += "\\n"; else result += c;
    }
    return result + '"';
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2 || std::filesystem::exists(argv[1]))
        throw std::runtime_error("Expected a new result.json path; preserve every attempt");
    std::string error;
    try { RfAndIdentity(); Guards(); RejectInputs(); }
    catch (const std::exception& failure) { error = failure.what(); Simulator::Destroy(); }
    std::ofstream report(argv[1]);
    report << "{\"schema\":1,\"passed\":" << (error.empty() ? "true" : "false")
           << ",\"checks\":" << g_checks << ",\"scope\":\"CPU native CIR cache and production Sionna spectrum boundary; no worker, RT or hardware acceptance\""
           << ",\"error\":" << Quote(error) << "}\n";
    if (!error.empty()) std::cerr << error << '\n';
    return error.empty() ? 0 : 1;
}
