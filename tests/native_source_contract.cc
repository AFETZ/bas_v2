// Compile as an ns-3 scratch target against the pinned private runtime.
// No scene solve, network sockets, wall-clock scheduler or GPU is used.
#include "pybind11/embed.h"
#include "ns3/core-module.h"
#include "ns3/spectrum-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-spectrum-propagation-loss-model.h"
#include "native-spectrum-sources.h"
#include "native-rt-cache.h"
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
    catch (const std::exception& error)
    {
        rejected = std::string(error.what()).find("hybrid scene qualification was not configured") != std::string::npos;
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
    std::cout << "PASS unconfigured hybrid and unknown profile rejection; stock Friis PSD preserved\n";
}

// CPU boundary contract: geometry classification is a named fixture here.
// The separate 4000-link GPU probe and real scene adapter tests supply geometry/
// numerical evidence. This contract uses the actual bounded NativeRtCache.
class HybridTestMobility : public ConstantVelocityMobilityModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid=TypeId("bas::HybridTestMobility").SetParent<ConstantVelocityMobilityModel>()
            .AddConstructor<HybridTestMobility>()
            .AddAttribute("Orientation","ENU Euler radians",VectorValue(),
                          MakeVectorAccessor(&HybridTestMobility::orientation),MakeVectorChecker());
        return tid;
    }
    Vector orientation;
};

