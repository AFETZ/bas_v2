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
#include <time.h>

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
    using CommitSnapshot = std::function<void(const std::vector<MeasuredState>&, const std::function<void()>&)>;
    using EventSink = std::function<void(const std::string&, const std::string&, double, const std::string&)>;
    struct SourceBinding
    {
        std::string id, profileId;
        Ptr<NativeRtCache> cache;
        std::vector<NativeRtCache::Endpoint> endpoints; // Base cohort, then emitter.
        MeasuredState emitterStatic; // Configuration, not a measured device pose.
        std::function<bool(int64_t lookaheadNs)> required; // Empty means required.
    };

    NativeRtPool(Ptr<NativeRtCache> cache, std::vector<NativeRtCache::Endpoint> endpoints,
                 Snapshot snapshot, EventSink event, CommitSnapshot commit = {})
        : m_cache(std::move(cache)), m_endpoints(std::move(endpoints)),
          m_snapshot(std::move(snapshot)), m_event(std::move(event)), m_commit(std::move(commit))
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

    void AddSourceBinding(SourceBinding binding)
    {
        Require(!m_started && !m_closed && !binding.id.empty() && !binding.profileId.empty() && binding.cache,
                "Bind an identified source/cache before Start");
        Require(binding.endpoints.size()==m_endpoints.size()+1,"Source binding must retain every CP/UAV receiver");
        for(size_t i=0;i<m_endpoints.size();++i)
            Require(binding.endpoints[i].mobility==m_endpoints[i].mobility &&
                    binding.endpoints[i].antenna==m_endpoints[i].antenna,"Source receiver cohort differs from desired cohort");
        for(const auto& source:m_sources) Require(source.binding.id!=binding.id,"Duplicate source binding ID");
        const auto states=binding.cache->Priorities();
        Require(states.size()==m_endpoints.size(),"Source provider is not a complete emitter STAR cache");
        std::set<size_t> receivers;
        for(const auto& state:states)
        {
            Require(state.second==m_endpoints.size() && state.first<m_endpoints.size(),"Source cache contains receiver/receiver links");
            receivers.insert(state.first);
        }
        Require(receivers.size()==m_endpoints.size(),"Source cache omits a receiver");
        ValidateStaticEmitter(binding);
        m_sources.push_back(SourceRuntime{std::move(binding)});
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
        m_fullMesh = config.contains("desired_mode") && config["desired_mode"].cast<std::string>() == "full_mesh";
        Require(!m_fullMesh || static_cast<bool>(m_commit), "Full mesh requires a measured-cohort commit owner");
        m_candidateCap = config["radio"]["max_paths_per_source"].cast<uint32_t>();
        m_samplesPerSource=config["radio"]["samples_per_source"].cast<uint32_t>();
        m_maxPaths=config["radio"]["max_number_of_paths"].cast<double>();
        Require(config["root"].cast<std::string>() == path(root).attr("resolve")().attr("__str__")().cast<std::string>(),
                "Frozen worker root does not match the run root");
        Require(config["cohort"].cast<py::list>().size() == m_endpoints.size(), "Frozen worker cohort size mismatch");
        Require(config["radio"]["frequency_hz"].cast<double>() == m_cache->GetFrequency(), "Worker/cache carrier mismatch");
        Require(config["radio"]["max_number_of_paths"].cast<double>() <= 90, "Cache supports at most 90 native paths");
        ConfigureSources(config,workers);
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
        m_fullMeshCallsByWorker.resize(workers,0);
        m_emitterCallsByWorker.resize(workers,0);
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
        if(!m_sources.empty())
        {
            m_warming=true;
            // Only each worker's assigned RF role is warmed. No desired solve
            // runs on a source worker and cold data never enters live caches.
            refresh(); BeginBundle(true); DispatchBundle();
            while(m_bundle)
            {
                Require(NativeRtCache::HostNowNs()<=deadline,"Assigned-role cold warmup timeout");
                refresh(); Collect(); DispatchBundle(); if(m_bundle)SleepWarm();
            }
            refresh(); BeginBundle(false); DispatchBundle();
            while(m_bundle)
            {
                Require(NativeRtCache::HostNowNs()<=deadline,"Joint fresh bootstrap timeout");
                refresh(); Collect(); DispatchBundle(); if(m_bundle)SleepWarm();
            }
            Emit("rt_pool_warm",m_endpoints.size()*(m_endpoints.size()-1)/2,
                 "desired_and_required_source_bundle_valid;assigned_roles_only;original_epochs");
            m_warming=false;
            return;
        }
        refresh();
        for (size_t worker = 0; worker < m_inflight.size(); ++worker)
            Submit(worker, m_fullMesh ? 0 : worker % m_endpoints.size(), true);
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
            m_warming=false;
            MarkBundleFailed();
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
            m_maxPollWallNs = std::max(m_maxPollWallNs, NativeRtCache::HostNowNs() - started);
            MarkBundleFailed();
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
    friend class NativeRtBundleContract;
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
        std::optional<size_t> source;
        uint64_t bundleId{0};
    };
    using SourceSignature=std::pair<double,std::string>;
    struct SourceRuntime
    {
        SourceBinding binding;
        SourceSignature signature;
        size_t worker{0};
        uint64_t completed{0},cold{0},publishedResults{0},publishedPairs{0},livePublishedResults{0};
        uint64_t discarded{0};
        std::map<std::string,uint64_t> discardReasons;
        uint64_t lastPublishedBundle{0},lastPublishedRequest{0},lastPublishedDesiredRequest{0};
        int64_t lastPublishedNativeNs{-1},lastPublishedHostNs{-1};
        std::string lastStatsJson,lastPublishedStatsJson;
    };
    struct Completed
    {
        Job job;
        std::vector<NativeRtCache::Record> records;
        std::string statsJson;
    };
    struct Bundle
    {
        uint64_t id;
        int64_t nativeNs,hostNs;
        std::vector<MeasuredState> poses;
        bool cold,bootstrap,desiredSubmitted{false};
        bool failedBeforeNotify{false};
        bool allPublishedBeforeNotify{false};
        std::vector<size_t> required;
        int64_t lookaheadNs{0};
        std::vector<std::pair<size_t,std::string>> requiredReasons;
        std::optional<Completed> desired;
        std::map<size_t,Completed> sources;
    };
    Ptr<NativeRtCache> m_cache;
    std::vector<NativeRtCache::Endpoint> m_endpoints;
    Snapshot m_snapshot;
    EventSink m_event;
    CommitSnapshot m_commit;
    py::object m_pool, m_json;
    std::vector<std::optional<Job>> m_inflight;
    EventId m_next;
    std::ofstream m_log;
    std::string m_logPath, m_summaryPath, m_error, m_finalJson;
    bool m_started{false}, m_closed{false}, m_failed{false};
    uint64_t m_request{0}, m_completed{0}, m_cold{0}, m_publishedResults{0}, m_publishedPairs{0}, m_discarded{0};
    uint64_t m_pollCalls{0};
    int64_t m_maxPollWallNs{0};
    uint64_t m_resultLogWrites{0},m_slowestLogRequest{0};
    int64_t m_resultLogWallNs{0},m_slowestLogBeginNs{0},m_slowestLogEncodedNs{0},m_slowestLogEndNs{0};
    int64_t m_slowestLogCpuNs{0};
    std::string m_slowestLogStage;
    std::map<std::string, uint64_t> m_discardReasons;
    NativeRtPrefetch m_prefetch;
    uint64_t m_forecastDispatches{0};
    bool m_fullMesh{false};
    uint32_t m_candidateCap{0},m_samplesPerSource{0};
    double m_maxPaths{90};
    uint64_t m_fullMeshCompleted{0};
    uint64_t m_committedPoseFrames{0};
    std::vector<uint64_t> m_fullMeshCallsByWorker;
    std::string m_lastFullMeshStatsJson;
    std::vector<SourceRuntime> m_sources;
    std::map<SourceSignature,size_t> m_sourceRoles;
    std::vector<uint64_t> m_emitterCallsByWorker;
    std::optional<Bundle> m_bundle;
    uint64_t m_bundleSequence{0},m_bundlesCommitted{0},m_bundlesFailed{0},m_desiredSubmitted{0};
    bool m_warming{false};
    std::vector<std::string> m_lastRequiredSources;
    std::string m_lastBundleJson;
    void MarkBundleFailed()
    {if(m_bundle && !m_bundle->allPublishedBeforeNotify && !m_bundle->failedBeforeNotify)
        {m_bundle->failedBeforeNotify=true;++m_bundlesFailed;}}

    static Vector EndpointOrientation(const NativeRtCache::Endpoint& endpoint)
    {
        VectorValue value;
        if(endpoint.mobility->GetAttributeFailSafe("Orientation",value))return value.Get();
        const auto array=DynamicCast<const UniformPlanarArray>(endpoint.antenna);
        Require(static_cast<bool>(array),"Static source requires a supported UPA");
        return {array->GetAlpha(),array->GetBeta(),0};
    }
    static void ValidateStaticEmitter(const SourceBinding& binding)
    {
        Require(!binding.endpoints.empty(),"Missing static emitter endpoint");
        const auto& endpoint=binding.endpoints.back();
        Require(endpoint.mobility && endpoint.antenna,"Missing static emitter mobility/array");
        const auto& configured=binding.emitterStatic;
        for(const auto& v:{configured.position,configured.velocity,configured.orientation})
            Require(std::isfinite(v.x)&&std::isfinite(v.y)&&std::isfinite(v.z),"Nonfinite configured emitter pose");
        Require(endpoint.mobility->GetPosition()==configured.position && endpoint.mobility->GetVelocity()==configured.velocity &&
            EndpointOrientation(endpoint)==configured.orientation,"Configured emitter differs from the native static source");
    }
    void ConfigureSources(py::dict config,size_t workers)
    {
        if(m_sources.empty())return;
        Require(m_fullMesh && m_commit && config.contains("source_profiles"),"Joint sources require full_mesh and a commit owner/registry");
        for(auto& source:m_sources)
        {
            std::optional<SourceSignature> signature;
            for(auto item:config["source_profiles"].cast<py::list>())
            {
                const auto profile=py::reinterpret_borrow<py::dict>(item);
                if(profile["id"].cast<std::string>()==source.binding.profileId)
                    signature=SourceSignature{profile["frequency_hz"].cast<double>(),profile["pattern"].cast<std::string>()};
            }
            Require(signature.has_value(),"Source binding has an unknown frozen profile_id");
            Require(source.binding.cache->GetFrequency()==signature->first,"Source cache/profile carrier mismatch");
            auto role=m_sourceRoles.find(*signature);
            if(role==m_sourceRoles.end())role=m_sourceRoles.emplace(*signature,m_sourceRoles.size()+1).first;
            Require(role->second<workers,"Not enough workers for desired plus distinct source RF roles");
            source.signature=*signature;source.worker=role->second;
        }
    }

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
    static std::pair<int64_t,int64_t> ConsumerTick()
    {
        timespec cpu{};
        if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&cpu)!=0)
            throw std::runtime_error("Cannot observe native RT consumer thread CPU clock");
        return {NativeRtCache::HostNowNs(),static_cast<int64_t>(cpu.tv_sec)*1000000000LL+cpu.tv_nsec};
    }
    void WriteResultLog(py::handle value,uint64_t request,const char* stage)
    {
        const auto begin=ConsumerTick();
        const auto encoded=Encode(value);
        const auto encodedEnd=ConsumerTick();
        m_log<<encoded<<'\n';m_log.flush();
        const auto end=ConsumerTick();
        ++m_resultLogWrites;m_resultLogWallNs+=end.first-begin.first;
        if(end.first-begin.first>m_slowestLogEndNs-m_slowestLogBeginNs)
        {
            m_slowestLogRequest=request;m_slowestLogStage=stage;
            m_slowestLogBeginNs=begin.first;m_slowestLogEncodedNs=encodedEnd.first;m_slowestLogEndNs=end.first;
            m_slowestLogCpuNs=end.second-begin.second;
        }
        Require(static_cast<bool>(m_log),"Producer result log write failed");
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
        Send(worker,std::move(job));
    }

    void Send(size_t worker,Job job)
    {
        Require(worker<m_inflight.size() && !m_inflight[worker],"Bundle worker is busy/unknown");
        Require(job.poses.size()==m_endpoints.size()+(job.source ? 1 : 0),"Wrong source/desired pose roster");
        py::list poses;
        for(const auto& pose:job.poses)
            poses.append(py::dict("position_m"_a=std::vector<double>{pose.position.x,pose.position.y,pose.position.z},
                "velocity_m_s"_a=std::vector<double>{pose.velocity.x,pose.velocity.y,pose.velocity.z},
                "orientation_rad"_a=std::vector<double>{pose.orientation.x,pose.orientation.y,pose.orientation.z},
                "sample_host_ns"_a=pose.sampleNs));
        auto request=py::dict("kind"_a="solve","request_id"_a=job.id,"sample_native_ns"_a=job.nativeNs,
            "sample_host_monotonic_ns"_a=job.hostNs,"anchor_index"_a=job.anchor,"poses"_a=poses);
        if(job.source)
        {
            request["source_mode"]="emitter_batch";
            request["profile_id"]=m_sources.at(*job.source).binding.profileId;
        }
        else if(m_fullMesh)request["channel_mode"]="full_mesh";
        Require(m_pool.attr("submit")(worker,request).cast<bool>(),"Python pool unexpectedly reported busy");
        if(!job.source)++m_desiredSubmitted;
        if(job.forecastBeforeHalf)++m_forecastDispatches;
        m_inflight[worker]=std::move(job);
    }

    void BeginBundle(bool cold)
    {
        Require(!m_bundle && !Busy() && m_fullMesh && m_commit,"Invalid joint bundle boundary");
        Bundle frame{++m_bundleSequence,Simulator::Now().GetNanoSeconds(),0,m_snapshot(),cold,m_warming};
        frame.hostNs=NativeRtCache::HostNowNs();
        frame.lookaheadNs=m_prefetch.LeadNs();
        Require(frame.poses.size()==m_endpoints.size(),"Joint bundle lost part of its measured cohort");
        for(size_t i=0;i<m_sources.size();++i)
        {
            ValidateStaticEmitter(m_sources[i].binding);
            const auto& required=m_sources[i].binding.required;
            const bool selected=m_warming || !required || required(frame.lookaheadNs);
            if(selected)frame.required.push_back(i);
            frame.requiredReasons.emplace_back(i,m_warming ? "bootstrap_force_all" : !required ? "default_required" : "schedule_callback");
        }
        m_bundle=std::move(frame);
    }

    void DispatchBundle()
    {
        if(!m_bundle)return;
        const auto submit=[&](size_t worker,std::optional<size_t> source) {
            Require(m_request<static_cast<uint64_t>(INT64_MAX),"Request ID overflow");
            Job job{++m_request,source ? m_endpoints.size() : 0,m_bundle->nativeNs,
                m_bundle->hostNs,m_bundle->poses,m_bundle->cold};
            job.source=source;job.bundleId=m_bundle->id;
            if(source)
            {
                auto configured=m_sources[*source].binding.emitterStatic;
                // Configuration observation at the shared frame host epoch;
                // measured UAV epochs above are copied without alteration.
                configured.sampleNs=job.hostNs;job.poses.push_back(configured);
            }
            Send(worker,std::move(job));
        };
        if(!m_bundle->desiredSubmitted)
        {
            submit(0,std::nullopt);m_bundle->desiredSubmitted=true;
        }
        for(const auto& [signature,worker]:m_sourceRoles)
        {
            if(m_inflight[worker])continue;
            for(size_t index:m_bundle->required)
                if(m_sources[index].worker==worker && !m_bundle->sources.count(index))
                {submit(worker,index);break;}
        }
    }

    void CommitBundle()
    {
        if(!m_bundle || !m_bundle->desired || m_bundle->sources.size()!=m_bundle->required.size())return;
        auto& frame=*m_bundle;
        if(frame.cold){m_bundle.reset();return;}
        for(size_t index=0;index<m_sources.size();++index)
        {
            const auto& required=m_sources[index].binding.required;
            Require((required && !required(0)) || std::find(frame.required.begin(),frame.required.end(),index)!=frame.required.end(),
                "Source became required while waiting for an incomplete bundle roster");
        }
        const auto publish=[&]() {
            // ns-3 cannot interleave a packet/event callback inside this
            // synchronous phase. A failure aborts the run before pose notify;
            // this is observable atomicity, not internal rollback on failure.
            const auto admit=[&](Ptr<NativeRtCache> cache,const Completed& result,SourceRuntime* source) {
                for(const auto& record:result.records)
                {
                    const auto outcome=cache->Publish(record);
                    if(!outcome.accepted)
                    {
                        if(source){++source->discarded;++source->discardReasons[outcome.reason];}
                        else {++m_discarded;++m_discardReasons[outcome.reason];}
                        throw std::runtime_error("Joint CIR bundle admission failed before notify: "+outcome.reason);
                    }
                    if(source)++source->publishedPairs;
                    else ++m_publishedPairs;
                }
            };
            admit(m_cache,*frame.desired,nullptr);
            for(size_t index:frame.required)admit(m_sources[index].binding.cache,frame.sources.at(index),&m_sources[index]);
            for(const auto& state:m_cache->Priorities())Require(state.valid,"Desired bundle expired before pose notification");
            for(size_t index:frame.required)
                for(const auto& state:m_sources[index].binding.cache->Priorities())
                    Require(state.valid,"Required source bundle expired before pose notification");
            frame.allPublishedBeforeNotify=true;
        };
        try {m_commit(frame.poses,publish);}
        catch(...){MarkBundleFailed();throw;}
        ++m_bundlesCommitted;++m_committedPoseFrames;++m_publishedResults;
        m_lastRequiredSources.clear();
        for(size_t index:frame.required)
        {
            auto& source=m_sources[index];++source.publishedResults;
            if(!frame.bootstrap)++source.livePublishedResults;
            source.lastPublishedBundle=frame.id;source.lastPublishedRequest=frame.sources.at(index).job.id;
            source.lastPublishedDesiredRequest=frame.desired->job.id;
            source.lastPublishedNativeNs=frame.nativeNs;source.lastPublishedHostNs=frame.hostNs;
            source.lastPublishedStatsJson=frame.sources.at(index).statsJson;
            m_lastRequiredSources.push_back(source.binding.id);
        }
        py::list sourceRequests,requirements;
        for(size_t index:frame.required)sourceRequests.append(py::dict("id"_a=m_sources[index].binding.id,
            "request_id"_a=frame.sources.at(index).job.id));
        for(const auto& [index,reason]:frame.requiredReasons)
            requirements.append(py::dict("id"_a=m_sources[index].binding.id,"required"_a=frame.sources.count(index)!=0,"reason"_a=reason));
        std::vector<int64_t> poseEpochs;for(const auto& pose:frame.poses)poseEpochs.push_back(pose.sampleNs);
        m_lastBundleJson=Encode(py::dict("id"_a=frame.id,"sample_native_ns"_a=frame.nativeNs,
            "sample_host_monotonic_ns"_a=frame.hostNs,"desired_request_id"_a=frame.desired->job.id,
            "pose_sample_host_ns"_a=poseEpochs,
            "required_source_ids"_a=m_lastRequiredSources,"source_requests"_a=sourceRequests,
            "required_lookahead_ns"_a=frame.lookaheadNs,"required_observations"_a=requirements,"bootstrap"_a=frame.bootstrap));
        WriteResultLog(py::dict("schema"_a=1,"stage"_a="bundle_committed","bundle_id"_a=frame.id,
            "sample_native_ns"_a=frame.nativeNs,"sample_host_monotonic_ns"_a=frame.hostNs,
            "required_source_ids"_a=m_lastRequiredSources,"bootstrap"_a=frame.bootstrap),
            frame.desired->job.id,"bundle_committed");
        m_bundle.reset();
    }

    // Keep one job per worker and the existing in-flight coverage graph.
    // Forecasts only advance refresh dispatch; the cache still admits each CIR.
    void Dispatch()
    {
        if(!m_sources.empty())
        {
            if(!m_bundle)BeginBundle(false);
            DispatchBundle();return;
        }
        const auto states = NativeRtPrefetch::Rank(m_cache->Priorities(), m_cache->GetLimits(), m_prefetch.LeadNs());
        if (m_fullMesh)
        {
            // One full-cohort solve covers every unordered pair. Overlapping
            // full jobs duplicate RT work and can only publish older poses.
            if (Busy()) return;
            // A measured velocity can jump at ground contact. Stage the newest
            // whole cohort continuously; apply it only together with its CIR.
            // Cache age/pose guards still run and cannot be extended by waiting.
            Require(!states.empty(),"Full-mesh cohort has no links");
            const NativeRtPrefetch::Choice choice{0,m_endpoints.size()*(m_endpoints.size()-1)/2,states.front()};
            Submit(0,0,false,&choice);
            return;
        }
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
    NativeRtCache::Record Decode(py::dict value, const Job& job, std::set<Pair>& peers)
    {
        const auto count=m_endpoints.size()+(job.source ? 1 : 0);
        const auto requested=value["pair"].cast<std::vector<size_t>>();
        const auto direction=value["direction"].cast<std::vector<size_t>>();
        Require(requested.size()==2 && requested[0]<count && requested[1]<count &&
            (job.source ? requested[0]==m_endpoints.size() && requested[1]<m_endpoints.size() :
             m_fullMesh ? requested[0]<requested[1] : requested[0]==job.anchor && requested[1]!=job.anchor) &&
            peers.insert(Key(requested[0],requested[1])).second && direction.size()==2 &&
            Key(direction[0],direction[1])==Key(requested[0],requested[1]), "Malformed anchor pair/direction");
        if(job.source)Require(direction==requested,"Emitter result reversed the declared source/receiver order");
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
        Require(parameters->m_delay.size()==shape[2] && parameters->m_doppler.size()==shape[2] &&
            parameters->m_angle.size()==4,"Malformed CIR scalar dimensions");
        for(const auto& angles:parameters->m_angle)
        {
            Require(angles.size()==shape[2],"Malformed CIR angle dimensions");
            for(double angle:angles)Require(std::isfinite(angle),"Nonfinite CIR angle");
        }
        for(size_t path=0;path<shape[2];++path)
        {
            const auto delay=parameters->m_delay[path];
            Require(std::isfinite(real[path]) && std::isfinite(imag[path]) && std::isfinite(delay) &&
                std::isfinite(parameters->m_doppler[path]) &&
                (delay>=0 || (delay==-1 && real[path]==0 && imag[path]==0)),"Nonfinite CIR or active negative delay");
        }
        record.matrix=matrix; record.parameters=parameters;
        return record;
    }

    void ValidateFullMeshReply(py::dict value,size_t worker,const Job& job)
    {
        Require(value["channel_mode"].cast<std::string>()=="full_mesh" &&
            value["new_full_mesh_calls"].cast<uint64_t>()==1,
            "Producer did not perform one explicit full-mesh solve");
        const auto stats=value["full_mesh_stats"].cast<py::dict>();
        const uint64_t n=m_endpoints.size(), cap=m_candidateCap*(n-1);
        const auto counts=stats["last_candidate_counts_per_tx"].cast<std::vector<uint32_t>>();
        const auto self=stats["last_self_candidate_counts_per_tx"].cast<std::vector<uint32_t>>();
        Require(stats["source_mode"].cast<std::string>()=="full_mesh" &&
            stats["registered_endpoints"].cast<uint64_t>()==n &&
            stats["last_transmitter_count"].cast<uint64_t>()==n && stats["last_receiver_count"].cast<uint64_t>()==n &&
            stats["last_generated_ns"].cast<int64_t>()==job.nativeNs &&
            stats["completed_calls"].cast<uint64_t>()==m_fullMeshCallsByWorker.at(worker)+1 &&
            stats["attempted_calls"].cast<uint64_t>()==stats["completed_calls"].cast<uint64_t>() &&
            stats["generated_links"].cast<uint64_t>()==stats["completed_calls"].cast<uint64_t>()*n*(n-1)/2 &&
            stats["candidate_capacity_reached_calls"].cast<uint64_t>()==0 &&
            stats["last_per_tx_candidate_cap"].cast<uint64_t>()==cap &&
            stats["last_candidate_buffer_size"].cast<uint64_t>()==n*cap &&
            counts.size()==n && self.size()==n,
            "Full-mesh SDK mode, cohort, budget, freshness or capacity proof disagrees");
        uint64_t total=0;
        for(size_t i=0;i<n;++i)
        {
            Require(counts[i]<cap && self[i]<=counts[i],"Full-mesh source buffer saturated or malformed");
            total+=counts[i];
        }
        Require(total==stats["last_candidate_count"].cast<uint64_t>(),"Full-mesh candidate accounting disagrees");
    }

    void ValidateEmitterReply(py::dict value,size_t worker,const Job& job)
    {
        const auto& source=m_sources.at(*job.source);
        Require(source.worker==worker && value["channel_mode"].cast<std::string>()=="source_star" &&
            value["source_mode"].cast<std::string>()=="emitter_batch" &&
            value["profile_id"].cast<std::string>()==source.binding.profileId &&
            value["frequency_hz"].cast<double>()==source.signature.first &&
            value["new_emitter_batch_calls"].cast<uint64_t>()==1,"Source result violated its assigned RF role/profile");
        const auto stats=value["emitter_batch_stats"].cast<py::dict>();
        const uint64_t n=m_endpoints.size(),cap=static_cast<uint64_t>(m_candidateCap)*n;
        Require(stats["source_mode"].cast<std::string>()=="emitter_batch" && stats["bound"].cast<bool>() &&
            stats["has_published_batch"].cast<bool>() && stats["candidate_allocation_valid"].cast<bool>() &&
            stats["pattern"].cast<std::string>()==source.signature.second && stats["frequency_hz"].cast<double>()==source.signature.first &&
            stats["registered_receivers"].cast<uint64_t>()==n && stats["last_transmitter_count"].cast<uint64_t>()==1 &&
            stats["last_receiver_count"].cast<uint64_t>()==n && stats["last_generated_ns"].cast<int64_t>()==job.nativeNs &&
            stats["configured_samples_per_source"].cast<uint64_t>()==m_samplesPerSource &&
            stats["configured_max_paths_per_source"].cast<uint64_t>()==m_candidateCap &&
            stats["configured_max_number_of_paths"].cast<double>()==m_maxPaths &&
            stats["completed_calls"].cast<uint64_t>()==m_emitterCallsByWorker.at(worker)+1 &&
            stats["attempted_calls"].cast<uint64_t>()==stats["completed_calls"].cast<uint64_t>() &&
            stats["generated_links"].cast<uint64_t>()==stats["completed_calls"].cast<uint64_t>()*n &&
            stats["cache_read_hits"].cast<uint64_t>()==stats["completed_calls"].cast<uint64_t>()*n &&
            stats["cache_read_misses"].cast<uint64_t>()==0 && stats["candidate_capacity_reached_calls"].cast<uint64_t>()==0 &&
            stats["configured_candidate_cap"].cast<uint64_t>()==cap &&
            stats["last_allocated_candidate_cap"].cast<uint64_t>()==cap && stats["last_candidate_buffer_size"].cast<uint64_t>()==cap &&
            stats["last_candidate_count"].cast<uint64_t>()<cap &&
            stats["last_retained_paths_per_receiver"].cast<std::vector<uint32_t>>().size()==n,
            "Source SDK cohort, actual counter, original epoch or candidate allocation proof disagrees");
        Require(ReadVector(stats,"last_source_position_m")==source.binding.emitterStatic.position &&
            ReadVector(stats,"last_source_velocity_m_s")==source.binding.emitterStatic.velocity &&
            ReadVector(stats,"last_source_orientation_rad")==source.binding.emitterStatic.orientation,
            "Source SDK pose differs from its configuration observation");
    }

    void CollectBundle()
    {
        for(auto item:m_pool.attr("poll")().cast<py::list>())
        {
            // Main-thread wall/CPU observations distinguish producer latency
            // from validation, allocation, Python encoding or descheduling.
            const auto entered=ConsumerTick();
            const auto value=py::reinterpret_borrow<py::dict>(item);
            const auto worker=value["worker_id"].cast<size_t>();
            Require(worker<m_inflight.size() && m_inflight[worker] && m_bundle,"Reply outside a pending joint bundle");
            const auto job=*m_inflight[worker];
            Require(job.bundleId==m_bundle->id && value["request_id"].cast<uint64_t>()==job.id &&
                value["schema"].cast<int>()==1,"Uncorrelated joint result");
            Require(value["kind"].cast<std::string>()=="result","Joint worker failure: "+
                (value.contains("error") ? value["error"].cast<std::string>() : "unknown"));
            if(job.source)ValidateEmitterReply(value,worker,job);
            else {Require(worker==0,"Desired result came from a source worker");ValidateFullMeshReply(value,worker,job);}
            const auto pairs=value["pairs"].cast<py::list>();
            Require(pairs.size()==(job.source ? m_endpoints.size() : m_endpoints.size()*(m_endpoints.size()-1)/2),
                "Incomplete desired/source bundle component");
            const auto validated=ConsumerTick();
            Completed completed{job,{},{}};std::set<Pair> peers;
            for(auto pair:pairs)completed.records.push_back(Decode(py::reinterpret_borrow<py::dict>(pair),job,peers));
            const auto decoded=ConsumerTick();
            const auto proof=value[job.source ? "emitter_batch_stats" : "full_mesh_stats"].cast<py::dict>();
            completed.statsJson=Encode(proof);
            const auto encoded=ConsumerTick();
            if(job.source)
            {
                auto& source=m_sources.at(*job.source);++source.completed;if(job.cold)++source.cold;
                ++m_emitterCallsByWorker.at(worker);source.lastStatsJson=completed.statsJson;
                Require(m_bundle->sources.emplace(*job.source,std::move(completed)).second,"Duplicate source result in a bundle");
            }
            else
            {
                Require(!m_bundle->desired,"Duplicate desired result in a bundle");
                ++m_completed;if(job.cold)++m_cold;++m_fullMeshCompleted;++m_fullMeshCallsByWorker.at(worker);
                m_lastFullMeshStatsJson=completed.statsJson;m_bundle->desired=std::move(completed);
            }
            const auto staged=ConsumerTick();
            const auto received=staged.first;
            const auto oldest=std::min_element(job.poses.begin(),job.poses.end(),[](const auto& a,const auto& b){return a.sampleNs<b.sampleNs;})->sampleNs;
            m_prefetch.ObserveResponse(received-oldest,job.cold);
            auto log=py::dict("schema"_a=1,"stage"_a="decoded_for_bundle","bundle_id"_a=job.bundleId,
                "worker_id"_a=worker,"request_id"_a=job.id,"sample_native_ns"_a=job.nativeNs,
                "sample_host_monotonic_ns"_a=job.hostNs,"cold_warmup"_a=job.cold,
                "channel_mode"_a=(job.source ? "source_star" : "full_mesh"),"published_pairs"_a=0,
                "producer_wall_ns"_a=value["producer_wall_ns"],"sample_to_completion_ns"_a=value["sample_to_completion_ns"],
                "received_host_monotonic_ns"_a=received,"poll_received_host_monotonic_ns"_a=value["received_host_monotonic_ns"]);
            log["consumer_processing_ns"]=py::dict("scope"_a="main_thread_wall_and_CLOCK_THREAD_CPUTIME_ID",
                "entered_host_monotonic_ns"_a=entered.first,
                "validation_wall"_a=validated.first-entered.first,"validation_cpu"_a=validated.second-entered.second,
                "decode_wall"_a=decoded.first-validated.first,"decode_cpu"_a=decoded.second-validated.second,
                "stats_encode_wall"_a=encoded.first-decoded.first,"stats_encode_cpu"_a=encoded.second-decoded.second,
                "stage_wall"_a=staged.first-encoded.first,"stage_cpu"_a=staged.second-encoded.second);
            if (value.contains("receiver_enqueued_host_monotonic_ns"))
                log["receiver_enqueued_host_monotonic_ns"]=value["receiver_enqueued_host_monotonic_ns"];
            else log["receiver_enqueued_host_monotonic_ns"]=py::none();
            log[job.source ? "emitter_batch_stats" : "full_mesh_stats"]=proof;
            if(job.source){log["source_id"]=m_sources[*job.source].binding.id;log["source_mode"]="emitter_batch";log["source_pose_scope"]="configuration_static";}
            std::vector<int64_t> epochs;for(const auto& pose:job.poses)epochs.push_back(pose.sampleNs);
            log["pose_sample_host_ns"]=epochs;
            WriteResultLog(log,job.id,"decoded_for_bundle");
            m_inflight[worker].reset();
        }
        CommitBundle();
    }

    void Collect()
    {
        if(!m_sources.empty()){CollectBundle();return;}
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
            if(m_fullMesh)ValidateFullMeshReply(value,worker,job);
            else Require(value["new_batch_calls"].cast<uint64_t>()==1, "Producer did not force exactly one fresh batch");
            const auto pairs=value["pairs"].cast<py::list>();
            Require(pairs.size()==(m_fullMesh ? m_endpoints.size()*(m_endpoints.size()-1)/2 : m_endpoints.size()-1),
                    "Producer returned incomplete cohort coverage");
            std::vector<NativeRtCache::Record> records;
            std::set<Pair> peers;
            for(auto pair:pairs) records.push_back(Decode(py::reinterpret_borrow<py::dict>(pair),job,peers));
            // Decode/validate the complete response before publishing any pair.
            uint64_t accepted=0,discarded=0;
            py::list outcomes;
            const auto publish = [&]() {
                for(const auto& record:records)
                {
                    const auto result=job.cold ? NativeRtCache::Publication{false,"cold_warmup_not_served"}
                                               : m_cache->Publish(record);
                    if(result.accepted) { ++accepted; ++m_publishedPairs; }
                    else
                    {
                        ++discarded; ++m_discarded; ++m_discardReasons[result.reason];
                        Require(job.cold || !m_fullMesh,"Coherent full-mesh frame was not admitted in full: "+result.reason);
                        Require(job.cold || SoftDiscard(result.reason), "Malformed/unusable publication: "+result.reason);
                    }
                    outcomes.append(py::dict("first"_a=record.direction[0],"second"_a=record.direction[1],
                        "accepted"_a=result.accepted,"reason"_a=result.reason));
                }
            };
            if(m_fullMesh && !job.cold)
            {
                // Validate the whole measured cohort, apply it quietly, publish
                // every CIR, THEN let the owner notify mobility observers.
                m_commit(job.poses,publish);
                ++m_committedPoseFrames;
            }
            else publish();
            ++m_completed; if(job.cold) ++m_cold;
            if(m_fullMesh)
            {
                ++m_fullMeshCompleted; ++m_fullMeshCallsByWorker.at(worker);
                m_lastFullMeshStatsJson=Encode(value["full_mesh_stats"]);
            }
            if(accepted) ++m_publishedResults;
            const auto receivedNs = NativeRtCache::HostNowNs();
            const auto oldestPose = std::min_element(job.poses.begin(), job.poses.end(),
                [](const auto& a, const auto& b) { return a.sampleNs < b.sampleNs; })->sampleNs;
            m_prefetch.ObserveResponse(receivedNs - oldestPose, job.cold);
            auto log=py::dict("schema"_a=1,"worker_id"_a=worker,"request_id"_a=job.id,"anchor_index"_a=job.anchor,
                "cold_warmup"_a=job.cold,"sample_native_ns"_a=job.nativeNs,
                "sample_host_monotonic_ns"_a=job.hostNs,"received_host_monotonic_ns"_a=receivedNs,
                "producer_wall_ns"_a=value["producer_wall_ns"],"sample_to_completion_ns"_a=value["sample_to_completion_ns"],
                "published_pairs"_a=accepted,"discarded_pairs"_a=discarded,
                "outcomes"_a=outcomes);
            // Transport receipt, poll pickup and post-publication receipt are
            // separate observations; none replaces the sampled CIR epoch.
            if(value.contains("receiver_enqueued_host_monotonic_ns"))
                log["receiver_enqueued_host_monotonic_ns"]=value["receiver_enqueued_host_monotonic_ns"];
            else log["receiver_enqueued_host_monotonic_ns"]=py::none();
            log["poll_received_host_monotonic_ns"]=value["received_host_monotonic_ns"];
            log["channel_mode"]=m_fullMesh ? "full_mesh" : "receiver_batch";
            if(m_fullMesh) log["full_mesh_stats"]=value["full_mesh_stats"];
            else log["batch_stats"]=value["batch_stats"];
            log["prefetch"] = py::dict("method"_a=(m_fullMesh ? "coherent_pose_frames" : NativeRtPrefetch::kMethod),"lead_ns"_a=job.leadNs,
                "remaining_ns"_a=job.remainingNs,"urgent_pair"_a=job.urgentPair,
                "limiting_guard"_a=job.limitingGuard,"before_cache_half_threshold"_a=job.forecastBeforeHalf,
                "covered_due_pairs"_a=job.coveredDuePairs,"pose_to_receipt_ns"_a=receivedNs-oldestPose);
            std::vector<int64_t> poseEpochs;
            for (const auto& pose : job.poses) poseEpochs.push_back(pose.sampleNs);
            log["pose_sample_host_ns"] = py::cast(poseEpochs);
            WriteResultLog(log,job.id,"desired_published");
            m_inflight[worker].reset();
        }
    }

    py::list SourceSnapshots()
    {
        py::list values;
        for(const auto& source:m_sources)
        {
            const auto stats=source.binding.cache->GetStats();py::list pairs;
            for(const auto& state:source.binding.cache->Priorities())
                pairs.append(py::dict("first"_a=state.first,"second"_a=state.second,"valid"_a=state.valid,
                    "due"_a=state.due,"reason"_a=state.reason,"source_request_id"_a=state.sourceRequestId,
                    "sample_native_ns"_a=state.sampleNativeNs,"sample_host_monotonic_ns"_a=state.sampleHostMonotonicNs,
                    "native_age_ns"_a=state.nativeAgeNs,"host_age_ns"_a=state.hostAgeNs,
                    "displacement_m"_a=state.displacementM,"velocity_delta_m_s"_a=state.velocityDeltaMps,
                    "orientation_delta_rad"_a=state.orientationDeltaRad));
            auto cache=py::dict("hits"_a=stats.hits,"misses"_a=stats.misses,"accepted"_a=stats.accepted,"rejected"_a=stats.rejected,
                "maximum_hit_native_age_ns"_a=stats.maximumHitNativeAgeNs,"maximum_hit_host_age_ns"_a=stats.maximumHitHostAgeNs,
                "rejection_reasons"_a=stats.rejectionReasons,"miss_reasons"_a=stats.missReasons,"pairs"_a=pairs);
            const auto& pose=source.binding.emitterStatic;
            const auto decode=[&](const std::string& json){return json.empty() ? py::dict() : m_json.attr("loads")(json).cast<py::dict>();};
            values.append(py::dict("id"_a=source.binding.id,"profile_id"_a=source.binding.profileId,
                "frequency_hz"_a=source.signature.first,"pattern"_a=source.signature.second,"worker_id"_a=source.worker,
                "configured_pose"_a=py::dict("position_m"_a=std::vector<double>{pose.position.x,pose.position.y,pose.position.z},
                    "velocity_m_s"_a=std::vector<double>{pose.velocity.x,pose.velocity.y,pose.velocity.z},
                    "orientation_rad"_a=std::vector<double>{pose.orientation.x,pose.orientation.y,pose.orientation.z},"scope"_a="configuration_static"),
                "required_now"_a=(!source.binding.required || source.binding.required(m_prefetch.LeadNs())),
                "completed_jobs"_a=source.completed,"cold_warmup_jobs"_a=source.cold,
                "published_results"_a=source.publishedResults,"published_pairs"_a=source.publishedPairs,
                "live_published_results"_a=source.livePublishedResults,
                "discarded_pairs"_a=source.discarded,"discard_reasons"_a=source.discardReasons,
                "last_emitter_batch_stats"_a=decode(source.lastStatsJson),"last_published_emitter_batch_stats"_a=decode(source.lastPublishedStatsJson),
                "last_published_bundle_id"_a=source.lastPublishedBundle,"last_published_request_id"_a=source.lastPublishedRequest,
                "last_published_desired_request_id"_a=source.lastPublishedDesiredRequest,
                "last_published_sample_native_ns"_a=source.lastPublishedNativeNs,
                "last_published_sample_host_monotonic_ns"_a=source.lastPublishedHostNs,
                "cache_snapshot_native_ns"_a=Simulator::Now().GetNanoSeconds(),
                "cache_snapshot_host_monotonic_ns"_a=NativeRtCache::HostNowNs(),"cache"_a=cache));
        }
        return values;
    }

    py::dict BundleSnapshot()
    {
        py::dict pending;
        if(m_bundle)
        {
            std::vector<std::string> required,ready;
            std::vector<int64_t> epochs;for(const auto& pose:m_bundle->poses)epochs.push_back(pose.sampleNs);
            for(size_t index:m_bundle->required)required.push_back(m_sources[index].binding.id);
            for(const auto& [index,result]:m_bundle->sources)ready.push_back(m_sources[index].binding.id);
            pending=py::dict("id"_a=m_bundle->id,"sample_native_ns"_a=m_bundle->nativeNs,
                "sample_host_monotonic_ns"_a=m_bundle->hostNs,"required_source_ids"_a=required,
                "pose_sample_host_ns"_a=epochs,
                "required_lookahead_ns"_a=m_bundle->lookaheadNs,
                "desired_ready"_a=m_bundle->desired.has_value(),"source_ready_ids"_a=ready,
                "cold_warmup"_a=m_bundle->cold,"bootstrap"_a=m_bundle->bootstrap);
        }
        return py::dict("enabled"_a=!m_sources.empty(),"policy"_a="desired_and_required_emitters_before_pose_notify",
            "atomicity"_a="observable_without_internal_rollback_on_failure","submitted"_a=m_bundleSequence,
            "committed"_a=m_bundlesCommitted,"failed_before_notify"_a=m_bundlesFailed,"pending"_a=pending,
            "last_committed_required_source_ids"_a=m_lastRequiredSources,
            "last_committed"_a=(m_lastBundleJson.empty() ? py::dict() : m_json.attr("loads")(m_lastBundleJson).cast<py::dict>()));
    }

    py::list WorkerRoles()
    {
        py::list roles;
        if(m_sources.empty())return roles;
        for(size_t worker=0;worker<m_inflight.size();++worker)
        {
            auto role=py::dict("worker_id"_a=worker,"role"_a=(worker==0 ? "desired_full_mesh" : "idle"),"source_ids"_a=std::vector<std::string>{});
            for(const auto& [signature,assigned]:m_sourceRoles)
                if(assigned==worker)
                {
                    std::vector<std::string> ids;
                    for(const auto& source:m_sources)if(source.worker==worker)ids.push_back(source.binding.id);
                    role["role"]="emitter_batch";role["frequency_hz"]=signature.first;role["pattern"]=signature.second;role["source_ids"]=ids;
                }
            roles.append(role);
        }
        return roles;
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
                auto value=py::dict("worker_id"_a=worker,"request_id"_a=job->id,"anchor_index"_a=job->anchor,
                    "sample_native_ns"_a=job->nativeNs,"sample_host_monotonic_ns"_a=job->hostNs,
                    "pose_sample_host_ns"_a=poses,"cold_warmup"_a=job->cold,
                    "lead_ns"_a=job->leadNs,"remaining_ns"_a=job->remainingNs,
                    "urgent_pair"_a=job->urgentPair,"limiting_guard"_a=job->limitingGuard,
                    "before_cache_half_threshold"_a=job->forecastBeforeHalf);
                if(job->source)
                {
                    value["source_id"]=m_sources[*job->source].binding.id;
                    value["profile_id"]=m_sources[*job->source].binding.profileId;
                    value["channel_mode"]="source_star";value["source_mode"]="emitter_batch";
                }
                else value["channel_mode"]=m_fullMesh ? "full_mesh" : "receiver_batch";
                value["bundle_id"]=job->bundleId;
                inflight.append(value);
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
            "get_channel_calls"_a=stats.getChannelCalls,"get_channel_wall_ns"_a=stats.getChannelWallNs,
            "max_get_channel_wall_ns"_a=stats.maxGetChannelWallNs,
            "rejected"_a=stats.rejected,"maximum_hit_native_age_ns"_a=stats.maximumHitNativeAgeNs,
            "maximum_hit_host_age_ns"_a=stats.maximumHitHostAgeNs,"rejection_reasons"_a=stats.rejectionReasons,
            "miss_reasons"_a=stats.missReasons,"pairs"_a=pairs);
        uint64_t sourceCompleted=0;for(const auto& source:m_sources)sourceCompleted+=source.completed;
        return py::dict("schema"_a=1,"execution_mode"_a="worker_pool","started"_a=m_started,"closed"_a=m_closed,
            "desired_mode"_a=(m_fullMesh ? "full_mesh" : "receiver_batch"),
            "pose_commit_policy"_a=(!m_sources.empty() ? "with_full_mesh_and_required_emitters" : m_fullMesh ? "with_full_mesh_result" : "independent"),
            "committed_pose_frames"_a=m_committedPoseFrames,
            "full_mesh_completed_jobs"_a=m_fullMeshCompleted,
            "last_full_mesh_stats"_a=(m_lastFullMeshStatsJson.empty() ? py::dict() :
                m_json.attr("loads")(m_lastFullMeshStatsJson).cast<py::dict>()),
            "failed"_a=m_failed,"error"_a=(m_error.empty() ? py::none() : py::cast(m_error)),"worker_count"_a=m_inflight.size(),
            "submitted_jobs"_a=m_desiredSubmitted,"completed_jobs"_a=m_completed,"cold_warmup_jobs"_a=m_cold,
            "total_submitted_jobs"_a=m_request,"total_completed_jobs"_a=m_completed+sourceCompleted,
            "source_bindings"_a=SourceSnapshots(),"worker_roles"_a=WorkerRoles(),"bundle"_a=BundleSnapshot(),
            "published_results"_a=m_publishedResults,"published_pairs"_a=m_publishedPairs,
            "discarded_pairs"_a=m_discarded,"discard_reasons"_a=m_discardReasons,
            "poll_calls"_a=m_pollCalls,"poll_period_ns"_a=kPollPeriodNs,
            "result_log_timing"_a=py::dict("writes"_a=m_resultLogWrites,"total_wall_ns"_a=m_resultLogWallNs,
                "scope"_a="synchronous_json_encode_and_stream_flush_main_thread",
                "slowest_request_id"_a=m_slowestLogRequest,"slowest_stage"_a=m_slowestLogStage,
                "slowest_begin_host_ns"_a=m_slowestLogBeginNs,"slowest_encoded_host_ns"_a=m_slowestLogEncodedNs,
                "slowest_end_host_ns"_a=m_slowestLogEndNs,"slowest_thread_cpu_ns"_a=m_slowestLogCpuNs),
            "prefetch"_a=py::dict("method"_a=(!m_sources.empty() ? "coherent_pose_and_source_bundles" : m_fullMesh ? "coherent_pose_frames" : NativeRtPrefetch::kMethod),
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
