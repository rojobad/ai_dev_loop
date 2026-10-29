"""Phase 30.1 production ingestion of compact SQLite usage facts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase25_3_sdk_recovery import (
    _submit_sdk_run,
    _tick_until_kind,
)
from tests.integration.test_phase25_8_sdk_no_progress import _instant, _pending_no_progress
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    _run_until,
    _submit,
    _tick,
)
from tests.unit.scheduler.test_phase27_4_implementer_timeout_retry import (
    _Clock,
    _drive_to_kind,
    _drive_to_timeout,
    _InlineCodexBackend,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.token_usage_query import TokenUsageReadService
from ai_dev_loop.scheduler.application.usage_ingestion import (
    UsageIngestionBarrier,
    reconcile_unacknowledged_usage,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_IMPLEMENTER_USAGE = {
    "input_tokens": 100,
    "cached_input_tokens": 60,
    "output_tokens": 10,
    "reasoning_output_tokens": 4,
}
_IMPLEMENTER_NORMALIZED = {
    "input_total": 100,
    "input_uncached": 40,
    "cache_read": 60,
    "output_total": 10,
    "reasoning_output": 4,
    "total_tokens": 110,
}
_CORRECTION_USAGE = {
    "input_tokens": 8,
    "cached_input_tokens": 0,
    "output_tokens": 2,
    "reasoning_output_tokens": 1,
}
_CORRECTION_NORMALIZED = {
    "input_total": 8,
    "input_uncached": 8,
    "cache_read": 0,
    "output_total": 2,
    "reasoning_output": 1,
    "total_tokens": 10,
}
_REVIEW_USAGE = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}
_REVIEW_NORMALIZED = {
    "input_total": 25,
    "input_uncached": 20,
    "cache_read": 5,
    "output_total": 5,
    "reasoning_output": None,
    "total_tokens": 30,
}
_SECOND_REVIEW_USAGE = {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 3}
_SECOND_REVIEW_NORMALIZED = {
    "input_total": 12,
    "input_uncached": 10,
    "cache_read": 2,
    "output_total": 3,
    "reasoning_output": None,
    "total_tokens": 15,
}
_CURSOR_USAGE = {
    "input_tokens": 4820,
    "output_tokens": 139,
    "cache_read_tokens": 2560,
    "cache_write_tokens": 0,
    "total_tokens": 7519,
    "reasoning_tokens": 118,
}
_CURSOR_NORMALIZED = {
    "input_total": 7380,
    "input_uncached": 4820,
    "cache_read": 2560,
    "cache_write": 0,
    "output_total": 139,
    "reasoning_output": 118,
    "total_tokens": 7519,
}
_DEADLINE_USAGE = {
    "input_tokens": 100,
    "output_tokens": 10,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "total_tokens": 110,
}
_DEADLINE_NORMALIZED = {
    "input_total": 100,
    "input_uncached": 100,
    "cache_read": 0,
    "cache_write": 0,
    "output_total": 10,
    "reasoning_output": None,
    "total_tokens": 110,
}
_PROMPT_TEXT = "Implement the sample plan exactly as written."


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": state_root / "engine.sqlite3", "artifact_root": state_root / "artifacts"}


def _facts(store: SqliteSchedulerStore, run_id: str) -> list[sqlite3.Row]:
    with store.begin_read() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM scheduler_usage_attempt_facts
                WHERE run_id = ?
                ORDER BY provider, role, stage, logical_attempt_id
                """,
                (run_id,),
            )
        )


def _observations(store: SqliteSchedulerStore, run_id: str) -> list[sqlite3.Row]:
    with store.begin_read() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM scheduler_usage_observations
                WHERE run_id = ?
                ORDER BY provider, role, stage, native_key
                """,
                (run_id,),
            )
        )


def _attempt_count(store: SqliteSchedulerStore, run_id: str) -> int:
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _assert_counters(
    row: sqlite3.Row, native: dict[str, int], normalized: dict[str, int | None]
) -> None:
    stored_native = json.loads(str(row["native_payload"]))
    stored_normalized = json.loads(str(row["normalized_payload"]))
    for key, value in native.items():
        assert stored_native[key] == value
    for key, value in normalized.items():
        assert stored_normalized[key] == value


def _assert_no_raw_usage_text(store: SqliteSchedulerStore) -> None:
    with store.begin_read() as conn:
        chunks: list[str] = []
        for table in ("scheduler_usage_attempt_facts", "scheduler_usage_observations"):
            for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608
                chunks.extend(str(value) for value in tuple(row) if value is not None)
    text = "\n".join(chunks)
    assert _PROMPT_TEXT not in text
    assert "cursor_fix_prompt" not in text
    assert "/home/" not in text
    assert "usage_limit_exceeded" not in text


def _restart_tick(
    git_repo: Path,
    db_path: Path,
    artifact_root: Path,
    *,
    backend: FakeAgentProcessBackend | None = None,
) -> TickService:
    return TickService(
        SqliteSchedulerStore(db_path),
        ProtectedArtifactStore(artifact_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 28, 20, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "phase30-restart",
        attempt_backend=backend or FakeAgentProcessBackend(),
    )


def _effect_attempt_ids(store: SqliteSchedulerStore, run_id: str, effect_kind: str) -> set[str]:
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT attempt.attempt_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = ?
            """,
            (run_id, effect_kind),
        ).fetchall()
    return {str(row["attempt_id"]) for row in rows}


