// Public SDK boundary test. Requires the pinned Sionna 1.2.0 GPU environment.
// Compile as an ns-3 scratch target; arguments: prepared-flat-urban.xml result.json.
// The supplied scene must leave these direct LoS links unobstructed.
#include "pybind11/embed.h"
#include "pybind11/stl.h"
#include "ns3/core-module.h"
#include "ns3/mobility-module.h"
#include "ns3/antenna-module.h"
#include "ns3/sionna-rt-channel-model.h"
#include <cmath>
#include <complex>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

using namespace ns3;
namespace py = pybind11;
using namespace pybind11::literals;
constexpr double kCarrierHz = 2412000000.0;
constexpr double kSpeedOfLightMps = 299792458.0;

void Require(bool condition, const std::string& reason)
{
    if (!condition) throw std::runtime_error(reason);
}

struct Observation
{
    Ptr<const MatrixBasedChannelModel::ChannelMatrix> matrix;
    std::complex<double> coefficient;
    double delay, doppler;
    std::vector<double> angles; // AOA, ZOA, AOD, ZOD, in requested direction.
};

int main(int argc, char** argv)
{
    if (argc != 3)
    {
        std::cerr << "Usage: native_receiver_batch_contract prepared-flat-urban.xml new-result.json\n";
        return 2;
    }
    Require(std::filesystem::is_regular_file(argv[1]), "Prepared scene does not exist");
    Require(!std::filesystem::exists(argv[2]), "Result already exists; preserve every attempt");
    py::scoped_interpreter python;
    py::dict report;
    report["scope"] = "Public native SDK GetChannel/GetParams GPU regression; no flight, MAC or hardware acceptance";
    report["scene"] = std::filesystem::absolute(argv[1]).string();
    report["frequency_hz"] = kCarrierHz;
    report["normalize_delays"] = false;
    report["numerical_tolerances"] = py::dict("gain_relative"_a=1e-4,
        "delay_absolute_s"_a=1e-12, "doppler_absolute_hz"_a=1e-3,
        "angle_absolute_rad"_a=1e-4, "pairwise_complex_relative"_a=1e-3);
    report["tolerance_scope"] = "Floating point regression against analytic LoS; not a modem or customer accuracy criterion";
    py::list rows;
    try
    {
        auto rt = py::module_::import("sionna.rt");
        Require(py::str(rt.attr("__version__")).cast<std::string>() == "1.2.0", "Pinned Sionna RT 1.2.0 required");
        const auto variant = py::module_::import("mitsuba").attr("variant")().cast<std::string>();
        Require(variant.rfind("cuda", 0) == 0, "GPU regression requires the CUDA variant; no CPU fallback");
        report["sionna_rt_version"] = "1.2.0"; report["mitsuba_variant"] = variant;
        std::vector<Ptr<ConstantVelocityMobilityModel>> moving;
        std::vector<Ptr<const MobilityModel>> mobility;
        std::vector<Ptr<const PhasedArrayModel>> antennas;
        const std::vector<Vector> positions{{0, 0, 20}, {10, 0, 20}, {0, 20, 30}};
        const std::vector<Vector> velocities{{0, 0, 0}, {1, 0, 0}, {0, 2, 1}};
        for (size_t i = 0; i < positions.size(); ++i)
        {
            auto node = CreateObject<Node>();
            auto position = CreateObject<ConstantVelocityMobilityModel>();
            position->SetPosition(positions[i]); position->SetVelocity(velocities[i]);
            node->AggregateObject(position);
            moving.push_back(position); mobility.push_back(position);
            auto array = CreateObject<UniformPlanarArray>();
            array->SetAttribute("NumRows", UintegerValue(1));
            array->SetAttribute("NumColumns", UintegerValue(1));
            array->SetAntennaElement(CreateObject<IsotropicAntennaModel>());
            PhasedArrayModel::ComplexVector weights(1); weights[0] = {1.0, 0.0};
            array->SetBeamformingVector(weights); antennas.push_back(array);
        }
        auto configure = [&](bool batch) {
            auto model = CreateObject<SionnaRtChannelModel>();
            model->SetAttribute("Frequency", DoubleValue(kCarrierHz));
            model->SetAttribute("Scenario", StringValue(std::filesystem::absolute(argv[1]).string()));
            model->SetAttribute("IsMergeShapeEnabled", BooleanValue(true));
            model->SetAttribute("NormalizeDelays", BooleanValue(false));
            model->SetAttribute("SamplesPerSource", UintegerValue(20000));
            model->SetAttribute("MaxNumPathsPerSource", UintegerValue(1000));
            model->SetAttribute("MaxNumberOfPaths", DoubleValue(90));
            model->SetAttribute("UpdatePeriod", TimeValue(Seconds(0.5)));
            model->SetUpdateDistanceThreshold(0.5);
            model->SetUpdateJitterFraction(0.5);
            SionnaRtChannelModel::RtPathSolverConfig config;
            config.maxDepth = 1; config.los = true;
            config.specularReflection = config.diffuseReflection = config.refraction = false;
            config.diffraction = config.edgeDiffraction = false;
            config.syntheticArray = true; config.seed = 42;
            model->SetRtPathSolverConfig(config);
            if (batch)
            {
                model->SetReceiverBatchEndpoints(mobility, antennas);
                model->SetAttribute("ReceiverBatchEnabled", BooleanValue(true));
            }
            return model;
        };
        auto pairwise = configure(false), batch = configure(true);
        auto read = [&](Ptr<SionnaRtChannelModel> model, size_t tx, size_t rx, const std::string& label) {
            auto matrix = model->GetChannel(mobility[tx], mobility[rx], antennas[tx], antennas[rx]);
            auto parameters = DynamicCast<const SionnaRtChannelModel::SionnaRtChannelParams>(
                model->GetParams(mobility[tx], mobility[rx]));
            Require(matrix && parameters, label + ": missing public channel or parameters");
            Require(matrix->m_channel.GetNumRows() == 1 && matrix->m_channel.GetNumCols() == 1 &&
                    matrix->m_channel.GetNumPages() == 1, label + ": expected exactly one LoS path");
            Require(parameters->m_delay.size() == 1 && parameters->m_doppler.size() == 1 &&
                    parameters->m_angle.size() == 4, label + ": path parameter dimensions disagree");
            const bool reverse = matrix->IsReverse(antennas[tx]->GetId(), antennas[rx]->GetId());
            const auto s = reverse ? rx : tx, u = reverse ? tx : rx;
            Require(parameters->m_nodeIds.first == mobility[s]->GetObject<Node>()->GetId() &&
                    parameters->m_nodeIds.second == mobility[u]->GetObject<Node>()->GetId(),
                    label + ": parameter node IDs disagree with the actual channel direction");
            std::vector<double> angles;
            for (const auto index : (reverse ? std::vector<size_t>{2, 3, 0, 1} : std::vector<size_t>{0, 1, 2, 3}))
            {
                Require(parameters->m_angle[index].size() == 1, label + ": invalid angle path count");
                angles.push_back(parameters->m_angle[index][0]);
            }
            const auto delta = mobility[rx]->GetPosition() - mobility[tx]->GetPosition();
            const auto relative = mobility[tx]->GetVelocity() - mobility[rx]->GetVelocity();
            const double distance = delta.GetLength(), wavelength = kSpeedOfLightMps / kCarrierHz;
            const double expectedGain = std::pow(wavelength / (4 * M_PI * distance), 2);
            const double gain = std::norm(matrix->m_channel(0, 0, 0));
            const double expectedDoppler = (relative.x * delta.x + relative.y * delta.y + relative.z * delta.z)
                                           / distance / wavelength;
            const std::vector<double> expectedAngles{std::atan2(-delta.y, -delta.x), std::acos(-delta.z / distance),
                                                      std::atan2(delta.y, delta.x), std::acos(delta.z / distance)};
            Require(std::isfinite(gain) && std::abs(gain / expectedGain - 1) < 1e-4, label + ": LoS gain is not Friis 1/r^2");
            Require(std::abs(parameters->m_delay[0] - distance / kSpeedOfLightMps) < 1e-12,
                    label + ": delay is not the requested receiver's distance/c");
            Require(std::abs(parameters->m_doppler[0] - expectedDoppler) < 1e-3,
                    label + ": Doppler is not the requested receiver's velocity projection");
            for (size_t i = 0; i < 4; ++i)
                Require(std::abs(std::remainder(angles[i] - expectedAngles[i], 2 * M_PI)) < 1e-4,
                        label + ": angle belongs to the wrong receiver or direction");
            py::dict row;
            row["label"] = label; row["requested_tx"] = tx; row["requested_rx"] = rx;
            row["reverse_cached_direction"] = reverse; row["distance_m"] = distance;
            row["power_gain"] = gain; row["friis_gain"] = expectedGain;
            row["delay_s"] = parameters->m_delay[0]; row["doppler_hz"] = parameters->m_doppler[0];
            row["angles_rad"] = py::cast(angles); row["coefficient"] = py::cast(std::vector<double>{
                std::real(matrix->m_channel(0, 0, 0)), std::imag(matrix->m_channel(0, 0, 0))});
            rows.append(row);
            return Observation{matrix, matrix->m_channel(0, 0, 0), parameters->m_delay[0], parameters->m_doppler[0], angles};
        };
        auto compare = [](const Observation& one, const Observation& many) {
            Require(std::abs(one.coefficient - many.coefficient) / std::abs(one.coefficient) < 1e-3,
                    "Batch coefficient differs from the public pairwise solver");
            Require(std::abs(one.delay - many.delay) < 1e-12 && std::abs(one.doppler - many.doppler) < 1e-3,
                    "Batch delays/Doppler differ from public pairwise parameters");
            for (size_t i = 0; i < 4; ++i)
                Require(std::abs(std::remainder(one.angles[i] - many.angles[i], 2 * M_PI)) < 1e-4,
                        "Batch angles differ from public pairwise parameters");
        };
        const auto firstReference = read(pairwise, 0, 1, "pairwise_first_rx");
        const auto secondReference = read(pairwise, 0, 2, "pairwise_second_rx");
        const auto first = read(batch, 0, 1, "batch_first_rx");
        Require(batch->GetReceiverBatchStats().completedCalls == 1, "First public call did not solve one batch");
        const auto second = read(batch, 0, 2, "batch_second_rx_cache");
        Require(batch->GetReceiverBatchStats().completedCalls == 1, "The second receiver did not use the populated cache");
        compare(firstReference, first); compare(secondReference, second);
        auto reverse = batch->GetChannel(mobility[2], mobility[0], antennas[2], antennas[0]);
        Require(reverse == second.matrix, "Reverse direction did not reuse the reciprocal cached matrix");
        compare(read(pairwise, 1, 2, "pairwise_other_tx"), read(batch, 1, 2, "batch_other_tx"));
        const auto before = batch->GetReceiverBatchStats().completedCalls;
        moving[2]->SetPosition(Vector(3, 21, 32));
        Require(batch->ChannelMatrixNeedsUpdate(second.matrix, mobility[0], mobility[2]), "Displacement invalidation was relaxed");
        compare(read(pairwise, 0, 2, "pairwise_displaced_rx"), read(batch, 0, 2, "batch_displaced_rx"));
        Require(batch->GetReceiverBatchStats().completedCalls == before + 1, "Displacement did not create a new batch");
        compare(read(pairwise, 0, 1, "pairwise_unchanged_peer"), read(batch, 0, 1, "batch_unchanged_peer"));
        const auto observed = batch->GetReceiverBatchStats();
        Require(observed.lastReceiverCount == 2 && observed.lastAllocatedMaxPathsPerSource == 2000 &&
                observed.configuredMaxPathsPerSource == 1000 && observed.candidateCapacityReachedCalls == 0,
                "Applied batch allocation or buffer validity is incorrect");
        report["batch_stats"] = py::dict("completed_calls"_a=observed.completedCalls,
            "generated_links"_a=observed.generatedLinks, "last_receiver_count"_a=observed.lastReceiverCount,
            "base_path_cap"_a=observed.configuredMaxPathsPerSource,
            "last_allocated_path_cap"_a=observed.lastAllocatedMaxPathsPerSource,
            "candidate_capacity_reached_calls"_a=observed.candidateCapacityReachedCalls);
        report["status"] = "passed";
        pairwise->Dispose(); batch->Dispose(); Simulator::Destroy();
    }
    catch (const std::exception& error)
    {
        report["status"] = "failed"; report["error"] = error.what();
        Simulator::Destroy();
    }
    report["observations"] = rows;
    auto encoded = py::module_::import("json").attr("dumps")(report, py::arg("indent") = 2,
                                                            py::arg("allow_nan") = false).cast<std::string>();
    std::ofstream(argv[2]) << encoded << '\n'; std::cout << encoded << '\n';
    return report["status"].cast<std::string>() == "passed" ? 0 : 1;
}
