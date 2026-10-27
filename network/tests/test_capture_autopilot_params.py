"""Protocol fixtures only; no simulator, UART or real autopilot is started."""
import struct

from pymavlink import mavutil

from scripts.product.capture_autopilot_params import ParameterTable, Requests, compare, decode_value


def param(name, count, index, value=1, param_type=6, system=1, component=1):
    sender = mavutil.mavlink.MAVLink(None, srcSystem=system, srcComponent=component)
    frame = sender.param_value_encode(name.encode(), value, param_type, count, index).pack(sender)
    parser = mavutil.mavlink.MAVLink(None)
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
