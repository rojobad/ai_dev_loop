from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest
from tests.integration.test_phase26_1_dashboard_queries import _fixture_ledger
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick,
    drive,
    prepare,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    scheduler_paths as _scheduler_paths,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    HttpServiceFactories,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.adapters.sqlite_read import SqliteDashboardReadRepository
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.server import DashboardServer
from ai_dev_loop.integration_api.dashboard_service import IntegrationDashboardReadService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

scheduler_paths = _scheduler_paths


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    request_headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    result = (
        response.status,
        {key.lower(): value for key, value in response.getheaders()},
        response.read(),
    )
    connection.close()
    return result


def _post(port: int, operation: str, parameters: dict[str, object]) -> dict[str, object]:
    body = json.dumps({"operation": operation, "parameters": parameters}).encode()
    status, _, payload = _request(
        port,
        "POST",
        "/api/dashboard/read",
        body=body,
        headers={
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
        },
    )
    decoded = json.loads(payload)
    assert status == 200, decoded
    assert decoded["ok"] is True
    return decoded


def _wait_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(f"server exited early: {stdout!r} {stderr!r}")
        try:
            status, _, _ = _request(port, "GET", "/")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("dashboard server did not become ready")


def _cli_command(port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from ai_dev_loop.cli import main; main()",
        "dashboard",
        "serve",
        "--port",
        str(port),
    ]


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_c262_1_real_cli_loopback_sigint_restart_and_no_storage_mutation(
    tmp_path: Path,
) -> None:
    xdg_state = tmp_path / "state"
    db_path = _fixture_ledger(xdg_state)
    state_root = xdg_state / "ai_dev_loop"
    artifact_root = state_root / "artifacts"
    before_db = db_path.read_bytes()
    before_artifacts = _tree_snapshot(artifact_root)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(xdg_state),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    port = _reserve_port()

    for _ in range(2):
        process = subprocess.Popen(
            _cli_command(port),
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_ready(port, process)
            payload = _post(port, "dashboard.projects", {})
            assert payload["data"]["totalCount"] == 1  # type: ignore[index]
            assert payload["data"]["items"][0]["runCount"] == 2  # type: ignore[index]
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, (stdout, stderr)
        assert f"http://127.0.0.1:{port}/" in stdout

    assert db_path.read_bytes() == before_db
    assert _tree_snapshot(artifact_root) == before_artifacts
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 2

    empty_state = tmp_path / "empty-state"
    invalid_env = dict(env, XDG_STATE_HOME=str(empty_state))
    invalid = subprocess.run(
        _cli_command(0),
        cwd=Path(__file__).resolve().parents[2],
        env=invalid_env,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )
    assert invalid.returncode == 4
    assert not empty_state.exists()
    invalid_type = subprocess.run(
        [*_cli_command(port)[:-1], "not-a-port"],
        cwd=Path(__file__).resolve().parents[2],
        env=invalid_env,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )
    assert invalid_type.returncode == 2
    assert not empty_state.exists()
    help_result = CliRunner().invoke(app, ["dashboard", "serve", "--help"])
    assert help_result.exit_code == 0 and "--host" not in help_result.stdout

    collision_port = _reserve_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", collision_port))
        occupied.listen()
        collision = subprocess.run(
            _cli_command(collision_port),
            cwd=Path(__file__).resolve().parents[2],
            env=invalid_env,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    assert collision.returncode == 1
    assert "127.0.0.1" in collision.stderr
    assert not empty_state.exists()


def test_c262_2_http_and_cli_share_dashboard_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "state"
    _fixture_ledger(xdg_state)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    runner = CliRunner()
    start = "2026-01-01T00:00:00Z"
    end = "2026-01-03T00:00:00Z"
    cli_result = runner.invoke(
        app,
        [
            "integration",
            "dashboard",
            "summary",
            "--from",
            start,
            "--to",
            end,
            "--time-zone",
            "UTC",
        ],
    )
    assert cli_result.exit_code == 0
    cli_payload = json.loads(cli_result.stdout)

    server = DashboardServer(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        http_payload = _post(
            server.port,
            "dashboard.summary",
            {"fromUtc": start, "toUtc": end, "timeZone": "UTC"},
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    cli_data = cli_payload["data"]
    http_data = http_payload["data"]
    cli_data.pop("observedAt")
    http_data.pop("observedAt")
    assert http_data == cli_data
    assert http_data["totals"]["runsStarted"] == 2


def test_c262_3_real_query_budget_expires_then_next_read_succeeds(tmp_path: Path) -> None:
    xdg_state = tmp_path / "state"
    db_path = _fixture_ledger(xdg_state)
    values = iter((0.0, 10.0))
    current = [10.0]

    def monotonic() -> float:
        with suppress(StopIteration):
            current[0] = next(values)
        return current[0]

    repository = SqliteDashboardReadRepository(
        db_path=db_path,
        query_budget_seconds=5,
        progress_steps=1,
        monotonic=monotonic,
    )
    defaults = default_http_service_factories(db_path=db_path)
    factories = HttpServiceFactories(
        dashboard=lambda: IntegrationDashboardReadService(DashboardQueries(repository)),
        run=defaults.run,
        sequence=defaults.sequence,
        review=defaults.review,
        process_output=defaults.process_output,
        capacity=defaults.capacity,
    )
    server = DashboardServer(0, dispatcher=DashboardHttpDispatcher(factories))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = {
        "operation": "dashboard.summary",
        "parameters": {
            "fromUtc": "2026-01-01T00:00:00Z",
            "toUtc": "2026-01-03T00:00:00Z",
            "timeZone": "UTC",
        },
    }
    try:
        body = json.dumps(request).encode()
        status, _, rejected = _request(
            server.port,
            "POST",
            "/api/dashboard/read",
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 400
        assert json.loads(rejected)["error"]["code"] == "INVALID_ARGUMENT"
        recovered = _post(server.port, request["operation"], request["parameters"])
        assert recovered["data"]["totals"]["runsStarted"] == 2  # type: ignore[index]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_c262_2_real_provider_owned_output_and_reviewer_content_boundaries(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    final = drive(tick, scheduler_paths, result.sequence_id, monkeypatch)
    assert len(final.materialized_entries) == 2

    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT a.run_id, a.attempt_id, a.component, e.effect_kind
            FROM scheduler_attempts AS a
            JOIN scheduler_effects AS e ON e.dispatch_id = a.dispatch_id
            WHERE a.component IN ('cursor', 'codex_implementer', 'codex')
              AND e.effect_kind != 'cursor.create_chat'
            ORDER BY a.created_at, a.attempt_id
            """
        ).fetchall()
    by_component = {str(row["component"]): row for row in rows}
    assert {"cursor", "codex_implementer", "codex"} <= set(by_component)

    dispatcher = DashboardHttpDispatcher(
        default_http_service_factories(
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    server = DashboardServer(0, dispatcher=dispatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for component in ("cursor", "codex_implementer"):
            row = by_component[component]
            output = _post(
                server.port,
                "run.output",
                {
                    "runId": str(row["run_id"]),
                    "attemptId": str(row["attempt_id"]),
                    "stream": "stdout",
                    "limit": 64,
                },
            )["data"]
            assert output["available"] is True  # type: ignore[index]
            assert output["component"] == component  # type: ignore[index]

        reviewer = by_component["codex"]
        review = _post(
            server.port,
            "run.review",
            {
                "runId": str(reviewer["run_id"]),
                "attemptId": str(reviewer["attempt_id"]),
            },
        )["data"]
        assert review["attemptId"] == str(reviewer["attempt_id"])  # type: ignore[index]
        content = _post(
            server.port,
            "run.review-content",
            {
                "runId": str(reviewer["run_id"]),
                "attemptId": str(reviewer["attempt_id"]),
                "kind": "prompt",
                "limit": 64,
            },
        )["data"]
        assert content["available"] is True  # type: ignore[index]

        implementer = by_component["codex_implementer"]
        body = json.dumps(
            {
                "operation": "run.review-content",
                "parameters": {
                    "runId": str(implementer["run_id"]),
                    "attemptId": str(implementer["attempt_id"]),
                    "kind": "prompt",
                },
            }
        ).encode()
        status, _, rejected = _request(
            server.port,
            "POST",
            "/api/dashboard/read",
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 400
        assert json.loads(rejected)["error"]["code"] == "INVALID_ARGUMENT"

        wrong = json.dumps(
            {
                "operation": "run.output",
                "parameters": {
                    "runId": "wrong-run",
                    "attemptId": str(implementer["attempt_id"]),
                    "stream": "stdout",
                },
            }
        ).encode()
        status, _, rejected = _request(
            server.port,
            "POST",
            "/api/dashboard/read",
            body=wrong,
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 404
        assert json.loads(rejected)["error"]["code"] == "NOT_FOUND"

        wrong_attempt = json.dumps(
            {
                "operation": "run.output",
                "parameters": {
                    "runId": str(implementer["run_id"]),
                    "attemptId": "missing-attempt",
                    "stream": "stdout",
                },
            }
        ).encode()
        status, _, rejected = _request(
            server.port,
            "POST",
            "/api/dashboard/read",
            body=wrong_attempt,
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 404
        assert json.loads(rejected)["error"]["code"] == "NOT_FOUND"

        inspect = _post(
            server.port,
            "run.inspect",
            {"runId": str(implementer["run_id"])},
        )["data"]
        assert inspect["implementerProvider"] == "codex"  # type: ignore[index]
        assert inspect["tokenUsage"]["normalizationVersion"] == "provider-usage-v1"  # type: ignore[index]
        timeline = _post(
            server.port,
            "run.timeline",
            {"runId": str(implementer["run_id"]), "limit": 100},
        )["data"]
        assert any(  # type: ignore[union-attr]
            item["component"] == "codex_implementer" for item in timeline["items"]
        )
        assert any(  # type: ignore[union-attr]
            item["component"] == "codex" for item in timeline["items"]
        )
        sequence = _post(
            server.port,
            "sequence.inspect",
            {"sequenceId": result.sequence_id},
        )["data"]
        assert sequence["tokenUsage"]["normalizationVersion"] == "provider-usage-v1"  # type: ignore[index]
        assert [phase["implementerProvider"] for phase in sequence["phases"]] == [  # type: ignore[index]
            "codex",
            "cursor",
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)
