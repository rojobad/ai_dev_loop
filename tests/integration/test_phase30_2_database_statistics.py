"""Phase 30.2 SQLite-only CLI, API, and Dashboard token statistics."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.test_phase25_3_sdk_recovery import _submit_sdk_run, _tick_until_kind
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.integration_api.dashboard_service import default_dashboard_read_service
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.run_service import default_run_read_service
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.token_usage_query import scheduler_usage

runner = CliRunner()
_CURSOR_USAGE = {
    "input_tokens": 4820,
    "output_tokens": 139,
    "cache_read_tokens": 2560,
    "cache_write_tokens": 0,
    "total_tokens": 7519,
    "reasoning_tokens": 118,
}
_REVIEW_USAGE = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}


def test_b03_b04_public_statistics_use_sqlite_facts_after_source_loss(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW_USAGE]))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR_USAGE))
    run_id, db, artifacts, _store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SQLite token facts",
    )
    _tick_until_kind(tick, _store, run_id, "completed")
    paths = {"db_path": db, "artifact_root": artifacts}
    _forbid_sources(monkeypatch)
    usage = _usage_payload(paths, run_id)
    assert usage["direct"]["known_total_tokens"] == 7549
    assert usage["direct"]["coverage"] == "complete"
    assert usage["recovery_lineage"]["known_total_tokens"] == 7549
    cursor = _breakdown(usage["direct"]["breakdowns"], provider="cursor", role="implementer")
    review = _breakdown(usage["direct"]["breakdowns"], provider="codex", role="reviewer")
    assert cursor["known_total_tokens"] == 7519
    assert cursor["categories"]["input_total"]["known_total"] == 7380
    assert cursor["categories"]["cache_read"]["known_total"] == 2560
    assert cursor["categories"]["cache_write"]["known_total"] == 0
    assert cursor["categories"]["output_total"]["known_total"] == 139
    assert cursor["categories"]["reasoning_output"]["known_total"] == 118
    assert review["known_total_tokens"] == 30
    assert review["categories"]["cache_read"]["known_total"] == 5

    cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert cli.exit_code == 0, cli.stderr
    assert json.loads(cli.stdout)["direct"]["known_total_tokens"] == 7549
    inspected = runner.invoke(app, ["integration", "run", "inspect", run_id])
    assert inspected.exit_code == 0, inspected.stdout
    body = json.loads(inspected.stdout)["data"]["tokenUsage"]["direct"]
    assert body["knownTotalTokens"] == 7549
    assert body["coverage"] == "complete"
    start, end = _window(db)
    dashboard = _dashboard(paths, run_id, start, end)
    assert dashboard["source"]["knownTotalTokens"] == 7549
    assert _dashboard_row(dashboard, provider="cursor")["configuredModel"] == "grok-4.7"
    assert _dashboard_row(dashboard, provider="cursor")["knownTotalTokens"] == 7519
    assert _flow(_dashboard_row(dashboard, provider="cursor"), "cache_read") == 2560
    assert _flow(_dashboard_row(dashboard, provider="cursor"), "reasoning_output") == 118
    assert _dashboard_row(dashboard, provider="codex")["configuredModel"] == "gpt-6.1-sol"
    http = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts)
    ).dispatch(
        "dashboard.token-usage",
        {"fromUtc": start, "toUtc": end, "timeZone": "UTC", "runId": run_id},
    )
    assert http["source"]["knownTotalTokens"] == 7549
    outside = _dashboard(
        paths,
        None,
        "2020-01-01T00:00:00Z",
        "2020-01-02T00:00:00Z",
    )
    assert outside["source"]["knownTotalTokens"] is None
    assert outside["rows"] == []

    shutil.rmtree(artifacts)
    assert scheduler_usage(
        run_id, db_path=db, artifact_root=artifacts
    ).direct.known_total_tokens == (7549)
    missing_root = default_run_read_service(
        db_path=db,
        artifact_root=artifacts.parent / "missing-artifacts",
    ).inspect_run(run_id)
    assert missing_root.token_usage.direct.known_total_tokens == 7549
    assert missing_root.token_usage.recovery_lineage.known_total_tokens == 7549
    assert _dashboard(paths, run_id, start, end)["source"]["knownTotalTokens"] == 7549

    _corrupt_fact(db)
    with pytest.raises(SchedulerEngineError, match="usage statistics payload is invalid") as raised:
        scheduler_usage(run_id, db_path=db, artifact_root=artifacts)
    assert raised.value.kind is SchedulerEngineErrorKind.CORRUPTION
    failed = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert failed.exit_code != 0
    assert "usage statistics payload is invalid" in failed.stderr
    with pytest.raises(IntegrationApiError, match="Scheduler run state is inconsistent.") as api:
        default_run_read_service(db_path=db, artifact_root=artifacts).inspect_run(run_id)
    assert api.value.code is IntegrationErrorCode.DATA_INTEGRITY
    with pytest.raises(
        IntegrationApiError, match="Scheduler dashboard data failed integrity validation."
    ):
        _dashboard(paths, run_id, start, end)


def _forbid_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
        ReadOnlyProtectedArtifactStore,
    )

    original = ReadOnlyProtectedArtifactStore.read_verified_bytes

    def guarded(
        self: ReadOnlyProtectedArtifactStore,
        run_id: str,
        relative_path: str,
        **kwargs: object,
    ) -> bytes:
        if "usage/" in relative_path or relative_path.endswith(".jsonl"):
            raise AssertionError("sqlite statistics opened a usage source")
        return original(self, run_id, relative_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
        _refuse,
    )
    monkeypatch.setattr(ReadOnlyProtectedArtifactStore, "read_verified_bytes", guarded)


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("sqlite statistics opened a usage source")


def _usage_payload(paths: dict[str, Path], run_id: str) -> dict[str, object]:
    report = scheduler_usage(
        run_id,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    )
    return json.loads(report.model_dump_json())


def _breakdown(rows: object, **expected: str) -> dict[str, object]:
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        if all(item[key] == value for key, value in expected.items()):
            return item
    raise AssertionError(expected)


def _dashboard(
    paths: dict[str, Path],
    run_id: str | None,
    start: str,
    end: str,
) -> dict[str, object]:
    data = default_dashboard_read_service(
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    ).token_usage(
        from_value=start,
        to_value=end,
        time_zone="UTC",
        run_id=run_id,
    )
    payload = data.model_dump(mode="json", by_alias=True)
    assert isinstance(payload, dict)
    return payload


def _dashboard_row(payload: dict[str, object], **expected: str) -> dict[str, object]:
    return _breakdown(payload["rows"], **expected)


def _flow(row: dict[str, object], name: str) -> int | None:
    flows = row["flows"]
    assert isinstance(flows, list)
    found = next(item for item in flows if item["name"] == name)
    total = found["knownTotal"]
    assert total is None or isinstance(total, int)
    return total


def _window(database: Path) -> tuple[str, str]:
    with sqlite3.connect(database) as conn:
        start = conn.execute(
            """
            SELECT MIN(launch_requested_at) FROM scheduler_attempts
            WHERE launch_requested_at IS NOT NULL
            """
        ).fetchone()[0]
    assert isinstance(start, str)
    moment = datetime.fromisoformat(start.replace("Z", "+00:00"))
    opened = moment - timedelta(minutes=1)
    closed = moment + timedelta(days=1)
    return (
        opened.strftime("%Y-%m-%dT%H:%M:%SZ"),
        closed.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _corrupt_fact(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            UPDATE scheduler_usage_attempt_facts
            SET fact_payload = '{"schema_version":1}'
            WHERE logical_attempt_id = (
                SELECT logical_attempt_id FROM scheduler_usage_attempt_facts LIMIT 1
            )
            """
        )
        conn.commit()