def _complete_codex(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    implementer_usage: object,
    review_usage: object,
    review_sequence: str = "no_findings",
    implementer_sequence: str | None = None,
) -> tuple[str, object]:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "usage-implemented.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON", json.dumps(implementer_usage))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps(review_usage))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    if implementer_sequence is not None:
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", implementer_sequence)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    state = _run_until(tick, run_id, "completed")
    return run_id, state


def test_s02_codex_initial_and_review_persist_exact_counters_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE],
        review_usage=[_REVIEW_USAGE],
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    facts = _facts(store, run_id)
    observations = _observations(store, run_id)
    assert {(str(row["provider"]), str(row["role"]), str(row["stage"])) for row in facts} == {
        ("codex", "implementer", "initial"),
        ("codex", "reviewer", "review"),
    }
    assert len(observations) == 2
    by_role = {str(row["role"]): row for row in observations}
    _assert_counters(by_role["implementer"], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    _assert_counters(by_role["reviewer"], _REVIEW_USAGE, _REVIEW_NORMALIZED)
    assert all(str(row["coverage"]) == "complete" for row in facts)
    assert all(str(row["settlement_status"]) == "settled" for row in facts)
    assert all(str(row["acknowledgement"]) == "acknowledged" for row in facts)
    models = {str(row["role"]): row["configured_model"] for row in facts}
    assert models["implementer"] == "gpt-5.5"
    assert models["reviewer"] == "gpt-5.6-sol"
    public = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).run_report(run_id)
    assert public.direct.known_total_tokens == 140
    missing = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"].parent / "missing-usage-artifacts",
    ).run_report(run_id)
    assert missing.direct.known_total_tokens is None
    assert len(_observations(store, run_id)) == 2
    _assert_no_raw_usage_text(store)


def test_s02_codex_correction_and_second_review_keep_separate_facts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE, _CORRECTION_USAGE],
        review_usage=[_REVIEW_USAGE, _SECOND_REVIEW_USAGE],
        review_sequence="findings,no_findings",
        implementer_sequence="success,success",
    )
    assert state.codex.reviews_completed == 2  # type: ignore[attr-defined]
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    observations = _observations(store, run_id)
    stages = {
        (
            str(row["role"]),
            str(row["stage"]),
            json.loads(str(row["normalized_payload"]))["total_tokens"],
        )
        for row in observations
    }
    assert stages == {
        ("implementer", "initial", 110),
        ("implementer", "correction", 10),
        ("reviewer", "review", 30),
        ("reviewer", "review", 15),
    }
    assert len(_facts(store, run_id)) == 4
    _assert_no_raw_usage_text(store)


def test_s02_multiple_codex_turns_are_one_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[[_IMPLEMENTER_USAGE, _CORRECTION_USAGE]],
        review_usage=[_REVIEW_USAGE],
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    implementer = [row for row in _observations(store, run_id) if str(row["role"]) == "implementer"]
    assert len(implementer) == 2
    assert len({str(row["logical_attempt_id"]) for row in implementer}) == 1
    totals = sorted(
        json.loads(str(row["normalized_payload"]))["total_tokens"] for row in implementer
    )
    assert totals == [10, 110]
    facts = [row for row in _facts(store, run_id) if str(row["role"]) == "implementer"]
    assert len(facts) == 1


def test_s02_malformed_review_usage_stays_unknown_and_run_completes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE],
        review_usage=[{"input_tokens": True, "output_tokens": 5}],
    )
    assert state.kind == "completed"  # type: ignore[attr-defined]
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reviewer = next(row for row in _facts(store, run_id) if str(row["role"]) == "reviewer")
    assert str(reviewer["coverage"]) == "unknown"
    assert str(reviewer["settlement_status"]) == "settled"
    reviewer_observations = [
        row for row in _observations(store, run_id) if str(row["role"]) == "reviewer"
    ]
    assert reviewer_observations == []
    implementer = next(
        row for row in _observations(store, run_id) if str(row["role"]) == "implementer"
    )
    _assert_counters(implementer, _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)


