# Minimal Test Matrix

Use `make test-changed` as the default selector. Run a listed runtime check only
when the corresponding component changed and its dependencies are available.
Never substitute the full repository suite for these targeted checks.

| Changed area | Minimum checks |
| --- | --- |
| Markdown | Resolve local Markdown links; `git diff --check` |
| TOML | Parse changed TOML with `tomllib`/`tomli`; `git diff --check` |
| JSON | Parse changed JSON with Python `json`; `git diff --check` |
| YAML | Parse changed YAML with `yaml.safe_load`; `git diff --check` |
| Shell | `bash -n` on changed shell files; run the script's self-test if it has one |
| Python | `python3 -m compileall -q` on changed Python files; nearest focused unit test |
| Five-UAV launch or scenario | Python compile check; `network/tests/check_five_uav_micro_ros_ports.sh` |
| Position tracker | Python compile check; `tracker.py --from-config-once` |
| Sionna provider | Python compile check; `network/tests/check_sionna_provider.py` |
| ns-3 config/glue | Parse config; `network/tests/check_ns3_packet_core_config.sh`; no rebuild unless ns-3 source inputs changed |
| Bridge | Python compile check; `priority_udp_bridge.py --self-test` |
| HitL loopback | Shell/Python syntax checks; `network/tests/test_hitl_loopback.sh` |
| Docker/dependency inputs | Relevant syntax checks, then image rebuild only when Dockerfile, lock files, or system dependencies changed |

Simulation startup, Sionna RT, ns-3, and HitL runtime checks are not implied by
documentation-only changes.
