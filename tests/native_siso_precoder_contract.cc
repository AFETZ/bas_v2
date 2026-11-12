// Differential contract for the production SISO identity-precoder specialization.
// An explicit identity matrix forces the unchanged generic matrix path.
#include <ns3/core-module.h>
#include <ns3/sionna-rt-channel-model.h>
#include <ns3/sionna-rt-spectrum-propagation-loss-model.h>
#include <ns3/spectrum-signal-parameters.h>
#include <ns3/spectrum-value.h>
#include <ns3/wifi-spectrum-value-helper.h>

#include <bit>
#include <cmath>
#include <complex>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

using namespace ns3;
using Matrix = MatrixBasedChannelModel::Complex3DVector;

class Probe : public SionnaRtSpectrumPropagationLossModel
{
  public:
    using SionnaRtSpectrumPropagationLossModel::CalcBeamformingGain;
};

bool Same(double a, double b)
{
    return std::isnan(a) ? std::isnan(b)
                         : std::bit_cast<uint64_t>(a) == std::bit_cast<uint64_t>(b);
}

void Check(bool ok)
{
    if (!ok)
    {
        throw std::runtime_error("implicit and explicit identity precoders differ");
    }
}

int main()
{
    const auto model = CreateObject<Probe>();
    model->SetChannelModelAttribute("Frequency", DoubleValue(2.412e9));
    const double tiny = std::numeric_limits<double>::denorm_min();
    const double inf = std::numeric_limits<double>::infinity();
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const std::vector<std::vector<std::complex<double>>> inputs{
        {}, {{0.0, -0.0}}, {{tiny, -tiny}}, {{0.5, -0.25}, {-0.5, 0.25}},
        {{0.7, 0.2}, {-0.1, 0.3}, {0.0, 0.0}}, {{inf, 0.0}}, {{nan, 1.0}}};
    size_t cases = 0;
    for (const auto& coefficients : inputs)
    {
        for (bool reverse : {false, true})
        {
            for (int64_t age : {0LL, 25000000LL, 99000000LL})
            {
                for (int mode = 0; mode < 5; ++mode)
                {
                    auto signal = Create<SpectrumSignalParameters>();
                    signal->psd = WifiSpectrumValueHelper::CreateHtOfdmTxPowerSpectralDensity(
                        {MHz_u{2412}}, MHz_u{20}, Watt_u{1}, MHz_u{20});
                    const auto bins = signal->psd->GetValuesN();
                    Check(bins == 193);
                    for (size_t rb = 0; rb < bins; ++rb)
                    {
                        if (mode == 1 && rb % 3) (*signal->psd)[rb] = -0.0;
                        if (mode == 2) (*signal->psd)[rb] = 0.0;
                        if (mode == 3 && rb == 96) (*signal->psd)[rb] = -1.0;
                        if (mode == 4 && rb == 96) (*signal->psd)[rb] = nan;
                    }
                    auto longTerm = Create<Matrix>(1, 1, coefficients.size());
                    for (size_t path = 0; path < coefficients.size(); ++path)
                        (*longTerm)(0, 0, path) = coefficients[path];
                    auto channel = Create<MatrixBasedChannelModel::ChannelMatrix>();
                    channel->m_channel = *longTerm;
                    channel->m_generatedTime = Simulator::Now() - NanoSeconds(age);
                    auto params = Create<SionnaRtChannelModel::SionnaRtChannelParams>();
                    params->m_generatedTime = channel->m_generatedTime;
                    params->m_angle.assign(4, std::vector<double>(coefficients.size(), 0.0));
                    for (size_t path = 0; path < coefficients.size(); ++path)
                    {
                        params->m_delay.push_back(path * 3.5e-8);
                        params->m_doppler.push_back(path * 1.7);
                    }
                    const auto calculate = [&] {
                        return model->CalcBeamformingGain(signal, longTerm, channel, params,
                                                          Vector{}, Vector{}, 1, 1, reverse);
                    };
                    auto implicit = calculate();
                    auto identity = Create<Matrix>(1, 1, bins);
                    for (size_t rb = 0; rb < bins; ++rb) (*identity)(0, 0, rb) = {1.0, 0.0};
                    signal->precodingMatrix = identity;
                    auto explicitIdentity = calculate();
                    for (size_t rb = 0; rb < bins; ++rb)
                    {
                        if (!Same((*implicit->psd)[rb], (*explicitIdentity->psd)[rb]))
                            std::cerr << "PSD case=" << cases << " bin=" << rb << " values="
                                      << (*implicit->psd)[rb] << "," << (*explicitIdentity->psd)[rb] << '\n';
                        Check(Same((*implicit->psd)[rb], (*explicitIdentity->psd)[rb]));
                        const auto a = (*implicit->spectrumChannelMatrix)(0, 0, rb);
                        const auto b = (*explicitIdentity->spectrumChannelMatrix)(0, 0, rb);
                        if (!(Same(a.real(), b.real()) && Same(a.imag(), b.imag())))
                            std::cerr << "CFR case=" << cases << " bin=" << rb << " values="
                                      << a << "," << b << '\n';
                        Check(Same(a.real(), b.real()) && Same(a.imag(), b.imag()));
                    }
                    ++cases;
                }
            }
        }
    }
    model->Dispose();
    Simulator::Destroy();
    std::cout << "PASS " << cases << " SISO identity-precoder cases, 193 bins each\n";
}
