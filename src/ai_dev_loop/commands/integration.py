"""JSON-only local Integration API CLI adapter."""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer
import typer.main
from typer import _click as click
from typer.core import TyperCommand, TyperGroup

from ai_dev_loop.integration_api.envelope import emit_failure, emit_success
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.run_projection import RunKindFilter
from ai_dev_loop.integration_api.run_service import default_run_read_service
from ai_dev_loop.integration_api.sequence_service import default_sequence_read_service
from ai_dev_loop.integration_api.validation import (
    COLLECTION_DEFAULT_LIMIT,
    validate_artifact_read_bounds,
    validate_collection_bounds,
)

IntegrationOutputOption = Annotated[
    str,
    typer.Option("--output", help="Output format (json only)."),
]


def _require_json_output(output: str) -> None:
    if output != "json":
        raise IntegrationApiError.invalid_argument(
            "Unsupported --output value; only json is supported.",
        )


def _map_click_exception(exc: click.ClickException) -> IntegrationApiError:
    message = str(exc)
    lowered = message.lower()
    if "unknown integration command" in lowered:
        return IntegrationApiError.invalid_argument("Unknown integration command.")
    if "missing integration command" in lowered:
        return IntegrationApiError.invalid_argument("Missing integration command.")
    if "requires an argument" in lowered and "--output" in lowered:
        return IntegrationApiError.invalid_argument("Missing value for --output.")
    if "missing" in lowered and "argument" in lowered:
        return IntegrationApiError.invalid_argument("Missing required argument.")
    if "no such option" in lowered or "unknown option" in lowered:
        return IntegrationApiError.invalid_argument("Unknown integration option.")
    return IntegrationApiError.invalid_argument("Invalid integration command arguments.")


def _map_parse_exception(exc: BaseException) -> IntegrationApiError:
    if isinstance(exc, click.exceptions.NoSuchOption):
        return IntegrationApiError.invalid_argument("Unknown integration option.")
    if isinstance(exc, click.exceptions.MissingParameter):
        if exc.param is not None and "output" in {*(exc.param.opts or ())}:
            return IntegrationApiError.invalid_argument("Missing value for --output.")
        return IntegrationApiError.invalid_argument("Missing required argument.")
    if isinstance(exc, click.exceptions.BadParameter):
        return IntegrationApiError.invalid_argument("Invalid integration command arguments.")
    if isinstance(exc, click.exceptions.UsageError):
        return _map_click_exception(exc)
    return IntegrationApiError.invalid_argument("Invalid integration command arguments.")


def _finalize_integration_cli_outcome(exc: BaseException) -> int:
    if isinstance(exc, KeyboardInterrupt):
        raise exc
    if isinstance(exc, click.exceptions.Exit):
        if exc.exit_code == 130:
            raise KeyboardInterrupt() from None
        return int(exc.exit_code) if exc.exit_code is not None else 0
    if isinstance(exc, IntegrationApiError):
        emit_failure(exc)
        return exc.exit_code
    if isinstance(exc, click.ClickException):
        mapped = _map_click_exception(exc)
        emit_failure(mapped)
        return mapped.exit_code
    if isinstance(exc, OSError):
        failure = IntegrationApiError(
            IntegrationErrorCode.IO_ERROR,
            "Unable to complete the integration request.",
        )
        emit_failure(failure)
        return failure.exit_code
    failure = IntegrationApiError.internal()
    emit_failure(failure)
    return failure.exit_code


class IntegrationParseMixin:
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)  # type: ignore[misc,no-any-return]
        except (
            click.exceptions.NoSuchOption,
            click.exceptions.MissingParameter,
            click.exceptions.BadParameter,
            click.exceptions.UsageError,
        ) as exc:
            mapped = _map_parse_exception(exc)
            emit_failure(mapped)
            raise click.exceptions.Exit(mapped.exit_code) from None


class IntegrationTyperCommand(IntegrationParseMixin, TyperCommand):
    pass


class IntegrationTyperGroup(IntegrationParseMixin, TyperGroup):
    command_class = IntegrationTyperCommand

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is None and cmd_name is not None:
            raise IntegrationApiError.invalid_argument("Unknown integration command.")
        return command

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.exceptions.Exit:
            raise
        except BaseException as exc:
            code = _finalize_integration_cli_outcome(exc)
            raise click.exceptions.Exit(code) from None


integration_app = typer.Typer(
    help="Local Integration API (JSON-only).",
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    cls=IntegrationTyperGroup,
)

