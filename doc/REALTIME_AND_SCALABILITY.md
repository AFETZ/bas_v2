# Real-Time and Scalability

## Why wall-clock behavior matters

Running slower or faster than wall-clock time can:

- violate MAVLink heartbeat and timeout behavior;
- distort UART byte pacing;
- accumulate queues incorrectly;
- change collision, contention, and arbitration timing;
- reuse stale channel state;
- make control loops look unrealistically optimistic;
- create clock drift among Gazebo, SITL, ROS, ns-3, and endpoint adapters;
- trigger hardware watchdogs at the wrong time.

The product therefore treats real-time factor, channel-state age, queue delay,
deadline misses, and cross-component clock offsets as runtime metrics rather
than post-run paperwork.

## Benchmark plan

Use a fixed scenario seed and record host CPU, GPU, memory, operating system,
container identity, dependency versions, and enabled propagation model. Do not
compare runs whose scene, traffic load, or fidelity differs without naming the
difference.

For each configuration, record a warm-up interval followed by a measured
interval and collect:

- Gazebo real-time factor distribution;
- Sionna query latency and update rate;
- radio-state age at ns-3 consumption;
- ns-3 event lag and packet throughput;
- endpoint queue depth, queue delay, and deadline misses;
- CPU, GPU, and memory utilization.

Run the matrix over:

1. Five UAVs with the required traffic channels.
2. Larger node counts chosen after the baseline is stable.
3. Multiple Sionna update rates.
4. Multiple scene-fidelity settings.
5. Periodic and event-driven channel updates.
6. Sionna-only and hybrid propagation regions.

## Operating envelope

The supported operating envelope is the largest measured configuration that
maintains the declared real-time-factor floor, channel-state maximum age,
queue bounds, and deadline budget. Threshold values must be chosen before the
benchmark that claims them. No benchmark results are asserted here.
