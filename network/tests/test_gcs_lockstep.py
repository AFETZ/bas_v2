"""GCS unit regressions; fixture ACKs are not integrated radio measurements."""

from collections import Counter
from contextlib import contextmanager
import os
import selectors
import socket
from types import SimpleNamespace

import pytest

from network.scripts import data_transport, simulation_clock as clock
from network.scripts.serial_transport import Encoder, MavlinkStreamCounter, TransportCounters
from network.tests.test_simulation_clock import model_clock
from scripts.product.native_radio_five_uav_scenario import NativeFiveUavHarness
from scripts.product.town01_full_stack_scenario import FlightHarness, ScenarioError, UAV_IDS


@pytest.fixture
def harness(model_clock):
    from pymavlink.dialects.v20 import common as mavlink

    value = object.__new__(NativeFiveUavHarness)
    value.mavutil = SimpleNamespace(mavlink=mavlink)
    value.timeout_scale = 1.0
    value.diagnostic_retry_interval_s = 1.0
    value.summary = {"command_acks": []}
    value.message_counts = Counter()
    value.latest, value.latest_at_ns, value.acks = {}, {}, {}
    value.transmitters, value.parsers = {}, {}
    value.transport_counters, value.transport_encoders = {}, {}
    value.transport_input_frames, value.transport_output_frames = {}, {}
    value.transport_malformed_datagrams = 0
    value.sent = []

    def sendto(data, address):
        value.sent.append((data, address))
        return len(data)

    value.sockets = {channel: SimpleNamespace(sendto=sendto) for channel in ("control", "payload")}
    for channel in value.sockets:
        for system_id in UAV_IDS:
            key = channel, system_id
            value.transmitters[key] = mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
            value.parsers[key] = mavlink.MAVLink(None)
            value.transport_counters[key] = TransportCounters()
            value.transport_encoders[key] = Encoder(channel=channel, uav_id=system_id, direction="gcs_to_uart")
            value.transport_input_frames[key] = MavlinkStreamCounter()
            value.transport_output_frames[key] = MavlinkStreamCounter()
    return value


def receive_ack(harness, channel, system_id=1, command=512, result=0):
    mavlink = harness.mavutil.mavlink
    sender = mavlink.MAVLink(None, srcSystem=system_id, srcComponent=1)
    frame = sender.command_ack_encode(command, result).pack(sender)
    harness._consume_mavlink(channel, system_id, [frame], ("127.0.0.1", 0), clock.monotonic_ns())


def test_mission_retries_lost_opener_without_restarting_item_transfer(harness, model_clock):
    # Protocol stimuli only: the first opener is lost, then the final ACK is
    # delayed beyond the opener retry interval. Neither is RF evidence.
    messages = []
    harness.positions = lambda: {"uav1": [0., 0., 0.]}
    harness.request_global_position = lambda _system: (40., 10.)
    harness.event = lambda *args, **kwargs: None
    harness.send = lambda channel, system, message: messages.append(message)
    request_sent = False

    def pump(_timeout):
        nonlocal request_sent
        model_clock(clock.monotonic_ns() + 1_000_000_000)
        openers = [m for m in messages if m.get_type() == "MISSION_COUNT"]
        items = [m for m in messages if m.get_type() == "MISSION_ITEM_INT"]
        if len(openers) >= 2 and not request_sent:
            key = ("control", 1, "MISSION_REQUEST_INT")
            harness.latest[key] = SimpleNamespace(seq=0)
            harness.latest_at_ns[key] = clock.monotonic_ns()
            request_sent = True
        if items and clock.monotonic() >= 7:
            key = ("control", 1, "MISSION_ACK")
            harness.latest[key] = SimpleNamespace(type=0)
            harness.latest_at_ns[key] = clock.monotonic_ns()

    harness.pump = pump
    harness.upload_uav_mission(1, [{"name": "test", "position_m": [10., 0., 20.], "hold_s": 0.}])
    assert [m.get_type() for m in messages] == ["MISSION_COUNT", "MISSION_COUNT", "MISSION_ITEM_INT"]
    assert harness.summary["missions"]["uav1"]["upload_ack"] == 0


