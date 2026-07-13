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
from ai_dev_loop.integrations.codex.desktop_bridge import (
    DesktopBridgeStatus,
    collect_bridge_status,
    install_desktop_bridge,
    render_bridge_status,
)
from ai_dev_loop.integrations.codex.target import (
    CodexIntegrationTarget,
    IntegrationTargetContext,
    IntegrationTargetOptions,
    resolve_target_context,
)
from ai_dev_loop.integrations.codex.windows_home import CODEX_DESKTOP_HOME_ENV
from ai_dev_loop.integrations.codex.wsl_invocation import WSL_DISTRO_ENV
from ai_dev_loop.paths import ensure_app_dirs, ensure_dir
from ai_dev_loop.state import atomic_write_text

TRUST_INSTRUCTION = (
    "Open /hooks in Codex and trust the ai_dev_loop hook. "
    "The hook may be skipped until it is trusted."
)
DESKTOP_TRUST_INSTRUCTION = (
    "Open /hooks in Codex Desktop and trust the ai_dev_loop hook. "
    "Trusting the WSL CLI hook browser is not sufficient. "
    "The hook may be skipped until it is trusted."
)
TRUST_STATUS_UNKNOWN_DETAIL = (
    "Hook trust cannot be detected safely from local files. "
    "Open /hooks in Codex to verify trust status."
)
SESSION_BRIDGE_NEXT_ACTION = "ai_dev_loop integrations sessions install"


class AssetAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    CURRENT = "current"
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    PRESERVED = "preserved"


@dataclass(frozen=True)
class AssetInstallResult:
    path: Path
    action: AssetAction


@dataclass(frozen=True)
class SkillAssetResult:
    directory_name: str
    path: Path
    action: AssetAction
    matches_package: bool = False


@dataclass(frozen=True)
class InstallResult:
    target: CodexIntegrationTarget
    skill: AssetInstallResult
    skills: tuple[SkillAssetResult, ...]
    hook_script: AssetInstallResult
    hooks_json: AssetInstallResult
    hooks_json_backup: Path | None
    trust_instruction: str
    session_bridge_status: DesktopBridgeStatus | None
    next_actions: list[str]


@dataclass(frozen=True)
class UninstallResult:
    target: CodexIntegrationTarget
    skill: AssetInstallResult
    skills: tuple[SkillAssetResult, ...]
    hook_script: AssetInstallResult
    hooks_json: AssetInstallResult
    hooks_json_backup: Path | None


@dataclass(frozen=True)
class IntegrationStatus:
    schema_version: int
    target: CodexIntegrationTarget
    wsl_home: Path
    skill_home: Path
    codex_home: Path
    windows_codex_home: Path | None
    skill_path: Path
    skill_installed: bool
    skill_matches_package: bool
    skills: tuple[SkillAssetResult, ...]
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
    session_bridge_status: DesktopBridgeStatus | None
    remediation: list[str]


