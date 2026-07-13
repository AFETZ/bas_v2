#!/usr/bin/env python3
"""Move a Gazebo UAV model through the shared rock-shadow line."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import time
from pathlib import Path


def interpolate(a: float, b: float, fraction: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, fraction))


def pose_at(elapsed_s: float, args: argparse.Namespace) -> tuple[float, float, float]:
    if elapsed_s < args.hold_before_s:
        x = args.start_x
    elif elapsed_s < args.hold_before_s + args.transition_s:
        fraction = (elapsed_s - args.hold_before_s) / max(args.transition_s, 0.001)
        x = interpolate(args.start_x, args.end_x, fraction)
    else:
        x = args.end_x
    return float(x), float(args.y), float(args.z)


class PoseSetter:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.native = False
        self.node = None
        self.pose_pb2 = None
        self.boolean_pb2 = None
        if args.transport in ("auto", "native"):
            try:
                os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
                from gz.msgs10 import boolean_pb2, pose_pb2
                from gz.transport13 import Node

                self.node = Node()
                self.pose_pb2 = pose_pb2
                self.boolean_pb2 = boolean_pb2
                self.native = True
                print("FLY_ROCK_SHADOW transport=native_gz_python", flush=True)
            except Exception as exc:
                if args.transport == "native":
                    raise
                print(f"FLY_ROCK_SHADOW transport=cli_gz_service fallback={exc!r}", flush=True)

    def set_pose(self, x: float, y: float, z: float) -> tuple[bool, str]:
        if self.native:
            return self._set_pose_native(x, y, z)
        return self._set_pose_cli(x, y, z)

    def _set_pose_native(self, x: float, y: float, z: float) -> tuple[bool, str]:
        req = self.pose_pb2.Pose()
        req.name = self.args.model
        req.position.x = float(x)
        req.position.y = float(y)
        req.position.z = float(z)
        req.orientation.w = 1.0
        ok, response = self.node.request(
            f"/world/{self.args.world}/set_pose",
            req,
            self.pose_pb2.Pose,
            self.boolean_pb2.Boolean,
            int(self.args.service_timeout_ms),
        )
        response_text = f"data: {str(bool(getattr(response, 'data', False))).lower()}"
        return bool(ok and getattr(response, "data", False)), response_text

    def _set_pose_cli(self, x: float, y: float, z: float) -> tuple[bool, str]:
        return set_pose_cli(self.args, x, y, z)


def set_pose_cli(args: argparse.Namespace, x: float, y: float, z: float) -> tuple[bool, str]:
    request = (
        f'name: "{args.model}" '
        f"position {{ x: {x:.3f} y: {y:.3f} z: {z:.3f} }} "
        "orientation { w: 1.0 }"
    )
    cmd = [
        "gz",
        "service",
        "-s",
        f"/world/{args.world}/set_pose",
        "--reqtype",
        "gz.msgs.Pose",
        "--reptype",
        "gz.msgs.Boolean",
        "--timeout",
        str(int(args.service_timeout_ms)),
        "--req",
        request,
    ]
    completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0 and "data: true" in output, output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", default="map")
    parser.add_argument("--model", default="uav2")
    parser.add_argument("--start-x", type=float, default=-300.0)
    parser.add_argument("--end-x", type=float, default=500.0)
    parser.add_argument("--y", type=float, default=0.0)
    parser.add_argument("--z", type=float, default=80.0)
    parser.add_argument("--hold-before-s", type=float, default=8.0)
    parser.add_argument("--transition-s", type=float, default=42.0)
    parser.add_argument("--hold-after-s", type=float, default=12.0)
    parser.add_argument("--rate-hz", type=float, default=4.0)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--service-timeout-ms", type=int, default=2000)
    parser.add_argument("--transport", choices=["auto", "native", "cli"], default="auto")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    duration_s = max(args.hold_before_s + args.transition_s + args.hold_after_s, 0.1)
    period_s = 1.0 / max(args.rate_hz, 0.1)
    output_csv = Path(args.output_csv) if args.output_csv else None
    csv_file = None
    writer = None
    if output_csv:
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        csv_file = output_csv.open("w", encoding="utf-8", newline="")
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["elapsed_s", "model", "x", "y", "z", "ok", "response"],
        )
        writer.writeheader()

    pose_setter = PoseSetter(args)
    start = time.monotonic()
    failures = 0
    successes = 0
    try:
        while True:
            now = time.monotonic()
            elapsed = now - start
            x, y, z = pose_at(elapsed, args)
            ok, response = pose_setter.set_pose(x, y, z)
            successes += 1 if ok else 0
            failures += 0 if ok else 1
            if writer:
                writer.writerow(
                    {
                        "elapsed_s": round(elapsed, 3),
                        "model": args.model,
                        "x": round(x, 3),
                        "y": round(y, 3),
                        "z": round(z, 3),
                        "ok": ok,
                        "response": response,
                    }
                )
                csv_file.flush()
            print(
                f"FLY_ROCK_SHADOW t={elapsed:.3f}s {args.model} "
                f"x={x:.3f} y={y:.3f} z={z:.3f} set_pose={ok}",
                flush=True,
            )
            if elapsed >= duration_s:
                break
            sleep_s = period_s - (time.monotonic() - now)
            if sleep_s > 0:
                time.sleep(sleep_s)
    finally:
        if csv_file:
            csv_file.close()
    if failures:
        print(
            f"FLY_ROCK_SHADOW warning: {failures} set_pose calls failed, "
            f"{successes} succeeded",
            flush=True,
        )
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
