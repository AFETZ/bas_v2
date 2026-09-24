# Worker Scope: ns-3 Packet Core

Implement Day 3 from the execution plan.

Primary files/directories:

- `network/ns3/`
- ns-3 build/run wrappers
- real-time topology for five UAVs plus command post
- PCAP and FlowMonitor output paths
- Sionna link-state adapter client

Use existing ns-3 models before writing custom MAC/PHY behavior. Document any
model choice in `network/DECISIONS.md`.

Done when:

- ns-3 runtime command exists.
- Topology and traffic classes are represented.
- Sionna-derived link state can affect packet behavior or the exact blocker is
  recorded.
