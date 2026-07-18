#!/usr/bin/env bash
set -euo pipefail

# This file is intentionally the complete, inspectable acceptance-container
# command.  Host-final validation compares Config.Cmd to this path and the
# exact M0 argv instead of accepting a runner token hidden in arbitrary shell
# arguments.
source /opt/ros/humble/setup.bash
source /workspace/ardu_ws/install/setup.bash

if [[ "${AMS_M0_SOURCE_MODE:-}" == "clean_git_clone_ro" ]]; then
  M0_RUN_ID=""
  if (($# == 3)) && [[ "$1" == "env" ]] && \
    [[ "$2" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]] && \
    [[ "$3" == "network/scripts/run_m0_baseline.sh" ]]; then
    M0_RUN_ID="${2#RUN_ID=}"
  elif (($# == 2)) && \
    [[ "$1" == "network/scripts/run_m0_host_reexecution.sh" ]] && \
    [[ "$2" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
    M0_RUN_ID="$2"
  else
    printf 'FAIL immutable M0 command/run identity is not exact\n' >&2
    exit 2
  fi
  if [[ ! "${AMS_M0_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'FAIL immutable M0 source commit is unavailable\n' >&2
    exit 2
  fi
  if [[ "${AMS_M0_PROJECT_OVERLAY_MODE:-}" != "none_q0_source_only" ]]; then
    printf 'FAIL immutable M0 Q0-only source mode is not exact\n' >&2
    exit 2
  fi
  if [[ "${AMS_M0_ARTIFACT_ROOT:-}" != "/run/ams/m0-artifacts" ]]; then
    printf 'FAIL immutable M0 artifact root is not isolated\n' >&2
    exit 2
  fi
  if [[ "${AMS_M0_COLLECTION_SECURITY:-}" != \
      "cap_drop_all_no_new_privileges" ]] || \
    [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" != \
      "host_final_isolated_exact_image" ]]; then
    printf 'FAIL M0 collection security profile is not exact\n' >&2
    exit 2
  fi
  CAP_BOUNDING="$(awk '$1 == "CapBnd:" {print $2}' /proc/self/status)"
  NO_NEW_PRIVS="$(awk '$1 == "NoNewPrivs:" {print $2}' /proc/self/status)"
  if [[ "$CAP_BOUNDING" != "0000000000000000" ]] || \
    [[ "$NO_NEW_PRIVS" != "1" ]] || sudo -n true >/dev/null 2>&1; then
    printf 'FAIL M0 collector retained capabilities or privilege escalation\n' >&2
    exit 2
  fi
  MOUNT_OPTIONS="$(findmnt -n -o OPTIONS -T "$PWD")"
  ARTIFACT_TARGET="$(findmnt -n -o TARGET -T "$AMS_M0_ARTIFACT_ROOT")"
  ARTIFACT_OPTIONS="$(findmnt -n -o OPTIONS -T "$AMS_M0_ARTIFACT_ROOT")"
  ROOT_OPTIONS="$(findmnt -n -o OPTIONS -T /)"
  TMP_TYPE="$(findmnt -n -o FSTYPE -T /tmp)"
  TMP_OPTIONS="$(findmnt -n -o OPTIONS -T /tmp)"
  if [[ ",$MOUNT_OPTIONS," != *,ro,* ]]; then
    printf 'FAIL M0 source checkout is not mounted read-only: %s\n' \
      "$MOUNT_OPTIONS" >&2
    exit 2
  fi
  if [[ "$ARTIFACT_TARGET" != "$AMS_M0_ARTIFACT_ROOT" ]] || \
    [[ ",$ARTIFACT_OPTIONS," != *,rw,* ]] || \
    [[ ",$ROOT_OPTIONS," != *,ro,* ]] || \
    [[ "$TMP_TYPE" != "tmpfs" ]] || \
    [[ ",$TMP_OPTIONS," != *,rw,* ]] || \
    [[ ",$TMP_OPTIONS," != *,nosuid,* ]] || \
    [[ ",$TMP_OPTIONS," != *,nodev,* ]]; then
    printf 'FAIL M0 artifact/rootfs/tmpfs mount contract is not exact\n' >&2
    exit 2
  fi
  if [[ -e build || -e install || -e log ]]; then
    printf 'FAIL immutable M0 checkout unexpectedly contains local build outputs\n' >&2
    exit 2
  fi
  export PYTHONNOUSERSITE=1
  export PYTHONPYCACHEPREFIX="/tmp/ams-m0-pycache-${M0_RUN_ID}"
  unset PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONPATH
  unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD
  if [[ -e "$PYTHONPYCACHEPREFIX" ]]; then
    printf 'FAIL controlled M0 Python cache already exists: %s\n' \
      "$PYTHONPYCACHEPREFIX" >&2
    exit 2
  fi
  install -d -m 0700 "$PYTHONPYCACHEPREFIX"
  export HOME=/tmp/ams-m0-home
  install -d -m 0700 "$HOME"
  export XDG_CACHE_HOME="$HOME/.cache"
  export MPLCONFIGDIR="$HOME/.config/matplotlib"
  install -d -m 0700 "$XDG_CACHE_HOME" "$MPLCONFIGDIR"
  export GIT_OPTIONAL_LOCKS=0
  export AMS_M0_PYTHON_GUARD="$PWD/network/scripts/m0_python_guard"
  export AMS_M0_BASE_PYTHONPATH="$AMS_M0_PYTHON_GUARD:$PWD:/workspace/ardu_ws/install/ros_gz_interfaces/local/lib/python3.10/dist-packages:/workspace/ardu_ws/build/ardupilot_dds_tests:/workspace/ardu_ws/install/ardupilot_dds_tests/lib/python3.10/site-packages:/workspace/ardu_ws/install/ardupilot_sitl/local/lib/python3.10/dist-packages:/workspace/ardu_ws/install/ardupilot_msgs/local/lib/python3.10/dist-packages:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages:/home/ubuntu/.local/lib/python3.10/site-packages:/usr/local/lib/python3.10/dist-packages:/usr/lib/python3/dist-packages"
  export PYTHONPATH="$AMS_M0_BASE_PYTHONPATH"
  if [[ "$(/usr/bin/python3.10 -c 'import pathlib,sitecustomize,sys; print(str(sys.flags.no_site) + ":" + str(int(getattr(sitecustomize,"AMS_M0_INERT_SITECUSTOMIZE",False))) + ":" + str(int("usercustomize" in sys.modules)) + ":" + str(pathlib.Path(sitecustomize.__file__).resolve()))')" != "0:1:0:$PWD/network/scripts/m0_python_guard/sitecustomize.py" ]]; then
    printf 'FAIL normal child Python did not load exactly the tracked inert guard\n' >&2
    exit 2
  fi
  if [[ "$("$PWD/network/scripts/m0_bin/python3" -c 'import sys; print(str(sys.flags.no_site) + ":" + str(int("sitecustomize" in sys.modules or "usercustomize" in sys.modules)) + ":" + sys.executable)')" != "1:0:/usr/bin/python3.10" ]]; then
    printf 'FAIL isolated M0 suite Python loaded customization modules\n' >&2
    exit 2
  fi
elif [[ "${AMS_COMPONENT_SOURCE_MODE:-}" == "clean_git_clone_ro" ]]; then
  if (($# != 7)) || [[ "$1" != "timeout" ]] || \
    [[ "$2" != "--signal=TERM" ]] || [[ "$3" != "--kill-after=20s" ]] || \
    [[ ! "$4" =~ ^([0-9]{3,4})s$ ]] || [[ "$5" != "env" ]] || \
    [[ ! "$6" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]]; then
    printf 'FAIL immutable component command identity is not exact\n' >&2
    exit 2
  fi
  COMPONENT_RUN_ID="${6#RUN_ID=}"
  COMPONENT_TIMEOUT_S="${4%s}"
  mapfile -t COMPONENT_PROFILE_VALUES < <(
    /usr/bin/python3.10 - "$7" "$COMPONENT_TIMEOUT_S" <<'PY'
import sys

from network.validation.component_profiles import match_profile

BITS = {
    "CHOWN": 0,
    "DAC_READ_SEARCH": 2,
    "NET_ADMIN": 12,
    "NET_RAW": 13,
    "SYS_ADMIN": 21,
}
name, profile = match_profile(sys.argv[1], int(sys.argv[2]))
mask = sum(1 << BITS[value] for value in profile["main_cap_add"])
print(name)
print(f"{mask:016x}")
print(len(profile["main_devices"]))
print(profile["prerequisite_status_count"])
print(profile["nvidia_driver_capabilities"])
print(profile["python_runtime"])
PY
  )
  if ((${#COMPONENT_PROFILE_VALUES[@]} != 6)); then
    printf 'FAIL immutable component profile did not resolve exactly\n' >&2
    exit 2
  fi
  COMPONENT_EXPECTED_CAPABILITY_MODE=inherited_m0_host_final
  if [[ "${COMPONENT_PROFILE_VALUES[2]}" == "1" ]]; then
    COMPONENT_EXPECTED_CAPABILITY_MODE=bounded_root_in_runtime
  fi
  if \
    [[ "${AMS_COMPONENT_PROFILE:-}" != "${COMPONENT_PROFILE_VALUES[0]}" ]] || \
    [[ "${AMS_COMPONENT_RUN_ID:-}" != "$COMPONENT_RUN_ID" ]] || \
    [[ ! "${AMS_COMPONENT_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || \
    [[ "${AMS_COMPONENT_STATUS_RESULT_PATH:-}" != "/run/ams/status-validation.json" ]] || \
    [[ "${AMS_COMPONENT_PREREQUISITES_PATH:-}" != "/run/ams/prerequisites.json" ]] || \
    [[ "${AMS_M1_SOURCE_MODE:-}" != "clean_git_clone_ro" ]] || \
    [[ "${AMS_M1_SOURCE_COMMIT:-}" != "$AMS_COMPONENT_SOURCE_COMMIT" ]] || \
    [[ "${AMS_M1_PROJECT_OVERLAY_MODE:-}" != "fresh_run_overlay" ]] || \
    [[ "${AMS_M1_RUN_ID:-}" != "$COMPONENT_RUN_ID" ]] || \
    [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" != \
      "$COMPONENT_EXPECTED_CAPABILITY_MODE" ]] || \
    [[ "${AMS_M1_M0_RECEIPT_PATH:-}" != "/run/ams/prerequisites/m0.json" ]] || \
    [[ ! "${AMS_M1_M0_RECEIPT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || \
    [[ "${AMS_M1_M0_STATUS_COMMIT:-}" != "$AMS_COMPONENT_SOURCE_COMMIT" ]] || \
    [[ "${NVIDIA_DRIVER_CAPABILITIES:-}" != "${COMPONENT_PROFILE_VALUES[4]}" ]] || \
    [[ ! "${AMS_M1_M0_RECEIPT_CANONICAL_PATH:-}" =~ ^runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/m0_host_final_receipt\.json$ ]]; then
    printf 'FAIL immutable component source/run/prerequisite identity is unavailable\n' >&2
    exit 2
  fi
  CAP_BOUNDING="$(awk '$1 == "CapBnd:" {print $2}' /proc/self/status)"
  CAP_PERMITTED="$(awk '$1 == "CapPrm:" {print $2}' /proc/self/status)"
  CAP_EFFECTIVE="$(awk '$1 == "CapEff:" {print $2}' /proc/self/status)"
  NO_NEW_PRIVS="$(awk '$1 == "NoNewPrivs:" {print $2}' /proc/self/status)"
  SOURCE_OPTIONS="$(findmnt -n -o OPTIONS -T "$PWD")"
  RUNS_TARGET="$(findmnt -n -o TARGET -T "$PWD/runs")"
  RUNS_OPTIONS="$(findmnt -n -o OPTIONS -T "$PWD/runs")"
  ROOT_OPTIONS="$(findmnt -n -o OPTIONS -T /)"
  TMP_TYPE="$(findmnt -n -o FSTYPE -T /tmp)"
  TMP_OPTIONS="$(findmnt -n -o OPTIONS -T /tmp)"
  STATUS_TARGET="$(findmnt -n -o TARGET -T /run/ams/status-validation.json)"
  STATUS_OPTIONS="$(findmnt -n -o OPTIONS -T /run/ams/status-validation.json)"
  PREREQUISITES_TARGET="$(findmnt -n -o TARGET -T /run/ams/prerequisites.json)"
  PREREQUISITES_OPTIONS="$(findmnt -n -o OPTIONS -T /run/ams/prerequisites.json)"
  if [[ "$CAP_BOUNDING" != "${COMPONENT_PROFILE_VALUES[1]}" ]] || \
    [[ "$NO_NEW_PRIVS" != "1" ]] || \
    [[ ",${SOURCE_OPTIONS}," != *,ro,* ]] || \
    [[ "$RUNS_TARGET" != "$PWD/runs" ]] || [[ ",${RUNS_OPTIONS}," != *,rw,* ]] || \
    [[ ",${ROOT_OPTIONS}," != *,ro,* ]] || [[ "$TMP_TYPE" != "tmpfs" ]] || \
    [[ ",${TMP_OPTIONS}," != *,rw,* ]] || [[ ",${TMP_OPTIONS}," != *,nosuid,* ]] || \
    [[ ",${TMP_OPTIONS}," != *,nodev,* ]] || \
    [[ "$STATUS_TARGET" != "/run/ams/status-validation.json" ]] || \
    [[ ",${STATUS_OPTIONS}," != *,ro,* ]] || \
    [[ "$PREREQUISITES_TARGET" != "/run/ams/prerequisites.json" ]] || \
    [[ ",${PREREQUISITES_OPTIONS}," != *,ro,* ]] || \
    [[ -L /run/ams/status-validation.json || \
       ! -f /run/ams/status-validation.json || \
       -w /run/ams/status-validation.json ]] || \
    [[ -L /run/ams/prerequisites.json || \
       ! -f /run/ams/prerequisites.json || \
       -w /run/ams/prerequisites.json ]]; then
    printf 'FAIL immutable component capability/rootfs/mount contract is not exact\n' >&2
    exit 2
  fi
  if [[ "${COMPONENT_PROFILE_VALUES[2]}" == "1" ]]; then
    NETNS_TYPE="$(findmnt -n -o FSTYPE -T /run/netns)"
    NETNS_OPTIONS="$(findmnt -n -o OPTIONS -T /run/netns)"
    if [[ "$(id -u)" != "0" || "$(id -g)" != "1000" ]] || \
      [[ "$CAP_PERMITTED" != "${COMPONENT_PROFILE_VALUES[1]}" ]] || \
      [[ "$CAP_EFFECTIVE" != "${COMPONENT_PROFILE_VALUES[1]}" ]] || \
      [[ ! -c /dev/net/tun ]] || [[ "$NETNS_TYPE" != "tmpfs" ]] || \
      [[ ",${NETNS_OPTIONS}," != *,rw,* ]] || \
      [[ ",${NETNS_OPTIONS}," != *,nosuid,* ]] || \
      [[ ",${NETNS_OPTIONS}," != *,nodev,* ]] || \
      [[ ",${NETNS_OPTIONS}," != *,noexec,* ]]; then
      printf 'FAIL component root/TUN/netns capability profile is not exact\n' >&2
      exit 2
    fi
  elif [[ "$(id -u)" != "1000" ]] || \
    [[ "$CAP_PERMITTED" != "0000000000000000" ]] || \
    [[ "$CAP_EFFECTIVE" != "0000000000000000" ]] || \
    sudo -n true >/dev/null 2>&1; then
    printf 'FAIL unprivileged component profile retained privilege\n' >&2
    exit 2
  fi
  if ! /usr/bin/python3.10 - \
      "$AMS_COMPONENT_PROFILE" "$AMS_COMPONENT_SOURCE_COMMIT" \
      "${COMPONENT_PROFILE_VALUES[3]}" \
      "$AMS_M1_M0_RECEIPT_CANONICAL_PATH" \
      "$AMS_M1_M0_RECEIPT_SHA256" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

from network.validation.component_profiles import load_profiles

profile, commit, count_text, m0_canonical, m0_sha256 = sys.argv[1:]
count = int(count_text)
status_path = pathlib.Path("/run/ams/status-validation.json")
prerequisites_path = pathlib.Path("/run/ams/prerequisites.json")
status_bytes = status_path.read_bytes()
prerequisites = json.loads(prerequisites_path.read_text(encoding="utf-8"))
receipts = prerequisites.get("receipts")
component_receipts = prerequisites.get("component_receipts")
expected_names = {f"m{index}" for index in range(count)}
profiles = load_profiles()
profile_record = profiles.get(profile)
expected_component_names = (
    set(profile_record["required_component_profiles"])
    if isinstance(profile_record, dict)
    else set()
)
if (
    prerequisites.get("schema_version") != 1
    or prerequisites.get("contract") != "ams.component-prerequisites/v1"
    or prerequisites.get("profile") != profile
    or prerequisites.get("source_commit") != commit
    or not isinstance(receipts, dict)
    or set(receipts) != expected_names
    or not isinstance(component_receipts, dict)
    or set(component_receipts) != expected_component_names
    or set(receipts).intersection(component_receipts)
    or prerequisites.get("status", {}).get("result_sha256")
    != hashlib.sha256(status_bytes).hexdigest()
):
    raise SystemExit("component prerequisite manifest is not exact")
status = json.loads(status_bytes.decode("utf-8"))
if (
    status.get("schema_version") != 1
    or status.get("contract") != "ams.live-status-lint/v1"
    or status.get("passed") is not True
    or status.get("failures") != []
    or status.get("report_commit") != commit
):
    raise SystemExit("component live-status result is not exact/current")
all_receipts = {**receipts, **component_receipts}
for name in sorted(set(all_receipts)):
    path = pathlib.Path(f"/run/ams/prerequisites/{name}.json")
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise SystemExit(f"component prerequisite receipt is mutable: {name}")
    raw = path.read_bytes()
    record = all_receipts[name]
    receipt = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("sha256") != hashlib.sha256(raw).hexdigest()
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or receipt.get("receipt_path") != record.get("canonical_path")
        or not isinstance(record.get("canonical_path"), str)
        or re.fullmatch(
            r"runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/[a-z0-9_]+_host_final_receipt\.json",
            record["canonical_path"],
        ) is None
    ):
        raise SystemExit(f"component prerequisite receipt differs: {name}")
if receipts["m0"].get("canonical_path") != m0_canonical or receipts["m0"].get(
    "sha256"
) != m0_sha256:
    raise SystemExit("component M0 environment binding differs")
PY
  then
    printf 'FAIL component status/prerequisite bytes are not exact\n' >&2
    exit 2
  fi
  if [[ -e build || -e install || -e log ]]; then
    printf 'FAIL immutable component checkout contains host build/install outputs\n' >&2
    exit 2
  fi
  export GIT_OPTIONAL_LOCKS=0
  if [[ "$(/usr/bin/git -c "safe.directory=$PWD" rev-parse HEAD)" \
      != "$AMS_COMPONENT_SOURCE_COMMIT" ]] || \
    [[ -n "$(/usr/bin/git -c "safe.directory=$PWD" status \
      --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL immutable component checkout differs from its committed source\n' >&2
    exit 2
  fi
  export HOME="/tmp/ams-component-home-$COMPONENT_RUN_ID"
  export XDG_CACHE_HOME="$HOME/.cache"
  export ROS_LOG_DIR="$HOME/.ros/log"
  export MPLCONFIGDIR="$HOME/.config/matplotlib"
  export DRJIT_CACHE_DIR="$HOME/.drjit"
  install -d -m 0700 "$HOME" "$XDG_CACHE_HOME" "$ROS_LOG_DIR" \
    "$MPLCONFIGDIR" "$DRJIT_CACHE_DIR"
  export PYTHONNOUSERSITE=1
  if [[ "${COMPONENT_PROFILE_VALUES[5]}" == "sionna_rt_cuda" ]]; then
    export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}:/home/ubuntu/.local/lib/python3.10/site-packages"
    if ! /usr/bin/python3.10 - <<'PY'
import importlib
import os
import pathlib
import sys

root = pathlib.Path.cwd().resolve()
allowed = (
    root,
    pathlib.Path("/workspace/ardu_ws"),
    pathlib.Path("/opt/ros/humble"),
    pathlib.Path("/home/ubuntu/.local/lib/python3.10/site-packages"),
)
raw_entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
if (
    os.environ.get("PYTHONNOUSERSITE") != "1"
    or not raw_entries
    or "" in raw_entries
    or pathlib.Path(raw_entries[0]).resolve() != root
    or raw_entries[-1] != str(allowed[-1])
    or len(raw_entries) != len(set(raw_entries))
    or pathlib.Path(sys.executable).resolve()
    != pathlib.Path("/usr/bin/python3.10")
):
    raise SystemExit("controlled component PYTHONPATH/interpreter is not exact")
for value in raw_entries:
    resolved = pathlib.Path(value).resolve()
    if not any(resolved == prefix or prefix in resolved.parents for prefix in allowed):
        raise SystemExit(f"uncontrolled component PYTHONPATH entry: {resolved}")
entries = [pathlib.Path(value) for value in sys.path if value]
if not entries or entries[0].resolve() != root or len(entries) != len(set(entries)):
    raise SystemExit("controlled component Python path is not exact/unique")
for entry in entries:
    resolved = entry.resolve()
    if not any(resolved == prefix or prefix in resolved.parents for prefix in allowed):
        # Interpreter-owned stdlib/dist-package entries do not originate in
        # PYTHONPATH and remain covered by the exact executable/image identity.
        if not str(resolved).startswith(("/usr/lib/python3", "/usr/local/lib/python3")):
            raise SystemExit(f"uncontrolled component Python path: {resolved}")
for name in ("sionna.rt", "mitsuba", "numpy"):
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    if not origin.is_file():
        raise SystemExit(f"component Python module origin is not regular: {name}")
    trusted = allowed[-1] in origin.parents
    if name == "numpy":
        trusted = trusted or str(origin).startswith(
            ("/usr/lib/python3", "/usr/local/lib/python3")
        )
    if not trusted:
        raise SystemExit(f"component Python module origin is uncontrolled: {name}")
PY
    then
      printf 'FAIL controlled Sionna component Python runtime is unavailable\n' >&2
      exit 2
    fi
  elif [[ "${COMPONENT_PROFILE_VALUES[5]}" != "base" ]]; then
    printf 'FAIL component Python runtime profile is unknown\n' >&2
    exit 2
  fi
elif [[ "${AMS_M1_SOURCE_MODE:-}" == "clean_git_clone_ro" ]]; then
  if (($# != 7)) || [[ "$1" != "timeout" ]] || \
    [[ "$2" != "--signal=TERM" ]] || [[ "$3" != "--kill-after=20s" ]] || \
    [[ "$4" != "600s" ]] || [[ "$5" != "env" ]] || \
    [[ ! "$6" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]] || \
    [[ "$7" != "network/scripts/run_five_uav_health.sh" ]]; then
    printf 'FAIL immutable M1 command identity is not exact\n' >&2
    exit 2
  fi
  M1_RUN_ID="${6#RUN_ID=}"
  if [[ "${AMS_M1_RUN_ID:-}" != "$M1_RUN_ID" ]] || \
    [[ ! "${AMS_M1_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || \
    [[ "${AMS_M1_PROJECT_OVERLAY_MODE:-}" != "fresh_run_overlay" ]] || \
    [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" != "inherited_m0_host_final" ]] || \
    [[ "${AMS_M1_M0_RECEIPT_PATH:-}" != "/run/ams/m0-receipt.json" ]] || \
    [[ ! "${AMS_M1_M0_RECEIPT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || \
    [[ "${AMS_M1_M0_STATUS_COMMIT:-}" != "$AMS_M1_SOURCE_COMMIT" ]] || \
    [[ ! "${AMS_M1_M0_RECEIPT_CANONICAL_PATH:-}" =~ ^runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/m0_host_final_receipt\.json$ ]]; then
    printf 'FAIL immutable M1 source/run/overlay identity is unavailable\n' >&2
    exit 2
  fi
  CAP_BOUNDING="$(awk '$1 == "CapBnd:" {print $2}' /proc/self/status)"
  NO_NEW_PRIVS="$(awk '$1 == "NoNewPrivs:" {print $2}' /proc/self/status)"
  SOURCE_OPTIONS="$(findmnt -n -o OPTIONS -T "$PWD")"
  RUNS_TARGET="$(findmnt -n -o TARGET -T "$PWD/runs")"
  RUNS_OPTIONS="$(findmnt -n -o OPTIONS -T "$PWD/runs")"
  ROOT_OPTIONS="$(findmnt -n -o OPTIONS -T /)"
  TMP_TYPE="$(findmnt -n -o FSTYPE -T /tmp)"
  TMP_OPTIONS="$(findmnt -n -o OPTIONS -T /tmp)"
  RECEIPT_TARGET="$(findmnt -n -o TARGET -T /run/ams/m0-receipt.json)"
  RECEIPT_OPTIONS="$(findmnt -n -o OPTIONS -T /run/ams/m0-receipt.json)"
  if [[ "$CAP_BOUNDING" != "0000000000000000" ]] || \
    [[ "$NO_NEW_PRIVS" != "1" ]] || sudo -n true >/dev/null 2>&1 || \
    [[ ",$SOURCE_OPTIONS," != *,ro,* ]] || \
    [[ "$RUNS_TARGET" != "$PWD/runs" ]] || [[ ",$RUNS_OPTIONS," != *,rw,* ]] || \
    [[ ",$ROOT_OPTIONS," != *,ro,* ]] || [[ "$TMP_TYPE" != "tmpfs" ]] || \
    [[ ",$TMP_OPTIONS," != *,rw,* ]] || [[ ",$TMP_OPTIONS," != *,nosuid,* ]] || \
    [[ ",$TMP_OPTIONS," != *,nodev,* ]] || \
    [[ "$RECEIPT_TARGET" != "/run/ams/m0-receipt.json" ]] || \
    [[ ",$RECEIPT_OPTIONS," != *,ro,* ]] || \
    [[ -L /run/ams/m0-receipt.json || ! -f /run/ams/m0-receipt.json ]] || \
    [[ -w /run/ams/m0-receipt.json ]] || \
    [[ "$(sha256sum /run/ams/m0-receipt.json | awk '{print $1}')" != "$AMS_M1_M0_RECEIPT_SHA256" ]]; then
    printf 'FAIL immutable M1 isolation/mount contract is not exact\n' >&2
    exit 2
  fi
  if [[ -e build || -e install || -e log ]]; then
    printf 'FAIL immutable M1 checkout contains host build/install outputs\n' >&2
    exit 2
  fi
  export GIT_OPTIONAL_LOCKS=0
  if [[ "$(git rev-parse HEAD)" != "$AMS_M1_SOURCE_COMMIT" ]] || \
    [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL immutable M1 checkout differs from its committed source\n' >&2
    exit 2
  fi
  export HOME="/tmp/ams-m1-home-$M1_RUN_ID"
  export XDG_CACHE_HOME="$HOME/.cache"
  export ROS_LOG_DIR="$HOME/.ros/log"
  export MPLCONFIGDIR="$HOME/.config/matplotlib"
  install -d -m 0700 "$HOME" "$XDG_CACHE_HOME" "$ROS_LOG_DIR" "$MPLCONFIGDIR"
  export PYTHONNOUSERSITE=1
else
  if [[ -f install/setup.bash ]]; then
    source install/setup.bash
  fi
fi

export GZ_VERSION=harmonic
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
exec "$@"
