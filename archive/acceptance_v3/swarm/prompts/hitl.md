# Worker Scope: Simulated HitL/Loopback And Timing

Implement Day 5 from the execution plan.

Important priority:

- Do not connect, configure, probe, or require physical radio hardware in this
  workstream.
- The project must first model the 2.4 GHz packet/radio path in software via
  Sionna/ns-3/bridge artifacts.
- Treat real hardware-in-the-loop as a later P1/P2 readiness path only.
- If live hardware support is mentioned, document exact future instructions and
  blockers, but do not make it part of the current runnable path.
- Preserve `network/config/radio_backend.yaml`: `sim_2_4ghz` is the current
  acceptance backend, while `real_modem_2_4ghz` is only a future switchable
  backend selected by `AMS_RADIO_BACKEND` or `--radio-backend`.

Primary files/directories:

- `network/hitl/`
- serial pseudo-terminal mode
- Ethernet endpoint mode
- timing supervisor logs
- queue/deadline/drop logging

Physical HitL hardware is not required for P0 and must not be used in this run.
Deliver loopback validation, virtual serial/Ethernet endpoint shims, and exact
live-hardware instructions for future use.

Done when:

- Serial loopback can traverse the same modeled software path or the blocker is
  recorded.
- Ethernet loopback can traverse the same modeled software path or the blocker
  is recorded.
- Timing logs can correlate endpoint, queue, ns-3, and Sionna latency.
- Any physical modem code path is disabled by default and fails closed unless
  the real-modem backend is explicitly selected in a future run.
