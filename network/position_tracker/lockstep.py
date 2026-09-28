"""Thin Gazebo WorldControl barrier. Physics and ArduPilot remain authoritative.

The native radio requests absolute macrostep targets through atomic files.
No retry of a multi_step request: an ambiguous result must stop the run.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from network.scripts.simulation_clock import write


def control_worker(pipe, world, timeout_s):
    # Keep synchronous transport requests apart from Python subscriber callbacks:
    # gz-transport13 request_raw can otherwise starve callbacks holding the GIL.
    from gz.transport13 import Node
    from gz.msgs10.world_control_pb2 import WorldControl
    from gz.msgs10.boolean_pb2 import Boolean
    node = Node()
    while True:
        steps = pipe.recv()
        if steps is None:
            break
        ok, reply = node.request(f"/world/{world}/control",
            WorldControl(pause=True, multi_step=steps), WorldControl, Boolean,
            int(timeout_s * 1000))
        pipe.send(bool(ok and reply.data))


class GazeboStepper:
    def __init__(self, world_file: Path, state_file: Path, timeout_s=60., max_pose_age_s=.5):
        from gz.transport13 import Node
        from gz.msgs10.world_stats_pb2 import WorldStatistics
        world = ET.parse(world_file).getroot().find("world")
        self.physics_ns = round(float(world.findtext("physics/max_step_size")) * 1e9)
        if self.physics_ns <= 0:
            raise ValueError("invalid Gazebo physics step")
        self.state_file, self.timeout_s = state_file, timeout_s
        self.max_pose_age_s = max_pose_age_s
        self.stats, self.mutex = {}, threading.Lock()
        self.node = Node()
        def observe(msg):
            with self.mutex:
                self.stats = dict(sim_ns=msg.sim_time.sec*10**9+msg.sim_time.nsec,
                    iterations=msg.iterations, paused=msg.paused, rx=time.monotonic())
        if not self.node.subscribe(WorldStatistics, f"/world/{world.get('name')}/stats", observe):
            raise RuntimeError("cannot subscribe to Gazebo stats")
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.worker = context.Process(target=control_worker,
            args=(child, world.get("name"), timeout_s), daemon=True)
        self.worker.start()
        self._request(0)
        self._wait(lambda: self.stats.get("paused") is True, "Gazebo pause")
        self.origin_ns = self.stats["sim_ns"]
        self.origin_iterations = self.stats["iterations"]
        self.target_ns = 0
        self._wait_state(self.origin_ns)
        self.session = self._state()["session_id"]

    def _request(self, steps):
        self.pipe.send(steps)
        if not self.pipe.poll(self.timeout_s + 1) or not self.pipe.recv():
            raise RuntimeError("Gazebo WorldControl failed; refusing to repeat an ambiguous step")

    def _wait(self, predicate, label):
        deadline = time.monotonic() + self.timeout_s
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(label)
            time.sleep(.002)

    def _state(self):
        return json.loads(self.state_file.read_text())

    def _wait_state(self, target):
        def received():
            value = self._state()
            if value.get("fault") or (hasattr(self, "session") and value["session_id"] != self.session):
                raise RuntimeError("source clock or tracker reset")
            if value.get("missing_nodes") or not value.get("source_sim_time_s"):
                return False
            if abs(value["source_sim_time_s"] - target/1e9) > self.physics_ns/1e9 + 1e-8:
                return False
            nodes = [n for n in value["nodes"] if n["role"] != "command_post"]
            return bool(nodes) and all(-1e-8 <= target/1e9-n["source_sim_time_s"] <= self.max_pose_age_s for n in nodes)
        try:
            self._wait(received, "ROS clock/odometry did not reach the Gazebo barrier")
        except TimeoutError as error:
            state = self._state()
            raise TimeoutError(f"{error}: target={target}, clock={state.get('source_sim_time_s')}, poses="
                + str([(n['id'], n.get('source_sim_time_s')) for n in state['nodes']])) from error

    def step(self, target_ns):
        delta = target_ns - self.target_ns
        if delta <= 0 or delta % self.physics_ns:
            raise ValueError("macrostep must advance by an integer number of physics steps")
        before = dict(self.stats)
        if not before["paused"] or before["sim_ns"] != self.origin_ns + self.target_ns:
            raise RuntimeError("Gazebo moved outside the lockstep controller")
        self._request(delta // self.physics_ns)
        expected = self.origin_iterations + target_ns // self.physics_ns
        self._wait(lambda: self.stats["paused"] and self.stats["iterations"] >= expected, "Gazebo step")
        if self.stats["iterations"] != expected or self.stats["sim_ns"] != self.origin_ns + target_ns:
            raise RuntimeError("Gazebo step overshoot / reset")
        self._wait_state(self.origin_ns + target_ns)
        self.target_ns = target_ns

    def close(self):
        # Leave physics paused even on failure; the owning runner cleans up SITL.
        self.worker.terminate()
        self.worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--node-state", type=Path, required=True)
    parser.add_argument("--clock", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=60.)
    args = parser.parse_args()
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    request = Path(str(args.clock) + ".request")
    ack = Path(str(args.clock) + ".ack")
    stepper = None
    try:
        stepper = GazeboStepper(args.world, args.node_state, args.timeout_s)
        def acknowledge():
            write(ack, dict(target_ns=stepper.target_ns, source_origin_ns=stepper.origin_ns,
                physics_step_ns=stepper.physics_ns, fault=None))
        acknowledge()
        while running:
            if request.exists():
                target = json.loads(request.read_text())["target_ns"]
                if target != stepper.target_ns:
                    stepper.step(target)
                    acknowledge()
            if args.clock.exists():
                clock = json.loads(args.clock.read_text())
                if clock.get("phase") == "stopped":
                    break
                if clock.get("phase") != "starting" and (time.monotonic_ns()-clock["monotonic_ns"])/1e9 > args.timeout_s:
                    raise TimeoutError("native radio did not complete its macrostep within the host timeout")
            time.sleep(.002)
    except Exception as error:
        write(ack, dict(fault=str(error)))
        raise
    finally:
        if stepper:
            stepper.close()


if __name__ == "__main__":
    main()
