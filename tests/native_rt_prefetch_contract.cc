// CPU-only ns-3 scratch contract. Uses the production prefetch policy and real
// CIR-cache admission guards; no Python interpreter, worker, RT solve or PHY mock.
// Stage native-rt-{pool,cache}.h and native-live-state.h beside this source.
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "native-rt-pool.h"
#include <filesystem>
#include <fstream>
#include <iostream>

using namespace ns3;
using Cache = bas::NativeRtCache;
using Policy = bas::NativeRtPrefetch;
namespace
{
size_t g_checks = 0;
void Require(bool value, const std::string& reason)
{
    ++g_checks;
    if (!value) throw std::runtime_error(reason);
}
class OrientedMobility : public ConstantVelocityMobilityModel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("bas::PrefetchContractMobility").SetParent<ConstantVelocityMobilityModel>()
            .AddConstructor<OrientedMobility>()
            .AddAttribute("Orientation", "Measured ENU yaw/pitch/roll radians", VectorValue(),
                          MakeVectorAccessor(&OrientedMobility::orientation), MakeVectorChecker());
        return tid;
    }
    Vector orientation;
};
struct Cohort
{
    std::vector<Ptr<OrientedMobility>> mobility;
    std::vector<Ptr<UniformPlanarArray>> arrays;
    std::vector<Cache::Endpoint> endpoints;
    Ptr<Cache> cache;
    uint64_t request{0};
    Cohort()
    {
        for (size_t i = 0; i < 6; ++i)
        {
            auto node = CreateObject<Node>();
            auto pose = CreateObject<OrientedMobility>();
            pose->SetPosition(Vector(10 * i, 0, 20)); node->AggregateObject(pose);
            auto array = CreateObject<UniformPlanarArray>();
            array->SetNumRows(1); array->SetNumColumns(1);
            array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1); weights[0] = {1, 0};
            array->SetBeamformingVector(weights);
            mobility.push_back(pose); arrays.push_back(array); endpoints.push_back({pose,array});
        }
        cache = CreateObject<Cache>(2412000000.0, endpoints, Cache::Limits{});
    }
    Cache::Record Publish(size_t a, size_t b)
    {
        Cache::Record record;
        record.direction = {a,b}; record.sourceRequestId = ++request;
        record.sampleNativeNs = Simulator::Now().GetNanoSeconds();
        record.sampleHostMonotonicNs = Cache::HostNowNs();
        for (size_t side = 0; side < 2; ++side)
        {
            const auto i = record.direction[side];
            record.poses[side] = {mobility[i]->GetPosition(), mobility[i]->GetVelocity(), mobility[i]->orientation,
                                 record.sampleHostMonotonicNs};
        }
        auto matrix = Create<Cache::ChannelMatrix>();
        matrix->m_generatedTime = NanoSeconds(record.sampleNativeNs);
        matrix->m_channel = Cache::Complex3DVector(1,1,1); matrix->m_channel(0,0,0) = {1,0};
        auto params = Create<SionnaRtChannelModel::SionnaRtChannelParams>();
        params->m_generatedTime = matrix->m_generatedTime;
        params->m_delay = {0}; params->m_doppler = {0}; params->m_angle.assign(4,std::vector<double>(1,0));
        record.matrix = matrix; record.parameters = params;
        Require(cache->Publish(record).accepted, "CPU fixture CIR publication failed");
        return record;
    }
    void Close() { cache->Dispose(); Simulator::Destroy(); }
};
void Advance(Time time)
{
    Simulator::Schedule(time, [] {}); Simulator::Run();
}
void Refuses(Cohort& cohort, size_t a, size_t b, const std::string& expected)
{
    bool refused = false;
    try { cohort.cache->GetChannel(cohort.mobility[a], cohort.mobility[b], cohort.arrays[a], cohort.arrays[b]); }
    catch (const std::runtime_error& error) { refused = std::string(error.what()).find(expected) != std::string::npos; }
    Require(refused, "Prefetch altered the hard cache guard: " + expected);
}
void RotationBeforeHalf()
{
    Cohort cohort;
    const auto record = cohort.Publish(2,5);
    Advance(MilliSeconds(75)); // Flight02:109.945 ->110.020, before its half-angle trigger.
    cohort.mobility[2]->orientation.x = 1.833438040460218 * M_PI / 180;
    const auto state = cohort.cache->PairState(2,5);
    Require(state.valid && !state.due && state.normalizedRisk < .5, "Rotation case already crossed the old half warning");
    Policy policy;
    policy.ObserveResponse(100000000,false); // Observed ~40ms source/receipt overhead plus 60ms RT.
    const auto forecast = Policy::Predict(state,cohort.cache->GetLimits(),policy.LeadNs());
    Require(forecast.eligible && forecast.limitingGuard == "orientation" &&
            forecast.remainingNs > 60000000 && forecast.remainingNs < 70000000,
            "Flight02 rotation was not prefetched before its 40ms source-pose jump");
    const auto selected = Policy::Choose(Policy::Rank({state},cohort.cache->GetLimits(),policy.LeadNs()),{});
    Require(selected.has_value(), "A valid pair below the half threshold was never scheduled");
    const auto actual = cohort.cache->GetChannel(cohort.mobility[2],cohort.mobility[5],cohort.arrays[2],cohort.arrays[5]);
    Require(actual->m_generatedTime.GetNanoSeconds() == record.sampleNativeNs,
            "Scheduling changed the CIR generation epoch");
    cohort.mobility[2]->orientation.x = 6.227565748971877 * M_PI / 180;
    Require(!cohort.cache->PairState(2,5).valid, "Expired orientation became valid through a forecast");
    Refuses(cohort,2,5,"endpoint_orientation_changed");
    cohort.Close();
}
void VelocityAndSameEpoch()
{
    Cohort cohort;
    cohort.Publish(0,1);
    Advance(MilliSeconds(100));
    cohort.mobility[0]->SetVelocity(Vector(.2,0,0));
    auto state=cohort.cache->PairState(0,1);
    auto forecast=Policy::Predict(state,cohort.cache->GetLimits(),100000000);
    Require(state.valid && !state.due && forecast.eligible && forecast.limitingGuard=="velocity" &&
            forecast.remainingNs>=74000000 && forecast.remainingNs<=75000000,
            "Velocity growth did not trigger before the half guard");
    cohort.mobility[0]->SetVelocity(Vector(.5,0,0));
    Refuses(cohort,0,1,"endpoint_velocity_changed");
    cohort.mobility[0]->SetVelocity(Vector());
    cohort.Publish(0,1);
    cohort.mobility[0]->orientation.x=.001;
    state=cohort.cache->PairState(0,1);
    forecast=Policy::Predict(state,cohort.cache->GetLimits(),100000000);
    Require(state.valid && forecast.eligible && forecast.remainingNs==0,
            "A same-epoch measured delta produced an infinite/undefined refresh deadline");
    cohort.Close();
}
void MovingEndpointBeforeStaticMajority()
{
    Cohort cohort;
    for (size_t a=0;a<6;++a) for(size_t b=a+1;b<6;++b) cohort.Publish(a,b);
    Advance(MilliSeconds(100));
    for(size_t peer=0;peer<6;++peer) if(peer!=3) cohort.Publish(3,peer);
    Advance(MilliSeconds(160));
    cohort.mobility[3]->SetPosition(cohort.mobility[3]->GetPosition()+Vector(.24,0,0));
    const auto moving = cohort.cache->PairState(0,3), stationary = cohort.cache->PairState(0,1);
    Require(moving.valid && !moving.due && stationary.valid && stationary.due,
            "Moving-vs-static fixture does not distinguish forecast from half-age scheduling");
    Require(moving.normalizedRisk < stationary.normalizedRisk, "Static age-majority no longer reproduces the old priority");
    const auto ranked = Policy::Rank(cohort.cache->Priorities(),cohort.cache->GetLimits(),100000000);
    const auto choice = Policy::Choose(ranked,{});
    Require(choice && choice->anchor==3 && choice->coveredPairs==5 &&
            choice->urgent.limitingGuard=="displacement" && choice->urgent.remainingNs<100000000,
            "Static age-majority displaced the moving endpoint's earliest deadlines");
    std::set<Policy::Pair> covered;
    for(size_t peer=0;peer<6;++peer) if(peer!=3) covered.insert({std::min(peer,size_t(3)),std::max(peer,size_t(3))});
    const auto next = Policy::Choose(ranked,covered);
    Require(next && next->anchor!=3, "An in-flight anchor was dispatched again for its covered links");
    for(size_t a=0;a<6;++a) for(size_t b=a+1;b<6;++b) covered.insert({a,b});
    Require(!Policy::Choose(ranked,covered), "The existing covered graph did not suppress duplicate jobs");
    cohort.mobility[3]->SetPosition(cohort.mobility[3]->GetPosition()+Vector(.260348,0,0));
    Refuses(cohort,0,3,"endpoint_displaced"); // Flight03's measured 0.500348m remains inadmissible.
    cohort.Close();
}
void ResponseWindowAndClockBudgets()
{
    Policy policy;
    policy.ObserveResponse(2000000000,true);
    Require(policy.WindowSize()==0 && policy.LeadNs()==100000000, "Cold GPU response contaminated the live lead");
    for(size_t i=0;i<64;++i) policy.ObserveResponse(20000000,false);
    Require(policy.WindowSize()==64 && policy.LeadNs()==100000000, "Minimum measured lead was reduced");
    for(size_t i=0;i<4;++i) policy.ObserveResponse(120000000,false);
    Require(policy.WindowSize()==64 && policy.ResponseP95Ns()==120000000 && policy.LeadNs()==125000000,
            "Nearest-rank hot-response p95 plus poll margin was not applied");
    for(size_t i=0;i<64;++i) policy.ObserveResponse(10000000,false);
    Require(policy.WindowSize()==64 && policy.LeadNs()==100000000, "Old response ages were retained without bound");
    Cohort cohort;
    cohort.Publish(0,1);
    Advance(MilliSeconds(410));
    const auto forecast = Policy::Predict(cohort.cache->PairState(0,1),cohort.cache->GetLimits(),policy.LeadNs());
    Require(forecast.eligible && forecast.remainingNs==90000000 && forecast.limitingGuard=="native_age",
            "Age deadline was forecast with an incorrect growth factor");
    Advance(MilliSeconds(91));
    Refuses(cohort,0,1,"native_sample_expired");
    cohort.Close();
}
} // namespace
int main(int argc,char** argv)
{
    if(argc!=2 || std::filesystem::exists(argv[1])) throw std::runtime_error("Expected a new result.json path");
    std::string error;
    try { RotationBeforeHalf(); MovingEndpointBeforeStaticMajority(); VelocityAndSameEpoch(); ResponseWindowAndClockBudgets(); }
    catch(const std::exception& failure) { error=failure.what(); Simulator::Destroy(); }
    // Keep the standalone CPU result readable without starting Python.
    std::ofstream report(argv[1]);
    report << "{\"schema\":1,\"passed\":" << (error.empty()?"true":"false") << ",\"checks\":" << g_checks
           << ",\"scope\":\"CPU production prefetch and hard CIR-cache guards; no RT, PHY or hardware acceptance\""
           << ",\"error\":\"";
    for(char c:error) { if(c=='\\' || c=='\"') report << '\\'; if(c=='\n') report << "\\n"; else report << c; }
    report << "\"}\n";
    if(!error.empty()) std::cerr << error << '\n';
    return error.empty()?0:1;
}
