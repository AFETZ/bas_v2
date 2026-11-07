// Main-thread bridge for native RT producers. Packet callbacks use NativeRtCache
// only; Python, process I/O and RT calculation never enter that callback path.
#pragma once
#include "native-live-state.h"
#include "native-rt-cache.h"
#include "ns3/event-id.h"
#include <cstdint>
#include <filesystem>
#include <functional>
#include <fstream>
#include <optional>
#include <set>
#include <thread>

namespace bas
{
using namespace pybind11::literals;
class NativeRtPool
{
  public:
    using Snapshot = std::function<std::vector<MeasuredState>()>;
    using EventSink = std::function<void(const std::string&, const std::string&, double, const std::string&)>;

    NativeRtPool(Ptr<NativeRtCache> cache, std::vector<NativeRtCache::Endpoint> endpoints,
                 Snapshot snapshot, EventSink event)
        : m_cache(std::move(cache)), m_endpoints(std::move(endpoints)),
          m_snapshot(std::move(snapshot)), m_event(std::move(event))
    {
        Require(m_cache && m_snapshot && m_endpoints.size() >= 2 && m_endpoints.size() <= 6,
                "A fixed cache/cohort and measured-state callback are required");
    }

    ~NativeRtPool()
    {
        // The owner calls Close explicitly to observe cleanup failures. This is
        // only an exception-path safety net, before scoped_interpreter dies.
        try { if (Py_IsInitialized()) Close(); } catch (...) {}
    }

    void Start(const std::string& root, const std::string& binary,
               const std::string& configPath, const std::string& artifacts, uint32_t workers)
    {
        py::gil_scoped_acquire gil;
        Require(!m_started && !m_closed && workers >= 1 && workers <= 3, "Invalid/repeated pool start");
        py::module_::import("sys").attr("path").attr("insert")(0, root);
        const auto module = py::module_::import("scripts.product.native_rt_workers");
        m_json = py::module_::import("json");
        const auto path = py::module_::import("pathlib").attr("Path");
        const auto input = m_json.attr("loads")(path(configPath).attr("read_text")("encoding"_a="utf-8"));
        const auto config = module.attr("validate_config")(input).cast<py::dict>();
        Require(config["root"].cast<std::string>() == path(root).attr("resolve")().attr("__str__")().cast<std::string>(),
                "Frozen worker root does not match the run root");
        Require(config["cohort"].cast<py::list>().size() == m_endpoints.size(), "Frozen worker cohort size mismatch");
        Require(config["radio"]["frequency_hz"].cast<double>() == m_cache->GetFrequency(), "Worker/cache carrier mismatch");
        Require(config["radio"]["max_number_of_paths"].cast<double>() <= 90, "Cache supports at most 90 native paths");
        std::filesystem::create_directories(artifacts);
        m_logPath = (std::filesystem::path(artifacts) / "results.jsonl").string();
        m_summaryPath = (std::filesystem::path(artifacts) / "summary.json").string();
        Require(!std::filesystem::exists(m_logPath) && !std::filesystem::exists(m_summaryPath),
            "Preserve the previous producer artifacts");
        m_log.open(m_logPath, std::ios::out);
        Require(static_cast<bool>(m_log), "Cannot open producer result log");
        m_pool = module.attr("NativeRtWorkerPool")(py::cast(std::vector<std::string>{binary}),
            configPath, (std::filesystem::path(artifacts) / "processes").string(), "workers"_a=workers);
        m_pool.attr("start")();
        m_inflight.resize(workers);
        m_started = true;
        Emit("rt_pool_started", workers, "one_inflight_per_worker;native_public_sdk;no_packet_path_python");
    }

