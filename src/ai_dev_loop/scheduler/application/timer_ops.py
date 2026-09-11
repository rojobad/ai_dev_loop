"""User-level systemd timer install/status/disable helpers for scheduler ticks."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import ProcessResult, run_process
from ai_dev_loop.scheduler.infrastructure.systemd_assets import (
    SERVICE_NAME,
    TIMER_NAME,
    load_service_template,
    load_timer_template,
    validate_packaged_assets,
)

OWNERSHIP_MARKER = "# ai_dev_loop packaged scheduler timer asset"
USER_UNIT_DIR = Path.home() / ".config/systemd/user"


@dataclass(frozen=True)
class TimerInstallResult:
    service_unit_path: str
    timer_unit_path: str
    installed: bool
    enabled: bool
    reloaded: bool


@dataclass(frozen=True)
class TimerStatusResult:
    service_unit_path: str
    timer_unit_path: str
    service_installed: bool
    timer_installed: bool
    service_content_matches: bool
    timer_content_matches: bool
    timer_enabled: str | None
    timer_active: str | None
    service_active: str | None
    ownership_ok: bool
    detail: str | None


@dataclass(frozen=True)
class TimerDisableResult:
    timer_unit_path: str
    disabled: bool
    stopped: bool
    detail: str | None


def _expected_service_text() -> str:
    return f"{OWNERSHIP_MARKER}\n{load_service_template()}"


def _expected_timer_text() -> str:
    return f"{OWNERSHIP_MARKER}\n{load_timer_template()}"


def _unit_paths(unit_dir: Path | None = None) -> tuple[Path, Path]:
    root = unit_dir or USER_UNIT_DIR
    return root / SERVICE_NAME, root / TIMER_NAME


def _read_unit(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _ownership_matches(content: str | None) -> bool:
    return content is not None and OWNERSHIP_MARKER in content


def _write_unit(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def install_scheduler_timer(
    *,
    enable: bool = False,
    unit_dir: Path | None = None,
    runner: Callable[..., ProcessResult] = run_process,
) -> TimerInstallResult:
    asset_errors = validate_packaged_assets()
    if asset_errors:
        raise ValidationError("packaged timer assets failed validation: " + "; ".join(asset_errors))

    service_path, timer_path = _unit_paths(unit_dir)
    existing_service = _read_unit(service_path)
    existing_timer = _read_unit(timer_path)
    if existing_service is not None and not _ownership_matches(existing_service):
        raise ValidationError(f"refusing to overwrite non-owned service unit at {service_path}")
    if existing_timer is not None and not _ownership_matches(existing_timer):
        raise ValidationError(f"refusing to overwrite non-owned timer unit at {timer_path}")

    installed = False
    if existing_service != _expected_service_text() or existing_timer != _expected_timer_text():
        _write_unit(service_path, _expected_service_text())
        _write_unit(timer_path, _expected_timer_text())
        installed = True

    reload = runner([*runner_systemctl(), "daemon-reload"], timeout=30.0)
    if reload.returncode != 0:
        raise ValidationError("systemctl --user daemon-reload failed after timer install")

    enabled = False
    if enable:
        enable_result = runner(
            [*runner_systemctl(), "enable", "--now", TIMER_NAME],
            timeout=30.0,
        )
        if enable_result.returncode != 0:
            raise ValidationError("systemctl --user enable --now failed for scheduler timer")
        enabled = True

    return TimerInstallResult(
        service_unit_path=str(service_path),
        timer_unit_path=str(timer_path),
        installed=installed,
        enabled=enabled,
        reloaded=True,
    )


def scheduler_timer_status(
    *,
    unit_dir: Path | None = None,
    runner: Callable[..., ProcessResult] = run_process,
) -> TimerStatusResult:
    service_path, timer_path = _unit_paths(unit_dir)
    service_content = _read_unit(service_path)
    timer_content = _read_unit(timer_path)
    service_installed = service_content is not None
    timer_installed = timer_content is not None
    ownership_ok = _ownership_matches(service_content) and _ownership_matches(timer_content)
    service_matches = service_content == _expected_service_text()
    timer_matches = timer_content == _expected_timer_text()

    timer_enabled = _show_property(runner, TIMER_NAME, "UnitFileState")
    timer_active = _show_property(runner, TIMER_NAME, "ActiveState")
    service_active = _show_property(runner, SERVICE_NAME, "ActiveState")

    detail = None
    if service_installed and not ownership_ok:
        detail = "installed unit files are not owned by ai_dev_loop"
    elif service_installed and (not service_matches or not timer_matches):
        detail = "installed unit files differ from packaged assets; reinstall with scheduler timer install"

    return TimerStatusResult(
        service_unit_path=str(service_path),
        timer_unit_path=str(timer_path),
        service_installed=service_installed,
        timer_installed=timer_installed,
        service_content_matches=service_matches,
        timer_content_matches=timer_matches,
        timer_enabled=timer_enabled,
        timer_active=timer_active,
        service_active=service_active,
        ownership_ok=ownership_ok,
        detail=detail,
    )


def disable_scheduler_timer(
    *,
    runner: Callable[..., ProcessResult] = run_process,
) -> TimerDisableResult:
    service_path, timer_path = _unit_paths()
    disable = runner(
        [*runner_systemctl(), "disable", "--now", TIMER_NAME],
        timeout=30.0,
    )
    if disable.returncode != 0:
        raise ValidationError("systemctl --user disable --now failed for scheduler timer")
    return TimerDisableResult(
        timer_unit_path=str(timer_path),
        disabled=True,
        stopped=True,
        detail=None,
    )


def runner_systemctl() -> Sequence[str]:
    return ("systemctl", "--user")


def _show_property(
    runner: Callable[..., ProcessResult],
    unit_name: str,
    property_name: str,
) -> str | None:
    result = runner(
        [*runner_systemctl(), "show", unit_name, f"-p{property_name}", "--value"],
        timeout=10.0,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


__all__ = [
    "OWNERSHIP_MARKER",
    "TimerDisableResult",
    "TimerInstallResult",
    "TimerStatusResult",
    "USER_UNIT_DIR",
    "disable_scheduler_timer",
    "install_scheduler_timer",
    "runner_systemctl",
    "scheduler_timer_status",
]