def test_s02_failed_implementer_is_unknown_and_abort_does_not_revive(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    _drive_to_timeout(tick, scheduler_paths, run_id)
    store = tick.store
    failed = [
        row
        for row in _facts(store, run_id)
        if str(row["provider"]) == "codex" and str(row["role"]) == "implementer"
    ]
    assert len(failed) == 1
    assert str(failed[0]["coverage"]) == "unknown"
    assert str(failed[0]["terminal_status"]) == "failed"
    assert _observations(store, run_id) == []
    before_attempts = _attempt_count(store, run_id)
    result = SchedulerAbortService(
        store,
        backend,
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).abort_run(run_id)
    assert result.state_kind == "aborted"
    clock.advance(minutes=60)
    for _ in range(5):
        tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    assert state.kind == "aborted"
    assert _attempt_count(store, run_id) == before_attempts
    assert _observations(store, run_id) == []
    assert len(_facts(store, run_id)) == 1
    _assert_no_raw_usage_text(store)


def test_s02_cursor_initial_and_review_counters_and_preflight_is_not_consumption(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW_USAGE]))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR_USAGE))
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK usage facts",
    )
    state = _tick_until_kind(tick, store, run_id, "completed")
    assert state.kind == "completed"  # type: ignore[attr-defined]
    cursor = next(row for row in _observations(store, run_id) if str(row["provider"]) == "cursor")
    _assert_counters(cursor, _CURSOR_USAGE, _CURSOR_NORMALIZED)
    cursor_fact = next(row for row in _facts(store, run_id) if str(row["provider"]) == "cursor")
    assert str(cursor_fact["configured_model"]) == "grok-4.7"
    assert str(cursor_fact["coverage"]) == "complete"
    reviewer = next(row for row in _facts(store, run_id) if str(row["role"]) == "reviewer")
    assert str(reviewer["configured_model"]) == "gpt-6.1-sol"
    with store.begin_read() as conn:
        create_rows = conn.execute(
            """
            SELECT attempt.attempt_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.create_chat'
            """,
            (run_id,),
        ).fetchall()
    create_ids = {str(row["attempt_id"]) for row in create_rows}
    assert create_ids
    assert create_ids.isdisjoint({str(row["logical_attempt_id"]) for row in _facts(store, run_id)})


