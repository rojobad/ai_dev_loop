"""Install, uninstall, and status for global Codex integrations."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import assets, hooks_json, paths
from ai_dev_loop.paths import ensure_app_dirs, ensure_dir
from ai_dev_loop.state import atomic_write_text

TRUST_INSTRUCTION = (
    "Open /hooks in Codex and trust the ai_dev_loop hook. "
    "The hook may be skipped until it is trusted."
)
TRUST_STATUS_UNKNOWN_DETAIL = (
    "Hook trust cannot be detected safely from local files. "
    "Open /hooks in Codex to verify trust status."
)


class AssetAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    CURRENT = "current"
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True)
class AssetInstallResult:
    path: Path
    action: AssetAction


@dataclass(frozen=True)
class InstallResult:
    skill: AssetInstallResult
    hook_script: AssetInstallResult
    hooks_json: AssetInstallResult
    hooks_json_backup: Path | None
    trust_instruction: str


@dataclass(frozen=True)
class UninstallResult:
    skill: AssetInstallResult
    hook_script: AssetInstallResult
    hooks_json: AssetInstallResult
    hooks_json_backup: Path | None


@dataclass(frozen=True)
class IntegrationStatus:
    schema_version: int
    skill_path: Path
    skill_installed: bool
    skill_matches_package: bool
    hook_script_path: Path
    hook_script_installed: bool
    hook_script_matches_package: bool
    hooks_json_path: Path
    hooks_json_exists: bool
    hooks_json_valid: bool
    hook_registration_present: bool
    hook_registration_duplicates: int
    hook_registration_command: str | None
    hook_trust_status: str
    hook_trust_detail: str
    remediation: list[str]


def _install_text_file(
    destination: Path,
    content: str,
    *,
    parent_mode: bool = True,
) -> AssetAction:
    if parent_mode:
        ensure_dir(destination.parent)
    if destination.is_file() and destination.read_text(encoding="utf-8") == content:
        return AssetAction.CURRENT
    action = AssetAction.UPDATED if destination.is_file() else AssetAction.CREATED
    atomic_write_text(destination, content, sensitive=True)
    return action


def install_integrations(*, home: Path | None = None) -> InstallResult:
    ensure_app_dirs()
    home_path = home if home is not None else Path.home()
    skill_destination = paths.skill_path(home_path)
    hook_destination = paths.hook_script_path(home_path)
    hooks_destination = paths.hooks_json_path(home_path)

    skill_content = assets.load_skill_content()
    hook_content = assets.load_hook_script_content()

    skill_action = _install_text_file(skill_destination, skill_content)
    ensure_dir(hook_destination.parent)
    hook_action = _install_text_file(hook_destination, hook_content)

    document = hooks_json.load_hooks_document(hooks_destination)
    merged, changed = hooks_json.merge_hook_registration(document, hook_script=hook_destination)
    backup_path: Path | None = None
    hooks_existed = hooks_destination.is_file()
    if changed:
        backup_path = hooks_json.write_hooks_document(
            hooks_destination, merged, backup=hooks_existed
        )
        hooks_action = AssetAction.UPDATED if hooks_existed else AssetAction.CREATED
    else:
        hooks_action = AssetAction.CURRENT

    return InstallResult(
        skill=AssetInstallResult(skill_destination, skill_action),
        hook_script=AssetInstallResult(hook_destination, hook_action),
        hooks_json=AssetInstallResult(hooks_destination, hooks_action),
        hooks_json_backup=backup_path,
        trust_instruction=TRUST_INSTRUCTION,
    )


def uninstall_integrations(*, home: Path | None = None) -> UninstallResult:
    home_path = home if home is not None else Path.home()
    skill_destination = paths.skill_path(home_path)
    hook_destination = paths.hook_script_path(home_path)
    hooks_destination = paths.hooks_json_path(home_path)

    hooks_action = AssetAction.NOT_FOUND
    backup_path: Path | None = None
    hooks_update: dict[str, Any] | None = None

    if hooks_destination.is_file():
        document = hooks_json.load_hooks_document(hooks_destination)
        updated, removed = hooks_json.remove_all_ai_dev_loop_hooks(
            document, hook_script=hook_destination
        )
        if removed:
            hooks_update = updated
            hooks_action = AssetAction.UPDATED

    if skill_destination.is_file():
        skill_destination.unlink()
        with suppress(OSError):
            skill_destination.parent.rmdir()
        skill_result = AssetInstallResult(skill_destination, AssetAction.REMOVED)
    else:
        skill_result = AssetInstallResult(skill_destination, AssetAction.NOT_FOUND)

    if hook_destination.is_file():
        hook_destination.unlink()
        hook_result = AssetInstallResult(hook_destination, AssetAction.REMOVED)
    else:
        hook_result = AssetInstallResult(hook_destination, AssetAction.NOT_FOUND)

    if hooks_update is not None:
        backup_path = hooks_json.write_hooks_document(hooks_destination, hooks_update, backup=True)

    return UninstallResult(
        skill=skill_result,
        hook_script=hook_result,
        hooks_json=AssetInstallResult(hooks_destination, hooks_action),
        hooks_json_backup=backup_path,
    )


def collect_integration_status(*, home: Path | None = None) -> IntegrationStatus:
    home_path = home if home is not None else Path.home()
    skill_destination = paths.skill_path(home_path)
    hook_destination = paths.hook_script_path(home_path)
    hooks_destination = paths.hooks_json_path(home_path)

    skill_content = assets.load_skill_content()
    hook_content = assets.load_hook_script_content()

    skill_installed = skill_destination.is_file()
    hook_installed = hook_destination.is_file()
    hooks_exists = hooks_destination.is_file()

    hooks_valid = False
    registration_present = False
    duplicate_count = 0
    registration_command: str | None = None

    if hooks_exists:
        try:
            document = hooks_json.load_hooks_document(hooks_destination)
            hooks_valid = True
            status = hooks_json.registration_status(
                document,
                expected_command=hooks_json.build_hook_command(hook_destination)
                if hook_installed
                else None,
            )
            registration_present = status.present
            duplicate_count = status.duplicate_count
            registration_command = status.command
        except ValidationError:
            hooks_valid = False

    remediation: list[str] = []
    if not skill_installed or not hook_installed or not registration_present or not hooks_valid:
        remediation.append("Run: ai_dev_loop integrations install")
    if skill_installed and not assets.content_matches_package(
        skill_destination, expected_text=skill_content
    ):
        remediation.append("Skill file differs from current package content; reinstall to update.")
    if hook_installed and not assets.content_matches_package(
        hook_destination, expected_text=hook_content
    ):
        remediation.append("Hook script differs from current package content; reinstall to update.")

    return IntegrationStatus(
        schema_version=1,
        skill_path=skill_destination,
        skill_installed=skill_installed,
        skill_matches_package=assets.content_matches_package(
            skill_destination, expected_text=skill_content
        ),
        hook_script_path=hook_destination,
        hook_script_installed=hook_installed,
        hook_script_matches_package=assets.content_matches_package(
            hook_destination, expected_text=hook_content
        ),
        hooks_json_path=hooks_destination,
        hooks_json_exists=hooks_exists,
        hooks_json_valid=hooks_valid,
        hook_registration_present=registration_present,
        hook_registration_duplicates=duplicate_count,
        hook_registration_command=registration_command,
        hook_trust_status="unknown",
        hook_trust_detail=TRUST_STATUS_UNKNOWN_DETAIL,
        remediation=remediation,
    )


def integration_status_payload(*, home: Path | None = None) -> dict[str, Any]:
    status = collect_integration_status(home=home)
    return {
        "schema_version": status.schema_version,
        "skill_installed": status.skill_installed,
        "skill_path": str(status.skill_path),
        "skill_matches_package": status.skill_matches_package,
        "hook_script_installed": status.hook_script_installed,
        "hook_script_path": str(status.hook_script_path),
        "hook_script_matches_package": status.hook_script_matches_package,
        "hooks_json_exists": status.hooks_json_exists,
        "hooks_json_path": str(status.hooks_json_path),
        "hooks_json_valid": status.hooks_json_valid,
        "hook_registration_present": status.hook_registration_present,
        "hook_registration_duplicates": status.hook_registration_duplicates,
        "hook_registration_command": status.hook_registration_command,
        "hook_trust_status": status.hook_trust_status,
        "hook_trust_detail": status.hook_trust_detail,
        "remediation": status.remediation,
    }


def render_integrations_status(*, output: str = "text", home: Path | None = None) -> str:
    status = collect_integration_status(home=home)
    if output == "json":
        return json.dumps(integration_status_payload(home=home), indent=2) + "\n"

    lines = [
        "ai_dev_loop integrations status",
        f"Skill installed: {status.skill_installed} ({status.skill_path})",
        f"Skill matches package: {status.skill_matches_package}",
        f"Hook script installed: {status.hook_script_installed} ({status.hook_script_path})",
        f"Hook script matches package: {status.hook_script_matches_package}",
        f"hooks.json present: {status.hooks_json_exists} ({status.hooks_json_path})",
        f"hooks.json valid: {status.hooks_json_valid}",
        f"Hook registration present: {status.hook_registration_present}",
    ]
    if status.hook_registration_duplicates:
        lines.append(f"Duplicate hook registrations: {status.hook_registration_duplicates}")
    lines.extend(
        [
            f"Hook trust status: {status.hook_trust_status}",
            status.hook_trust_detail,
        ]
    )
    if status.remediation:
        lines.append("Remediation:")
        lines.extend(f"- {item}" for item in status.remediation)
    if status.hook_registration_present:
        lines.append(TRUST_INSTRUCTION)
    return "\n".join(lines) + "\n"


def render_install_output(result: InstallResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "status": "installed",
            "skill": {"path": str(result.skill.path), "action": result.skill.action.value},
            "hook_script": {
                "path": str(result.hook_script.path),
                "action": result.hook_script.action.value,
            },
            "hooks_json": {
                "path": str(result.hooks_json.path),
                "action": result.hooks_json.action.value,
            },
            "hooks_json_backup": str(result.hooks_json_backup)
            if result.hooks_json_backup
            else None,
            "trust_instruction": result.trust_instruction,
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        "ai_dev_loop integrations install",
        f"Skill: {result.skill.action.value} ({result.skill.path})",
        f"Hook script: {result.hook_script.action.value} ({result.hook_script.path})",
        f"hooks.json: {result.hooks_json.action.value} ({result.hooks_json.path})",
    ]
    if result.hooks_json_backup is not None:
        lines.append(f"hooks.json backup: {result.hooks_json_backup}")
    lines.append(result.trust_instruction)
    return "\n".join(lines) + "\n"


def render_uninstall_output(result: UninstallResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "status": "uninstalled",
            "skill": {"path": str(result.skill.path), "action": result.skill.action.value},
            "hook_script": {
                "path": str(result.hook_script.path),
                "action": result.hook_script.action.value,
            },
            "hooks_json": {
                "path": str(result.hooks_json.path),
                "action": result.hooks_json.action.value,
            },
            "hooks_json_backup": str(result.hooks_json_backup)
            if result.hooks_json_backup
            else None,
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        "ai_dev_loop integrations uninstall",
        f"Skill: {result.skill.action.value} ({result.skill.path})",
        f"Hook script: {result.hook_script.action.value} ({result.hook_script.path})",
        f"hooks.json: {result.hooks_json.action.value} ({result.hooks_json.path})",
    ]
    if result.hooks_json_backup is not None:
        lines.append(f"hooks.json backup: {result.hooks_json_backup}")
    return "\n".join(lines) + "\n"


def doctor_integration_checks(*, home: Path | None = None) -> list[dict[str, str | bool]]:
    status = collect_integration_status(home=home)
    checks: list[dict[str, str | bool]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add(
        "integration_skill",
        status.skill_installed and status.skill_matches_package,
        f"{status.skill_path} installed={status.skill_installed} matches_package={status.skill_matches_package}",
    )
    add(
        "integration_hook_script",
        status.hook_script_installed and status.hook_script_matches_package,
        (
            f"{status.hook_script_path} installed={status.hook_script_installed} "
            f"matches_package={status.hook_script_matches_package}"
        ),
    )
    add(
        "integration_hooks_json",
        status.hooks_json_exists and status.hooks_json_valid,
        f"{status.hooks_json_path} exists={status.hooks_json_exists} valid={status.hooks_json_valid}",
    )
    add(
        "integration_hook_registration",
        status.hook_registration_present and status.hook_registration_duplicates == 0,
        (
            f"present={status.hook_registration_present} "
            f"duplicates={status.hook_registration_duplicates}"
        ),
    )
    add(
        "integration_hook_trust",
        True,
        f"{status.hook_trust_status}: {status.hook_trust_detail}",
    )
    if status.remediation:
        add("integration_remediation", False, "; ".join(status.remediation))
    elif status.hook_registration_present:
        add("integration_remediation", True, TRUST_INSTRUCTION)
    return checks
