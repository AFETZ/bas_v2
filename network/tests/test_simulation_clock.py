"""Clock-domain and barrier fixtures; these do not simulate radio outcomes."""
import time
import json
import os
import subprocess
from pathlib import Path

import pytest

from network.scripts import simulation_clock as clock
from network.scripts import data_transport
from network.scripts.serial_transport import Encoder, Reassembler, BoundedQueue, radio_is_live
from network.position_tracker.tracker import LiveState
from network.tests.test_live_coupling import odom


@pytest.fixture
def model_clock(tmp_path, monkeypatch):
    path = tmp_path / "clock.json"
    monkeypatch.setenv("BAS_SIMULATION_MODE", "lockstep")
    monkeypatch.setenv("BAS_SIM_CLOCK", str(path))
    def publish(ns, phase="exchange", healthy=True, wall_age=0):
        clock.write(path, dict(mode="lockstep", simulation_ns=ns, phase=phase,
            healthy=healthy, monotonic_ns=time.monotonic_ns()-int(wall_age*1e9)))
    publish(1_000_000_000)
    return publish


def test_pause_preserves_deadlines_then_model_progress_expires(model_clock):
    tx = Encoder(channel="control", uav_id=1, direction="gcs_to_uart")
    rx = Reassembler(channel="control", uav_id=1, direction="gcs_to_uart", max_age_ms=250)
    frame = tx.encode(b"command")[0]
    # Host time moves while model time stays fixed (long solver computation).
    model_clock(1_000_000_000, phase="radio", wall_age=10)
    assert clock.monotonic_ns() == 1_000_000_000 and radio_is_live("unused", .3)
    assert rx.ingest(frame) == [b"command"]
    queue = BoundedQueue(2, 100, .25)
    queue.put(b"later", clock.monotonic())
    queue.expire(clock.monotonic())
    assert queue.bytes == 5
    expired = tx.encode(b"expired")[0]
    model_clock(1_300_000_000)
    assert rx.ingest(expired) == [] and rx.counters.deadline_drops == 1
    queue.expire(clock.monotonic())
    assert queue.bytes == 0 and queue.expired == 1


def test_transport_rejects_mixed_clock_domains(model_clock, monkeypatch):
    tx = Encoder(channel="control", uav_id=1, direction="gcs_to_uart")
    serial = tx.encode(b"model")[0]
    data = data_transport.encode("p2p_downlink", sender_id=0, receiver_id=1, sequence=1, payload=b"model")
    monkeypatch.setenv("BAS_SIMULATION_MODE", "realtime")
    rx = Reassembler(channel="control", uav_id=1, direction="gcs_to_uart")
    assert rx.ingest(serial) == [] and rx.counters.malformed_chunks == 1
    with pytest.raises(data_transport.DataProtocolError):
        data_transport.decode(data)


def test_host_watchdog_and_stop_are_independent_of_frozen_model_time(model_clock):
    model_clock(10, "radio", wall_age=61)
    assert not clock.live()
    model_clock(10, "stopped", healthy=False)
    assert not clock.live() and clock.monotonic_ns() == 10
    model_clock(10, "exchange")
    assert clock.exchanging()
    model_clock(10, "physics")
    assert not clock.exchanging()


def test_no_bypass_observation_finishes_after_model_clock_stops(model_clock):
    from scripts.product.native_radio_five_uav_scenario import NativeFiveUavHarness
    model_clock(10, "stopped", healthy=False)
    harness = object.__new__(NativeFiveUavHarness)
    observations = []
    harness.pump = lambda seconds: (observations.append(clock.monotonic_ns()), time.sleep(seconds))
    harness.observe_for(.01, wall_time=True)
    assert observations and set(observations) == {10}


def test_tracker_pause_uses_source_age_and_still_rejects_clock_reset():
    state = LiveState({"robots": [{"name": "uav1"}]}, simulation_mode="lockstep")
    state.clock(10., 1_000_000_000)
    assert state.odometry("uav1", odom(10.), 1_000_000_000)
    assert not state.snapshot(60_000_000_000)["stale_nodes"]
    state.clock(10.6, 60_000_000_000)
    assert state.snapshot(60_000_000_000)["stale_nodes"] == ["uav1"]
    state.clock(1., 60_000_000_000)
    assert state.fault == "gazebo_clock_reset_restart_required"