def test_s02_sdk_preflight_failure_is_not_consumption(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_TURN_PREFLIGHT_FAILURE", "1")
    send_counter = tmp_path / "preflight-sends"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK preflight facts",
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_sdk_preflight_failed"  # type: ignore[attr-defined]
    facts = _facts(store, run_id)
    assert len(facts) == 1
    assert str(facts[0]["coverage"]) == "not_applicable"
    assert str(facts[0]["settlement_status"]) == "settled"
    assert _observations(store, run_id) == []
    assert not send_counter.exists()


def test_s03_commit_barrier_replays_one_contribution_without_another_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE],
        review_usage=[_REVIEW_USAGE],
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    store.usage_artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    before_attempts = _attempt_count(store, run_id)
    with store.begin_read() as conn:
        attempt_id = str(
            conn.execute(
                """
                SELECT logical_attempt_id FROM scheduler_usage_attempt_facts
                WHERE run_id = ? AND role = 'implementer'
                """,
                (run_id,),
            ).fetchone()["logical_attempt_id"]
        )
        conn.execute("DELETE FROM scheduler_usage_observations")
        conn.execute("DELETE FROM scheduler_usage_attempt_facts")
    with store.begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_usage_observations").fetchone()[0] == 0
    store.usage_ingestion_barrier = "before_commit"
    with pytest.raises(UsageIngestionBarrier), store.begin_immediate() as conn:
        store.mark_attempt_ingested(
            conn,
            attempt_id=attempt_id,
            now=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
        )
    with store.begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_usage_observations").fetchone()[0] == 0
    with store.begin_immediate() as conn:
        store.mark_attempt_ingested(
            conn,
            attempt_id=attempt_id,
            now=datetime(2026, 10, 28, 18, 1, tzinfo=UTC),
        )
    observations = [
        row for row in _observations(store, run_id) if str(row["role"]) == "implementer"
    ]
    assert len(observations) == 1
    _assert_counters(observations[0], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    assert _attempt_count(store, run_id) == before_attempts
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    assert state.kind == "completed"


def test_s03_ack_barrier_converges_without_a_second_observation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    send_counter = tmp_path / "ack-implementer-sends"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "ack-barrier.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([_IMPLEMENTER_USAGE]),
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW_USAGE]))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    tick.store.usage_ingestion_barrier = "before_ack"
    held_ids: list[str] = []
    for _ in range(40):
        tick.run_once()
        held_ids = [
            str(row["observation_id"])
            for row in _observations(tick.store, run_id)
            if str(row["role"]) == "implementer"
        ]
        with tick.store.begin_read() as conn:
            pending = conn.execute(
                """
                SELECT COUNT(*) FROM scheduler_usage_attempt_facts
                WHERE run_id = ? AND role = 'implementer' AND acknowledgement = 'pending'
                """,
                (run_id,),
            ).fetchone()
        if held_ids and pending is not None and int(pending[0]) == 1:
            break
    assert held_ids == [held_ids[0]]
    assert send_counter.read_text(encoding="utf-8") == "1"
    with tick.store.begin_read() as conn:
        held_state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
    held_reviews = int(getattr(held_state.codex, "reviews_completed", 0))
    restarted = _restart_tick(
        git_repo,
        scheduler_paths["db_path"],
        scheduler_paths["artifact_root"],
    )
    restarted.run_once()
    replayed = [
        str(row["observation_id"])
        for row in _observations(restarted.store, run_id)
        if str(row["role"]) == "implementer"
    ]
    assert replayed == held_ids
    assert send_counter.read_text(encoding="utf-8") == "1"
    with restarted.store.begin_read() as conn:
        acknowledged = conn.execute(
            """
            SELECT acknowledgement, settlement_status FROM scheduler_usage_attempt_facts
            WHERE logical_attempt_id = (
                SELECT logical_attempt_id FROM scheduler_usage_observations
                WHERE observation_id = ?
            )
            """,
            (held_ids[0],),
        ).fetchone()
        restarted_state, _, _ = restarted.store.load_validated_snapshot(conn, run_id)
    assert acknowledged is not None
    assert str(acknowledged["acknowledgement"]) == "acknowledged"
    assert int(getattr(restarted_state.codex, "reviews_completed", 0)) == held_reviews
    state = _run_until(restarted, run_id, "completed")
    assert state.kind == "completed"  # type: ignore[attr-defined]
    assert send_counter.read_text(encoding="utf-8") == "1"
    final_ids = [
        str(row["observation_id"])
        for row in _observations(restarted.store, run_id)
        if str(row["role"]) == "implementer"
    ]
    assert final_ids == held_ids
    _assert_counters(
        next(
            row
            for row in _observations(restarted.store, run_id)
            if str(row["role"]) == "implementer"
        ),
        _IMPLEMENTER_USAGE,
        _IMPLEMENTER_NORMALIZED,
    )


def test_s03_deadline_reconciliation_counts_the_source_attempt_once(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(_DEADLINE_USAGE))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_DEADLINE_USAGE))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK deadline facts",
    )
    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    cursor_rows = [row for row in _observations(store, run_id) if str(row["provider"]) == "cursor"]
    assert len(cursor_rows) == 1
    _assert_counters(cursor_rows[0], _DEADLINE_USAGE, _DEADLINE_NORMALIZED)
    with store.begin_read() as conn:
        source_attempts = {
            str(row["attempt_id"])
            for row in conn.execute(
                """
                SELECT attempt.attempt_id
                FROM scheduler_attempts AS attempt
                JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
                WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.run_turn'
                """,
                (run_id,),
            )
        }
        reconcile_attempts = {
            str(row["attempt_id"])
            for row in conn.execute(
                """
                SELECT attempt.attempt_id
                FROM scheduler_attempts AS attempt
                JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
                WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.reconcile_sdk_run'
                """,
                (run_id,),
            )
        }
    assert str(cursor_rows[0]["logical_attempt_id"]) in source_attempts
    assert str(cursor_rows[0]["logical_attempt_id"]) not in reconcile_attempts
    assert reconcile_attempts


def test_s03_no_progress_replay_keeps_one_observation_and_one_send(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR_USAGE))
    run_id, _artifacts, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK no progress facts",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    clock[0] = _instant(str(row["due_at"]))
    finished = _tick_until_kind(tick, store, run_id, "completed")
    assert finished.kind == "completed"  # type: ignore[attr-defined]
    cursor_rows = [
        item for item in _observations(store, run_id) if str(item["provider"]) == "cursor"
    ]
    assert len(cursor_rows) == 1
    _assert_counters(cursor_rows[0], _CURSOR_USAGE, _CURSOR_NORMALIZED)
    assert counter.read_text(encoding="utf-8") == "1"


