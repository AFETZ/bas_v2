"""Protocol fixtures only; no simulator, UART or real autopilot is started."""
import struct
import socket
import json
import threading
import time

from pymavlink import mavutil
from pymavlink.dialects.v10 import ardupilotmega as mav1
from pymavlink.dialects.v20 import ardupilotmega as mav2

from scripts.product.capture_autopilot_params import ParameterTable, Requests, compare, decode_value, main, receive_logged, value_wire_bytes


def param(name, count, index, value=1, param_type=6, system=1, component=1, dialect=None):
    dialect = dialect or mav1
    sender = dialect.MAVLink(None, srcSystem=system, srcComponent=component)
    frame = sender.param_value_encode(name.encode(), value, param_type, count, index).pack(sender)
    parser = dialect.MAVLink(None)
    return parser.parse_char(frame)


def test_complete_requires_exact_source_and_all_distinct_indexed_names():
    table = ParameterTable(1, 1, "c-cast")
    assert table.accept(param("LAST", 3, 2), 1)
    assert not table.accept(param("MID", 3, 1, system=2), 2)
    assert not table.accept(param("MID", 3, 1, component=2), 3)
    assert table.accept(param("FIRST", 3, 0), 4)
    assert table.report()["missing_indices"] == [1]
    assert not table.report()["complete"]
    assert table.accept(param("MID", 3, 1), 5)
    assert table.report()["complete"]
    assert table.report()["rejected_messages"] == {"foreign_system_or_component": 2}


def test_count_identity_and_value_conflicts_are_not_hidden_by_union():
    table = ParameterTable(1, 1, "c-cast")
    table.accept(param("A", 2, 0), 1)
    table.accept(param("B", 2, 0), 2)  # one index cannot establish two identities
    table.accept(param("A", 2, 1, 2), 3)  # name moved and value changed
    table.accept(param("C", 3, 2), 4)  # another count generation
    report = table.report()
    assert not report["complete"]
    assert report["param_counts_observed"] == [2, 3]
    assert report["value_changes_during_readout"]
    assert {r["reason"] for r in report["conflicts"]} >= {
        "index_name_changed", "name_index_changed", "param_count_changed"}


def test_missing_reads_are_paced_bounded_and_do_not_restart_stream():
    table = ParameterTable(1, 1, "c-cast")
    requests = Requests(10, 1, 2, 2)
    assert requests.next(table, 0, None) == ("list", None)
    table.accept(param("A", 3, 0), 10_000_000)
    assert requests.next(table, 1_000_000_000, 10_000_000) is None
    assert requests.next(table, 2_100_000_000, 10_000_000) == ("read", 1)
    assert requests.next(table, 2_150_000_000, 10_000_000) is None
    table.accept(param("B", 3, 1), 2_160_000_000)
    # Receipt of one repair does not insert another two-second list-stream wait.
    assert requests.next(table, 2_200_000_000, 2_160_000_000) == ("read", 2)
    assert requests.next(table, 3_200_000_000, 2_160_000_000) == ("read", 2)
    assert requests.next(table, 4_200_000_000, 2_160_000_000) is None
    assert requests.list_attempts == 1
    assert requests.read_attempts[2][0] == 2
    assert table.report()["missing_indices"] == [2]


def test_bytewise_nan_bits_and_unsupported_values_are_preserved():
    raw = struct.pack("<I", 0x7FC00001)
    assert decode_value(raw, 5, "bytewise") == (0x7FC00001, None)
    assert decode_value(raw, 5, "c-cast")[1] == "nonintegral_or_nonfinite_c_cast_integer"
    table = ParameterTable(1, 1, "bytewise")
    table.accept(param("MASK", 1, 0, struct.unpack("<f", raw)[0], 5), 1)
    record = table.report()["parameters"][0]
    assert record["wire_value_hex"] == raw.hex()
    assert record["value_json"] == str(0x7FC00001)
    assert table.report()["complete"]
    assert decode_value(struct.pack("<f", 300), 1, "c-cast")[1] == "c_cast_value_outside_declared_integer_range"
    assert decode_value(struct.pack("<f", 1), 10, "bytewise")[1]


