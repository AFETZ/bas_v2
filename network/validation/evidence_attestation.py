#!/usr/bin/env python3
"""Ed25519 verification for externally attested run-evidence manifests.

The JSON attestation is only a transport envelope.  Signatures cover a
domain-separated, length-prefixed binary representation with a fixed field
order, so JSON whitespace and object ordering never affect the signed value.
Only caller-pinned public keys are trusted; a key or fingerprint named by the
attestation itself never becomes a trust root.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


ROOT_DIR = Path(__file__).resolve().parents[2]
ATTESTATION_SCHEMA_VERSION = 1
ATTESTATION_TYPE = "ams.run-evidence.ed25519"
ATTESTATION_DOMAIN = b"Ardupilot_Multiagent_Simulation/run-evidence-attestation/v1"
LEDGER_DOMAIN = b"Ardupilot_Multiagent_Simulation/run-evidence-ledger-key/v1"
ATTESTATION_RELATIVE_PATH = Path("metrics/evidence_attestation.json")
SIGNATURE_RELATIVE_PATH = Path("metrics/evidence_attestation.sig")

SIGNED_FIELDS = (
    "schema_version",
    "attestation_type",
    "run_id",
    "runtime_id",
    "manifest_sha256",
    "source_hash",
    "matrix_sha256",
    "container_id",
    "image_id",
    "sealed_utc",
    "attested_utc",
    "nonce",
    "key_id",
    "public_key_sha256",
)
ATTESTATION_FIELDS = frozenset(SIGNED_FIELDS)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "runtime_id",
        "source_hash",
        "sealed_utc",
        "matrix_sha256",
        "files",
    }
)
LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "entry_type",
        "run_id",
        "runtime_id",
        "key_id",
        "public_key_sha256",
        "nonce",
        "manifest_sha256",
        "attestation_sha256",
        "signature_sha256",
        "attested_utc",
    }
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{64}\Z")
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class AttestationError(ValueError):
    """Raised when evidence or attestation input fails a strict check."""


@dataclass(frozen=True)
class TrustedPublicKey:
    """A verifier trust-root selected by key ID and pinned SPKI fingerprint."""

    path: Path
    public_key_sha256: str


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value: str) -> None:
    raise AttestationError(f"non-finite JSON number is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AttestationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json_object_bytes(payload: bytes, label: str) -> dict[str, Any]:
    """Decode one strict JSON object, rejecting duplicate keys and NaN values."""

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except AttestationError:
        raise
    except Exception as exc:
        raise AttestationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AttestationError(f"{label} must be a JSON object")
    return value


def _read_regular_file(path: Path, label: str, *, maximum_bytes: int | None = None) -> bytes:
    """Read a non-symlink regular file through one descriptor."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AttestationError(f"could not open {label}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AttestationError(f"{label} is not a regular file")
        if maximum_bytes is not None and metadata.st_size > maximum_bytes:
            raise AttestationError(f"{label} exceeds {maximum_bytes} bytes")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AttestationError(f"{label} changed or was truncated while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AttestationError(f"{label} changed or grew while being read")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise AttestationError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def hash_regular_file(path: Path, label: str) -> tuple[str, int]:
    """Hash a non-symlink regular file and reject an observable concurrent change."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AttestationError(f"could not open {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AttestationError(f"{label} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) or size != before.st_size:
            raise AttestationError(f"{label} changed while being hashed")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise AttestationError(f"{label} is not canonical UTC seconds")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise AttestationError(f"{label} is not a valid timestamp") from exc
    return parsed


def validate_attestation_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plain, validated claim dictionary or raise fail-closed."""

    if set(claims) != ATTESTATION_FIELDS:
        missing = sorted(ATTESTATION_FIELDS - set(claims))
        extra = sorted(set(claims) - ATTESTATION_FIELDS)
        raise AttestationError(f"attestation fields differ; missing={missing}, extra={extra}")
    value = dict(claims)
    if value["schema_version"] != ATTESTATION_SCHEMA_VERSION or isinstance(
        value["schema_version"], bool
    ):
        raise AttestationError("attestation schema_version is not 1")
    if value["attestation_type"] != ATTESTATION_TYPE:
        raise AttestationError("attestation_type is not recognized")
    if not isinstance(value["run_id"], str) or _SAFE_ID_RE.fullmatch(value["run_id"]) is None:
        raise AttestationError("run_id is not a safe canonical identifier")
    if not isinstance(value["runtime_id"], str) or _SAFE_ID_RE.fullmatch(value["runtime_id"]) is None:
        raise AttestationError("runtime_id is not a safe canonical identifier")
    for name in ("manifest_sha256", "source_hash", "matrix_sha256"):
        if not isinstance(value[name], str) or _SHA256_RE.fullmatch(value[name]) is None:
            raise AttestationError(f"{name} is not a lowercase SHA-256 value")
    if not isinstance(value["container_id"], str) or _CONTAINER_ID_RE.fullmatch(
        value["container_id"]
    ) is None:
        raise AttestationError("container_id is not a full 64-character Docker ID")
    if not isinstance(value["image_id"], str) or _DIGEST_RE.fullmatch(value["image_id"]) is None:
        raise AttestationError("image_id is not an immutable sha256 Docker image ID")
    sealed = _parse_utc(value["sealed_utc"], "sealed_utc")
    attested = _parse_utc(value["attested_utc"], "attested_utc")
    if attested < sealed:
        raise AttestationError("attested_utc predates sealed_utc")
    if not isinstance(value["nonce"], str) or _NONCE_RE.fullmatch(value["nonce"]) is None:
        raise AttestationError("nonce is not a 256-bit lowercase hexadecimal value")
    if not isinstance(value["key_id"], str) or _KEY_ID_RE.fullmatch(value["key_id"]) is None:
        raise AttestationError("key_id is not a safe canonical identifier")
    if not isinstance(value["public_key_sha256"], str) or _DIGEST_RE.fullmatch(
        value["public_key_sha256"]
    ) is None:
        raise AttestationError("public_key_sha256 is not a pinned SHA-256 fingerprint")
    return value


def _framed_field(name: str, value: bytes) -> bytes:
    name_bytes = name.encode("ascii")
    return struct.pack(">H", len(name_bytes)) + name_bytes + struct.pack(">I", len(value)) + value


def canonical_attestation_payload(claims: Mapping[str, Any]) -> bytes:
    """Encode the exact signed value with domain and unambiguous lengths."""

    value = validate_attestation_claims(claims)
    output = bytearray()
    output.extend(struct.pack(">H", len(ATTESTATION_DOMAIN)))
    output.extend(ATTESTATION_DOMAIN)
    output.extend(struct.pack(">H", len(SIGNED_FIELDS)))
    for name in SIGNED_FIELDS:
        item = value[name]
        encoded = str(item).encode("utf-8")
        output.extend(_framed_field(name, encoded))
    return bytes(output)


def _openssl_output(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["openssl", *arguments],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError(f"OpenSSL invocation failed: {exc}") from exc
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError(f"OpenSSL rejected the key or signature: {error}")
    return result.stdout


def public_key_der(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    encoded = _openssl_output(["pkey", "-pubin", "-in", str(resolved), "-outform", "DER"])
    if len(encoded) != 44 or not encoded.startswith(_ED25519_SPKI_PREFIX):
        raise AttestationError("public key is not an Ed25519 SubjectPublicKeyInfo key")
    return encoded


def private_key_public_der(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    encoded = _openssl_output(["pkey", "-in", str(resolved), "-pubout", "-outform", "DER"])
    if len(encoded) != 44 or not encoded.startswith(_ED25519_SPKI_PREFIX):
        raise AttestationError("private key is not an Ed25519 key")
    return encoded


def public_key_fingerprint(path: Path) -> str:
    """Return ``sha256:<hex>`` over canonical DER SubjectPublicKeyInfo bytes."""

    return "sha256:" + sha256_bytes(public_key_der(path))


def sign_payload(private_key: Path, payload: bytes) -> bytes:
    # Ed25519 is a one-shot operation in OpenSSL.  Some OpenSSL builds reject
    # a pipe because they must know the complete input length before signing.
    with tempfile.NamedTemporaryFile(prefix="ams-evidence-payload-", delete=True) as handle:
        handle.write(payload)
        handle.flush()
        signature = _openssl_output(
            [
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key.resolve(strict=True)),
                "-in",
                handle.name,
            ]
        )
    if len(signature) != 64:
        raise AttestationError("OpenSSL did not produce a 64-byte Ed25519 signature")
    return signature


def verify_payload_signature(public_key: Path, payload: bytes, signature: bytes) -> None:
    if len(signature) != 64:
        raise AttestationError("detached Ed25519 signature is not 64 bytes")
    with tempfile.NamedTemporaryFile(
        prefix="ams-evidence-payload-", delete=True
    ) as payload_handle, tempfile.NamedTemporaryFile(
        prefix="ams-evidence-signature-", delete=True
    ) as signature_handle:
        payload_handle.write(payload)
        payload_handle.flush()
        signature_handle.write(signature)
        signature_handle.flush()
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(public_key.resolve(strict=True)),
                    "-rawin",
                    "-in",
                    payload_handle.name,
                    "-sigfile",
                    signature_handle.name,
                ],
                capture_output=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AttestationError(f"OpenSSL signature verification failed: {exc}") from exc
    if result.returncode != 0:
        raise AttestationError("detached Ed25519 signature is invalid")


def encode_signature(signature: bytes) -> bytes:
    if len(signature) != 64:
        raise AttestationError("detached Ed25519 signature is not 64 bytes")
    return base64.b64encode(signature) + b"\n"


def decode_signature(payload: bytes) -> bytes:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AttestationError("signature file is not ASCII base64") from exc
    if not text.endswith("\n") or "\n" in text[:-1] or "\r" in text:
        raise AttestationError("signature file is not one canonical base64 line")
    encoded = text[:-1]
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AttestationError("signature file contains malformed base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
        raise AttestationError("signature file is not canonical Ed25519 base64")
    return signature


def _safe_manifest_relative_path(relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        raise AttestationError(f"invalid manifest file path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise AttestationError(f"manifest file path escapes run directory: {relative!r}")
    if pure.as_posix() != relative:
        raise AttestationError(f"manifest file path is not canonical POSIX: {relative!r}")
    return Path(*pure.parts)


def run_file_path(run_dir: Path, relative: Path, label: str) -> Path:
    """Resolve a run file while rejecting escapes and symlink components."""

    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise AttestationError(f"{label} path is not a safe run-relative path")
    candidate = run_dir / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(run_dir)
    except (OSError, ValueError) as exc:
        raise AttestationError(f"{label} path escapes or is missing from run directory: {exc}") from exc
    if resolved != candidate:
        raise AttestationError(f"{label} path contains a symbolic-link component")
    return candidate


def validate_manifest_and_files(run_dir: Path, manifest_bytes: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate the seal and every referenced raw file, returning identity bytes."""

    manifest = load_json_object_bytes(manifest_bytes, "evidence manifest")
    if set(manifest) != MANIFEST_FIELDS:
        raise AttestationError("evidence manifest fields do not match schema 2")
    if manifest.get("schema_version") != 2 or isinstance(manifest.get("schema_version"), bool):
        raise AttestationError("evidence manifest schema_version is not 2")
    if manifest.get("run_id") != run_dir.name:
        raise AttestationError("evidence manifest run_id does not match run directory")
    if not isinstance(manifest.get("runtime_id"), str) or _SAFE_ID_RE.fullmatch(
        manifest["runtime_id"]
    ) is None:
        raise AttestationError("evidence manifest runtime_id is invalid")
    if not isinstance(manifest.get("source_hash"), str) or _SHA256_RE.fullmatch(
        manifest["source_hash"]
    ) is None:
        raise AttestationError("evidence manifest source_hash is invalid")
    _parse_utc(manifest.get("sealed_utc"), "evidence manifest sealed_utc")
    if not isinstance(manifest.get("matrix_sha256"), str) or _SHA256_RE.fullmatch(
        manifest["matrix_sha256"]
    ) is None:
        raise AttestationError("evidence manifest matrix_sha256 is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AttestationError("evidence manifest files map is empty or invalid")
    identity_payloads: dict[str, bytes] = {}
    for relative, record in files.items():
        path = _safe_manifest_relative_path(relative)
        if not isinstance(record, dict) or set(record) != {"sha256", "size_bytes"}:
            raise AttestationError(f"manifest record is invalid: {relative!r}")
        expected_hash = record.get("sha256")
        expected_size = record.get("size_bytes")
        if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
            raise AttestationError(f"manifest hash is invalid: {relative!r}")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 1
        ):
            raise AttestationError(f"manifest size is invalid: {relative!r}")
        absolute = run_file_path(run_dir, path, f"raw evidence {relative}")
        actual_hash, actual_size = hash_regular_file(absolute, f"raw evidence {relative}")
        if actual_hash != expected_hash or actual_size != expected_size:
            raise AttestationError(f"raw evidence does not match manifest: {relative}")
        if relative in {"metrics/provenance.json", "metrics/joint_runtime.json"}:
            payload = _read_regular_file(absolute, f"raw evidence {relative}", maximum_bytes=16 * 1024 * 1024)
            if sha256_bytes(payload) != expected_hash or len(payload) != expected_size:
                raise AttestationError(f"identity evidence changed while being read: {relative}")
            identity_payloads[relative] = payload
    required = {"metrics/provenance.json", "metrics/joint_runtime.json"}
    if set(identity_payloads) != required:
        raise AttestationError("manifest does not bind provenance.json and joint_runtime.json")
    return manifest, identity_payloads


def derive_evidence_identity(
    run_dir: Path,
    manifest: Mapping[str, Any],
    identity_payloads: Mapping[str, bytes],
) -> dict[str, str]:
    """Derive the signed identity solely from sealed evidence records."""

    provenance = load_json_object_bytes(
        identity_payloads["metrics/provenance.json"], "provenance"
    )
    joint_runtime = load_json_object_bytes(
        identity_payloads["metrics/joint_runtime.json"], "joint runtime"
    )
    source_hash = manifest["source_hash"]
    runtime_id = manifest["runtime_id"]
    if provenance.get("schema_version") != 2:
        raise AttestationError("provenance schema_version is not 2")
    if provenance.get("run_id") != run_dir.name:
        raise AttestationError("provenance run_id does not match run directory")
    if provenance.get("source_hash") != source_hash:
        raise AttestationError("provenance source_hash does not match manifest")
    if joint_runtime.get("schema_version") != 2:
        raise AttestationError("joint runtime schema_version is not 2")
    if joint_runtime.get("run_id") != run_dir.name:
        raise AttestationError("joint runtime run_id does not match run directory")
    if joint_runtime.get("runtime_id") != runtime_id:
        raise AttestationError("joint runtime runtime_id does not match manifest")
    if joint_runtime.get("source_hash") != source_hash:
        raise AttestationError("joint runtime source_hash does not match manifest")
    container = provenance.get("container_image")
    if not isinstance(container, dict):
        raise AttestationError("provenance container_image is missing")
    container_id = container.get("runtime_container_id")
    image_id = container.get("digest")
    if not isinstance(container_id, str) or _CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise AttestationError("provenance does not contain a full Docker container ID")
    if not isinstance(image_id, str) or _DIGEST_RE.fullmatch(image_id) is None:
        raise AttestationError("provenance does not contain an immutable Docker image ID")
    return {
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "matrix_sha256": manifest["matrix_sha256"],
        "container_id": container_id,
        "image_id": image_id,
        "sealed_utc": manifest["sealed_utc"],
    }


def ledger_entry_name(run_id: str, runtime_id: str) -> str:
    """Return a deterministic opaque filename for a one-time identity tuple."""

    payload = bytearray(struct.pack(">H", len(LEDGER_DOMAIN)))
    payload.extend(LEDGER_DOMAIN)
    payload.extend(_framed_field("run_id", run_id.encode("utf-8")))
    payload.extend(_framed_field("runtime_id", runtime_id.encode("utf-8")))
    return hashlib.sha256(payload).hexdigest() + ".json"


def _verify_ledger(
    ledger_dir: Path,
    claims: Mapping[str, Any],
    attestation_bytes: bytes,
    signature_bytes: bytes,
) -> None:
    entry_path = ledger_dir / ledger_entry_name(claims["run_id"], claims["runtime_id"])
    entry_bytes = _read_regular_file(entry_path, "external attestation ledger entry", maximum_bytes=64 * 1024)
    entry = load_json_object_bytes(entry_bytes, "external attestation ledger entry")
    if set(entry) != LEDGER_FIELDS:
        raise AttestationError("external ledger entry fields are invalid")
    expected = {
        "schema_version": 1,
        "entry_type": "ams.run-evidence.attestation-ledger",
        "run_id": claims["run_id"],
        "runtime_id": claims["runtime_id"],
        "key_id": claims["key_id"],
        "public_key_sha256": claims["public_key_sha256"],
        "nonce": claims["nonce"],
        "manifest_sha256": claims["manifest_sha256"],
        "attestation_sha256": sha256_bytes(attestation_bytes),
        "signature_sha256": sha256_bytes(signature_bytes),
        "attested_utc": claims["attested_utc"],
    }
    if entry != expected:
        raise AttestationError("external ledger entry does not match detached attestation")


def verify_evidence_attestation(
    run_dir: Path,
    trusted_keys: Mapping[str, TrustedPublicKey],
    *,
    attestation_path: Path | None = None,
    signature_path: Path | None = None,
    ledger_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify a detached attestation and all evidence identities, never raising.

    ``trusted_keys`` is the caller's trust policy.  The dictionary key selects
    the public key and each value pins that key's canonical DER fingerprint.
    """

    failures: list[str] = []
    claims: dict[str, Any] | None = None
    try:
        resolved_run = run_dir.resolve(strict=True)
        if not resolved_run.is_dir():
            raise AttestationError("run_dir is not a directory")
        if attestation_path is not None:
            supplied_attestation = attestation_path.resolve(strict=True)
            try:
                attestation_relative = supplied_attestation.relative_to(resolved_run)
            except ValueError as exc:
                raise AttestationError("attestation path is outside run directory") from exc
        else:
            attestation_relative = ATTESTATION_RELATIVE_PATH
        if signature_path is not None:
            supplied_signature = signature_path.resolve(strict=True)
            try:
                signature_relative = supplied_signature.relative_to(resolved_run)
            except ValueError as exc:
                raise AttestationError("signature path is outside run directory") from exc
        else:
            signature_relative = SIGNATURE_RELATIVE_PATH
        resolved_attestation = run_file_path(
            resolved_run, attestation_relative, "evidence attestation"
        )
        resolved_signature = run_file_path(
            resolved_run, signature_relative, "detached evidence signature"
        )
        attestation_bytes = _read_regular_file(
            resolved_attestation, "evidence attestation", maximum_bytes=64 * 1024
        )
        signature_file_bytes = _read_regular_file(
            resolved_signature, "detached evidence signature", maximum_bytes=4096
        )
        claims = validate_attestation_claims(
            load_json_object_bytes(attestation_bytes, "evidence attestation")
        )
        signature = decode_signature(signature_file_bytes)
        key_id = claims["key_id"]
        trusted = trusted_keys.get(key_id)
        if not isinstance(trusted, TrustedPublicKey):
            raise AttestationError(f"unknown attestation key_id: {key_id}")
        if not isinstance(trusted.public_key_sha256, str) or _DIGEST_RE.fullmatch(
            trusted.public_key_sha256
        ) is None:
            raise AttestationError(f"caller pin for key_id {key_id} is malformed")
        actual_fingerprint = public_key_fingerprint(trusted.path)
        if actual_fingerprint != trusted.public_key_sha256:
            raise AttestationError(f"public key file does not match caller pin for key_id {key_id}")
        if claims["public_key_sha256"] != trusted.public_key_sha256:
            raise AttestationError("attestation fingerprint does not match caller-pinned key")
        payload = canonical_attestation_payload(claims)
        verify_payload_signature(trusted.path, payload, signature)

        manifest_path = run_file_path(
            resolved_run, Path("metrics/evidence_manifest.json"), "evidence manifest"
        )
        manifest_bytes = _read_regular_file(
            manifest_path, "evidence manifest", maximum_bytes=64 * 1024 * 1024
        )
        if sha256_bytes(manifest_bytes) != claims["manifest_sha256"]:
            raise AttestationError("current evidence manifest hash does not match attestation")
        manifest, identity_payloads = validate_manifest_and_files(
            resolved_run, manifest_bytes
        )
        identity = derive_evidence_identity(resolved_run, manifest, identity_payloads)
        for name in (
            "run_id",
            "runtime_id",
            "source_hash",
            "matrix_sha256",
            "container_id",
            "image_id",
            "sealed_utc",
        ):
            if claims[name] != identity[name]:
                raise AttestationError(f"attested {name} does not match sealed evidence")
        matrix_path = ROOT_DIR / "network/config/validation_matrix.yaml"
        matrix_hash, _ = hash_regular_file(matrix_path, "authoritative validation matrix")
        if claims["matrix_sha256"] != matrix_hash:
            raise AttestationError("attested matrix hash does not match authoritative matrix")
        if ledger_dir is not None:
            resolved_ledger = ledger_dir.resolve(strict=True)
            if not resolved_ledger.is_dir():
                raise AttestationError("external ledger path is not a directory")
            try:
                resolved_ledger.relative_to(ROOT_DIR.resolve())
            except ValueError:
                pass
            else:
                raise AttestationError("external ledger path is inside source repository")
            try:
                resolved_ledger.relative_to(resolved_run)
            except ValueError:
                pass
            else:
                raise AttestationError("external ledger path is inside run directory")
            _verify_ledger(
                resolved_ledger,
                claims,
                attestation_bytes,
                signature_file_bytes,
            )
    except Exception as exc:
        failures.append(str(exc) or exc.__class__.__name__)
    if failures:
        return {
            "status": "failed",
            "proof": "external evidence attestation failed closed",
            "details": {"failures": failures},
        }
    assert claims is not None
    return {
        "status": "passed",
        "proof": "external Ed25519 attestation matches sealed evidence and caller-pinned key",
        "details": {
            "key_id": claims["key_id"],
            "public_key_sha256": claims["public_key_sha256"],
            "manifest_sha256": claims["manifest_sha256"],
            "container_id": claims["container_id"],
            "image_id": claims["image_id"],
            "ledger_verified": ledger_dir is not None,
        },
    }