def test_s03_missing_source_becomes_debt_without_a_new_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE],
        review_usage=[_REVIEW_USAGE],
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    attempts_before = _attempt_count(store, run_id)
    run_root = artifacts.run_root(run_id)
    for path in run_root.rglob("*"):
        if path.is_file():
            path.unlink()
    with store.begin_immediate() as conn:
        conn.execute("DELETE FROM scheduler_usage_observations WHERE run_id = ?", (run_id,))
        conn.execute(
            """
            UPDATE scheduler_usage_attempt_facts
            SET settlement_status = 'pending',
                coverage = NULL,
                acknowledgement = 'pending'
            WHERE run_id = ?
            """,
            (run_id,),
        )
    reconcile_unacknowledged_usage(
        store,
        artifacts,
        now=datetime(2026, 10, 28, 19, 0, tzinfo=UTC),
    )
    facts = _facts(store, run_id)
    assert facts
    assert {str(row["settlement_status"]) for row in facts} == {"debt"}
    assert all(row["coverage"] is None for row in facts)
    assert _observations(store, run_id) == []
    assert _attempt_count(store, run_id) == attempts_before
    with store.begin_read() as conn:
        assert store.load_validated_snapshot(conn, run_id)[0].kind == "completed"
    _assert_no_raw_usage_text(store)


