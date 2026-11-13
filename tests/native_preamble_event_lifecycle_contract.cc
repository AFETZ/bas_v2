// EventId ownership boundaries for the expired-preamble retention fix.
// Compile with preamble-event-prune.inc extracted verbatim from the patched
// PhyEntity::StartPreambleDetectionPeriod; do not substitute a model of EventId.
#include "ns3/core-module.h"

#include <algorithm>
#include <array>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace ns3;

namespace
{
void Require(bool condition, const char* message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

struct Ownership
{
    std::array<bool, 7> alive{};
    std::array<int64_t, 7> destroyedAt{};
};

struct Owner : SimpleRefCount<Owner>
{
    Owner(Ownership& state, unsigned index) : state(state), index(index)
    {
        state.alive[index] = true;
        state.destroyedAt[index] = -1;
    }
    ~Owner()
    {
        state.alive[index] = false;
        state.destroyedAt[index] = Simulator::Now().GetNanoSeconds();
    }
    Ownership& state;
    unsigned index;
};

struct Contract
{
    explicit Contract(bool enabled) : enabled(enabled) {}

    void Prune()
    {
        if (enabled)
        {
#include "preamble-event-prune.inc"
        }
    }

    void Record(const std::string& name)
    {
        trace.push_back(std::to_string(Simulator::Now().GetNanoSeconds()) + ":" + name);
    }

    const EventId& Find(uint32_t uid) const
    {
        const auto it = std::find_if(m_endPreambleDetectionEvents.begin(),
                                    m_endPreambleDetectionEvents.end(),
                                    [uid](const auto& id) { return id.GetUid() == uid; });
        Require(it != m_endPreambleDetectionEvents.end(), "pending EventId was lost");
        return *it;
    }

    void Add(unsigned index, Time delay, const std::string& name)
    {
        auto owner = Create<Owner>(ownership, index);
        m_endPreambleDetectionEvents.push_back(Simulator::Schedule(delay, [this, owner, name]() {
            Require(ownership.alive[owner->index], "callback lost argument ownership");
            Record(name);
        }));
    }

    void Start()
    {
        Add(0, MilliSeconds(1), "expired-fired");
        Add(1, MilliSeconds(4), "cancelled-MUST-NOT-FIRE");
        m_endPreambleDetectionEvents.back().Cancel();
        Require(m_endPreambleDetectionEvents.back().IsExpired(), "cancelled ID is not expired");
        Add(2, MilliSeconds(3), "pending-fired");

        auto owner = Create<Owner>(ownership, 3);
        m_endPreambleDetectionEvents.push_back(Simulator::Schedule(MilliSeconds(2), [this, owner]() {
            Require(Find(currentUid).IsExpired(), "executing ID must be expired");
            Require(Find(higherUid).IsPending(), "same-time higher UID must remain pending");
            Require(Find(higherUid).GetTs() == Find(currentUid).GetTs(), "timestamps differ");
            Require(higherUid > currentUid, "UID ordering differs");
            Require(ownership.alive[0], "old callback owner was not retained before prune");
            Prune();
            Require(ownership.alive[owner->index], "prune destroyed active callback owner");
            Require(ownership.alive[1], "cancelled queued event lost scheduler ownership");
            Require(Find(higherUid).IsPending(), "prune lost same-time pending event");
            Require(Simulator::GetDelayLeft(Find(higherUid)) == Time(0), "same-time delay changed");
            Require(m_endPreambleDetectionEvents.size() == (enabled ? 2u : 5u),
                    "unexpected retained set after interleaved prune");
            Require(ownership.alive[0] == !enabled, "expired owner release boundary differs");
            Record("active-callback-exit");
        }));
        currentUid = m_endPreambleDetectionEvents.back().GetUid();
        Add(4, MilliSeconds(2), "same-time-higher-fired");
        higherUid = m_endPreambleDetectionEvents.back().GetUid();

        Simulator::Schedule(MilliSeconds(5), [this]() {
            Require(ownership.alive[3] == !enabled, "active callback lifetime did not close");
            Require(ownership.alive[1] == !enabled, "cancelled callback owner boundary differs");
            Prune();
            Require(m_endPreambleDetectionEvents.size() == (enabled ? 0u : 5u),
                    "executed IDs were not pruned exactly");
            Add(5, MilliSeconds(1), "success-cancelled-a-MUST-NOT-FIRE");
            Add(6, MilliSeconds(1), "success-cancelled-b-MUST-NOT-FIRE");
            // Same operation used after a successful PHY preamble detection.
            for (auto& id : m_endPreambleDetectionEvents)
            {
                id.Cancel();
            }
            m_endPreambleDetectionEvents.clear();
            Record("successful-detection-cancels-pending");
        });
        Simulator::Schedule(MilliSeconds(7), [this]() {
            for (bool alive : ownership.alive)
            {
                Require(!alive, "callback owner leaked after scheduler/ID release");
            }
            Require(ownership.destroyedAt[0] == (enabled ? 2000000 : 5000000),
                    "expired owner destruction time differs");
            Require(ownership.destroyedAt[3] == (enabled ? 2000000 : 5000000),
                    "current callback destruction time differs");
            Require(ownership.destroyedAt[1] == (enabled ? 4000000 : 5000000),
                    "cancelled event destruction time differs");
            Require(ownership.destroyedAt[5] == 6000000 && ownership.destroyedAt[6] == 6000000,
                    "success-cancelled scheduler ownership differs");
            Record("all-owners-released");
        });
    }

    bool enabled;
    Ownership ownership;
    std::vector<EventId> m_endPreambleDetectionEvents;
    std::vector<std::string> trace;
    uint32_t currentUid = 0;
    uint32_t higherUid = 0;
};

std::vector<std::string> Run(const std::string& implementation, bool prune)
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue(implementation));
    Contract contract(prune);
    contract.Start();
    // RealtimeSimulatorImpl waits for externally injected events when its
    // scheduler becomes empty, so end each isolated case explicitly.
    Simulator::Stop(MilliSeconds(8));
    Simulator::Run();
    Simulator::Destroy();
    return contract.trace;
}
} // namespace

int main(int argc, char** argv)
{
    try
    {
        Require(argc == 2, "expected result.json path");
        const std::vector<std::string> expected{
            "1000000:expired-fired", "2000000:active-callback-exit",
            "2000000:same-time-higher-fired", "3000000:pending-fired",
            "5000000:successful-detection-cancels-pending", "7000000:all-owners-released"};
        for (const auto& implementation : {"ns3::DefaultSimulatorImpl", "ns3::RealtimeSimulatorImpl"})
        {
            const auto baseline = Run(implementation, false);
            const auto patched = Run(implementation, true);
            Require(baseline == expected, "baseline callback trace differs from expected contract");
            Require(patched == baseline, "pruning changed callback times, order or cancellation");
        }
        std::ofstream result(argv[1]);
        Require(bool(result), "cannot open result file");
        result << "{\"status\":\"passed\",\"simulator_implementations\":[\"DefaultSimulatorImpl\","
               << "\"RealtimeSimulatorImpl\"],\"scenarios\":4,\"callback_trace_equal\":true,"
               << "\"same_time_future_higher_uid_preserved\":true,\"active_callback_lifetime_preserved\":true,"
               << "\"expired_owner_released\":true,\"cancelled_scheduler_ownership_preserved\":true,"
               << "\"successful_detection_cancellation_preserved\":true,\"all_owners_released\":true,"
               << "\"pending_callback_times_and_order_preserved\":true,\"simulator_thread_only\":true}\n";
        std::cout << "PASS: four actual ns-3 lifecycle scenarios, exact callback traces\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
