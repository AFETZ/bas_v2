// Fixed-memory diagnostics; no interpolation, timing policy or packet logging.
#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <ostream>

namespace bas {
class TimingDistribution {
public:
    // Mutually exclusive bins: <= first bound, then (previous, bound], overflow.
    inline static constexpr std::array<int64_t,15> UpperNs{
        0,1000000,5000000,10000000,20000000,40000000,60000000,80000000,
        90000000,95000000,100000000,125000000,250000000,500000000,1000000000};
    void Observe(int64_t valueNs, int64_t observedNs, int64_t limitNs) {
        if (valueNs<0 || observedNs<0 || limitNs<=0) { ++invalid; lastOver=false; currentSpan=0; return; }
        const auto bucket=std::lower_bound(UpperNs.begin(),UpperNs.end(),valueNs)-UpperNs.begin();
        ++bins[bucket]; ++samples; maximum=std::max(maximum,static_cast<uint64_t>(valueNs));
        const bool over=valueNs>limitNs;
        if (samples>1 && limitNs!=lastLimit) ++limitChanges;
        const bool ordered=samples==1 || observedNs>=lastAt;
        if (!ordered) ++clockRegressions;
        if (over) {
            ++overLimit;
            if (!lastOver || !ordered || limitNs!=lastLimit) { ++episodes; currentSpan=0; }
            else {
                currentSpan=SaturatingAdd(currentSpan,static_cast<uint64_t>(observedNs-lastAt));
                overSpan=SaturatingAdd(overSpan,static_cast<uint64_t>(observedNs-lastAt));
                maximumSpan=std::max(maximumSpan,currentSpan);
            }
        } else currentSpan=0;
        lastAt=observedNs; lastLimit=limitNs; lastOver=over;
    }
    void Write(std::ostream& out) const {
        out << "{\"units\":\"ns\",\"bin_policy\":\"exclusive_lower_inclusive_upper;first_includes_zero;last_overflow\","
            << "\"upper_bounds_ns\":[";
        for(size_t i=0;i<UpperNs.size();++i) { if(i) out << ','; out << UpperNs[i]; }
        out << ",null],\"counts\":[";
        for(size_t i=0;i<bins.size();++i) { if(i) out << ','; out << bins[i]; }
        out << "],\"samples\":" << samples << ",\"invalid_samples\":" << invalid
            << ",\"maximum_ns\":";
        if(samples) out << maximum; else out << "null";
        out << ",\"limit_ns\":";
        if(samples) out << lastLimit; else out << "null";
        out
            << ",\"limit_changes\":" << limitChanges << ",\"over_limit_samples\":" << overLimit
            << ",\"over_limit_episodes\":" << episodes << ",\"consecutive_over_limit_span_ns\":" << overSpan
            << ",\"maximum_consecutive_over_limit_span_ns\":" << maximumSpan
            << ",\"last_observation_over_limit\":" << (lastOver ? "true":"false")
            << ",\"observation_clock_regressions\":" << clockRegressions
            << ",\"duration_scope\":\"sum_of_intervals_between_consecutive_over_limit_observations;"
               "no_interpolation_no_unsampled_duration;terminal_episode_right_censored\"}";
    }
private:
    static uint64_t SaturatingAdd(uint64_t a,uint64_t b) {
        return b>std::numeric_limits<uint64_t>::max()-a ? std::numeric_limits<uint64_t>::max() : a+b;
    }
    std::array<uint64_t,UpperNs.size()+1> bins{};
    uint64_t samples{0},invalid{0},maximum{0},overLimit{0},episodes{0};
    uint64_t overSpan{0},currentSpan{0},maximumSpan{0},clockRegressions{0},limitChanges{0};
    int64_t lastAt{0},lastLimit{0};
    bool lastOver{false};
};
} // namespace bas