def test_model_report_never_claims_realtime_readiness(tmp_path):
    from scripts.product.summarize_native_radio_five_uav import build_realtime, UAVS
    mobility = {"uavs": {u: {"applied_position_age_ms": {"p95": 30.}} for u in UAVS}}
    stats = dict(simulation_mode="lockstep", coupling_step_ms=20, completed_lockstep_steps=8,
                 stale_pose_samples=0, state_max_age_s=.5, stop_reason="duration")
    value = build_realtime(tmp_path, [], mobility, stats, {})
    assert value["realtime_readiness"] == "not_applicable" and value["lockstep_coupling_passed"]
    stats["stale_pose_samples"] = 1
    assert not build_realtime(tmp_path, [], mobility, stats, {})["lockstep_coupling_passed"]


@pytest.mark.skipif(__import__('os').name == 'nt', reason="runtime Linux flock barrier")
def test_uart_barrier_excludes_writes(model_clock, monkeypatch):
    import fcntl
    import os
    with open(os.environ["BAS_SIM_CLOCK"] + ".lock", "a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with clock.io_window() as allowed:
            assert not allowed
        fcntl.flock(owner, fcntl.LOCK_UN)
    with clock.io_window() as allowed:
        assert allowed
    model_clock(1_000_000_000, "physics")
    with clock.io_window() as allowed:
        assert not allowed


@pytest.mark.skipif(__import__('os').name == 'nt', reason="real Linux PTY boundary")
def test_real_uart_waits_for_barrier_and_ages_in_model_time(tmp_path, model_clock):
    import os
    import pty
    import select
    import socket
    import subprocess
    import sys
    master, slave = pty.openpty()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        address = probe.getsockname()
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen([sys.executable, str(root/"network/scripts/communication_vertical.py"),
        "uart-adapter", "--channel", "control", "--framed", "--tty", os.ttyname(slave),
        "--bind", "%s:%s" % address, "--peer", "%s:%s" % peer.getsockname(),
        "--event-log", str(tmp_path/"events"), "--ready-file", str(tmp_path/"ready")], stderr=subprocess.PIPE)
    tx = Encoder(channel="control", uav_id=1, direction="gcs_to_uart")
    try:
        until = time.monotonic()+15
        while not (tmp_path/"ready").exists() and time.monotonic()<until:
            assert process.poll() is None
            time.sleep(.02)
        assert (tmp_path/"ready").exists()
        model_clock(1_000_000_000, "radio")
        time.sleep(.1)  # let any prior selector iteration drain
        peer.sendto(tx.encode(b"held-command")[0], address)
        assert not select.select([master], [], [], .4)[0]  # longer than control deadline
        model_clock(1_000_000_000, "exchange")
        assert select.select([master], [], [], 1)[0]
        assert os.read(master, 4096) == b"held-command"
        old = tx.encode(b"expired-command")[0]
        model_clock(1_300_000_000)
        peer.sendto(old, address)
        assert not select.select([master], [], [], .2)[0]
        peer.sendto(tx.encode(b"new-command")[0], address)
        assert select.select([master], [], [], 1)[0]
        assert os.read(master, 4096) == b"new-command"
    finally:
        process.terminate()
        _, error = process.communicate(timeout=5)
        os.close(master); os.close(slave); peer.close()
        assert process.returncode == 0, error.decode()


@pytest.mark.skipif(os.name == 'nt', reason='Linux flock exchange barrier')
def test_exchange_acknowledges_steps_and_rejoining_endpoint(model_clock, monkeypatch):
    monkeypatch.setenv('BAS_LOCKSTEP_PARTICIPANT', 'rejoining')
    model_clock(20_000_000, 'exchange')
    cycles = []
    for _ in range(2):
        with clock.io_window() as allowed:
            assert allowed
            cycles.append(clock.monotonic_ns())
    path = clock.participant_path()
    assert json.loads(path.read_text())['simulation_ns'] == 20_000_000
    clock.close_participant()
    with clock.io_window() as allowed:
        assert allowed
    assert json.loads(path.read_text())['simulation_ns'] == 20_000_000
    model_clock(40_000_000, 'exchange')
    with clock.io_window() as allowed:
        assert allowed
    assert json.loads(path.read_text())['simulation_ns'] == 40_000_000
    assert cycles == [20_000_000, 20_000_000]
    clock.close_participant()


@pytest.mark.skipif(os.name == 'nt', reason='Linux flock exchange barrier')
def test_gcs_real_datagram_waits_for_exchange(model_clock, monkeypatch):
    import fcntl
    import socket
    from concurrent.futures import ThreadPoolExecutor
    from pymavlink import mavutil
    from network.scripts.serial_transport import TransportCounters, MavlinkStreamCounter
    from scripts.product import town01_full_stack_scenario as scenario
    h = object.__new__(scenario.FlightHarness)
    key = ('control', 1)
    h.transmitters = {key: mavutil.mavlink.MAVLink(None)}
    h.transport_counters = {key: TransportCounters()}
    h.transport_input_frames = {key: MavlinkStreamCounter()}
    h.transport_encoders = {key: Encoder(channel='control', uav_id=1, direction='gcs_to_uart')}
    monkeypatch.setattr(scenario, 'endpoint_ip', lambda _: '127.0.0.1')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
        rx.bind(('127.0.0.1', 14601))
        rx.settimeout(.05)
        h.sockets = {'control': tx}
        model_clock(1_000_000_000, 'radio')
        with open(os.environ['BAS_SIM_CLOCK']+'.lock', 'a') as owner, ThreadPoolExecutor(1) as pool:
            fcntl.flock(owner, fcntl.LOCK_EX)
            future = pool.submit(h.send, 'control', 1, h.transmitters[key].heartbeat_encode(6, 8, 0, 0, 0))
            try:
                with pytest.raises(socket.timeout):
                    rx.recvfrom(65535)
            finally:
                model_clock(1_000_000_000)
                fcntl.flock(owner, fcntl.LOCK_UN)
            assert future.result(timeout=2) > 0
            rx.settimeout(1)
            data, _ = rx.recvfrom(65535)
            records = Reassembler(channel='control', uav_id=1, direction='gcs_to_uart').ingest(data)
            assert records and mavutil.mavlink.MAVLink(None).parse_buffer(records[0])[0].get_type() == 'HEARTBEAT'


@pytest.mark.skipif(os.name == 'nt', reason='Linux native exchange binary')
def test_native_exchange_waits_for_every_registered_endpoint(tmp_path, model_clock, monkeypatch):
    binary = Path(__file__).resolve().parents[2]/'.external/ns-3-sionna-native/build/scratch/ns3.48-native-live-state-test-default'
    if not binary.exists():
        pytest.skip('focused native state test binary not built')
    path = Path(os.environ['BAS_SIM_CLOCK'])
    clock.initial(path)
    for name in ('first', 'delayed'):
        monkeypatch.setenv('BAS_LOCKSTEP_PARTICIPANT', name)
        with clock.io_window() as allowed:
            assert allowed
    ack = Path(str(path)+'.ack')
    clock.write(ack, dict(target_ns=0, source_origin_ns=0, physics_step_ns=1_000_000, fault=None))
    process = subprocess.Popen([str(binary), '--exchange', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic()+2
        while not Path(str(path)+'.request').exists() and time.monotonic()<deadline:
            time.sleep(.001)
        assert Path(str(path)+'.request').exists()
        clock.write(ack, dict(target_ns=20_000_000, source_origin_ns=0, physics_step_ns=1_000_000, fault=None))
        while clock.snapshot()['phase'] != 'exchange' and time.monotonic()<deadline:
            time.sleep(.001)
        monkeypatch.setenv('BAS_LOCKSTEP_PARTICIPANT', 'first')
        with clock.io_window() as allowed:
            assert allowed
        time.sleep(.1)  # greater than the removed fixed 20 ms opportunity
        assert process.poll() is None
        monkeypatch.setenv('BAS_LOCKSTEP_PARTICIPANT', 'delayed')
        with clock.io_window() as allowed:
            assert allowed
        stdout, stderr = process.communicate(timeout=2)
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
