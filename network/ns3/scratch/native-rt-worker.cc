// Technical probe: public SDK RT producer. No packet path or real-time scheduler.
// Result transport is Python stdlib multiprocessing.connection on owned AF_UNIX.
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-channel-model.h"
#include "ns3/three-gpp-antenna-model.h"
#include "native-live-state.h"
#include <cmath>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>

using namespace ns3;
namespace py = pybind11;
using namespace pybind11::literals;

namespace {
// Worker-only access to the stock phased-array invalidation mechanism. Source
// jobs can refresh an ordinary pair before expiry, even at the same native0.
class SourceArray : public UniformPlanarArray
{
  public:
    void ForceFresh() const { InvalidateChannels(); }
};
using SourceSignature=std::pair<double,std::string>;
struct SourceContext
{
    Ptr<SourceArray> array;
    Ptr<SionnaRtChannelModel> pairwise,emitter;
    std::vector<std::string> profileIds;
};
std::vector<double> Coordinates(const Vector& value) {return {value.x,value.y,value.z};}
py::dict EmitterStats(const SionnaRtChannelModel::EmitterBatchStats& stats)
{
    return py::dict("source_mode"_a=stats.sourceMode,"pattern"_a=stats.pattern,
        "bound"_a=stats.bound,"has_published_batch"_a=stats.hasPublishedBatch,
        "frequency_hz"_a=stats.frequencyHz,"source_node_id"_a=stats.sourceNodeId,
        "source_antenna_id"_a=stats.sourceAntennaId,"receiver_node_ids"_a=stats.receiverNodeIds,
        "receiver_antenna_ids"_a=stats.receiverAntennaIds,"registered_receivers"_a=stats.registeredReceivers,
        "configured_samples_per_source"_a=stats.configuredSamplesPerSource,
        "configured_max_paths_per_source"_a=stats.configuredMaxPathsPerSource,
        "configured_max_number_of_paths"_a=stats.configuredMaxNumberOfPaths,
        "candidate_allocation_valid"_a=stats.candidateAllocationValid,
        "configured_candidate_cap"_a=stats.configuredCandidateCap,
        "attempted_calls"_a=stats.attemptedCalls,"completed_calls"_a=stats.completedCalls,
        "generated_links"_a=stats.generatedLinks,"candidate_capacity_reached_calls"_a=stats.candidateCapacityReachedCalls,
        "cache_read_calls"_a=stats.cacheReadCalls,"cache_read_hits"_a=stats.cacheReadHits,"cache_read_misses"_a=stats.cacheReadMisses,
        "last_transmitter_count"_a=stats.lastTransmitterCount,"last_receiver_count"_a=stats.lastReceiverCount,
        "last_allocated_candidate_cap"_a=stats.lastAllocatedCandidateCap,
        "last_candidate_count"_a=stats.lastCandidateCount,"last_candidate_buffer_size"_a=stats.lastCandidateBufferSize,
        "last_retained_paths_per_receiver"_a=stats.lastRetainedPathsPerReceiver,"last_generated_ns"_a=stats.lastGeneratedNs,
        "last_source_position_m"_a=Coordinates(stats.lastSourcePosition),"last_source_velocity_m_s"_a=Coordinates(stats.lastSourceVelocity),
        "last_source_orientation_rad"_a=Coordinates(stats.lastSourceOrientation),
        "scene_prepare_ns"_a=stats.lastScenePrepareWallNs,"solver_ns"_a=stats.lastSolverWallNs,
        "extraction_ns"_a=stats.lastExtractionWallNs,"cache_publish_ns"_a=stats.lastCachePublishWallNs,
        "batch_ns"_a=stats.lastBatchWallNs,"max_batch_ns"_a=stats.maxBatchWallNs);
}

void Require(bool condition, const std::string& reason)
{
    if (!condition) throw std::runtime_error(reason);
}

py::dict PathStorageStats(const SionnaRtChannelModel::PathStorageStats& stats)
{
    return py::dict("enabled"_a=stats.enabled,"capacity"_a=stats.capacity,"source_hashes_verified"_a=stats.sourceHashesVerified,
        "sionna_version"_a=stats.sionnaVersion,"drjit_version"_a=stats.drjitVersion,
        "paths_source_sha256"_a=stats.pathsSourceSha256,"path_solver_source_sha256"_a=stats.pathSolverSourceSha256,
        "attempted_calls"_a=stats.attemptedCalls,"completed_calls"_a=stats.completedCalls,"export_calls"_a=stats.exportCalls,
        "verified_trim_calls"_a=stats.verifiedTrimCalls,"official_empty_calls"_a=stats.officialEmptyCalls,
        "original_axis"_a=stats.originalAxis,"storage_axis"_a=stats.storageAxis,"exported_axis"_a=stats.exportedAxis,
        "verified_trim"_a=stats.verifiedTrim,"official_empty"_a=stats.officialEmpty);
}

py::dict BatchStats(const SionnaRtChannelModel::ReceiverBatchStats& stats)
{
    return py::dict("attempted_calls"_a=stats.attemptedCalls, "completed_calls"_a=stats.completedCalls,
        "generated_links"_a=stats.generatedLinks, "last_receiver_count"_a=stats.lastReceiverCount,
        "base_candidate_cap"_a=stats.configuredMaxPathsPerSource,
        "allocated_candidate_cap"_a=stats.lastAllocatedMaxPathsPerSource,
        "candidate_count"_a=stats.lastCandidateCount, "candidate_buffer_size"_a=stats.lastCandidateBufferSize,
        "candidate_capacity_reached_calls"_a=stats.candidateCapacityReachedCalls,
        "scene_prepare_ns"_a=stats.lastScenePrepareWallNs, "solver_ns"_a=stats.lastSolverWallNs,
        "extraction_ns"_a=stats.lastExtractionWallNs, "cache_publish_ns"_a=stats.lastCachePublishWallNs,
        "batch_ns"_a=stats.lastBatchWallNs);
}

py::dict FullMeshStats(const SionnaRtChannelModel::FullMeshStats& stats)
{
    return py::dict("source_mode"_a=stats.sourceMode,"registered_endpoints"_a=stats.registeredEndpoints,
        "configured_samples_per_source"_a=stats.configuredSamplesPerSource,
        "configured_max_paths_per_source"_a=stats.configuredMaxPathsPerSource,
        "configured_max_number_of_paths"_a=stats.configuredMaxNumberOfPaths,
        "attempted_calls"_a=stats.attemptedCalls,"completed_calls"_a=stats.completedCalls,
        "generated_links"_a=stats.generatedLinks,"candidate_capacity_reached_calls"_a=stats.candidateCapacityReachedCalls,
        "last_transmitter_count"_a=stats.lastTransmitterCount,"last_receiver_count"_a=stats.lastReceiverCount,
        "last_per_tx_candidate_cap"_a=stats.lastPerTxCandidateCap,"last_candidate_count"_a=stats.lastCandidateCount,
        "last_candidate_buffer_size"_a=stats.lastCandidateBufferSize,
        "last_candidate_counts_per_tx"_a=stats.lastCandidateCountsPerTx,
        "last_self_candidate_counts_per_tx"_a=stats.lastSelfCandidateCountsPerTx,
        "last_retained_paths_per_pair"_a=stats.lastRetainedPathsPerPair,
        "last_retained_diagonal_paths"_a=stats.lastRetainedDiagonalPaths,
        "last_generated_ns"_a=stats.lastGeneratedNs,"scene_prepare_ns"_a=stats.lastScenePrepareWallNs,
        "solver_ns"_a=stats.lastSolverWallNs,"extraction_ns"_a=stats.lastExtractionWallNs,
        "cache_publish_ns"_a=stats.lastCachePublishWallNs,"batch_ns"_a=stats.lastBatchWallNs,
        "max_batch_ns"_a=stats.maxBatchWallNs);
}
}

