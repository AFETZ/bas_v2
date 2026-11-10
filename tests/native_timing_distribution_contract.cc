// CPU-only analytic histogram boundaries and sampled exceedance spans.
#include "native-timing-distribution.h"
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>

int main()
{
    try {
        bas::TimingDistribution known,invalid;
        static_assert(sizeof(bas::TimingDistribution)<=512,"Timing diagnostics must remain bounded");
        // Every exact bin edge and every positive interval above one edge.
        for(size_t i=0;i<bas::TimingDistribution::UpperNs.size();++i)
            known.Observe(bas::TimingDistribution::UpperNs[i],i*10000000,100000000);
        known.Observe(1000000001,150000000,100000000);
        // Analytic 100ms boundary: equality passes; two exceeding observations
        // span10ms, recovery closes it; terminal exceedance is right-censored.
        bas::TimingDistribution span;
        const std::vector<int64_t> values{0,100000000,100000001,150000000,90000000,101000000};
        for(size_t i=0;i<values.size();++i) span.Observe(values[i],i*10000000,100000000);
        invalid.Observe(110000000,10000000,100000000);
        invalid.Observe(-1,15000000,100000000);
        invalid.Observe(110000000,20000000,100000000);
        invalid.Observe(110000000,10000000,100000000); // Clock reset must not underflow a span.
        invalid.Observe(110000000,30000000,200000000); // Changed criterion cannot bridge spans.
        invalid.Observe(1,-1,100000000);
        invalid.Observe(1,0,0);
        bas::TimingDistribution empty;
        std::cout << "{\"known_bins\":"; known.Write(std::cout);
        std::cout << ",\"spans\":"; span.Write(std::cout);
        std::cout << ",\"invalid_and_clock_reset\":"; invalid.Write(std::cout);
        std::cout << ",\"empty\":"; empty.Write(std::cout);
        std::cout << "}\n";
        return 0;
    } catch(const std::exception& error) { std::cerr << error.what() << '\n';return 1; }
}
