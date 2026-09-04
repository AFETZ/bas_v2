#pragma once
#include <chrono>
namespace bas {
inline void CacheStudy(const std::string& csvPath, const std::string& scene,
                       const SionnaRtChannelModel::RtPathSolverConfig& solver)
{
    auto tx = CreateObject<ConstantPositionMobilityModel>();
    auto rx = CreateObject<ConstantPositionMobilityModel>();
    auto tn = CreateObject<Node>(); tn->AggregateObject(tx);
    auto rn = CreateObject<Node>(); rn->AggregateObject(rx);
    tx->SetPosition({5,0,2}); rx->SetPosition({80,0,17});
    auto array = [] {
        auto a = CreateObjectWithAttributes<UniformPlanarArray>("NumRows", UintegerValue(1), "NumColumns", UintegerValue(1));
        PhasedArrayModel::ComplexVector w(1); w[0]={1,0}; a->SetBeamformingVector(w); return a;
    };
    auto ta=array(), ra=array();
    std::vector<Ptr<SionnaRtSpectrumPropagationLossModel>> models;
    for (auto [ttl, distance] : std::vector<std::pair<double,double>>{{20,10},{1,1},{.1,.1}})
    {
        auto m=CreateObject<SionnaRtSpectrumPropagationLossModel>();
        m->SetChannelModelAttribute("Frequency", DoubleValue(2412e6));
        m->SetChannelModelAttribute("Scenario", StringValue(scene));
        m->SetChannelModelAttribute("IsMergeShapeEnabled", BooleanValue(true));
        m->SetChannelModelAttribute("MaxNumberOfPaths", DoubleValue(90));
        m->SetChannelModelAttribute("UpdatePeriod", TimeValue(Seconds(ttl)));
        m->SetChannelModelAttribute("UpdateDistanceThreshold", DoubleValue(distance));
        m->SetChannelModelAttribute("UpdateJitterFraction", DoubleValue(.5));
        m->SetRtPathSolverConfig(solver); models.push_back(m);
    }
    auto csv=std::make_shared<std::ofstream>(csvPath);
    *csv << "sim_time_s,x_m,y_m,z_m,profile,signal_w,path_count,channel_age_s,call_ms\n" << std::setprecision(17);
    auto params=Create<SpectrumSignalParameters>();
    params->psd=WifiSpectrumValueHelper::CreateHtOfdmTxPowerSpectralDensity({2412},20,.01,20);
    for (int step=0;step<=55;++step)
        Simulator::Schedule(Seconds(step*.5), [=] {
            rx->SetPosition({80,step*2.0,17});
            for (unsigned i=0;i<models.size();++i)
            {
                const auto begin=std::chrono::steady_clock::now();
                auto value=models[i]->CalcRxPowerSpectralDensity(params,tx,rx,ta,ra);
                const double elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-begin).count();
                auto state=models[i]->GetChannelModel()->GetParams(tx,rx);
                *csv << Simulator::Now().GetSeconds() << ",80," << step*2 << ",17," << i << ','
                     << InBandPower(value->psd) << ',' << state->m_delay.size() << ','
                     << (Simulator::Now()-state->m_generatedTime).GetSeconds() << ',' << elapsed << '\n';
            }
        });
    Simulator::Run(); csv->close(); Simulator::Destroy();
}
}
