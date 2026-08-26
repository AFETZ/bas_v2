# Worker Scope: Foundation

Implement Day 1 from the execution plan.

Primary files/directories:

- `network/config/scenario_5uav.yaml`
- `network/config/endpoints.yaml`
- `network/config/service_tiers.yaml`
- `network/scripts/check_deps.sh`
- `network/scripts/run_network_demo.sh`
- `network/scripts/clean_runtime.sh`
- first no-bypass isolation test or design

Avoid deep implementation of Sionna, ns-3, MAVLink bridge, or HitL unless a
thin interface stub is required for diagnostics.

Done when:

- Five UAV names and system IDs are defined.
- The intended full-loop command exists and fails with clear diagnostics when
  dependencies are missing.
- A first no-bypass check exists or is precisely documented as blocked.
