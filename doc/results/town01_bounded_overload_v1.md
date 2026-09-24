# Town01 bounded-overload v1

## product-first-workflow

- Branch: `product-first-workflow`
- Source commit: `28c52f5`
- Baseline commit: `26fa99c`
- Runs: `overload-profile-before-20260827T193206Z`,
  `meltdown-global-20260827T214951Z`, and focused
  `town01-qos-hardening-20260829T144515Z`.

The focused run selected `controlled_overload`; heatmaps and the flight
lifecycle were intentionally skipped. It still ran five SITLs, Gazebo, live
position tracking, real Sionna, ns-3, ten UART adapters, runtime monitoring,
the selected profile, stop-probe, and cleanup.

## Result

- Medium access: `centralized_priority_scheduler_over_csma_channel`.
  Arbitration is `centralized_priority_scheduler` over `ns3_csma_channel`;
  collisions are not expected under centralized grants and a current frame is
  non-preemptive.
- Offered/admitted/delivered application rate: 33,804,800 / 12,860,800 /
  12,832,800 bit/s (offered-window normalized).
- Control: 600/600 delivered (PDR 1.000), p95 latency 1.391 ms.
- Scheduler lag p95: 5.992 ms. Payload/additional-data delivered Jain fairness:
  0.9999979 / 0.9999935.
- Logical terminal pending: 0; all 18,600 logical packets are terminal.
  Physical ns-3 queue state was not measured, so physical queue empty is null.
- Profile RTF: `unmeasured`; available Gazebo RTF samples are run-global and
  are not used as controlled-profile proof.
- No-bypass: true, scope `full_run`, and explicitly not profile-local.
- Capacity/headroom: 20,000,000 configured, 13,000,000 lower admission total,
  7,000,000 actual static headroom, 4,000,000 minimum control headroom bit/s.

## Asymmetric demand characterization

The deterministic token-bucket test keeps payload demand only on uav1. Its
maximum sustained admitted payload rate is 1,300,000 bit/s versus the
6,500,000 bit/s aggregate payload bucket. Therefore
`work_conserving_across_idle_uavs: false`; borrowing idle UAV capacity is not
implemented.

## Known limitations

- Live jammer packet-path integration and dynamic channel-aware airtime
  admission are pending.
- Stock/distributed CSMA contention mode is not yet characterized.
- Physical queue emptiness is not sampled at the logical drain cutoff.
- This is a Town01 development baseline, not the deferred 10 by 10 km scene.

## Reproduction

```bash
BAS_TOWN01_PROFILES=controlled_overload \
BAS_TOWN01_SKIP_HEATMAPS=1 \
BAS_TOWN01_SKIP_FLIGHT_SCENARIO=1 \
BAS_TOWN01_RUN_ID=town01-qos-hardening-$(date -u +%Y%m%dT%H%M%SZ) \
./scripts/product/run_town01_full_stack.sh
```