runs_app = typer.Typer(
    help="Run collection queries.",
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    cls=IntegrationTyperGroup,
)

run_app = typer.Typer(
    help="Single-run inspection.",
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    cls=IntegrationTyperGroup,
)

sequences_app = typer.Typer(
    help="Sequence collection queries.",
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    cls=IntegrationTyperGroup,
)

sequence_app = typer.Typer(
    help="Single-sequence inspection.",
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    cls=IntegrationTyperGroup,
)

integration_app.add_typer(runs_app, name="runs")
integration_app.add_typer(run_app, name="run")
integration_app.add_typer(sequences_app, name="sequences")
integration_app.add_typer(sequence_app, name="sequence")

OrdinalOption = Annotated[
    int,
    typer.Option("--ordinal", help="Sequence phase ordinal (1-based)."),
]

KindOption = Annotated[
    RunKindFilter,
    typer.Option("--kind", help="Filter runs: all, standalone, or sequence."),
]

OffsetOption = Annotated[
    int,
    typer.Option("--offset", help="Collection offset (default 0)."),
]

LimitOption = Annotated[
    int,
    typer.Option("--limit", help="Collection page size (default 100, max 500)."),
]

ByteOffsetOption = Annotated[
    int,
    typer.Option("--offset", help="Artifact byte offset (default 0)."),
]

ByteLimitOption = Annotated[
    int,
    typer.Option("--limit", help="Artifact byte limit (default 65536, max 262144)."),
]


@integration_app.callback(invoke_without_command=True)
def integration_root(ctx: typer.Context) -> None:
    """Machine-readable local integration boundary for future Bridge clients."""
    if ctx.invoked_subcommand is None:
        raise IntegrationApiError.invalid_argument("Missing integration command.")


@integration_app.command("info")
def integration_info_command(
    output: IntegrationOutputOption = "json",
) -> None:
    """Return API contract version, package version, and honest capabilities."""
    _require_json_output(output)
    data = build_integration_info_data()
    emit_success(data.model_dump(by_alias=True))


