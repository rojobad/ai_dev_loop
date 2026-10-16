"""Focused Phase 26.5 regression through real readers and the loopback adapter."""

from __future__ import annotations

import base64
import json
import threading

from tests.integration.test_phase26_2_dashboard_server import _post, _request
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick,
    drive,
    prepare,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    scheduler_paths as _scheduler_paths,
)

from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.server import DashboardServer
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.domain.codex_contract import codex_review_prompt_rel
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

scheduler_paths = _scheduler_paths


def _read_all(port: int, operation: str, parameters: dict[str, object]) -> tuple[bytes, list[dict]]:
    offset = 0
    chunks: list[dict] = []
    content = bytearray()
    while True:
        payload = _post(
            port,
            operation,
            {**parameters, "byteOffset": offset, "limit": 13},
        )["data"]
        assert isinstance(payload, dict)
        chunks.append(payload)
        content.extend(base64.b64decode(payload["contentBase64"]))
        if not payload["hasMore"]:
            break
        offset = payload["nextOffset"]
    return bytes(content), chunks


def test_c265_2_real_readers_http_chunks_and_role_ownership(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
) -> None:
    """The server serves exact verified artifacts; reviewer ownership stays separate."""

    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    final = drive(tick, scheduler_paths, result.sequence_id, monkeypatch)
    assert len(final.materialized_entries) == 2

    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT a.run_id, a.attempt_id, a.component, a.iteration, e.effect_kind
            FROM scheduler_attempts AS a
            JOIN scheduler_effects AS e ON e.dispatch_id = a.dispatch_id
            WHERE a.component IN ('codex_implementer', 'codex')
            ORDER BY a.created_at, a.attempt_id
            """
        ).fetchall()
    implementer = next(row for row in rows if row["component"] == "codex_implementer")
    reviewer = next(row for row in rows if row["component"] == "codex")

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
        initial, initial_chunks = _read_all(
            server.port,
            "run.initial-prompt",
            {"runId": str(implementer["run_id"])},
        )
        assert initial == (git_repo / "docs/plans/prompt_sample-plan.txt").read_bytes()
        assert all(chunk["runId"] == implementer["run_id"] for chunk in initial_chunks)
        assert all(chunk["returnedBytes"] <= 13 for chunk in initial_chunks)

        prompt, review_chunks = _read_all(
            server.port,
            "run.review-content",
            {
                "runId": str(reviewer["run_id"]),
                "attemptId": str(reviewer["attempt_id"]),
                "kind": "prompt",
            },
        )
        prompt_path = run_artifact_root(
            scheduler_paths["artifact_root"], str(reviewer["run_id"])
        ) / codex_review_prompt_rel(int(reviewer["iteration"]), str(reviewer["attempt_id"]))
        assert prompt == prompt_path.read_bytes()
        assert all(chunk["attemptId"] == reviewer["attempt_id"] for chunk in review_chunks)

        output = _post(
            server.port,
            "run.output",
            {
                "runId": str(implementer["run_id"]),
                "attemptId": str(implementer["attempt_id"]),
                "stream": "stdout",
                "limit": 17,
            },
        )["data"]
        assert output["available"] is True
        assert output["component"] == "codex_implementer"
        assert output["returnedBytes"] == 17
        assert output["complete"] is True
        assert output["hasMore"] is True

        rejected_body = json.dumps(
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
            body=rejected_body,
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 400
        assert json.loads(rejected)["error"]["code"] == "INVALID_ARGUMENT"
    finally:
        server.shutdown()
        thread.join(timeout=5)
