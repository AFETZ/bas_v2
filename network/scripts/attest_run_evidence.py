#!/usr/bin/env python3
"""Create a host-side Ed25519 attestation for a sealed, stopped run.

This command must run on the Docker host after the runtime container exits.
The private key is never accepted from inside the source repository or run
directory.  Docker identity is obtained from ``docker container inspect`` by
this host process, not from producer-supplied environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence_attestation import (  # noqa: E402
    ATTESTATION_RELATIVE_PATH,
    ATTESTATION_SCHEMA_VERSION,
    ATTESTATION_TYPE,
    LEDGER_FIELDS,
    SIGNATURE_RELATIVE_PATH,
    AttestationError,
    _read_regular_file,
    canonical_attestation_payload,
    derive_evidence_identity,
    encode_signature,
    hash_regular_file,
    ledger_entry_name,
    load_json_object_bytes,
    private_key_public_der,
    public_key_der,
    public_key_fingerprint,
    run_file_path,
    sha256_bytes,
    sign_payload,
    validate_attestation_claims,
    validate_manifest_and_files,
    verify_payload_signature,
)


DockerInspector = Callable[[str], dict[str, Any]]
_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _outside_repository(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AttestationError(f"{label} does not exist: {exc}") from exc
    if _is_within(resolved, ROOT_DIR.resolve()):
        raise AttestationError(f"{label} must be outside the source repository")
    return resolved


def _private_key_path(path: Path, run_dir: Path) -> Path:
    resolved = _outside_repository(path, "private key")
    if _is_within(resolved, run_dir):
        raise AttestationError("private key must be outside the run directory")
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise AttestationError("private key is not a regular file")
    if metadata.st_mode & 0o077:
        raise AttestationError("private key must not be accessible to group or other users")
    return resolved


def _external_ledger_dir(path: Path, run_dir: Path) -> Path:
    resolved = _outside_repository(path, "external ledger directory")
    if not resolved.is_dir():
        raise AttestationError("external ledger path is not a directory")
    if _is_within(resolved, run_dir):
        raise AttestationError("external ledger must be outside the run directory")
    return resolved


def inspect_docker_container(container_id: str) -> dict[str, Any]:
    """Read one container record using the host Docker CLI."""

    if _FULL_CONTAINER_ID.fullmatch(container_id) is None:
        raise AttestationError("refusing to inspect a non-full Docker container ID")
    try:
        result = subprocess.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{json .}}",
                container_id,
            ],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError(f"host docker inspect failed: {exc}") from exc
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError(f"host docker inspect rejected container: {error}")
    if len(result.stdout) > 16 * 1024 * 1024:
        raise AttestationError("host docker inspect output is unexpectedly large")
    return load_json_object_bytes(result.stdout, "host docker inspect output")


def _validated_docker_snapshot(
    record: dict[str, Any], container_id: str, image_id: str
) -> dict[str, Any]:
    if record.get("Id") != container_id:
        raise AttestationError("docker inspect Id does not match sealed provenance container ID")
    if record.get("Image") != image_id:
        raise AttestationError("docker inspect Image does not match sealed provenance image ID")
    if _FULL_CONTAINER_ID.fullmatch(str(record.get("Id", ""))) is None:
        raise AttestationError("docker inspect did not return a full immutable container ID")
    if _IMAGE_ID.fullmatch(str(record.get("Image", ""))) is None:
        raise AttestationError("docker inspect did not return an immutable image ID")
    state = record.get("State")
    if not isinstance(state, dict):
        raise AttestationError("docker inspect State is missing")
    if state.get("Running") is not False:
        raise AttestationError("runtime container is still running")
    if state.get("Paused") is not False or state.get("Restarting") is not False:
        raise AttestationError("runtime container is paused or restarting, not stopped")
    if state.get("Status") != "exited":
        raise AttestationError("runtime container state is not exited")
    finished_at = state.get("FinishedAt")
    if not isinstance(finished_at, str) or not finished_at or finished_at.startswith(
        "0001-01-01T00:00:00"
    ):
        raise AttestationError("docker inspect does not contain a real container finish time")
    restart_count = record.get("RestartCount")
    if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count < 0:
        raise AttestationError("docker inspect RestartCount is invalid")
    return {
        "Id": record["Id"],
        "Image": record["Image"],
        "RestartCount": restart_count,
        "State": {
            "Status": state["Status"],
            "Running": state["Running"],
            "Paused": state["Paused"],
            "Restarting": state["Restarting"],
            "FinishedAt": finished_at,
        },
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _same_inode(first: Path, second: Path) -> bool:
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False


def _atomic_exclusive_files(items: list[tuple[Path, bytes]]) -> None:
    """Publish complete files with O_EXCL/link semantics and no replacement."""

    if not items:
        raise AttestationError("no attestation files were provided")
    destinations = [path for path, _ in items]
    if len({str(path) for path in destinations}) != len(destinations):
        raise AttestationError("attestation destinations are not unique")
    temporary_files: list[tuple[Path, Path]] = []
    linked: list[tuple[Path, Path]] = []
    directories: set[Path] = set()
    try:
        for destination, payload in items:
            parent = destination.parent.resolve(strict=True)
            if not parent.is_dir():
                raise AttestationError(f"attestation parent is not a directory: {parent}")
            destination = parent / destination.name
            temporary = parent / f".{destination.name}.{os.getpid()}.{secrets.token_hex(12)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            temporary_files.append((destination, temporary))
            directories.add(parent)
        for destination, temporary in temporary_files:
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise AttestationError(
                    f"refusing to re-sign: attestation target already exists: {destination}"
                ) from exc
            linked.append((destination, temporary))
        for destination, _ in linked:
            os.chmod(destination, 0o444, follow_symlinks=False)
        for directory in directories:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        for destination, temporary in reversed(linked):
            if _same_inode(destination, temporary):
                try:
                    destination.unlink()
                except OSError:
                    pass
        raise
    finally:
        for _, temporary in temporary_files:
            try:
                temporary.unlink()
            except OSError:
                pass


def _ledger_payload(
    claims: dict[str, Any], attestation_bytes: bytes, signature_file_bytes: bytes
) -> bytes:
    entry = {
        "schema_version": 1,
        "entry_type": "ams.run-evidence.attestation-ledger",
        "run_id": claims["run_id"],
        "runtime_id": claims["runtime_id"],
        "key_id": claims["key_id"],
        "public_key_sha256": claims["public_key_sha256"],
        "nonce": claims["nonce"],
        "manifest_sha256": claims["manifest_sha256"],
        "attestation_sha256": sha256_bytes(attestation_bytes),
        "signature_sha256": sha256_bytes(signature_file_bytes),
        "attested_utc": claims["attested_utc"],
    }
    if set(entry) != LEDGER_FIELDS:
        raise AssertionError("internal ledger schema mismatch")
    return (json.dumps(entry, indent=2, sort_keys=True) + "\n").encode("utf-8")


def attest_run_evidence(
    run_dir: Path,
    private_key: Path,
    public_key: Path,
    *,
    key_id: str,
    expected_public_key_sha256: str,
    ledger_dir: Path | None = None,
    docker_inspector: DockerInspector = inspect_docker_container,
) -> dict[str, Any]:
    """Create one non-replaceable detached attestation for a stopped run."""

    resolved_run = run_dir.resolve(strict=True)
    if not resolved_run.is_dir():
        raise AttestationError("run_dir is not a directory")
    metrics_dir = resolved_run / "metrics"
    if metrics_dir.is_symlink() or not metrics_dir.is_dir() or metrics_dir.resolve() != metrics_dir:
        raise AttestationError("run metrics directory is missing or contains a symbolic link")
    resolved_private = _private_key_path(private_key, resolved_run)
    resolved_public = public_key.resolve(strict=True)
    if not resolved_public.is_file():
        raise AttestationError("public key is not a regular file")
    fingerprint = public_key_fingerprint(resolved_public)
    if fingerprint != expected_public_key_sha256:
        raise AttestationError("public key does not match caller-pinned fingerprint")
    if private_key_public_der(resolved_private) != public_key_der(resolved_public):
        raise AttestationError("private key does not correspond to caller-pinned public key")

    attestation_path = resolved_run / ATTESTATION_RELATIVE_PATH
    signature_path = resolved_run / SIGNATURE_RELATIVE_PATH
    if attestation_path.exists() or attestation_path.is_symlink():
        raise AttestationError(f"refusing to re-sign: {attestation_path} already exists")
    if signature_path.exists() or signature_path.is_symlink():
        raise AttestationError(f"refusing to re-sign: {signature_path} already exists")

    resolved_ledger: Path | None = None
    if ledger_dir is not None:
        resolved_ledger = _external_ledger_dir(ledger_dir, resolved_run)

    manifest_path = run_file_path(
        resolved_run, Path("metrics/evidence_manifest.json"), "evidence manifest"
    )
    manifest_bytes = _read_regular_file(
        manifest_path, "evidence manifest", maximum_bytes=64 * 1024 * 1024
    )
    manifest, identity_payloads = validate_manifest_and_files(resolved_run, manifest_bytes)
    identity = derive_evidence_identity(resolved_run, manifest, identity_payloads)

    matrix_hash, _ = hash_regular_file(
        ROOT_DIR / "network/config/validation_matrix.yaml", "authoritative validation matrix"
    )
    if identity["matrix_sha256"] != matrix_hash:
        raise AttestationError("sealed matrix hash does not match authoritative validation matrix")

    first_snapshot = _validated_docker_snapshot(
        docker_inspector(identity["container_id"]),
        identity["container_id"],
        identity["image_id"],
    )

    # Re-read every manifest-bound file after the first Docker inspection.  A
    # stopped container and identical second inspection bracket the host seal.
    second_manifest_bytes = _read_regular_file(
        manifest_path, "evidence manifest", maximum_bytes=64 * 1024 * 1024
    )
    if second_manifest_bytes != manifest_bytes:
        raise AttestationError("evidence manifest changed during host attestation")
    second_manifest, second_identity_payloads = validate_manifest_and_files(
        resolved_run, second_manifest_bytes
    )
    second_identity = derive_evidence_identity(
        resolved_run, second_manifest, second_identity_payloads
    )
    if second_identity != identity:
        raise AttestationError("sealed evidence identity changed during host attestation")
    second_snapshot = _validated_docker_snapshot(
        docker_inspector(identity["container_id"]),
        identity["container_id"],
        identity["image_id"],
    )
    if second_snapshot != first_snapshot:
        raise AttestationError("Docker container identity or stopped state changed during attestation")

    now = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    claims = validate_attestation_claims(
        {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "attestation_type": ATTESTATION_TYPE,
            "run_id": identity["run_id"],
            "runtime_id": identity["runtime_id"],
            "manifest_sha256": sha256_bytes(manifest_bytes),
            "source_hash": identity["source_hash"],
            "matrix_sha256": identity["matrix_sha256"],
            "container_id": identity["container_id"],
            "image_id": identity["image_id"],
            "sealed_utc": identity["sealed_utc"],
            "attested_utc": now,
            "nonce": secrets.token_hex(32),
            "key_id": key_id,
            "public_key_sha256": fingerprint,
        }
    )
    payload = canonical_attestation_payload(claims)
    signature = sign_payload(resolved_private, payload)
    verify_payload_signature(resolved_public, payload, signature)
    attestation_bytes = (json.dumps(claims, indent=2, sort_keys=True) + "\n").encode("utf-8")
    signature_file_bytes = encode_signature(signature)

    items = [
        (attestation_path, attestation_bytes),
        (signature_path, signature_file_bytes),
    ]
    ledger_path: Path | None = None
    if resolved_ledger is not None:
        ledger_path = resolved_ledger / ledger_entry_name(
            claims["run_id"], claims["runtime_id"]
        )
        items.append(
            (ledger_path, _ledger_payload(claims, attestation_bytes, signature_file_bytes))
        )
    _atomic_exclusive_files(items)
    return {
        "attestation_path": str(attestation_path),
        "signature_path": str(signature_path),
        "ledger_path": str(ledger_path) if ledger_path is not None else None,
        "claims": claims,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument(
        "--expected-public-key-sha256",
        required=True,
        help="Caller-pinned sha256:<hex> fingerprint of public-key DER SPKI",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        help="Optional pre-existing append-only ledger directory outside the repository",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = attest_run_evidence(
            args.run_dir,
            args.private_key,
            args.public_key,
            key_id=args.key_id,
            expected_public_key_sha256=args.expected_public_key_sha256,
            ledger_dir=args.ledger_dir,
        )
    except Exception as exc:
        print(f"FAIL evidence attestation: {exc}", file=sys.stderr)
        return 2
    print(f"Evidence attestation: {result['attestation_path']}")
    print(f"Detached signature: {result['signature_path']}")
    if result["ledger_path"]:
        print(f"External ledger entry: {result['ledger_path']}")
    print(f"Key ID: {result['claims']['key_id']}")
    print(f"Public key fingerprint: {result['claims']['public_key_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
