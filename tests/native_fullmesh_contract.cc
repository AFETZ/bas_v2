// SDK11 contract. CPU: tensor/candidate validation -> real native reciprocal cache.
// GPU: explicit public RefreshFullMesh -> GetChannel/GetParams, analytic clear LoS.
// Usage: --cpu NEW.json | --gpu PREPARED_FLAT_SCENE.xml NEW.json.
// Neither mode qualifies dynamic full-stack timing, terrain accuracy, MAC or HIL.
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

using namespace ns3;
namespace py=pybind11;
using namespace pybind11::literals;
namespace
{
size_t checks=0;
constexpr double carrier=2412000000., c=299792458.;
void Require(bool value,const std::string& reason)
{ ++checks; if(!value) throw std::runtime_error(reason); }
void Reject(std::function<void()> call,const std::string& reason)
{
    bool rejected=false;
    try {call();} catch(const std::exception&) {rejected=true;}
    Require(rejected,reason);
}
class ProbeModel : public SionnaRtChannelModel
{
  public:
    using SionnaRtChannelModel::PublishFullMeshArrays;
    using SionnaRtChannelModel::ValidateFullMeshCandidates;
};
struct Cohort
{
    std::vector<Ptr<ConstantVelocityMobilityModel>> moving;
    std::vector<Ptr<const MobilityModel>> poses;
    std::vector<Ptr<const PhasedArrayModel>> arrays;
    explicit Cohort(size_t n)
    {
        const std::vector<Vector> positions{{0,0,20},{10,0,20},{0,20,30},{-10,0,20},{0,-20,30},{5,5,40}};
        // Shift real IDs from semantic indices; conversion must use actual IDs.
        (void)CreateObject<Node>(); (void)CreateObject<UniformPlanarArray>();
        for(size_t i=0;i<n;++i)
        {
            auto node=CreateObject<Node>(); auto pose=CreateObject<ConstantVelocityMobilityModel>();
            pose->SetPosition(positions[i]); pose->SetVelocity({.1*i,-.2*i,.03*i}); node->AggregateObject(pose);
            moving.push_back(pose); poses.push_back(pose);
            auto array=CreateObject<UniformPlanarArray>(); array->SetNumRows(1); array->SetNumColumns(1);
            array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1); weights[0]={1,0}; array->SetBeamformingVector(weights);
            arrays.push_back(array);
        }
    }
    Ptr<ProbeModel> Model(bool registered=true,bool normalize=true,const std::string& scene="not_loaded.xml")
    {
        auto model=CreateObject<ProbeModel>();
        model->SetAttribute("Frequency",DoubleValue(carrier));
        model->SetAttribute("Scenario",StringValue(scene)); model->SetAttribute("NormalizeDelays",BooleanValue(normalize));
        model->SetAttribute("SamplesPerSource",UintegerValue(20000));
        model->SetAttribute("MaxNumPathsPerSource",UintegerValue(1000));
        model->SetAttribute("MaxNumberOfPaths",DoubleValue(90));
        model->SetAttribute("UpdatePeriod",TimeValue(Seconds(.5))); model->SetUpdateDistanceThreshold(.5);
        model->SetUpdateJitterFraction(0);
        SionnaRtChannelModel::RtPathSolverConfig solver;
        solver.maxDepth=0; solver.los=true; solver.specularReflection=false; solver.diffuseReflection=false;
        solver.refraction=false; solver.diffraction=false; solver.edgeDiffraction=false; solver.syntheticArray=true; solver.seed=42;
        model->SetRtPathSolverConfig(solver);
        if(registered) { model->SetReceiverBatchEndpoints(poses,arrays); model->SetReceiverBatchEnabled(true); }
        return model;
    }
};
struct Tensors
{
    py::object coefficients,valid;
    std::vector<py::object> scalars;
    explicit Tensors(size_t n,size_t paths=3)
    {
        auto np=py::module_::import("numpy");
        coefficients=np.attr("zeros")(py::make_tuple(n,1,n,1,paths,1),"dtype"_a="complex128");
        valid=np.attr("zeros")(py::make_tuple(n,n,paths),"dtype"_a="bool");
        for(size_t i=0;i<6;++i) scalars.push_back(np.attr("zeros")(py::make_tuple(n,n,paths),"dtype"_a="float64"));
        for(size_t rx=0;rx<n;++rx) for(size_t tx=0;tx<n;++tx) for(size_t p=0;p<paths;++p)
        {
            const auto key=py::make_tuple(rx,tx,p);
            const auto active=p<2;
            const std::complex<double> value=active ? std::complex<double>(.001*(1+rx+2*tx+p),-.0002*(1+p)) : 0.;
            coefficients.attr("__setitem__")(py::make_tuple(rx,0,tx,0,p,0),py::cast(value));
            valid.attr("__setitem__")(key,active);
            scalars[0].attr("__setitem__")(key,active ? p*2e-8 : -1.);
            scalars[1].attr("__setitem__")(key,active ? p*25. : 0.);
            for(size_t i=2;i<6;++i) scalars[i].attr("__setitem__")(key,active ? .1*i+.01*rx+.02*tx : 0.);
        }
    }
    void Publish(Ptr<ProbeModel> model) {model->PublishFullMeshArrays(coefficients,scalars,valid);}
};
void Cpu(py::dict& report)
{
    for(size_t n=2;n<=6;++n)
    {
        Cohort cohort(n); auto model=cohort.Model(); Tensors data(n);
        data.Publish(model);
        for(size_t a=0;a<n;++a)
        {
            Require(!model->GetParams(cohort.poses[a],cohort.poses[a]),"Diagonal was published as a network pair");
            for(size_t b=a+1;b<n;++b)
            {
                auto matrix=model->GetChannel(cohort.poses[a],cohort.poses[b],cohort.arrays[a],cohort.arrays[b]);
                auto reverse=model->GetChannel(cohort.poses[b],cohort.poses[a],cohort.arrays[b],cohort.arrays[a]);
                auto params=DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(model->GetParams(cohort.poses[a],cohort.poses[b]));
                Require(matrix==reverse && reverse->IsReverse(cohort.arrays[b]->GetId(),cohort.arrays[a]->GetId()),"Reciprocal matrix identity changed");
                Require(matrix->m_generatedTime==Simulator::Now() && params->m_generatedTime==Simulator::Now(),"Tensor publication retimestamped the sampled epoch");
                Require(matrix->m_channel.GetNumPages()==3 &&
                    std::abs(matrix->m_channel(0,0,0)-std::complex<double>(.001*(1+b+2*a),-.0002))<1e-15 &&
                    std::abs(matrix->m_channel(0,0,1)-std::complex<double>(.001*(2+b+2*a),-.0004))<1e-15 &&
                    matrix->m_channel(0,0,2)==std::complex<double>{},"Wrong TX/RX/path tensor slice or complex phase lost");
                Require(params->m_nodeIds==std::make_pair(cohort.poses[a]->GetObject<Node>()->GetId(),cohort.poses[b]->GetObject<Node>()->GetId()) &&
                    params->m_delay==std::vector<double>({0,2e-8,-1}) && params->m_doppler==std::vector<double>({0,25,0}),"Real IDs/delays/Doppler were changed");
                Require(std::abs(params->m_angle[2][1]-(.4+.01*b+.02*a))<1e-15,"Departure angle selected another TX/RX");
            }
        }
        Require(model->GetReceiverBatchStats().completedCalls==0,"Full mesh cache read unexpectedly invoked stock RT");
        auto stats=model->GetFullMeshStats();
        Require(stats.sourceMode=="full_mesh" && stats.configuredSamplesPerSource==20000 && stats.configuredMaxNumberOfPaths==90 &&
            stats.lastRetainedDiagonalPaths==std::vector<uint32_t>(n,2) && stats.lastRetainedPathsPerPair==std::vector<uint32_t>(n*n,2),"Actual full mesh mode/caps/diagonal counts not exposed");
        if(n==6)
        {
            auto old=model->GetChannel(cohort.poses[0],cohort.poses[5],cohort.arrays[0],cohort.arrays[5]);
            Simulator::Schedule(NanoSeconds(100),[]{}); Simulator::Run(); data.Publish(model);
            auto fresh=model->GetChannel(cohort.poses[0],cohort.poses[5],cohort.arrays[0],cohort.arrays[5]);
            Require(fresh!=old && old->m_generatedTime.GetNanoSeconds()==0 && fresh->m_generatedTime.GetNanoSeconds()==100,
                    "New publication mutated/retimestamped an old CIR");
            auto np=py::module_::import("numpy");
            auto wrong=np.attr("zeros")(py::make_tuple(n,1,n+1,1,3,1),"dtype"_a="complex128");
            Reject([&]{model->PublishFullMeshArrays(wrong,data.scalars,data.valid);},"Incorrect TX axis accepted");
            auto invalidScalars=data.scalars; invalidScalars[0]=np.attr("zeros")(py::make_tuple(n,n+1,3));
            Reject([&]{model->PublishFullMeshArrays(data.coefficients,invalidScalars,data.valid);},"Incorrect scalar shape accepted");
            Tensors nonfinite(n); nonfinite.coefficients.attr("__setitem__")(py::make_tuple(0,0,0,0,0,0),py::cast(std::complex<double>(NAN,0)));
            Reject([&]{nonfinite.Publish(model);},"Nonfinite diagonal bypassed whole-result validation");
            Tensors negative(n); negative.scalars[0].attr("__setitem__")(py::make_tuple(1,0,0),-1.);
            Reject([&]{negative.Publish(model);},"Active negative delay accepted");
            Tensors tooMany(n,91); Reject([&]{tooMany.Publish(model);},"Post-solve90 path cap was bypassed");
            Require(model->GetChannel(cohort.poses[0],cohort.poses[5],cohort.arrays[0],cohort.arrays[5])==fresh,
                    "Failed validation partially replaced a good cache result");
            model->ValidateFullMeshCandidates(6,30000,std::vector<uint32_t>(6,1),std::vector<uint32_t>(6,0));
            Reject([&]{model->ValidateFullMeshCandidates(5001,30000,{5000,1,0,0,0,0},std::vector<uint32_t>(6,0));},
                    "One saturated transmitter was hidden by spare global buffer space");
            Reject([&]{model->ValidateFullMeshCandidates(7,30000,std::vector<uint32_t>(6,1),std::vector<uint32_t>(6,0));},"Candidate total/count mismatch accepted");
            Reject([&]{model->ValidateFullMeshCandidates(6,30001,std::vector<uint32_t>(6,1),std::vector<uint32_t>(6,0));},"Candidate allocation mismatch accepted");
            Require(model->GetFullMeshStats().candidateCapacityReachedCalls==1,"Per-TX saturation was not counted");
            auto outside=CreateObject<ConstantPositionMobilityModel>(); auto node=CreateObject<Node>(); node->AggregateObject(outside);
            Reject([&]{model->GetChannel(outside,cohort.poses[0],cohort.arrays[0],cohort.arrays[1]);},"Unknown roster admitted");
        }
        model->Dispose(); Simulator::Destroy();
    }
    Cohort cohort(2); auto disabled=cohort.Model(false);
    Reject([&]{disabled->RefreshFullMesh();},"Disabled batching invoked full mesh"); disabled->Dispose();
    auto overflow=cohort.Model();overflow->SetAttribute("MaxNumPathsPerSource",UintegerValue(3000000000u));
    Reject([&]{overflow->RefreshFullMesh();},"Full mesh uint32 total allocation overflow accepted");overflow->Dispose();Simulator::Destroy();
    auto storage=cohort.Model(false);
    Require(!storage->GetPathStorageStats().enabled && storage->GetPathStorageStats().capacity==0,"Storage optimization changed the default SDK mode");
    storage->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));
    const auto initial=storage->GetPathStorageStats();
    Require(initial.enabled&&initial.capacity==90&&!initial.sourceHashesVerified&&initial.attemptedCalls==0&&
        initial.pathsSourceSha256.empty()&&initial.sionnaVersion.empty(),"Unsolved storage getter claimed source/GPU verification");
    storage->SetAttribute("MaxNumberOfPaths",DoubleValue(90.5));Reject([&]{storage->GetPathStorageStats();},"Fractional fixed storage axis silently rounded");
    storage->SetAttribute("MaxNumberOfPaths",DoubleValue(90));storage->Dispose();
    report["scope"]="CPU production tensor conversion and native cache/candidate/epoch guards; no RT solve or RF acceptance";
}

