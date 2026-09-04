# Town01 bounded-overload v2

## Scope

- Product run: `runs/town01-qos-final-20260904`.
- Profiles: nominal, contention, and controlled overload in one five-SITL Town01 run.
- Comparative inputs: `overload-profile-before-20260827T193206Z` and
  `meltdown-global-20260827T214951Z`.
- Versioned outputs: `metrics/controlled_overload_summary.json`,
  `metrics/meltdown_characterization.json`, and `metrics/event_profile.json`.

## Exact scheduler-backlog cause

The old overload engine admitted unbounded offered traffic before doing expensive
ingress/Sionna/queue lifecycle work. Work following `ingress` caused 8,117.697 ms of
same-simulation-time positive lag growth across 16,485 samples. A legacy
`SetBackoffParams` fourth/fifth-argument mismatch left `maxRetries=1000000`; 414
packets then generated 45,927 rich backoff rows (up to 642 for one packet). Every
callback also reparsed and SHA-256-hashed its frame, serialized about 2 KiB JSON, and
synchronously flushed it. BSF1 fragmentation was not the cause: both measured sets
have exactly 1.0 fragment per serial record.

## Controlled overload

The six-second sources offered 33.8048 Mbit/s (18,600 packets). Per-UAV ingress token
buckets admitted 12.8608 Mbit/s (7,380 packets), and the application delivered
12.842133 Mbit/s (7,370 packets). Terminal outcomes were 7,370 delivered, 11,220
dropped at ingress, 10 dropped in medium, 0 expired, and 0 pending after the bounded
2.5 s drain. All 18,600 unique packet IDs have an explicit terminal row; the
canonical controlled-profile ledger SHA-256 is
`49211aa87e86600520aaa444d25fc04995defc3e21c0c14d0801fdc6757206d5`.

Control delivered 600/600: PDR 1.0 and latency p95 1.335 ms. Payload and additional
data delivered 6.3168 and 6.320533 Mbit/s. Their delivered Jain indices were
0.9999970 and 0.9999988; every UAV delivered traffic. Queue-delay p95 was 1.173 ms
for control, 5.343 ms for payload, and 5.335 ms for additional data. There were zero
retry/backoff events in this selected sample.

Scheduler-lag p95 was 6.125 ms; ns-3 CPU mean/p95 was 22.215/33.499% of one core.
Gazebo RTF was measured inside the profile window: mean 1.000414, p5 0.999478 over
84 samples. Live Sionna coupling and the full-run stop-based no-bypass proof remained
enabled. All controlled-overload criteria pass.

The implemented ordering is strict control priority followed by fair round-robin
among five UAVs inside payload and additional-data classes. The 20 Mbit/s product
capacity reserves at least 4 Mbit/s for control; lower-class token buckets total
13 Mbit/s, leaving 7 Mbit/s static headroom. Deadline rejection occurs before queue
and PHY event creation. Lower-priority retries are bounded at 16 and MAC retries at
64. All values live in `network/config/communication_qos.yaml`.

## Before/after

| Metric | Before | Controlled |
| --- | ---: | ---: |
| Scheduler lag p95 | 7,594.223 ms | 6.125 ms |
| ns-3 events / delivered logical packet | 12.8266 | 7.5254 |
| Pending logical packets | 15,013 | 0 |
| Retry/backoff events | 45,927 | 0 |
| ns-3 CPU mean, one core | 93.989% | 22.215% |
| Control queue-delay p95 | 180.280 ms | 1.173 ms |

## Meltdown characterization

Shaping was disabled only for characterization. At the single maximum tested point,
33.8048 Mbit/s was offered/admitted, 22.850688 Mbit/s was application-delivered on
the six-second offered-window basis (14.061962 Mbit/s over the full profile wall
interval), and measured wire rate was 14.542457 Mbit/s over that wall interval.
Control delivered 399/400 (PDR 0.9975), latency p95 2.151 ms; lower classes incurred
3,912 medium drops (3,913 total including one control drop) and roughly 752 ms p95
queue delay. Scheduler-lag p95 was
18.891 ms, pending reached 0 after drain, and CPU mean was 36.774% of one core.
All 12,400 unique packet IDs are terminal; the canonical ledger SHA-256 is
`07027a3d33e4a839ba55f88459e4133ca2d04f105ccfaeb136b004f23536a162`.
This is one point, not a sweep: no saturation threshold beyond it was established,
and the run has no profile-local Gazebo RTF sample. It is excluded from pass/fail.

## Regression and aggregation decision

Nominal control stayed at PDR 1.0 with 1.329 ms p95; contention was PDR 0.99875 with
1.247 ms p95. Both have pending 0. These are no worse than the original audited
5.399/5.373 ms latency contour and remain inside the existing PDR variation.
The ten UART paths, real SITL ACKs, P2P/P2MP, no-bypass, and Sionna state application
also passed in the associated native product evidence.

Serial aggregation remains disabled. The measured baseline and current data contain
8,813/8,813 and 560/560 records/fragments respectively, so aggregation would add
latency without reducing the profiled BQO1 event fan-out.

## Remaining limits

Physical ns-3 queue emptiness is not sampled at the logical accounting cutoff; the
proved invariant is that every logical packet is terminal and pending is zero. The
meltdown result is not a capacity sweep. Hardware HitL and jammer campaigns remain
outside this result.
