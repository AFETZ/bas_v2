// SDK12 CPU: actual tensor conversion -> reciprocal native STAR cache/guards.
// GPU: public emitter batch -> stock pairwise reference and clear-LoS checks.
// Usage: --cpu NEW.json | --gpu PREPARED_FLAT_SCENE.xml NEW.json.
// These are integration diagnostics, not terrain, MAC, timing or HIL acceptance.
#include "pybind11/embed.h"
#include "pybind11/complex.h"
#include "pybind11/stl.h"
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-channel-model.h"
#include <cmath>
#include <complex>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <numeric>

using namespace ns3;
namespace py=pybind11;
using namespace pybind11::literals;
namespace
{
size_t checks=0;
constexpr double carrier=2412000000., c=299792458.;
void Require(bool value,const std::string& reason)
{ ++checks; if(!value) throw std::runtime_error(reason); }
template<typename Call> void Reject(Call call,const std::string& reason)
{
    bool rejected=false;
    try {call();} catch(const std::exception&) {rejected=true;}
    Require(rejected,reason);
}
class OrientedMobility : public ConstantVelocityMobilityModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid=TypeId("bas::EmitterContractMobility").SetParent<ConstantVelocityMobilityModel>()
            .AddConstructor<OrientedMobility>()
            .AddAttribute("Orientation","Measured ENU yaw/pitch/roll radians",VectorValue(),
                MakeVectorAccessor(&OrientedMobility::orientation),MakeVectorChecker());
        return tid;
    }
    Vector orientation;
};
class ProbeModel : public SionnaRtChannelModel
{
  public:
    using SionnaRtChannelModel::PublishReceiverBatchArrays;
    using SionnaRtChannelModel::ValidateEmitterBatchCandidates;
};
struct Cohort
{
    size_t n;
    double frequencyHz{carrier};
    std::vector<Ptr<OrientedMobility>> moving;
    std::vector<Ptr<const MobilityModel>> poses;
    std::vector<Ptr<UniformPlanarArray>> arrays;
    std::vector<Ptr<const MobilityModel>> receiverPoses;
    std::vector<Ptr<const PhasedArrayModel>> receiverArrays;
    std::vector<size_t> receiverIndices;
    explicit Cohort(size_t receivers=6,bool directional=false):n(receivers)
    {
        // CP first, then UAVs; the external source is last. Real IDs differ.
        const std::vector<Vector> positions{{10,0,20},{0,20,30},{-10,0,20},
            {0,-20,30},{5,5,40},{20,15,25},{0,0,20}};
        (void)CreateObject<Node>(); (void)CreateObject<UniformPlanarArray>();
        for(size_t i=0;i<=n;++i)
        {
            auto node=CreateObject<Node>(); auto pose=CreateObject<OrientedMobility>();
            pose->SetPosition(i==n ? positions[6] : positions[i]); node->AggregateObject(pose);
            moving.push_back(pose);poses.push_back(pose);
            auto array=CreateObject<UniformPlanarArray>();array->SetNumRows(1);array->SetNumColumns(1);
            if(i==n && directional)array->SetAntennaElement(CreateObject<ThreeGppAntennaModel>());
            else array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1);weights[0]={1,0};array->SetBeamformingVector(weights);
            arrays.push_back(array);
            if(i<n){receiverPoses.push_back(pose);receiverArrays.push_back(array);receiverIndices.push_back(i);}
        }
    }
    Ptr<ProbeModel> Model(bool bind=true,bool normalize=false,const std::string& scene="not_loaded.xml",bool registered=true)
    {
        auto model=CreateObject<ProbeModel>();model->SetAttribute("Frequency",DoubleValue(frequencyHz));
        model->SetAttribute("Scenario",StringValue(scene));model->SetAttribute("NormalizeDelays",BooleanValue(normalize));
        model->SetAttribute("SamplesPerSource",UintegerValue(20000));
        model->SetAttribute("MaxNumPathsPerSource",UintegerValue(1000));
        model->SetAttribute("MaxNumberOfPaths",DoubleValue(90));
        model->SetAttribute("UpdatePeriod",TimeValue(Seconds(.5)));model->SetUpdateDistanceThreshold(.5);
        model->SetUpdateJitterFraction(0);
        SionnaRtChannelModel::RtPathSolverConfig solver;
        solver.maxDepth=0;solver.los=true;solver.specularReflection=false;solver.diffuseReflection=false;
        solver.refraction=false;solver.diffraction=false;solver.edgeDiffraction=false;solver.syntheticArray=true;solver.seed=42;
        model->SetRtPathSolverConfig(solver);
        if(registered){model->SetReceiverBatchEndpoints(receiverPoses,receiverArrays);model->SetReceiverBatchEnabled(true);}
        if(bind)model->SetEmitterBatchEndpoint(poses[n],arrays[n]);
        return model;
    }
    Ptr<const MatrixBasedChannelModel::ChannelMatrix> Read(Ptr<ProbeModel> model,size_t rx=0,bool reverse=false)
    {
        return reverse ? model->GetChannel(poses[rx],poses[n],arrays[rx],arrays[n])
                       : model->GetChannel(poses[n],poses[rx],arrays[n],arrays[rx]);
    }
};
struct Tensors
{
    py::object coefficients,valid;
    std::vector<py::object> scalars;
    explicit Tensors(size_t n,size_t paths=3)
    {
        auto np=py::module_::import("numpy");
        coefficients=np.attr("zeros")(py::make_tuple(n,1,1,1,paths,1),"dtype"_a="complex128");
        valid=np.attr("zeros")(py::make_tuple(n,1,paths),"dtype"_a="bool");
        for(size_t i=0;i<6;++i)scalars.push_back(np.attr("zeros")(py::make_tuple(n,1,paths),"dtype"_a="float64"));
        for(size_t rx=0;rx<n;++rx)for(size_t path=0;path<paths;++path)
        {
            const bool active=path<2;const auto key=py::make_tuple(rx,0,path);
            const auto value=active ? std::complex<double>(.001*(1+rx+path),-.0002*(1+path)) : std::complex<double>{};
            coefficients.attr("__setitem__")(py::make_tuple(rx,0,0,0,path,0),py::cast(value));
            valid.attr("__setitem__")(key,active);
            scalars[0].attr("__setitem__")(key,active ? 1e-8+path*2e-8 : -1.);
            scalars[1].attr("__setitem__")(key,active ? path*25. : 0.);
            for(size_t a=2;a<6;++a)scalars[a].attr("__setitem__")(key,active ? .1*a+.01*rx : 0.);
        }
    }
    void Publish(Cohort& cohort,Ptr<ProbeModel> model)
    {model->PublishReceiverBatchArrays(coefficients,scalars,valid,cohort.poses[cohort.n],cohort.arrays[cohort.n],cohort.receiverIndices);}
};
void Cpu(py::dict& report)
{
    for(bool directional:{false,true})for(size_t n=2;n<=6;++n)
    {
        Cohort cohort(n,directional);auto model=cohort.Model();Tensors data(n);
        Reject([&]{cohort.Read(model);},"Missing STAR silently invoked an RT solve");data.Publish(cohort,model);
        for(size_t rx=0;rx<n;++rx)
        {
            auto matrix=cohort.Read(model,rx),reverse=cohort.Read(model,rx,true);
            auto params=DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(model->GetParams(cohort.poses[n],cohort.poses[rx]));
            Require(matrix==reverse && matrix->IsReverse(cohort.arrays[rx]->GetId(),cohort.arrays[n]->GetId()),"Reciprocity/array IDs lost");
            Require(matrix->m_nodeIds==std::make_pair(cohort.poses[n]->GetObject<Node>()->GetId(),cohort.poses[rx]->GetObject<Node>()->GetId()) &&
                params->m_nodeIds==matrix->m_nodeIds,"Semantic indices substituted for actual node IDs");
            Require(matrix->m_channel.GetNumPages()==3 &&
                std::abs(matrix->m_channel(0,0,0)-std::complex<double>(.001*(1+rx),-.0002))<1e-15 &&
                std::abs(matrix->m_channel(0,0,1)-std::complex<double>(.001*(2+rx),-.0004))<1e-15 &&
                matrix->m_channel(0,0,2)==std::complex<double>{},"Wrong RX/path/complex coefficient slice");
            Require(params->m_delay.size()==3 && std::abs(params->m_delay[0]-1e-8)<1e-20 &&
                std::abs(params->m_delay[1]-3e-8)<1e-20 && params->m_delay[2]==-1 && params->m_doppler==std::vector<double>({0,25,0}) &&
                std::abs(params->m_angle[2][1]-(.4+.01*rx))<1e-15,"Delay/Doppler/radian metadata changed");
            Require(matrix->m_generatedTime==Simulator::Now() && params->m_generatedTime==matrix->m_generatedTime &&
                model->GetParams(cohort.poses[rx],cohort.poses[n])==params,"Original solve epoch/reverse params lost");
        }
        auto stats=model->GetEmitterBatchStats();
        Require(stats.sourceMode=="emitter_batch" && stats.bound && stats.hasPublishedBatch &&
            stats.pattern==(directional ? "tr38901" : "iso") && stats.frequencyHz==carrier &&
            stats.registeredReceivers==n && stats.receiverNodeIds.front()==cohort.poses[0]->GetObject<Node>()->GetId() &&
            stats.configuredCandidateCap==1000*n && stats.lastRetainedPathsPerReceiver==std::vector<uint32_t>(n,2),"Actual emitter/CP/profile/allocation provenance missing");
        Require(stats.completedCalls==0 && model->GetReceiverBatchStats().completedCalls==0 && model->GetFullMeshStats().completedCalls==0,
            "CPU tensor/cache test unexpectedly invoked RT");
        auto old=cohort.Read(model);data.Publish(cohort,model);Require(cohort.Read(model)!=old && old->m_generatedTime==Simulator::Now(),"Same-native-time refresh retained cached pointer");
        Reject([&]{model->GetChannel(cohort.poses[0],cohort.poses[1],cohort.arrays[0],cohort.arrays[1]);},"RX/RX channel accepted");
        Reject([&]{model->GetParams(cohort.poses[0],cohort.poses[1]);},"RX/RX params accepted");
        Reject([&]{model->GetParams(cohort.poses[n],cohort.poses[n]);},"Emitter diagonal accepted");
        Reject([&]{model->GetChannel(cohort.poses[n],cohort.poses[0],cohort.arrays[0],cohort.arrays[n]);},"Wrong array mapping accepted");
        auto node=CreateObject<Node>();auto outside=CreateObject<OrientedMobility>();node->AggregateObject(outside);
        Reject([&]{model->GetParams(cohort.poses[n],outside);},"Unknown STAR peer accepted");
        Reject([&]{model->SetEmitterBatchEndpoint(cohort.poses[n],cohort.arrays[n]);},"Emitter rebind accepted");
        Reject([&]{model->SetReceiverBatchEnabled(false);},"Emitter silently disabled");
        Reject([&]{model->RefreshFullMesh();},"Emitter entered full mesh");
        Reject([&]{model->RefreshReceiverBatch(cohort.poses[0],cohort.arrays[0]);},"Emitter entered receiver batch");
        Reject([&]{model->SetAttribute("Frequency",DoubleValue(carrier+10000000));},"Emitter carrier changed after binding");
        if(n==6)
        {
            model->ValidateEmitterBatchCandidates(6,6000);
            Reject([&]{model->ValidateEmitterBatchCandidates(6000,6000);},"Saturated6K candidate buffer accepted");
            Reject([&]{model->ValidateEmitterBatchCandidates(6,6001);},"Incorrect candidate allocation accepted");
            Require(model->GetEmitterBatchStats().candidateCapacityReachedCalls==1,"Saturation not recorded");
            cohort.moving[n]->SetPosition({.51,0,20});Reject([&]{cohort.Read(model);},"Source displacement ignored");data.Publish(cohort,model);
            cohort.moving[5]->orientation={0,.09,0};Reject([&]{model->GetParams(cohort.poses[n],cohort.poses[5]);},"Receiver pitch guard ignored");data.Publish(cohort,model);
            cohort.moving[n]->SetVelocity({.51,0,0});Reject([&]{cohort.Read(model);},"Source velocity guard ignored");data.Publish(cohort,model);
            cohort.moving[n]->orientation={.09,0,0};Reject([&]{cohort.Read(model);},"Source yaw guard ignored");data.Publish(cohort,model);
            cohort.moving[n]->SetVelocity({0,0,0});data.Publish(cohort,model);
            auto fresh=cohort.Read(model);Simulator::Schedule(MilliSeconds(501),[]{});Simulator::Run();
            Reject([&]{cohort.Read(model);},"Stale source CIR silently refreshed");data.Publish(cohort,model);
            Require(fresh->m_generatedTime==Seconds(0) && cohort.Read(model)->m_generatedTime==MilliSeconds(501),"Old CIR epoch was replaced");
            auto np=py::module_::import("numpy");
            const auto invalid=[&](Tensors& value,const std::string& reason){Reject([&]{value.Publish(cohort,model);},reason);Reject([&]{cohort.Read(model);},"Failed emitter publication left a usable old batch");data.Publish(cohort,model);};
            Tensors wrong(n);wrong.coefficients=np.attr("zeros")(py::make_tuple(n,1,2,1,3,1),"dtype"_a="complex128");invalid(wrong,"Wrong TX axis accepted");
            Tensors dtype(n);dtype.scalars[0]=np.attr("zeros")(py::make_tuple(n,1,3),"dtype"_a="float32");invalid(dtype,"Wrong scalar precision accepted");
            Tensors nonfinite(n);nonfinite.coefficients.attr("__setitem__")(py::make_tuple(5,0,0,0,0,0),py::cast(std::complex<double>(NAN,0)));invalid(nonfinite,"Nonfinite last RX partially published");
            Tensors negative(n);negative.scalars[0].attr("__setitem__")(py::make_tuple(0,0,0),-1.);invalid(negative,"Active negative delay accepted");
            Tensors mask(n);mask.valid.attr("__setitem__")(py::make_tuple(0,0,0),false);invalid(mask,"Invalid path with nonzero coefficient accepted");
            Tensors tooMany(n,91);invalid(tooMany,"Retained path cap bypassed");
            const auto element=ConstCast<AntennaModel>(cohort.arrays[n]->GetAntennaElement());
            if(directional)cohort.arrays[n]->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            else element->SetAttribute("Gain",DoubleValue(3));
            Reject([&]{cohort.Read(model);},"Changed source pattern/gain accepted");
            cohort.arrays[n]->SetAntennaElement(element);if(!directional)element->SetAttribute("Gain",DoubleValue(0));
            PhasedArrayModel::ComplexVector bad(1);bad[0]={.5,0};cohort.arrays[n]->SetBeamformingVector(bad);
            Reject([&]{cohort.Read(model);},"Changed source weights accepted");bad[0]={1,0};cohort.arrays[n]->SetBeamformingVector(bad);
            cohort.arrays[n]->SetAlpha(.1);Reject([&]{cohort.Read(model);},"Changed source mount accepted");cohort.arrays[n]->SetAlpha(0);
            bad[0]={.5,0};cohort.arrays[0]->SetBeamformingVector(bad);Reject([&]{cohort.Read(model);},"Changed CP weights accepted");
            bad[0]={1,0};cohort.arrays[0]->SetBeamformingVector(bad);
            cohort.moving[n]->orientation={NAN,0,0};Reject([&]{cohort.Read(model);},"Nonfinite source pose accepted");cohort.moving[n]->orientation={.09,0,0};
        }
        model->Dispose();Require(!model->GetEmitterBatchStats().bound && model->GetEmitterBatchStats().registeredReceivers==0,"Dispose retained endpoints");Simulator::Destroy();
    }
    Cohort cohort;auto disabled=cohort.Model(false,false,"not_loaded.xml",false);
    Reject([&]{disabled->SetEmitterBatchEndpoint(cohort.poses[6],cohort.arrays[6]);},"Disabled/unregistered bind accepted");disabled->Dispose();
    auto alias=cohort.Model(false);Reject([&]{alias->SetEmitterBatchEndpoint(cohort.poses[0],cohort.arrays[6]);},"Source aliases RX node");alias->Dispose();
    auto indoor=cohort.Model(false);auto element=CreateObject<ThreeGppAntennaModel>();element->SetRadiationPattern(ThreeGppAntennaModel::RadiationPattern::INDOOR);
    cohort.arrays[6]->SetAntennaElement(element);Reject([&]{indoor->SetEmitterBatchEndpoint(cohort.poses[6],cohort.arrays[6]);},"Unsupported Indoor source accepted");indoor->Dispose();
    cohort.arrays[6]->SetAntennaElement(CreateObject<IsotropicAntennaModel>());PhasedArrayModel::ComplexVector one(1);one[0]={1,0};cohort.arrays[6]->SetBeamformingVector(one);
    auto overflow=cohort.Model();overflow->SetAttribute("MaxNumPathsPerSource",UintegerValue(3000000000u));
    Reject([&]{overflow->RefreshEmitterBatch();},"Emitter candidate product overflow reached RT");overflow->Dispose();Simulator::Destroy();
    report["scope"]="CPU real tensor conversion, STAR/identity/epoch/candidate guards; no RT solve or hardware acceptance";
}
void Gpu(const std::string& scene,py::dict& report)
{
    auto rt=py::module_::import("sionna.rt");Require(py::str(rt.attr("__version__")).cast<std::string>()=="1.2.0","Pinned Sionna1.2.0 required");
    const auto variant=py::module_::import("mitsuba").attr("variant")().cast<std::string>();Require(variant.rfind("cuda",0)==0,"GPU contract cannot use CPU RT");
    py::list rows;double directionalFront=0;
    for(double frequency:{carrier,carrier+10000000})for(bool directional:{false,true})for(bool normalize:{false,true})
    {
        Cohort cohort(6,directional);cohort.frequencyHz=frequency;auto batch=cohort.Model(true,normalize,scene);
        for(size_t phase=0;phase<3;++phase)
        {
            if(phase==1)cohort.moving[6]->orientation={M_PI,0,0};
            if(phase==2){cohort.moving[6]->SetPosition({.75,.25,20});cohort.moving[6]->SetVelocity({1,.2,0});cohort.moving[5]->SetVelocity({0,.7,.1});}
            if(phase)Reject([&]{cohort.Read(batch);},"Changed source body pose/velocity did not invalidate cache");
            batch->RefreshEmitterBatch();auto baseline=cohort.Model(false,normalize,scene,false);
            for(size_t rx=0;rx<6;++rx)
            {
                const auto matrix=cohort.Read(batch,rx),reverse=cohort.Read(batch,rx,true);
                const auto reference=baseline->GetChannel(cohort.poses[6],cohort.poses[rx],cohort.arrays[6],cohort.arrays[rx]);
                const auto params=DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(batch->GetParams(cohort.poses[6],cohort.poses[rx]));
                const auto expectedParams=DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(baseline->GetParams(cohort.poses[6],cohort.poses[rx]));
                Require(matrix==reverse && matrix->IsReverse(cohort.arrays[rx]->GetId(),cohort.arrays[6]->GetId()),"Emitter reciprocal IDs failed");
                Require(matrix->m_channel.GetNumPages()==1 && params->m_delay.size()==1,"Prepared flat scene must provide one clear LoS path");
                const auto delta=cohort.poses[rx]->GetPosition()-cohort.poses[6]->GetPosition();const auto velocity=cohort.poses[6]->GetVelocity()-cohort.poses[rx]->GetVelocity();
                const double distance=delta.GetLength(),wavelength=c/frequency,gain=std::norm(matrix->m_channel(0,0,0));
                const double delay=normalize ? 0 : distance/c,doppler=(velocity.x*delta.x+velocity.y*delta.y+velocity.z*delta.z)/distance/wavelength;
                Require(std::abs(params->m_delay[0]-delay)<1e-12 && std::abs(params->m_doppler[0]-doppler)<1e-3,"Actual delay normalization/Doppler changed");
                Require(std::abs(matrix->m_channel(0,0,0)-reference->m_channel(0,0,0))/std::abs(reference->m_channel(0,0,0))<1e-3,"Batch CIR differs from stock pairwise SDK");
                for(size_t a=0;a<4;++a)Require(params->m_angle[a].size()==1 && std::abs(std::remainder(params->m_angle[a][0]-expectedParams->m_angle[a][0],2*M_PI))<1e-4,"Source/rx angle slice or radian units changed");
                if(!directional)Require(std::abs(gain/std::pow(wavelength/(4*M_PI*distance),2)-1)<1e-4,"Isotropic Friis gain inconsistent");
                if(directional && rx==0 && phase==0)
                {directionalFront=gain;Require(std::abs(10*std::log10(gain/std::pow(wavelength/(4*M_PI*distance),2))-8)<1e-3,"TR peak8dBi not included exactly once");}
                if(directional && rx==0 && phase==1)Require(std::abs(10*std::log10(gain/directionalFront)+30)<1e-3,"Measured yaw did not apply stock TR front/back30dB");
                Require(matrix->m_generatedTime.GetNanoSeconds()==0 && params->m_generatedTime==matrix->m_generatedTime,"Same-native0 refresh retimestamped CIR");
                rows.append(py::dict("pattern"_a=directional ? "tr38901" : "iso","frequency_hz"_a=frequency,"normalize_delays"_a=normalize,"phase"_a=phase,"rx"_a=rx,
                    "gain"_a=gain,"delay_s"_a=params->m_delay[0],"doppler_hz"_a=params->m_doppler[0],"angles_rad"_a=params->m_angle));
            }
            const auto stats=batch->GetEmitterBatchStats();
            Require(stats.sourceMode=="emitter_batch" && stats.completedCalls==2*phase+1 && stats.generatedLinks==6*(2*phase+1) &&
                stats.lastTransmitterCount==1 && stats.lastReceiverCount==6 && stats.lastAllocatedCandidateCap==6000 &&
                stats.frequencyHz==frequency && stats.lastCandidateBufferSize==6000 && stats.lastCandidateCount==6 && stats.lastRetainedPathsPerReceiver==std::vector<uint32_t>(6,1) &&
                stats.candidateCapacityReachedCalls==0,"Actual source six-RX candidate allocation/counts not verified");
            auto old=cohort.Read(batch);batch->RefreshEmitterBatch();Require(cohort.Read(batch)!=old,"Same-native0 public refresh reused previous matrix");
            // The additional fresh solve above is included in the next phase count.
            baseline->Dispose();
        }
        batch->Dispose();Simulator::Destroy();
    }
    report["variant"]=variant;report["observations"]=rows;
    report["scope"]="GPU public emitter6RX, stock pairwise/analytic LoS/normalized-delay/body-P-O-V contract; no dynamic full-stack or hardware qualification";
}
} // namespace
int main(int argc,char** argv)
{
    const bool cpu=argc==3 && std::string(argv[1])=="--cpu",gpu=argc==4 && std::string(argv[1])=="--gpu";
    if(!cpu && !gpu)throw std::invalid_argument("--cpu NEW.json | --gpu PREPARED_FLAT.xml NEW.json");
    const std::filesystem::path output=argv[argc-1];if(std::filesystem::exists(output))throw std::runtime_error("Preserve prior result");
    py::scoped_interpreter python;py::dict report;std::string error;
    try{if(cpu)Cpu(report);else Gpu(std::filesystem::absolute(argv[2]).string(),report);}
    catch(const std::exception& failure){error=failure.what();Simulator::Destroy();}
    report["schema"]=1;report["passed"]=error.empty();report["checks"]=checks;report["error"]=error;
    const auto encoded=py::module_::import("json").attr("dumps")(report,"indent"_a=2,"allow_nan"_a=false).cast<std::string>();
    std::ofstream(output)<<encoded<<'\n';std::cout<<encoded<<'\n';return error.empty()?0:1;
}
