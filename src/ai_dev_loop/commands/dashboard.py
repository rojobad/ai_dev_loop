"""Local loopback dashboard server CLI."""

from __future__ import annotations

import errno
from typing import Annotated

import typer

from ai_dev_loop.dashboard.server import DASHBOARD_DEFAULT_PORT, DashboardServer
from ai_dev_loop.errors import AiDevLoopError, ValidationError

dashboard_app = typer.Typer(
    help="Serve the read-only local dashboard on IPv4 loopback.",
    no_args_is_help=True,
    add_completion=False,
)


def validate_dashboard_port(port: object) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValidationError("Dashboard port must be an integer from 1 through 65535.")
    return port


def run_dashboard_server(port: object) -> None:
    validated_port = validate_dashboard_port(port)
    try:
        server = DashboardServer(validated_port)
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
            raise AiDevLoopError(
                f"Unable to bind the dashboard to 127.0.0.1:{validated_port}."
            ) from exc
        raise AiDevLoopError("Unable to start the loopback dashboard server.") from exc
    with server:
        typer.echo(f"Dashboard available at http://127.0.0.1:{server.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            typer.echo("Dashboard server stopped.")


@dashboard_app.command("serve")
def dashboard_serve_command(
    port: Annotated[
        int,
        typer.Option("--port", help="IPv4 loopback port (1-65535)."),
    ] = DASHBOARD_DEFAULT_PORT,
) -> None:
    """Serve package assets and the same-origin typed read API until interrupted."""

    try:
        run_dashboard_server(port)
    except AiDevLoopError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.exit_code) from exc