def _resolve_wsl_distro(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    import os

    from ai_dev_loop.integrations.codex.wsl_invocation import detect_default_wsl_distro

    return os.environ.get(WSL_DISTRO_ENV, "").strip() or detect_default_wsl_distro() or None


def _resolve_windows_codex_home_option(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    import os

    from ai_dev_loop.integrations.codex.windows_home import detect_windows_userprofile

    env_value = os.environ.get(CODEX_DESKTOP_HOME_ENV, "").strip()
    if env_value:
        return Path(env_value)
    profile = detect_windows_userprofile()
    if profile is not None:
        return profile / ".codex"
    return None


def _collect_bridge_status_for_target(
    *,
    target: CodexIntegrationTarget,
    wsl_home: Path,
    windows_codex_home: Path | None,
) -> DesktopBridgeStatus | None:
    if target != CodexIntegrationTarget.CODEX_DESKTOP_WSL or windows_codex_home is None:
        return None
    try:
        return collect_bridge_status(
            wsl_codex_home=wsl_home / ".codex",
            windows_codex_home=windows_codex_home,
        )
    except ValidationError:
        return None


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


def _expected_hook_command(context: IntegrationTargetContext) -> str | None:
    if context.target == CodexIntegrationTarget.WSL_CLI:
        if not context.hook_script_path.is_file():
            return None
        return hooks_json.build_hook_command(context.hook_script_path)
    if context.wsl_invocation is None:
        return None
    return context.wsl_invocation.hook_command


def _install_wsl_hook_script(context: IntegrationTargetContext) -> AssetInstallResult:
    hook_content = assets.load_hook_script_content()
    ensure_dir(context.hook_script_path.parent)
    action = _install_text_file(context.hook_script_path, hook_content)
    return AssetInstallResult(context.hook_script_path, action)


def install_integrations(
    *,
    home: Path | None = None,
    target: CodexIntegrationTarget | str = CodexIntegrationTarget.WSL_CLI,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
    wsl_hook_python: str = "python3",
    wsl_hook_script_path: Path | None = None,
    install_session_bridge: bool = False,
) -> InstallResult:
    ensure_app_dirs()
    if isinstance(target, str):
        target = CodexIntegrationTarget(target)

    wsl_distro = _resolve_wsl_distro(wsl_distro)
    windows_codex_home = _resolve_windows_codex_home_option(windows_codex_home)

    context = resolve_target_context(
        IntegrationTargetOptions(
            target=target,
            wsl_home=home,
            windows_codex_home=windows_codex_home,
            wsl_distro=wsl_distro,
            wsl_hook_python=wsl_hook_python,
            wsl_hook_script_path=wsl_hook_script_path,
            require_hook_script=False,
        )
    )

    skill_results: list[SkillAssetResult] = []
    for descriptor in assets.OWNED_SKILLS:
        destination = paths.skill_path_for(descriptor, context.skill_home)
        content = assets.load_skill_content(descriptor)
        action = _install_text_file(destination, content)
        skill_results.append(
            SkillAssetResult(
                directory_name=descriptor.directory_name,
                path=destination,
                action=action,
                matches_package=True,
            )
        )
    handoff_skill = next(
        item for item in skill_results if item.directory_name == assets.SKILL_DIRECTORY_NAME
    )
    hooks_destination = context.hooks_json_path
    hook_result = _install_wsl_hook_script(context)

    if context.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL:
        context = resolve_target_context(
            IntegrationTargetOptions(
                target=target,
                wsl_home=home,
                windows_codex_home=windows_codex_home,
                wsl_distro=wsl_distro,
                wsl_hook_python=wsl_hook_python,
                wsl_hook_script_path=context.hook_script_path,
                require_hook_script=True,
            )
        )

    hook_command = _expected_hook_command(context)
    if hook_command is None:
        raise ValidationError("Could not determine the hook registration command.")

    document = hooks_json.load_hooks_document(hooks_destination)
    merged, changed = hooks_json.merge_hook_registration(
        document,
        hook_script=context.hook_script_path,
        command=hook_command,
    )
    backup_path: Path | None = None
    hooks_existed = hooks_destination.is_file()
    if changed:
        backup_path = hooks_json.write_hooks_document(
            hooks_destination, merged, backup=hooks_existed
        )
        hooks_action = AssetAction.UPDATED if hooks_existed else AssetAction.CREATED
    else:
        hooks_action = AssetAction.CURRENT

    bridge_status: DesktopBridgeStatus | None = None
    next_actions: list[str] = []
    if context.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL:
        if install_session_bridge:
            bridge_status = install_desktop_bridge(
                wsl_codex_home=context.wsl_home / ".codex",
                windows_codex_home=context.windows_codex_home,
            )
        else:
            bridge_status = collect_bridge_status(
                wsl_codex_home=context.wsl_home / ".codex",
                windows_codex_home=context.windows_codex_home,
            )
            if not bridge_status.bridge_present:
                next_actions.append(SESSION_BRIDGE_NEXT_ACTION)

    trust_instruction = (
        DESKTOP_TRUST_INSTRUCTION
        if context.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL
        else TRUST_INSTRUCTION
    )

    return InstallResult(
        target=context.target,
        skill=AssetInstallResult(handoff_skill.path, handoff_skill.action),
        skills=tuple(skill_results),
        hook_script=hook_result,
        hooks_json=AssetInstallResult(hooks_destination, hooks_action),
        hooks_json_backup=backup_path,
        trust_instruction=trust_instruction,
        session_bridge_status=bridge_status,
        next_actions=next_actions,
    )


def uninstall_integrations(
    *,
    home: Path | None = None,
    target: CodexIntegrationTarget | str = CodexIntegrationTarget.WSL_CLI,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
    wsl_hook_python: str = "python3",
    wsl_hook_script_path: Path | None = None,
) -> UninstallResult:
    if isinstance(target, str):
        target = CodexIntegrationTarget(target)

    wsl_distro = _resolve_wsl_distro(wsl_distro)
    windows_codex_home = _resolve_windows_codex_home_option(windows_codex_home)

    context = resolve_target_context(
        IntegrationTargetOptions(
            target=target,
            wsl_home=home,
            windows_codex_home=windows_codex_home,
            wsl_distro=wsl_distro,
            wsl_hook_python=wsl_hook_python,
            wsl_hook_script_path=wsl_hook_script_path,
            require_hook_script=False,
        )
    )

    skill_results: list[SkillAssetResult] = []
    hooks_destination = context.hooks_json_path

    hooks_action = AssetAction.NOT_FOUND
    backup_path: Path | None = None
    hooks_update: dict[str, Any] | None = None

    expected_command = _expected_hook_command(context)
    if hooks_destination.is_file():
        document = hooks_json.load_hooks_document(hooks_destination)
        updated, removed = hooks_json.remove_all_ai_dev_loop_hooks(
            document,
            hook_script=context.hook_script_path,
            managed=True,
            expected_command=expected_command,
        )
        if removed:
            hooks_update = updated
            hooks_action = AssetAction.UPDATED

    for descriptor in assets.OWNED_SKILLS:
        destination = paths.skill_path_for(descriptor, context.skill_home)
        if destination.is_file():
            destination.unlink()
            with suppress(OSError):
                destination.parent.rmdir()
            skill_results.append(
                SkillAssetResult(
                    directory_name=descriptor.directory_name,
                    path=destination,
                    action=AssetAction.REMOVED,
                )
            )
        else:
            skill_results.append(
                SkillAssetResult(
                    directory_name=descriptor.directory_name,
                    path=destination,
                    action=AssetAction.NOT_FOUND,
                )
            )
    handoff_skill = next(
        item for item in skill_results if item.directory_name == assets.SKILL_DIRECTORY_NAME
    )

    if context.target == CodexIntegrationTarget.WSL_CLI:
        if context.hook_script_path.is_file():
            context.hook_script_path.unlink()
            hook_result = AssetInstallResult(context.hook_script_path, AssetAction.REMOVED)
        else:
            hook_result = AssetInstallResult(context.hook_script_path, AssetAction.NOT_FOUND)
    else:
        hook_result = AssetInstallResult(context.hook_script_path, AssetAction.PRESERVED)

    if hooks_update is not None:
        backup_path = hooks_json.write_hooks_document(hooks_destination, hooks_update, backup=True)

    return UninstallResult(
        target=context.target,
        skill=AssetInstallResult(handoff_skill.path, handoff_skill.action),
        skills=tuple(skill_results),
        hook_script=hook_result,
        hooks_json=AssetInstallResult(hooks_destination, hooks_action),
        hooks_json_backup=backup_path,
    )


def collect_integration_status(
    *,
    home: Path | None = None,
    target: CodexIntegrationTarget | str = CodexIntegrationTarget.WSL_CLI,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
    wsl_hook_python: str = "python3",
    wsl_hook_script_path: Path | None = None,
) -> IntegrationStatus:
    if isinstance(target, str):
        target = CodexIntegrationTarget(target)

    wsl_distro = _resolve_wsl_distro(wsl_distro)
    windows_codex_home = _resolve_windows_codex_home_option(windows_codex_home)
    wsl_home = home if home is not None else Path.home()
    bridge_status = _collect_bridge_status_for_target(
        target=target,
        wsl_home=wsl_home,
        windows_codex_home=windows_codex_home,
    )
    remediation_items: list[str] = []

    try:
        context = resolve_target_context(
            IntegrationTargetOptions(
                target=target,
                wsl_home=home,
                windows_codex_home=windows_codex_home,
                wsl_distro=wsl_distro,
                wsl_hook_python=wsl_hook_python,
                wsl_hook_script_path=wsl_hook_script_path,
                require_hook_script=False,
            )
        )
    except ValidationError:
        remediation_items.append(f"Run: ai_dev_loop integrations install --target {target.value}")
        if bridge_status is not None and not bridge_status.bridge_present:
            remediation_items.append(f"Run: {SESSION_BRIDGE_NEXT_ACTION}")
        return IntegrationStatus(
            schema_version=2,
            target=target,
            wsl_home=wsl_home,
            skill_home=windows_codex_home.parent
            if target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and windows_codex_home
            else wsl_home,
            codex_home=windows_codex_home
            if target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and windows_codex_home
            else wsl_home / ".codex",
            windows_codex_home=windows_codex_home,
            skill_path=paths.skill_path(
                windows_codex_home.parent
                if target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and windows_codex_home
                else wsl_home
            ),
            skill_installed=False,
            skill_matches_package=False,
            skills=tuple(
                SkillAssetResult(
                    directory_name=descriptor.directory_name,
                    path=paths.skill_path_for(
                        descriptor,
                        windows_codex_home.parent
                        if target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and windows_codex_home
                        else wsl_home,
                    ),
                    action=AssetAction.NOT_FOUND,
                    matches_package=False,
                )
                for descriptor in assets.OWNED_SKILLS
            ),
            hook_script_path=paths.hook_script_path(wsl_home),
            hook_script_installed=False,
            hook_script_matches_package=False,
            hooks_json_path=(windows_codex_home / "hooks.json")
            if target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and windows_codex_home
            else paths.hooks_json_path(wsl_home),
            hooks_json_exists=False,
            hooks_json_valid=False,
            hook_registration_present=False,
            hook_registration_duplicates=0,
            hook_registration_command=None,
            hook_trust_status="unknown",
            hook_trust_detail=TRUST_STATUS_UNKNOWN_DETAIL,
            session_bridge_status=bridge_status,
            remediation=remediation_items,
        )

    skill_destination = paths.skill_path(context.skill_home)
    hook_destination = context.hook_script_path
    hooks_destination = context.hooks_json_path

    skill_content = assets.load_skill_content()
    hook_content = assets.load_hook_script_content()

    skill_statuses: list[SkillAssetResult] = []
    any_skill_missing = False
    for descriptor in assets.OWNED_SKILLS:
        destination = paths.skill_path_for(descriptor, context.skill_home)
        expected = assets.load_skill_content(descriptor)
        installed = destination.is_file()
        matches = assets.content_matches_package(destination, expected_text=expected)
        if not installed:
            any_skill_missing = True
        if installed and not matches:
            remediation_items.append(
                f"Skill {descriptor.directory_name} differs from package content; "
                "reinstall to update."
            )
        skill_statuses.append(
            SkillAssetResult(
                directory_name=descriptor.directory_name,
                path=destination,
                action=AssetAction.CURRENT if installed else AssetAction.NOT_FOUND,
                matches_package=matches,
            )
        )

    skill_installed = skill_destination.is_file()
    hook_installed = hook_destination.is_file()
    hooks_exists = hooks_destination.is_file()

    hooks_valid = False
    registration_present = False
    duplicate_count = 0
    registration_command: str | None = None
    expected_command = _expected_hook_command(context)

    if hooks_exists:
        try:
            document = hooks_json.load_hooks_document(hooks_destination)
            hooks_valid = True
            status = hooks_json.registration_status(
                document,
                expected_command=expected_command,
            )
            registration_present = status.present
            duplicate_count = status.duplicate_count
            registration_command = status.command
        except ValidationError:
            hooks_valid = False

    install_cmd = f"ai_dev_loop integrations install --target {context.target.value}"
    if (
        not skill_installed
        or any_skill_missing
        or not hook_installed
        or not registration_present
        or not hooks_valid
    ):
        remediation_items.append(f"Run: {install_cmd}")
    if (
        skill_installed
        and not assets.content_matches_package(skill_destination, expected_text=skill_content)
        and not any(
            item.startswith("Skill ai-dev-loop-handoff differs") for item in remediation_items
        )
    ):
        remediation_items.append(
            "Skill file differs from current package content; reinstall to update."
        )
    if hook_installed and not assets.content_matches_package(
        hook_destination, expected_text=hook_content
    ):
        remediation_items.append(
            "Hook script differs from current package content; reinstall to update."
        )

    bridge_status = _collect_bridge_status_for_target(
        target=context.target,
        wsl_home=context.wsl_home,
        windows_codex_home=context.windows_codex_home,
    )
    if context.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL and bridge_status is not None:
        if not bridge_status.bridge_present:
            remediation_items.append(f"Run: {SESSION_BRIDGE_NEXT_ACTION}")
        for warning in bridge_status.warnings:
            remediation_items.append(warning)

    return IntegrationStatus(
        schema_version=2,
        target=context.target,
        wsl_home=context.wsl_home,
        skill_home=context.skill_home,
        codex_home=context.codex_home,
        windows_codex_home=context.windows_codex_home,
        skill_path=skill_destination,
        skill_installed=skill_installed,
        skill_matches_package=assets.content_matches_package(
            skill_destination, expected_text=skill_content
        ),
        skills=tuple(skill_statuses),
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
        session_bridge_status=bridge_status,
        remediation=remediation_items,
    )


def integration_status_payload(
    *,
    home: Path | None = None,
    target: CodexIntegrationTarget | str = CodexIntegrationTarget.WSL_CLI,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
    wsl_hook_python: str = "python3",
    wsl_hook_script_path: Path | None = None,
) -> dict[str, Any]:
    status = collect_integration_status(
        home=home,
        target=target,
        windows_codex_home=windows_codex_home,
        wsl_distro=wsl_distro,
        wsl_hook_python=wsl_hook_python,
        wsl_hook_script_path=wsl_hook_script_path,
    )
    payload: dict[str, Any] = {
        "schema_version": status.schema_version,
        "target": status.target.value,
        "wsl_home": str(status.wsl_home),
        "skill_home": str(status.skill_home),
        "codex_home": str(status.codex_home),
        "windows_codex_home": str(status.windows_codex_home)
        if status.windows_codex_home is not None
        else None,
        "skill_installed": status.skill_installed,
        "skill_path": str(status.skill_path),
        "skill_matches_package": status.skill_matches_package,
        "skills": [
            {
                "directory_name": skill.directory_name,
                "path": str(skill.path),
                "installed": skill.action != AssetAction.NOT_FOUND,
                "matches_package": skill.matches_package,
            }
            for skill in status.skills
        ],
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
    if status.session_bridge_status is not None:
        payload["session_bridge"] = {
            "bridge_present": status.session_bridge_status.bridge_present,
            "bridge_link": str(status.session_bridge_status.bridge_link),
            "bridge_target": status.session_bridge_status.bridge_target,
            "bridge_target_matches": status.session_bridge_status.bridge_target_matches,
            "desktop_sessions_dir": str(status.session_bridge_status.desktop_sessions_dir)
            if status.session_bridge_status.desktop_sessions_dir is not None
            else None,
            "rollout_count": status.session_bridge_status.rollout_count,
            "warnings": status.session_bridge_status.warnings,
        }
    return payload


def render_integrations_status(
    *,
    output: str = "text",
    home: Path | None = None,
    target: CodexIntegrationTarget | str = CodexIntegrationTarget.WSL_CLI,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
    wsl_hook_python: str = "python3",
    wsl_hook_script_path: Path | None = None,
) -> str:
    status = collect_integration_status(
        home=home,
        target=target,
        windows_codex_home=windows_codex_home,
        wsl_distro=wsl_distro,
        wsl_hook_python=wsl_hook_python,
        wsl_hook_script_path=wsl_hook_script_path,
    )
    if output == "json":
        return (
            json.dumps(
                integration_status_payload(
                    home=home,
                    target=target,
                    windows_codex_home=windows_codex_home,
                    wsl_distro=wsl_distro,
                    wsl_hook_python=wsl_hook_python,
                    wsl_hook_script_path=wsl_hook_script_path,
                ),
                indent=2,
            )
            + "\n"
        )

    lines = [
        "ai_dev_loop integrations status",
        f"Target: {status.target.value}",
        f"WSL home: {status.wsl_home}",
    ]
    if status.windows_codex_home is not None:
        lines.append(f"Windows Codex home: {status.windows_codex_home}")
    lines.extend(
        [
            f"Skill installed: {status.skill_installed} ({status.skill_path})",
            f"Skill matches package: {status.skill_matches_package}",
        ]
    )
    for skill in status.skills:
        lines.append(
            f"Skill {skill.directory_name}: installed={skill.action != AssetAction.NOT_FOUND} "
            f"matches_package={skill.matches_package} ({skill.path})"
        )
    lines.extend(
        [
            f"Hook script installed: {status.hook_script_installed} ({status.hook_script_path})",
            f"Hook script matches package: {status.hook_script_matches_package}",
            f"hooks.json present: {status.hooks_json_exists} ({status.hooks_json_path})",
            f"hooks.json valid: {status.hooks_json_valid}",
            f"Hook registration present: {status.hook_registration_present}",
        ]
    )
    if status.hook_registration_command:
        lines.append(f"Hook registration command: {status.hook_registration_command}")
    if status.hook_registration_duplicates:
        lines.append(f"Duplicate hook registrations: {status.hook_registration_duplicates}")
    if status.session_bridge_status is not None:
        bridge = status.session_bridge_status
        lines.append(f"Session bridge present: {bridge.bridge_present} ({bridge.bridge_link})")
        lines.append(f"Reachable rollout files: {bridge.rollout_count}")
        for warning in bridge.warnings:
            lines.append(f"Warning: {warning}")
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
        trust = (
            DESKTOP_TRUST_INSTRUCTION
            if status.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL
            else TRUST_INSTRUCTION
        )
        lines.append(trust)
    return "\n".join(lines) + "\n"


def render_install_output(result: InstallResult, *, output: str = "text") -> str:
    if output == "json":
        payload: dict[str, Any] = {
            "schema_version": 2,
            "target": result.target.value,
            "status": "installed",
            "skill": {"path": str(result.skill.path), "action": result.skill.action.value},
            "skills": [
                {
                    "directory_name": skill.directory_name,
                    "path": str(skill.path),
                    "action": skill.action.value,
                }
                for skill in result.skills
            ],
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
            "next_actions": result.next_actions,
        }
        if result.session_bridge_status is not None:
            payload["session_bridge"] = {
                "bridge_present": result.session_bridge_status.bridge_present,
                "rollout_count": result.session_bridge_status.rollout_count,
            }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        "ai_dev_loop integrations install",
        f"Target: {result.target.value}",
    ]
    for skill in result.skills:
        lines.append(f"Skill {skill.directory_name}: {skill.action.value} ({skill.path})")
    lines.extend(
        [
            f"Hook script: {result.hook_script.action.value} ({result.hook_script.path})",
            f"hooks.json: {result.hooks_json.action.value} ({result.hooks_json.path})",
        ]
    )
    if result.hooks_json_backup is not None:
        lines.append(f"hooks.json backup: {result.hooks_json_backup}")
    if result.session_bridge_status is not None:
        lines.append(
            "Session bridge present: "
            f"{result.session_bridge_status.bridge_present} "
            f"({result.session_bridge_status.bridge_link})"
        )
    for action in result.next_actions:
        lines.append(f"Next action: {action}")
    lines.append(result.trust_instruction)
    return "\n".join(lines) + "\n"


def render_uninstall_output(result: UninstallResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 2,
            "target": result.target.value,
            "status": "uninstalled",
            "skill": {"path": str(result.skill.path), "action": result.skill.action.value},
            "skills": [
                {
                    "directory_name": skill.directory_name,
                    "path": str(skill.path),
                    "action": skill.action.value,
                }
                for skill in result.skills
            ],
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
        f"Target: {result.target.value}",
    ]
    for skill in result.skills:
        lines.append(f"Skill {skill.directory_name}: {skill.action.value} ({skill.path})")
    lines.extend(
        [
            f"Hook script: {result.hook_script.action.value} ({result.hook_script.path})",
            f"hooks.json: {result.hooks_json.action.value} ({result.hooks_json.path})",
        ]
    )
    if result.hooks_json_backup is not None:
        lines.append(f"hooks.json backup: {result.hooks_json_backup}")
    return "\n".join(lines) + "\n"


def doctor_integration_checks(
    *,
    home: Path | None = None,
    include_desktop: bool = True,
    windows_codex_home: Path | None = None,
    wsl_distro: str | None = None,
) -> list[dict[str, str | bool]]:
    checks: list[dict[str, str | bool]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    for target in (CodexIntegrationTarget.WSL_CLI,):
        status = collect_integration_status(home=home, target=target)
        prefix = "integration"
        add(
            f"{prefix}_skill",
            status.skill_installed and status.skill_matches_package,
            (
                f"{status.skill_path} installed={status.skill_installed} "
                f"matches_package={status.skill_matches_package}"
            ),
        )
        for skill in status.skills:
            add(
                f"{prefix}_skill_{skill.directory_name.replace('-', '_')}",
                skill.action != AssetAction.NOT_FOUND and skill.matches_package,
                (
                    f"{skill.path} installed={skill.action != AssetAction.NOT_FOUND} "
                    f"matches_package={skill.matches_package}"
                ),
            )
        add(
            f"{prefix}_hook_script",
            status.hook_script_installed and status.hook_script_matches_package,
            (
                f"{status.hook_script_path} installed={status.hook_script_installed} "
                f"matches_package={status.hook_script_matches_package}"
            ),
        )
        add(
            f"{prefix}_hooks_json",
            status.hooks_json_exists and status.hooks_json_valid,
            (
                f"{status.hooks_json_path} exists={status.hooks_json_exists} "
                f"valid={status.hooks_json_valid}"
            ),
        )
        add(
            f"{prefix}_hook_registration",
            status.hook_registration_present and status.hook_registration_duplicates == 0,
            (
                f"present={status.hook_registration_present} "
                f"duplicates={status.hook_registration_duplicates}"
            ),
        )
        add(
            f"{prefix}_hook_trust",
            True,
            f"{status.hook_trust_status}: {status.hook_trust_detail}",
        )

    desktop_detectable = windows_codex_home is not None
    if not desktop_detectable:
        from ai_dev_loop.integrations.codex.windows_home import detect_windows_userprofile

        desktop_detectable = detect_windows_userprofile() is not None

    if include_desktop and desktop_detectable:
        try:
            desktop_status = collect_integration_status(
                home=home,
                target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
                windows_codex_home=windows_codex_home,
                wsl_distro=wsl_distro,
            )
            prefix = "integration_codex_desktop_wsl"
            add(
                f"{prefix}_skill",
                desktop_status.skill_installed and desktop_status.skill_matches_package,
                (
                    f"{desktop_status.skill_path} installed={desktop_status.skill_installed} "
                    f"matches_package={desktop_status.skill_matches_package}"
                ),
            )
            for skill in desktop_status.skills:
                add(
                    f"{prefix}_skill_{skill.directory_name.replace('-', '_')}",
                    skill.action != AssetAction.NOT_FOUND and skill.matches_package,
                    (
                        f"{skill.path} installed={skill.action != AssetAction.NOT_FOUND} "
                        f"matches_package={skill.matches_package}"
                    ),
                )
            add(
                f"{prefix}_hook_script",
                desktop_status.hook_script_installed and desktop_status.hook_script_matches_package,
                (
                    f"{desktop_status.hook_script_path} "
                    f"installed={desktop_status.hook_script_installed} "
                    f"matches_package={desktop_status.hook_script_matches_package}"
                ),
            )
            add(
                f"{prefix}_hooks_json",
                desktop_status.hooks_json_exists and desktop_status.hooks_json_valid,
                (
                    f"{desktop_status.hooks_json_path} exists={desktop_status.hooks_json_exists} "
                    f"valid={desktop_status.hooks_json_valid}"
                ),
            )
            add(
                f"{prefix}_hook_registration",
                desktop_status.hook_registration_present
                and desktop_status.hook_registration_duplicates == 0,
                (
                    f"present={desktop_status.hook_registration_present} "
                    f"duplicates={desktop_status.hook_registration_duplicates}"
                ),
            )
            add(
                f"{prefix}_hook_trust",
                True,
                f"{desktop_status.hook_trust_status}: {desktop_status.hook_trust_detail}",
            )
            if desktop_status.session_bridge_status is not None:
                bridge = desktop_status.session_bridge_status
                add(
                    f"{prefix}_session_bridge",
                    bridge.bridge_present
                    and bridge.bridge_target_matches is not False
                    and not bridge.warnings,
                    (
                        f"present={bridge.bridge_present} "
                        f"rollout_count={bridge.rollout_count} "
                        f"warnings={len(bridge.warnings)}"
                    ),
                )
        except ValidationError as exc:
            add("integration_codex_desktop_wsl", False, str(exc))

    wsl_status = collect_integration_status(home=home, target=CodexIntegrationTarget.WSL_CLI)
    if wsl_status.remediation:
        add("integration_remediation", False, "; ".join(wsl_status.remediation))
    elif wsl_status.hook_registration_present:
        add("integration_remediation", True, TRUST_INSTRUCTION)

    return checks


__all__ = [
    "AssetAction",
    "InstallResult",
    "IntegrationStatus",
    "UninstallResult",
    "collect_integration_status",
    "doctor_integration_checks",
    "install_integrations",
    "integration_status_payload",
    "render_bridge_status",
    "render_install_output",
    "render_integrations_status",
    "render_uninstall_output",
    "uninstall_integrations",
]
