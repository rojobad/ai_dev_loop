"""Integration tests for global Codex integrations commands."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import assets, hooks_json
from ai_dev_loop.integrations.codex import install as integration_install
from ai_dev_loop.integrations.codex import paths as integration_paths

runner = CliRunner()


def test_first_time_install(isolated_integrations: Path, isolated_xdg: Path) -> None:
    result = integration_install.install_integrations(home=isolated_integrations)
    assert result.skill.action.value == "created"
    assert len(result.skills) == 2
    assert {skill.directory_name for skill in result.skills} == {
        "ai-dev-loop-handoff",
        "ai-dev-loop-controller",
    }
    assert all(skill.action.value == "created" for skill in result.skills)
    assert result.hook_script.action.value == "created"
    assert result.hooks_json.action.value == "created"
    assert integration_paths.skill_path(isolated_integrations).is_file()
    assert integration_paths.skill_path(
        isolated_integrations, directory_name="ai-dev-loop-controller"
    ).is_file()
    assert integration_paths.hook_script_path(isolated_integrations).is_file()
    assert integration_paths.hooks_json_path(isolated_integrations).is_file()


def test_idempotent_second_install(isolated_integrations: Path, isolated_xdg: Path) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    second = integration_install.install_integrations(home=isolated_integrations)
    assert second.skill.action.value == "current"
    assert all(skill.action.value == "current" for skill in second.skills)
    assert second.hook_script.action.value == "current"
    assert second.hooks_json.action.value == "current"


def test_install_with_existing_unrelated_hooks(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    hooks_path = integration_paths.hooks_json_path(isolated_integrations)
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
    integration_install.install_integrations(home=isolated_integrations)
    document = hooks_json.load_hooks_document(hooks_path)
    assert "PreToolUse" in document["hooks"]
    status = hooks_json.registration_status(
        document,
        expected_command=hooks_json.build_hook_command(
            integration_paths.hook_script_path(isolated_integrations)
        ),
    )
    assert status.present is True


def test_invalid_hooks_json_fails_without_overwrite(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    hooks_path = integration_paths.hooks_json_path(isolated_integrations)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValidationError):
        integration_install.install_integrations(home=isolated_integrations)
    assert hooks_path.read_text(encoding="utf-8") == "{bad"


def test_invalid_hooks_json_uninstall_leaves_installed_files(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    hooks_path = integration_paths.hooks_json_path(isolated_integrations)
    skill_path = integration_paths.skill_path(isolated_integrations)
    controller_path = integration_paths.skill_path(
        isolated_integrations, directory_name="ai-dev-loop-controller"
    )
    hook_path = integration_paths.hook_script_path(isolated_integrations)
    hooks_path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValidationError):
        integration_install.uninstall_integrations(home=isolated_integrations)

    assert skill_path.is_file()
    assert controller_path.is_file()
    assert hook_path.is_file()
    assert hooks_path.read_text(encoding="utf-8") == "{bad"


def test_uninstall_preserves_unrelated_hooks(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    hooks_path = integration_paths.hooks_json_path(isolated_integrations)
    document = hooks_json.load_hooks_document(hooks_path)
    document["hooks"]["PreToolUse"] = [
        {"matcher": ".*", "hooks": [{"type": "command", "command": "echo keep"}]}
    ]
    hooks_json.write_hooks_document(hooks_path, document, backup=False)

    result = integration_install.uninstall_integrations(home=isolated_integrations)
    assert result.skill.action.value == "removed"
    assert all(skill.action.value == "removed" for skill in result.skills)
    assert result.hook_script.action.value == "removed"
    assert result.hooks_json.action.value == "updated"
    assert not integration_paths.skill_path(isolated_integrations).is_file()
    assert not integration_paths.skill_path(
        isolated_integrations, directory_name="ai-dev-loop-controller"
    ).is_file()
    assert not integration_paths.hook_script_path(isolated_integrations).is_file()
    remaining = hooks_json.load_hooks_document(hooks_path)
    assert "PreToolUse" in remaining["hooks"]
    assert hooks_json.registration_status(remaining).present is False


def test_status_json_after_install_and_uninstall(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    installed = runner.invoke(app, ["integrations", "status", "--output", "json"])
    assert installed.exit_code == 0
    payload = json.loads(installed.stdout)
    assert payload["target"] == "wsl-cli"
    assert payload["skill_installed"] is True
    assert len(payload["skills"]) == 2
    assert payload["hook_registration_present"] is True
    assert payload["hook_trust_status"] == "unknown"

    integration_install.uninstall_integrations(home=isolated_integrations)
    removed = runner.invoke(app, ["integrations", "status", "--output", "json"])
    payload = json.loads(removed.stdout)
    assert payload["skill_installed"] is False
    assert payload["hook_registration_present"] is False
    assert all(not skill["installed"] for skill in payload["skills"])


def test_cli_install_and_uninstall(isolated_integrations: Path, isolated_xdg: Path) -> None:
    install_result = runner.invoke(app, ["integrations", "install", "--output", "json"])
    assert install_result.exit_code == 0
    payload = json.loads(install_result.stdout)
    assert payload["status"] == "installed"
    assert "trust_instruction" in payload

    uninstall_result = runner.invoke(app, ["integrations", "uninstall", "--output", "json"])
    assert uninstall_result.exit_code == 0
    assert json.loads(uninstall_result.stdout)["status"] == "uninstalled"


def test_doctor_reports_integration_checks(isolated_integrations: Path, isolated_xdg: Path) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    result = runner.invoke(app, ["doctor", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {check["name"] for check in payload["checks"]}
    assert "integration_skill" in names
    assert "integration_hook_script" in names
    assert "integration_hooks_json" in names
    assert "integration_hook_registration" in names


def test_installed_file_permissions(
    isolated_integrations: Path, isolated_xdg: Path, permission_test_root: Path
) -> None:
    if stat.S_IMODE(permission_test_root.stat().st_mode) != 0o700:
        pytest.skip("permission test root does not enforce modes")
    integration_install.install_integrations(home=isolated_integrations)
    skill = integration_paths.skill_path(isolated_integrations)
    hook = integration_paths.hook_script_path(isolated_integrations)
    hooks = integration_paths.hooks_json_path(isolated_integrations)
    assert stat.S_IMODE(skill.stat().st_mode) == 0o600
    assert stat.S_IMODE(hook.stat().st_mode) == 0o600
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o600
    assert stat.S_IMODE(skill.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(hook.parent.stat().st_mode) == 0o700


def test_skill_content_matches_package(isolated_integrations: Path, isolated_xdg: Path) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    skill = integration_paths.skill_path(isolated_integrations)
    assert assets.content_matches_package(skill, expected_text=assets.load_skill_content())


def test_uninstall_preserves_user_files_in_skill_directory(
    isolated_integrations: Path, isolated_xdg: Path
) -> None:
    integration_install.install_integrations(home=isolated_integrations)
    skill_dir = integration_paths.skill_directory(isolated_integrations)
    extra_file = skill_dir / "notes.txt"
    extra_file.write_text("keep me", encoding="utf-8")

    integration_install.uninstall_integrations(home=isolated_integrations)

    assert not integration_paths.skill_path(isolated_integrations).is_file()
    assert extra_file.is_file()
    assert extra_file.read_text(encoding="utf-8") == "keep me"
