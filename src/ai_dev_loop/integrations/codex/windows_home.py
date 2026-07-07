"""Resolve the Windows Codex home from WSL."""

from __future__ import annotations

import os
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import run_process

CODEX_DESKTOP_HOME_ENV = "CODEX_DESKTOP_HOME"


def _normalize_codex_home(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute():
        raise ValidationError(f"Windows Codex home must be absolute: {path}")
    if resolved.name != ".codex":
        raise ValidationError(
            f"Windows Codex home must end with '.codex', got: {resolved}. "
            "Pass the Codex home directory, not the Windows user profile."
        )
    return resolved


def detect_windows_userprofile() -> Path | None:
    """Best-effort detection of the Windows user profile via cmd.exe and wslpath."""

    cmd_exe = _find_executable("cmd.exe")
    wslpath = _find_executable("wslpath")
    if cmd_exe is None or wslpath is None:
        return None

    profile_result = run_process([cmd_exe, "/c", "echo %USERPROFILE%"])
    if profile_result.returncode != 0:
        return None
    profile_text = profile_result.stdout.strip().replace("\r", "").replace("\x00", "")
    if not profile_text or "%USERPROFILE%" in profile_text:
        return None

    wslpath_result = run_process([wslpath, "-u", profile_text])
    if wslpath_result.returncode != 0:
        return None
    unix_profile = wslpath_result.stdout.strip()
    if not unix_profile:
        return None
    return Path(unix_profile)


def resolve_windows_codex_home(*, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return _normalize_codex_home(explicit)

    env_value = os.environ.get(CODEX_DESKTOP_HOME_ENV, "").strip()
    if env_value:
        return _normalize_codex_home(Path(env_value))

    profile = detect_windows_userprofile()
    if profile is None:
        raise ValidationError(
            "Could not determine the Windows Codex home. "
            f"Set {CODEX_DESKTOP_HOME_ENV} or pass --windows-codex-home."
        )
    return _normalize_codex_home(profile / ".codex")


def _find_executable(name: str) -> str | None:
    from shutil import which

    return which(name)