Ptr<bas::SourcePropagation> CheckQualifiedHybrid()
{
    namespace py=pybind11;
    auto module=py::module_::import("scripts.product.hybrid_policy");
    py::object original=module.attr("scene_evidence");
    module.attr("scene_evidence")=py::cpp_function([](const std::string& name) {
        py::dict evidence;
        evidence["policy_id"]="qualified_siso_psd_2412_v2";
        evidence["scene_sha256"]=std::string(64,'a');
        evidence["shape_count"]=(name=="empty_fixture" ? 0 : 1);
        evidence["object_count"]=(name=="empty_fixture" ? 0 : 1);
        evidence["qualified_empty_scene"]=(name=="empty_fixture");
        evidence["qualified_flat_ground_scene"]=false;
        return evidence;
    });
    auto nodeA=CreateObject<Node>(), nodeB=CreateObject<Node>();
    auto a=CreateObject<HybridTestMobility>(), b=CreateObject<HybridTestMobility>();
    a->SetPosition({0,0,120});b->SetPosition({10,0,120});
    nodeA->AggregateObject(a);nodeB->AggregateObject(b);
    auto aa=CreateObject<UniformPlanarArray>(), ba=CreateObject<UniformPlanarArray>();
    for (const auto& array : {aa,ba})
    {
        array->SetNumRows(1);array->SetNumColumns(1);
        array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
        PhasedArrayModel::ComplexVector weights(1);weights[0]={1,0};
        array->SetBeamformingVector(weights);
    }
    using Cache=bas::NativeRtCache;
    Cache::Limits limits;limits.maximumAge=MilliSeconds(100);
    auto cache=CreateObject<Cache>(2412000000.0,std::vector<Cache::Endpoint>{{a,aa},{b,ba}},limits);
    Cache::Record record;record.direction={0,1};record.sourceRequestId=1;
    record.sampleNativeNs=Simulator::Now().GetNanoSeconds();record.sampleHostMonotonicNs=Cache::HostNowNs();
    record.poses={Cache::Pose{a->GetPosition(),{}, {},record.sampleHostMonotonicNs},
                  Cache::Pose{b->GetPosition(),{}, {},record.sampleHostMonotonicNs}};
    auto matrix=Create<Cache::ChannelMatrix>();
    matrix->m_channel=Cache::Complex3DVector(1,1,1);matrix->m_channel(0,0,0)={1,0};
    matrix->m_generatedTime=NanoSeconds(record.sampleNativeNs);
    auto parameters=Create<SionnaRtChannelModel::SionnaRtChannelParams>();
    parameters->m_generatedTime=matrix->m_generatedTime;
    parameters->m_delay={0};parameters->m_doppler={0};parameters->m_angle.assign(4,std::vector<double>(1,0));
    record.matrix=matrix;record.parameters=parameters;
    Require(cache->Publish(record).accepted,"hybrid fixture cache rejected a fresh valid record");
    auto spectrum=CreateObject<SionnaRtSpectrumPropagationLossModel>();spectrum->SetChannelModel(cache);
    auto router=CreateObject<bas::SourcePropagation>();router->profile="hybrid";router->reference=spectrum;
    router->ConfigureHybridScene("empty_fixture");
    Bands bands{{2401000000,2402000000,2403000000},{2411000000,2412000000,2413000000},
                {2421000000,2422000000,2423000000}};
    auto signal=Create<SpectrumSignalParameters>();
    signal->psd=Create<SpectrumValue>(Create<SpectrumModel>(bands));*signal->psd=1e-9;
    auto stock=CreateObject<FriisSpectrumPropagationLossModel>();
    auto expected=stock->CalcRxPowerSpectralDensity(signal,a,b);
    auto result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
    for (size_t index=0;index<bands.size();++index)
        Require((*result->psd)[index]==(*expected)[index],"hybrid changed or doubled stock Friis loss");
    Require(router->HybridFriisEvaluations()==1 && cache->GetStats().hits==1,
            "Friis branch bypassed the actual RT cache guard");
    Require(router->HybridSnapshotJson().find("qualified_empty_static_siso_psd")!=std::string::npos,
            "hybrid selected model/reason is absent from per-link evidence");

    // The emitter lookup must use its registered RT model, not silently fall
    // back to the desired channel. No physical modem claim is made.
    auto emitter=CreateObject<WaveformGenerator>();signal->txPhy=emitter;
    router->sources[emitter]=spectrum;router->reference=nullptr;
    result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
    Require(router->HybridFriisEvaluations()==2,"emitter did not use the shared qualified dispatcher");
    signal->txPhy=nullptr;
    bool unavailable=false;
    try { router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba); }
    catch (const std::runtime_error&) { unavailable=true; }
    Require(unavailable,"missing desired RT reference silently selected Friis");
    router->reference=spectrum;
    b->SetVelocity({.1,0,0});
    expected=spectrum->CalcRxPowerSpectralDensity(signal,a,b,aa,ba)->psd;
    result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
    for (size_t index=0;index<bands.size();++index)
        Require((*result->psd)[index]==(*expected)[index],"moving endpoint did not retain RT PSD");
    Require(router->HybridRtEvaluations()==1 &&
            router->HybridSnapshotJson().find("moving_endpoint")!=std::string::npos,
            "moving endpoint fallback is not visible");
    b->SetVelocity({});
    auto terrain=CreateObject<bas::SourcePropagation>();terrain->profile="hybrid";terrain->reference=spectrum;
    terrain->ConfigureHybridScene("terrain_fixture");
    expected=spectrum->CalcRxPowerSpectralDensity(signal,a,b,aa,ba)->psd;
    result=terrain->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
    for (size_t index=0;index<bands.size();++index)
        Require((*result->psd)[index]==(*expected)[index],"unqualified geometry did not retain RT PSD");
    Require(terrain->HybridSnapshotJson().find("unqualified_geometry")!=std::string::npos,
            "geometry fallback is not visible");

    // A cached sampled zero is retained as an RT result, even with aligned
    // orientations; a padded/zero CIR page must not be turned into Friis power.
    ++record.sourceRequestId;record.sampleHostMonotonicNs=Cache::HostNowNs();
    for (auto& pose : record.poses) pose.sampleHostNs=record.sampleHostMonotonicNs;
    matrix->m_channel(0,0,0)={0,0};
    Require(cache->Publish(record).accepted,"fresh zero reference CIR was rejected");
    result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
    for (size_t index=0;index<bands.size();++index)
        Require((*result->psd)[index]==0,"sampled zero RT reference was replaced with Friis power");
    Require(router->HybridFriisEvaluations()==2 &&
            router->HybridSnapshotJson().find("reference_not_single_nonzero_path")!=std::string::npos,
            "zero-reference RT fallback is not visible");

    // Effective measured body orientation overrides the UPA's zero bearing
    // and downtilt in the production RT pool. Keep a real, freshly published
    // CIR for each case, including a zero cross-polarized reference.
    for (const auto& values : {std::pair{Vector(0,0,M_PI/2),std::complex<double>(0,0)},
                               std::pair{Vector(M_PI/2,0,0),std::complex<double>(.5,.25)}})
    {
        b->orientation=values.first;
        ++record.sourceRequestId;record.sampleHostMonotonicNs=Cache::HostNowNs();
        record.poses={Cache::Pose{a->GetPosition(),a->GetVelocity(),a->orientation,record.sampleHostMonotonicNs},
                      Cache::Pose{b->GetPosition(),b->GetVelocity(),b->orientation,record.sampleHostMonotonicNs}};
        matrix->m_channel(0,0,0)=values.second;
        Require(cache->Publish(record).accepted,"body-orientation CIR fixture was not fresh/current");
        expected=spectrum->CalcRxPowerSpectralDensity(signal,a,b,aa,ba)->psd;
        result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
        for (size_t index=0;index<bands.size();++index)
            Require((*result->psd)[index]==(*expected)[index],"body orientation incorrectly selected co-polar Friis");
        Require(router->HybridFriisEvaluations()==2 &&
                router->HybridSnapshotJson().find("unqualified_orientation")!=std::string::npos,
                "effective orientation fallback is not visible");
    }
    b->orientation={};
    ++record.sourceRequestId;record.sampleHostMonotonicNs=Cache::HostNowNs();
    record.poses={Cache::Pose{a->GetPosition(),{}, {},record.sampleHostMonotonicNs},
                  Cache::Pose{b->GetPosition(),{}, {},record.sampleHostMonotonicNs}};
    matrix->m_channel(0,0,0)={1,0};
    Require(cache->Publish(record).accepted,"neutral CIR was not restored before the age-guard test");

    // Advance model time without changing the sample epoch. A mathematically
    // available Friis formula must not hide the same expired RT cohort.
    Simulator::Schedule(MilliSeconds(101),[]{});Simulator::Run();
    bool expired=false;
    try { router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba); }
    catch (const std::runtime_error&) { expired=true; }
    Require(expired && router->HybridFriisEvaluations()==2,"hybrid bypassed the 100ms RT cache limit");
    // EventSink is generic: unlike the actual stateless main callback it may
    // own Python state. Clear outside the GIL and require the capture to be
    // released immediately, before the router's ordinary C++ destruction.
    bool captureReleasedWithGil=false;
    auto clearedForLateDestruction=CreateObject<bas::SourcePropagation>();
    py::object callbackToken=py::module_::import("builtins").attr("object")();
    const auto initialReferences=Py_REFCNT(callbackToken.ptr());
    auto keep=std::shared_ptr<py::object>(new py::object(callbackToken),
        [&captureReleasedWithGil](py::object* captured) {
            captureReleasedWithGil=PyGILState_Check()!=0;
            delete captured;
        });
    clearedForLateDestruction->ConfigureHybridScene("empty_fixture",
        [keep](const auto&,const auto&,double,const auto&) { (void)keep; });
    keep.reset();
    Require(Py_REFCNT(callbackToken.ptr())>initialReferences,
            "hybrid lifecycle fixture did not retain its Python callback capture");
    {
        py::gil_scoped_release release;
        clearedForLateDestruction->ClearSourceSchedule();
        clearedForLateDestruction->ClearSourceSchedule();
    }
    Require(Py_REFCNT(callbackToken.ptr())==initialReferences,
            "hybrid Python callback capture survived ClearSourceSchedule");
    Require(captureReleasedWithGil,"hybrid callback capture was destroyed without the GIL");
    std::cout << "PASS hybrid callback capture released under GIL and idempotent cleanup\n";
    router->Dispose();terrain->Dispose();spectrum->Dispose();cache->Dispose();
    module.attr("scene_evidence")=original;
    Simulator::Destroy();
    std::cout << "PASS qualified hybrid shared desired/emitter dispatch; native cache/RT fallback/100ms guards\n";
    return clearedForLateDestruction;
}

