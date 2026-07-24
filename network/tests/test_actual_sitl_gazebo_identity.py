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
            process(1414, 1406, "dash", "/bin/sh", "-c", "ruby /usr/bin/gz sim -s"),
            process(1418, 1414, "ruby", f"gz sim -v4 -s -r {WORLD}"),
        ]

        selected = select_gazebo_server(records, flight_pid=1406, world_path=WORLD)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.pid, 1418)

    def test_selects_single_field_ruby_process_title(self) -> None:
        records = [
            process(1414, 1406, "dash", "/bin/sh", "-c", "ruby /usr/bin/gz sim -s"),
            process(
                1418,
                1414,
                "ruby",
                f"gz sim -v4 -s -r {WORLD} --force-version 8",
            ),
        ]

        selected = select_gazebo_server(records, flight_pid=1406, world_path=WORLD)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.pid, 1418)

    def test_duplicate_ruby_servers_fail_closed(self) -> None:
        records = [
            process(1414, 1406, "dash", "/bin/sh", "-c", "ruby /usr/bin/gz sim -s"),
            process(1418, 1414, "ruby", f"gz sim -v4 -s -r {WORLD}"),
            process(1419, 1414, "ruby3.0", "/usr/bin/ruby3.0", "/usr/bin/gz", "sim", "-s", WORLD),
        ]

        self.assertIsNone(
            select_gazebo_server(records, flight_pid=1406, world_path=WORLD)
        )

    def test_foreign_or_wrong_world_server_fails_closed(self) -> None:
        records = [
            process(1418, 1, "ruby", f"gz sim -v4 -s -r {WORLD}"),
            process(1420, 1406, "ruby", "gz sim -v4 -s -r /tmp/other.sdf"),
            process(1421, 1406, "ruby", "/usr/bin/ruby3.0", "--title", "gz sim", "-s", WORLD),
        ]

        self.assertIsNone(
            select_gazebo_server(records, flight_pid=1406, world_path=WORLD)
        )

    def test_single_field_title_without_server_mode_fails_closed(self) -> None:
        records = [
            process(1418, 1406, "ruby", f"gz sim -v4 -r {WORLD}"),
        ]

        self.assertIsNone(
            select_gazebo_server(records, flight_pid=1406, world_path=WORLD)
        )