    // Cold GPU/JIT jobs are observed but never admitted as live channels. All
    // bootstrap records retain their actual sampled host times at native time 0.
    void Warm(std::function<void()> refresh, double timeoutSeconds = 30)
    {
        py::gil_scoped_acquire gil;
        try
        {
        Require(m_started && !m_closed && Simulator::Now() == NanoSeconds(0) && refresh &&
                std::isfinite(timeoutSeconds) && timeoutSeconds > 0, "Invalid pre-Run warmup");
        const auto deadline = NativeRtCache::HostNowNs() + static_cast<int64_t>(timeoutSeconds * 1e9);
        refresh();
        for (size_t worker = 0; worker < m_inflight.size(); ++worker)
            Submit(worker, worker % m_endpoints.size(), true);
        while (Busy())
        {
            Require(NativeRtCache::HostNowNs() <= deadline, "Cold RT warmup timeout");
            refresh(); Collect(); SleepWarm();
        }
        refresh();
        for (;;)
        {
            Require(NativeRtCache::HostNowNs() <= deadline, "Fresh RT bootstrap timeout");
            refresh(); Collect();
            const auto states = m_cache->Priorities();
            const bool valid = std::all_of(states.begin(), states.end(), [](const auto& state) { return state.valid; });
            if (valid && !Busy()) break;
            // Once complete, drain already running work without submitting more.
            if (!valid) Dispatch();
            SleepWarm();
        }
        Emit("rt_pool_warm", m_endpoints.size() * (m_endpoints.size() - 1) / 2,
             "all_reciprocal_pairs_valid;no_inflight;native_epoch_zero;original_pose_epochs");
        }
        catch (const std::exception& error)
        {
            m_failed = true; m_error = error.what();
            Emit("rt_pool_failed", 0, m_error);
            throw;
        }
    }

    // Root schedules the first Poll once. Subsequent callbacks contain this
    // pointer only, never py::object captures destroyed with the GIL released.
    void Poll()
    {
        if (m_closed || m_failed) return;
        const auto started = NativeRtCache::HostNowNs();
        py::gil_scoped_acquire gil;
        try
        {
            Require(m_started, "Pool poll before Start");
            Collect(); Dispatch();
            ++m_pollCalls;
            m_maxPollWallNs = std::max(m_maxPollWallNs, NativeRtCache::HostNowNs() - started);
            m_next = Simulator::Schedule(MilliSeconds(20), &NativeRtPool::Poll, this);
        }
        catch (const std::exception& error)
        {
            m_failed = true; m_error = error.what();
            Emit("rt_pool_failed", 0, m_error);
            throw;
        }
    }

    std::string SnapshotJson()
    {
        py::gil_scoped_acquire gil;
        if (m_closed && !m_finalJson.empty()) return m_finalJson;
        return Encode(SnapshotValue());
    }

    void Close()
    {
        py::gil_scoped_acquire gil;
        if (m_closed) return;
        m_next.Cancel();
        py::dict final;
        if (m_started) final = SnapshotValue(); // Ages measured before cleanup delays.
        try { if (m_pool) m_pool.attr("close")(); }
        catch (const std::exception& error)
        {
            m_failed = true; m_error = std::string("RT pool cleanup: ") + error.what();
            if (m_log) m_log.flush();
            throw;
        }
        m_closed = true;
        if (m_started)
        {
            final["closed"] = true;
            final["cache_snapshot_stage"] = "before_close";
            final["pool"] = m_pool.attr("snapshot")();
            m_finalJson = Encode(final);
            std::ofstream summary(m_summaryPath, std::ios::out);
            summary << m_finalJson << '\n'; summary.close();
            Require(static_cast<bool>(summary), "Cannot save final RT pool summary");
        }
        m_pool = py::object(); m_json = py::object();
        if (m_log) m_log.close();
    }

  private:
    using Pair = std::pair<size_t, size_t>;
    struct Job
    {
        uint64_t id;
        size_t anchor;
        int64_t nativeNs, hostNs;
        std::vector<MeasuredState> poses;
        bool cold;
    };
    Ptr<NativeRtCache> m_cache;
    std::vector<NativeRtCache::Endpoint> m_endpoints;
    Snapshot m_snapshot;
    EventSink m_event;
    py::object m_pool, m_json;
    std::vector<std::optional<Job>> m_inflight;
    EventId m_next;
    std::ofstream m_log;
    std::string m_logPath, m_summaryPath, m_error, m_finalJson;
    bool m_started{false}, m_closed{false}, m_failed{false};
    uint64_t m_request{0}, m_completed{0}, m_cold{0}, m_publishedResults{0}, m_publishedPairs{0}, m_discarded{0};
    uint64_t m_pollCalls{0};
    int64_t m_maxPollWallNs{0};
    std::map<std::string, uint64_t> m_discardReasons;

