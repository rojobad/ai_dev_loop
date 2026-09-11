"""Unit tests for Phase 17.7 scheduler timer install/status/disable."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.scheduler.application.timer_ops import (
    OWNERSHIP_MARKER,
    disable_scheduler_timer,
    install_scheduler_timer,
    scheduler_timer_status,
)


def _fake_runner_factory() -> dict[str, list[list[str]]]:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: object) -> ProcessResult:
        calls.append(list(args))
        return ProcessResult(args=list(args), returncode=0, stdout="enabled\n", stderr="")

    return {"runner": runner, "calls": calls}


def test_timer_install_writes_owned_units_and_reloads(tmp_path: Path) -> None:
    fake = _fake_runner_factory()
    result = install_scheduler_timer(
        enable=False,
        unit_dir=tmp_path / "systemd/user",
        runner=fake["runner"],
    )
    service_path = Path(result.service_unit_path)
    timer_path = Path(result.timer_unit_path)
    assert service_path.is_file()
    assert timer_path.is_file()
    assert OWNERSHIP_MARKER in service_path.read_text(encoding="utf-8")
    service_text = service_path.read_text(encoding="utf-8")
    assert "ai_dev_loop scheduler tick" in service_text
    assert "/usr/bin/env ai_dev_loop scheduler tick" in service_text
    assert "Environment=PATH=%h/.local/bin:" in service_text
    assert fake["calls"] == [["systemctl", "--user", "daemon-reload"]]


def test_timer_install_enable_invokes_systemctl(tmp_path: Path) -> None:
    fake = _fake_runner_factory()
    install_scheduler_timer(
        enable=True,
        unit_dir=tmp_path / "systemd/user",
        runner=fake["runner"],
    )
    assert ["systemctl", "--user", "enable", "--now", "ai-dev-loop-scheduler-tick.timer"] in fake[
        "calls"
    ]


def test_timer_install_refuses_non_owned_existing_unit(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "ai-dev-loop-scheduler-tick.service").write_text(
        "[Service]\nExecStart=other\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="refusing to overwrite non-owned"):
        install_scheduler_timer(unit_dir=unit_dir)


def test_timer_status_reports_installed_units(tmp_path: Path) -> None:
    fake = _fake_runner_factory()
    install_scheduler_timer(
        unit_dir=tmp_path / "systemd/user",
        runner=fake["runner"],
    )
    status = scheduler_timer_status(
        unit_dir=tmp_path / "systemd/user",
        runner=fake["runner"],
    )
    assert status.service_installed
    assert status.timer_installed
    assert status.ownership_ok
    assert status.service_content_matches
    assert status.timer_content_matches


def test_timer_disable_calls_systemctl(tmp_path: Path) -> None:
    fake = _fake_runner_factory()
    result = disable_scheduler_timer(runner=fake["runner"])
    assert result.disabled
    assert fake["calls"] == [
        ["systemctl", "--user", "disable", "--now", "ai-dev-loop-scheduler-tick.timer"]
    ]
