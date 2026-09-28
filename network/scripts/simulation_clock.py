"""One explicit deadline clock per run; host time remains the liveness clock."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def mode() -> str:
    value = os.environ.get("BAS_SIMULATION_MODE", "realtime")
    if value not in ("realtime", "lockstep"):
        raise ValueError("BAS_SIMULATION_MODE must be realtime or lockstep")
    return value


def wire_version() -> int:
    return 2 if mode() == "lockstep" else 1


def snapshot() -> dict:
    path = os.environ.get("BAS_SIM_CLOCK")
    if not path:
        raise RuntimeError("lockstep requires BAS_SIM_CLOCK")
    value = json.loads(Path(path).read_text())
    if value.get("mode") != "lockstep" or not isinstance(value.get("simulation_ns"), int):
        raise RuntimeError("invalid lockstep clock")
    if value["simulation_ns"] < 0:
        raise RuntimeError("negative simulation time")
    return value


def monotonic_ns() -> int:
    return snapshot()["simulation_ns"] if mode() == "lockstep" else time.monotonic_ns()


def monotonic() -> float:
    return monotonic_ns() / 1e9


def live() -> bool:
    try:
        value = snapshot()
        age = (time.monotonic_ns() - value["monotonic_ns"]) / 1e9
        return value.get("healthy") is True and 0 <= age <= float(os.environ.get("BAS_LOCKSTEP_TIMEOUT_S", "60"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def exchanging() -> bool:
    return mode() == "realtime" or (live() and snapshot().get("phase") == "exchange")


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False) + "\n")
    temporary.replace(path)


def initial(path: Path) -> None:
    write(path, dict(mode="lockstep", simulation_ns=0, monotonic_ns=time.monotonic_ns(),
                     healthy=False, phase="starting"))


@contextmanager
def io_window():
    """Do not write to SITL while either simulator is inside a macrostep."""
    if mode() == "realtime":
        yield True
        return
    if snapshot().get("phase") not in ("starting", "exchange", "stopped"):
        yield False
        return
    import fcntl
    with open(os.environ["BAS_SIM_CLOCK"] + ".lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            # A stopped radio still drains/discards incoming traffic for the
            # no-bypass probe. A valid computation keeps queues intact.
            yield not live() or exchanging()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
