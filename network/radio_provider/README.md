# Sionna Radio Provider

This adapter implements the Day 2 TCP JSON-lines contract from
`doc/network_radio_integration_plan.md`.

Runtime command:

```bash
./network/scripts/run_sionna_provider.sh
```

The default mode is `real_sionna`. It imports `sionna.rt`, loads the configured
Sionna scene, adds runtime transmitters/receivers/jammers from each request,
and returns pathloss, RSS, SINR, J/S, service tier, PER input, link state, and
staleness.

Test-only mode:

```bash
SIONNA_PROVIDER_MODE=test_free_space ./network/scripts/run_sionna_provider.sh
```

`test_free_space` is only for unit and dependency smoke tests. It is marked in
responses with `test_only: true` and `acceptance_eligible: false`, and cannot
satisfy customer acceptance.

Each TCP request and response is logged under:

```text
runs/<run_id>/logs/sionna_link_queries.jsonl
```

Useful local commands:

```bash
python3 network/radio_provider/provider.py sample-request --include-jammers
python3 network/radio_provider/provider.py oneshot --mode real_sionna --request-file request.json
./network/scripts/generate_radio_heatmaps.sh
```