def test_partial_after_read_does_not_claim_parameter_removal():
    before = ParameterTable(1, 1, "c-cast")
    after = ParameterTable(1, 1, "c-cast")
    for i, name in enumerate(["A", "B"]):
        before.accept(param(name, 2, i), i + 1)
    after.accept(param("A", 2, 0, 2), 3)
    changes = compare({"components": [before.report()]}, {"components": [after.report()]})
    assert {row["param_id"]: row["change"] for row in changes} == {
        "A": "value_type_or_index_changed", "B": "not_observed_after"}


def test_actual_v1_and_v2_frame_value_bytes_not_the_legacy_payload_accessor():
    for dialect, marker in [(mav1, 0xFE), (mav2, 0xFD)]:
        message = param("BARO1_GND_PRESS", 1388, 100, 101325., 9, dialect=dialect)
        assert message.get_msgbuf()[0] == marker
        assert value_wire_bytes(message) == struct.pack("<f", 101325.)
        table = ParameterTable(1, 1, "c-cast")
        table.accept(message, 1)
        assert table.parameters["BARO1_GND_PRESS"]["value_json"] == "101325.0"


def test_unindexed_broadcast_can_resolve_to_later_index_and_changes_remain_visible():
    table = ParameterTable(1, 1, "c-cast")
    table.accept(param("STAT_RUNTIME", 1, 65535, 10, dialect=mav2), 1)
    assert not table.report()["complete"]
    table.accept(param("STAT_RUNTIME", 1, 0, 11, dialect=mav2), 2)
    result = table.report()
    assert result["complete"] and result["index_coverage_complete"]
    assert result["unresolved_unindexed_parameters"] == []
    assert not result["stable_values_observed"] and result["value_changes_during_readout"]