def test_s04_usage_replay_does_not_change_operational_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER_USAGE],
        review_usage=[_REVIEW_USAGE],
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])

    def snapshot() -> tuple[str, str, int, int, int, str]:
        with store.begin_read() as conn:
            run = conn.execute(
                """
                SELECT state_kind, state_payload_sha256, version
                FROM scheduler_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            reservations = conn.execute(
                "SELECT COUNT(*) FROM scheduler_repository_reservations"
            ).fetchone()
        assert run is not None and events is not None and reservations is not None
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return (
            str(run["state_kind"]),
            str(run["state_payload_sha256"]),
            int(run["version"]),
            int(events[0]),
            int(reservations[0]),
            porcelain,
        )

    before = snapshot()
    reviews = state.codex.reviews_completed  # type: ignore[attr-defined]
    tick = _tick(git_repo, scheduler_paths)
    tick.run_once()
    reconcile_unacknowledged_usage(
        store,
        artifacts,
        now=datetime(2026, 10, 28, 20, 0, tzinfo=UTC),
    )
    assert snapshot() == before
    with store.begin_read() as conn:
        assert store.load_validated_snapshot(conn, run_id)[0].codex.reviews_completed == reviews
    for fact in _facts(store, run_id):
        assert fact["configured_model"] in {"gpt-5.5", "gpt-5.6-sol"}
        assert str(fact["coverage"]) == "complete"
    _assert_no_raw_usage_text(store)


def _drop_one_bound_observation(
    monkeypatch: pytest.MonkeyPatch,
    artifact_root: Path,
) -> list[Path]:
    original = SqliteSchedulerStore.mark_attempt_ingested
    removed: list[Path] = []

    def wrapped(
        self: SqliteSchedulerStore, conn: sqlite3.Connection, *, attempt_id: str, now: datetime
    ) -> bool:
        if not removed:
            attempt = self.get_attempt_by_id(conn, attempt_id)
            if attempt is not None:
                root = ProtectedArtifactStore(artifact_root).run_root(str(attempt["run_id"]))
                files = sorted((root / "usage" / "observations").glob("*/*.json"))
                if len(files) >= 2:
                    removed.append(files[-1])
                    files[-1].unlink()
        return original(self, conn, attempt_id=attempt_id, now=now)

    monkeypatch.setattr(SqliteSchedulerStore, "mark_attempt_ingested", wrapped)
    return removed


def test_f01_reconciliation_write_failure_keeps_a_statistics_obligation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    send_counter = tmp_path / "deadline-sends"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(_DEADLINE_USAGE))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_DEADLINE_USAGE))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK write failure facts",
    )
    store.usage_write_fault = "update_fact"
    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    reviews = int(completed.codex.reviews_completed)  # type: ignore[attr-defined]
    sends = send_counter.read_text(encoding="utf-8")
    attempts = _attempt_count(store, run_id)
    cursor_facts = [row for row in _facts(store, run_id) if str(row["provider"]) == "cursor"]
    assert len(cursor_facts) == 1
    fact = cursor_facts[0]
    assert str(fact["settlement_status"]) == "pending"
    assert fact["coverage"] is None
    assert str(fact["acknowledgement"]) == "pending"
    assert "usage_statistics_write_failed" in str(fact["diagnostic_codes"])
    assert "usage_source_debt" not in str(fact["diagnostic_codes"])
    assert "attempt_usage_evidence_unavailable" not in str(fact["diagnostic_codes"])
    source_attempts = _effect_attempt_ids(store, run_id, "cursor.run_turn")
    reconcile_attempts = _effect_attempt_ids(store, run_id, "cursor.reconcile_sdk_run")
    assert str(fact["logical_attempt_id"]) in source_attempts
    assert str(fact["publisher_attempt_id"]) in reconcile_attempts
    retained = [row for row in _observations(store, run_id) if str(row["provider"]) == "cursor"]
    assert len(retained) == 1
    _assert_counters(retained[0], _DEADLINE_USAGE, _DEADLINE_NORMALIZED)
    restarted = _restart_tick(git_repo, db, artifacts)
    restarted.run_once()
    recovered = [
        row for row in _observations(restarted.store, run_id) if str(row["provider"]) == "cursor"
    ]
    assert len(recovered) == 1
    assert str(recovered[0]["observation_id"]) == str(retained[0]["observation_id"])
    _assert_counters(recovered[0], _DEADLINE_USAGE, _DEADLINE_NORMALIZED)
    assert str(recovered[0]["logical_attempt_id"]) == str(fact["logical_attempt_id"])
    recovered_fact = next(
        row for row in _facts(restarted.store, run_id) if str(row["provider"]) == "cursor"
    )
    assert str(recovered_fact["settlement_status"]) == "settled"
    assert str(recovered_fact["coverage"]) in {"partial", "complete"}
    assert str(recovered_fact["acknowledgement"]) == "acknowledged"
    assert send_counter.read_text(encoding="utf-8") == sends
    assert _attempt_count(restarted.store, run_id) == attempts
    with restarted.store.begin_read() as conn:
        state, _, _ = restarted.store.load_validated_snapshot(conn, run_id)
    assert state.kind == "completed"
    assert int(state.codex.reviews_completed) == reviews  # type: ignore[attr-defined]


def test_f02_partial_codex_observation_recovers_from_authenticated_events(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _drop_one_bound_observation(monkeypatch, scheduler_paths["artifact_root"])
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[[_IMPLEMENTER_USAGE, _CORRECTION_USAGE]],
        review_usage=[_REVIEW_USAGE],
    )
    assert len(removed) == 1
    assert not removed[0].exists()
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    implementer = [row for row in _observations(store, run_id) if str(row["role"]) == "implementer"]
    assert len(implementer) == 2
    assert len({str(row["logical_attempt_id"]) for row in implementer}) == 1
    totals = sorted(
        json.loads(str(row["normalized_payload"]))["total_tokens"] for row in implementer
    )
    assert totals == [10, 110]
    by_total = {
        json.loads(str(row["normalized_payload"]))["total_tokens"]: row for row in implementer
    }
    _assert_counters(by_total[110], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    _assert_counters(by_total[10], _CORRECTION_USAGE, _CORRECTION_NORMALIZED)
    sources = {str(row["source_relative_path"]) for row in implementer}
    assert sources
    assert all(not path.startswith("usage/observations/") for path in sources)
    assert all(path.endswith(".jsonl") or "events" in path for path in sources)
    facts = [row for row in _facts(store, run_id) if str(row["role"]) == "implementer"]
    assert len(facts) == 1
    assert str(facts[0]["coverage"]) == "complete"
    assert str(facts[0]["settlement_status"]) == "settled"
    assert "usage_source_debt" not in str(facts[0]["diagnostic_codes"])


def test_f02_recovered_bindings_stay_partial_when_a_turn_omits_usage(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _drop_one_bound_observation(monkeypatch, scheduler_paths["artifact_root"])
    run_id, state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[[_IMPLEMENTER_USAGE, _CORRECTION_USAGE, None]],
        review_usage=[_REVIEW_USAGE],
    )
    assert state.kind == "completed"  # type: ignore[attr-defined]
    assert int(state.codex.reviews_completed) == 1  # type: ignore[attr-defined]
    assert len(removed) == 1
    assert not removed[0].exists()
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    implementer = [row for row in _observations(store, run_id) if str(row["role"]) == "implementer"]
    assert len(implementer) == 2
    assert len({str(row["logical_attempt_id"]) for row in implementer}) == 1
    assert len({str(row["observation_id"]) for row in implementer}) == 2
    by_total = {
        json.loads(str(row["normalized_payload"]))["total_tokens"]: row for row in implementer
    }
    assert sorted(by_total) == [10, 110]
    _assert_counters(by_total[110], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    _assert_counters(by_total[10], _CORRECTION_USAGE, _CORRECTION_NORMALIZED)
    facts = [row for row in _facts(store, run_id) if str(row["role"]) == "implementer"]
    assert len(facts) == 1
    assert str(facts[0]["settlement_status"]) == "settled"
    assert str(facts[0]["coverage"]) == "partial"
    codes = str(facts[0]["diagnostic_codes"])
    assert "missing_codex_usage_turn_3" in codes
    assert "usage_source_debt" not in codes
    reviewer = next(row for row in _facts(store, run_id) if str(row["role"]) == "reviewer")
    assert str(reviewer["settlement_status"]) == "settled"
    assert str(reviewer["coverage"]) == "complete"
    reviewer_rows = [row for row in _observations(store, run_id) if str(row["role"]) == "reviewer"]
    assert len(reviewer_rows) == 1
    _assert_counters(reviewer_rows[0], _REVIEW_USAGE, _REVIEW_NORMALIZED)


def test_f02_partial_source_loss_keeps_surviving_counters_as_debt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _drop_one_bound_observation(monkeypatch, scheduler_paths["artifact_root"])
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.usage_ingestion._historical_codex_observations",
        lambda **_kwargs: ([], False),
    )
    run_id, state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[[_IMPLEMENTER_USAGE, _CORRECTION_USAGE]],
        review_usage=[_REVIEW_USAGE],
    )
    assert state.kind == "completed"  # type: ignore[attr-defined]
    assert len(removed) == 1
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    implementer = [row for row in _observations(store, run_id) if str(row["role"]) == "implementer"]
    assert len(implementer) == 1
    total = json.loads(str(implementer[0]["normalized_payload"]))["total_tokens"]
    assert total in {10, 110}
    if total == 110:
        _assert_counters(implementer[0], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    else:
        _assert_counters(implementer[0], _CORRECTION_USAGE, _CORRECTION_NORMALIZED)
    facts = [row for row in _facts(store, run_id) if str(row["role"]) == "implementer"]
    assert len(facts) == 1
    assert str(facts[0]["settlement_status"]) == "debt"
    assert facts[0]["coverage"] is None
    assert "usage_source_debt" in str(facts[0]["diagnostic_codes"])
    assert str(facts[0]["coverage"]) != "partial"


def test_f03_commit_barrier_restarts_without_another_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    send_counter = tmp_path / "crash-implementer-sends"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "crash-barrier.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([_IMPLEMENTER_USAGE]),
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW_USAGE]))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    tick.store.usage_ingestion_barrier = "before_commit"
    crashed = False
    for _ in range(40):
        try:
            tick.run_once()
        except UsageIngestionBarrier as exc:
            assert exc.phase == "before_commit"
            crashed = True
            break
    assert crashed
    assert send_counter.read_text(encoding="utf-8") == "1"
    assert _observations(tick.store, run_id) == []
    with tick.store.begin_read() as conn:
        ingest = conn.execute(
            """
            SELECT attempt.ingested
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.implementer_bootstrap'
            """,
            (run_id,),
        ).fetchone()
    assert ingest is not None and int(ingest["ingested"]) == 0
    restarted = _restart_tick(
        git_repo,
        scheduler_paths["db_path"],
        scheduler_paths["artifact_root"],
    )
    state = _run_until(restarted, run_id, "completed")
    assert send_counter.read_text(encoding="utf-8") == "1"
    assert int(state.codex.reviews_completed) == 1  # type: ignore[attr-defined]
    implementer = [
        row for row in _observations(restarted.store, run_id) if str(row["role"]) == "implementer"
    ]
    assert len(implementer) == 1
    _assert_counters(implementer[0], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
    assert len(_effect_attempt_ids(restarted.store, run_id, "codex.implementer_bootstrap")) == 1
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert porcelain.count("crash-barrier.txt") == 1
    assert (git_repo / "crash-barrier.txt").read_text(encoding="utf-8").count("implementer") == 1


def test_f03_cursor_correction_persists_separate_sqlite_facts(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    send_counter = tmp_path / "correction-sends"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR_USAGE))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([_REVIEW_USAGE, _SECOND_REVIEW_USAGE]),
    )
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK correction facts",
    )
    state = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert int(state.codex.reviews_completed) == 2  # type: ignore[attr-defined]
    assert send_counter.read_text(encoding="utf-8") == "2"
    cursor = [row for row in _observations(store, run_id) if str(row["provider"]) == "cursor"]
    assert {(str(row["stage"]), str(row["logical_attempt_id"])) for row in cursor}.__len__() == 2
    assert {str(row["stage"]) for row in cursor} == {"initial", "correction"}
    assert len({str(row["logical_attempt_id"]) for row in cursor}) == 2
    for row in cursor:
        _assert_counters(row, _CURSOR_USAGE, _CURSOR_NORMALIZED)
    cursor_facts = [row for row in _facts(store, run_id) if str(row["provider"]) == "cursor"]
    assert {str(row["stage"]) for row in cursor_facts} == {"initial", "correction"}
    assert all(str(row["coverage"]) == "complete" for row in cursor_facts)
    reviewer = [row for row in _observations(store, run_id) if str(row["role"]) == "reviewer"]
    totals = sorted(json.loads(str(row["normalized_payload"]))["total_tokens"] for row in reviewer)
    assert totals == [15, 30]


def test_f03_quota_failure_does_not_invent_counters(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    quota = _InlineCodexBackend(["usage_limit"])
    quota_run = _submit(git_repo, scheduler_paths)
    start_run(quota_run, db_path=scheduler_paths["db_path"])
    quota_tick = _tick(git_repo, scheduler_paths, backend=quota, now_factory=clock)
    waiting = _drive_to_kind(quota_tick, scheduler_paths, quota_run, "waiting_codex_capacity")
    assert waiting.kind == "waiting_codex_capacity"  # type: ignore[attr-defined]
    store = quota_tick.store
    quota_facts = [row for row in _facts(store, quota_run) if str(row["role"]) == "implementer"]
    assert len(quota_facts) == 1
    assert str(quota_facts[0]["coverage"]) == "unknown"
    assert str(quota_facts[0]["terminal_status"]) == "failed"
    assert _observations(store, quota_run) == []
    assert quota.implementer_calls == 1
    _assert_no_raw_usage_text(store)


def test_f03_generic_failure_does_not_invent_counters(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    generic_clock = _Clock()
    generic = _InlineCodexBackend(["fail"])
    generic_run = _submit(git_repo, scheduler_paths)
    start_run(generic_run, db_path=scheduler_paths["db_path"])
    generic_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=generic,
        now_factory=generic_clock,
    )
    blocked = None
    for _ in range(40):
        generic_tick.run_once()
        with generic_tick.store.begin_read() as conn:
            blocked, _, _ = generic_tick.store.load_validated_snapshot(conn, generic_run)
        if blocked.kind == "blocked":
            break
    assert blocked is not None and blocked.kind == "blocked"
    generic_facts = [
        row for row in _facts(generic_tick.store, generic_run) if str(row["role"]) == "implementer"
    ]
    assert len(generic_facts) == 1
    assert str(generic_facts[0]["coverage"]) == "unknown"
    assert _observations(generic_tick.store, generic_run) == []
    assert generic.implementer_calls == 1
    _assert_no_raw_usage_text(generic_tick.store)


def test_f03_active_inference_cancellation_has_no_fabricated_counters(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=40))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    active = False
    for _ in range(20):
        tick.run_once()
        with tick.store.begin_read() as conn:
            row = conn.execute(
                """
                SELECT attempt.status
                FROM scheduler_attempts AS attempt
                JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
                WHERE attempt.run_id = ?
                  AND effect.effect_kind = 'codex.implementer_bootstrap'
                """,
                (run_id,),
            ).fetchone()
        if row is not None and str(row["status"]) in {"launching", "active"}:
            active = True
            break
    assert active
    attempts = _attempt_count(tick.store, run_id)
    result = SchedulerAbortService(
        tick.store,
        backend,
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).abort_run(run_id)
    assert result.state_kind == "aborted"
    for _ in range(8):
        tick.run_once()
    facts = [row for row in _facts(tick.store, run_id) if str(row["role"]) == "implementer"]
    assert len(facts) == 1
    assert str(facts[0]["terminal_status"]) == "cancelled"
    assert _observations(tick.store, run_id) == []
    assert _attempt_count(tick.store, run_id) == attempts
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
    assert state.kind == "aborted"


def test_f03_retry_ancestry_keeps_separate_logical_facts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW_USAGE]))
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"], success_usage=_IMPLEMENTER_USAGE)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    _drive_to_timeout(tick, scheduler_paths, run_id)
    ancestor_rows = [row for row in _facts(tick.store, run_id) if str(row["role"]) == "implementer"]
    assert len(ancestor_rows) == 1
    ancestor_id = str(ancestor_rows[0]["logical_attempt_id"])
    assert str(ancestor_rows[0]["coverage"]) == "unknown"
    assert _observations(tick.store, run_id) == []
    clock.advance(minutes=30)
    state = _run_until(tick, run_id, "completed")
    assert int(state.codex.reviews_completed) == 1  # type: ignore[attr-defined]
    assert backend.implementer_calls == 2
    facts = {
        str(row["logical_attempt_id"]): row
        for row in _facts(tick.store, run_id)
        if str(row["role"]) == "implementer"
    }
    assert len(facts) == 2
    assert str(facts[ancestor_id]["coverage"]) == "unknown"
    assert str(facts[ancestor_id]["terminal_status"]) == "failed"
    observations = [
        row for row in _observations(tick.store, run_id) if str(row["role"]) == "implementer"
    ]
    assert len(observations) == 1
    assert str(observations[0]["logical_attempt_id"]) != ancestor_id
    _assert_counters(observations[0], _IMPLEMENTER_USAGE, _IMPLEMENTER_NORMALIZED)
