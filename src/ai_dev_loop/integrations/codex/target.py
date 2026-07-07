"""Codex integration target definitions and path resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import paths
from ai_dev_loop.integrations.codex.windows_home import resolve_windows_codex_home
from ai_dev_loop.integrations.codex.wsl_invocation import (
    WslInvocationConfig,
    resolve_wsl_invocation,
)


class CodexIntegrationTarget(StrEnum):
    WSL_CLI = "wsl-cli"
    CODEX_DESKTOP_WSL = "codex-desktop-wsl"


@dataclass(frozen=True)
class IntegrationTargetOptions:
    target: CodexIntegrationTarget = CodexIntegrationTarget.WSL_CLI
    wsl_home: Path | None = None
    windows_codex_home: Path | None = None
    wsl_distro: str | None = None
    wsl_hook_python: str = "python3"
    wsl_hook_script_path: Path | None = None
    require_hook_script: bool = True


@dataclass(frozen=True)
class IntegrationTargetContext:
    target: CodexIntegrationTarget
    wsl_home: Path
    skill_home: Path
    codex_home: Path
    hook_script_path: Path
    hooks_json_path: Path
    windows_codex_home: Path | None
    wsl_invocation: WslInvocationConfig | None


def resolve_target_context(options: IntegrationTargetOptions) -> IntegrationTargetContext:
    wsl_home = options.wsl_home if options.wsl_home is not None else Path.home()

    if options.target == CodexIntegrationTarget.WSL_CLI:
        return IntegrationTargetContext(
            target=options.target,
            wsl_home=wsl_home,
            skill_home=wsl_home,
            codex_home=wsl_home / ".codex",
            hook_script_path=options.wsl_hook_script_path or paths.hook_script_path(wsl_home),
            hooks_json_path=paths.hooks_json_path(wsl_home),
            windows_codex_home=None,
            wsl_invocation=None,
        )

    windows_codex_home = resolve_windows_codex_home(explicit=options.windows_codex_home)
    windows_profile = windows_codex_home.parent
    hook_script = options.wsl_hook_script_path or paths.hook_script_path(wsl_home)
    wsl_invocation = resolve_wsl_invocation(
        wsl_distro=options.wsl_distro,
        hook_script=hook_script,
        python_command=options.wsl_hook_python,
        require_hook_script=options.require_hook_script,
    )

    return IntegrationTargetContext(
        target=options.target,
        wsl_home=wsl_home,
        skill_home=windows_profile,
        codex_home=windows_codex_home,
        hook_script_path=hook_script,
        hooks_json_path=windows_codex_home / "hooks.json",
        windows_codex_home=windows_codex_home,
        wsl_invocation=wsl_invocation,
    )


def parse_target(value: str) -> CodexIntegrationTarget:
    try:
        return CodexIntegrationTarget(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in CodexIntegrationTarget)
        raise ValidationError(
            f"Unsupported integration target {value!r}. Supported targets: {supported}."
        ) from exc
