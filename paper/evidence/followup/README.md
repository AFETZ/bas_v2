# Follow-up evidence, 27 October 2026

Runtime freeze: `e6453c8e2a1ed4f6e4f9b16c224104e62e2df5d1`. Main30 remains a separate cohort at `8e16ec2`. This compact Git bundle contains the frozen plan/settings, terminal controller journal, all18 independent small-raw/MAVLink audits, final analysis and failed-run account. Full raw, assets, source tar and images remain locally in `runs/`; they are not reconstructed from summaries. Native PCAP was independently reviewed for the earlier first planned main pair; no new all18 PCAP audit is claimed.

18 attempted, 15 completed, three failed **after complete windows**. All18 network outcomes remain in the analysis; parameter coverage and lifecycle success remain separate. `all18-independent.json` deliberately has `complete18_cohort=false`: it records 18 terminal attempts, not 18 successful full runs. No failed run was retried. `all18-upstream-parameters.json` retains the original audit output. Its three partial CSV comparisons used `str(None)` against empty cells for genuinely unindexed values; the explicit normalized review is in `../../data/sensitivity_independent_summary.json`. Partial remains partial.

From repository root, with the prepared native image/assets and a **fresh** run ID:

```bash
( source paper/evidence/followup/reproduce-one.env
  BAS_NATIVE_FIVE_RUN_ID=user-selective10-01 flock -n -E 75 /tmp/bas-paper-runtime.lock ./network/ns3/run_native_radio_five_uav.sh
)
```

`reproduce-one.env` is the measured ten-second failover profile, repeat3. `SKIP_BUILD=1` assumes the already prepared identical native binary; build with the documented environment first on another host. To reproduce the whole study, use `scripts/product/relay_sensitivity.py` with the frozen plan/source in a fresh output location. Its resume mode skips existing attempted IDs, including failed; it does not repeat them. Do not reuse the saved raw IDs to overwrite results. Freeze validation rejects a changed runtime source.

Read-only reanalysis of existing raw (pinned image, one heavy operation at a time):

```bash
flock -n -E 75 /tmp/bas-paper-runtime.lock docker run --rm --network none \
  --user "$(id -u):$(id -g)" --group-add 1000 \
  -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
  -e MPLCONFIGDIR=/tmp/bas-paper-mpl -e MPLBACKEND=Agg \
  -v "$PWD:$PWD" -w "$PWD" \
  sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5 \
  python3 scripts/product/relay_sensitivity.py --analyze \
    --plan network/config/relay_sensitivity_18.csv --raw-runs-root runs/native-radio-realtime \
    --output-dir runs/paper-followup-20261027/sensitivity/analysis
```

Use the same image/lock wrapper for `python3 paper/tools/research/SENSITIVITY_analysis_figures.py --analysis runs/paper-followup-20261027/sensitivity/analysis --output runs/paper-followup-20261027/sensitivity/figures`. The companion consumes existing outcomes/intervals; it does not resimulate or pool cohorts. Independent upstream audit scripts here require the corresponding raw and pinned pymavlink, and run sequentially. `summarize_followup.py` explicitly joins their results with the final analysis.

For manual flight, use the subshell/profile in [the report](../../../doc/LIVE_LEADER_FOLLOWUP_RESULTS.md), then `make gcs` in another terminal. Select `vehicle 3`, `set link 3` (MAVProxy link setting is 1-based), request `long SET_MESSAGE_INTERVAL 24 200000` and `long SET_MESSAGE_INTERVAL 33 200000`, and wait for normal EKF position readiness before `mode GUIDED`, `arm throttle`, `takeoff 16`. A `guided LAT LON 20` target requires the actual vehicle latitude/longitude. `mode BRAKE`, then `mode LAND`; wait for ground/disarm before owned `docker stop --timeout 120 bas-v2-native-radio-five-uav`. Operator timeout does not land.

Full parameter reads require closing MAVProxy while the operator GCS service remains alive. Run inside the existing namespace, using a fresh raw output location:

```bash
docker exec bas-v2-native-radio-five-uav ip netns exec ams-gcs \
  env PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages python3 \
  /workspace/multiagent_simulation/scripts/product/capture_autopilot_params.py \
  --run-id user-operator-01 --workers 5 \
  --output-dir /workspace/multiagent_simulation/runs/native-radio-realtime/user-operator-01/configuration/parameters-before
```

After LAND/disarm, read into `parameters-after` with `--compare-before` pointing at the first directory. Do this outside the measurement window. No `PARAM_SET`, separate autopilot path or force arming is used. A timeout leaves partial evidence and nonzero status. Repair is bounded; the successful measured full reads did not require missing-index requests.

`paper/tools/research/run_*gui_probe.py` are the historical bounded engineering controllers with fixed IDs/paths, not general launchers; preserve them for inspection. The GUI analysis companion has only portable repository-root discovery changed in its Git copy; metric calculations are unchanged. Original executed files are retained locally. Grounded GUI07 is separate from the 18 sensitivity attempts and from the headless operator flight.

Build the manuscript: `flock -n -E 75 /tmp/bas-paper-runtime.lock bash paper/tools/render_document.sh`, then `python3 paper/tools/check_manuscript.py --final`. Document dependencies are isolated in `bas-paper-doc-tools:20261027`; see `../../README.md`. Only macro-free anonymous DOCX/PDF are the review artifacts. No conference submission was performed.
