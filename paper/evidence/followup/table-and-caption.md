| Nominal RF outage (s) | Policy | Actual RF min..max (s) | Complete / failed / other | Network eligible (failed included) | Timely / offered (%) | UART observed / censored | UART mean observed (s) | Physical observed / censored | Physical mean observed (s) |
|---:|:---|:---:|---:|---:|---:|:---:|---:|:---:|---:|
| 0.5 | fixed | 0.4507..0.5000 | 2 / 1 / 0 | 3 (1) | 98.7037 | 3 / 0 | 1.2609 | 3 / 0 | 18.6030 |
| 0.5 | failover | 0.4987..0.5000 | 3 / 0 / 0 | 3 (0) | 98.9259 | 3 / 0 | 1.2706 | 3 / 0 | 18.6129 |
| 2 | fixed | 1.9999..2.0001 | 2 / 1 / 0 | 3 (1) | 97.9630 | 3 / 0 | 3.6024 | 3 / 0 | 19.1675 |
| 2 | failover | 1.9999..2.0001 | 2 / 1 / 0 | 3 (1) | 98.0370 | 3 / 0 | 3.9435 | 3 / 0 | 18.6240 |
| 10 | fixed | 9.9999..10.0500 | 3 / 0 / 0 | 3 (0) | 93.1111 | 3 / 0 | 11.6167 | 3 / 0 | 20.9527 |
| 10 | failover | 9.9989..10.0001 | 3 / 0 / 0 | 3 (0) | 97.7037 | 3 / 0 | 3.9394 | 3 / 0 | 18.6145 |

All counts below use the three planned runs per cell. Capture completeness is the recorded exporter verdict; independent wire/index audit remains separate.

| Nominal outage (s) | Policy | Full measurement windows / 3 | Lifecycle successes / 3 | Pre complete / incomplete / skipped / unknown | Post complete / incomplete / skipped / unknown |
|---:|:---|---:|---:|:---:|:---:|
| 0.5 | fixed | 3 / 3 | 3 / 3 | 3 / 0 / 0 / 0 | 2 / 1 / 0 / 0 |
| 0.5 | failover | 3 / 3 | 3 / 3 | 3 / 0 / 0 / 0 | 3 / 0 / 0 / 0 |
| 2 | fixed | 3 / 3 | 2 / 3 | 3 / 0 / 0 / 0 | 2 / 0 / 1 / 0 |
| 2 | failover | 3 / 3 | 3 / 3 | 3 / 0 / 0 / 0 | 2 / 1 / 0 / 0 |
| 10 | fixed | 3 / 3 | 3 / 3 | 3 / 0 / 0 / 0 | 3 / 0 / 0 / 0 |
| 10 | failover | 3 / 3 | 3 / 3 | 3 / 0 / 0 / 0 | 3 / 0 / 0 / 0 |

Overall failed runs retained: 3 / 18. Their valid network observations are assessed by network_eligible, independently of completion.

- `paper-sensitivity-d00500ms-fixed-s1-r02`: measurement_complete=True; network_eligible=True; lifecycle_success=1.0; error=parameters RuntimeError: native radio watchdog expired or disappeared after readiness
- `paper-sensitivity-d02000ms-failover-s1-r03`: measurement_complete=True; network_eligible=True; lifecycle_success=1.0; error=parameters RuntimeError: native radio watchdog expired or disappeared after readiness
- `paper-sensitivity-d02000ms-fixed-s1-r03`: measurement_complete=True; network_eligible=True; lifecycle_success=0.0; error=cleanup RuntimeError: native radio watchdog expired or disappeared after readiness

All planned rows remain in all-18-run-points.csv and all-paired-points.csv. Conditional means do not impute censored or missing runs. Counts and mean denominators are explicit in six-cells.csv.

Outage factors are nominal planned durations. Native RF switching is observed, and polling can give unequal actual exposure within a nominal pair. Per-cell actual means/min/max and every actual failover-minus-fixed mismatch are retained in six-cells.csv and paired-actual-RF-exposure.csv; raw actual phases remain in the analysis. These three-pair comparisons include timing/phase variation and are exploratory, with no post hoc exposure adjustment or retuning.

Caption: Prespecified selective-fault sensitivity study, separate from the original 30-run experiment. Columns label nominal planned outages; actual per-run exposures and paired mismatches are reported separately. Each line joins the same whole-run random repeat under fixed and failover policies; colors identify three paired repeats per nominal duration. Upward triangles show right-censor bounds; unobserved outcomes have no plotted zero. The 4 s reference concerns command receipt/common UART; changed-relay handover is audited separately. Physical goal completion has no 4 s success threshold. No curve smoothing, packet-level replication, cross-duration pooling, or equivalence claim is used.
