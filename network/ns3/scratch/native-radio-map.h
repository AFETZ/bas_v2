#pragma once
#include "ns3/wifi-spectrum-value-helper.h"
#include "ns3/spectrum-converter.h"

namespace bas
{
inline double InBandPower(Ptr<const SpectrumValue> psd)
{
    double power = 0;
    auto value = psd->ConstValuesBegin();
    for (auto band = psd->ConstBandsBegin(); band != psd->ConstBandsEnd(); ++band, ++value)
        power += *value * std::max(0.0, std::min(band->fh, 2422e6)-std::max(band->fl, 2402e6));
    return power;
}

// Offline map of the same native received-PSD computation. These are predictions,
// never packet PDR measurements. No extra solve is inserted into the live packet run.
inline void WriteRadioMap(const std::string& output,
    Ptr<SionnaRtSpectrumPropagationLossModel> model, Ptr<SourcePropagation> sources,
    Ptr<MobilityModel> txMobility, Ptr<PhasedArrayModel> txArray, double sourceTime)
{
    auto txPsd = WifiSpectrumValueHelper::CreateHtOfdmTxPowerSpectralDensity({2412}, 20, .01, 20);
    // Configured thermal floor, exactly as InterferenceHelper::CalculateSnr in ns-3.48.
    // This is a derived map input, not an additional runtime noise measurement.
    const double noise = 1.3803e-23 * 290.0 * 20e6 * DbToRatio(7.0);
    model->SetChannelModelAttribute("UpdateDistanceThreshold", DoubleValue(.001));
    if (sources)
        for (auto& [phy, sourceModel] : sources->sources)
            sourceModel->SetChannelModelAttribute("UpdateDistanceThreshold", DoubleValue(.001));
    std::ofstream csv(output);
    csv << "x_m,y_m,z_m,source_time_s,signal_w,noise_w,jammer_w,path_count,model\n";
    csv << std::setprecision(17);
    for (int row = 0; row < 8; ++row)
        for (int column = 0; column < 8; ++column)
        {
            // Independent map samples at t=0 need independent link identities:
            // upstream long-term beamforming cache uses generated simulation time.
            auto node = CreateObject<Node>();
            auto rxMobility = CreateObject<ConstantPositionMobilityModel>();
            node->AggregateObject(rxMobility);
            auto rxArray = CreateObjectWithAttributes<UniformPlanarArray>(
                "NumColumns", UintegerValue(1), "NumRows", UintegerValue(1));
            PhasedArrayModel::ComplexVector weights(1);
            weights[0] = {1, 0};
            rxArray->SetBeamformingVector(weights);
            const Vector xyz(column*20.0, row*20.0-20.0, 2.0);
            rxMobility->SetPosition(xyz);
            auto params = Create<SpectrumSignalParameters>();
            params->psd = txPsd;
            auto received = model->CalcRxPowerSpectralDensity(params, txMobility, rxMobility, txArray, rxArray);
            double jammer = 0;
            if (sources)
                for (auto& [phy, sourceModel] : sources->sources)
                {
                    auto times = sources->sourceTimes.at(phy);
                    if (sourceTime < times[0] || sourceTime >= times[1] ||
                        std::fmod(sourceTime-times[0], times[2]) >= times[2]*times[3])
                        continue;
                    auto signal = Create<SpectrumSignalParameters>();
                    auto sourcePsd = sources->sourcePsds.at(phy);
                    SpectrumConverter converter(sourcePsd->GetSpectrumModel(), txPsd->GetSpectrumModel());
                    signal->psd = converter.Convert(sourcePsd);
                    auto antenna = phy->GetAntenna()->GetObject<PhasedArrayModel>();
                    auto propagated = sourceModel->CalcRxPowerSpectralDensity(
                        signal, phy->GetMobility(), rxMobility, antenna, rxArray);
                    jammer += InBandPower(propagated->psd);
                }
            auto paths = model->GetChannelModel()->GetParams(txMobility, rxMobility);
            csv << xyz.x << ',' << xyz.y << ',' << xyz.z << ',' << sourceTime << ','
                << InBandPower(received->psd) << ',' << noise << ',' << jammer << ','
                << (paths ? paths->m_delay.size() : 0) << ",native_sionna_received_psd\n";
        }
}
} // namespace bas
