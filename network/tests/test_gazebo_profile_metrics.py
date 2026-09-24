from pathlib import Path

from scripts.product.gazebo_profile_metrics import profile_gazebo_rtf


def test_profile_gazebo_rtf_uses_only_bounded_monotonic_samples(tmp_path: Path) -> None:
    log = tmp_path / "gazebo_stats.log"
    log.write_text(
        "99\treal_time_factor: 0.10\n"
        "100\treal_time_factor: 0.96\n"
        "150\treal_time_factor: 1.00\n"
        "200\treal_time_factor: 0.98\n"
        "201\treal_time_factor: 0.20\n",
        encoding="utf-8",
    )

    measured = profile_gazebo_rtf(log, 100, 200)

    assert measured["count"] == 3
    assert measured["min"] == 0.96
    assert measured["max"] == 1.0
    assert measured["mean"] == 0.98


def test_profile_gazebo_rtf_rejects_untimestamped_global_samples(tmp_path: Path) -> None:
    log = tmp_path / "gazebo_stats.log"
    log.write_text("real_time_factor: 1.0\n", encoding="utf-8")

    measured = profile_gazebo_rtf(log, 100, 200)

    assert measured["count"] == 0
    assert measured["mean"] is None