    static void Require(bool condition, const std::string& message)
    { if (!condition) throw std::runtime_error("native RT pool: " + message); }
    void Emit(const std::string& event, double value, const std::string& details)
    { if (m_event) m_event(event, "ns3", value, details); }
    static Pair Key(size_t a, size_t b) { return {std::min(a,b), std::max(a,b)}; }
    bool Busy() const
    { return std::any_of(m_inflight.begin(), m_inflight.end(), [](const auto& job) { return job.has_value(); }); }
    static void SleepWarm()
    {
        py::gil_scoped_release release;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    std::string Encode(py::handle value)
    {
        if (!m_json) m_json = py::module_::import("json");
        return m_json.attr("dumps")(value, "allow_nan"_a=false).cast<std::string>();
    }

    void Submit(size_t worker, size_t anchor, bool cold)
    {
        Require(!m_inflight.at(worker) && m_request < static_cast<uint64_t>(INT64_MAX), "Invalid/busy submission");
        Job job{++m_request, anchor, Simulator::Now().GetNanoSeconds(), 0, m_snapshot(), cold};
        job.hostNs = NativeRtCache::HostNowNs(); // After the entire measured snapshot was copied.
        Require(job.poses.size() == m_endpoints.size(), "Measured snapshot does not match the complete cohort");
        py::list poses;
        for (const auto& pose : job.poses)
            poses.append(py::dict("position_m"_a=std::vector<double>{pose.position.x,pose.position.y,pose.position.z},
                "velocity_m_s"_a=std::vector<double>{pose.velocity.x,pose.velocity.y,pose.velocity.z},
                "orientation_rad"_a=std::vector<double>{pose.orientation.x,pose.orientation.y,pose.orientation.z},
                "sample_host_ns"_a=pose.sampleNs));
        const auto request = py::dict("kind"_a="solve", "request_id"_a=job.id,
            "sample_native_ns"_a=job.nativeNs, "sample_host_monotonic_ns"_a=job.hostNs,
            "anchor_index"_a=anchor, "poses"_a=poses);
        Require(m_pool.attr("submit")(worker, request).cast<bool>(), "Python pool unexpectedly reported busy");
        m_inflight[worker] = std::move(job);
    }

    // Count only uncovered due links. Different anchors can share one pair;
    // their completion ordering is resolved by the cache's original epochs.
    void Dispatch()
    {
        auto states = m_cache->Priorities();
        std::set<Pair> covered;
        for (const auto& job : m_inflight)
            if (job) for (size_t peer=0;peer<m_endpoints.size();++peer)
                if (peer!=job->anchor) covered.insert(Key(job->anchor,peer));
        for (size_t worker=0;worker<m_inflight.size();++worker)
        {
            if (m_inflight[worker]) continue;
            const auto urgent = std::find_if(states.begin(), states.end(), [&](const auto& state) {
                return state.due && !covered.count(Key(state.first,state.second));
            });
            if (urgent==states.end()) break;
            size_t selected=0, bestCount=0;
            double bestRisk=-1;
            for (size_t anchor : {urgent->first,urgent->second})
            {
                size_t count=0; double risk=0;
                for (const auto& state:states)
                    if (state.due && (state.first==anchor || state.second==anchor) &&
                        !covered.count(Key(state.first,state.second)))
                    { ++count; risk+=state.normalizedRisk; }
                if (count>bestCount || (count==bestCount && count && risk>bestRisk))
                { selected=anchor; bestCount=count; bestRisk=risk; }
            }
            if (!bestCount) break;
            Submit(worker,selected,false);
            for(size_t peer=0;peer<m_endpoints.size();++peer)
                if(peer!=selected) covered.insert(Key(selected,peer));
        }
    }

    static bool SoftDiscard(const std::string& reason)
    {
        return reason=="out_of_order" || reason=="native_sample_expired" || reason=="host_sample_expired" ||
            reason=="endpoint_displaced" || reason=="endpoint_velocity_changed" || reason=="endpoint_orientation_changed";
    }
    NativeRtCache::Record Decode(py::dict value, const Job& job, std::set<size_t>& peers)
    {
        const auto requested=value["pair"].cast<std::vector<size_t>>();
        const auto direction=value["direction"].cast<std::vector<size_t>>();
        Require(requested.size()==2 && requested[0]==job.anchor && requested[1]<m_endpoints.size() &&
            requested[1]!=job.anchor && peers.insert(requested[1]).second && direction.size()==2 &&
            Key(direction[0],direction[1])==Key(requested[0],requested[1]), "Malformed anchor pair/direction");
        Require(value["fresh_for_request"].cast<bool>() && value["source_request_id"].cast<uint64_t>()==job.id &&
            value["sample_native_ns"].cast<int64_t>()==job.nativeNs &&
            value["generated_ns"].cast<int64_t>()==job.nativeNs &&
            value["sample_host_monotonic_ns"].cast<int64_t>()==job.hostNs, "Producer sample provenance mismatch");
        const auto poses=value["poses"].cast<py::list>();
        Require(poses.size()==job.poses.size(), "Producer pose roster mismatch");
        for(size_t i=0;i<job.poses.size();++i)
            Require(ReadVector(poses[i],"position_m")==job.poses[i].position &&
                ReadVector(poses[i],"velocity_m_s")==job.poses[i].velocity &&
                ReadVector(poses[i],"orientation_rad")==job.poses[i].orientation &&
                poses[i]["sample_host_ns"].cast<int64_t>()==job.poses[i].sampleNs,
                "Producer retimestamped or changed the sampled pose");
        const auto shape=value["coefficient_shape"].cast<std::vector<size_t>>();
        Require(shape.size()==3 && shape[0]==1 && shape[1]==1 && shape[2]<=90 &&
            value["coefficient_order"].cast<std::string>()=="rx_tx_path", "Malformed native CIR shape/order");
        const auto real=value["coefficients_real"].cast<std::vector<double>>();
        const auto imag=value["coefficients_imag"].cast<std::vector<double>>();
        Require(real.size()==shape[2] && imag.size()==shape[2], "Malformed CIR coefficient dimensions");
        NativeRtCache::Record record;
        record.direction={direction[0],direction[1]}; record.sourceRequestId=job.id;
        record.sampleNativeNs=job.nativeNs; record.sampleHostMonotonicNs=job.hostNs;
        for(size_t side=0;side<2;++side)
        {
            const auto& pose=job.poses.at(direction[side]);
            record.poses[side]={pose.position,pose.velocity,pose.orientation,pose.sampleNs};
        }
        auto matrix=Create<MatrixBasedChannelModel::ChannelMatrix>();
        matrix->m_generatedTime=NanoSeconds(job.nativeNs);
        matrix->m_channel=MatrixBasedChannelModel::Complex3DVector(1,1,shape[2]);
        for(size_t path=0;path<shape[2];++path) matrix->m_channel(0,0,path)={real[path],imag[path]};
        auto parameters=Create<SionnaRtChannelModel::SionnaRtChannelParams>();
        parameters->m_generatedTime=matrix->m_generatedTime;
        parameters->m_delay=value["delays_s"].cast<std::vector<double>>();
        parameters->m_doppler=value["doppler_hz"].cast<std::vector<double>>();
        parameters->m_angle=value["angles_rad"].cast<std::vector<std::vector<double>>>();
        record.matrix=matrix; record.parameters=parameters;
        return record;
    }

    void Collect()
    {
        const auto replies=m_pool.attr("poll")().cast<py::list>();
        for(auto item:replies)
        {
            const auto value=py::reinterpret_borrow<py::dict>(item);
            const auto worker=value["worker_id"].cast<size_t>();
            Require(worker<m_inflight.size() && m_inflight[worker], "Reply from an unknown/idle worker");
            const auto job=*m_inflight[worker];
            Require(value["request_id"].cast<uint64_t>()==job.id && value["schema"].cast<int>()==1,
                "Uncorrelated worker completion");
            Require(value["kind"].cast<std::string>()=="result", "Worker failure: "+
                (value.contains("error") ? value["error"].cast<std::string>() : "unknown"));
            Require(value["new_batch_calls"].cast<uint64_t>()==1, "Producer did not force exactly one fresh batch");
            const auto pairs=value["pairs"].cast<py::list>();
            Require(pairs.size()+1==m_endpoints.size(), "Producer returned incomplete anchor coverage");
            std::vector<NativeRtCache::Record> records;
            std::set<size_t> peers;
            for(auto pair:pairs) records.push_back(Decode(py::reinterpret_borrow<py::dict>(pair),job,peers));
            // Decode/validate the complete response before publishing any pair.
            uint64_t accepted=0,discarded=0;
            py::list outcomes;
            for(const auto& record:records)
            {
                const auto result=job.cold ? NativeRtCache::Publication{false,"cold_warmup_not_served"}
                                           : m_cache->Publish(record);
                if(result.accepted) { ++accepted; ++m_publishedPairs; }
                else
                {
                    ++discarded; ++m_discarded; ++m_discardReasons[result.reason];
                    Require(job.cold || SoftDiscard(result.reason), "Malformed/unusable publication: "+result.reason);
                }
                outcomes.append(py::dict("first"_a=record.direction[0],"second"_a=record.direction[1],
                    "accepted"_a=result.accepted,"reason"_a=result.reason));
            }
            ++m_completed; if(job.cold) ++m_cold;
            if(accepted) ++m_publishedResults;
            auto log=py::dict("schema"_a=1,"worker_id"_a=worker,"request_id"_a=job.id,"anchor_index"_a=job.anchor,
                "cold_warmup"_a=job.cold,"sample_native_ns"_a=job.nativeNs,
                "sample_host_monotonic_ns"_a=job.hostNs,"received_host_monotonic_ns"_a=NativeRtCache::HostNowNs(),
                "producer_wall_ns"_a=value["producer_wall_ns"],"sample_to_completion_ns"_a=value["sample_to_completion_ns"],
                "batch_stats"_a=value["batch_stats"],"published_pairs"_a=accepted,"discarded_pairs"_a=discarded,
                "outcomes"_a=outcomes);
            std::vector<int64_t> poseEpochs;
            for (const auto& pose : job.poses) poseEpochs.push_back(pose.sampleNs);
            log["pose_sample_host_ns"] = py::cast(poseEpochs);
            m_log<<Encode(log)<<'\n'; m_log.flush();
            Require(static_cast<bool>(m_log), "Producer result log write failed");
            m_inflight[worker].reset();
        }
    }

    py::dict SnapshotValue()
    {
        const auto stats=m_cache->GetStats();
        py::list pairs;
        for(const auto& state:m_cache->Priorities())
            pairs.append(py::dict("first"_a=state.first,"second"_a=state.second,"valid"_a=state.valid,
                "due"_a=state.due,"reason"_a=state.reason,"source_request_id"_a=state.sourceRequestId,
                "sample_native_ns"_a=state.sampleNativeNs,"sample_host_monotonic_ns"_a=state.sampleHostMonotonicNs,
                "native_age_ns"_a=state.nativeAgeNs,"host_age_ns"_a=state.hostAgeNs,
                "displacement_m"_a=state.displacementM,"velocity_delta_m_s"_a=state.velocityDeltaMps,
                "orientation_delta_rad"_a=state.orientationDeltaRad,"normalized_risk"_a=state.normalizedRisk));
        auto cache=py::dict("hits"_a=stats.hits,"misses"_a=stats.misses,"accepted"_a=stats.accepted,
            "rejected"_a=stats.rejected,"maximum_hit_native_age_ns"_a=stats.maximumHitNativeAgeNs,
            "maximum_hit_host_age_ns"_a=stats.maximumHitHostAgeNs,"rejection_reasons"_a=stats.rejectionReasons,
            "miss_reasons"_a=stats.missReasons,"pairs"_a=pairs);
        return py::dict("schema"_a=1,"execution_mode"_a="worker_pool","started"_a=m_started,"closed"_a=m_closed,
            "failed"_a=m_failed,"error"_a=(m_error.empty() ? py::none() : py::cast(m_error)),"worker_count"_a=m_inflight.size(),
            "submitted_jobs"_a=m_request,"completed_jobs"_a=m_completed,"cold_warmup_jobs"_a=m_cold,
            "published_results"_a=m_publishedResults,"published_pairs"_a=m_publishedPairs,
            "discarded_pairs"_a=m_discarded,"discard_reasons"_a=m_discardReasons,
            "poll_calls"_a=m_pollCalls,"max_poll_wall_ns"_a=m_maxPollWallNs,"cache"_a=cache,
            "cache_snapshot_native_ns"_a=Simulator::Now().GetNanoSeconds(),
            "cache_snapshot_host_monotonic_ns"_a=NativeRtCache::HostNowNs(),
            "pool"_a=(m_pool ? m_pool.attr("snapshot")() : py::none()),"log_path"_a=m_logPath);
    }
};
} // namespace bas
