# Network/Radio Integration Workspace

This directory is the durable workspace for the packet-in-the-loop network and
radio integration. `doc/network_radio_integration_plan_v3.md` is the
authoritative execution and acceptance contract; the original plan is retained
as historical design context.

Important files:

- `PROGRESS.md` records what has been done and what is blocked.
- `DECISIONS.md` records architectural decisions and sources.
- `VALIDATION_REPORT.md` records P0/P1/P2 gate status and proof.
- `NEXT_TASK.md` records the exact next action for a resumed or new agent.
- `config/scenario_5uav.yaml` is the five-UAV scenario file. It remains
  compatible with the existing `multiagent_simulation.launch.py`
  `robots_config_file` argument and adds explicit system IDs plus radio
  metadata.
- `config/endpoints.yaml` maps command-post, UAV, SITL, and planned bridge
  endpoints. Direct SITL/GCS localhost ports listed under `no_bypass` are
  forbidden from the accepted packet-in-the-loop path.
- `config/service_tiers.yaml` defines the Day 1 service-tier and traffic-class
  policy consumed by later bridge/ns-3/Sionna work.
- `config/radio_backend.yaml` defines the switchable radio backend contract:
  `sim_2_4ghz` is the current modeled-radio acceptance backend and
  `real_modem_2_4ghz` is a future physical-modem backend.
- `radio_provider/` and `position_tracker/` provide the Sionna RT 2.4 GHz
  backend and node-state feed.
- `ns3/` provides the packet-core topology generator, ns-3 scratch program,
  build wrapper, runtime wrapper, and content-addressed build receipt.
- `bridge/` provides MAVLink endpoint config rendering, priority queues, and
  traffic-generation helpers.
- `hitl/` provides virtual serial/Ethernet loopback and timing correlation for
  future physical modem readiness without requiring hardware now.
- `scripts/check_deps.sh` reports required external runtime dependencies with
  actionable diagnostics.
- `scripts/run_network_demo.sh` is the intended full-loop entry point. It
  refuses to launch a partial demo when dependencies or packet-path components
  are missing.
- `scripts/run_validation.sh` and `scripts/collect_artifacts.sh` provide the
  validation/handoff scaffold.
- `../scripts/run_acceptance_container.sh` launches a retained container by
  immutable image ID and records its full identity for host-side inspection.
- `scripts/attest_run_evidence.py` creates the detached Ed25519 signature only
  after a sealed run container has stopped. Its private key and one-time ledger
  must remain outside this repository and outside the run container.
- `tests/check_no_bypass.sh` is the first no-bypass smoke check. It does not
  replace the final active namespace/TAP/ns-3 stopped P0 proof.
- `swarm/` contains VS Code/CLI friendly multi-agent orchestration scripts.

Agents must treat these files as persistent memory. Conversation history is not
authoritative after context compaction, restart, or handoff.

Day 1 commands:

```bash
./network/scripts/check_deps.sh
./network/scripts/run_network_demo.sh
./network/tests/check_no_bypass.sh
./network/scripts/clean_runtime.sh
```

Integrated smoke commands:

```bash
python3 network/tests/check_sionna_provider.py
./network/tests/check_ns3_packet_core_config.sh
python3 network/bridge/bridge_config.py --check
python3 network/bridge/priority_udp_bridge.py --self-test
./network/tests/test_hitl_loopback.sh
RUN_ID=integrated_validation_smoke ./network/scripts/run_validation.sh
```

Diagnostic `sim_2_4ghz` runtime command:

```bash
AMS_RADIO_BACKEND=sim_2_4ghz ./network/scripts/run_sim_2_4ghz_loop.sh
RUN_ID=<run_id> ./network/scripts/run_validation.sh --run-dir runs/<run_id>
./network/scripts/collect_artifacts.sh --force runs/<run_id>
```

There is currently no accepted P0 run. `real_packet_loop_20260702T113341Z` is a
negative validation regression fixture and must fail the v3 validator.

