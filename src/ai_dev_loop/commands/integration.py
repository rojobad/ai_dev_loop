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