int main(int argc, char** argv)
{
    std::string socketPath, configPath;
    int workerId = -1;
    for (int i = 1; i < argc; i += 2)
    {
        if (i + 1 == argc) return 2;
        const std::string key = argv[i];
        if (key == "--socket") socketPath = argv[i + 1];
        else if (key == "--config") configPath = argv[i + 1];
        else if (key == "--worker-id") workerId = std::stoi(argv[i + 1]);
        else return 2;
    }
    if (socketPath.empty() || configPath.empty() || workerId < 0)
    {
        std::cerr << "Usage: native-rt-worker --socket PATH --config PATH --worker-id INT\n";
        return 2;
    }
    py::scoped_interpreter python;
    py::object connection = py::none();
    py::object workerGc = py::none();
    Ptr<SionnaRtChannelModel> model;
    Ptr<SionnaRtChannelModel> lastActiveModel;
    std::map<SourceSignature,SourceContext> sourceContexts;
    int64_t requestId = 0;
    try
    {
        auto pathlib = py::module_::import("pathlib");
        auto json = py::module_::import("json");
        auto configObject = json.attr("loads")(pathlib.attr("Path")(configPath).attr("read_text")("encoding"_a="utf-8"));
        const auto root = configObject["root"].cast<std::string>();
        py::module_::import("sys").attr("path").attr("insert")(0, root);
        auto contract = py::module_::import("scripts.product.native_rt_workers");
        py::dict config = contract.attr("validate_config")(configObject).cast<py::dict>();
        const auto radio = config["radio"].cast<py::dict>();
        const auto solver = radio["solver"].cast<py::dict>();
        Require(config["fixed_path_storage"].cast<bool>(),"Worker requires explicit resolved fixed_path_storage=True");
        auto mi = py::module_::import("mitsuba");
        mi.attr("set_variant")("cuda_ad_mono_polarized");
        py::module_::import("sionna.rt");
        auto versionsApi = py::module_::import("importlib.metadata");
        auto versions = py::dict("sionna_rt"_a=versionsApi.attr("version")("sionna-rt"),
            "mitsuba"_a=versionsApi.attr("version")("mitsuba"), "drjit"_a=versionsApi.attr("version")("drjit"));
        Require(versions.attr("__eq__")(contract.attr("VERSIONS")).cast<bool>(),
            "Pinned Sionna/Mitsuba/DrJit versions required");
        const auto variant = mi.attr("variant")().cast<std::string>();
        Require(variant.rfind("cuda", 0) == 0, "CUDA variant required; no CPU fallback");
        Simulator::SetImplementation(CreateObject<DefaultSimulatorImpl>());
        std::vector<Ptr<bas::MeasuredMobility>> measured;
        std::vector<Ptr<const MobilityModel>> mobility;
        std::vector<Ptr<const PhasedArrayModel>> antennas;
        const auto cohort = config["cohort"].cast<py::list>();
        const auto baseCount = cohort.size();
        std::map<uint32_t, size_t> semanticIds;
        for (size_t i = 0; i < cohort.size(); ++i)
        {
            auto node = CreateObject<Node>();
            auto pose = CreateObject<bas::MeasuredMobility>();
            node->AggregateObject(pose);
            measured.push_back(pose); mobility.push_back(pose);
            semanticIds.emplace(node->GetId(), i);
            auto array = CreateObject<UniformPlanarArray>();
            array->SetAttribute("NumRows", UintegerValue(1));
            array->SetAttribute("NumColumns", UintegerValue(1));
            array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1); weights[0] = {1., 0.};
            array->SetBeamformingVector(weights); antennas.push_back(array);
        }
        SionnaRtChannelModel::RtPathSolverConfig settings;
        settings.maxDepth = solver["max_depth"].cast<int>();
        settings.seed = solver["seed"].cast<int>();
        settings.los = solver["los"].cast<bool>();
        settings.specularReflection = solver["specular_reflection"].cast<bool>();
        settings.diffuseReflection = solver["diffuse_reflection"].cast<bool>();
        settings.refraction = solver["refraction"].cast<bool>();
        settings.diffraction = solver["diffraction"].cast<bool>();
        settings.edgeDiffraction = solver["edge_diffraction"].cast<bool>();
        settings.syntheticArray = solver["synthetic_array"].cast<bool>();
        const auto makeModel = [&](double frequency) {
            auto value=CreateObject<SionnaRtChannelModel>();
            value->SetReceiverBatchEnabled(false);
            value->SetAttribute("Frequency", DoubleValue(frequency));
            value->SetAttribute("Scenario", StringValue(config["scene"]["path"].cast<std::string>()));
            value->SetAttribute("IsMergeShapeEnabled", BooleanValue(radio["merge_shapes"].cast<bool>()));
            value->SetAttribute("IsImageRenderedEnabled", BooleanValue(false));
            value->SetAttribute("NormalizeDelays", BooleanValue(config["normalize_delays"].cast<bool>()));
            value->SetAttribute("SamplesPerSource", UintegerValue(radio["samples_per_source"].cast<uint32_t>()));
            value->SetAttribute("MaxNumPathsPerSource", UintegerValue(radio["max_paths_per_source"].cast<uint32_t>()));
            value->SetAttribute("MaxNumberOfPaths", DoubleValue(radio["max_number_of_paths"].cast<double>()));
            // Producer time follows the job; the consumer owns serving freshness.
            value->SetAttribute("UpdatePeriod", TimeValue(NanoSeconds(1)));
            value->SetUpdateDistanceThreshold(0.); value->SetUpdateJitterFraction(0.);
            value->SetRtPathSolverConfig(settings);
            value->SetAttribute("FixedPathStorageEnabled",BooleanValue(true));
            return value;
        };
        model=makeModel(radio["frequency_hz"].cast<double>());
        lastActiveModel=model;
        model->SetReceiverBatchEndpoints(mobility, antennas);
        model->SetReceiverBatchEnabled(true);
        std::map<std::string,SourceSignature> sourceProfiles;
        if(config.contains("source_profiles"))
        {
            // The one measured emitter retains semantic indexbaseCount. RF
            // aliases share fixed arrays/models, never mutable pattern changes.
            auto node=CreateObject<Node>(); auto pose=CreateObject<bas::MeasuredMobility>();
            node->AggregateObject(pose); measured.push_back(pose); mobility.push_back(pose);
            semanticIds.emplace(node->GetId(),baseCount);
            for(auto item:config["source_profiles"].cast<py::list>())
            {
                const auto profile=py::reinterpret_borrow<py::dict>(item);
                const auto id=profile["id"].cast<std::string>();
                const SourceSignature signature{profile["frequency_hz"].cast<double>(),profile["pattern"].cast<std::string>()};
                auto found=sourceContexts.find(signature);
                if(found==sourceContexts.end())
                {
                    auto array=CreateObject<SourceArray>(); array->SetNumRows(1); array->SetNumColumns(1);
                    if(signature.second=="tr38901")
                    {
                        auto element=CreateObject<ThreeGppAntennaModel>();
                        element->SetRadiationPattern(ThreeGppAntennaModel::RadiationPattern::OUTDOOR);
                        array->SetAntennaElement(element);
                    }
                    else array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
                    PhasedArrayModel::ComplexVector weights(1); weights[0]={1.,0.}; array->SetBeamformingVector(weights);
                    auto pairwise=makeModel(signature.first),emitter=makeModel(signature.first);
                    emitter->SetReceiverBatchEndpoints(std::vector<Ptr<const MobilityModel>>(mobility.begin(),mobility.begin()+baseCount),antennas);
                    emitter->SetReceiverBatchEnabled(true);
                    emitter->SetEmitterBatchEndpoint(mobility[baseCount],array);
                    found=sourceContexts.emplace(signature,SourceContext{array,pairwise,emitter,{}}).first;
                }
                found->second.profileIds.push_back(id);
                sourceProfiles.emplace(id,signature);
            }
        }
        std::map<std::pair<std::string,size_t>,Ptr<const MatrixBasedChannelModel::ChannelMatrix>> previousSourceMatrices;
        connection = py::module_::import("multiprocessing.connection").attr("Client")(
            socketPath, "family"_a="AF_UNIX", "authkey"_a=py::none());
        auto time = py::module_::import("time");
        const auto hostNow = [&]() { return time.attr("monotonic_ns")().cast<int64_t>(); };
        auto ready=py::dict("schema"_a=1, "kind"_a="ready", "worker_id"_a=workerId,
            "pid"_a=py::module_::import("os").attr("getpid")(), "versions"_a=versions,
            "mitsuba_variant"_a=variant, "gpu_solve_verified"_a=false,
            "applied_config"_a=config, "worker_cache_update_period_ns"_a=1, "render_images"_a=false,
            "scope"_a="public_native_sdk_rt_producer_probe");
        ready["path_storage"]=PathStorageStats(model->GetPathStorageStats());
        if(!sourceProfiles.empty())
        {
            std::set<double> actualFrequencies;
            py::list contexts;
            for(const auto& [signature,context]:sourceContexts)
            {
                DoubleValue actual; context.emitter->GetAttribute("Frequency",actual);
                Require(actual.Get()==signature.first,"Source model carrier changed at readiness");
                const auto stats=context.emitter->GetEmitterBatchStats();
                Require(stats.bound && stats.pattern==signature.second && stats.frequencyHz==signature.first &&
                    stats.registeredReceivers==baseCount,"Source emitter context binding differs from frozen RF signature");
                actualFrequencies.insert(actual.Get());
                contexts.append(py::dict("frequency_hz"_a=signature.first,"pattern"_a=signature.second,
                    "profile_ids"_a=context.profileIds,"source_node_id"_a=stats.sourceNodeId,
                    "source_antenna_id"_a=stats.sourceAntennaId,
                    "path_storage"_a=PathStorageStats(context.emitter->GetPathStorageStats()),
                    "source_modes"_a=std::vector<std::string>{"pairwise","emitter_batch"}));
            }
            ready["source_model_frequencies_hz"]=std::vector<double>(actualFrequencies.begin(),actualFrequencies.end());
            ready["source_profile_count"]=sourceProfiles.size();
            ready["source_profile_contexts"]=contexts;
        }
        workerGc=contract.attr("_GcObserver")();
        workerGc.attr("start")();
        connection.attr("send")(ready);
        int64_t lastRequestId = 0;
        while (true)
        {
            auto message = connection.attr("recv")();
            if (py::isinstance<py::dict>(message) && message.cast<py::dict>().contains("kind") &&
                message["kind"].cast<std::string>() == "stop") break;
            contract.attr("validate_job")(message, baseCount, config);
            py::dict job = message.cast<py::dict>();
            requestId = job["request_id"].cast<int64_t>();
            Require(requestId > lastRequestId, "request_id must be strictly increasing for this worker");
            lastRequestId = requestId;
            const auto sampleNs = job["sample_native_ns"].cast<int64_t>();
            Require(sampleNs >= Simulator::Now().GetNanoSeconds(), "Worker sample time cannot move backwards");
            const auto started = hostNow();
            const auto cpuStarted=time.attr("thread_time_ns")().cast<int64_t>();
            const auto gcWallBefore=workerGc.attr("total_wall_ns").cast<int64_t>();
            const auto gcCountBefore=workerGc.attr("completed").cast<uint64_t>();
            Require(job["sample_host_monotonic_ns"].cast<int64_t>() <= started,
                "Sample host timestamp is in the future on this AF_UNIX host");
            py::dict result;
            Simulator::Schedule(NanoSeconds(sampleNs - Simulator::Now().GetNanoSeconds()), [&]() {
                const auto poses = job["poses"].cast<py::list>();
                for (size_t i = 0; i < poses.size(); ++i)
                {
                    auto pose = poses[i];
                    bas::MeasuredState state;
                    state.position = bas::ReadVector(pose, "position_m");
                    state.velocity = bas::ReadVector(pose, "velocity_m_s");
                    state.orientation = bas::ReadVector(pose, "orientation_rad");
                    state.sampleNs = pose["sample_host_ns"].cast<int64_t>();
                    measured[i]->Apply(state);
                }
                const auto anchor = job["anchor_index"].cast<size_t>();
                const bool sourceJob=job.contains("profile_id");
                const bool emitterJob=sourceJob && job.contains("source_mode"); // Explicit validated SDK12 opt-in.
                const bool fullMeshJob=!sourceJob && job.contains("channel_mode"); // Validated against the frozen config.
                const auto profileId=sourceJob ? job["profile_id"].cast<std::string>() : std::string();
                auto* sourceContext=sourceJob ? &sourceContexts.at(sourceProfiles.at(profileId)) : nullptr;
                auto activeModel=sourceJob ? (emitterJob ? sourceContext->emitter : sourceContext->pairwise) : model;
                lastActiveModel=activeModel;
                Ptr<const PhasedArrayModel> anchorArray;
                if(sourceJob) anchorArray=sourceContext->array;
                else anchorArray=antennas[anchor];
                py::list pairs;
                const auto callsBefore = model->GetReceiverBatchStats().completedCalls;
                const auto fullCallsBefore = model->GetFullMeshStats().completedCalls;
                const auto emitterCallsBefore=emitterJob ? activeModel->GetEmitterBatchStats().completedCalls : 0;
                const auto sourceBatchBefore=emitterJob ? activeModel->GetReceiverBatchStats().completedCalls : 0;
                // Our minimal SDK extension forces prefetch even at the same
                // simulation timestamp or before ordinary cache expiry.
                if(emitterJob) activeModel->RefreshEmitterBatch();
                else if(sourceJob) sourceContext->array->ForceFresh();
                else if(fullMeshJob) model->RefreshFullMesh();
                else model->RefreshReceiverBatch(mobility[anchor], antennas[anchor]);
                std::vector<std::pair<size_t,size_t>> requested;
                if(emitterJob)for(size_t peer=0;peer<baseCount;++peer)requested.emplace_back(anchor,peer);
                else if(sourceJob) requested.emplace_back(anchor,job["peer_index"].cast<size_t>());
                else if(fullMeshJob)
                    for(size_t tx=0;tx<baseCount;++tx)
                        for(size_t rx=tx+1;rx<baseCount;++rx) requested.emplace_back(tx,rx);
                else for(size_t peer=0;peer<baseCount;++peer) if(peer!=anchor) requested.emplace_back(anchor,peer);
                for (const auto& [txIndex,peer] : requested)
                {
                    const auto txArray=fullMeshJob ? antennas[txIndex] : anchorArray;
                    auto matrix = activeModel->GetChannel(mobility[txIndex], mobility[peer], txArray, antennas[peer]);
                    auto params = DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(
                        activeModel->GetParams(mobility[txIndex], mobility[peer]));
                    Require(matrix && params, "Public SDK returned missing channel/parameters");
                    Require(matrix->m_generatedTime.GetNanoSeconds() == sampleNs,
                        "Refresh did not publish the sampled channel epoch");
                    if(sourceJob)
                    {
                        const auto key=std::make_pair(profileId,peer);
                        Require(!previousSourceMatrices.count(key) || previousSourceMatrices.at(key)!=matrix,
                            "Source refresh returned a previous request channel matrix");
                        previousSourceMatrices[key]=matrix;
                    }
                    const auto rows = matrix->m_channel.GetNumRows(), cols = matrix->m_channel.GetNumCols();
                    const auto paths = matrix->m_channel.GetNumPages();
                    Require(rows == 1 && cols == 1 && params->m_delay.size() == paths &&
                        params->m_doppler.size() == paths && params->m_angle.size() == 4,
                        "Native iso1x1 tensor/parameter dimensions disagree");
                    std::vector<double> real, imag;
                    for (size_t rx = 0; rx < rows; ++rx)
                        for (size_t tx = 0; tx < cols; ++tx)
                            for (size_t path = 0; path < paths; ++path)
                            {
                                const auto value = matrix->m_channel(rx, tx, path);
                                Require(std::isfinite(value.real()) && std::isfinite(value.imag()), "Nonfinite CIR");
                                real.push_back(value.real()); imag.push_back(value.imag());
                            }
                    for (const auto& angles : params->m_angle)
                    {
                        Require(angles.size() == paths, "Angle/path dimensions disagree");
                        for (double value : angles) Require(std::isfinite(value), "Nonfinite angle");
                    }
                    for (double value : params->m_delay) Require(std::isfinite(value), "Nonfinite delay");
                    for (double value : params->m_doppler) Require(std::isfinite(value), "Nonfinite Doppler");
                    const auto source = semanticIds.at(matrix->m_nodeIds.first);
                    const auto target = semanticIds.at(matrix->m_nodeIds.second);
                    Require(params->m_nodeIds == matrix->m_nodeIds &&
                        params->m_generatedTime == matrix->m_generatedTime, "Native params/matrix metadata disagree");
                    auto pair = py::dict("direction"_a=py::cast(std::vector<size_t>{source, target}),
                        "coefficient_shape"_a=py::cast(std::vector<size_t>{rows, cols, paths}),
                        "coefficient_order"_a="rx_tx_path", "coefficients_real"_a=py::cast(real),
                        "coefficients_imag"_a=py::cast(imag), "delays_s"_a=py::cast(params->m_delay),
                        "doppler_hz"_a=py::cast(params->m_doppler), "angles_rad"_a=py::cast(params->m_angle),
                        "generated_ns"_a=matrix->m_generatedTime.GetNanoSeconds(),
                        "source_request_id"_a=requestId, "sample_native_ns"_a=sampleNs,
                        "sample_host_monotonic_ns"_a=job["sample_host_monotonic_ns"], "poses"_a=poses);
                    pair["pair"] = py::cast(std::vector<size_t>{txIndex, peer});
                    pair["fresh_for_request"] = true;
                    pairs.append(pair);
                }
                if(emitterJob)
                {
                    const auto stats=activeModel->GetEmitterBatchStats();
                    const auto expectedCap=static_cast<uint64_t>(radio["max_paths_per_source"].cast<uint32_t>())*baseCount;
                    Require(stats.completedCalls-emitterCallsBefore==1 &&
                        activeModel->GetReceiverBatchStats().completedCalls-sourceBatchBefore==1 &&
                        stats.lastGeneratedNs==sampleNs && stats.bound && stats.hasPublishedBatch &&
                        stats.lastTransmitterCount==1 && stats.lastReceiverCount==baseCount &&
                        stats.lastAllocatedCandidateCap==expectedCap && stats.lastCandidateBufferSize==expectedCap &&
                        stats.lastCandidateCount<stats.lastCandidateBufferSize && stats.candidateCapacityReachedCalls==0 &&
                        stats.lastRetainedPathsPerReceiver.size()==baseCount,
                        "Emitter job must perform one fresh six/registered-RX solve without fallback/clipping");
                }
                else if(fullMeshJob)
                    Require(model->GetFullMeshStats().completedCalls-fullCallsBefore==1 &&
                            model->GetFullMeshStats().lastGeneratedNs==sampleNs &&
                            model->GetReceiverBatchStats().completedCalls==callsBefore,
                            "Full-mesh job must perform one fresh solve without anchor fallback");
                else if(!sourceJob) Require(model->GetReceiverBatchStats().completedCalls - callsBefore == 1,
                        "Solve job must perform exactly one explicit fresh batch");
                result = py::dict("schema"_a=1, "kind"_a="result", "worker_id"_a=workerId,
                    "request_id"_a=requestId, "pairs"_a=pairs);
                result["path_storage"]=PathStorageStats(activeModel->GetPathStorageStats());
                if(sourceJob)
                {
                    DoubleValue actualFrequency; activeModel->GetAttribute("Frequency",actualFrequency);
                    Require(actualFrequency.Get()==sourceProfiles.at(profileId).first,"Source carrier was changed");
                    result["profile_id"]=profileId; result["channel_mode"]="source_star";
                    result["frequency_hz"]=actualFrequency.Get();
                    if(emitterJob)
                    {
                        const auto stats=activeModel->GetEmitterBatchStats();
                        result["source_mode"]="emitter_batch";
                        result["new_emitter_batch_calls"]=stats.completedCalls-emitterCallsBefore;
                        result["emitter_batch_stats"]=EmitterStats(stats);
                        result["allocated_candidate_cap"]=stats.lastAllocatedCandidateCap;
                        result["candidate_count"]=stats.lastCandidateCount;
                        result["candidate_buffer_size"]=stats.lastCandidateBufferSize;
                        result["candidate_count_scope"]="actual_public_emitter_batch_api";
                    }
                    else
                    {
                        result["new_pairwise_calls"]=1;
                        result["allocated_candidate_cap"]=radio["max_paths_per_source"];
                        result["candidate_count"]=py::none();
                        result["candidate_count_scope"]="not_exposed_by_public_pairwise_api";
                    }
                }
                else if(fullMeshJob)
                {
                    result["channel_mode"]="full_mesh";
                    result["new_full_mesh_calls"]=model->GetFullMeshStats().completedCalls-fullCallsBefore;
                    result["full_mesh_stats"]=FullMeshStats(model->GetFullMeshStats());
                }
                else
                {
                    result["new_batch_calls"]=model->GetReceiverBatchStats().completedCalls - callsBefore;
                    result["batch_stats"]=BatchStats(model->GetReceiverBatchStats());
                }
            });
            Simulator::Run(); // Default scheduler advances directly to sampled time; never wall-clock pacing.
            const auto finished = hostNow();
            result["producer_thread_cpu_ns"]=time.attr("thread_time_ns")().cast<int64_t>()-cpuStarted;
            result["producer_gc_wall_ns"]=workerGc.attr("total_wall_ns").cast<int64_t>()-gcWallBefore;
            result["producer_gc_collections"]=workerGc.attr("completed").cast<uint64_t>()-gcCountBefore;
            result["worker_started_host_monotonic_ns"] = started;
            result["worker_completed_host_monotonic_ns"] = finished;
            result["sample_to_completion_ns"] = finished - job["sample_host_monotonic_ns"].cast<int64_t>();
            result["producer_wall_ns"] = finished - started;
            connection.attr("send")(result);
            requestId = 0;
        }
        workerGc.attr("close")();
        model->Dispose();
        for(auto& [signature,context]:sourceContexts) {context.pairwise->Dispose();context.emitter->Dispose();}
        Simulator::Destroy(); connection.attr("close")();
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "Native RT worker failed: " << error.what() << '\n';
        try
        {
            if (!connection.is_none())
            {
                auto failure=py::dict("schema"_a=1,"kind"_a="error","worker_id"_a=workerId,"request_id"_a=requestId,"error"_a=error.what());
                if(lastActiveModel)failure["path_storage"]=PathStorageStats(lastActiveModel->GetPathStorageStats());
                connection.attr("send")(failure);
            }
        }
        catch (const std::exception&) {}
        if (model) model->Dispose();
        for(auto& [signature,context]:sourceContexts) {context.pairwise->Dispose();context.emitter->Dispose();}
        Simulator::Destroy();
        return 1;
    }
}
