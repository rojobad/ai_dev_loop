"""Resolve WSL invocation details for Codex Desktop hook commands."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import run_process

WSL_DISTRO_ENV = "AI_DEV_LOOP_WSL_DISTRO"
WSL_EXE = "wsl.exe"
_WINDOWS_CMD_METACHARACTERS = frozenset("&|<>^%()!")


@dataclass(frozen=True)
class WslInvocationConfig:
    distro: str
    hook_script: Path
    python_command: str

    @property
    def hook_command(self) -> str:
        return build_desktop_hook_command(
            distro=self.distro,
            hook_script=self.hook_script,
            python_command=self.python_command,
        )


def _validate_command_token(label: str, token: str) -> str:
    token = token.strip()
    if not token:
        raise ValidationError(f"{label} must not be empty.")
    if any(character in token for character in ("\n", "\r", '"')):
        raise ValidationError(
            f"{label} contains unsupported characters. Pass a plain value for {label.lower()}."
        )
    if any(character in token for character in _WINDOWS_CMD_METACHARACTERS):
        raise ValidationError(
            f"{label} contains Windows command metacharacters. "
            f"Pass a plain value for {label.lower()}."
        )
    return token


def _validate_wsl_distro_name(distro: str) -> str:
    return _validate_command_token("WSL distribution name", distro)


def _validate_python_command(python_command: str) -> str:
    return _validate_command_token("Python command", python_command)


def _validate_hook_script_path(path: str) -> str:
    return _validate_command_token("Hook script path", path)


def _quote_windows_command_token(token: str) -> str:
    if any(character.isspace() for character in token) or '"' in token:
        escaped = token.replace('"', '\\"')
        return f'"{escaped}"'
    return token


def build_desktop_hook_command(
    *,
    distro: str,
    hook_script: Path,
    python_command: str = "python3",
) -> str:
    validated_distro = _validate_wsl_distro_name(distro)
    validated_python = _validate_python_command(python_command)
    script_path = _validate_hook_script_path(str(hook_script.resolve()))
    return (
        f"{WSL_EXE} -d {_quote_windows_command_token(validated_distro)} "
        f"--exec {_quote_windows_command_token(validated_python)} "
        f"{_quote_windows_command_token(script_path)}"
    )


def _normalize_wsl_text(text: str) -> str:
    return text.replace("\x00", "").replace("\r", "")


def detect_default_wsl_distro() -> str | None:
    wsl_exe = _find_executable(WSL_EXE)
    if wsl_exe is None:
        return None

    result = run_process([wsl_exe, "-l", "-v"])
    if result.returncode != 0:
        return None

    lines = _normalize_wsl_text(result.stdout).splitlines()
    for line in lines:
        match = re.match(r"^\*?\s*(.+?)\s+(Running|Stopped)\s+\d+\s*$", line)
        if match:
            return match.group(1).strip()
    return None


def resolve_wsl_invocation(
    *,
    wsl_distro: str | None,
    hook_script: Path,
    python_command: str = "python3",
    require_hook_script: bool = True,
) -> WslInvocationConfig:
    if require_hook_script and not hook_script.is_file():
        raise ValidationError(
            f"WSL hook script not found at {hook_script}. "
            "Install the hook script in WSL before configuring the desktop target."
        )

    distro = wsl_distro
    if not distro:
        distro = os.environ.get(WSL_DISTRO_ENV, "").strip() or None
    if not distro:
        distro = detect_default_wsl_distro()
    if not distro:
        raise ValidationError(
            "Could not determine the WSL distribution name. "
            f"Pass --wsl-distro or set {WSL_DISTRO_ENV}."
        )

    validated_distro = _validate_wsl_distro_name(distro)
    validated_python = _validate_python_command(python_command)

    return WslInvocationConfig(
        distro=validated_distro,
        hook_script=hook_script,
        python_command=validated_python,
    )


def _find_executable(name: str) -> str | None:
    from shutil import which

    return which(name)
