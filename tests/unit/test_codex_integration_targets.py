"""Unit tests for Codex integration target resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.target import (
    CodexIntegrationTarget,
    IntegrationTargetOptions,
    resolve_target_context,
)
from ai_dev_loop.integrations.codex.windows_home import resolve_windows_codex_home
from ai_dev_loop.integrations.codex.wsl_invocation import (
    build_desktop_hook_command,
    resolve_wsl_invocation,
)


@pytest.fixture
def desktop_homes(hermetic_tmp_path: Path) -> dict[str, Path]:
    wsl_home = hermetic_tmp_path / "wsl-home"
    wsl_home.mkdir()
    hook_script = wsl_home / ".codex" / "hooks" / "ai_dev_loop_session_start.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("# hook", encoding="utf-8")

    windows_codex = hermetic_tmp_path / "mnt" / "c" / "Users" / "WinUser" / ".codex"
    windows_codex.mkdir(parents=True)
    (windows_codex / "sessions").mkdir()

    return {
        "wsl_home": wsl_home,
        "hook_script": hook_script,
        "windows_codex": windows_codex,
    }


def test_desktop_homes_use_native_linux_temp(hermetic_tmp_path: Path) -> None:
    assert not str(hermetic_tmp_path).startswith("/mnt/")


def test_resolve_windows_codex_home_explicit(desktop_homes: dict[str, Path]) -> None:
    resolved = resolve_windows_codex_home(explicit=desktop_homes["windows_codex"])
    assert resolved == desktop_homes["windows_codex"].resolve()


def test_resolve_windows_codex_home_rejects_profile_path(desktop_homes: dict[str, Path]) -> None:
    with pytest.raises(ValidationError, match="must end with '.codex'"):
        resolve_windows_codex_home(explicit=desktop_homes["windows_codex"].parent)


def test_build_desktop_hook_command(desktop_homes: dict[str, Path]) -> None:
    command = build_desktop_hook_command(
        distro="Ubuntu",
        hook_script=desktop_homes["hook_script"],
    )
    assert command.startswith("wsl.exe -d Ubuntu --exec python3 ")
    assert str(desktop_homes["hook_script"].resolve()) in command


def test_build_desktop_hook_command_quotes_distro_with_spaces(
    desktop_homes: dict[str, Path],
) -> None:
    command = build_desktop_hook_command(
        distro="Ubuntu Preview",
        hook_script=desktop_homes["hook_script"],
    )
    assert 'wsl.exe -d "Ubuntu Preview" --exec python3 ' in command
    assert str(desktop_homes["hook_script"].resolve()) in command


def test_build_desktop_hook_command_rejects_unsafe_distro_chars() -> None:
    with pytest.raises(ValidationError, match="unsupported characters"):
        build_desktop_hook_command(
            distro='Ubuntu"Preview',
            hook_script=Path("/tmp/hook.py"),
        )


def test_build_desktop_hook_command_quotes_python_with_spaces(
    desktop_homes: dict[str, Path],
) -> None:
    command = build_desktop_hook_command(
        distro="Ubuntu",
        hook_script=desktop_homes["hook_script"],
        python_command="/home/user/my python/bin/python3",
    )
    assert '--exec "/home/user/my python/bin/python3" ' in command


def test_build_desktop_hook_command_rejects_python_metacharacters() -> None:
    with pytest.raises(ValidationError, match="Windows command metacharacters"):
        build_desktop_hook_command(
            distro="Ubuntu",
            hook_script=Path("/tmp/hook.py"),
            python_command="python3&whoami",
        )


def test_build_desktop_hook_command_rejects_hook_path_metacharacters(
    desktop_homes: dict[str, Path],
) -> None:
    unsafe_hook = desktop_homes["hook_script"].with_name("hook|bad.py")
    unsafe_hook.write_text("# hook", encoding="utf-8")
    with pytest.raises(ValidationError, match="Windows command metacharacters"):
        build_desktop_hook_command(
            distro="Ubuntu",
            hook_script=unsafe_hook,
        )


def test_desktop_target_context(desktop_homes: dict[str, Path]) -> None:
    context = resolve_target_context(
        IntegrationTargetOptions(
            target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
            wsl_home=desktop_homes["wsl_home"],
            windows_codex_home=desktop_homes["windows_codex"],
            wsl_distro="Ubuntu",
        )
    )
    assert context.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL
    assert context.skill_home == desktop_homes["windows_codex"].parent
    assert context.hooks_json_path == desktop_homes["windows_codex"] / "hooks.json"
    assert context.hook_script_path == desktop_homes["hook_script"]
    assert context.wsl_invocation is not None
    assert "wsl.exe" in context.wsl_invocation.hook_command


def test_wsl_cli_target_context(desktop_homes: dict[str, Path]) -> None:
    context = resolve_target_context(
        IntegrationTargetOptions(
            target=CodexIntegrationTarget.WSL_CLI,
            wsl_home=desktop_homes["wsl_home"],
        )
    )
    assert context.skill_home == desktop_homes["wsl_home"]
    assert context.hooks_json_path == desktop_homes["wsl_home"] / ".codex" / "hooks.json"
    assert context.wsl_invocation is None


def test_resolve_wsl_invocation_requires_hook_for_install(desktop_homes: dict[str, Path]) -> None:
    missing = desktop_homes["wsl_home"] / ".codex" / "hooks" / "missing.py"
    with pytest.raises(ValidationError, match="WSL hook script not found"):
        resolve_wsl_invocation(
            wsl_distro="Ubuntu",
            hook_script=missing,
            require_hook_script=True,
        )
