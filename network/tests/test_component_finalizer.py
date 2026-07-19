#!/usr/bin/env python3
"""Mutation tests for the generic downstream component host boundary."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from network.scripts import finalize_component_host as finalizer
from network.scripts import validate_status_documents as status_validator
from network.validation.component_profiles import load_profiles


IMAGE = "sha256:" + "1" * 64
MAIN_ID = "2" * 64
VALIDATION_ID = "3" * 64
COMMIT = "4" * 40
RUN_ID = "capacity_fixture"


def mount(destination: str, source: Path, writable: bool) -> dict:
    return {
        "Type": "bind",
        "Source": str(source.resolve()),
        "Destination": destination,
        "Mode": "rw" if writable else "ro",
        "RW": writable,
        "Propagation": "rprivate",
    }


class ComponentFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sys_path_before = list(sys.path)
        self.addCleanup(self.assert_sys_path_unchanged)
        self.profile = load_profiles()["flight_capacity_prerequisite"]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = {
            name: root / name
            for name in (
                "source",
                "staging",
                "project",
                "identity",
                "status",
                "prerequisites",
                "m0",
                "m1",
            )
        }
        for name in ("source", "staging", "project"):
            self.paths[name].mkdir()
        (self.paths["project"] / ".external/ns-3").mkdir(parents=True)
        for name in ("identity", "status", "prerequisites", "m0", "m1"):
            self.paths[name].write_text("{}\n", encoding="utf-8")
        self.prerequisite_receipts = {
            "m0": self.paths["m0"],
            "m1": self.paths["m1"],
        }
        self.m0_record = {
            "canonical_path": "runs/m0_fixture/metrics/m0_host_final_receipt.json",
            "sha256": "5" * 64,
        }

    def assert_sys_path_unchanged(self) -> None:
        observed = list(sys.path)
        sys.path[:] = self.sys_path_before
        self.assertEqual(observed, self.sys_path_before)

    def main_documents(self) -> tuple[dict, dict]:
        required = [
            "AMS_CONTAINER_IMAGE=multiagent_simulation:latest",
            f"AMS_CONTAINER_IMAGE_DIGEST={IMAGE}",
            "AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host",
            "AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id",
            "AMS_COMPONENT_PROFILE=flight_capacity_prerequisite",
            "AMS_COMPONENT_SOURCE_MODE=clean_git_clone_ro",
            f"AMS_COMPONENT_SOURCE_COMMIT={COMMIT}",
            f"AMS_COMPONENT_RUN_ID={RUN_ID}",
            "AMS_COMPONENT_STATUS_RESULT_PATH=/run/ams/status-validation.json",
            "AMS_COMPONENT_PREREQUISITES_PATH=/run/ams/prerequisites.json",
            "AMS_M1_SOURCE_MODE=clean_git_clone_ro",
            f"AMS_M1_SOURCE_COMMIT={COMMIT}",
            "AMS_M1_PROJECT_OVERLAY_MODE=fresh_run_overlay",
            f"AMS_M1_RUN_ID={RUN_ID}",
            "AMS_M0_CAPABILITY_PROBE_MODE=inherited_m0_host_final",
            "AMS_M1_M0_RECEIPT_PATH=/run/ams/prerequisites/m0.json",
            f"AMS_M1_M0_RECEIPT_CANONICAL_PATH={self.m0_record['canonical_path']}",
            f"AMS_M1_M0_RECEIPT_SHA256={self.m0_record['sha256']}",
            f"AMS_M1_M0_STATUS_COMMIT={COMMIT}",
            "NVIDIA_VISIBLE_DEVICES=all",
            "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized",
            "GZ_VERSION=harmonic",
        ]
        config = {
            "Image": IMAGE,
            "User": "ubuntu",
            "Entrypoint": ["/ros_entrypoint.sh"],
            "Cmd": [
                "scripts/acceptance_entrypoint.sh",
                "timeout",
                "--signal=TERM",
                "--kill-after=20s",
                "600s",
                "env",
                f"RUN_ID={RUN_ID}",
                "network/scripts/run_five_uav_capacity.sh",
            ],
            "WorkingDir": "/workspace/multiagent_simulation",
            "Env": required,
        }
        host = {
            "Privileged": False,
            "NetworkMode": "host",
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "Devices": [],
            "DeviceRequests": finalizer.EXPECTED_GPU_DEVICE_REQUESTS,
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"},
            "SecurityOpt": ["no-new-privileges:true"],
        }
        mounts = [
            mount("/workspace/multiagent_simulation", self.paths["source"], False),
            mount("/workspace/multiagent_simulation/runs", self.paths["staging"], True),
            mount(
                "/workspace/multiagent_simulation/.external/ns-3",
                self.paths["project"] / ".external/ns-3",
                False,
            ),
            mount("/run/ams/container_id", self.paths["identity"], False),
            mount("/run/ams/status-validation.json", self.paths["status"], False),
            mount("/run/ams/prerequisites.json", self.paths["prerequisites"], False),
            mount("/run/ams/prerequisites/m0.json", self.paths["m0"], False),
            mount("/run/ams/prerequisites/m1.json", self.paths["m1"], False),
        ]
        common = {
            "Id": MAIN_ID,
            "Image": IMAGE,
            "RestartCount": 0,
            "Config": config,
            "HostConfig": host,
            "Mounts": mounts,
            "Path": "/ros_entrypoint.sh",
            "Args": config["Cmd"],
        }
        return (
            {**common, "State": {"Status": "created", "Running": False}},
            {
                **common,
                "State": {
                    "Status": "exited",
                    "Running": False,
                    "OOMKilled": False,
                    "ExitCode": 0,
                },
            },
        )

    def validate_main(self, initial: dict, final: dict) -> None:
        finalizer.validate_main_container(
            initial,
            final,
            profile_name="flight_capacity_prerequisite",
            profile=self.profile,
            run_id=RUN_ID,
            container_id=MAIN_ID,
            image_reference="multiagent_simulation:latest",
            image_digest=IMAGE,
            source_commit=COMMIT,
            source_snapshot=self.paths["source"],
            artifact_staging=self.paths["staging"],
            container_identity_file=self.paths["identity"],
            status_result=self.paths["status"],
            prerequisites_path=self.paths["prerequisites"],
            prerequisite_receipts=self.prerequisite_receipts,
            project_root=self.paths["project"],
            m0_record=self.m0_record,
        )

    @staticmethod
    def sionna_runtime_provenance() -> dict:
        versions = {"sionna-rt": "1.0.2", "mitsuba": "3.6.2", "numpy": "1.26.4"}
        return {
            "implementation": {
                "packet_ingress_mode": "tap_bridge_external",
                "medium_model": "csma_surrogate",
                "radio_provider_id": "tcp_jsonl_real_sionna",
                "radio_provider_runtime_consumed": True,
                "runtime_provider_id": "tcp_jsonl_real_sionna",
                "reason": "profile_m4_runtime",
            },
            "dependency_versions": {
                **versions,
                "python_runtime": {
                    "contract": "ams.component-python-runtime/v1",
                    "profile": "sionna_rt_cuda",
                    "status": "passed",
                    "python_no_user_site": "1",
                    "pythonpath": (
                        "/workspace/multiagent_simulation:"
                        "/workspace/ardu_ws/install/ardupilot_msgs/lib/python3.10/site-packages:"
                        "/opt/ros/humble/lib/python3.10/site-packages:"
                        "/home/ubuntu/.local/lib/python3.10/site-packages"
                    ),
                    "pythonpath_entries": [
                        "/workspace/multiagent_simulation",
                        "/workspace/ardu_ws/install/ardupilot_msgs/lib/python3.10/site-packages",
                        "/opt/ros/humble/lib/python3.10/site-packages",
                        "/home/ubuntu/.local/lib/python3.10/site-packages",
                    ],
                    "executable": {
                        "configured_path": "/usr/bin/python3.10",
                        "realpath": "/usr/bin/python3.10",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    },
                    "modules": {
                        "sionna.rt": {
                            "distribution": "sionna-rt",
                            "origin": "/home/ubuntu/.local/lib/python3.10/site-packages/sionna/rt/__init__.py",
                            "sha256": "b" * 64,
                            "size_bytes": 2,
                            "version": versions["sionna-rt"],
                        },
                        "mitsuba": {
                            "distribution": "mitsuba",
                            "origin": "/home/ubuntu/.local/lib/python3.10/site-packages/mitsuba/__init__.py",
                            "sha256": "c" * 64,
                            "size_bytes": 3,
                            "version": versions["mitsuba"],
                        },
                        "numpy": {
                            "distribution": "numpy",
                            "origin": "/usr/lib/python3/dist-packages/numpy/__init__.py",
                            "sha256": "d" * 64,
                            "size_bytes": 4,
                            "version": versions["numpy"],
                        },
                    },
                },
            }
        }

    def test_capacity_main_container_exact_boundary_passes(self) -> None:
        initial, final = self.main_documents()
        initial["HostConfig"]["OomKillDisable"] = False
        final["HostConfig"]["OomKillDisable"] = None
        final["Mounts"].reverse()
        self.validate_main(initial, final)
        initial["HostConfig"]["OomKillDisable"] = True
        final["HostConfig"]["OomKillDisable"] = True
        with self.assertRaisesRegex(ValueError, "HostConfig"):
            self.validate_main(initial, final)

    def test_m4_main_container_requires_profile_graphics_device_request(self) -> None:
        profile = load_profiles()["m4_capacity_prerequisite"]
        initial, final = self.main_documents()
        for document in (initial, final):
            config = document["Config"]
            config["User"] = "root:1000"
            config["Env"] = [
                "AMS_COMPONENT_PROFILE=m4_capacity_prerequisite"
                if value == "AMS_COMPONENT_PROFILE=flight_capacity_prerequisite"
                else "AMS_M0_CAPABILITY_PROBE_MODE=bounded_root_in_runtime"
                if value
                == "AMS_M0_CAPABILITY_PROBE_MODE=inherited_m0_host_final"
                else "NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics"
                if value == "NVIDIA_DRIVER_CAPABILITIES=compute,utility"
                else value
                for value in config["Env"]
            ]
            config["Cmd"][4] = f"{profile['timeout_s']}s"
            config["Cmd"][7] = "network/scripts/run_m4_capacity.sh"
            host = document["HostConfig"]
            host["NetworkMode"] = "none"
            host["CapAdd"] = [
                "CAP_CHOWN",
                "CAP_DAC_READ_SEARCH",
                "CAP_NET_ADMIN",
                "CAP_NET_RAW",
                "CAP_SYS_ADMIN",
            ]
            host["Devices"] = [
                {
                    "PathOnHost": "/dev/net/tun",
                    "PathInContainer": "/dev/net/tun",
                    "CgroupPermissions": "rwm",
                }
            ]
            host["DeviceRequests"] = (
                finalizer.expected_gpu_device_requests("compute,utility,graphics")
            )
            host["Tmpfs"]["/run/netns"] = (
                "rw,nosuid,nodev,noexec,size=16m,mode=0755"
            )
            host["SecurityOpt"] = [
                "no-new-privileges:true",
                "apparmor=unconfined",
            ]
        finalizer.validate_main_container(
            initial,
            final,
            profile_name="m4_capacity_prerequisite",
            profile=profile,
            run_id=RUN_ID,
            container_id=MAIN_ID,
            image_reference="multiagent_simulation:latest",
            image_digest=IMAGE,
            source_commit=COMMIT,
            source_snapshot=self.paths["source"],
            artifact_staging=self.paths["staging"],
            container_identity_file=self.paths["identity"],
            status_result=self.paths["status"],
            prerequisites_path=self.paths["prerequisites"],
            prerequisite_receipts=self.prerequisite_receipts,
            project_root=self.paths["project"],
            m0_record=self.m0_record,
        )
        for document in (initial, final):
            document["HostConfig"]["DeviceRequests"] = (
                finalizer.expected_gpu_device_requests("compute,utility")
            )
        with self.assertRaisesRegex(ValueError, "HostConfig"):
            finalizer.validate_main_container(
                initial,
                final,
                profile_name="m4_capacity_prerequisite",
                profile=profile,
                run_id=RUN_ID,
                container_id=MAIN_ID,
                image_reference="multiagent_simulation:latest",
                image_digest=IMAGE,
                source_commit=COMMIT,
                source_snapshot=self.paths["source"],
                artifact_staging=self.paths["staging"],
                container_identity_file=self.paths["identity"],
                status_result=self.paths["status"],
                prerequisites_path=self.paths["prerequisites"],
                prerequisite_receipts=self.prerequisite_receipts,
                project_root=self.paths["project"],
                m0_record=self.m0_record,
            )

    def test_live_status_rederives_component_main_container_boundary(self) -> None:
        initial, final = self.main_documents()
        control = self.paths["project"].parent / (
            f".ams-component-control-{RUN_ID}.ABC123"
        )
        replacement_sources = {
            "/workspace/multiagent_simulation": "/tmp/ams-component-source.ABC123",
            "/workspace/multiagent_simulation/runs": str(
                self.paths["project"]
                / "runs"
                / f".component-stage-{RUN_ID}.ABC123"
            ),
            "/workspace/multiagent_simulation/.external/ns-3": str(
                self.paths["project"] / ".external/ns-3"
            ),
            "/run/ams/container_id": "/tmp/ams-container-id.ABC123",
            "/run/ams/status-validation.json": str(control / "status_validation.json"),
            "/run/ams/prerequisites.json": str(control / "prerequisites.json"),
            "/run/ams/prerequisites/m0.json": str(self.paths["m0"]),
            "/run/ams/prerequisites/m1.json": str(self.paths["m1"]),
        }
        for document in (initial, final):
            for item in document["Mounts"]:
                item["Source"] = replacement_sources[item["Destination"]]
        payloads = {
            "main/initial_container_inspect.json": json.dumps([initial]).encode(),
            "main/final_container_inspect.json": json.dumps([final]).encode(),
            "main/initial_image_inspect.json": json.dumps([{"Id": IMAGE}]).encode(),
            "main/final_image_inspect.json": json.dumps([{"Id": IMAGE}]).encode(),
        }
        receipt = {
            "image_reference": "multiagent_simulation:latest",
            "image_digest": IMAGE,
            "container_id": MAIN_ID,
            "source_commit": COMMIT,
            "prerequisite_receipts": {
                "m0": {
                    **self.m0_record,
                    "host_path": str(self.paths["m0"]),
                },
                "m1": {"host_path": str(self.paths["m1"])},
            },
            "required_component_receipts": {},
        }
        self.assertEqual(
            status_validator._validate_component_main_container(
                self.paths["project"],
                payloads,
                profile_name="flight_capacity_prerequisite",
                profile=self.profile,
                run_id=RUN_ID,
                receipt=receipt,
            ),
            [],
        )
        for document in (initial, final):
            document["HostConfig"]["DeviceRequests"] = (
                finalizer.expected_gpu_device_requests("compute,utility,graphics")
            )
        payloads["main/initial_container_inspect.json"] = json.dumps([initial]).encode()
        payloads["main/final_container_inspect.json"] = json.dumps([final]).encode()
        failures = status_validator._validate_component_main_container(
            self.paths["project"],
            payloads,
            profile_name="flight_capacity_prerequisite",
            profile=self.profile,
            run_id=RUN_ID,
            receipt=receipt,
        )
        self.assertTrue(any("Config/HostConfig" in failure for failure in failures))

    def test_live_status_rebinds_copied_prerequisite_receipt_bytes(self) -> None:
        config_source = (
            Path(__file__).resolve().parents[1]
            / "config/component_acceptance_profiles.json"
        )
        config_target = (
            self.paths["project"]
            / "network/config/component_acceptance_profiles.json"
        )
        config_target.parent.mkdir(parents=True)
        config_target.write_bytes(config_source.read_bytes())
        status_result = {
            "schema_version": 1,
            "contract": "ams.live-status-lint/v1",
            "passed": True,
            "failures": [],
            "report_commit": COMMIT,
            "status_paths": list(status_validator.STATUS_PATHS),
        }
        status_payload = json.dumps(status_result, indent=2, sort_keys=True).encode() + b"\n"
        payloads = {"status/validation.json": status_payload}
        records = {}
        for index in range(2):
            name = f"m{index}"
            run_id = f"{name}_fixture"
            relative = f"runs/{run_id}/metrics/{name}_host_final_receipt.json"
            receipt = {
                "contract": f"ams.{name}.host-final-receipt/v1",
                "run_id": run_id,
                "receipt_path": relative,
                "formal_accepted": True,
                "passed": True,
                "failures": [],
            }
            raw = json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n"
            path = self.paths["project"] / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            path.chmod(0o400)
            payloads[f"status/receipts/{name}.json"] = raw
            records[name] = {
                "milestone": name.upper(),
                "canonical_path": relative,
                "host_path": str(path),
                "sha256": status_validator._sha256(raw),
                "contract": receipt["contract"],
                "run_id": run_id,
            }
        prerequisites = {
            "schema_version": 1,
            "contract": "ams.component-prerequisites/v1",
            "profile": "flight_capacity_prerequisite",
            "source_commit": COMMIT,
            "status": {
                "contract": "ams.live-status/v2",
                "closed_count": 2,
                "result_path": str(self.paths["status"]),
                "result_sha256": status_validator._sha256(status_payload),
                "report_commit": COMMIT,
            },
            "receipts": records,
            "component_receipts": {},
        }
        self.assertEqual(
            status_validator._validate_component_prerequisite_authority(
                self.paths["project"],
                payloads,
                prerequisites=prerequisites,
                profile_name="flight_capacity_prerequisite",
                profile=self.profile,
                execution_commit=COMMIT,
                expected_vector={},
            ),
            [],
        )
        payloads["status/receipts/m1.json"] += b" "
        failures = status_validator._validate_component_prerequisite_authority(
            self.paths["project"],
            payloads,
            prerequisites=prerequisites,
            profile_name="flight_capacity_prerequisite",
            profile=self.profile,
            execution_commit=COMMIT,
            expected_vector={},
        )
        self.assertTrue(any("milestone receipt differs: m1" in item for item in failures))

        payloads["status/receipts/m1.json"] = (
            self.paths["project"] / records["m1"]["canonical_path"]
        ).read_bytes()
        capacity_profile = load_profiles()["flight_capacity_prerequisite"]
        capacity_run = "capacity_dependency"
        capacity_relative = (
            f"runs/{capacity_run}/metrics/{capacity_profile['receipt_name']}"
        )
        capacity_receipt = {
            "contract": capacity_profile["receipt_contract"],
            "profile": "flight_capacity_prerequisite",
            "run_id": capacity_run,
            "receipt_path": capacity_relative,
            "source_commit": COMMIT,
            "formal_accepted": True,
            "passed": True,
            "failures": [],
        }
        capacity_raw = (
            json.dumps(capacity_receipt, indent=2, sort_keys=True).encode() + b"\n"
        )
        capacity_path = self.paths["project"] / capacity_relative
        capacity_path.parent.mkdir(parents=True)
        capacity_path.write_bytes(capacity_raw)
        capacity_path.chmod(0o400)
        payloads["status/receipts/flight_capacity_prerequisite.json"] = capacity_raw
        prerequisites["profile"] = "m2_component"
        prerequisites["component_receipts"] = {
            "flight_capacity_prerequisite": {
                "profile": "flight_capacity_prerequisite",
                "canonical_path": capacity_relative,
                "host_path": str(capacity_path),
                "sha256": status_validator._sha256(capacity_raw),
                "contract": capacity_profile["receipt_contract"],
                "run_id": capacity_run,
            }
        }
        with mock.patch.object(
            status_validator, "_validate_component_receipt", return_value=[]
        ) as recursive:
            self.assertEqual(
                status_validator._validate_component_prerequisite_authority(
                    self.paths["project"],
                    payloads,
                    prerequisites=prerequisites,
                    profile_name="m2_component",
                    profile=load_profiles()["m2_component"],
                    execution_commit=COMMIT,
                    expected_vector={"sentinel": True},
                ),
                [],
            )
            recursive.assert_called_once()

    def test_durable_publication_is_no_replace_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            staging = runs / ".component-stage-fixture.abcdefghij"
            run = staging / RUN_ID
            run.mkdir(parents=True)
            (run / "result.json").write_text("{}\n", encoding="utf-8")
            destination = runs / RUN_ID
            finalizer.freeze_tree(run)
            finalizer.fsync_tree(run)
            finalizer.publish_durable(run, destination, staging, runs)
            self.assertTrue(destination.is_dir())
            self.assertFalse(staging.exists())
            self.assertEqual(destination.stat().st_mode & 0o222, 0)
            self.assertEqual((destination / "result.json").stat().st_mode & 0o222, 0)

    def test_durable_publication_never_replaces_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            staging = runs / ".component-stage-fixture.abcdefghij"
            run = staging / RUN_ID
            run.mkdir(parents=True)
            (run / "result.json").write_text("candidate\n", encoding="utf-8")
            destination = runs / RUN_ID
            destination.mkdir()
            (destination / "owner.txt").write_text("existing\n", encoding="utf-8")
            finalizer.freeze_tree(run)
            finalizer.fsync_tree(run)
            with self.assertRaises(FileExistsError):
                finalizer.publish_durable(run, destination, staging, runs)
            self.assertEqual(
                (destination / "owner.txt").read_text(encoding="utf-8"),
                "existing\n",
            )
            self.assertEqual(
                [path.name for path in runs.iterdir() if ".publish-" in path.name],
                [],
            )

    def test_tun_profile_requires_exact_root_capability_boundary(self) -> None:
        profile = load_profiles()["m2_component"]
        initial, final = self.main_documents()
        for document in (initial, final):
            config = document["Config"]
            config["User"] = "root:1000"
            config["Env"] = [
                "AMS_COMPONENT_PROFILE=m2_component"
                if value == "AMS_COMPONENT_PROFILE=flight_capacity_prerequisite"
                else "AMS_M0_CAPABILITY_PROBE_MODE=bounded_root_in_runtime"
                if value
                == "AMS_M0_CAPABILITY_PROBE_MODE=inherited_m0_host_final"
                else value
                for value in config["Env"]
            ]
            config["Cmd"][4] = "600s"
            config["Cmd"][7] = "network/scripts/run_one_uav_vertical_slice.sh"
            host = document["HostConfig"]
            host["NetworkMode"] = "none"
            host["CapAdd"] = [
                "CAP_CHOWN",
                "CAP_DAC_READ_SEARCH",
                "CAP_NET_ADMIN",
                "CAP_NET_RAW",
                "CAP_SYS_ADMIN",
            ]
            host["Devices"] = [
                {
                    "PathOnHost": "/dev/net/tun",
                    "PathInContainer": "/dev/net/tun",
                    "CgroupPermissions": "rwm",
                }
            ]
            host["Tmpfs"]["/run/netns"] = (
                "rw,nosuid,nodev,noexec,size=16m,mode=0755"
            )
            host["SecurityOpt"] = [
                "no-new-privileges:true",
                "apparmor=unconfined",
            ]
        finalizer.validate_main_container(
            initial,
            final,
            profile_name="m2_component",
            profile=profile,
            run_id=RUN_ID,
            container_id=MAIN_ID,
            image_reference="multiagent_simulation:latest",
            image_digest=IMAGE,
            source_commit=COMMIT,
            source_snapshot=self.paths["source"],
            artifact_staging=self.paths["staging"],
            container_identity_file=self.paths["identity"],
            status_result=self.paths["status"],
            prerequisites_path=self.paths["prerequisites"],
            prerequisite_receipts=self.prerequisite_receipts,
            project_root=self.paths["project"],
            m0_record=self.m0_record,
        )

    def test_gpu_or_extra_ams_environment_mutation_fails(self) -> None:
        initial, final = self.main_documents()
        mutated_initial = copy.deepcopy(initial)
        mutated_final = copy.deepcopy(final)
        mutated_initial["Config"]["Env"].append("AMS_UNDECLARED=1")
        mutated_final["Config"]["Env"].append("AMS_UNDECLARED=1")
        with self.assertRaisesRegex(ValueError, "environment"):
            self.validate_main(mutated_initial, mutated_final)
        initial, final = self.main_documents()
        initial["HostConfig"]["DeviceRequests"] = []
        final["HostConfig"]["DeviceRequests"] = []
        with self.assertRaisesRegex(ValueError, "HostConfig"):
            self.validate_main(initial, final)

    def test_privileged_or_extra_mount_fails(self) -> None:
        initial, final = self.main_documents()
        initial["HostConfig"]["Privileged"] = True
        final["HostConfig"]["Privileged"] = True
        with self.assertRaisesRegex(ValueError, "HostConfig"):
            self.validate_main(initial, final)
        initial, final = self.main_documents()
        extra = Path(self.temp.name) / "extra"
        extra.write_text("x", encoding="utf-8")
        initial["Mounts"].append(mount("/tmp/extra", extra, False))
        final["Mounts"].append(mount("/tmp/extra", extra, False))
        with self.assertRaisesRegex(ValueError, "mount"):
            self.validate_main(initial, final)

    def test_result_requires_independent_equality_and_all_gates(self) -> None:
        result = {
            "schema_version": 1,
            "contract": "ams.flight-capacity-validation/v1",
            "passed": True,
            "failures": [],
            "gates": {"rtf": {"passed": True, "failures": []}},
        }
        payload = json.dumps(result).encode("utf-8")
        self.assertEqual(
            finalizer.validate_component_result(self.profile, payload, payload), result
        )
        mutated = copy.deepcopy(result)
        mutated["gates"]["rtf"]["passed"] = False
        with self.assertRaisesRegex(ValueError, "gate"):
            finalizer.validate_component_result(
                self.profile, json.dumps(mutated).encode("utf-8"), json.dumps(mutated).encode("utf-8")
            )
        with self.assertRaisesRegex(ValueError, "differ"):
            finalizer.validate_component_result(
                self.profile, payload, json.dumps({**result, "extra": 1}).encode("utf-8")
            )

    def test_finalizer_requires_exact_profile_bound_sionna_runtime(self) -> None:
        m4_profile = load_profiles()["m4_component"]
        provenance = self.sionna_runtime_provenance()
        finalizer.validate_provenance_python_runtime(m4_profile, provenance)
        finalizer.validate_provenance_python_runtime(self.profile, {"dependency_versions": {}})

        mutations = {
            "missing": lambda value: value["dependency_versions"].pop("python_runtime"),
            "pythonpath": lambda value: value["dependency_versions"]["python_runtime"].update(
                {"pythonpath": "/tmp/forged"}
            ),
            "executable": lambda value: value["dependency_versions"]["python_runtime"][
                "executable"
            ].update({"realpath": "/usr/bin/python3.11"}),
            "module_origin": lambda value: value["dependency_versions"]["python_runtime"][
                "modules"
            ]["sionna.rt"].update({"origin": "/tmp/sionna.py"}),
            "module_hash": lambda value: value["dependency_versions"]["python_runtime"][
                "modules"
            ]["mitsuba"].update({"sha256": "not-a-hash"}),
            "module_version": lambda value: value["dependency_versions"]["python_runtime"][
                "modules"
            ]["numpy"].update({"version": "0.0"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                candidate = copy.deepcopy(provenance)
                mutate(candidate)
                with self.assertRaisesRegex(ValueError, "Python runtime"):
                    finalizer.validate_provenance_python_runtime(m4_profile, candidate)

        with self.assertRaisesRegex(ValueError, "undeclared"):
            finalizer.validate_provenance_python_runtime(self.profile, provenance)

    def test_finalizer_enforces_truthful_profile_aware_provider_consumption(self) -> None:
        m4 = self.sionna_runtime_provenance()
        finalizer.validate_provenance_implementation("m4_component", m4)
        for field, replacement in (
            ("radio_provider_runtime_consumed", False),
            ("runtime_provider_id", "not_applicable_pre_m4"),
            ("reason", "profile_pre_m4"),
        ):
            with self.subTest(m4_mutation=field):
                candidate = copy.deepcopy(m4)
                candidate["implementation"][field] = replacement
                with self.assertRaisesRegex(ValueError, "provider-consumption"):
                    finalizer.validate_provenance_implementation(
                        "m4_component", candidate
                    )

        m2 = copy.deepcopy(m4)
        m2["implementation"].update(
            {
                "radio_provider_runtime_consumed": False,
                "runtime_provider_id": "not_applicable_pre_m4",
                "reason": "profile_pre_m4",
            }
        )
        finalizer.validate_provenance_implementation("m2_component", m2)
        m2["implementation"]["radio_provider_runtime_consumed"] = True
        m2["implementation"]["runtime_provider_id"] = "tcp_jsonl_real_sionna"
        with self.assertRaisesRegex(ValueError, "provider-consumption"):
            finalizer.validate_provenance_implementation("m2_component", m2)

    def test_prerequisite_argument_names_cover_auxiliary_receipts_safely(self) -> None:
        parsed = finalizer.parse_receipt_arguments(
            [
                f"m0={self.paths['m0']}",
                f"flight_capacity_prerequisite={self.paths['m1']}",
            ]
        )
        self.assertEqual(
            set(parsed), {"m0", "flight_capacity_prerequisite"}
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            finalizer.parse_receipt_arguments([f"../escape={self.paths['m0']}"])


if __name__ == "__main__":
    unittest.main()