def run_command(harness, method, channel):
    params = [148.0, 0, 0, 0, 0, 0, 0]
    if method == "command_operation":
        return harness.command_operation(
            channel=channel, system_id=1, command=512, params=params, label="freshness",
            retry_policy="one_shot", retry_interval_s=None, maximum_attempts=1, timeout_s=.01,
        )["first_attempt_success"]
    if method == "parallel_one_shot_operations":
        operations = harness.parallel_one_shot_operations(
            systems=UAV_IDS, command=512, params=params, round_number=1, timeout_s=.01,
        )
        return all(operation["first_attempt_success"] for operation in operations)
    if method == "command_all":
        harness.command_all(channel, 512, params, .01, "freshness")
    elif method == "command_until_accepted":
        harness.command_until_accepted(1, 512, params, "freshness", .01)
    else:
        getattr(harness, method)(channel, 1, 512, params, .01, "freshness")
    return True


@pytest.mark.parametrize("method,channel", [
    ("command_operation", "control"),
    ("command_operation", "payload"),
    ("parallel_one_shot_operations", "control"),
    ("native_command_one", "control"),
    ("command_one", "payload"),
    ("command_all", "control"),
    ("command_until_accepted", "control"),
])
def test_cached_ack_cannot_complete_another_command(harness, model_clock, method, channel):
    def fresh_response(_timeout):
        for system_id in UAV_IDS:
            receive_ack(harness, channel, system_id)

    harness.pump = fresh_response
    assert run_command(harness, method, channel)
    # Two subsequent operations start at the cached ACK's exact model time.
    for _ in range(2):
        model_clock(1_000_000_000)
        calls = []

        def no_response(_timeout):
            calls.append(None)
            if len(calls) == 2:
                model_clock(1_020_000_000)

        harness.pump = no_response
        if method in ("command_operation", "parallel_one_shot_operations"):
            assert not run_command(harness, method, channel)
        else:
            with pytest.raises(ScenarioError, match="ACK|not accepted"):
                run_command(harness, method, channel)


@pytest.mark.parametrize("advance_ns", [0, 2_000_000])
def test_new_ack_is_accepted_and_other_keys_are_preserved(harness, model_clock, advance_ns):
    receive_ack(harness, "payload")
    receive_ack(harness, "control", system_id=2)
    receive_ack(harness, "control", command=400)
    other_acks = dict(harness.acks)
    def fresh_response(_timeout):
        model_clock(clock.monotonic_ns() + advance_ns)
        receive_ack(harness, "control")

    harness.pump = fresh_response
    assert run_command(harness, "command_operation", "control")
    assert run_command(harness, "command_operation", "control")
    assert all(harness.acks[key] is ack for key, ack in other_acks.items())
    assert [op["first_attempt_rtt_ms"] for op in harness.summary["command_operations"]] == [advance_ns / 1e6] * 2


@pytest.fixture
def gcs_receiver(model_clock, monkeypatch):
    # Exercise phase transitions on Windows too; the Linux cases use real flock.
    if os.name == "nt":
        @contextmanager
        def io_window(**_kwargs):
            yield clock.mode() == "realtime" or clock.snapshot()["phase"] in ("exchange", "stopped")
        monkeypatch.setattr(clock, "io_window", io_window)
    value = object.__new__(FlightHarness)
    value.transport_receivers = {}
    value.additional_received = []
    value.additional_malformed = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, selectors.DefaultSelector() as selector:
        receiver.bind(("127.0.0.1", 0))
        receiver.setblocking(False)
        selector.register(receiver, selectors.EVENT_READ, "additional_data")
        value.selector = selector
        yield value, receiver, selector


