"""Typer CLI entry point for ai_dev_loop."""

from __future__ import annotations

import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_loop import __version__
from ai_dev_loop.commands.abort import render_abort_output, run_abort
from ai_dev_loop.commands.config_cmd import run_validate_config
from ai_dev_loop.commands.doctor import render_doctor
from ai_dev_loop.commands.inspect import render_inspect
from ai_dev_loop.commands.integrations import (
    CodexIntegrationTarget,
    collect_bridge_status,
    install_desktop_bridge,
    install_integrations,
    list_desktop_sessions,
    remove_desktop_bridge,
    render_bridge_status,
    render_install_output,
    render_integrations_status,
    render_session_list,
    render_uninstall_output,
    uninstall_integrations,
)
from ai_dev_loop.commands.list_runs import render_list
from ai_dev_loop.commands.logs import render_logs
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run, render_prepare_output
from ai_dev_loop.commands.resume import render_resume_output, resume_run
from ai_dev_loop.commands.start import CODEX_TUI_WARNING, render_start_output, start_run
from ai_dev_loop.commands.status import render_status
from ai_dev_loop.errors import AiDevLoopError

app = typer.Typer(
    name="ai_dev_loop",
    help="Deterministic local orchestrator for Codex/Cursor development loops.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Configuration commands.")
integrations_app = typer.Typer(help="Global Codex integration commands.")
sessions_app = typer.Typer(help="Desktop session rollout bridge commands.")
integrations_app.add_typer(sessions_app, name="sessions")
app.add_typer(config_app, name="config")
app.add_typer(integrations_app, name="integrations")


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


OutputOption = Annotated[OutputFormat, typer.Option("--output", help="Output format.")]
DEFAULT_OUTPUT = OutputFormat.text

TargetOption = Annotated[
    CodexIntegrationTarget,
    typer.Option(
        "--target",
        help="Codex integration target: wsl-cli or codex-desktop-wsl.",
    ),
]
DEFAULT_TARGET = CodexIntegrationTarget.WSL_CLI

WindowsCodexHomeOption = Annotated[
    Path | None,
    typer.Option(
        "--windows-codex-home",
        help="Windows Codex home path (must end with .codex).",
    ),
]
WslDistroOption = Annotated[
    str | None,
    typer.Option("--wsl-distro", help="WSL distribution name for desktop hook commands."),
]
WslHookPythonOption = Annotated[
    str,
    typer.Option("--wsl-hook-python", help="Python executable used in desktop hook commands."),
]
WslHookScriptPathOption = Annotated[
    Path | None,
    typer.Option(
        "--wsl-hook-script-path",
        help="Override WSL hook script path for desktop hook commands.",
    ),
]


