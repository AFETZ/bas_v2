// Main-thread bridge for native RT producers. Packet callbacks use NativeRtCache
// only; Python, process I/O and RT calculation never enter that callback path.
#pragma once
#include "native-live-state.h"
#include "native-rt-cache.h"
#include "ns3/event-id.h"
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <fstream>
#include <optional>
#include <set>
#include <thread>

namespace bas
{
using namespace pybind11::literals;

// Scheduling hints only: neither the forecast nor a worker completion can
// change the cache's measured poses, sample epochs, or admission guards.
class NativeRtPrefetch
{
  public:
    static constexpr int64_t kPollPeriodNs = 5000000;
    static constexpr int64_t kMinimumLeadNs = 100000000;
    static constexpr size_t kWindow = 64;
    static constexpr double kGrowthFactor = 2;
    static constexpr const char* kMethod = "edf_guard_forecast_v1";
    using Pair = std::pair<size_t, size_t>;
    struct Forecast
    {
        NativeRtCache::PairStatus state;
        int64_t remainingNs;
        bool eligible;
        std::string limitingGuard;
    };
    struct Choice
    {
        size_t anchor, coveredPairs;
        Forecast urgent;
    };

    void ObserveResponse(int64_t poseToReceiptNs, bool cold)
    {
        if (cold) return;
        if (poseToReceiptNs < 0) throw std::invalid_argument("Negative RT response age");
        m_responseAges.push_back(poseToReceiptNs);
        if (m_responseAges.size() > kWindow) m_responseAges.pop_front();
    }
    size_t WindowSize() const { return m_responseAges.size(); }
    int64_t ResponseP95Ns() const
    {
        if (m_responseAges.empty()) return 0;
        std::vector<int64_t> ordered(m_responseAges.begin(), m_responseAges.end());
        std::sort(ordered.begin(), ordered.end());
        return ordered[(95 * ordered.size() + 99) / 100 - 1]; // Nearest rank.
    }
    int64_t LeadNs() const
    {
        const auto p95 = ResponseP95Ns();
        return std::max(kMinimumLeadNs, p95 > INT64_MAX - kPollPeriodNs
            ? INT64_MAX : p95 + kPollPeriodNs);
    }
    static Forecast Predict(const NativeRtCache::PairStatus& state,
                            const NativeRtCache::Limits& limits, int64_t leadNs)
    {
        Forecast result{state, 0, true, "invalid"};
        if (!state.valid || state.nativeAgeNs < 0 || state.hostAgeNs < 0) return result;
        double remaining = std::max<int64_t>(0, limits.maximumAge.GetNanoSeconds() -
            std::max(state.nativeAgeNs, state.hostAgeNs));
        result.limitingGuard = state.hostAgeNs > state.nativeAgeNs ? "host_age" : "native_age";
        const auto constrain = [&](double delta, double limit, const char* guard) {
            if (delta <= 0) return;
            // Double the observed growth for acceleration. This extrapolates
            // urgency, never a pose or a CIR. Unknown same-epoch growth is due.
            const double time = delta >= limit || state.nativeAgeNs == 0 ? 0 :
                state.nativeAgeNs * ((limit - delta) / (kGrowthFactor * delta));
            if (time < remaining) { remaining = time; result.limitingGuard = guard; }
        };
        constrain(state.displacementM, limits.displacementM, "displacement");
        constrain(state.velocityDeltaMps, limits.velocityDeltaMps, "velocity");
        constrain(state.orientationDeltaRad, limits.orientationDeltaRad, "orientation");
        result.remainingNs = static_cast<int64_t>(remaining);
        result.eligible = state.due || result.remainingNs <= leadNs;
        return result;
    }
    static std::vector<Forecast> Rank(const std::vector<NativeRtCache::PairStatus>& states,
                                     const NativeRtCache::Limits& limits, int64_t leadNs)
    {
        std::vector<Forecast> ranked;
        for (const auto& state : states) ranked.push_back(Predict(state, limits, leadNs));
        std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b) {
            if (a.remainingNs != b.remainingNs) return a.remainingNs < b.remainingNs;
            if (a.state.normalizedRisk != b.state.normalizedRisk)
                return a.state.normalizedRisk > b.state.normalizedRisk;
            return Pair(a.state.first, a.state.second) < Pair(b.state.first, b.state.second);
        });
        return ranked;
    }
    static std::optional<Choice> Choose(const std::vector<Forecast>& ranked, const std::set<Pair>& covered)
    {
        const auto uncovered = [&](const Forecast& forecast) {
            return forecast.eligible && !covered.count(Key(forecast.state.first, forecast.state.second));
        };
        const auto urgent = std::find_if(ranked.begin(), ranked.end(), uncovered);
        if (urgent == ranked.end()) return std::nullopt;
        size_t selected = urgent->state.first;
        const auto other = urgent->state.second;
        // Both anchors cover the earliest pair. Prefer coverage of the next
        // earliest uncovered deadline, before any static count or risk sum.
        for (const auto& forecast : ranked)
        {
            if (!uncovered(forecast)) continue;
            const auto contains = [&](size_t anchor) {
                return forecast.state.first == anchor || forecast.state.second == anchor;
            };
            if (contains(selected) != contains(other))
            { if (contains(other)) selected = other; break; }
        }
        size_t count = 0;
        for (const auto& forecast : ranked)
            if (uncovered(forecast) && (forecast.state.first == selected || forecast.state.second == selected)) ++count;
        return Choice{selected, count, *urgent};
    }
  private:
    std::deque<int64_t> m_responseAges;
    static Pair Key(size_t a, size_t b) { return {std::min(a,b), std::max(a,b)}; }
};

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
            m_next = Simulator::Schedule(NanoSeconds(kPollPeriodNs), &NativeRtPool::Poll, this);
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
    // Completed-result pickup is independent of the unchanged cache guards.
    static constexpr int64_t kPollPeriodNs = NativeRtPrefetch::kPollPeriodNs;
    using Pair = std::pair<size_t, size_t>;
    struct Job
    {
        uint64_t id;
        size_t anchor;
        int64_t nativeNs, hostNs;
        std::vector<MeasuredState> poses;
        bool cold;
        int64_t leadNs{0}, remainingNs{0};
        Pair urgentPair{0,0};
        std::string limitingGuard{"cold_warmup"};
        bool forecastBeforeHalf{false};
        size_t coveredDuePairs{0};
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
    NativeRtPrefetch m_prefetch;
    uint64_t m_forecastDispatches{0};

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

    void Submit(size_t worker, size_t anchor, bool cold, const NativeRtPrefetch::Choice* choice = nullptr)
    {
        Require(!m_inflight.at(worker) && m_request < static_cast<uint64_t>(INT64_MAX), "Invalid/busy submission");
        Job job{++m_request, anchor, Simulator::Now().GetNanoSeconds(), 0, m_snapshot(), cold};
        if (choice)
        {
            job.leadNs = m_prefetch.LeadNs();
            job.remainingNs = choice->urgent.remainingNs;
            job.urgentPair = Key(choice->urgent.state.first, choice->urgent.state.second);
            job.limitingGuard = choice->urgent.limitingGuard;
            job.forecastBeforeHalf = !choice->urgent.state.due;
            job.coveredDuePairs = choice->coveredPairs;
        }
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
        if (job.forecastBeforeHalf) ++m_forecastDispatches;
        m_inflight[worker] = std::move(job);
    }

    // Keep one job per worker and the existing in-flight coverage graph.
    // Forecasts only advance refresh dispatch; the cache still admits each CIR.
    void Dispatch()
    {
        const auto states = NativeRtPrefetch::Rank(m_cache->Priorities(), m_cache->GetLimits(), m_prefetch.LeadNs());
        std::set<Pair> covered;
        for (const auto& job : m_inflight)
            if (job) for (size_t peer=0;peer<m_endpoints.size();++peer)
                if (peer!=job->anchor) covered.insert(Key(job->anchor,peer));
        for (size_t worker=0;worker<m_inflight.size();++worker)
        {
            if (m_inflight[worker]) continue;
            const auto choice = NativeRtPrefetch::Choose(states, covered);
            if (!choice) break;
            const auto selected = choice->anchor;
            Submit(worker,selected,false,&*choice);
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
            const auto receivedNs = NativeRtCache::HostNowNs();
            const auto oldestPose = std::min_element(job.poses.begin(), job.poses.end(),
                [](const auto& a, const auto& b) { return a.sampleNs < b.sampleNs; })->sampleNs;
            m_prefetch.ObserveResponse(receivedNs - oldestPose, job.cold);
            auto log=py::dict("schema"_a=1,"worker_id"_a=worker,"request_id"_a=job.id,"anchor_index"_a=job.anchor,
                "cold_warmup"_a=job.cold,"sample_native_ns"_a=job.nativeNs,
                "sample_host_monotonic_ns"_a=job.hostNs,"received_host_monotonic_ns"_a=receivedNs,
                "producer_wall_ns"_a=value["producer_wall_ns"],"sample_to_completion_ns"_a=value["sample_to_completion_ns"],
                "batch_stats"_a=value["batch_stats"],"published_pairs"_a=accepted,"discarded_pairs"_a=discarded,
                "outcomes"_a=outcomes);
            log["prefetch"] = py::dict("method"_a=NativeRtPrefetch::kMethod,"lead_ns"_a=job.leadNs,
                "remaining_ns"_a=job.remainingNs,"urgent_pair"_a=job.urgentPair,
                "limiting_guard"_a=job.limitingGuard,"before_cache_half_threshold"_a=job.forecastBeforeHalf,
                "covered_due_pairs"_a=job.coveredDuePairs,"pose_to_receipt_ns"_a=receivedNs-oldestPose);
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
        const auto leadNs=m_prefetch.LeadNs();
        py::list inflight;
        for(size_t worker=0;worker<m_inflight.size();++worker)
            if(const auto& job=m_inflight[worker])
            {
                std::vector<int64_t> poses;
                for(const auto& pose:job->poses) poses.push_back(pose.sampleNs);
                inflight.append(py::dict("worker_id"_a=worker,"request_id"_a=job->id,"anchor_index"_a=job->anchor,
                    "sample_native_ns"_a=job->nativeNs,"sample_host_monotonic_ns"_a=job->hostNs,
                    "pose_sample_host_ns"_a=poses,"cold_warmup"_a=job->cold,
                    "lead_ns"_a=job->leadNs,"remaining_ns"_a=job->remainingNs,
                    "urgent_pair"_a=job->urgentPair,"limiting_guard"_a=job->limitingGuard,
                    "before_cache_half_threshold"_a=job->forecastBeforeHalf));
            }
        py::list pairs;
        for(const auto& state:m_cache->Priorities())
        {
            const auto forecast=NativeRtPrefetch::Predict(state,m_cache->GetLimits(),leadNs);
            pairs.append(py::dict("first"_a=state.first,"second"_a=state.second,"valid"_a=state.valid,
                "due"_a=state.due,"reason"_a=state.reason,"source_request_id"_a=state.sourceRequestId,
                "sample_native_ns"_a=state.sampleNativeNs,"sample_host_monotonic_ns"_a=state.sampleHostMonotonicNs,
                "native_age_ns"_a=state.nativeAgeNs,"host_age_ns"_a=state.hostAgeNs,
                "displacement_m"_a=state.displacementM,"velocity_delta_m_s"_a=state.velocityDeltaMps,
                "orientation_delta_rad"_a=state.orientationDeltaRad,"normalized_risk"_a=state.normalizedRisk,
                "prefetch_remaining_ns"_a=forecast.remainingNs,"prefetch_eligible"_a=forecast.eligible,
                "prefetch_limiting_guard"_a=forecast.limitingGuard));
        }
        auto cache=py::dict("hits"_a=stats.hits,"misses"_a=stats.misses,"accepted"_a=stats.accepted,
            "rejected"_a=stats.rejected,"maximum_hit_native_age_ns"_a=stats.maximumHitNativeAgeNs,
            "maximum_hit_host_age_ns"_a=stats.maximumHitHostAgeNs,"rejection_reasons"_a=stats.rejectionReasons,
            "miss_reasons"_a=stats.missReasons,"pairs"_a=pairs);
        return py::dict("schema"_a=1,"execution_mode"_a="worker_pool","started"_a=m_started,"closed"_a=m_closed,
            "failed"_a=m_failed,"error"_a=(m_error.empty() ? py::none() : py::cast(m_error)),"worker_count"_a=m_inflight.size(),
            "submitted_jobs"_a=m_request,"completed_jobs"_a=m_completed,"cold_warmup_jobs"_a=m_cold,
            "published_results"_a=m_publishedResults,"published_pairs"_a=m_publishedPairs,
            "discarded_pairs"_a=m_discarded,"discard_reasons"_a=m_discardReasons,
            "poll_calls"_a=m_pollCalls,"poll_period_ns"_a=kPollPeriodNs,
            "prefetch"_a=py::dict("method"_a=NativeRtPrefetch::kMethod,
                "growth_safety_factor"_a=NativeRtPrefetch::kGrowthFactor,
                "minimum_lead_ns"_a=NativeRtPrefetch::kMinimumLeadNs,"lead_ns"_a=leadNs,
                "hot_window_capacity"_a=NativeRtPrefetch::kWindow,"hot_window_size"_a=m_prefetch.WindowSize(),
                "hot_pose_to_receipt_p95_ns"_a=m_prefetch.ResponseP95Ns(),
                "dispatches_before_cache_half_threshold"_a=m_forecastDispatches,"inflight"_a=inflight),
            "max_poll_wall_ns"_a=m_maxPollWallNs,"cache"_a=cache,
            "cache_snapshot_native_ns"_a=Simulator::Now().GetNanoSeconds(),
            "cache_snapshot_host_monotonic_ns"_a=NativeRtCache::HostNowNs(),
            "pool"_a=(m_pool ? m_pool.attr("snapshot")() : py::none()),"log_path"_a=m_logPath);
    }
};
} // namespace bas
