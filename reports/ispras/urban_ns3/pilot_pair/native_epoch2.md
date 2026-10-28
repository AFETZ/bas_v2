Run `urban-staged-pilot05-failover` — epoch 2, master UAV4
Observed complete packet/UART/telemetry chains: [1, 2, 3, 5]; expected followers [1, 2, 3, 5].
Target-bearing inspected commands: 4.
Former master UAV1 in observed follower chains: True.

Capture source: native per-node radiotap PHY captures.

| Command | Stage | Capture / log | Source → destination | MAC source → destination | Bytes | Payload SHA-256 prefix | Time |
|---|---|---|---|---|---:|---|---|
| 2:1:2190 | GCS→master | native_radio.pcap.radiotap-1-1.pcap #195617 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 96b3ba4b9ce7661b | ns-3 254.342154 s |
| 2:1:2190 | GCS→master | native_radio.pcap.radiotap-5-0.pcap #205644 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 96b3ba4b9ce7661b | ns-3 254.342801 s |
| 2:1:2190 | master→follower | native_radio.pcap.radiotap-5-0.pcap #205670 | 10.71.4.10:15000 → 10.71.1.10:15000 | 02:71:04:00:10:10 → 02:71:01:00:10:10 | 65 | 96b3ba4b9ce7661b | ns-3 254.353285 s |
| 2:1:2190 | master→follower | native_radio.pcap.radiotap-2-0.pcap #205006 | 10.71.4.10:15000 → 10.71.1.10:15000 | 02:71:04:00:10:10 → 02:71:01:00:10:10 | 65 | 96b3ba4b9ce7661b | ns-3 254.353931 s |
| 2:1:2190 | UART admission | coordination_uav1.jsonl:15901 | UART UAV1 | — | 65 | 96b3ba4b9ce7661b | mono 518320342302913 ns |
| 2:1:2190 | telemetry follower→master | native_radio.pcap.radiotap-2-0.pcap #205040 | 10.71.1.10:15000 → 10.71.4.10:15000 | 02:71:01:00:10:10 → 02:71:04:00:10:10 | 169 | e155388d270f50d7 | ns-3 254.369735 s |
| 2:1:2190 | telemetry follower→master | native_radio.pcap.radiotap-5-0.pcap #205705 | 10.71.1.10:15000 → 10.71.4.10:15000 | 02:71:01:00:10:10 → 02:71:04:00:10:10 | 169 | e155388d270f50d7 | ns-3 254.370381 s |
| 2:1:2190 | telemetry master→GCS | native_radio.pcap.radiotap-5-0.pcap #205707 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | e155388d270f50d7 | ns-3 254.370691 s |
| 2:1:2190 | telemetry master→GCS | native_radio.pcap.radiotap-1-1.pcap #195673 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | e155388d270f50d7 | ns-3 254.371338 s |
| 2:2:2191 | GCS→master | native_radio.pcap.radiotap-1-1.pcap #195619 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 3ecc35fde59c471a | ns-3 254.342871 s |
| 2:2:2191 | GCS→master | native_radio.pcap.radiotap-5-0.pcap #205646 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 3ecc35fde59c471a | ns-3 254.343517 s |
| 2:2:2191 | master→follower | native_radio.pcap.radiotap-5-0.pcap #205672 | 10.71.4.10:15000 → 10.71.2.10:15000 | 02:71:04:00:10:10 → 02:71:02:00:10:10 | 65 | 3ecc35fde59c471a | ns-3 254.354341 s |
| 2:2:2191 | master→follower | native_radio.pcap.radiotap-3-0.pcap #192268 | 10.71.4.10:15000 → 10.71.2.10:15000 | 02:71:04:00:10:10 → 02:71:02:00:10:10 | 65 | 3ecc35fde59c471a | ns-3 254.354987 s |
| 2:2:2191 | UART admission | coordination_uav2.jsonl:3167 | UART UAV2 | — | 65 | 3ecc35fde59c471a | mono 518320343208526 ns |
| 2:2:2191 | telemetry follower→master | native_radio.pcap.radiotap-3-0.pcap #192305 | 10.71.2.10:15000 → 10.71.4.10:15000 | 02:71:02:00:10:10 → 02:71:04:00:10:10 | 169 | d5f48e76351f0cbf | ns-3 254.373124 s |
| 2:2:2191 | telemetry follower→master | native_radio.pcap.radiotap-5-0.pcap #205711 | 10.71.2.10:15000 → 10.71.4.10:15000 | 02:71:02:00:10:10 → 02:71:04:00:10:10 | 169 | d5f48e76351f0cbf | ns-3 254.373771 s |
| 2:2:2191 | telemetry master→GCS | native_radio.pcap.radiotap-5-0.pcap #205715 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | d5f48e76351f0cbf | ns-3 254.374797 s |
| 2:2:2191 | telemetry master→GCS | native_radio.pcap.radiotap-1-1.pcap #195679 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | d5f48e76351f0cbf | ns-3 254.375443 s |
| 2:3:2192 | GCS→master | native_radio.pcap.radiotap-1-1.pcap #195621 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 91de76bb7357303c | ns-3 254.343587 s |
| 2:3:2192 | GCS→master | native_radio.pcap.radiotap-5-0.pcap #205648 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 91de76bb7357303c | ns-3 254.344233 s |
| 2:3:2192 | master→follower | native_radio.pcap.radiotap-5-0.pcap #205676 | 10.71.4.10:15000 → 10.71.3.10:15000 | 02:71:04:00:10:10 → 02:71:03:00:10:10 | 65 | 91de76bb7357303c | ns-3 254.355894 s |
| 2:3:2192 | master→follower | native_radio.pcap.radiotap-4-0.pcap #204381 | 10.71.4.10:15000 → 10.71.3.10:15000 | 02:71:04:00:10:10 → 02:71:03:00:10:10 | 65 | 91de76bb7357303c | ns-3 254.356540 s |
| 2:3:2192 | UART admission | coordination_uav3.jsonl:3161 | UART UAV3 | — | 65 | 91de76bb7357303c | mono 518320344727420 ns |
| 2:3:2192 | telemetry follower→master | native_radio.pcap.radiotap-4-0.pcap #204424 | 10.71.3.10:15000 → 10.71.4.10:15000 | 02:71:03:00:10:10 → 02:71:04:00:10:10 | 169 | d5769c724be5d798 | ns-3 254.379507 s |
| 2:3:2192 | telemetry follower→master | native_radio.pcap.radiotap-5-0.pcap #205721 | 10.71.3.10:15000 → 10.71.4.10:15000 | 02:71:03:00:10:10 → 02:71:04:00:10:10 | 169 | d5769c724be5d798 | ns-3 254.380153 s |
| 2:3:2192 | telemetry master→GCS | native_radio.pcap.radiotap-5-0.pcap #205723 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | d5769c724be5d798 | ns-3 254.382303 s |
| 2:3:2192 | telemetry master→GCS | native_radio.pcap.radiotap-1-1.pcap #195685 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 169 | d5769c724be5d798 | ns-3 254.382949 s |
| 2:5:2194 | GCS→master | native_radio.pcap.radiotap-1-1.pcap #195625 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 883590d1e54d9163 | ns-3 254.345020 s |
| 2:5:2194 | GCS→master | native_radio.pcap.radiotap-5-0.pcap #205652 | 10.71.0.10:15000 → 10.71.4.10:15000 | 02:71:ff:00:00:01 → 02:71:04:00:10:10 | 65 | 883590d1e54d9163 | ns-3 254.345666 s |
| 2:5:2194 | master→follower | native_radio.pcap.radiotap-5-0.pcap #205678 | 10.71.4.10:15000 → 10.71.5.10:15000 | 02:71:04:00:10:10 → 02:71:05:00:10:10 | 65 | 883590d1e54d9163 | ns-3 254.356810 s |
| 2:5:2194 | master→follower | native_radio.pcap.radiotap-6-0.pcap #193227 | 10.71.4.10:15000 → 10.71.5.10:15000 | 02:71:04:00:10:10 → 02:71:05:00:10:10 | 65 | 883590d1e54d9163 | ns-3 254.357456 s |
| 2:5:2194 | UART admission | coordination_uav5.jsonl:3202 | UART UAV5 | — | 65 | 883590d1e54d9163 | mono 518320345658531 ns |
| 2:5:2194 | telemetry follower→master | native_radio.pcap.radiotap-6-0.pcap #193366 | 10.71.5.10:15000 → 10.71.4.10:15000 | 02:71:05:00:10:10 → 02:71:04:00:10:10 | 190 | 0977c7af82e09dea | ns-3 254.543031 s |
| 2:5:2194 | telemetry follower→master | native_radio.pcap.radiotap-5-0.pcap #205859 | 10.71.5.10:15000 → 10.71.4.10:15000 | 02:71:05:00:10:10 → 02:71:04:00:10:10 | 190 | 0977c7af82e09dea | ns-3 254.543709 s |
| 2:5:2194 | telemetry master→GCS | native_radio.pcap.radiotap-5-0.pcap #205876 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 190 | 0977c7af82e09dea | ns-3 254.551264 s |
| 2:5:2194 | telemetry master→GCS | native_radio.pcap.radiotap-1-1.pcap #195795 | 10.71.4.10:15000 → 10.71.0.10:15000 | 02:71:04:00:10:10 → 02:71:ff:00:00:01 | 190 | 0977c7af82e09dea | ns-3 254.551943 s |

- Native radiotap requires both sender and recipient captures per hop; UART logs independently support admission.
- Native PCAP timestamps are ns-3 simulator seconds; event/producer timestamps are host monotonic; no cross-clock latency is computed.
- Subsequent UART telemetry is not attributed to a specific command ACK or completed maneuver.
- Absent matches may reflect packet loss, capture gaps or insufficient telemetry window.
- Automatic selection prefers admitted target-bearing commands and labels diagnostic non-target fallback explicitly.