def _handle(fn: Callable[[], None]) -> None:
    try:
        fn()
    except AiDevLoopError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def cli_root(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """ai_dev_loop orchestrator CLI."""


@app.command("prepare")
def prepare_command(
    config_path: Annotated[
        Path | None,
        typer.Option("--config-path", help="Path to ai_dev_loop.yaml."),
    ] = None,
    project_name: Annotated[
        str | None,
        typer.Option("--project-name", help="Override project.name."),
    ] = None,
    repo_path: Annotated[
        Path | None,
        typer.Option("--repo-path", help="Target repository root."),
    ] = None,
    plan_path: Annotated[
        Path | None,
        typer.Option("--plan-path", help="Approved plan path inside the repository."),
    ] = None,
    prompt_source_path: Annotated[
        Path | None,
        typer.Option("--prompt-source-path", help="Prompt source path inside the repository."),
    ] = None,
    codex_session_id: Annotated[
        str | None,
        typer.Option("--codex-session-id", help="Exact active Codex session ID."),
    ] = None,
    cursor_command: Annotated[
        str | None,
        typer.Option("--cursor-command", help="Override cursor.command."),
    ] = None,
    cursor_model: Annotated[
        str | None,
        typer.Option("--cursor-model", help="Override cursor.model."),
    ] = None,
    cursor_output_format: Annotated[
        str | None,
        typer.Option("--cursor-output-format", help="Override cursor.output_format."),
    ] = None,
    codex_command: Annotated[
        str | None,
        typer.Option("--codex-command", help="Override codex.command."),
    ] = None,
    codex_review_model: Annotated[
        str | None,
        typer.Option("--codex-review-model", help="Override codex.review_model."),
    ] = None,
    codex_review_reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--codex-review-reasoning-effort",
            help="Override codex.review_reasoning_effort.",
        ),
    ] = None,
    review_skill: Annotated[
        str | None,
        typer.Option("--review-skill", help="Override codex.review_skill."),
    ] = None,
    max_review_iterations: Annotated[
        int | None,
        typer.Option("--max-review-iterations", help="Override workflow.max_review_iterations."),
    ] = None,
    cursor_timeout_minutes: Annotated[
        int | None,
        typer.Option("--cursor-timeout-minutes", help="Override workflow.cursor_timeout_minutes."),
    ] = None,
    codex_timeout_minutes: Annotated[
        int | None,
        typer.Option("--codex-timeout-minutes", help="Override workflow.codex_timeout_minutes."),
    ] = None,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Prepare a run from stdin prompt content without starting the loop."""

    def run() -> None:
        options = PrepareOptions(
            config_path=config_path,
            project_name=project_name,
            repo_path=repo_path,
            plan_path=plan_path,
            prompt_source_path=prompt_source_path,
            codex_session_id=codex_session_id,
            cursor_command=cursor_command,
            cursor_model=cursor_model,
            cursor_output_format=cursor_output_format,
            codex_command=codex_command,
            codex_review_model=codex_review_model,
            codex_review_reasoning_effort=codex_review_reasoning_effort,
            review_skill=review_skill,
            max_review_iterations=max_review_iterations,
            cursor_timeout_minutes=cursor_timeout_minutes,
            codex_timeout_minutes=codex_timeout_minutes,
            output=output.value,
        )
        result = prepare_run(options)
        typer.echo(render_prepare_output(result, output=output.value), nl=False)

    _handle(run)


@app.command("start")
def start_command(
    run_id: Annotated[str, typer.Argument(help="Prepared run identifier.")],
    update_tools: Annotated[
        bool,
        typer.Option(
            "--update-tools",
            help="Run official Cursor/Codex self-updaters for incompatible tools without prompting.",
        ),
    ] = False,
    skip_tool_update: Annotated[
        bool,
        typer.Option(
            "--skip-tool-update",
            help="Never run CLI self-updaters; fail on incompatible tools unless allowed.",
        ),
    ] = False,
    allow_incompatible_tools: Annotated[
        bool,
        typer.Option(
            "--allow-incompatible-tools",
            help="Continue even when required models are not listed by the installed CLIs.",
        ),
    ] = False,
) -> None:
    """Start the automated Cursor/Codex loop for a prepared run."""

    def run() -> None:
        from ai_dev_loop.runners.tool_updates import (
            ToolUpdateFlags,
            ToolUpdatePrompt,
            policy_from_flags,
        )

        def ask_callback(prompt: ToolUpdatePrompt) -> bool:
            typer.echo(
                f"{prompt.tool} CLI may be incompatible with required model "
                f"{prompt.required_model or '(unknown)'} "
                f"(version={prompt.installed_version or 'unknown'}). "
                f"{prompt.detail}"
            )
            return typer.confirm(
                f"Run `{prompt.command} update` now?",
                default=False,
            )

        flags = ToolUpdateFlags(
            update_tools=update_tools,
            skip_tool_update=skip_tool_update,
            allow_incompatible_tools=allow_incompatible_tools,
        )
        policy = policy_from_flags(
            flags,
            stdin_is_tty=sys.stdin.isatty(),
            ask_callback=ask_callback,
        )
        typer.echo(CODEX_TUI_WARNING)
        result = start_run(run_id, tool_policy=policy)
        typer.echo(render_start_output(result), nl=False)

    _handle(run)


@app.command("resume")
def resume_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier to resume.")],
    update_tools: Annotated[
        bool,
        typer.Option(
            "--update-tools",
            help="Run official Cursor/Codex self-updaters for incompatible tools without prompting.",
        ),
    ] = False,
    skip_tool_update: Annotated[
        bool,
        typer.Option(
            "--skip-tool-update",
            help="Never run CLI self-updaters; fail on incompatible tools unless allowed.",
        ),
    ] = False,
    allow_incompatible_tools: Annotated[
        bool,
        typer.Option(
            "--allow-incompatible-tools",
            help="Continue even when required models are not listed by the installed CLIs.",
        ),
    ] = False,
) -> None:
    """Resume an interrupted or checkpointed run."""

    def run() -> None:
        from ai_dev_loop.runners.tool_updates import (
            ToolUpdateFlags,
            ToolUpdatePrompt,
            policy_from_flags,
        )

        def ask_callback(prompt: ToolUpdatePrompt) -> bool:
            typer.echo(
                f"{prompt.tool} CLI may be incompatible with required model "
                f"{prompt.required_model or '(unknown)'} "
                f"(version={prompt.installed_version or 'unknown'}). "
                f"{prompt.detail}"
            )
            return typer.confirm(
                f"Run `{prompt.command} update` now?",
                default=False,
            )

        flags = ToolUpdateFlags(
            update_tools=update_tools,
            skip_tool_update=skip_tool_update,
            allow_incompatible_tools=allow_incompatible_tools,
        )
        policy = policy_from_flags(
            flags,
            stdin_is_tty=sys.stdin.isatty(),
            ask_callback=ask_callback,
        )
        typer.echo(CODEX_TUI_WARNING)
        result = resume_run(run_id, tool_policy=policy)
        typer.echo(render_resume_output(result), nl=False)

    _handle(run)


@app.command("status")
def status_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Show run status."""

    def run() -> None:
        typer.echo(render_status(run_id, output=output.value), nl=False)

    _handle(run)