Formal v3 acceptance is currently at M0 requalification. The exact pinned image,
runtime manifests, dependency lock, signing key, and ledger are already
provisioned; `NEXT_TASK.md` requires a clean v3 source run against that exact
image. Minimal M0 and component-only M1 are independently validated but do not
claim or require an integrated P0 seal/attestation. The final M7/M8 profiles must
use the profile-specific sealing, host signature, ledger, recursive validation,
and handoff chain defined by v3. An interactive `latest` container or diagnostic
run cannot substitute for any formal profile.

Live SINR commands:

When Gazebo/SITL is already running in one terminal, open the second terminal
with `./scripts/enter_container.sh`. Do not run `./scripts/run_container.sh`
again for live ROS consumers, because that starts a second container; it may
discover ROS topics but miss the actual odometry/PointCloud2 samples in this
runtime setup.

```bash
# From ~/bas0.1 on the host, with the simulation container already running:
# launches the live dashboard, moves uav2 through the rock-shadow line, and
# writes metrics/live_sinr.csv from real ROS odometry.
./scripts/run_live_rock_flight_demo.sh --duration 72 --rate-hz 2

# Dashboard:
# http://127.0.0.1:8765/

# Development smoke: no ROS/Gazebo required, test-only channel model.
RUN_ID=live_sinr_smoke ./network/scripts/run_live_sinr_demo.sh \
  --test-free-space --source replay --duration 10 --rate-hz 2

# Real Sionna RT plus ns-3, using deterministic replay positions.
RUN_ID=live_sinr_real ./network/scripts/run_live_sinr_demo.sh \
  --source replay --duration 20 --rate-hz 1

# Real Sionna RT plus ns-3 with a custom Mitsuba XML scene selected through
# the radio YAML's sionna.scene.path. The same radio config is passed to the
# provider, live monitor, and generated ns-3 topology.
RUN_ID=live_sinr_custom RADIO_FILE=/path/to/custom_radio.yaml \
  ./network/scripts/run_live_sinr_demo.sh --source replay --duration 65 --rate-hz 1

# Equivalent explicit flag form.
RUN_ID=live_sinr_custom ./network/scripts/run_live_sinr_demo.sh \
  --radio-config /path/to/custom_radio.yaml --source replay --duration 65 --rate-hz 1

# Matched Gazebo/Sionna rock-shadow proof: uav2 moves behind the shared
# obstacle mesh and the live SINR graph drops when LoS is blocked.
RUN_ID=rock_scene_live_ns3 ./network/scripts/run_live_sinr_demo.sh \
  --source replay --duration 65 --rate-hz 2 \
  --scenario network/config/scenario_rock_demo.yaml \
  --jammers-config network/config/jammers_rock_demo.yaml \
  --radio-config network/config/radio_24ghz_rock_demo.yaml \
  --tx uav1 --rx uav2 \
  --replay-moving-node uav2 \
  --replay-amplitude-m 1200 \
  --replay-period-s 120

# Hand-flown ArduPilot/Gazebo mode: start the two UAVs first, then run this.
RUN_ID=live_sinr_ros ./network/scripts/run_live_sinr_demo.sh \
  --source ros --tx uav1 --rx uav2 --duration 120 --rate-hz 1 \
  --ros-node-state-timeout 60
```

For custom Mitsuba XML, point the selected radio YAML at the scene:

```yaml
sionna:
  scene:
    id: customer_scene_v1
    source: mitsuba_xml
    path: /path/to/scene.xml
```

Live outputs are written to `runs/<run_id>/metrics/live_sinr.csv`,
`runs/<run_id>/plots/live_sinr.png`, `runs/<run_id>/logs/live_sinr_queries.jsonl`,
and, when ns-3 is enabled, `runs/<run_id>/metrics/ns3_link_states.csv` plus
`runs/<run_id>/metrics/ns3_flow_rates.csv`.
