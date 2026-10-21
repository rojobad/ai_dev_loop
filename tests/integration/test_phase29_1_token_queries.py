"""Phase 29.1 cohort token queries across CLI, HTTP, and authenticated evidence."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_phase25_3_sdk_recovery import _submit_sdk_run, _tick_until_kind
from tests.integration.test_phase26_1_dashboard_queries import (
    HISTORICAL,
    _insert_attempt,
    _insert_run,
)
from tests.unit.scheduler.test_phase27_2_codex_implementer import _run_until, _submit, _tick
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.adapters.sqlite_read import SqliteDashboardReadRepository
from ai_dev_loop.dashboard.adapters.token_usage_read import (
    SqliteDirectUsageReader,
    token_usage_queries,
)
from ai_dev_loop.dashboard.application import token_usage as token_usage_module
from ai_dev_loop.dashboard.application.ports import DashboardObservationSet, DashboardReadScope
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.domain.token_analytics import TokenAggregate
from ai_dev_loop.dashboard.server import DashboardServer
from ai_dev_loop.integration_api.dashboard_service import (
    IntegrationDashboardReadService,
    default_dashboard_read_service,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.schemas import (
    DASHBOARD_TOKEN_USAGE_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.cursor_evidence import invocation_evidence_sha256
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()
FROM = "2026-01-02T00:00:00Z"
TO = "2026-01-03T00:00:00Z"
IN = datetime(2026, 1, 2, 6, tzinfo=UTC)
BEFORE = datetime(2026, 1, 1, 6, tzinfo=UTC)
AFTER = datetime(2026, 1, 4, 6, tzinfo=UTC)
LATER = datetime(2026, 2, 1, 6, tzinfo=UTC)
SCOPE_MESSAGE = "Dashboard scope is too large; narrow the project or time range."
NOT_FOUND_MESSAGE = "The selected run is outside this dashboard cohort."


def _db(isolated_xdg: Path) -> Path:
    return isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3"


def _artifacts(isolated_xdg: Path) -> Path:
    return isolated_xdg / "state" / "ai_dev_loop" / "artifacts"


def _service(isolated_xdg: Path, **kwargs: object) -> IntegrationDashboardReadService:
    db = _db(isolated_xdg)
    artifacts = _artifacts(isolated_xdg)
    return IntegrationDashboardReadService(
        DashboardQueries(SqliteDashboardReadRepository(db_path=db)),
        token_queries=token_usage_queries(db_path=db, artifact_root=artifacts, **kwargs),  # type: ignore[arg-type]
    )


def _query(service: IntegrationDashboardReadService, **kwargs: object) -> object:
    params: dict[str, object] = {"from_value": FROM, "to_value": TO, "time_zone": "UTC"}
    params.update(kwargs)
    return service.token_usage(**params)  # type: ignore[arg-type]


def _flow_total(payload: dict[str, object], name: str) -> int | None:
    flows = payload["flows"]
    assert isinstance(flows, list)
    found = next(item for item in flows if item["name"] == name)
    return found["knownTotal"]  # type: ignore[no-any-return]


def _row(payload: dict[str, object], **expected: object) -> dict[str, object]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    for item in rows:
        if all(item[key] == value for key, value in expected.items()):
            return item
    raise AssertionError(expected)


def _seed(isolated_xdg: Path) -> SqliteSchedulerStore:
    store = SqliteSchedulerStore(_db(isolated_xdg))
    source = _insert_run(store, "source-run", submitted_at=BEFORE)
    successor = _insert_run(store, "successor-run", submitted_at=AFTER)
    outside = _insert_run(store, "outside-run", submitted_at=IN)
    other = _insert_run(store, "other-run", project_name="project-b", submitted_at=BEFORE)
    for suffix, kind, iteration, launch in (
        ("chat", "cursor.create_chat", 1, IN),
        ("preflight", "cursor.preflight_and_probes", 1, IN),
        ("turn-1", "cursor.run_turn", 1, IN),
        ("turn-2", "cursor.run_turn", 2, LATER),
    ):
        _insert_attempt(
            store,
            run_id="source-run",
            event_id=source,
            suffix=suffix,
            component="cursor",
            effect_kind=kind,
            iteration=iteration,
            launch=launch,
            completed=launch,
        )
    _insert_attempt(
        store,
        run_id="successor-run",
        event_id=successor,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=IN,
        completed=IN,
    )
    _insert_attempt(
        store,
        run_id="outside-run",
        event_id=outside,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=AFTER,
        completed=AFTER,
    )
    _insert_attempt(
        store,
        run_id="other-run",
        event_id=other,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=IN,
        completed=IN,
    )
    return store


def test_t291_01_cohort_is_started_runs_and_direct_does_not_expand_recovery(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _seed(isolated_xdg)
    with store.begin_immediate() as conn:
        store.insert_review_recovery_successor(
            conn,
            source_run_id="source-run",
            recovery_key="review-recovery",
            successor_run_id="successor-run",
            now=IN,
        )
        store.insert_run_short_name(conn, run_id="source-run", short_name="Alpha Source", now=IN)
    service = _service(isolated_xdg)
    cohort = _query(service, project_name="project-a")
    summary = service.summary(
        from_value=FROM,
        to_value=TO,
        project_name="project-a",
        time_zone="UTC",
    )
    page = service.runs(
        from_value=FROM,
        to_value=TO,
        project_name="project-a",
        time_zone="UTC",
        limit=1,
    )
    assert cohort.cohort_run_count == summary.totals.runs_started == 2
    assert cohort.selected_run_count == 2
    assert cohort.scope == "cohort"
    assert cohort.selected_run is None
    assert len(page.items) == 1 and page.page.has_more is True
    assert cohort.source.inference_attempts == 3
    assert cohort.source.known_total_tokens is None
    assert cohort.source.coverage == "unknown"
    direct = _query(service, project_name="project-a", run_id="source-run")
    assert direct.scope == "direct"
    assert direct.selected_run is not None
    assert direct.selected_run.short_name == "Alpha Source"
    assert direct.selected_run_count == 1
    assert direct.cohort_run_count == 2
    assert direct.source.inference_attempts == 2
    successor = _query(service, project_name="project-a", run_id="successor-run")
    assert successor.source.inference_attempts == 1
    everyone = _query(service)
    assert everyone.cohort_run_count == 3
    assert everyone.source.inference_attempts == 4
    text = json.dumps(cohort.model_dump(mode="json", by_alias=True))
    assert "source-run" not in text
    assert "Alpha Source" not in text
    assert "successor-run" not in text

    def fail_collect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("usage collected for a run outside the cohort")

    monkeypatch.setattr(SqliteDirectUsageReader, "collect", fail_collect)
    for missing in ("outside-run", "missing-run"):
        with pytest.raises(IntegrationApiError) as failure:
            _query(service, project_name="project-a", run_id=missing)
        assert failure.value.code == "NOT_FOUND"
        assert failure.value.message == NOT_FOUND_MESSAGE
        assert missing not in failure.value.message


def test_t291_04_05_cli_http_bounds_cancellation_and_historical_reads(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _seed(isolated_xdg)
    with store.begin_immediate() as conn:
        store.insert_run_short_name(conn, run_id="source-run", short_name="Alpha Source", now=IN)
    db = _db(isolated_xdg)
    before = db.read_bytes()
    cli = runner.invoke(
        app,
        [
            "integration",
            "dashboard",
            "token-usage",
            "--from",
            FROM,
            "--to",
            TO,
            "--time-zone",
            "UTC",
            "--project",
            "project-a",
            "--run-id",
            "source-run",
        ],
    )
    assert cli.exit_code == 0, cli.stdout
    cli_payload = json.loads(cli.stdout)
    validate_integration_instance(cli_payload["data"], DASHBOARD_TOKEN_USAGE_DATA_SCHEMA)
    http = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=_artifacts(isolated_xdg))
    ).dispatch(
        "dashboard.token-usage",
        {
            "fromUtc": FROM,
            "toUtc": TO,
            "timeZone": "UTC",
            "projectName": "project-a",
            "runId": "source-run",
        },
    )
    assert {key: value for key, value in cli_payload["data"].items() if key != "observedAt"} == {
        key: value for key, value in http.items() if key != "observedAt"
    }
    assert http["scope"] == "direct"
    assert http["selectedRun"]["shortName"] == "Alpha Source"
    assert http["source"]["inferenceAttempts"] == 2
    assert db.read_bytes() == before
    help_result = runner.invoke(app, ["integration", "dashboard", "--help"])
    assert help_result.exit_code == 0 and "token-usage" in help_result.stdout

    def opened(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("storage opened for a rejected argument")

    with monkeypatch.context() as blocked:
        blocked.setattr(SqliteDashboardReadRepository, "read_observations", opened)
        service = default_dashboard_read_service(db_path=db, artifact_root=_artifacts(isolated_xdg))
        for bad in ("", " run", "../run", "a/b", "a\\b", "a\nb", 4):
            with pytest.raises(IntegrationApiError) as rejected:
                service.token_usage(from_value=FROM, to_value=TO, time_zone="UTC", run_id=bad)
            assert rejected.value.code == "INVALID_ARGUMENT"
        with pytest.raises(IntegrationApiError) as too_wide:
            service.token_usage(
                from_value="2026-01-01T00:00:00Z",
                to_value="2027-02-01T00:00:00Z",
                time_zone="UTC",
            )
        assert too_wide.value.code == "INVALID_ARGUMENT"

    bounded = IntegrationDashboardReadService(
        DashboardQueries(SqliteDashboardReadRepository(db_path=db)),
        token_queries=token_usage_queries(
            db_path=db,
            artifact_root=_artifacts(isolated_xdg),
            max_runs=1,
        ),
    )

    def collect_should_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("run bound still collected usage")

    with monkeypatch.context() as blocked:
        blocked.setattr(SqliteDirectUsageReader, "collect", collect_should_not_run)
        with pytest.raises(IntegrationApiError) as run_bound:
            bounded.token_usage(
                from_value=FROM,
                to_value=TO,
                project_name="project-a",
                time_zone="UTC",
            )
        assert run_bound.value.code == "INVALID_ARGUMENT"
        assert run_bound.value.message == SCOPE_MESSAGE

    calls = {"auth": 0}

    def authenticate(*_args: object, **_kwargs: object) -> None:
        calls["auth"] += 1
        raise AssertionError("attempt bound authenticated an artifact")

    with monkeypatch.context() as blocked:
        blocked.setattr(
            "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
            authenticate,
        )
        attempt_bound = IntegrationDashboardReadService(
            DashboardQueries(SqliteDashboardReadRepository(db_path=db)),
            token_queries=token_usage_queries(
                db_path=db,
                artifact_root=_artifacts(isolated_xdg),
                max_attempts=1,
            ),
        )
        with pytest.raises(IntegrationApiError) as attempt_error:
            attempt_bound.token_usage(
                from_value=FROM,
                to_value=TO,
                project_name="project-a",
                time_zone="UTC",
                run_id="source-run",
            )
    assert attempt_error.value.message == SCOPE_MESSAGE
    assert calls["auth"] == 0

    tiny = IntegrationDashboardReadService(
        DashboardQueries(SqliteDashboardReadRepository(db_path=db)),
        token_queries=token_usage_queries(db_path=db, artifact_root=_artifacts(isolated_xdg)),
        envelope_max_bytes=32,
    )
    with pytest.raises(IntegrationApiError) as envelope:
        tiny.token_usage(from_value=FROM, to_value=TO, time_zone="UTC", project_name="project-a")
    assert (
        envelope.value.message == "Dashboard result is too large; narrow the project or time range."
    )

    missing = tmp_path / "absent" / "engine.sqlite3"
    empty = default_dashboard_read_service(db_path=missing, artifact_root=tmp_path / "no-artifacts")
    empty_result = empty.token_usage(from_value=FROM, to_value=TO, time_zone="UTC")
    assert empty_result.source.coverage == "not_applicable"
    assert empty_result.source.known_total_tokens is None
    assert empty_result.selected_run_count == 0
    assert not missing.exists()

    historical = tmp_path / "historical.sqlite3"
    shutil.copyfile(HISTORICAL, historical)
    historical_before = historical.read_bytes()
    with sqlite3.connect(historical) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 11
    historical_service = default_dashboard_read_service(
        db_path=historical,
        artifact_root=tmp_path / "historical-artifacts",
    )
    historical_service.token_usage(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-12-31T00:00:00Z",
        time_zone="UTC",
    )
    assert historical.read_bytes() == historical_before
    with sqlite3.connect(historical) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11

    clock = {"n": 0, "auth": 0}

    def cancelled() -> bool:
        clock["n"] += 1
        return clock["n"] >= 4

    def auth_after_cancel(*_args: object, **_kwargs: object) -> None:
        clock["auth"] += 1
        raise AssertionError("cancelled evidence loop continued")

    ProtectedArtifactStore(_artifacts(isolated_xdg)).run_root("source-run")
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
        auth_after_cancel,
    )
    cancelling = IntegrationDashboardReadService(
        DashboardQueries(SqliteDashboardReadRepository(db_path=db)),
        token_queries=token_usage_queries(
            db_path=db,
            artifact_root=_artifacts(isolated_xdg),
            monotonic=lambda: 0.0,
            cancelled=cancelled,
        ),
    )
    with pytest.raises(IntegrationApiError) as cancelled_error:
        cancelling.token_usage(
            from_value=FROM,
            to_value=TO,
            project_name="project-a",
            time_zone="UTC",
            run_id="source-run",
        )
    assert cancelled_error.value.message == SCOPE_MESSAGE
    assert clock["auth"] == 0
    assert clock["n"] >= 4, clock


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _post_token_usage(port: int) -> tuple[int, dict[str, object]]:
    body = json.dumps(
        {
            "operation": "dashboard.token-usage",
            "parameters": {"fromUtc": FROM, "toUtc": TO, "timeZone": "UTC"},
        }
    ).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    connection.request(
        "POST",
        "/api/dashboard/read",
        body=body,
        headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    assert isinstance(payload, dict)
    return status, payload


def test_f01_public_deadline_checkpoints_return_narrow_scope_errors(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not _db(isolated_xdg).exists()
    script: list[float] = []
    observed = {"read": 0, "aggregate": 0}

    def monotonic() -> float:
        if not script:
            raise AssertionError("token query clock exhausted")
        return script.pop(0)

    original_read = SqliteDashboardReadRepository.read_observations

    def read_observations(
        self: SqliteDashboardReadRepository,
        scope: DashboardReadScope,
    ) -> DashboardObservationSet:
        observed["read"] += 1
        return original_read(self, scope)

    real_aggregate = token_usage_module.aggregate_token_usage

    def aggregate(*args: Any, **kwargs: Any) -> TokenAggregate:
        observed["aggregate"] += 1
        return real_aggregate(*args, **kwargs)

    monkeypatch.setattr(token_usage_module, "_default_monotonic", monotonic)
    monkeypatch.setattr(token_usage_module, "aggregate_token_usage", aggregate)
    monkeypatch.setattr(SqliteDashboardReadRepository, "read_observations", read_observations)

    cases = (
        ("before-cohort", (0.0, 10.0), 0, 0),
        ("after-cohort", (0.0, 0.0, 10.0), 1, 0),
        ("after-aggregation", (0.0, 0.0, 0.0, 10.0), 1, 1),
    )
    port = _reserve_port()
    server = DashboardServer(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ready = time.monotonic() + 5
    while time.monotonic() < ready:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.02)
    else:
        server.shutdown()
        raise AssertionError("dashboard server did not accept connections")
    try:
        for name, ticks, reads, aggregates in cases:
            for surface in ("cli", "http"):
                script[:] = list(ticks)
                observed["read"] = 0
                observed["aggregate"] = 0
                if surface == "cli":
                    cli = runner.invoke(
                        app,
                        [
                            "integration",
                            "dashboard",
                            "token-usage",
                            "--from",
                            FROM,
                            "--to",
                            TO,
                            "--time-zone",
                            "UTC",
                        ],
                    )
                    payload = json.loads(cli.stdout)
                    assert cli.exit_code == 2, (name, cli.stdout)
                else:
                    status, payload = _post_token_usage(server.port)
                    assert status == 400, (name, payload)
                assert payload["ok"] is False, name
                assert payload["data"] is None, name
                error = payload["error"]
                assert isinstance(error, dict)
                assert error["code"] == "INVALID_ARGUMENT", (name, surface, payload)
                assert error["message"] == SCOPE_MESSAGE
                assert observed == {"read": reads, "aggregate": aggregates}, (name, surface)
    finally:
        server.shutdown()
    assert not _db(isolated_xdg).exists()


def _window(db: Path) -> tuple[str, str]:
    with sqlite3.connect(db) as conn:
        start = conn.execute(
            "SELECT MIN(launch_requested_at) FROM scheduler_attempts WHERE launch_requested_at IS NOT NULL"
        ).fetchone()[0]
    assert isinstance(start, str)
    day = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
    return (
        datetime(day.year, day.month, day.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_no_private_content(payload: dict[str, object]) -> None:
    text = json.dumps(payload)
    for forbidden in ("sdk-identity", ".jsonl", "invocation", "usage/observations", "session_id"):
        assert forbidden not in text


def test_t291_02_03_codex_roles_keep_configured_models_and_historical_jsonl(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    paths = {"db_path": _db(isolated_xdg), "artifact_root": _artifacts(isolated_xdg)}
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "usage-implemented.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 4,
                },
                {"input_tokens": 40, "cached_input_tokens": 10, "output_tokens": 5},
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5},
                {"input_tokens": 8, "output_tokens": 2},
            ]
        ),
    )
    run_id = _submit(git_repo, paths, max_reviews=2)
    start_run(run_id, db_path=paths["db_path"])
    _run_until(_tick(git_repo, paths), run_id, "completed", limit=80)
    start, end = _window(paths["db_path"])
    database_before = paths["db_path"].read_bytes()
    artifacts_before = _snapshot(paths["artifact_root"])

    def query() -> dict[str, object]:
        monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")

        def no_provider(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("token query launched a provider")

        monkeypatch.setattr(subprocess, "run", no_provider)
        data = default_dashboard_read_service(
            db_path=paths["db_path"],
            artifact_root=paths["artifact_root"],
        ).token_usage(from_value=start, to_value=end, time_zone="UTC", run_id=run_id)
        payload = data.model_dump(mode="json", by_alias=True)
        assert paths["db_path"].read_bytes() == database_before
        return payload

    payload = query()
    assert _snapshot(paths["artifact_root"]) == artifacts_before
    assert payload["source"]["knownTotalTokens"] == 195
    initial = _row(payload, provider="codex", role="implementer", stage="initial")
    correction = _row(payload, provider="codex", role="implementer", stage="correction")
    first_review = _row(payload, provider="codex", role="reviewer", stage="review")
    assert initial["configuredModel"] == "gpt-5.5"
    assert correction["configuredModel"] == "gpt-5.5"
    assert first_review["configuredModel"] == "gpt-5.6-sol"
    assert initial["knownTotalTokens"] == 110
    assert _flow_total(initial, "input_other") == 40
    assert _flow_total(initial, "cache_read") == 60
    assert _flow_total(initial, "output_other") == 6
    assert _flow_total(initial, "reasoning_output") == 4
    assert _flow_total(initial, "cache_write") is None
    assert correction["knownTotalTokens"] == 45
    assert _flow_total(correction, "input_other") == 30
    assert _flow_total(correction, "cache_read") == 10
    assert _flow_total(correction, "output_unsplit") == 5
    review = _row(payload, provider="codex", role="reviewer", stage="review")
    assert review["knownTotalTokens"] == 40
    assert review["inferenceAttempts"] == 2
    assert _flow_total(review, "input_other") == 20
    assert _flow_total(review, "cache_read") == 5
    assert _flow_total(review, "input_unsplit") == 8
    assert _flow_total(review, "output_unsplit") == 7
    assert _flow_total(review, "cache_write") is None
    assert _flow_total(payload["source"], "input_other") == 90
    assert _flow_total(payload["source"], "cache_read") == 75
    assert _flow_total(payload["source"], "input_unsplit") == 8
    assert _flow_total(payload["source"], "output_other") == 6
    assert _flow_total(payload["source"], "reasoning_output") == 4
    assert _flow_total(payload["source"], "output_unsplit") == 12
    _assert_no_private_content(payload)

    yaml_path = git_repo / "ai_dev_loop.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("gpt-5.5", "yaml-model"), encoding="utf-8"
    )
    assert query()["rows"][0]["configuredModel"] in {"gpt-5.5", "gpt-5.6-sol"}
    unchanged = query()
    assert _row(unchanged, stage="initial")["configuredModel"] == "gpt-5.5"

    for path in paths["artifact_root"].rglob("usage/observations/*/*.json"):
        path.unlink()
    remaining = _snapshot(paths["artifact_root"])
    historical = query()
    assert historical["source"]["knownTotalTokens"] == 195
    assert _snapshot(paths["artifact_root"]) == remaining

    with sqlite3.connect(paths["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        attempt = conn.execute(
            """
            SELECT attempt.*
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.implementer_bootstrap'
            """,
            (run_id,),
        ).fetchone()
    assert attempt is not None
    evidence_path = ProtectedArtifactStore(paths["artifact_root"]).run_root(
        run_id
    ) / invocation_evidence_rel(str(attempt["attempt_id"]))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["model"] == "gpt-5.5"
    evidence.pop("model")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    os.chmod(evidence_path, 0o600)
    launch_intent = json.dumps(
        {
            "attempt_id": str(attempt["attempt_id"]),
            "dispatch_id": str(attempt["dispatch_id"]),
            "run_id": run_id,
            "unit_identity": str(attempt["unit_identity"]),
            "launch_nonce": str(attempt["launch_nonce"]),
            "effect_kind": "codex.implementer_bootstrap",
            "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute(
            "UPDATE scheduler_attempts SET launch_intent_sha256 = ? WHERE attempt_id = ?",
            (payload_sha256(launch_intent), str(attempt["attempt_id"])),
        )
        conn.commit()
    forgotten = query()
    missing_model = _row(forgotten, provider="codex", role="implementer", stage="initial")
    assert missing_model["configuredModel"] is None
    assert missing_model["knownTotalTokens"] == 110
    assert forgotten["source"]["knownTotalTokens"] == 195
    assert "Unknown" not in json.dumps(missing_model)


def test_t291_02_03_sdk_replay_is_one_configured_model_observation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 4820,
        "output_tokens": 139,
        "cache_read_tokens": 2560,
        "cache_write_tokens": 0,
        "total_tokens": 7519,
        "reasoning_tokens": 118,
    }
    marker = tmp_path / "stream-interrupted"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK replay tokens",
    )
    state = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert state.kind == "completed"  # type: ignore[attr-defined]
    start, end = _window(db)
    database_before = db.read_bytes()
    payload = default_dashboard_read_service(db_path=db, artifact_root=artifacts_root).token_usage(
        from_value=start,
        to_value=end,
        time_zone="UTC",
        run_id=run_id,
    )
    body = payload.model_dump(mode="json", by_alias=True)
    cursor = _row(body, provider="cursor", role="implementer", stage="initial")
    assert cursor["configuredModel"] == "grok-4.7"
    assert cursor["inferenceAttempts"] == 1
    assert cursor["knownTotalTokens"] == 7519
    assert _flow_total(cursor, "input_other") == 4820
    assert _flow_total(cursor, "cache_read") == 2560
    assert _flow_total(cursor, "cache_write") == 0
    assert _flow_total(cursor, "output_other") == 21
    assert _flow_total(cursor, "reasoning_output") == 118
    assert body["source"]["knownTotalTokens"] == 7519
    reviewer = _row(body, provider="codex", role="reviewer", stage="review")
    assert reviewer["configuredModel"] == "gpt-6.1-sol"
    cohort = default_dashboard_read_service(db_path=db, artifact_root=artifacts_root).token_usage(
        from_value=start,
        to_value=end,
        time_zone="UTC",
    )
    cohort_text = json.dumps(cohort.model_dump(mode="json", by_alias=True))
    assert run_id not in cohort_text
    assert "SDK replay tokens" not in cohort_text
    assert db.read_bytes() == database_before
    _assert_no_private_content(body)

    yaml_path = git_repo / "ai_dev_loop.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("grok-4.7", "yaml-not-used"),
        encoding="utf-8",
    )
    again = default_dashboard_read_service(db_path=db, artifact_root=artifacts_root).token_usage(
        from_value=start,
        to_value=end,
        time_zone="UTC",
        run_id=run_id,
    )
    assert (
        _row(again.model_dump(mode="json", by_alias=True), provider="cursor")["configuredModel"]
        == "grok-4.7"
    )

    http = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts_root)
    ).dispatch(
        "dashboard.token-usage",
        {"fromUtc": start, "toUtc": end, "timeZone": "UTC", "runId": run_id},
    )
    assert http["source"]["knownTotalTokens"] == 7519
    validate_integration_instance(http, DASHBOARD_TOKEN_USAGE_DATA_SCHEMA)

    observation = next(artifacts_root.rglob("usage/observations/*/*.json"))
    observation.write_bytes(observation.read_bytes() + b" ")
    tampered = default_dashboard_read_service(db_path=db, artifact_root=artifacts_root).token_usage(
        from_value=start,
        to_value=end,
        time_zone="UTC",
        run_id=run_id,
    )
    tampered_body = tampered.model_dump(mode="json", by_alias=True)
    assert tampered_body["source"]["knownTotalTokens"] != 7519
    assert "usage_observation_integrity_error" in tampered_body["source"]["diagnostics"]
    _assert_no_private_content(tampered_body)
    assert observation.name not in json.dumps(tampered_body)


def test_t291_02_historical_cli_model_comes_from_the_authenticated_invocation(
    tmp_path: Path,
) -> None:
    fixture = HISTORICAL.parent
    db = tmp_path / "engine.sqlite3"
    artifacts = tmp_path / "artifacts"
    shutil.copyfile(HISTORICAL, db)
    shutil.copytree(fixture / "artifacts", artifacts)
    fixture_before = HISTORICAL.read_bytes()
    database_before = db.read_bytes()
    service = default_dashboard_read_service(db_path=db, artifact_root=artifacts)
    payload = service.token_usage(
        from_value="2026-09-01T00:00:00Z",
        to_value="2026-10-01T00:00:00Z",
        time_zone="UTC",
    )
    body = payload.model_dump(mode="json", by_alias=True)
    cursor = _row(body, provider="cursor", role="implementer", stage="initial")
    reviewer = _row(body, provider="codex", role="reviewer", stage="review")
    assert cursor["configuredModel"] == "composer-2.5-fast"
    assert reviewer["configuredModel"] == "gpt-5.6-sol"
    assert cursor["knownTotalTokens"] is None
    assert payload.source.coverage == "unknown"
    assert payload.cohort_run_count == 1
    text = json.dumps(body)
    assert "fixture-project-20260921T013201Z-50f2bd" not in text
    assert "pytest-of-rojobad" not in text
    assert "/tmp/" not in text
    for path in artifacts.rglob("*.yaml"):
        path.chmod(0o600)
        path.write_text(
            path.read_text(encoding="utf-8").replace("composer-2.5-fast", "later-yaml-model"),
            encoding="utf-8",
        )
    again = service.token_usage(
        from_value="2026-09-01T00:00:00Z",
        to_value="2026-10-01T00:00:00Z",
        time_zone="UTC",
    )
    assert (
        _row(again.model_dump(mode="json", by_alias=True), provider="cursor")["configuredModel"]
        == "composer-2.5-fast"
    )
    assert db.read_bytes() == database_before
    assert HISTORICAL.read_bytes() == fixture_before