@app.command("list")
def list_command(
    project: Annotated[
        str | None,
        typer.Option("--project", help="Filter by project slug."),
    ] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by run status.")] = None,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """List prepared and historical runs."""

    def run() -> None:
        typer.echo(render_list(project=project, status=status, output=output.value), nl=False)

    _handle(run)


@app.command("logs")
def logs_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
    component: Annotated[
        str | None,
        typer.Option("--component", help="Log component: cursor, codex, or ai_dev_loop."),
    ] = None,
) -> None:
    """Show run logs."""

    def run() -> None:
        typer.echo(render_logs(run_id, component=component), nl=False)

    _handle(run)


@app.command("inspect")
def inspect_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
    output: OutputOption = DEFAULT_OUTPUT,
    show_prompts: Annotated[
        bool,
        typer.Option("--show-prompts", help="Print prompt contents."),
    ] = False,
) -> None:
    """Inspect run artifacts."""

    def run() -> None:
        typer.echo(render_inspect(run_id, output=output.value, show_prompts=show_prompts), nl=False)

    _handle(run)


@app.command("abort")
def abort_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
) -> None:
    """Abort an active run."""

    def run() -> None:
        result = run_abort(run_id)
        typer.echo(render_abort_output(result), nl=False)

    _handle(run)


@app.command("doctor")
def doctor_command(
    repo_path: Annotated[
        Path | None,
        typer.Option("--repo", help="Validate target repository configuration."),
    ] = None,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Check local runtime, paths, and optional repository configuration."""

    def run() -> None:
        typer.echo(render_doctor(repo_path=repo_path, output=output.value), nl=False)

    _handle(run)


@config_app.command("validate")
def config_validate_command(
    repo_path: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config-path", help="Explicit ai_dev_loop.yaml path."),
    ] = None,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Validate repository ai_dev_loop.yaml configuration."""

    def run() -> None:
        typer.echo(
            run_validate_config(repo_path=repo_path, config_path=config_path, output=output.value),
            nl=False,
        )

    _handle(run)


@integrations_app.command("install")
def integrations_install_command(
    output: OutputOption = DEFAULT_OUTPUT,
    target: TargetOption = DEFAULT_TARGET,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_distro: WslDistroOption = None,
    wsl_hook_python: WslHookPythonOption = "python3",
    wsl_hook_script_path: WslHookScriptPathOption = None,
    install_session_bridge: Annotated[
        bool,
        typer.Option(
            "--install-session-bridge",
            help="Also install the desktop session rollout bridge.",
        ),
    ] = False,
) -> None:
    """Install global Codex skill and SessionStart hook."""

    def run() -> None:
        result = install_integrations(
            target=target,
            windows_codex_home=windows_codex_home,
            wsl_distro=wsl_distro,
            wsl_hook_python=wsl_hook_python,
            wsl_hook_script_path=wsl_hook_script_path,
            install_session_bridge=install_session_bridge,
        )
        typer.echo(render_install_output(result, output=output.value), nl=False)

    _handle(run)


@integrations_app.command("uninstall")
def integrations_uninstall_command(
    output: OutputOption = DEFAULT_OUTPUT,
    target: TargetOption = DEFAULT_TARGET,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_distro: WslDistroOption = None,
    wsl_hook_python: WslHookPythonOption = "python3",
    wsl_hook_script_path: WslHookScriptPathOption = None,
) -> None:
    """Remove ai_dev_loop global integration assets."""

    def run() -> None:
        result = uninstall_integrations(
            target=target,
            windows_codex_home=windows_codex_home,
            wsl_distro=wsl_distro,
            wsl_hook_python=wsl_hook_python,
            wsl_hook_script_path=wsl_hook_script_path,
        )
        typer.echo(render_uninstall_output(result, output=output.value), nl=False)

    _handle(run)


@integrations_app.command("status")
def integrations_status_command(
    output: OutputOption = DEFAULT_OUTPUT,
    target: TargetOption = DEFAULT_TARGET,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_distro: WslDistroOption = None,
    wsl_hook_python: WslHookPythonOption = "python3",
    wsl_hook_script_path: WslHookScriptPathOption = None,
) -> None:
    """Report global integration installation status."""

    def run() -> None:
        typer.echo(
            render_integrations_status(
                output=output.value,
                target=target,
                windows_codex_home=windows_codex_home,
                wsl_distro=wsl_distro,
                wsl_hook_python=wsl_hook_python,
                wsl_hook_script_path=wsl_hook_script_path,
            ),
            nl=False,
        )

    _handle(run)


@sessions_app.command("install")
def integrations_sessions_install_command(
    output: OutputOption = DEFAULT_OUTPUT,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_codex_home: Annotated[
        Path | None,
        typer.Option("--wsl-codex-home", help="Override WSL Codex home."),
    ] = None,
) -> None:
    """Install the desktop session rollout bridge symlink."""

    def run() -> None:
        status = install_desktop_bridge(
            wsl_codex_home=wsl_codex_home,
            windows_codex_home=windows_codex_home,
        )
        typer.echo(render_bridge_status(output=output.value, status=status), nl=False)

    _handle(run)


@sessions_app.command("status")
def integrations_sessions_status_command(
    output: OutputOption = DEFAULT_OUTPUT,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_codex_home: Annotated[
        Path | None,
        typer.Option("--wsl-codex-home", help="Override WSL Codex home."),
    ] = None,
) -> None:
    """Report desktop session bridge status."""

    def run() -> None:
        status = collect_bridge_status(
            wsl_codex_home=wsl_codex_home,
            windows_codex_home=windows_codex_home,
        )
        typer.echo(render_bridge_status(output=output.value, status=status), nl=False)

    _handle(run)


@sessions_app.command("list")
def integrations_sessions_list_command(
    output: OutputOption = DEFAULT_OUTPUT,
    windows_codex_home: WindowsCodexHomeOption = None,
    wsl_codex_home: Annotated[
        Path | None,
        typer.Option("--wsl-codex-home", help="Override WSL Codex home."),
    ] = None,
    desktop_sessions_dir: Annotated[
        Path | None,
        typer.Option(
            "--desktop-sessions-dir",
            help="Override desktop sessions directory for listing.",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum sessions to list.")] = 20,
) -> None:
    """List recent desktop rollout session IDs."""

    def run() -> None:
        entries = list_desktop_sessions(
            wsl_codex_home=wsl_codex_home,
            windows_codex_home=windows_codex_home,
            desktop_sessions_dir=desktop_sessions_dir,
            limit=limit,
        )
        typer.echo(render_session_list(entries, output=output.value), nl=False)

    _handle(run)


@sessions_app.command("remove")
def integrations_sessions_remove_command(
    output: OutputOption = DEFAULT_OUTPUT,
    wsl_codex_home: Annotated[
        Path | None,
        typer.Option("--wsl-codex-home", help="Override WSL Codex home."),
    ] = None,
) -> None:
    """Remove the desktop session rollout bridge symlink."""

    def run() -> None:
        status = remove_desktop_bridge(wsl_codex_home=wsl_codex_home)
        typer.echo(render_bridge_status(output=output.value, status=status), nl=False)

    _handle(run)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