@runs_app.command("list")
def integration_runs_list_command(
    output: IntegrationOutputOption = "json",
    kind: KindOption = "all",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List scheduler runs with optional kind filter and pagination."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_run_read_service()
    data = service.list_runs(kind=kind, offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@run_app.command("inspect")
def integration_run_inspect_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
) -> None:
    """Inspect one run summary without loading artifact bodies."""
    _require_json_output(output)
    service = default_run_read_service()
    data = service.inspect_run(run_id)
    emit_success(data.model_dump(by_alias=True))


@run_app.command("attempts")
def integration_run_attempts_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List bounded attempt records for one run."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_run_read_service()
    data = service.list_attempts(run_id, offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@run_app.command("timeline")
def integration_run_timeline_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List bounded attempt timeline rows for one run."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_run_read_service()
    data = service.list_timeline(run_id, offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@run_app.command("history")
def integration_run_history_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List bounded redacted scheduler event history for one run."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_run_read_service()
    data = service.list_history(run_id, offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@run_app.command("plan")
def integration_run_plan_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
    byte_offset: ByteOffsetOption = 0,
    byte_limit: ByteLimitOption = 65536,
) -> None:
    """Read a verified chunk of the frozen plan artifact."""
    _require_json_output(output)
    validate_artifact_read_bounds(byte_offset, byte_limit)
    service = default_run_read_service()
    data = service.read_plan_chunk(run_id, byte_offset=byte_offset, limit=byte_limit)
    emit_success(data.model_dump(by_alias=True))


@sequences_app.command("list")
def integration_sequences_list_command(
    output: IntegrationOutputOption = "json",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List prepared and materialized sequences."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_sequence_read_service()
    data = service.list_sequences(offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@sequence_app.command("inspect")
def integration_sequence_inspect_command(
    sequence_id: str,
    output: IntegrationOutputOption = "json",
) -> None:
    """Inspect one sequence summary including phase lineage overview."""
    _require_json_output(output)
    service = default_sequence_read_service()
    data = service.inspect_sequence(sequence_id)
    emit_success(data.model_dump(by_alias=True))


@sequence_app.command("phase-runs")
def integration_sequence_phase_runs_command(
    sequence_id: str,
    ordinal: OrdinalOption,
    output: IntegrationOutputOption = "json",
    offset: OffsetOption = 0,
    limit: LimitOption = COLLECTION_DEFAULT_LIMIT,
) -> None:
    """List bounded run-attempt history for one sequence phase."""
    _require_json_output(output)
    validate_collection_bounds(offset, limit)
    service = default_sequence_read_service()
    data = service.list_phase_runs(sequence_id, ordinal=ordinal, offset=offset, limit=limit)
    emit_success(data.model_dump(by_alias=True))


@sequence_app.command("phase-plan")
def integration_sequence_phase_plan_command(
    sequence_id: str,
    ordinal: OrdinalOption,
    output: IntegrationOutputOption = "json",
    byte_offset: ByteOffsetOption = 0,
    byte_limit: ByteLimitOption = 65536,
) -> None:
    """Read a verified chunk of the frozen phase plan from the sequence definition."""
    _require_json_output(output)
    validate_artifact_read_bounds(byte_offset, byte_limit)
    service = default_sequence_read_service()
    data = service.read_phase_plan_chunk(
        sequence_id,
        ordinal=ordinal,
        byte_offset=byte_offset,
        limit=byte_limit,
    )
    emit_success(data.model_dump(by_alias=True))


@sequence_app.command("phase-prompt")
def integration_sequence_phase_prompt_command(
    sequence_id: str,
    ordinal: OrdinalOption,
    output: IntegrationOutputOption = "json",
    byte_offset: ByteOffsetOption = 0,
    byte_limit: ByteLimitOption = 65536,
) -> None:
    """Read a verified chunk of the frozen phase prompt from the sequence definition."""
    _require_json_output(output)
    validate_artifact_read_bounds(byte_offset, byte_limit)
    service = default_sequence_read_service()
    data = service.read_phase_prompt_chunk(
        sequence_id,
        ordinal=ordinal,
        byte_offset=byte_offset,
        limit=byte_limit,
    )
    emit_success(data.model_dump(by_alias=True))


@sequence_app.command("report")
def integration_sequence_report_command(
    sequence_id: str,
    output: IntegrationOutputOption = "json",
    byte_offset: ByteOffsetOption = 0,
    byte_limit: ByteLimitOption = 65536,
) -> None:
    """Read the published sequence completion report when available."""
    _require_json_output(output)
    validate_artifact_read_bounds(byte_offset, byte_limit)
    service = default_sequence_read_service()
    data = service.read_report_chunk(
        sequence_id,
        byte_offset=byte_offset,
        limit=byte_limit,
    )
    emit_success(data.model_dump(by_alias=True))


@run_app.command("initial-prompt")
def integration_run_initial_prompt_command(
    run_id: str,
    output: IntegrationOutputOption = "json",
    byte_offset: ByteOffsetOption = 0,
    byte_limit: ByteLimitOption = 65536,
) -> None:
    """Read a verified chunk of the frozen initial Cursor prompt artifact."""
    _require_json_output(output)
    validate_artifact_read_bounds(byte_offset, byte_limit)
    service = default_run_read_service()
    data = service.read_initial_prompt_chunk(run_id, byte_offset=byte_offset, limit=byte_limit)
    emit_success(data.model_dump(by_alias=True))


def run_integration_cli(argv: list[str] | None = None) -> int:
    """Run the integration Typer app with envelope error handling."""
    args = list(argv if argv is not None else sys.argv[2:])
    if args == ["--help"] or args == ["-h"]:
        integration_app(args=["--help"], prog_name="ai_dev_loop integration", standalone_mode=True)
        return 0
    command = typer.main.get_command(integration_app)
    try:
        command.main(
            args=args,
            prog_name="ai_dev_loop integration",
            standalone_mode=True,
        )
    except SystemExit as exc:
        code = exc.code
        if code == 130:
            raise KeyboardInterrupt() from None
        if isinstance(code, int):
            return code
        return 0 if code is None else 1
    except click.exceptions.Exit as exc:
        if exc.exit_code == 130:
            raise KeyboardInterrupt() from None
        return int(exc.exit_code) if exc.exit_code is not None else 0
    except IntegrationApiError as exc:
        emit_failure(exc)
        return exc.exit_code
    except click.ClickException as exc:
        mapped = _map_click_exception(exc)
        emit_failure(mapped)
        return mapped.exit_code
    except KeyboardInterrupt:
        raise
    except OSError:
        failure = IntegrationApiError(
            IntegrationErrorCode.IO_ERROR,
            "Unable to complete the integration request.",
        )
        emit_failure(failure)
        return failure.exit_code
    except Exception:
        failure = IntegrationApiError.internal()
        emit_failure(failure)
        return failure.exit_code
    return 0
