#!/usr/bin/env python3
"""Collect one ROS 2 PointCloud2 message into an XYZ CSV file."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2


class PointCloudCollector(Node):
    def __init__(self, topic: str) -> None:
        super().__init__("ams_pointcloud2_xyz_collector")
        self.message: PointCloud2 | None = None
        self.subscription = self.create_subscription(PointCloud2, topic, self._callback, 10)

    def _callback(self, message: PointCloud2) -> None:
        self.message = message


def collect(topic: str, timeout_s: float) -> PointCloud2:
    rclpy.init()
    node = PointCloudCollector(topic)
    deadline = time.monotonic() + timeout_s
    try:
        while rclpy.ok() and node.message is None and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
        if node.message is None:
            raise TimeoutError(f"no PointCloud2 message received on {topic!r} within {timeout_s:.1f}s")
        return node.message
    finally:
        node.destroy_node()
        rclpy.shutdown()


def write_points(message: PointCloud2, output: Path, max_points: int) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    available_fields = {field.name for field in message.fields}
    fields = ["x", "y", "z"]
    if "intensity" in available_fields:
        fields.append("intensity")

    rows = 0
    finite_rows = 0
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        for point in point_cloud2.read_points(message, field_names=fields, skip_nans=True):
            rows += 1
            values = [float(value) for value in point]
            if not all(math.isfinite(value) for value in values[:3]):
                continue
            writer.writerow(values)
            finite_rows += 1
            if max_points > 0 and finite_rows >= max_points:
                break

    metadata = {
        "topic_frame_id": message.header.frame_id,
        "stamp": {"sec": int(message.header.stamp.sec), "nanosec": int(message.header.stamp.nanosec)},
        "height": int(message.height),
        "width": int(message.width),
        "point_step": int(message.point_step),
        "row_step": int(message.row_step),
        "is_dense": bool(message.is_dense),
        "fields": [
            {
                "name": field.name,
                "offset": int(field.offset),
                "datatype": int(field.datatype),
                "count": int(field.count),
            }
            for field in message.fields
        ],
        "csv": str(output),
        "rows_written": rows,
        "finite_rows_written": finite_rows,
        "max_points": max_points,
    }
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-points", type=int, default=20000)
    args = parser.parse_args()

    message = collect(args.topic, args.timeout)
    metadata = write_points(message, Path(args.output), args.max_points)
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
