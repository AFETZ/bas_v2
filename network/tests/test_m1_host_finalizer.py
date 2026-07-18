#!/usr/bin/env python3
"""Mutation tests for the formal M1 host-final trust boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from network.scripts import finalize_m1_host as finalizer
from network.scripts import validate_status_documents as status_validator


IMAGE = "sha256:" + "1" * 64
MAIN_ID = "2" * 64
VALIDATION_ID = "3" * 64
COMMIT = "4" * 40
RUN_ID = "m1_fixture"
M0_CANONICAL = "runs/m0_fixture/metrics/m0_host_final_receipt.json"
M0_SHA256 = "5" * 64


def mount(destination: str, source: Path, writable: bool) -> dict:
    return {
        "Type": "bind",
        "Source": str(source.resolve()),
        "Destination": destination,
        "Mode": "rw" if writable else "ro",
        "RW": writable,
        "Propagation": "rprivate",
    }


class M1HostFinalizerTests(unittest.TestCase):
    def make_paths(self, root: Path) -> dict[str, Path]:
        paths = {
            name: root / name
            for name in ("source", "staging", "project", "identity", "m0_receipt")
        }
        for name in ("source", "staging", "project"):
            paths[name].mkdir()
        (paths["project"] / ".external/ns-3").mkdir(parents=True)
        paths["identity"].write_text(MAIN_ID + "\n", encoding="ascii")
        paths["m0_receipt"].write_text("{}\n", encoding="utf-8")
        return paths

    def main_documents(self, paths: dict[str, Path]) -> tuple[dict, dict]:
        environment = [
            "AMS_CONTAINER_IMAGE=multiagent_simulation:latest",
            f"AMS_CONTAINER_IMAGE_DIGEST={IMAGE}",
            "AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host",
            "AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id",
            "AMS_M1_SOURCE_MODE=clean_git_clone_ro",
            f"AMS_M1_SOURCE_COMMIT={COMMIT}",
            "AMS_M1_PROJECT_OVERLAY_MODE=fresh_run_overlay",
            f"AMS_M1_RUN_ID={RUN_ID}",
            "AMS_M0_CAPABILITY_PROBE_MODE=inherited_m0_host_final",
            "AMS_M1_M0_RECEIPT_PATH=/run/ams/m0-receipt.json",
            f"AMS_M1_M0_RECEIPT_CANONICAL_PATH={M0_CANONICAL}",
            f"AMS_M1_M0_RECEIPT_SHA256={M0_SHA256}",
            f"AMS_M1_M0_STATUS_COMMIT={COMMIT}",
            "GZ_VERSION=harmonic",
            "NVIDIA_VISIBLE_DEVICES=all",
            "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized",
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
                "network/scripts/run_five_uav_health.sh",
            ],
            "WorkingDir": "/workspace/multiagent_simulation",
            "Env": environment,
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
            mount("/workspace/multiagent_simulation", paths["source"], False),
            mount("/workspace/multiagent_simulation/runs", paths["staging"], True),
            mount(
                "/workspace/multiagent_simulation/.external/ns-3",
                paths["project"] / ".external/ns-3",
                False,
            ),
            mount("/run/ams/container_id", paths["identity"], False),
            mount("/run/ams/m0-receipt.json", paths["m0_receipt"], False),
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
        initial = {
            **common,
            "State": {"Status": "created", "Running": False},
        }
        final = {
            **common,
            "State": {
                "Status": "exited",
                "Running": False,
                "OOMKilled": False,
                "ExitCode": 0,
            },
        }
        return initial, final

    def test_main_container_exact_boundary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            initial, final = self.main_documents(paths)
            finalizer.validate_main_container(
                initial,
                final,
                run_id=RUN_ID,
                container_id=MAIN_ID,
                image_digest=IMAGE,
                image_reference="multiagent_simulation:latest",
                source_commit=COMMIT,
                source_snapshot=paths["source"],
                artifact_staging=paths["staging"],
                container_identity_file=paths["identity"],
                m0_receipt_path=paths["m0_receipt"],
                m0_receipt_canonical=M0_CANONICAL,
                m0_receipt_sha256=M0_SHA256,
                project_root=paths["project"],
            )

    def test_receipt_schema_is_an_exact_status_v2_handoff(self) -> None:
        vector = {"vector_sha256": "6" * 64}
        artifact = {"content_sha256": "7" * 64}
        host = {"content_sha256": "8" * 64}
        component = {
            "path": f"runs/{RUN_ID}/metrics/m1_result.json",
            "bytes": 123,
            "sha256": "9" * 64,
        }
        receipt = finalizer.build_m1_receipt(
            run_id=RUN_ID,
            source_commit=COMMIT,
            image_reference="multiagent_simulation:latest",
            image_digest=IMAGE,
            runtime_container_id=MAIN_ID,
            validation_container_id=VALIDATION_ID,
            qualification_content_vector=vector,
            inherited_m0_qualification={"available": True},
            m0_status_authority={"contract": "ams.live-status-lint/v1"},
            component_result=component,
            component_result_sha256=component["sha256"],
            artifact_content_manifest=artifact,
            host_validation_content_manifest=host,
            m0_receipt_sha256="a" * 64,
            m0_status_validation_sha256="b" * 64,
        )
        expected_path = f"runs/{RUN_ID}/metrics/m1_host_final_receipt.json"
        self.assertEqual(set(receipt), status_validator.M1_RECEIPT_TOP_LEVEL_KEYS)
        self.assertEqual(receipt["receipt_path"], expected_path)
        expected_contract = {
            "run_id": RUN_ID,
            "receipt_path": expected_path,
            "source_commit": COMMIT,
            "image_digest": IMAGE,
            "vector_sha256": vector["vector_sha256"],
            "component_result_sha256": component["sha256"],
            "artifact_content_sha256": artifact["content_sha256"],
            "host_validation_content_sha256": host["content_sha256"],
            "m0_receipt_sha256": "a" * 64,
            "m0_status_validation_sha256": "b" * 64,
            "consumed_nodes": ["Q0", "Q1"],
        }
        self.assertEqual(
            receipt["qualification_contract_sha256"],
            finalizer.sha256(finalizer.canonical(expected_contract)),
        )

    def test_main_container_extra_mount_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            initial, final = self.main_documents(paths)
            extra = mount("/unexpected", paths["project"], False)
            initial["Mounts"] = [*initial["Mounts"], extra]
            final["Mounts"] = [*final["Mounts"], extra]
            with self.assertRaisesRegex(ValueError, "mount set is not exact"):
                finalizer.validate_main_container(
                    initial,
                    final,
                    run_id=RUN_ID,
                    container_id=MAIN_ID,
                    image_digest=IMAGE,
                    image_reference="multiagent_simulation:latest",
                    source_commit=COMMIT,
                    source_snapshot=paths["source"],
                    artifact_staging=paths["staging"],
                    container_identity_file=paths["identity"],
                    m0_receipt_path=paths["m0_receipt"],
                    m0_receipt_canonical=M0_CANONICAL,
                    m0_receipt_sha256=M0_SHA256,
                    project_root=paths["project"],
                )

    def test_durable_publication_is_no_replace_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            staging = runs / ".m1-stage-fixture.abcdefghij"
            run = staging / RUN_ID
            run.mkdir(parents=True)
            (run / "result.json").write_text("{}\n", encoding="utf-8")
            destination = runs / RUN_ID
            finalizer.freeze_and_fsync_tree(run)
            finalizer.publish_durable(run, destination, staging, runs)
            self.assertTrue(destination.is_dir())
            self.assertFalse(staging.exists())
            self.assertEqual(destination.stat().st_mode & 0o222, 0)
            self.assertEqual((destination / "result.json").stat().st_mode & 0o222, 0)

    def test_durable_publication_never_replaces_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            staging = runs / ".m1-stage-fixture.abcdefghij"
            run = staging / RUN_ID
            run.mkdir(parents=True)
            (run / "result.json").write_text("candidate\n", encoding="utf-8")
            destination = runs / RUN_ID
            destination.mkdir()
            (destination / "owner.txt").write_text("existing\n", encoding="utf-8")
            finalizer.freeze_and_fsync_tree(run)
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


if __name__ == "__main__":
    unittest.main()
