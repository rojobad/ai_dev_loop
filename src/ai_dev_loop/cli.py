"""Typer CLI entry point for ai_dev_loop."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_loop import __version__
from ai_dev_loop.commands.config_cmd import run_validate_config
from ai_dev_loop.commands.controller import controller_status, render_controller_status
from ai_dev_loop.commands.doctor import render_doctor
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
from ai_dev_loop.commands.scheduler import (
    SubmitOptions,
    render_cutover_cleanup_output,
    render_list_output,
    render_scheduler_abort_output,
    render_scheduler_history_output,
    render_status_output,
    render_submit_output,
    render_tick_output,
    render_timer_disable_output,
    render_timer_install_output,
    render_timer_status_output,
    render_timer_validate_output,
    run_scheduler_tick,
    scheduler_abort_run,
    scheduler_history,
    scheduler_list,
    scheduler_status,
    scheduler_timeline,
    submit_run,
)
from ai_dev_loop.commands.scheduler import (
    render_start_output as render_scheduler_start_output,
)
from ai_dev_loop.commands.scheduler import (
    start_run as start_scheduler_run,
)
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import runs_dir
from ai_dev_loop.scheduler.application.cutover_cleanup import (
    CUTOVER_CONFIRMATION_TOKEN,
    cutover_target_paths,
    run_cutover_cleanup,
)
from ai_dev_loop.scheduler.application.timer_ops import (
    disable_scheduler_timer,
    install_scheduler_timer,
    scheduler_timer_status,
)

app = typer.Typer(
    name="ai_dev_loop",
    help="Deterministic local orchestrator for Codex/Cursor development loops.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Configuration commands.")
controller_app = typer.Typer(help="Controller-session status and control helpers.")
scheduler_app = typer.Typer(
    help=(
        "Central tick scheduler for local A/B runs. "
        "submit freezes a queued run; start authorizes it; tick reconciles one bounded pass."
    ),
)
cutover_app = typer.Typer(
    help="Explicit destructive cleanup of retired legacy XDG state roots.",
)
timer_app = typer.Typer(help="Packaged systemd timer asset helpers (no auto-enable).")
integrations_app = typer.Typer(help="Global Codex integration commands.")
sessions_app = typer.Typer(help="Desktop session rollout bridge commands.")
integrations_app.add_typer(sessions_app, name="sessions")
scheduler_app.add_typer(cutover_app, name="cutover")
scheduler_app.add_typer(timer_app, name="timer")
app.add_typer(config_app, name="config")
app.add_typer(controller_app, name="controller")
app.add_typer(scheduler_app, name="scheduler")
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


@scheduler_app.command("submit")
def scheduler_submit_command(
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
    controller_session_id: Annotated[
        str | None,
        typer.Option(
            "--controller-session-id",
            help=(
                "Optional controller Codex session ID provenance. Requires "
                "--codex-review-model and --codex-review-reasoning-effort."
            ),
        ),
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
        typer.Option(
            "--codex-review-model",
            help=("Frozen Codex review model (required). No YAML or session fallback."),
        ),
    ] = None,
    codex_review_reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--codex-review-reasoning-effort",
            help=("Frozen Codex review reasoning effort (required). No YAML or session fallback."),
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
    resubmission_id: Annotated[
        str | None,
        typer.Option(
            "--resubmission-id",
            help=(
                "Explicit UUID for an intentional fresh submission after a terminal run. "
                "Reuse the same value to replay that submission idempotently."
            ),
        ),
    ] = None,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Submit a frozen A/B scheduler run without launching agents or Git CLI probes."""

    def run() -> None:
        options = SubmitOptions(
            config_path=config_path,
            project_name=project_name,
            repo_path=repo_path,
            plan_path=plan_path,
            prompt_source_path=prompt_source_path,
            codex_session_id=None,
            controller_session_id=controller_session_id,
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
            resubmission_id=resubmission_id,
        )
        result = submit_run(options)
        typer.echo(render_submit_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("start")
def scheduler_start_command(
    run_id: Annotated[str, typer.Argument(help="Submitted scheduler run ID.")],
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Authorize a submitted scheduler run for tick reconciliation."""

    def run() -> None:
        result = start_scheduler_run(run_id)
        typer.echo(render_scheduler_start_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("tick")
def scheduler_tick_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Run one bounded scheduler tick without waiting for agents or sleeping."""

    def run() -> None:
        result = run_scheduler_tick()
        typer.echo(render_tick_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("status")
def scheduler_status_command(
    run_id: Annotated[str, typer.Argument(help="Scheduler run ID.")],
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Show a redacted scheduler run status projection."""

    def run() -> None:
        result = scheduler_status(run_id)
        typer.echo(render_status_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("list")
def scheduler_list_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """List submitted scheduler runs with redacted identity fields."""

    def run() -> None:
        summaries = scheduler_list()
        typer.echo(render_list_output(summaries, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("abort")
def scheduler_abort_command(
    run_id: Annotated[str, typer.Argument(help="Scheduler run ID.")],
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Persist scheduler abort first, then stop only the exact owned attempt unit."""

    def run() -> None:
        result = scheduler_abort_run(run_id)
        typer.echo(render_scheduler_abort_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("timeline")
def scheduler_timeline_command(
    run_id: Annotated[str, typer.Argument(help="Scheduler run ID.")],
    limit: Annotated[int, typer.Option(help="Maximum attempts to return.")] = 50,
    order: Annotated[
        str,
        typer.Option(help="Attempt order: oldest or newest."),
    ] = "oldest",
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Show bounded scheduler Cursor/Codex attempt timing for one run."""

    def run() -> None:
        from ai_dev_loop.commands.scheduler import render_scheduler_timeline_output

        result = scheduler_timeline(run_id, limit=limit, order=order)
        typer.echo(render_scheduler_timeline_output(result, output=output.value), nl=False)

    _handle(run)


@scheduler_app.command("history")
def scheduler_history_command(
    run_id: Annotated[str, typer.Argument(help="Scheduler run ID.")],
    limit: Annotated[int, typer.Option(help="Maximum events to return.")] = 50,
    order: Annotated[
        str,
        typer.Option(help="Event order: oldest or newest."),
    ] = "oldest",
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Show bounded redacted scheduler event history for one run."""

    def run() -> None:
        result = scheduler_history(run_id, limit=limit, order=order)
        typer.echo(render_scheduler_history_output(result, output=output.value), nl=False)

    _handle(run)


@cutover_app.command("cleanup")
def scheduler_cutover_cleanup_command(
    confirmation_token: Annotated[
        str,
        typer.Option(
            "--confirm",
            help=f"Required confirmation token: {CUTOVER_CONFIRMATION_TOKEN}",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate targets and print paths without deleting."),
    ] = False,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Delete only the retired legacy XDG roots runs/ and pr-review-v2/."""

    def run() -> None:
        resolved_root, targets = cutover_target_paths()
        announcement_to_stderr = output.value == OutputFormat.json
        typer.echo(
            "Legacy cutover cleanup will affect only these exact paths:",
            err=announcement_to_stderr,
        )
        for path in targets:
            typer.echo(f"  {path}", err=announcement_to_stderr)
        typer.echo(
            f"Preserved scheduler authority remains under: {resolved_root}",
            err=announcement_to_stderr,
        )
        typer.echo(
            f"Legacy runs helper path (may be absent): {runs_dir()}",
            err=announcement_to_stderr,
        )
        result = run_cutover_cleanup(confirmation_token=confirmation_token, dry_run=dry_run)
        typer.echo(render_cutover_cleanup_output(result, output=output.value), nl=False)

    _handle(run)


@timer_app.command("validate")
def scheduler_timer_validate_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Validate packaged scheduler timer/service templates without enabling systemd."""

    def run() -> None:
        from ai_dev_loop.scheduler.infrastructure.systemd_assets import validate_packaged_assets

        errors = validate_packaged_assets()
        typer.echo(render_timer_validate_output(errors, output=output.value), nl=False)
        if errors:
            raise typer.Exit(code=1)

    _handle(run)


@timer_app.command("install")
def scheduler_timer_install_command(
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Enable and start the timer after install."),
    ] = False,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Install packaged scheduler timer units under the user systemd directory."""

    def run() -> None:
        result = install_scheduler_timer(enable=enable)
        typer.echo(render_timer_install_output(result, output=output.value), nl=False)

    _handle(run)


@timer_app.command("status")
def scheduler_timer_status_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Report installed scheduler timer units and systemd state."""

    def run() -> None:
        result = scheduler_timer_status()
        typer.echo(render_timer_status_output(result, output=output.value), nl=False)

    _handle(run)


@timer_app.command("disable")
def scheduler_timer_disable_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Disable and stop the scheduler timer without deleting installed unit files."""

    def run() -> None:
        result = disable_scheduler_timer()
        typer.echo(render_timer_disable_output(result, output=output.value), nl=False)

    _handle(run)


@controller_app.command("status")
def controller_status_command(
    repo_path: Annotated[
        Path,
        typer.Option("--repo-path", help="Target repository root."),
    ],
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Exact scheduler run ID."),
    ] = None,
    controller_session_id: Annotated[
        str | None,
        typer.Option(
            "--controller-session-id",
            help="Legacy optional controller session ID for discovery.",
        ),
    ] = None,
    include_terminal: Annotated[
        bool,
        typer.Option(
            "--include-terminal",
            help="Include terminal runs when matching by controller session.",
        ),
    ] = False,
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Read-only status lookup for runs owned by a controller session."""

    def run() -> None:
        result = controller_status(
            controller_session_id=controller_session_id,
            repo_path=repo_path,
            run_id=run_id,
            include_terminal=include_terminal,
        )
        typer.echo(render_controller_status(result, output=output.value), nl=False)

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
