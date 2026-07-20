#!/usr/bin/env python3
"""Adversarial tests for host-side Ed25519 evidence attestation."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.attest_run_evidence import (  # noqa: E402
    attest_run_evidence,
)
from network.validation.evidence_attestation import (  # noqa: E402
    ATTESTATION_RELATIVE_PATH,
    SIGNATURE_RELATIVE_PATH,
    AttestationError,
    TrustedPublicKey,
    canonical_attestation_payload,
    encode_signature,
    public_key_fingerprint,
    sign_payload,
    verify_evidence_attestation,
)


OPENSSL_AVAILABLE = shutil.which("openssl") is not None


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _replace_readonly(path: Path, payload: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(payload)
    path.chmod(0o444)


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.run_dir = root / "attested_run"
        self.ledger_dir = root / "external_ledger"
        self.key_dir = root / "host_keys"
        self.ledger_dir.mkdir(parents=True)
        self.key_dir.mkdir(parents=True)
        self.private_key = self.key_dir / "private.pem"
        self.public_key = self.key_dir / "public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.private_key)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(self.private_key),
                "-pubout",
                "-out",
                str(self.public_key),
            ],
            check=True,
            capture_output=True,
        )
        self.private_key.chmod(0o600)
        self.fingerprint = public_key_fingerprint(self.public_key)
        self.container_id = "a" * 64
        self.image_id = "sha256:" + "b" * 64
        self.source_hash = "c" * 64
        self.runtime_id = "runtime-attestation-0001"
        self.matrix_sha256 = hashlib.sha256(
            (ROOT_DIR / "network/config/validation_matrix.yaml").read_bytes()
        ).hexdigest()
        provenance = {
            "schema_version": 2,
            "run_id": self.run_dir.name,
            "source_hash": self.source_hash,
            "container_image": {
                "reference": "ams:fixture",
                "digest": self.image_id,
                "runtime_container_id": self.container_id,
            },
        }
        joint_runtime = {
            "schema_version": 2,
            "run_id": self.run_dir.name,
            "runtime_id": self.runtime_id,
            "source_hash": self.source_hash,
        }
        provenance_path = self.run_dir / "metrics/provenance.json"
        joint_path = self.run_dir / "metrics/joint_runtime.json"
        _write(provenance_path, _json_bytes(provenance))
        _write(joint_path, _json_bytes(joint_runtime))
        manifest = {
            "schema_version": 2,
            "run_id": self.run_dir.name,
            "runtime_id": self.runtime_id,
            "source_hash": self.source_hash,
            "sealed_utc": "2026-01-01T00:00:00Z",
            "matrix_sha256": self.matrix_sha256,
            "files": {
                "metrics/provenance.json": {
                    "sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
                    "size_bytes": provenance_path.stat().st_size,
                },
                "metrics/joint_runtime.json": {
                    "sha256": hashlib.sha256(joint_path.read_bytes()).hexdigest(),
                    "size_bytes": joint_path.stat().st_size,
                },
            },
        }
        _write(self.run_dir / "metrics/evidence_manifest.json", _json_bytes(manifest))
        provenance_path.chmod(0o444)
        joint_path.chmod(0o444)
        (self.run_dir / "metrics/evidence_manifest.json").chmod(0o444)
        self.inspect_calls: list[str] = []
        self.inspect_record = {
            "Id": self.container_id,
            "Image": self.image_id,
            "RestartCount": 0,
            "State": {
                "Status": "exited",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "FinishedAt": "2026-01-01T01:00:00.000000000Z",
            },
        }

    def inspector(self, container_id: str) -> dict[str, object]:
        self.inspect_calls.append(container_id)
        return copy.deepcopy(self.inspect_record)

    def attest(self, *, ledger: bool = True) -> dict[str, object]:
        return attest_run_evidence(
            self.run_dir,
            self.private_key,
            self.public_key,
            key_id="host-main",
            expected_public_key_sha256=self.fingerprint,
            ledger_dir=self.ledger_dir if ledger else None,
            docker_inspector=self.inspector,
        )

    def trust(self) -> dict[str, TrustedPublicKey]:
        return {
            "host-main": TrustedPublicKey(
                path=self.public_key,
                public_key_sha256=self.fingerprint,
            )
        }


@unittest.skipUnless(OPENSSL_AVAILABLE, "OpenSSL is required for Ed25519 tests")
class EvidenceAttestationV2Tests(unittest.TestCase):
    def test_positive_attestation_binds_full_identity_and_external_ledger(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            result = fixture.attest()
            self.assertEqual(fixture.inspect_calls, [fixture.container_id, fixture.container_id])
            claims = result["claims"]
            self.assertEqual(claims["container_id"], fixture.container_id)
            self.assertEqual(claims["image_id"], fixture.image_id)
            self.assertEqual(claims["source_hash"], fixture.source_hash)
            self.assertEqual(claims["matrix_sha256"], fixture.matrix_sha256)
            self.assertRegex(claims["nonce"], r"^[0-9a-f]{64}$")
            verification = verify_evidence_attestation(
                fixture.run_dir,
                fixture.trust(),
                ledger_dir=fixture.ledger_dir,
            )
            self.assertEqual(verification["status"], "passed", verification)
            self.assertTrue(verification["details"]["ledger_verified"])

    def test_manifest_or_signature_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            fixture.attest()
            manifest_path = fixture.run_dir / "metrics/evidence_manifest.json"
            original_manifest = manifest_path.read_bytes()
            _replace_readonly(manifest_path, original_manifest + b" ")
            verification = verify_evidence_attestation(fixture.run_dir, fixture.trust())
            self.assertEqual(verification["status"], "failed")
            self.assertIn("manifest hash", "\n".join(verification["details"]["failures"]))
            _replace_readonly(manifest_path, original_manifest)

            signature_path = fixture.run_dir / SIGNATURE_RELATIVE_PATH
            signature = bytearray(signature_path.read_bytes())
            signature[0] = ord("A") if signature[0] != ord("A") else ord("B")
            _replace_readonly(signature_path, bytes(signature))
            verification = verify_evidence_attestation(fixture.run_dir, fixture.trust())
            self.assertEqual(verification["status"], "failed")

    def test_manifest_listed_raw_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            fixture.attest()
            raw_path = fixture.run_dir / "metrics/joint_runtime.json"
            _replace_readonly(raw_path, raw_path.read_bytes() + b" ")
            verification = verify_evidence_attestation(
                fixture.run_dir, fixture.trust()
            )
            self.assertEqual(verification["status"], "failed", verification)
            self.assertIn(
                "raw evidence does not match manifest",
                "\n".join(verification["details"]["failures"]),
            )

    def test_existing_outputs_and_external_ledger_forbid_resigning(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            first = fixture.attest()
            ledger_path = Path(first["ledger_path"])
            ledger_bytes = ledger_path.read_bytes()
            with self.assertRaisesRegex(AttestationError, "re-sign"):
                fixture.attest()

            (fixture.run_dir / ATTESTATION_RELATIVE_PATH).unlink()
            (fixture.run_dir / SIGNATURE_RELATIVE_PATH).unlink()
            with self.assertRaisesRegex(AttestationError, "re-sign"):
                fixture.attest()
            self.assertFalse((fixture.run_dir / ATTESTATION_RELATIVE_PATH).exists())
            self.assertFalse((fixture.run_dir / SIGNATURE_RELATIVE_PATH).exists())
            self.assertEqual(ledger_path.read_bytes(), ledger_bytes)

    def test_unknown_or_substituted_key_is_never_trusted(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            fixture.attest(ledger=False)
            unknown = verify_evidence_attestation(fixture.run_dir, {})
            self.assertEqual(unknown["status"], "failed")
            self.assertIn("unknown", "\n".join(unknown["details"]["failures"]))

            rogue_private = fixture.key_dir / "rogue-private.pem"
            rogue_public = fixture.key_dir / "rogue-public.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(rogue_private)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    str(rogue_private),
                    "-pubout",
                    "-out",
                    str(rogue_public),
                ],
                check=True,
                capture_output=True,
            )
            rogue_trust = {
                "host-main": TrustedPublicKey(
                    rogue_public, public_key_fingerprint(rogue_public)
                )
            }
            substituted = verify_evidence_attestation(fixture.run_dir, rogue_trust)
            self.assertEqual(substituted["status"], "failed")
            self.assertIn("fingerprint", "\n".join(substituted["details"]["failures"]))

    def test_rogue_resigned_claims_and_malformed_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            result = fixture.attest(ledger=False)
            claims = dict(result["claims"])
            rogue_private = fixture.key_dir / "rogue-private.pem"
            rogue_public = fixture.key_dir / "rogue-public.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(rogue_private)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    str(rogue_private),
                    "-pubout",
                    "-out",
                    str(rogue_public),
                ],
                check=True,
                capture_output=True,
            )
            rogue_private.chmod(0o600)
            claims["key_id"] = "rogue"
            claims["public_key_sha256"] = public_key_fingerprint(rogue_public)
            signature = sign_payload(rogue_private, canonical_attestation_payload(claims))
            _replace_readonly(
                fixture.run_dir / ATTESTATION_RELATIVE_PATH, _json_bytes(claims)
            )
            _replace_readonly(
                fixture.run_dir / SIGNATURE_RELATIVE_PATH, encode_signature(signature)
            )
            verification = verify_evidence_attestation(fixture.run_dir, fixture.trust())
            self.assertEqual(verification["status"], "failed")
            self.assertIn("unknown", "\n".join(verification["details"]["failures"]))

            malformed = (
                '{"schema_version":1,"schema_version":1,"attestation_type":"bad"}\n'
            ).encode("utf-8")
            _replace_readonly(fixture.run_dir / ATTESTATION_RELATIVE_PATH, malformed)
            verification = verify_evidence_attestation(fixture.run_dir, fixture.trust())
            self.assertEqual(verification["status"], "failed")
            self.assertIn("duplicate", "\n".join(verification["details"]["failures"]))

    def test_running_or_wrong_image_container_cannot_be_attested(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            fixture.inspect_record["State"]["Running"] = True
            fixture.inspect_record["State"]["Status"] = "running"
            with self.assertRaisesRegex(AttestationError, "still running"):
                fixture.attest(ledger=False)
            self.assertFalse((fixture.run_dir / ATTESTATION_RELATIVE_PATH).exists())

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            fixture.inspect_record["Image"] = "sha256:" + "d" * 64
            with self.assertRaisesRegex(AttestationError, "Image does not match"):
                fixture.attest(ledger=False)

    def test_caller_fingerprint_pin_is_mandatory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            fixture = Fixture(Path(temporary))
            with self.assertRaisesRegex(AttestationError, "caller-pinned"):
                attest_run_evidence(
                    fixture.run_dir,
                    fixture.private_key,
                    fixture.public_key,
                    key_id="host-main",
                    expected_public_key_sha256="sha256:" + "0" * 64,
                    docker_inspector=fixture.inspector,
                )


if __name__ == "__main__":
    unittest.main()
