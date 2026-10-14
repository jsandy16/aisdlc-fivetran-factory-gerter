"""Phase 7: the local end-to-end demo plays through (spec → build → run → two changes), stand-ins only."""

from __future__ import annotations

from pathlib import Path

from factory_testing.demo import run_demo


def test_demo_plays_through(tmp_path: Path) -> None:
    steps, report = run_demo(tmp_path / "demo")
    assert all(s.ok for s in steps)
    facts = {s.title.split(":")[0]: s.facts for s in steps}
    assert facts["Run 1 (spec 0.1.0)"]["delivered_to_y_sftp"][-2:] == ["ONC101_PKG0042.zip.md5",
                                                                       "ONC101_PKG0042.zip.sha256"]  # fmt: skip
    assert not any(n.endswith(".md5") for n in facts["Run 2 (spec 0.2.0)"]["delivered_to_x_s3"])
    assert facts["Run 3 (spec 2.0.0)"]["dispatcher_started"] == "study_pkg_delivery__v2"
    assert facts["Evidence"]["secret_values_in_logs"] == "none"
    assert facts["Evidence"]["changelog"] == ["0001.json", "0002.json"]
    assert "Change 2 at 2.0.0" in report.read_text()
