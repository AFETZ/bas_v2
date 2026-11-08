// CPU boundary contract: real NativeRtPool decoder/barrier, measured mobility,
// native CIR caches and CourseChange observers. Python fixture transports known
// records only; no GPU/RT model, packets, modem or HIL acceptance is claimed.
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include "native-rt-pool.h"
#include "ns3/antenna-module.h"
#include "ns3/core-module.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>

namespace py=pybind11;
using namespace ns3;
using namespace pybind11::literals;
namespace bas
{
class NativeRtBundleContract
{
    static inline size_t checks=0;
    static void Require(bool value,const std::string& message)
    {++checks;if(!value)throw std::runtime_error(message);}
    template<typename Call> static void Reject(Call call,const std::string& message)
    {bool rejected=false;try{call();}catch(const std::exception&){rejected=true;}Require(rejected,message);}
    struct Fixture
    {
        std::vector<Ptr<MeasuredMobility>> measured;
        std::vector<Ptr<MobilityModel>> models;
        std::vector<NativeRtCache::Endpoint> endpoints;
        std::vector<Ptr<UniformPlanarArray>> sourceArrays;
        std::vector<Ptr<NativeRtCache>> sourceCaches;
        std::vector<MeasuredState> frame,applied;
        std::vector<Vector> positions;
        Ptr<NativeRtCache> cache;
        std::unique_ptr<NativeRtPool> pool;
        size_t notifications{0};bool required{true},mutateLastSource{false};
        std::filesystem::path base;
        explicit Fixture(const std::filesystem::path& prefix,size_t sources=2):base(prefix)
        {
            const auto observed=NativeRtCache::HostNowNs()-1000000;
            for(size_t i=0;i<6;++i)
            {
                auto node=CreateObject<Node>();auto pose=CreateObject<MeasuredMobility>();node->AggregateObject(pose);
                MeasuredState initial;initial.position={10.*i,0,20};pose->Apply(initial,false);
                auto array=Array();measured.push_back(pose);models.push_back(pose);endpoints.push_back({pose,array});positions.push_back(initial.position);
                auto latest=initial;latest.position.y=1;latest.sampleNs=observed+100*i;latest.sourceTime=1.+i*.000001;frame.push_back(latest);
            }
            cache=CreateObject<NativeRtCache>(2412000000.,endpoints,NativeRtCache::Limits{});
            pool=std::make_unique<NativeRtPool>(cache,endpoints,[this]{return frame;},NativeRtPool::EventSink{},
                [this](const auto& states,const auto& publish){
                    CommitMeasuredCohort(states,models,positions,applied,NativeRtCache::HostNowNs(),NativeRtCache::HostNowNs(),100000000,true,[&]{
                        if(mutateLastSource)ConstCast<AntennaModel>(sourceArrays.back()->GetAntennaElement())->SetAttribute("Gain",DoubleValue(3));
                        publish();
                    });
                });
            for(size_t index=0;index<sources;++index)
            {
                auto node=CreateObject<Node>();auto pose=CreateObject<MeasuredMobility>();node->AggregateObject(pose);
                MeasuredState configured;configured.position={100.+index*10,20,30};pose->Apply(configured,false);
                auto array=Array();sourceArrays.push_back(array);
                const double frequency=index==1 ? 2422000000. : 2412000000.;
                auto source=CreateObject<NativeRtCache>(frequency,endpoints,NativeRtCache::Endpoint{pose,array},NativeRtCache::Limits{});
                sourceCaches.push_back(source);
                auto all=endpoints;all.push_back({pose,array});
                pool->AddSourceBinding(NativeRtPool::SourceBinding{"source"+std::to_string(index),"profile"+std::to_string(index),source,
                    all,configured,[this](int64_t){return required;}});
            }
            for(const auto& pose:measured)pose->TraceConnectWithoutContext("CourseChange",MakeCallback(&Fixture::Observe,this));
        }
        static Ptr<UniformPlanarArray> Array()
        {
            auto value=CreateObject<UniformPlanarArray>();value->SetNumRows(1);value->SetNumColumns(1);
            value->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector one(1);one[0]={1,0};value->SetBeamformingVector(one);return value;
        }
        py::dict Config()
        {
            py::list profiles;
            for(size_t i=0;i<sourceCaches.size();++i)profiles.append(py::dict("id"_a="profile"+std::to_string(i),
                "frequency_hz"_a=sourceCaches[i]->GetFrequency(),"pattern"_a="iso"));
            return py::dict("source_profiles"_a=profiles);
        }
        void Configure(size_t workers=3)
        {
            pool->m_fullMesh=true;pool->m_candidateCap=1000;pool->m_samplesPerSource=20000;pool->m_maxPaths=90;
            pool->ConfigureSources(Config(),workers);
            pool->m_inflight.resize(workers);pool->m_fullMeshCallsByWorker.resize(workers);pool->m_emitterCallsByWorker.resize(workers);
            pool->m_json=py::module_::import("json");
            py::dict scope;py::exec(R"(
class FixtureTransport:
    def __init__(self): self.sent=[]; self.inbox=[]
    def submit(self,worker,job): self.sent.append((worker,job)); return True
    def poll(self): out,self.inbox=self.inbox,[]; return out
    def snapshot(self): return {'scope':'CPU fixture transport; no worker/GPU proof','workers':[]}
    def close(self): pass
)",scope);
            pool->m_pool=scope["FixtureTransport"]();
            pool->m_logPath=base.string()+".jsonl";pool->m_summaryPath=base.string()+"-summary.json";
            Require(!std::filesystem::exists(pool->m_logPath)&&!std::filesystem::exists(pool->m_summaryPath),"Preserve earlier CPU attempts");
            pool->m_log.open(pool->m_logPath);
            Require(static_cast<bool>(pool->m_log),"Cannot open CPU result log");pool->m_started=true;
        }
        void Observe(Ptr<const MobilityModel>)
        {
            ++notifications;
            for(size_t a=0;a<6;++a)for(size_t b=a+1;b<6;++b)
                Require(cache->GetChannel(endpoints[a].mobility,endpoints[b].mobility,endpoints[a].antenna,endpoints[b].antenna)->m_generatedTime.GetNanoSeconds()==0,
                    "CourseChange observed missing/retimestamped desired CIR");
            if(required)for(const auto& source:pool->m_sources)
                for(size_t peer=0;peer<6;++peer)
                    Require(source.binding.cache->GetChannel(source.binding.endpoints[6].mobility,endpoints[peer].mobility,
                        source.binding.endpoints[6].antenna,endpoints[peer].antenna)->m_generatedTime.GetNanoSeconds()==0,
                        "CourseChange observed a partial source bundle");
        }
        void Reply(size_t worker,bool failure=false,bool partial=false,bool receiptPresent=true)
        {
            const auto& job=*pool->m_inflight.at(worker);
            auto value=py::dict("schema"_a=1,"worker_id"_a=worker,"kind"_a=(failure ? "error" : "result"),"request_id"_a=job.id);
            if(failure){value["error"]="queued producer failure";pool->m_pool.attr("inbox").attr("append")(value);return;}
            py::list poses,pairs;
            for(const auto& pose:job.poses)poses.append(py::dict("position_m"_a=std::vector<double>{pose.position.x,pose.position.y,pose.position.z},
                "velocity_m_s"_a=std::vector<double>{pose.velocity.x,pose.velocity.y,pose.velocity.z},
                "orientation_rad"_a=std::vector<double>{pose.orientation.x,pose.orientation.y,pose.orientation.z},"sample_host_ns"_a=pose.sampleNs));
            const auto pair=[&](size_t a,size_t b){return py::dict("pair"_a=std::vector<size_t>{a,b},"direction"_a=std::vector<size_t>{a,b},
                "fresh_for_request"_a=true,"source_request_id"_a=job.id,"sample_native_ns"_a=job.nativeNs,"generated_ns"_a=job.nativeNs,
                "sample_host_monotonic_ns"_a=job.hostNs,"poses"_a=poses,"coefficient_shape"_a=std::vector<size_t>{1,1,1},
                "coefficient_order"_a="rx_tx_path","coefficients_real"_a=std::vector<double>{1},"coefficients_imag"_a=std::vector<double>{0},
                "delays_s"_a=std::vector<double>{0},"doppler_hz"_a=std::vector<double>{0},"angles_rad"_a=std::vector<std::vector<double>>(4,std::vector<double>{0}));};
            if(job.source)for(size_t peer=0;peer<(partial ? 5 : 6);++peer)pairs.append(pair(6,peer));
            else for(size_t a=0;a<6;++a)for(size_t b=a+1;b<6;++b)pairs.append(pair(a,b));
            value["pairs"]=pairs;
            if(job.source)
            {
                const auto& source=pool->m_sources[*job.source];const auto calls=pool->m_emitterCallsByWorker[worker]+1;
                value["channel_mode"]="source_star";value["source_mode"]="emitter_batch";value["profile_id"]=source.binding.profileId;
                value["frequency_hz"]=source.signature.first;value["new_emitter_batch_calls"]=1;
                const auto& pose=source.binding.emitterStatic;
                value["emitter_batch_stats"]=py::dict("source_mode"_a="emitter_batch","bound"_a=true,"has_published_batch"_a=true,
                    "candidate_allocation_valid"_a=true,"pattern"_a="iso","frequency_hz"_a=source.signature.first,"registered_receivers"_a=6,
                    "last_transmitter_count"_a=1,"last_receiver_count"_a=6,"last_generated_ns"_a=job.nativeNs,
                    "configured_samples_per_source"_a=20000,"configured_max_paths_per_source"_a=1000,"configured_max_number_of_paths"_a=90,
                    "attempted_calls"_a=calls,"completed_calls"_a=calls,"generated_links"_a=6*calls,"cache_read_hits"_a=6*calls,
                    "cache_read_misses"_a=0,"candidate_capacity_reached_calls"_a=0,"configured_candidate_cap"_a=6000,
                    "last_allocated_candidate_cap"_a=6000,"last_candidate_buffer_size"_a=6000,"last_candidate_count"_a=6,
                    "last_retained_paths_per_receiver"_a=std::vector<uint32_t>(6,1),
                    "last_source_position_m"_a=std::vector<double>{pose.position.x,pose.position.y,pose.position.z},
                    "last_source_velocity_m_s"_a=std::vector<double>{0,0,0},"last_source_orientation_rad"_a=std::vector<double>{0,0,0});
            }
            else
            {
                const auto calls=pool->m_fullMeshCallsByWorker[worker]+1;
                value["channel_mode"]="full_mesh";value["new_full_mesh_calls"]=1;
                value["full_mesh_stats"]=py::dict("source_mode"_a="full_mesh","registered_endpoints"_a=6,
                    "last_transmitter_count"_a=6,"last_receiver_count"_a=6,"last_generated_ns"_a=job.nativeNs,
                    "attempted_calls"_a=calls,"completed_calls"_a=calls,"generated_links"_a=15*calls,"candidate_capacity_reached_calls"_a=0,
                    "last_per_tx_candidate_cap"_a=5000,"last_candidate_buffer_size"_a=30000,"last_candidate_count"_a=30,
                    "last_candidate_counts_per_tx"_a=std::vector<uint32_t>(6,5),"last_self_candidate_counts_per_tx"_a=std::vector<uint32_t>(6,0));
            }
            value["producer_wall_ns"]=1;value["sample_to_completion_ns"]=1;
            value["received_host_monotonic_ns"]=NativeRtCache::HostNowNs();
            if(receiptPresent)value["receiver_enqueued_host_monotonic_ns"]=NativeRtCache::HostNowNs();
            pool->m_pool.attr("inbox").attr("append")(value);
        }
        void Empty() {Require(notifications==0&&cache->GetStats().accepted==0,"Partial result published desired cache or notified");
            for(const auto& source:sourceCaches)Require(source->GetStats().accepted==0,"Partial source response reached a live cache");}
        ~Fixture(){if(pool){pool->Close();pool.reset();}cache->Dispose();for(auto& source:sourceCaches)source->Dispose();Simulator::Destroy();}
    };
  public:
    static size_t Run(const std::filesystem::path& prefix)
    {
        {
            // Exercise both old Collect and new CollectBundle log paths with
            // actual int receipts and an absent receipt (JSON null). A py::none
            // ternary would attempt to cast the int to none at runtime.
            Fixture f(prefix.string()+"-desired-only",0);f.Configure(1);f.pool->Poll();
            f.Reply(0,false,false,false);f.pool->Poll();f.Reply(0);f.pool->Poll();
            std::ifstream input(f.pool->m_logPath);std::string line;
            std::getline(input,line);auto first=f.pool->m_json.attr("loads")(line).cast<py::dict>();
            std::getline(input,line);auto second=f.pool->m_json.attr("loads")(line).cast<py::dict>();
            Require(first["receiver_enqueued_host_monotonic_ns"].is_none()&&
                py::isinstance<py::int_>(second["receiver_enqueued_host_monotonic_ns"]),"Desired receipt int/null logging changed type");
            Require(f.notifications==12&&f.cache->GetStats().accepted==30,"Desired-only branch stopped publishing nominal frames");
        }
        {
            Fixture f(prefix.string()+"-success");f.Configure();f.pool->Poll();
            Require(f.pool->m_inflight[0]&&!f.pool->m_inflight[0]->source&&f.pool->m_inflight[1]->source&&f.pool->m_inflight[2]->source,"Fixed worker roles not used");
            f.Reply(0,false,false,false);f.pool->Poll();f.Empty();f.Reply(1);f.pool->Poll();f.Empty();
            f.Reply(2);f.pool->Poll();Require(f.notifications==6&&f.cache->GetStats().accepted==15,"Complete bundle did not notify all six nodes");
            for(const auto& source:f.sourceCaches)Require(source->GetStats().accepted==6,"Source omitted CP/UAV pairs");
            const auto committed=f.pool->m_json.attr("loads")(f.pool->m_lastBundleJson).cast<py::dict>();
            const auto epochs=committed["pose_sample_host_ns"].cast<std::vector<int64_t>>();
            for(size_t i=0;i<6;++i)Require(epochs[i]==f.frame[i].sampleNs,"Bundle retimestamped an original UAV observation");
            Require(f.pool->m_completed==1&&f.pool->m_sources[0].completed==1&&f.pool->m_bundlesCommitted==1,"Desired/source counters lost scope");
            std::ifstream input(f.pool->m_logPath);std::string line;
            std::getline(input,line);auto first=f.pool->m_json.attr("loads")(line).cast<py::dict>();
            std::getline(input,line);auto second=f.pool->m_json.attr("loads")(line).cast<py::dict>();
            Require(first["receiver_enqueued_host_monotonic_ns"].is_none()&&
                py::isinstance<py::int_>(second["receiver_enqueued_host_monotonic_ns"]),"Bundle receipt int/null logging changed type");
        }
        for(bool error:{false,true})
        {
            Fixture f(prefix.string()+(error ? "-queued-error" : "-partial"));f.Configure();f.pool->Poll();
            f.Reply(0);f.Reply(1);f.pool->Poll();f.Empty();f.Reply(2,error,!error);
            Reject([&]{f.pool->Poll();},"Malformed/failed last source was accepted");f.Empty();
            Require(f.pool->m_failed&&f.pool->m_bundlesFailed==1,"Failed bundle not recorded before notify");
        }
        {
            Fixture f(prefix.string()+"-admission");f.Configure();f.pool->Poll();f.mutateLastSource=true;
            f.Reply(0);f.Reply(1);f.Reply(2);Reject([&]{f.pool->Poll();},"Changed native source gain bypassed admission");
            Require(f.notifications==0&&f.pool->m_failed&&f.pool->m_bundlesFailed==1&&f.pool->m_bundlesCommitted==0,
                "Actual cache admission failure allowed notification or claimed a committed frame");
            Require(f.cache->GetStats().accepted==15&&f.sourceCaches[0]->GetStats().accepted==6&&f.sourceCaches[1]->GetStats().rejected==1,
                "Internal partial writes/failure statistics were hidden as a rollback");
        }
        {
            Fixture f(prefix.string()+"-aliases",3);f.Configure();f.pool->Poll();f.Reply(0);f.Reply(1);f.Reply(2);f.pool->Poll();f.Empty();
            Require(f.pool->m_inflight[1]&&f.pool->m_inflight[1]->source==2&&f.pool->m_inflight[1]->hostNs==f.pool->m_bundle->hostNs,
                "RF alias was not serialized on its fixed worker at the original frame epoch");
            f.Reply(1);f.pool->Poll();Require(f.notifications==6&&f.sourceCaches[2]->GetStats().accepted==6,"Alias blocked the complete frame");
        }
        {
            Fixture f(prefix.string()+"-inactive");f.required=false;f.Configure();f.pool->Poll();
            Require(!f.pool->m_inflight[1]&&!f.pool->m_inflight[2],"Unrequired sources consumed live jobs");
            f.Reply(0);f.pool->Poll();Require(f.notifications==6&&f.pool->m_lastRequiredSources.empty(),"Inactive sources blocked a frame");
            for(const auto& source:f.pool->m_sources)Require(source.completed==0&&source.livePublishedResults==0,"Inactive source claimed live verification");
        }
        {
            Fixture f(prefix.string()+"-activation-race");f.required=false;f.Configure();f.pool->Poll();
            f.required=true;f.Reply(0);Reject([&]{f.pool->Poll();},"A newly active source was omitted before notification");f.Empty();
        }
        {
            Fixture f(prefix.string()+"-capacity");Reject([&]{f.Configure(2);},"Insufficient RF role capacity silently switched models");
        }
        {
            Fixture f(prefix.string()+"-age");for(auto& pose:f.frame)pose.sampleNs=NativeRtCache::HostNowNs()-101000000;
            f.Configure();f.pool->Poll();f.Reply(0);f.Reply(1);f.Reply(2);
            Reject([&]{f.pool->Poll();},"100ms original pose-age guard was relaxed");f.Empty();
        }
        return checks;
    }
};
} // namespace bas
int main(int argc,char** argv)
{
    if(argc!=2||std::filesystem::exists(argv[1]))throw std::runtime_error("Expected a new result.json path");
    py::scoped_interpreter python;std::string error;size_t checks=0;
    try{checks=bas::NativeRtBundleContract::Run(std::filesystem::path(argv[1]).replace_extension());}
    catch(const std::exception& failure){error=failure.what();Simulator::Destroy();}
    const auto result=py::dict("schema"_a=1,"passed"_a=error.empty(),"checks"_a=checks,"error"_a=error,
        "scope"_a="CPU production bundle decoder/cache/quiet-publish-notify boundary with fixture transport; no RT or hardware qualification");
    const auto encoded=py::module_::import("json").attr("dumps")(result,"indent"_a=2).cast<std::string>();
    std::ofstream(argv[1])<<encoded<<'\n';std::cout<<encoded<<'\n';return error.empty()?0:1;
}