void CheckGuardedGroundHybrid()
{
    namespace py=pybind11;
    using namespace pybind11::literals;
    auto module=py::module_::import("scripts.product.hybrid_policy");
    py::object original=module.attr("scene_evidence");
    module.attr("scene_evidence")=py::cpp_function([](const std::string&) {
        return py::dict("policy_id"_a="qualified_siso_psd_2412_v2",
            "scene_sha256"_a="905807072f010dbca786a3842919de50d1d3988656bb08400cb428c1166acdde",
            "shape_count"_a=1,"object_count"_a=1,
            "qualified_empty_scene"_a=false,"qualified_flat_ground_scene"_a=true);
    });
    auto nodeA=CreateObject<Node>(),nodeB=CreateObject<Node>();
    auto a=CreateObject<HybridTestMobility>(),b=CreateObject<HybridTestMobility>();
    a->SetPosition({0,0,2});b->SetPosition({80,0,20});
    nodeA->AggregateObject(a);nodeB->AggregateObject(b);
    auto aa=CreateObject<UniformPlanarArray>(),ba=CreateObject<UniformPlanarArray>();
    for(const auto& array:{aa,ba})
    {
        array->SetNumRows(1);array->SetNumColumns(1);
        array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
        PhasedArrayModel::ComplexVector weights(1);weights[0]={1,0};array->SetBeamformingVector(weights);
    }
    using Cache=bas::NativeRtCache;
    Cache::Limits limits;limits.maximumAge=MilliSeconds(100);
    auto cache=CreateObject<Cache>(2412000000.,std::vector<Cache::Endpoint>{{a,aa},{b,ba}},limits);
    auto spectrum=CreateObject<SionnaRtSpectrumPropagationLossModel>();spectrum->SetChannelModel(cache);
    auto router=CreateObject<bas::SourcePropagation>();router->profile="hybrid";router->reference=spectrum;
    router->hybridCommandPost=a;router->ConfigureHybridScene("ground_boundary_fixture");
    Bands bands{{2401000000,2402000000,2403000000},{2411000000,2412000000,2413000000},
                {2421000000,2422000000,2423000000}};
    auto signal=Create<SpectrumSignalParameters>();
    signal->psd=Create<SpectrumValue>(Create<SpectrumModel>(bands));*signal->psd=1e-9;
    (*signal->psd)[1]=0; // zero-input bins cannot turn an otherwise valid guard into zero-reference failure.
    auto stock=CreateObject<FriisSpectrumPropagationLossModel>();
    uint64_t request=0;
    auto publish=[&](double powerScale,double secondDelay=0,bool overflow=false) {
        Cache::Record record;record.direction={0,1};record.sourceRequestId=++request;
        record.sampleNativeNs=Simulator::Now().GetNanoSeconds();record.sampleHostMonotonicNs=Cache::HostNowNs();
        record.poses={Cache::Pose{a->GetPosition(),a->GetVelocity(),a->orientation,record.sampleHostMonotonicNs},
                      Cache::Pose{b->GetPosition(),b->GetVelocity(),b->orientation,record.sampleHostMonotonicNs}};
        auto matrix=Create<Cache::ChannelMatrix>();
        matrix->m_channel=Cache::Complex3DVector(1,1,2);
        const auto amplitude=overflow ? 1e308 :
            .5*std::sqrt(powerScale/stock->CalculateLoss(2412000000.,CalculateDistance(a->GetPosition(),b->GetPosition())));
        matrix->m_channel(0,0,0)={amplitude,0};matrix->m_channel(0,0,1)={amplitude,0};
        matrix->m_generatedTime=NanoSeconds(record.sampleNativeNs);
        auto parameters=Create<SionnaRtChannelModel::SionnaRtChannelParams>();
        parameters->m_generatedTime=matrix->m_generatedTime;
        parameters->m_delay={0,secondDelay};parameters->m_doppler={0,0};
        parameters->m_angle.assign(4,std::vector<double>(2,0));
        record.matrix=matrix;record.parameters=parameters;
        Require(cache->Publish(record).accepted,"ground fixture fresh multipath reference was rejected");
    };
    auto calculate=[&] {
        const auto before=cache->GetStats().hits;
        auto result=router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
        Require(cache->GetStats().hits==before+1,"ground dispatch evaluated native RT more than once");
        Require(result!=signal && result->psd!=signal->psd,"ground dispatch mutated its original signal");
        return result;
    };
    auto requireRt=[&](const std::string& reason) {
        auto result=calculate();
        auto expected=spectrum->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);
        for(size_t i=0;i<bands.size();++i)
            Require((*result->psd)[i]==(*expected->psd)[i],"ground fallback changed/doubled native RT attenuation");
        Require(router->HybridSnapshotJson().find(reason)!=std::string::npos,"ground fallback reason is absent");
    };
    auto requireFriis=[&] {
        auto result=calculate();auto expected=stock->CalcRxPowerSpectralDensity(signal,a,b);
        for(size_t i=0;i<bands.size();++i)
            Require((*result->psd)[i]==(*expected)[i],"guarded ground changed/doubled stock Friis attenuation");
    };
    publish(1);requireFriis();
    const auto reverseHits=cache->GetStats().hits;
    auto reverse=router->CalcRxPowerSpectralDensity(signal,b,a,ba,aa);
    auto reverseExpected=stock->CalcRxPowerSpectralDensity(signal,b,a);
    Require(cache->GetStats().hits==reverseHits+1,"reverse ground dispatch did not evaluate RT exactly once");
    for(size_t i=0;i<bands.size();++i)
        Require((*reverse->psd)[i]==(*reverseExpected)[i],"reverse CP role or single attenuation is wrong");
    publish(std::pow(10.,.9/10));requireFriis();
    publish(std::pow(10.,1.1/10));requireRt("ground_rss_budget_exceeded");
    publish(1,50e-9);requireRt("ground_rss_budget_exceeded");
    publish(0);requireRt("ground_reference_zero");
    // A positive reference on one band cannot admit a silent/invalid other positive-input band.
    *signal->psd=0;publish(1);requireRt("ground_no_positive_input");*signal->psd=1e-9;
    // Current pose bounds are independent from the reference's allowed five-degree cache tolerance.
    b->orientation={2.9,.034,0};b->SetVelocity({2.9,0,0});publish(1);requireFriis();
    b->orientation.y=2.1*M_PI/180;publish(1);requireRt("unqualified_ground_pose");
    b->orientation={};b->SetVelocity({});b->SetPosition({80,0,20.5001});publish(1);
    requireRt("unqualified_ground_pose");
    b->SetPosition({80,0,20});publish(1);b->orientation={0,5*M_PI/180,0};
    bool rotationRejected=false;
    try {router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);}
    catch(const std::runtime_error&){rotationRejected=true;}
    Require(rotationRejected,"ground bypassed existing five-degree reference orientation guard");
    b->orientation={};publish(1);
    b->SetVelocity({3.001,0,0});publish(1);requireRt("unqualified_ground_velocity");
    b->SetVelocity({});a->SetPosition({.000001,0,2});publish(1);requireRt("unqualified_ground_pose");
    a->SetPosition({0,0,2});publish(1);
    // Coordinates do not grant a command-post role, including a coincident emitter.
    router->hybridCommandPost=nullptr;requireRt("unqualified_ground_endpoint_role");router->hybridCommandPost=a;
    // A registered source must use its own channel, even without a desired reference.
    auto emitter=CreateObject<WaveformGenerator>();signal->txPhy=emitter;
    router->sources[emitter]=spectrum;router->reference=nullptr;
    requireRt("unqualified_ground_endpoint_role");
    signal->txPhy=nullptr;
    bool missingRejected=false;
    try {router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);}
    catch(const std::runtime_error&){missingRejected=true;}
    Require(missingRejected,"ground silently selected Friis without desired RT");
    router->reference=spectrum;
    // Finite source CIR coefficients may overflow the native PSD calculation.
    // Returning its NaN/Inf to PHY is forbidden; unavailable measurements remain fatal.
    publish(1,0,true);
    bool nonfiniteRejected=false;
    try {router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);}
    catch(const std::runtime_error& error) {
        nonfiniteRejected=std::string(error.what()).find("nonfinite_or_negative_power")!=std::string::npos;
    }
    Require(nonfiniteRejected,"ground exposed invalid native PSD or hid it with Friis");
    publish(1);
    Simulator::Schedule(MilliSeconds(101),[]{});Simulator::Run();
    bool expired=false;
    try {router->CalcRxPowerSpectralDensity(signal,a,b,aa,ba);}
    catch(const std::runtime_error&){expired=true;}
    Require(expired,"guarded ground bypassed the100ms RT reference limit");
    router->Dispose();spectrum->Dispose();cache->Dispose();module.attr("scene_evidence")=original;
    Simulator::Destroy();
    std::cout<<"PASS guarded ground actual RT-cache PSD: multipath inside/outside/all-bin/zero/nonfinite; pose/role/source/100ms boundaries\n";
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 2) throw std::runtime_error("expected prepared emitter JSON");
    Ptr<bas::SourcePropagation> clearedHybrid;
    {
    pybind11::scoped_interpreter interpreter;
    CheckPropagationProfiles();
    clearedHybrid=CheckQualifiedHybrid();
    CheckGuardedGroundHybrid();
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
    Require(!Py_IsInitialized(),"late hybrid destruction test ran before Python finalization");
    clearedHybrid=nullptr;
    std::cout << "PASS cleared hybrid router destroyed after Python finalization\n";
}
