"""Desktop session rollout bridge for Codex Desktop on Windows with WSL CLI."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.windows_home import resolve_windows_codex_home

BRIDGE_LINK_NAME = "from-desktop"
ROLLOUT_FILENAME_PATTERN = re.compile(
    r"rollout-([0-9T-]+)-([0-9a-f]{8}-[0-9a-f-]+)\.jsonl$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WslBridgePaths:
    wsl_codex_home: Path
    wsl_sessions_dir: Path
    bridge_link: Path


@dataclass(frozen=True)
class DesktopBridgePaths:
    wsl_codex_home: Path
    wsl_sessions_dir: Path
    bridge_link: Path
    desktop_sessions_dir: Path


@dataclass(frozen=True)
class DesktopBridgeStatus:
    wsl_codex_home: Path
    wsl_sessions_dir: Path
    bridge_link: Path
    bridge_present: bool
    bridge_target: str | None
    bridge_target_matches: bool | None
    desktop_sessions_dir: Path | None
    rollout_count: int
    warnings: list[str]


@dataclass(frozen=True)
class DesktopSessionEntry:
    timestamp: str
    session_id: str
    filename: str


def resolve_wsl_codex_home(*, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    env_value = os.environ.get("CODEX_HOME", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def resolve_wsl_bridge_paths(*, wsl_codex_home: Path | None = None) -> WslBridgePaths:
    codex_home = resolve_wsl_codex_home(explicit=wsl_codex_home)
    _reject_mnt_codex_home(codex_home)
    sessions_dir = codex_home / "sessions"
    return WslBridgePaths(
        wsl_codex_home=codex_home,
        wsl_sessions_dir=sessions_dir,
        bridge_link=sessions_dir / BRIDGE_LINK_NAME,
    )


def try_resolve_desktop_sessions_dir(
    *,
    windows_codex_home: Path | None = None,
) -> tuple[Path | None, str | None]:
    try:
        desktop_home = resolve_windows_codex_home(explicit=windows_codex_home)
    except ValidationError as exc:
        return None, str(exc)
    return desktop_home / "sessions", None


def resolve_bridge_paths(
    *,
    wsl_codex_home: Path | None = None,
    windows_codex_home: Path | None = None,
) -> DesktopBridgePaths:
    wsl_paths = resolve_wsl_bridge_paths(wsl_codex_home=wsl_codex_home)
    desktop_sessions, warning = try_resolve_desktop_sessions_dir(
        windows_codex_home=windows_codex_home
    )
    if desktop_sessions is None:
        raise ValidationError(
            warning
            or "Could not determine the Windows Codex home. "
            "Set CODEX_DESKTOP_HOME or pass --windows-codex-home."
        )
    return DesktopBridgePaths(
        wsl_codex_home=wsl_paths.wsl_codex_home,
        wsl_sessions_dir=wsl_paths.wsl_sessions_dir,
        bridge_link=wsl_paths.bridge_link,
        desktop_sessions_dir=desktop_sessions,
    )


def _reject_mnt_codex_home(codex_home: Path) -> None:
    codex_text = str(codex_home)
    if codex_text.startswith("/mnt/"):
        raise ValidationError(
            f"CODEX_HOME points at '{codex_home}' (Windows/DrvFS). "
            "Unset CODEX_HOME and use the native WSL ~/.codex home before installing "
            "the desktop session bridge."
        )


def _reject_symlinked_sessions_dir(sessions_dir: Path) -> None:
    if sessions_dir.is_symlink():
        raise ValidationError(
            f"'{sessions_dir}' is itself a symlink (whole-sessions share). "
            "Remove it and restore the native sessions directory before installing "
            "the desktop session bridge."
        )


def _resolved_symlink_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    if os.path.isabs(target):
        return Path(target).resolve()
    return (link.parent / target).resolve()


def _bridge_target_matches(link: Path, expected: Path) -> bool:
    resolved = _resolved_symlink_target(link)
    if resolved is None:
        return False
    return resolved == expected.resolve()


def count_rollout_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    count = 0
    for path in directory.rglob("rollout-*.jsonl"):
        if path.is_file():
            count += 1
    return count


def collect_bridge_status(
    *,
    wsl_codex_home: Path | None = None,
    windows_codex_home: Path | None = None,
) -> DesktopBridgeStatus:
    wsl_paths = resolve_wsl_bridge_paths(wsl_codex_home=wsl_codex_home)
    warnings: list[str] = []
    try:
        _reject_mnt_codex_home(wsl_paths.wsl_codex_home)
    except ValidationError as exc:
        warnings.append(str(exc))

    if wsl_paths.wsl_sessions_dir.is_symlink():
        warnings.append(
            f"'{wsl_paths.wsl_sessions_dir}' is itself a symlink (whole-sessions share)."
        )

    desktop_sessions_dir, windows_warning = try_resolve_desktop_sessions_dir(
        windows_codex_home=windows_codex_home
    )
    if windows_warning is not None:
        warnings.append(windows_warning)

    bridge_present = wsl_paths.bridge_link.is_symlink()
    bridge_target: str | None = None
    bridge_target_matches: bool | None = None
    if bridge_present:
        bridge_target = os.readlink(wsl_paths.bridge_link)
        if desktop_sessions_dir is not None:
            bridge_target_matches = _bridge_target_matches(
                wsl_paths.bridge_link,
                desktop_sessions_dir,
            )
            if not bridge_target_matches:
                warnings.append(
                    "Bridge symlink does not point at the resolved Windows desktop "
                    f"sessions directory. Expected '{desktop_sessions_dir}', "
                    f"but '{wsl_paths.bridge_link}' points to '{bridge_target}'. "
                    "Run: ai_dev_loop integrations sessions install"
                )

    rollout_count = 0
    if bridge_present and bridge_target_matches is not False:
        rollout_count = count_rollout_files(wsl_paths.bridge_link)

    if desktop_sessions_dir is not None and not desktop_sessions_dir.is_dir():
        warnings.append(f"Desktop sessions directory not found: {desktop_sessions_dir}")

    return DesktopBridgeStatus(
        wsl_codex_home=wsl_paths.wsl_codex_home,
        wsl_sessions_dir=wsl_paths.wsl_sessions_dir,
        bridge_link=wsl_paths.bridge_link,
        bridge_present=bridge_present,
        bridge_target=bridge_target,
        bridge_target_matches=bridge_target_matches,
        desktop_sessions_dir=desktop_sessions_dir,
        rollout_count=rollout_count,
        warnings=warnings,
    )


def install_desktop_bridge(
    *,
    wsl_codex_home: Path | None = None,
    windows_codex_home: Path | None = None,
) -> DesktopBridgeStatus:
    paths = resolve_bridge_paths(
        wsl_codex_home=wsl_codex_home,
        windows_codex_home=windows_codex_home,
    )
    _reject_mnt_codex_home(paths.wsl_codex_home)
    _reject_symlinked_sessions_dir(paths.wsl_sessions_dir)

    if not paths.wsl_sessions_dir.is_dir():
        paths.wsl_sessions_dir.mkdir(parents=True, exist_ok=True)

    if not paths.desktop_sessions_dir.is_dir():
        raise ValidationError(
            f"Desktop sessions folder not found: {paths.desktop_sessions_dir}. "
            "Verify the Windows Codex home and ensure Codex Desktop has created sessions."
        )

    if paths.bridge_link.exists() and not paths.bridge_link.is_symlink():
        raise ValidationError(f"Refusing to replace non-symlink bridge path: {paths.bridge_link}")

    if paths.bridge_link.is_symlink():
        paths.bridge_link.unlink()
    paths.bridge_link.symlink_to(paths.desktop_sessions_dir, target_is_directory=True)

    return collect_bridge_status(
        wsl_codex_home=paths.wsl_codex_home,
        windows_codex_home=paths.desktop_sessions_dir.parent,
    )


def remove_desktop_bridge(
    *,
    wsl_codex_home: Path | None = None,
    windows_codex_home: Path | None = None,
) -> DesktopBridgeStatus:
    wsl_paths = resolve_wsl_bridge_paths(wsl_codex_home=wsl_codex_home)
    if wsl_paths.bridge_link.is_symlink():
        wsl_paths.bridge_link.unlink()
    elif wsl_paths.bridge_link.exists():
        raise ValidationError(
            f"Refusing to remove non-symlink bridge path: {wsl_paths.bridge_link}"
        )
    return collect_bridge_status(
        wsl_codex_home=wsl_paths.wsl_codex_home,
        windows_codex_home=windows_codex_home,
    )


def list_desktop_sessions(
    *,
    wsl_codex_home: Path | None = None,
    windows_codex_home: Path | None = None,
    desktop_sessions_dir: Path | None = None,
    limit: int = 20,
) -> list[DesktopSessionEntry]:
    if desktop_sessions_dir is not None:
        sessions_dir = desktop_sessions_dir
    else:
        status = collect_bridge_status(
            wsl_codex_home=wsl_codex_home,
            windows_codex_home=windows_codex_home,
        )
        if not status.bridge_present:
            raise ValidationError(
                "Desktop session bridge is not installed. "
                "Run: ai_dev_loop integrations sessions install"
            )
        if status.bridge_target_matches is False:
            raise ValidationError(
                "Desktop session bridge points at an unexpected target. "
                "Run: ai_dev_loop integrations sessions install"
            )
        if status.desktop_sessions_dir is None:
            raise ValidationError(
                "Could not resolve the Windows desktop sessions directory. "
                "Pass --windows-codex-home or set CODEX_DESKTOP_HOME."
            )
        sessions_dir = status.desktop_sessions_dir

    if not sessions_dir.is_dir():
        raise ValidationError(f"Desktop sessions directory not found: {sessions_dir}")

    entries: list[tuple[float, DesktopSessionEntry]] = []
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        if not path.is_file():
            continue
        match = ROLLOUT_FILENAME_PATTERN.search(path.name)
        if not match:
            continue
        timestamp, session_id = match.groups()
        entries.append(
            (
                path.stat().st_mtime,
                DesktopSessionEntry(
                    timestamp=timestamp,
                    session_id=session_id,
                    filename=path.name,
                ),
            )
        )

    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in entries[:limit]]


def bridge_status_payload(status: DesktopBridgeStatus) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "wsl_codex_home": str(status.wsl_codex_home),
        "wsl_sessions_dir": str(status.wsl_sessions_dir),
        "bridge_link": str(status.bridge_link),
        "bridge_present": status.bridge_present,
        "bridge_target": status.bridge_target,
        "bridge_target_matches": status.bridge_target_matches,
        "desktop_sessions_dir": str(status.desktop_sessions_dir)
        if status.desktop_sessions_dir is not None
        else None,
        "rollout_count": status.rollout_count,
        "warnings": status.warnings,
    }


def render_bridge_status(*, output: str = "text", status: DesktopBridgeStatus) -> str:
    if output == "json":
        return json.dumps(bridge_status_payload(status), indent=2) + "\n"

    lines = [
        "ai_dev_loop integrations sessions status",
        f"WSL Codex home: {status.wsl_codex_home}",
        f"WSL sessions directory: {status.wsl_sessions_dir}",
        f"Bridge link: {status.bridge_link}",
        f"Bridge present: {status.bridge_present}",
    ]
    if status.bridge_target is not None:
        lines.append(f"Bridge target: {status.bridge_target}")
    if status.bridge_target_matches is not None:
        lines.append(f"Bridge target matches desktop sessions: {status.bridge_target_matches}")
    if status.desktop_sessions_dir is not None:
        lines.append(f"Desktop sessions directory: {status.desktop_sessions_dir}")
    lines.append(f"Reachable rollout files: {status.rollout_count}")
    for warning in status.warnings:
        lines.append(f"Warning: {warning}")
    if not status.bridge_present or status.bridge_target_matches is False:
        lines.append("Next action: ai_dev_loop integrations sessions install")
    return "\n".join(lines) + "\n"


def render_session_list(entries: list[DesktopSessionEntry], *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "sessions": [
                {
                    "timestamp": entry.timestamp,
                    "session_id": entry.session_id,
                    "filename": entry.filename,
                }
                for entry in entries
            ],
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = ["ai_dev_loop integrations sessions list", "Recent desktop sessions:"]
    if not entries:
        lines.append("  (none found)")
    else:
        for entry in entries:
            lines.append(f"  {entry.timestamp}  {entry.session_id}")
    return "\n".join(lines) + "\n"