def test_upstream_udp_receive_really_writes_tlog_and_raw(tmp_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(1)
    link = mavutil.mavlink_connection(f"udpout:127.0.0.1:{server.getsockname()[1]}", source_system=255)
    tlog, raw = tmp_path / "actual.tlog", tmp_path / "actual.raw"
    link.setup_logfile(str(tlog)); link.setup_logfile_raw(str(raw))
    frame = bytes(param("VALUE", 1, 0, 42, dialect=mav2).get_msgbuf())
    try:
        link.mav.heartbeat_send(6, 8, 0, 0, 0)
        _, peer = server.recvfrom(4096)
        server.sendto(frame, peer)
        received = None
        until = time.monotonic() + 1
        while received is None and time.monotonic() < until:
            received = receive_logged(link)
        assert received is not None and received.get_type() == "PARAM_VALUE"
    finally:
        link.logfile.close(); link.logfile_raw.close(); link.close(); server.close()
    assert raw.read_bytes() == frame
    assert len(tlog.read_bytes()) == 8 + len(frame)
    replay = mavutil.mavlink_connection(str(tlog))
    try:
        observed = replay.recv_match(type="PARAM_VALUE")
        assert observed is not None and observed.param_id == "VALUE" and observed.param_value == 42
    finally:
        replay.close()


def test_five_real_udp_workers_isolate_mixed_frames_and_bounded_partial_repairs(tmp_path):
    # Isolated protocol peers, not autopilot/SITL flight substitutes. All five
    # must receive their LIST before any sends its table, proving concurrency.
    for _ in range(30):
        servers = []
        try:
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            first.bind(("127.0.0.1", 0)); servers.append(first)
            base = first.getsockname()[1] - 1
            if base > 65530:
                raise OSError("need five contiguous ports")
            for sid in range(2, 6):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                servers.append(sock); sock.bind(("127.0.0.1", base + sid))
            break
        except OSError:
            for sock in servers:
                sock.close()
    else:
        raise AssertionError("could not reserve five isolated local UDP ports")
    barrier, stop = threading.Barrier(5), threading.Event()
    sent, reads, request_types, errors = {}, {}, {}, []

    def serve(sid, sock):
        sent[sid], reads[sid], request_types[sid] = [], [], []
        parser = mav2.MAVLink(None)
        sock.settimeout(.025)
        try:
            while not stop.is_set():
                try:
                    data, peer = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                for message in parser.parse_buffer(data) or []:
                    kind = message.get_type(); request_types[sid].append(kind)
                    if kind == "PARAM_REQUEST_LIST":
                        assert (message.target_system, message.target_component) == (sid, 1)
                        barrier.wait(timeout=2)
                        frames = [param("FOREIGN", 3, 1, system=99),
                                  param("FOREIGN_COMP", 3, 1, system=sid, component=2, dialect=mav2),
                                  param(f"U{sid}_A", 3, 0, sid * 100, system=sid),
                                  param(f"U{sid}_C", 3, 2, sid * 100 + 2, system=sid, dialect=mav2)]
                        for message in frames:
                            frame = bytes(message.get_msgbuf()); sock.sendto(frame, peer); sent[sid].append(frame)
                    elif kind == "PARAM_REQUEST_READ":
                        assert (message.target_system, message.target_component, message.param_index) == (sid, 1, 1)
                        reads[sid].append(time.monotonic())
                        if sid != 5:  # Honest partial coverage for one nonresponding index.
                            frame = bytes(param(f"U{sid}_B", 3, 1, sid * 100 + 1, system=sid,
                                                dialect=mav2 if sid % 2 else mav1).get_msgbuf())
                            sock.sendto(frame, peer); sent[sid].append(frame)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=serve, args=(sid, sock)) for sid, sock in enumerate(servers, 1)]
    for thread in threads:
        thread.start()
    output = tmp_path / "five"
    try:
        assert main(["--output-dir", str(output), "--run-id", "udp-only",
                     "--workers", "5", "--port-base", str(base), "--timeout-s", ".8",
                     "--read-rate-hz", "20", "--retry-s", ".07", "--list-idle-s", ".08",
                     "--settle-s", ".04", "--retries", "3"]) == 2
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=3)
        for sock in servers:
            sock.close()
    assert not errors and not any(t.is_alive() for t in threads)
    result = json.loads((output / "summary.json").read_text())
    assert result["settings"]["workers"] == result["effective_workers"] == 5
    assert result["mavlink_parser_protocol"] == "2.0" and result["status"] == "partial"
    for sid, component in enumerate(result["components"], 1):
        assert component["system_id"] == sid and component["component_id"] == 1
        assert component["complete"] is (sid != 5)
        assert component["missing_indices"] == ([1] if sid == 5 else [])
        assert component["list_requests"] == 1 and component["read_requests"] == (3 if sid == 5 else 1)
        assert component["rejected_messages"]["foreign_system_or_component"] == 2
        assert {p["param_id"] for p in component["parameters"]} == {f"U{sid}_{x}" for x in ("AC" if sid == 5 else "ABC")}
        assert all(json.loads(p["value_json"]) == sid * 100 + p["param_index"] for p in component["parameters"])
        assert set(request_types[sid]) <= {"HEARTBEAT", "PARAM_REQUEST_LIST", "PARAM_REQUEST_READ"}
        assert len(reads[sid]) <= 3 and all(b - a >= .065 for a, b in zip(reads[sid], reads[sid][1:]))
        assert (output / f"uav{sid}.raw").read_bytes() == b"".join(sent[sid])
        replay = mavutil.mavlink_connection(str(output / f"uav{sid}.tlog"))
        frames = []
        try:
            while True:
                m = replay.recv_match(blocking=False)
                if m is None:
                    break
                frames.append(bytes(m.get_msgbuf()))
        finally:
            replay.close()
        assert frames == sent[sid] and {frame[0] for frame in frames} == {0xFE, 0xFD}


def test_workers_default_is_sequential_and_out_of_range_is_rejected(tmp_path):
    import pytest
    from unittest.mock import patch
    from scripts.product import capture_autopilot_params as module
    seen = []
    def empty(args, running):
        seen.append(args.workers)
        return [dict(**ParameterTable(sid, 1, "c-cast").report(), stop_reason="unit-only") for sid in args.sysids]
    with patch.object(module, "capture_all", side_effect=empty):
        assert main(["--output-dir", str(tmp_path / "default")]) == 2
    assert seen == [1]
    for value in ("0", "6"):
        with pytest.raises(SystemExit):
            main(["--output-dir", str(tmp_path / value), "--workers", value])
