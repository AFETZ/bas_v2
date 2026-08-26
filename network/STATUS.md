# Product Status

Updated: 2026-08-26. Runtime observations are recorded separately from pending
product behavior; neither file existence nor a validator-only result is proof.

| Stage | Status | What works now | What does not work yet | Next one task | Verification command | Last simple artifact |
| --- | --- | --- | --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules, short sources of truth, one agent profile, archive, Make targets, and changed-path tests are present. | Nothing known within P0 scope. | Start P1; do not extend process infrastructure. | `make test-changed` | `network/STATUS.md` |
| P1 Five-UAV baseline | in_progress | Five-UAV baseline runtime passed: five live SITLs, five Gazebo models, five odometry streams, unique endpoints, health JSON, and managed stop were observed. | Flight lifecycle (arm, takeoff, hold, movement, and landing) is pending. | Demonstrate the flight lifecycle without changing the proven baseline topology. | `make run-base`; in another terminal `make stop` | `runs/baseline-20260826T190042Z/metrics/health.json` |
| P2 Communication vertical slice | in_progress | One-UAV two-UART ns-3 vertical slice passed with exact MAVLink2 command/ACK and telemetry bytes, a separate data channel, contention, PCAP, and stop-break behavior. | Five-UAV UART scaling, the manual QGC path, and priority scheduling are pending. | Scale the proven dual-UART path to five UAVs after the active P3A asset-selection task. | Do not repeat `make run-network` for P3A. | `runs/communication-20260826T192436Z/metrics/communication_summary.json` |
| P3 Shared 10 km scene | in_progress | Existing CAVISE map inventory and selection are active; bounded ZIP metadata inspection and the external-asset workflow are prepared. | No official bundle was found locally, so no town or measured 10 km by 10 km ROI is selected; Gazebo derivative, alignment, and LOS/NLOS tests remain pending. | Put `CAVISE_SIONNA_Town13_EditorLOD0_Full_Official_20260731.zip` in `CAVISE_MAPS_DIR`, inspect metadata, and select only if measured criteria pass. | `scripts/product/prepare_cavise_map.sh --metadata-only` | `network/config/cavise_map_catalog.yaml` |
| P4 Interference and medium access | todo | Basic continuous jammer config, Sionna metrics, ns-3 CSMA surrogate, and heatmap tooling exist. | Full time/directional jammer model, required heatmap families, simultaneous contention, and measured control priority are incomplete. | Implement and observe one timed directional jammer through Sionna link state. | `python3 network/radio_provider/provider.py sample-request --include-jammers` (partial only) | `network/config/jammers.yaml` (partial configuration) |
| P5 HitL and real time | todo | Software serial/UDP loopback and timing-shaped artifacts exist. | Live hardware serial/Ethernet through real ns-3 and Sionna/hybrid state, watchdog behavior, and wall-clock limits are not demonstrated. | Replace one virtual timing stage with the real ns-3 path while keeping bounded queues. | `./network/tests/test_hitl_loopback.sh` (software only) | None from this task |
| P6 Scalability and hybrid propagation | todo | Architecture defines timestamped Sionna state and explicit simple-model fallback. | No benchmark matrix or measured operating envelope exists. | After P5, benchmark the five-UAV baseline at multiple Sionna update rates. | Not implemented | None |
| P7 Integrated demo | todo | Individual base, network, provider, ns-3, and HitL entrypoints exist. | No single complete script produces summary JSON, CSV, PCAP, heatmaps, log, and report. | After P6, compose the proven component commands into one lifecycle-managed demo. | Not implemented | None |

Communication metric note: contention attempted 160 packets and delivered 42,
so the contention packet loss is 118. `ns3_drop_events=119` is a global sum of
`MacTxDrop` and `PhyTxDrop` callback events across all devices and phases, not a
unique contention-packet count. Future summary work must expose packet counts
separately from event counts; the network runtime is unchanged in P3A.
