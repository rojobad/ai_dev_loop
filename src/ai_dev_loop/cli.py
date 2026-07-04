"""Typer CLI entry point for ai_dev_loop."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_loop import __version__
from ai_dev_loop.commands.config_cmd import run_validate_config
from ai_dev_loop.commands.doctor import render_doctor
from ai_dev_loop.commands.inspect import render_inspect
from ai_dev_loop.commands.integrations import raise_not_implemented, render_integrations_status
from ai_dev_loop.commands.list_runs import render_list
from ai_dev_loop.commands.logs import render_logs
from ai_dev_loop.commands.placeholders import not_implemented
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run, render_prepare_output
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
app.add_typer(config_app, name="config")
app.add_typer(integrations_app, name="integrations")


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


OutputOption = Annotated[OutputFormat, typer.Option("--output", help="Output format.")]
DEFAULT_OUTPUT = OutputFormat.text


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
) -> None:
    """Start the automated Cursor/Codex loop for a prepared run."""

    def run() -> None:
        not_implemented("start")

    _handle(run)


@app.command("resume")
def resume_command(
    run_id: Annotated[str, typer.Argument(help="Run identifier to resume.")],
) -> None:
    """Resume an interrupted run."""

    def run() -> None:
        not_implemented("resume")

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
        not_implemented("abort")

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
def integrations_install_command() -> None:
    """Install global Codex skill and SessionStart hook."""

    def run() -> None:
        raise_not_implemented("install")

    _handle(run)


@integrations_app.command("uninstall")
def integrations_uninstall_command() -> None:
    """Remove ai_dev_loop global integration assets."""

    def run() -> None:
        raise_not_implemented("uninstall")

    _handle(run)


@integrations_app.command("status")
def integrations_status_command(
    output: OutputOption = DEFAULT_OUTPUT,
) -> None:
    """Report global integration installation status."""

    def run() -> None:
        typer.echo(render_integrations_status(output=output.value), nl=False)

    _handle(run)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
