#!/usr/bin/env python3
"""Tests for the generic downstream acceptance profile boundary."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from network.validation.component_profiles import (
    DEFAULT_PATH,
    load_profiles,
    match_profile,
)


class ComponentProfileTests(unittest.TestCase):
    def test_acceptance_entrypoint_suspends_nounset_only_for_ros_setup(self) -> None:
        entrypoint = (
            Path(__file__).resolve().parents[2]
            / "scripts/acceptance_entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "set +u\n"
            "source /opt/ros/humble/setup.bash\n"
            "source /workspace/ardu_ws/install/setup.bash\n"
            "set -u\n",
            entrypoint,
        )
        self.assertIn(
            "set +u\n"
            "    source install/setup.bash\n"
            "    set -u\n",
            entrypoint,
        )
        self.assertIn(
            "export PATH=/opt/ros/humble/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin\n",
            entrypoint,
        )
        self.assertIn(
            'export PYTHONPATH="/tmp/ams-m0-overlay-${M0_RUN_ID}/install/'
            'multiagent_simulation/lib/python3.10/site-packages:'
            '$AMS_M0_BASE_PYTHONPATH"\n',
            entrypoint,
        )
        self.assertEqual(entrypoint.count("export GZ_IP=127.0.0.1\n"), 1)
        self.assertLess(
            entrypoint.index("export GZ_IP=127.0.0.1\n"),
            entrypoint.index('exec "$@"'),
        )

    def test_root_component_git_checks_use_only_exact_checkout_safe_directory(self) -> None:
        entrypoint = (
            Path(__file__).resolve().parents[2]
            / "scripts/acceptance_entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertEqual(entrypoint.count('safe.directory=$PWD'), 2)
        self.assertNotIn("safe.directory=*", entrypoint)
        self.assertNotIn("git config --global", entrypoint)

    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))

    def write(self, document: dict) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        temporary.close()
        path = Path(temporary.name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_repository_profiles_are_exact_and_match_capacity(self) -> None:
        profiles = load_profiles()
        self.assertEqual(
            set(profiles),
            {
                "flight_capacity_prerequisite",
                "m2_component",
                "m3_component",
                "m4_capacity_prerequisite",
                "m4_component",
            },
        )
        self.assertEqual(
            {name: profile["python_runtime"] for name, profile in profiles.items()},
            {
                "flight_capacity_prerequisite": "base",
                "m2_component": "base",
                "m3_component": "base",
                "m4_capacity_prerequisite": "sionna_rt_cuda",
                "m4_component": "sionna_rt_cuda",
            },
        )
        name, profile = match_profile(
            "network/scripts/run_five_uav_capacity.sh", 600
        )
        self.assertEqual(name, "flight_capacity_prerequisite")
        self.assertEqual(profile["consumed_nodes"], ["Q0", "Q1"])
        self.assertEqual(
            profiles["m2_component"]["required_component_profiles"],
            ["flight_capacity_prerequisite"],
        )
        self.assertEqual(
            profiles["m3_component"]["required_component_profiles"],
            [],
        )
        self.assertEqual(
            profiles["m4_component"]["required_component_profiles"],
            ["m4_capacity_prerequisite"],
        )
        self.assertEqual(
            {
                name: profile["nvidia_driver_capabilities"]
                for name, profile in profiles.items()
            },
            {
                "flight_capacity_prerequisite": "compute,utility",
                "m2_component": "compute,utility",
                "m3_component": "compute,utility",
                "m4_capacity_prerequisite": "compute,utility,graphics",
                "m4_component": "compute,utility,graphics",
            },
        )

    def test_wrong_timeout_or_runner_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            match_profile("network/scripts/run_five_uav_capacity.sh", 601)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            match_profile("network/scripts/not-real.sh", 600)

    def test_duplicate_runner_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m3_component"]["runner"] = document["profiles"][
            "m2_component"
        ]["runner"]
        with self.assertRaisesRegex(ValueError, "duplicated"):
            load_profiles(self.write(document))

    def test_nonprefix_consumed_nodes_are_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m3_component"]["consumed_nodes"] = ["Q0", "Q2"]
        with self.assertRaisesRegex(ValueError, "prefix"):
            load_profiles(self.write(document))

    def test_tun_requires_exact_bounded_capabilities(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m2_component"]["main_cap_add"] = ["NET_ADMIN"]
        with self.assertRaisesRegex(ValueError, "exact capabilities"):
            load_profiles(self.write(document))

    def test_nvidia_capability_matrix_is_exact(self) -> None:
        for profile_name, mutation in (
            ("m3_component", "compute,utility,graphics"),
            ("m4_component", "compute,utility"),
            ("m4_capacity_prerequisite", "all"),
        ):
            with self.subTest(profile=profile_name):
                document = copy.deepcopy(self.document)
                document["profiles"][profile_name][
                    "nvidia_driver_capabilities"
                ] = mutation
                with self.assertRaisesRegex(ValueError, "NVIDIA"):
                    load_profiles(self.write(document))

    def test_python_runtime_matrix_is_exact(self) -> None:
        for profile_name, mutation in (
            ("m3_component", "sionna_rt_cuda"),
            ("m4_component", "base"),
            ("m4_capacity_prerequisite", "host"),
        ):
            with self.subTest(profile=profile_name):
                document = copy.deepcopy(self.document)
                document["profiles"][profile_name]["python_runtime"] = mutation
                with self.assertRaisesRegex(ValueError, "Python runtime"):
                    load_profiles(self.write(document))

    def test_unsafe_path_and_placeholder_are_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m2_component"]["runner"] = "../escape.sh"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            load_profiles(self.write(document))
        document = copy.deepcopy(self.document)
        document["profiles"]["m2_component"]["validator_arguments"].append(
            "{unbound}"
        )
        with self.assertRaisesRegex(ValueError, "arguments"):
            load_profiles(self.write(document))

    def test_duplicate_json_key_is_rejected(self) -> None:
        path = self.write({})
        path.write_text(
            '{"schema_version":1,"schema_version":1,"contract":"x","profiles":{}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            load_profiles(path)

    def test_unknown_or_future_required_profile_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m2_component"]["required_component_profiles"] = [
            "missing_profile"
        ]
        with self.assertRaisesRegex(ValueError, "invalid required profile"):
            load_profiles(self.write(document))

    def test_required_profile_graph_is_exact(self) -> None:
        document = copy.deepcopy(self.document)
        document["profiles"]["m3_component"]["required_component_profiles"] = [
            "m2_component"
        ]
        with self.assertRaisesRegex(ValueError, "invalid required profile"):
            load_profiles(self.write(document))
        document = copy.deepcopy(self.document)
        document["profiles"]["m2_component"]["required_component_profiles"] = [
            "m4_capacity_prerequisite"
        ]
        with self.assertRaisesRegex(ValueError, "invalid required profile"):
            load_profiles(self.write(document))

    def test_direct_receipt_edges_never_cross_status_commit_epochs(self) -> None:
        profiles = load_profiles()
        direct_edges = {
            name: profile["required_component_profiles"]
            for name, profile in profiles.items()
        }
        self.assertEqual(
            direct_edges,
            {
                "flight_capacity_prerequisite": [],
                "m2_component": ["flight_capacity_prerequisite"],
                "m3_component": [],
                "m4_capacity_prerequisite": [],
                "m4_component": ["m4_capacity_prerequisite"],
            },
        )
        for name, required_names in direct_edges.items():
            for required_name in required_names:
                with self.subTest(profile=name, required=required_name):
                    self.assertEqual(
                        profiles[name]["prerequisite_status_contract"],
                        profiles[required_name]["prerequisite_status_contract"],
                    )
                    self.assertEqual(
                        profiles[name]["prerequisite_status_count"],
                        profiles[required_name]["prerequisite_status_count"],
                    )


if __name__ == "__main__":
    unittest.main()
