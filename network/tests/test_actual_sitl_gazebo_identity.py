"""Unit tests for the actual-SITL Gazebo server identity selector."""

from __future__ import annotations

import unittest

from network.scripts.actual_sitl_gazebo_identity import (
    ProcessRecord,
    select_gazebo_server,
)


WORLD = "/workspace/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"


def process(
    pid: int,
    ppid: int,
    comm: str,
    *argv: str,
    pgid: int = 1406,
) -> ProcessRecord:
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        session_id=1406,
        start_ticks=pid * 10,
        comm=comm,
        argv=tuple(argv),
    )


class GazeboIdentityTests(unittest.TestCase):
    def test_selects_ruby_server_beneath_dash_launcher(self) -> None:
        records = [
            process(1414, 1406, "dash", "/bin/sh", "/usr/bin/gz", "sim", "-s", WORLD),
            process(1418, 1414, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", WORLD),
        ]

        selected = select_gazebo_server(records, flight_pid=1406, world_path=WORLD)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.pid, 1418)

    def test_duplicate_ruby_servers_fail_closed(self) -> None:
        records = [
            process(1414, 1406, "dash", "/bin/sh", "/usr/bin/gz", "sim", "-s", WORLD),
            process(1418, 1414, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", WORLD),
            process(1419, 1414, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", WORLD),
        ]

        self.assertIsNone(
            select_gazebo_server(records, flight_pid=1406, world_path=WORLD)
        )

    def test_foreign_or_wrong_world_server_fails_closed(self) -> None:
        records = [
            process(1418, 1, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", WORLD),
            process(1420, 1406, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", "/tmp/other.sdf"),
        ]

        self.assertIsNone(
            select_gazebo_server(records, flight_pid=1406, world_path=WORLD)
        )
