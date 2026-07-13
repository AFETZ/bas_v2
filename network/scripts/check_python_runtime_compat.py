#!/usr/bin/env python3
"""Fail-closed Python ABI and package-pin smoke check for the accepted runtime.

The accepted radio path needs only ``sionna.rt``.  Deliberately do not import
the Sionna meta package: it may pull TensorFlow into a ROS Humble process.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
from importlib import metadata
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT_DIR / "network/config/dependency_lock.yaml"
ImportModule = Callable[[str], Any]
VersionResolver = Callable[[str], str]


class Results:
    """Collect stable, serializable check results."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(
        self,
        name: str,
        passed: bool,
        detail: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "name": name,
            "passed": bool(passed),
            "detail": detail,
        }
        if expected is not None:
            row["expected"] = expected
        if actual is not None:
            row["actual"] = actual
        self.checks.append(row)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(row["passed"] for row in self.checks)

    def document(self, lock_path: Path) -> dict[str, Any]:
        failures = sum(not row["passed"] for row in self.checks)
        return {
            "schema_version": 1,
            "check": "python_runtime_compat",
            "lock_path": str(lock_path.resolve()),
            "passed": self.passed,
            "failure_count": failures,
            "checks": self.checks,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _locked_version(
    results: Results,
    dependencies: Mapping[str, Any],
    key: str,
    package_key: str,
) -> str | None:
    component = _mapping(dependencies.get(key))
    component_pin = component.get("version")
    packages = _mapping(dependencies.get("python_packages"))
    package_pin = packages.get(package_key)

    valid_component = isinstance(component_pin, str) and bool(component_pin.strip())
    valid_package = isinstance(package_pin, str) and bool(package_pin.strip())
    consistent = valid_component and valid_package and component_pin == package_pin
    results.add(
        f"lock.pin.{package_key}",
        consistent,
        "component and python_packages pins are present and identical"
        if consistent
        else "dependency lock must contain identical non-empty component and python_packages pins",
        expected=str(component_pin) if valid_component else "non-empty exact version",
        actual=str(package_pin) if valid_package else "missing",
    )
    return str(component_pin) if consistent else None


def _major(version: str) -> int | None:
    match = re.match(r"^\s*(\d+)(?:\.|$)", version)
    return int(match.group(1)) if match else None


def _safe_origin(module: Any) -> str | None:
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, (str, bytes)) or not origin:
        return None
    try:
        return str(Path(origin).resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(path: str, parent: Path) -> bool:
    try:
        Path(path).relative_to(parent.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _import_one(
    results: Results,
    name: str,
    importer: ImportModule,
    repo_root: Path,
) -> Any | None:
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
            captured_stderr
        ):
            module = importer(name)
    except BaseException as exc:  # Import-time ABI errors are part of this gate.
        results.add(
            f"import.{name}",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return None

    origin = _safe_origin(module)
    if origin is None:
        results.add(
            f"import.{name}",
            False,
            "module imported but has no resolvable __file__; refusing unverifiable runtime module",
        )
        return None
    if _inside(origin, repo_root):
        results.add(
            f"import.{name}",
            False,
            "module resolves inside the repository and may shadow the installed dependency",
            actual=origin,
        )
        return None

    captured = " ".join(
        part.strip()
        for part in (captured_stdout.getvalue(), captured_stderr.getvalue())
        if part.strip()
    )
    detail = f"imported from {origin}"
    if captured:
        detail += f"; import output: {captured[:500]}"
    results.add(f"import.{name}", True, detail)
    return module


def _installed_version(
    results: Results,
    distribution: str,
    expected: str | None,
    resolver: VersionResolver,
) -> str | None:
    if expected is None:
        results.add(
            f"version.{distribution}",
            False,
            "cannot validate installed version because the lock pin is invalid",
        )
        return None
    try:
        actual = resolver(distribution)
    except BaseException as exc:
        results.add(
            f"version.{distribution}",
            False,
            f"cannot resolve installed distribution: {type(exc).__name__}: {exc}",
            expected=expected,
        )
        return None
    exact = actual == expected
    results.add(
        f"version.{distribution}",
        exact,
        "installed distribution exactly matches the accepted pin"
        if exact
        else "installed distribution differs from the accepted pin",
        expected=expected,
        actual=actual,
    )
    return actual


def run_checks(
    lock_path: Path,
    *,
    importer: ImportModule = importlib.import_module,
    version_resolver: VersionResolver = metadata.version,
    repo_root: Path = ROOT_DIR,
) -> Results:
    """Run checks without exiting; injectable callables keep unit tests lightweight."""

    results = Results()
    try:
        loaded = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except BaseException as exc:
        results.add("lock.read", False, f"{type(exc).__name__}: {exc}")
        return results
    if not isinstance(loaded, Mapping):
        results.add("lock.read", False, "dependency lock root must be a mapping")
        return results
    results.add("lock.read", True, "dependency lock parsed as YAML mapping")

    schema_ok = loaded.get("schema_version") == 2
    results.add(
        "lock.schema_version",
        schema_ok,
        "supported dependency lock schema" if schema_ok else "expected dependency lock schema_version 2",
        expected="2",
        actual=str(loaded.get("schema_version")),
    )

    dependencies = _mapping(loaded.get("dependencies"))
    ros = _mapping(dependencies.get("ros"))
    ros_distribution = ros.get("distribution")
    humble = ros_distribution == "humble"
    results.add(
        "lock.ros_distribution",
        humble,
        "ROS Humble ABI policy applies" if humble else "this acceptance checker requires ROS Humble",
        expected="humble",
        actual=str(ros_distribution),
    )

    packages = _mapping(dependencies.get("python_packages"))
    runtime_policy = _mapping(loaded.get("runtime_policy"))
    mitsuba_variant = runtime_policy.get("mitsuba_variant")
    valid_variant = isinstance(mitsuba_variant, str) and bool(mitsuba_variant.strip())
    results.add(
        "lock.mitsuba_variant",
        valid_variant,
        "accepted Mitsuba variant is explicitly locked"
        if valid_variant
        else "runtime_policy.mitsuba_variant must be a non-empty string",
        actual=str(mitsuba_variant),
    )
    forbidden = sorted(
        str(name)
        for name in packages
        if str(name).lower() == "sionna" or str(name).lower().startswith("tensorflow")
    )
    results.add(
        "lock.no_sionna_meta_or_tensorflow",
        not forbidden,
        "accepted runtime contains only the RT-specific Sionna package"
        if not forbidden
        else f"remove forbidden accepted package pin(s): {', '.join(forbidden)}",
    )

    numpy_pin = _locked_version(results, dependencies, "numpy", "numpy")
    sionna_rt_pin = _locked_version(results, dependencies, "sionna_rt", "sionna-rt")
    mitsuba_pin = _locked_version(results, dependencies, "mitsuba", "mitsuba")

    numpy_major = _major(numpy_pin) if numpy_pin is not None else None
    numpy_abi_ok = humble and numpy_major is not None and numpy_major < 2
    results.add(
        "lock.numpy_ros_humble_abi",
        numpy_abi_ok,
        "NumPy major is compatible with ROS Humble binary extensions"
        if numpy_abi_ok
        else "ROS Humble acceptance requires an exact NumPy 1.x pin; NumPy >=2 is rejected",
        expected="major version 1",
        actual=numpy_pin or "invalid pin",
    )

    # Order matters: importing cv2/cv_bridge after NumPy exercises their binary ABI.
    imported: dict[str, Any | None] = {}
    for module_name in (
        "numpy",
        "cv2",
        "cv_bridge",
        "sionna.rt",
        "mitsuba",
        "mpl_toolkits.mplot3d",
    ):
        imported[module_name] = _import_one(results, module_name, importer, repo_root)

    numpy_actual = _installed_version(results, "numpy", numpy_pin, version_resolver)
    _installed_version(results, "sionna-rt", sionna_rt_pin, version_resolver)
    _installed_version(results, "mitsuba", mitsuba_pin, version_resolver)

    numpy_module_version = getattr(imported.get("numpy"), "__version__", None)
    numpy_module_ok = (
        isinstance(numpy_module_version, str)
        and numpy_actual is not None
        and numpy_module_version == numpy_actual
    )
    results.add(
        "version.numpy_module",
        numpy_module_ok,
        "NumPy module and distribution versions agree"
        if numpy_module_ok
        else "NumPy module __version__ must exactly match installed distribution metadata",
        expected=numpy_actual or "resolved distribution version",
        actual=str(numpy_module_version),
    )

    mitsuba_module = imported.get("mitsuba")
    try:
        available_variants = list(mitsuba_module.variants())
    except BaseException as exc:
        results.add(
            "runtime.mitsuba_variant",
            False,
            f"cannot enumerate Mitsuba variants: {type(exc).__name__}: {exc}",
            expected=str(mitsuba_variant),
        )
    else:
        variant_available = valid_variant and mitsuba_variant in available_variants
        results.add(
            "runtime.mitsuba_variant",
            variant_available,
            "locked Mitsuba variant is available"
            if variant_available
            else "locked Mitsuba variant is unavailable",
            expected=str(mitsuba_variant),
            actual=", ".join(sorted(str(item) for item in available_variants)),
        )

    tensorflow_loaded = sorted(
        name for name in sys.modules if name == "tensorflow" or name.startswith("tensorflow.")
    )
    results.add(
        "runtime.no_tensorflow",
        not tensorflow_loaded,
        "RT imports did not load TensorFlow"
        if not tensorflow_loaded
        else f"forbidden TensorFlow modules loaded: {', '.join(tensorflow_loaded[:10])}",
    )
    return results


def _emit_human(document: Mapping[str, Any]) -> None:
    print("Python runtime compatibility check")
    print(f"Dependency lock: {document['lock_path']}")
    for row in document["checks"]:
        status = "PASS" if row["passed"] else "FAIL"
        suffix = ""
        if "expected" in row or "actual" in row:
            suffix = f" (expected={row.get('expected', '-')}, actual={row.get('actual', '-')})"
        print(f"{status:<4} {row['name']:<38} {row['detail']}{suffix}")
    print(
        "Python runtime compatibility passed."
        if document["passed"]
        else f"Python runtime compatibility failed: {document['failure_count']} check(s)."
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    importer: ImportModule = importlib.import_module,
    version_resolver: VersionResolver = metadata.version,
    repo_root: Path = ROOT_DIR,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    results = run_checks(
        args.lock,
        importer=importer,
        version_resolver=version_resolver,
        repo_root=repo_root,
    )
    document = results.document(args.lock)
    if args.format == "json":
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    else:
        _emit_human(document)
    return 0 if document["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
