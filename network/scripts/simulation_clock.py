"""One explicit deadline clock per run; host time remains the liveness clock."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


_acknowledged_steps: dict[Path, int] = {}


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


def participant_path() -> Path | None:
    name = os.environ.get("BAS_LOCKSTEP_PARTICIPANT")
    if mode() != "lockstep" or not name:
        return None
    if not all(c.isalnum() or c in "_-" for c in name):
        raise ValueError("invalid lockstep participant name")
    return Path(os.environ["BAS_SIM_CLOCK"] + ".participants") / (name + ".json")


def close_participant() -> None:
    path = participant_path()
    if path is not None:
        path.unlink(missing_ok=True)
        _acknowledged_steps.pop(path, None)


@contextmanager
def io_window(*, wait=False, acknowledge=True):
    """Service one bounded I/O cycle under the shared simulator barrier.

    Registered endpoint loops acknowledge the model step after completing their
    cycle. Host time bounds liveness only; packet deadlines use model time.
    """
    if mode() == "realtime":
        yield True
        return
    import fcntl
    path = participant_path()
    if path is not None and not path.exists():
        path.parent.mkdir(exist_ok=True)
        write(path, dict(pid=os.getpid(), simulation_ns=-1))
        _acknowledged_steps.pop(path, None)
    deadline = time.monotonic() + float(os.environ.get("BAS_LOCKSTEP_TIMEOUT_S", "60"))
    while True:
        # Do not let idle readers contend with the coordinator closing a step.
        # Recheck under the lock below to cover a concurrent phase transition.
        if snapshot()['phase'] not in ('starting', 'exchange', 'stopped'):
            if not wait:
                yield False
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("lockstep I/O window did not open within the host timeout")
            time.sleep(.001)
            continue
        with open(os.environ["BAS_SIM_CLOCK"] + ".lock", "a") as handle:
            acquired = False
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            if acquired:
                try:
                    value = snapshot()
                    if value["phase"] in ("starting", "exchange", "stopped"):
                        yield True
                        if acknowledge and path is not None and value["phase"] == "exchange" and _acknowledged_steps.get(path) != value['simulation_ns']:
                            write(path, dict(pid=os.getpid(), simulation_ns=value["simulation_ns"]))
                            _acknowledged_steps[path] = value['simulation_ns']
                        return
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        if not wait:
            yield False
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("lockstep I/O window did not open within the host timeout")
        time.sleep(.001)
