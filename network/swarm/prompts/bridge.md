# Worker Scope: MAVLink And Endpoint Bridge

Implement Day 4 from the execution plan.

Primary files/directories:

- `network/bridge/`
- endpoint mapping for five UAVs
- configurable ground-control endpoint
- control/payload/additional-data queues
- PCAP proof hooks for traffic classes
- no-bypass checks around `move_drone.py` and direct localhost paths

Prefer existing MAVLink routing tools before writing local routing logic.

Done when:

- Direct `udp:127.0.0.1:14550` bypass risk is removed, wrapped, or explicitly
  blocked.
- Endpoint mappings preserve UAV names and MAVLink system IDs.
- Control traffic can be prioritized over payload traffic.
