from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]

from network.validation.endpoint_transaction import (  # noqa: E402
    CONTRACT,
    DIRECTIONS,
    M2_PROFILE,
    M3_PROFILE,
    TRAFFIC_CLASSES,
    MatrixError,
    build_resolved_matrix,
    canonical_json,
    load_strict_json,
    select_profile,
    sha256_bytes,
    validate_matrix_data,
    validate_matrix_file,
)


MATRIX_PATH = ROOT_DIR / "network/config/endpoint_matrix_5uav.json"
SCHEMA_PATH = ROOT_DIR / "network/config/endpoint_transaction_schema.json"


def load_matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def refresh_cells_hash(matrix: dict) -> None:
    matrix["resolved_cells_sha256"] = sha256_bytes(canonical_json(matrix["cells"]))


class EndpointTransactionMatrixTests(unittest.TestCase):
    def test_tracked_matrix_is_exact_deterministic_30_cell_resolution(self) -> None:
        tracked = validate_matrix_file(MATRIX_PATH)
        self.assertEqual(tracked, build_resolved_matrix())
        self.assertEqual(tracked["contract"], CONTRACT)
        self.assertEqual(len(tracked["cells"]), 5 * 3 * 2)
        self.assertEqual(
            [
                (cell["uav"]["name"], cell["traffic_class"], cell["direction"])
                for cell in tracked["cells"]
            ],
            [
                (f"uav{index}", traffic_class, direction)
                for index in range(1, 6)
                for traffic_class in TRAFFIC_CLASSES
                for direction in DIRECTIONS
            ],
        )

    def test_schema_freezes_contract_and_exact_cell_cardinality(self) -> None:
        schema = load_strict_json(SCHEMA_PATH)
        self.assertEqual(
            schema["$id"],
            "https://ams.local/schemas/endpoint-transaction-v1.json",
        )
        self.assertEqual(schema["properties"]["contract"]["const"], CONTRACT)
        self.assertEqual(schema["properties"]["cells"]["minItems"], 30)
        self.assertEqual(schema["properties"]["cells"]["maxItems"], 30)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["cell"]["additionalProperties"])
        capture_patterns = {
            capture_rule["pattern"]
            for condition in schema["$defs"]["cell"]["allOf"]
            for capture_rule in condition["then"]["properties"]["capture_points"][
                "properties"
            ].values()
        }
        self.assertIn(r"^uav[1-5]\.mavproxy\.tail$", capture_patterns)
        self.assertIn(r"^uav[1-5]\.sink\.eth0$", capture_patterns)
        self.assertIn(r"^uav[1-5]\.source\.eth0$", capture_patterns)

    def test_m2_profile_is_subset_of_same_full_matrix(self) -> None:
        matrix = validate_matrix_file(MATRIX_PATH)
        selected = select_profile(matrix, M2_PROFILE)
        self.assertEqual(selected["profile"]["expected_cell_count"], 2)
        self.assertEqual(
            [cell["cell_id"] for cell in selected["cells"]],
            ["uav1.control.downlink", "uav1.control.uplink"],
        )
        self.assertEqual(
            selected["cells"][0]["capture_points"]["remote_after_adapter"],
            "uav1.mavproxy.tail",
        )
        self.assertEqual(
            selected["cells"][1]["capture_points"]["source_before_adapter"],
            "uav1.mavproxy.tail",
        )
        full = select_profile(matrix, M3_PROFILE)
        self.assertEqual(full["profile"]["expected_cell_count"], 30)
        self.assertEqual(len(full["cells"]), 30)
        self.assertEqual(
            set(selected["profile"]["cell_ids"]),
            set(full["profile"]["cell_ids"][:2]),
        )

    def test_missing_cell_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"].pop()
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("expected exactly 30" in failure for failure in failures))
        self.assertTrue(any("matrix tuple set is not exact" in failure for failure in failures))

    def test_duplicate_cell_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"].append(copy.deepcopy(matrix["cells"][0]))
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("duplicate matrix cells" in failure for failure in failures))
        self.assertTrue(any("reused nonce domain" in failure for failure in failures))

    def test_wrong_system_id_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"][0]["uav"]["system_id"] = 5
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("wrong MAVLink system ID" in failure for failure in failures))

    def test_reused_port_allocation_is_rejected(self) -> None:
        matrix = load_matrix()
        reference = matrix["cells"][0]["transport_ports"]
        for cell in matrix["cells"]:
            if cell["uav"]["name"] != "uav2" or cell["traffic_class"] != "control":
                continue
            cell["transport_ports"] = copy.deepcopy(reference)
            if cell["direction"] == "downlink":
                cell["source"]["udp_port"] = reference["command_post_udp"]
                cell["destination"]["udp_port"] = reference["uav_udp"]
                cell["ns3_path"]["ingress_udp_port"] = reference["ns3_ground_handoff_udp"]
                cell["ns3_path"]["egress_udp_port"] = reference["ns3_uav_handoff_udp"]
            else:
                cell["source"]["udp_port"] = reference["uav_udp"]
                cell["destination"]["udp_port"] = reference["command_post_udp"]
                cell["ns3_path"]["ingress_udp_port"] = reference["ns3_uav_handoff_udp"]
                cell["ns3_path"]["egress_udp_port"] = reference["ns3_ground_handoff_udp"]
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("reused port allocation" in failure for failure in failures))

    def test_reused_nonce_domain_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"][1]["identity"]["nonce_domain"] = matrix["cells"][0]["identity"]["nonce_domain"]
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("reused nonce domain" in failure for failure in failures))

    def test_control_capture_cannot_claim_companion_eth0(self) -> None:
        matrix = load_matrix()
        matrix["cells"][0]["capture_points"]["remote_after_adapter"] = "uav1.sink.eth0"
        matrix["cells"][1]["capture_points"]["source_before_adapter"] = "uav1.source.eth0"
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(
            any("actual endpoint adapter sides" in failure for failure in failures),
            failures,
        )

    def test_payload_capture_must_remain_on_companion_eth0(self) -> None:
        matrix = load_matrix()
        matrix["cells"][2]["capture_points"]["remote_after_adapter"] = "uav1.mavproxy.tail"
        matrix["cells"][3]["capture_points"]["source_before_adapter"] = "uav1.mavproxy.tail"
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(
            any("actual endpoint adapter sides" in failure for failure in failures),
            failures,
        )

    def test_nonexact_traffic_class_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"][0]["traffic_class"] = "CONTROL"
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("nonexact traffic class" in failure for failure in failures))
        self.assertTrue(any("matrix tuple set is not exact" in failure for failure in failures))

    def test_nonexact_direction_is_rejected(self) -> None:
        matrix = load_matrix()
        matrix["cells"][0]["direction"] = "ground_to_air"
        refresh_cells_hash(matrix)
        failures = validate_matrix_data(matrix)
        self.assertTrue(any("nonexact direction" in failure for failure in failures))
        self.assertTrue(any("matrix tuple set is not exact" in failure for failure in failures))

    def test_source_hash_substitution_is_rejected_by_file_validator(self) -> None:
        matrix = load_matrix()
        matrix["source_configs"]["scenario"]["sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matrix.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            with self.assertRaisesRegex(MatrixError, "deterministic source resolution"):
                validate_matrix_file(path)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(MatrixError, "duplicate JSON key"):
                load_strict_json(path)


if __name__ == "__main__":
    unittest.main()