def test_gcs_defers_udp_when_exchange_closes_during_select(gcs_receiver, model_clock):
    harness, receiver, selector = gcs_receiver
    packet = data_transport.encode("p2p_downlink", sender_id=0, receiver_id=1, sequence=1, payload=b"test")

    def select_then_close(timeout):
        model_clock(1_000_000_000, "radio")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(packet, receiver.getsockname())
        return selector.select(timeout)

    harness.selector = SimpleNamespace(select=select_then_close)
    harness.pump(.1)
    assert harness.additional_received == []
    assert selector.select(0), "packet must remain queued until the next exchange"
    model_clock(1_020_000_000)
    harness.selector = selector
    harness.pump(0)
    assert len(harness.additional_received) == 1
    assert harness.additional_received[0][2] == 1_020_000_000


@pytest.mark.parametrize("mode,phase", [
    ("lockstep", "exchange"),
    ("lockstep", "stopped"),
    ("realtime", "exchange"),
])
def test_gcs_receives_in_allowed_modes(gcs_receiver, model_clock, monkeypatch, mode, phase):
    harness, receiver, _selector = gcs_receiver
    model_clock(1_000_000_000, phase, healthy=phase != "stopped")
    monkeypatch.setenv("BAS_SIMULATION_MODE", mode)
    packet = data_transport.encode("p2p_downlink", sender_id=0, receiver_id=1, sequence=1, payload=b"test")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(packet, receiver.getsockname())
    harness.pump(.1)
    assert len(harness.additional_received) == 1
    assert harness.additional_received[0][0].payload == b"test"


@pytest.mark.skipif(os.name == "nt", reason="runtime Linux flock barrier")
def test_gcs_leaves_udp_queued_while_controller_holds_barrier(gcs_receiver):
    import fcntl

    harness, receiver, selector = gcs_receiver
    packet = data_transport.encode("p2p_downlink", sender_id=0, receiver_id=1, sequence=1, payload=b"test")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(packet, receiver.getsockname())
    with open(os.environ["BAS_SIM_CLOCK"] + ".lock", "a") as controller:
        fcntl.flock(controller, fcntl.LOCK_EX)
        harness.pump(0)
        assert harness.additional_received == []
        assert selector.select(0)
        fcntl.flock(controller, fcntl.LOCK_UN)
    harness.pump(0)
    assert len(harness.additional_received) == 1


@pytest.mark.skipif(os.name == "nt", reason="runtime Linux flock barrier")
def test_gcs_holds_barrier_while_draining_but_not_while_waiting(gcs_receiver, monkeypatch):
    import fcntl
    from scripts.product import town01_full_stack_scenario as scenario

    harness, receiver, selector = gcs_receiver
    packet = data_transport.encode("p2p_downlink", sender_id=0, receiver_id=1, sequence=1, payload=b"test")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(packet, receiver.getsockname())
    with open(os.environ["BAS_SIM_CLOCK"] + ".lock", "a") as controller:
        selections = []
        def select_without_barrier(timeout):
            if not selections:
                fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(controller, fcntl.LOCK_UN)
            else:
                # Drain already available bytes atomically; never wait under
                # the phase barrier for bytes a future radio step must deliver.
                assert timeout == 0
                with pytest.raises(BlockingIOError):
                    fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
            selections.append(timeout)
            return selector.select(timeout)

        def require_barrier():
            with pytest.raises(BlockingIOError):
                fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def expire(_now):
            require_barrier()
            return []

        def decode_with_barrier(data):
            require_barrier()
            return data_transport.decode(data)

        harness.selector = SimpleNamespace(select=select_without_barrier)
        harness.transport_receivers[("control", 1)] = SimpleNamespace(expire=expire)
        monkeypatch.setattr(scenario, "decode_data", decode_with_barrier)
        harness.pump(.1)
        assert len(harness.additional_received) == 1
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(controller, fcntl.LOCK_UN)
