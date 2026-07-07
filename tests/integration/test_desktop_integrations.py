"""Integration tests for Codex Desktop WSL bridge commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import hooks_json
from ai_dev_loop.integrations.codex import install as integration_install
from ai_dev_loop.integrations.codex import paths as integration_paths
from ai_dev_loop.integrations.codex.target import CodexIntegrationTarget

runner = CliRunner()


@pytest.fixture
def desktop_fixture(
    hermetic_tmp_path: Path,
    hermetic_home: Path,
    hermetic_xdg: Path,
    hermetic_codex_env: Path,
) -> dict[str, Path]:
    windows_codex = hermetic_tmp_path / "mnt" / "c" / "Users" / "WinUser" / ".codex"
    windows_profile = windows_codex.parent
    desktop_sessions = windows_codex / "sessions"
    desktop_sessions.mkdir(parents=True)
    (
        desktop_sessions / "rollout-2026-07-07T12-00-00-019abc00-1111-2222-3333-444444444444.jsonl"
    ).write_text("{}", encoding="utf-8")
    return {
        "wsl_home": hermetic_home,
        "wsl_codex": hermetic_codex_env,
        "windows_codex": windows_codex,
        "windows_profile": windows_profile,
        "desktop_sessions": desktop_sessions,
    }


def test_desktop_install_writes_windows_assets(desktop_fixture: dict[str, Path]) -> None:
    result = integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
    )
    assert result.skill.path == integration_paths.skill_path(desktop_fixture["windows_profile"])
    assert result.skill.action.value in {"created", "updated"}
    assert result.hooks_json.path == desktop_fixture["windows_codex"] / "hooks.json"
    assert integration_paths.hook_script_path(desktop_fixture["wsl_home"]).is_file()
    assert not (desktop_fixture["wsl_codex"] / "sessions" / "from-desktop").exists()


def test_desktop_hook_command_uses_wsl_exe(desktop_fixture: dict[str, Path]) -> None:
    integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
    )
    hooks_path = desktop_fixture["windows_codex"] / "hooks.json"
    document = hooks_json.load_hooks_document(hooks_path)
    hook_script = integration_paths.hook_script_path(desktop_fixture["wsl_home"])
    expected = f"wsl.exe -d Ubuntu --exec python3 {hook_script.resolve()}"
    status = hooks_json.registration_status(document, expected_command=expected)
    assert status.present is True
    assert status.command == expected


def test_desktop_install_with_explicit_bridge_flag(desktop_fixture: dict[str, Path]) -> None:
    integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
        install_session_bridge=True,
    )
    bridge = desktop_fixture["wsl_codex"] / "sessions" / "from-desktop"
    assert bridge.is_symlink()


def test_sessions_install_cli(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    result = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "install",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["bridge_present"] is True
    assert payload["rollout_count"] == 1


def test_sessions_status_json(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "install",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
        ],
    )
    result = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "status",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "bridge_link" in payload
    assert payload["rollout_count"] == 1


def test_sessions_list_cli(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "install",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
        ],
    )
    result = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "list",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sessions"][0]["session_id"] == "019abc00-1111-2222-3333-444444444444"


def test_sessions_remove_cli(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "install",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
        ],
    )
    bridge = desktop_fixture["wsl_codex"] / "sessions" / "from-desktop"
    result = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "remove",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
        ],
    )
    assert result.exit_code == 0
    assert not bridge.exists()
    assert desktop_fixture["desktop_sessions"].is_dir()


def test_sessions_install_rejects_mnt_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "/mnt/c/Users/WinUser/.codex")
    monkeypatch.setenv("CODEX_DESKTOP_HOME", "/tmp/windows/.codex")
    result = runner.invoke(app, ["integrations", "sessions", "install"])
    assert result.exit_code != 0
    assert "CODEX_HOME" in result.output


def test_sessions_install_cli_with_propagated_windows_env(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    result = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "install",
            "--wsl-codex-home",
            str(hermetic_desktop_cli_env),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert not str(hermetic_desktop_cli_env).startswith("/mnt/")


def test_desktop_uninstall_preserves_bridge_and_unrelated_hooks(
    desktop_fixture: dict[str, Path],
) -> None:
    hooks_path = desktop_fixture["windows_codex"] / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": ".*", "hooks": [{"type": "command", "command": "echo keep"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
        install_session_bridge=True,
    )
    integration_install.uninstall_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
    )
    bridge = desktop_fixture["wsl_codex"] / "sessions" / "from-desktop"
    assert bridge.is_symlink()
    assert integration_paths.hook_script_path(desktop_fixture["wsl_home"]).is_file()
    remaining = hooks_json.load_hooks_document(hooks_path)
    assert "PreToolUse" in remaining["hooks"]


def test_invalid_windows_hooks_json_fails_without_deleting_assets(
    desktop_fixture: dict[str, Path],
) -> None:
    hooks_path = desktop_fixture["windows_codex"] / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValidationError):
        integration_install.install_integrations(
            home=desktop_fixture["wsl_home"],
            target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
            windows_codex_home=desktop_fixture["windows_codex"],
            wsl_distro="Ubuntu",
        )
    assert hooks_path.read_text(encoding="utf-8") == "{bad"


def test_desktop_status_json(desktop_fixture: dict[str, Path]) -> None:
    integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
    )
    result = runner.invoke(
        app,
        [
            "integrations",
            "status",
            "--target",
            "codex-desktop-wsl",
            "--windows-codex-home",
            str(desktop_fixture["windows_codex"]),
            "--wsl-distro",
            "Ubuntu",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["target"] == "codex-desktop-wsl"
    assert payload["hook_registration_present"] is True
    assert payload["hook_registration_command"].startswith("wsl.exe")


def test_wsl_cli_backward_compatible(isolated_integrations: Path, isolated_xdg: Path) -> None:
    result = integration_install.install_integrations(home=isolated_integrations)
    assert result.target == CodexIntegrationTarget.WSL_CLI
    assert integration_paths.skill_path(isolated_integrations).is_file()


def test_doctor_includes_desktop_checks_when_detectable(
    desktop_fixture: dict[str, Path],
    hermetic_desktop_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DESKTOP_HOME", str(desktop_fixture["windows_codex"]))
    integration_install.install_integrations(
        home=desktop_fixture["wsl_home"],
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=desktop_fixture["windows_codex"],
        wsl_distro="Ubuntu",
        install_session_bridge=True,
    )
    result = runner.invoke(app, ["doctor", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {check["name"] for check in payload["checks"]}
    assert "integration_codex_desktop_wsl_skill" in names
    assert "integration_codex_desktop_wsl_session_bridge" in names