void StorageGpu(const std::string& scene,py::dict& report)
{
    auto rt=py::module_::import("sionna.rt");
    py::object pathClass=rt.attr("Paths");
    py::object build=pathClass.attr("_build_from_buffer"),empty=pathClass.attr("_build_empty_paths");
    py::list checks;
    for(bool normalize:{false,true})
    {
        Cohort cohort(3);auto model=cohort.Model(true,normalize,scene),reference=cohort.Model(true,normalize,scene);
        model->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));model->RefreshFullMesh();reference->RefreshFullMesh();
        for(size_t a=0;a<3;++a)for(size_t b=a+1;b<3;++b)
        {
            auto actual=model->GetChannel(cohort.poses[a],cohort.poses[b],cohort.arrays[a],cohort.arrays[b]);
            auto expected=reference->GetChannel(cohort.poses[a],cohort.poses[b],cohort.arrays[a],cohort.arrays[b]);
            auto params=model->GetParams(cohort.poses[a],cohort.poses[b]),baseline=reference->GetParams(cohort.poses[a],cohort.poses[b]);
            Require(actual->m_channel.GetNumPages()==1&&std::abs(actual->m_channel(0,0,0)-expected->m_channel(0,0,0))<1e-8&&
                params->m_delay==baseline->m_delay,"Fixed storage changed clear-LoS public CIR/delay normalization");
        }
        const auto stats=model->GetPathStorageStats();
        Require(stats.enabled&&stats.capacity==90&&stats.sourceHashesVerified&&stats.attemptedCalls==1&&stats.completedCalls==1&&
            stats.exportCalls==1&&stats.verifiedTrimCalls==1&&stats.originalAxis==1&&stats.storageAxis==90&&stats.exportedAxis==1&&
            stats.verifiedTrim&&!stats.officialEmpty&&stats.sionnaVersion=="1.2.0"&&stats.drjitVersion=="1.2.0",
            "Actual fixed storage solve/export/trim proof missing");
        Require(pathClass.attr("_build_from_buffer").is(build)&&pathClass.attr("_build_empty_paths").is(empty),"Class hooks leaked after successful solve");
        // Pairwise uses the same wrapper and inspects an angle before cir().
        auto pairwise=cohort.Model(false,normalize,scene);pairwise->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));
        auto matrix=pairwise->GetChannel(cohort.poses[0],cohort.poses[1],cohort.arrays[0],cohort.arrays[1]);
        Require(matrix->m_channel.GetNumPages()==1&&pairwise->GetPathStorageStats().exportCalls==1&&
            pairwise->GetPathStorageStats().verifiedTrimCalls==1,"Pairwise pre-CIR scalar access bypassed the common verified export");
        auto noPaths=cohort.Model(true,normalize,scene);noPaths->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));
        auto flags=noPaths->GetRtPathSolverConfig();flags.los=false;noPaths->SetRtPathSolverConfig(flags);noPaths->RefreshFullMesh();
        const auto zero=noPaths->GetPathStorageStats();
        Require(zero.officialEmpty&&zero.officialEmptyCalls==1&&zero.originalAxis==0&&zero.storageAxis==0&&zero.exportedAxis==0&&
            zero.verifiedTrim&&zero.exportCalls==1&&noPaths->GetChannel(cohort.poses[0],cohort.poses[1],cohort.arrays[0],cohort.arrays[1])->m_channel.GetNumPages()==0,
            "Official empty path branch was hidden/replaced");
        flags.los=true;noPaths->SetRtPathSolverConfig(flags);noPaths->RefreshFullMesh();
        Require(noPaths->GetPathStorageStats().completedCalls==2&&noPaths->GetPathStorageStats().officialEmptyCalls==1&&
            !noPaths->GetPathStorageStats().officialEmpty&&noPaths->GetPathStorageStats().originalAxis==1,
            "Official empty to nonempty transition changed outputs/counters");
        // An actual two-path (LoS plus ground reflection) control must reject a
        // capacity1 before allocation, then restore every temporary class hook.
        auto limited=cohort.Model(true,normalize,scene);limited->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));
        limited->SetAttribute("MaxNumberOfPaths",DoubleValue(1));auto reflected=limited->GetRtPathSolverConfig();
        reflected.maxDepth=1;reflected.specularReflection=true;limited->SetRtPathSolverConfig(reflected);
        Reject([&]{limited->RefreshFullMesh();},"Actual path count above capacity was silently clipped");
        Require(pathClass.attr("_build_from_buffer").is(build)&&pathClass.attr("_build_empty_paths").is(empty),"Class hooks leaked after failed solver construction");
        py::dict lifetime;
        py::exec(R"(
import gc, weakref
def _storage_helpers():
    return {id(value):weakref.ref(value) for value in gc.get_objects()
            if type(value).__name__ == '_PathStorageAdapter'}
)",lifetime);
        const auto before=lifetime["_storage_helpers"]().cast<py::dict>();
        auto disposable=cohort.Model(true,normalize,scene);
        disposable->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));disposable->RefreshFullMesh();
        const auto after=lifetime["_storage_helpers"]().cast<py::dict>();py::list references;
        for(auto entry:after)if(!before.contains(entry.first))references.append(entry.second);
        Require(references.size()==1,"Could not identify the actual enabled model's sole helper instance");
        {
            py::gil_scoped_release released;
            disposable->Dispose(); // Must acquire the GIL and DECREF, never detach a live helper.
        }
        lifetime["gc"].attr("collect")();
        Require(references[0]().is_none(),"Dispose with released GIL detached/leaked the live storage helper");
        Require(disposable->GetPathStorageStats().completedCalls==1&&disposable->GetPathStorageStats().verifiedTrim,
            "Released-GIL cleanup lost the final actual storage statistics");
        Require(pathClass.attr("_build_from_buffer").is(build)&&pathClass.attr("_build_empty_paths").is(empty),
            "Storage class hooks survived released-GIL disposal");
        checks.append(py::dict("normalize_delays"_a=normalize,"original_axis"_a=stats.originalAxis,"storage_axis"_a=stats.storageAxis,
            "exported_axis"_a=stats.exportedAxis,"official_empty"_a=zero.officialEmpty,"paths_source_sha256"_a=stats.pathsSourceSha256));
        model->Dispose();reference->Dispose();pairwise->Dispose();noPaths->Dispose();limited->Dispose();Simulator::Destroy();
    }
    report["fixed_storage_public_checks"]=checks;
    report["fixed_storage_open_edge"]="An initially empty role may still compile its first nonempty tensor family later; dynamic-scene deadline acceptance remains open";
}
void Gpu(const std::string& scene,py::dict& report)
{
    auto rt=py::module_::import("sionna.rt");
    Require(py::str(rt.attr("__version__")).cast<std::string>()=="1.2.0","Pinned Sionna1.2.0 required");
    const auto variant=py::module_::import("mitsuba").attr("variant")().cast<std::string>();
    Require(variant.rfind("cuda",0)==0,"GPU contract must not silently use CPU RT");
    Cohort cohort(6);auto full=cohort.Model(true,false,scene), baseline=cohort.Model(true,false,scene);
    full->RefreshFullMesh();
    py::list rows;
    for(size_t tx=0;tx<6;++tx)for(size_t rx=0;rx<6;++rx)
    {
        if(tx==rx){Require(!full->GetParams(cohort.poses[tx],cohort.poses[rx]),"Self link published");continue;}
        auto matrix=full->GetChannel(cohort.poses[tx],cohort.poses[rx],cohort.arrays[tx],cohort.arrays[rx]);
        auto reference=baseline->GetChannel(cohort.poses[tx],cohort.poses[rx],cohort.arrays[tx],cohort.arrays[rx]);
        auto params=DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(full->GetParams(cohort.poses[tx],cohort.poses[rx]));
        Require(matrix->m_channel.GetNumPages()==1 && params->m_delay.size()==1,"Prepared scene must have one unobstructed LoS path per pair");
        const auto delta=cohort.poses[rx]->GetPosition()-cohort.poses[tx]->GetPosition();
        const auto velocity=cohort.poses[tx]->GetVelocity()-cohort.poses[rx]->GetVelocity();
        const double distance=delta.GetLength(),wavelength=c/carrier;
        const double gain=std::norm(matrix->m_channel(0,0,0)), expected=std::pow(wavelength/(4*M_PI*distance),2);
        const double doppler=(velocity.x*delta.x+velocity.y*delta.y+velocity.z*delta.z)/distance/wavelength;
        Require(std::abs(gain/expected-1)<1e-4 && std::abs(params->m_delay[0]-distance/c)<1e-12 &&
            std::abs(params->m_doppler[0]-doppler)<1e-3,"Full mesh LoS gain/delay/Doppler disagree with analytic solution");
        Require(std::abs(matrix->m_channel(0,0,0)-reference->m_channel(0,0,0))/std::abs(reference->m_channel(0,0,0))<1e-3,
                "Clear LoS full mesh coefficient differs from stock receiver batch");
        const bool reverse=matrix->IsReverse(cohort.arrays[tx]->GetId(),cohort.arrays[rx]->GetId());
        const std::vector<double> expectedAngles{std::atan2(-delta.y,-delta.x),std::acos(-delta.z/distance),
            std::atan2(delta.y,delta.x),std::acos(delta.z/distance)};
        const auto indices=reverse ? std::vector<size_t>{2,3,0,1} : std::vector<size_t>{0,1,2,3};
        for(size_t i=0;i<4;++i)
            Require(params->m_angle[indices[i]].size()==1 &&
                std::abs(std::remainder(params->m_angle[indices[i]][0]-expectedAngles[i],2*M_PI))<1e-4,
                "Full mesh angle direction/radian units mismatch");
        Require(matrix->m_generatedTime.GetNanoSeconds()==0 && params->m_generatedTime==matrix->m_generatedTime,
                "Full mesh matrix/params do not retain the actual solve epoch");
        rows.append(py::dict("tx"_a=tx,"rx"_a=rx,"gain"_a=gain,"expected_gain"_a=expected,
            "delay_s"_a=params->m_delay[0],"doppler_hz"_a=params->m_doppler[0],
            "reverse"_a=matrix->IsReverse(cohort.arrays[tx]->GetId(),cohort.arrays[rx]->GetId())));
    }
    auto stats=full->GetFullMeshStats();
    Require(stats.sourceMode=="full_mesh" && stats.completedCalls==1 && stats.generatedLinks==15 &&
        stats.lastTransmitterCount==6 && stats.lastReceiverCount==6 && stats.lastPerTxCandidateCap==5000 &&
        stats.lastCandidateBufferSize==30000 && stats.lastCandidateCount==30 &&
        stats.lastCandidateCountsPerTx==std::vector<uint32_t>(6,5) && stats.lastSelfCandidateCountsPerTx==std::vector<uint32_t>(6,0),
        "Actual public full mesh per-TX allocation/count/mode not observed");
    Require(full->GetReceiverBatchStats().completedCalls==0,"Full mesh public reads fell back to stock solves");
    auto old=full->GetChannel(cohort.poses[0],cohort.poses[5],cohort.arrays[0],cohort.arrays[5]);
    Simulator::Schedule(MilliSeconds(100),[]{});Simulator::Run();full->RefreshFullMesh();
    auto fresh=full->GetChannel(cohort.poses[0],cohort.poses[5],cohort.arrays[0],cohort.arrays[5]);
    Require(old!=fresh && old->m_generatedTime.GetNanoSeconds()==0 && fresh->m_generatedTime==MilliSeconds(100),
            "Public full mesh refresh did not preserve separate original epochs");
    report["scope"]="GPU public SDK fullmesh analytic clear-LoS/reciprocal/epoch contract; no terrain/accuracy/full-stack qualification";
    report["sampling_note"]="Multi-source RT sampling differs from stock on complex scenes; accuracy/convergence remains open";
    report["variant"]=variant;report["observations"]=rows;
    report["actual_stats"]=py::dict("source_mode"_a=stats.sourceMode,"completed_calls"_a=stats.completedCalls,
        "published_pairs"_a=stats.generatedLinks,"per_tx_candidate_cap"_a=stats.lastPerTxCandidateCap,
        "per_tx_candidates"_a=stats.lastCandidateCountsPerTx,"total_candidate_buffer"_a=stats.lastCandidateBufferSize);
    full->Dispose();baseline->Dispose();Simulator::Destroy();
}
} // namespace
int main(int argc,char** argv)
{
    const bool cpu=argc==3 && std::string(argv[1])=="--cpu";
    const bool gpu=argc==4 && std::string(argv[1])=="--gpu";
    if(!cpu && !gpu) throw std::invalid_argument("--cpu NEW.json | --gpu PREPARED_FLAT.xml NEW.json");
    const std::filesystem::path output=argv[argc-1];
    if(std::filesystem::exists(output)) throw std::runtime_error("Preserve prior result");
    py::scoped_interpreter python;py::dict report;std::string error;
    try {if(cpu)Cpu(report);else {Gpu(std::filesystem::absolute(argv[2]).string(),report);StorageGpu(std::filesystem::absolute(argv[2]).string(),report);}}
    catch(const std::exception& failure){error=failure.what();Simulator::Destroy();}
    report["schema"]=1;report["passed"]=error.empty();report["checks"]=checks;report["error"]=error;
    const auto encoded=py::module_::import("json").attr("dumps")(report,"indent"_a=2,"allow_nan"_a=false).cast<std::string>();
    std::ofstream(output)<<encoded<<'\n';std::cout<<encoded<<'\n';return error.empty()?0:1;
}
