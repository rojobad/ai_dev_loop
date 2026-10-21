"""Phase 27.5 standalone Codex implementer failure-recovery contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick as _sequence_tick,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    leaf_state,
    prepare,
    sequence_state,
)
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    IMPLEMENTER_ID,
    REVIEWER_ID,
    _run_until,
    _submit,
    _tick,
)
from tests.unit.scheduler.test_phase27_2_implementer_capacity import _run_to_blocked
from tests.unit.scheduler.test_phase27_4_implementer_timeout_retry import (
    _Clock,
    _drive_to_timeout,
    _InlineCodexBackend,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_budget_carry import authenticated_reviews_completed
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
    CodexImplementerFailureRecoveryService,
    load_implementer_recovery_record,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.recovery_ancestry import recovery_ancestor_edges
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {
                        "id": "gpt-5.6-sol",
                        "supported_reasoning_efforts": ["high", "xhigh"],
                    },
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _blocked(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    implementer_sequence: str,
    review_sequence: str = "no_findings",
    prompt: str | None = None,
    max_reviews: int = 4,
    edit_path: str | None = "phase27-5-recovered.txt",
) -> tuple[str, object]:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", implementer_sequence)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    if edit_path is None:
        monkeypatch.delenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", raising=False)
    else:
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", edit_path)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    run_id = _submit(repo, paths, max_reviews=max_reviews, prompt=prompt)
    start_run(run_id, db_path=paths["db_path"])
    blocked = _run_to_blocked(_tick(repo, paths), run_id)
    assert blocked.block_reason_kind == "codex_implementer_failure"
    return run_id, blocked


def _service(
    paths: dict[str, Path],
    *,
    fault_point: str | None = None,
    successor_run_id: str = "phase27-5-successor",
):  # type: ignore[no-untyped-def]
    return CodexImplementerFailureRecoveryService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        now_factory=lambda: NOW,
        run_id_factory=lambda _project, _now: successor_run_id,
        fault_point=fault_point,
    )


def _relation(paths: dict[str, Path], source: str) -> sqlite3.Row:
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT * FROM scheduler_codex_implementer_recoveries WHERE source_run_id=?",
            (source,),
        ).fetchone()
    assert row is not None
    return row


def _authority_witness(paths: dict[str, Path]) -> tuple[object, object]:
    tables = (
        "scheduler_runs",
        "scheduler_events",
        "scheduler_effects",
        "scheduler_attempts",
        "scheduler_repository_reservations",
        "scheduler_codex_implementer_recoveries",
    )
    with sqlite3.connect(paths["db_path"]) as conn:
        ledger = tuple(
            (table, tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid")))
            for table in tables
        )
    root = paths["artifact_root"]
    artifacts = tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    return ledger, artifacts


def _exhaust_automatic_timeout_retries_into_generic_failure(
    tick: object,
    paths: dict[str, Path],
    run_id: str,
    clock: _Clock,
) -> object:
    for generation in (1, 2, 3):
        state = _drive_to_timeout(tick, paths, run_id, generation)  # type: ignore[arg-type]
        continuation = state.codex.implementer_timeout_continuation
        assert continuation.automatic_retries == generation
        assert continuation.scheduled_generation == generation
        clock.advance(minutes=30)
    blocked = _run_to_blocked(tick, run_id)  # type: ignore[arg-type]
    assert blocked.block_reason_kind == "codex_implementer_failure"
    return blocked


def test_current_schema_and_real_cli_initial_recovery_preserve_prompt_identity_and_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    witness = scheduler_paths["db_path"].parent / "stdin-witness"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    source, blocked = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,success",
        prompt=prompt,
    )
    source_payload = blocked.model_dump_json()

    import ai_dev_loop.scheduler.application.implementer_timeout_retry as retry_module

    production_retry = ImplementerRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: NOW,
    )
    monkeypatch.setattr(
        retry_module,
        "scheduler_implementer_retry",
        lambda requested_run_id, force=False: production_retry.retry(
            requested_run_id,
            force=force,
        ),
    )

    result = CliRunner().invoke(
        app,
        ["scheduler", "implementer-retry", source, "--force", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    successor = json.loads(result.stdout)["run_id"]
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )

    assert SCHEMA_VERSION == 19
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert [path.read_bytes() for path in sorted(witness.glob("*.bin"))] == [
        prompt.encode(),
        prompt.encode(),
    ]
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        current, _, _ = store.load_validated_snapshot(conn, source)
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
        created_row = conn.execute(
            "SELECT event_payload FROM scheduler_events WHERE run_id=? "
            "AND event_kind='codex_implementer_recovery_successor_created'",
            (successor,),
        ).fetchone()
        assert store.get_reservation_for_run(conn, source) is None
        assert store.get_reservation_for_run(conn, successor) is None
    assert current.model_dump_json() == source_payload
    assert record is not None
    assert record.turn_kind == "initial"
    assert record.implementer.session_id == IMPLEMENTER_ID
    assert record.reviewer.form == "not_created"
    assert record.exact_prompt.sha256 == hashlib.sha256(prompt.encode()).hexdigest()
    assert record.plan.owner_run_id == source
    assert record.effective_config.owner_run_id == source
    assert record.source_config.owner_run_id == source
    assert record.partial_work_status.owner_run_id == source
    assert record.timeout_automatic_retries_consumed == 0
    assert b'"timeout_automatic_retries_consumed"' not in record.canonical_bytes()
    jsonschema.validate(
        record.model_dump(mode="json"),
        json.loads(schema_path("scheduler-codex-implementer-recovery-record-v1.json").read_text()),
    )
    assert created_row is not None
    jsonschema.validate(
        json.loads(str(created_row["event_payload"])),
        json.loads(
            schema_path("scheduler-codex-implementer-recovery-successor-event-v1.json").read_text()
        ),
    )
    replay = ImplementerRetryService(store, artifacts, now_factory=lambda: NOW).retry(
        source,
        force=True,
    )
    assert replay.run_id == successor and replay.changed is False


def test_recovered_initial_turn_releases_later_correction_to_normal_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "initial-then-correction-witness"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", "Correct the recovered initial turn exactly.\n")
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,success,success",
        review_sequence="findings,no_findings",
    )
    successor, changed = _service(scheduler_paths).force(source)
    assert changed is True
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert completed.codex.reviews_completed == 2
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[0] == prompts[1]
    assert prompts[2].endswith(b"Correct the recovered initial turn exactly.\n")


def test_recovered_correction_releases_further_correction_and_can_recover_again(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "successive-correction-witness"
    fix = "Preserve exact correction bytes λ.\n"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="success,fail,success,fail,success",
        review_sequence="findings,findings,no_findings",
    )
    first_successor, _ = _service(scheduler_paths).force(source)
    second_block = _run_to_blocked(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        first_successor,
    )
    assert second_block.block_reason_kind == "codex_implementer_failure"
    second_successor, changed = _service(
        scheduler_paths,
        successor_run_id="phase27-5-successor-2",
    ).force(first_successor)
    assert changed is True
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        second_successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert completed.codex.reviews_completed == 3
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        second_record = load_implementer_recovery_record(
            store,
            artifacts,
            conn,
            second_successor,
        )
    assert second_record is not None
    assert second_record.turn_kind == "correction"
    assert second_record.iteration == 3
    assert second_record.reviews_completed == 2
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[-1] == prompts[-2]
    assert prompts[-1].endswith(fix.encode())


def test_recovered_turn_capacity_continuation_uses_durable_prompt_and_identity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "recovery-capacity-witness"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,usage_limit,success",
    )
    successor, _ = _service(scheduler_paths).force(source)
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert len(prompts) == 3
    assert prompts[0] == prompts[1] == prompts[2]


def test_recovered_initial_turn_runs_scheduled_timeout_retry_after_restart_and_capacity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail",
    )
    successor, _ = _service(scheduler_paths).force(source)
    clock = _Clock()
    clock.current = NOW
    timed_out_backend = _InlineCodexBackend(["timeout"])
    timed = _drive_to_timeout(
        _tick(
            git_repo,
            scheduler_paths,
            backend=timed_out_backend,
            now_factory=clock,
        ),
        scheduler_paths,
        successor,
    )
    continuation = timed.codex.implementer_timeout_continuation
    assert continuation.automatic_retries == 1
    assert continuation.scheduled_generation == 1
    assert continuation.wait_until is not None
    clock.advance(minutes=30)
    resumed_backend = _InlineCodexBackend(["usage_limit", "success"])
    completed = _run_until(
        _tick(
            git_repo,
            scheduler_paths,
            backend=resumed_backend,
            owner="tick-recovered-automatic-restart",
            now_factory=clock,
        ),
        successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert resumed_backend.prompt_bytes == timed_out_backend.prompt_bytes * 2
    assert all("resume" in argv for argv in resumed_backend.provider_argv)
    assert all(IMPLEMENTER_ID in argv for argv in resumed_backend.provider_argv)


def test_recovered_correction_runs_scheduled_retry_from_partial_inherited_budget(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = "Continue the partially consumed correction exactly.\n"
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    clock = _Clock()
    clock.current = NOW
    source_backend = _InlineCodexBackend(["success", "timeout", "fail"])
    source = _submit(git_repo, scheduler_paths, max_reviews=3)
    start_run(source, db_path=scheduler_paths["db_path"])
    source_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=source_backend,
        owner="tick-partial-source",
        now_factory=clock,
    )
    source_timeout = _drive_to_timeout(source_tick, scheduler_paths, source)
    assert source_timeout.cursor.iteration == 2
    assert source_timeout.codex.implementer_timeout_continuation.automatic_retries == 1
    clock.advance(minutes=30)
    blocked = _run_to_blocked(source_tick, source)
    assert blocked.block_reason_kind == "codex_implementer_failure"

    successor, _ = _service(scheduler_paths).force(source)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
    assert record is not None
    assert record.turn_kind == "correction"
    assert record.timeout_automatic_retries_consumed == 1

    successor_timeout_backend = _InlineCodexBackend(["timeout"])
    successor_timeout = _drive_to_timeout(
        _tick(
            git_repo,
            scheduler_paths,
            backend=successor_timeout_backend,
            owner="tick-partial-successor-timeout",
            now_factory=clock,
        ),
        scheduler_paths,
        successor,
    )
    successor_continuation = successor_timeout.codex.implementer_timeout_continuation
    assert successor_continuation.automatic_retries == 2
    assert successor_continuation.scheduled_generation == 1
    assert successor_continuation.wait_until is not None

    clock.advance(minutes=30)
    resumed_backend = _InlineCodexBackend(["success"])
    completed = _run_until(
        _tick(
            git_repo,
            scheduler_paths,
            backend=resumed_backend,
            owner="tick-partial-successor-restart",
            now_factory=clock,
        ),
        successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert completed.codex.implementer_timeout_automatic_retries_consumed == 2
    assert successor_timeout_backend.prompt_bytes == resumed_backend.prompt_bytes
    assert resumed_backend.prompt_bytes[0].endswith(fix.encode())


def test_initial_recovery_preserves_exhausted_timeout_policy_and_manual_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    clock = _Clock()
    clock.current = NOW
    source_backend = _InlineCodexBackend(["timeout", "timeout", "timeout", "fail"])
    source = _submit(git_repo, scheduler_paths, max_reviews=3, prompt=prompt)
    start_run(source, db_path=scheduler_paths["db_path"])
    blocked = _exhaust_automatic_timeout_retries_into_generic_failure(
        _tick(git_repo, scheduler_paths, backend=source_backend, now_factory=clock),
        scheduler_paths,
        source,
        clock,
    )
    source_payload = blocked.model_dump_json()

    successor, changed = _service(scheduler_paths).force(source)
    assert changed is True
    assert _service(scheduler_paths).force(source) == (successor, False)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
        unchanged, _, _ = store.load_validated_snapshot(conn, source)
    assert record is not None
    assert record.timeout_automatic_retries_consumed == 3
    assert unchanged.model_dump_json() == source_payload

    exhausted_backend = _InlineCodexBackend(["timeout"])
    exhausted = _drive_to_timeout(
        _tick(
            git_repo,
            scheduler_paths,
            backend=exhausted_backend,
            owner="tick-recovered-timeout",
            now_factory=clock,
        ),
        scheduler_paths,
        successor,
    )
    continuation = exhausted.codex.implementer_timeout_continuation
    assert continuation.automatic_retries == 3
    assert continuation.scheduled_generation is None
    assert continuation.wait_until is None
    with store.begin_read() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=? "
            "AND effect_kind='codex.implementer_resume' AND status IN ('pending','claimed')",
            (successor,),
        ).fetchone()
    assert live is not None and live[0] == 0

    manual = ImplementerRetryService(store, artifacts, now_factory=clock).retry(successor)
    assert manual.changed is True
    assert manual.automatic_retries == 3
    repeated_failure_backend = _InlineCodexBackend(["fail"])
    repeated_block = _run_to_blocked(
        _tick(
            git_repo,
            scheduler_paths,
            backend=repeated_failure_backend,
            owner="tick-recovered-manual-failure",
            now_factory=clock,
        ),
        successor,
    )
    assert repeated_block.block_reason_kind == "codex_implementer_failure"
    repeated_successor, repeated_changed = _service(
        scheduler_paths,
        successor_run_id="phase27-5-successor-2",
    ).force(successor)
    assert repeated_changed is True
    with store.begin_read() as conn:
        repeated_record = load_implementer_recovery_record(
            store,
            artifacts,
            conn,
            repeated_successor,
        )
    assert repeated_record is not None
    assert repeated_record.timeout_automatic_retries_consumed == 3

    repeated_timeout_backend = _InlineCodexBackend(["timeout"])
    repeated_timeout = _drive_to_timeout(
        _tick(
            git_repo,
            scheduler_paths,
            backend=repeated_timeout_backend,
            owner="tick-repeated-successor-timeout",
            now_factory=clock,
        ),
        scheduler_paths,
        repeated_successor,
    )
    assert repeated_timeout.codex.implementer_timeout_continuation.automatic_retries == 3
    assert repeated_timeout.codex.implementer_timeout_continuation.wait_until is None
    repeated_manual = ImplementerRetryService(store, artifacts, now_factory=clock).retry(
        repeated_successor
    )
    assert repeated_manual.changed is True
    assert repeated_manual.automatic_retries == 3
    completed_backend = _InlineCodexBackend(["success"])
    completed = _run_until(
        _tick(
            git_repo,
            scheduler_paths,
            backend=completed_backend,
            owner="tick-recovered-manual",
            now_factory=clock,
        ),
        repeated_successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.implementer_timeout_automatic_retries_consumed == 3
    assert source_backend.prompt_bytes == [prompt.encode()] * 4
    assert (
        exhausted_backend.prompt_bytes == repeated_failure_backend.prompt_bytes == [prompt.encode()]
    )
    assert (
        repeated_timeout_backend.prompt_bytes == completed_backend.prompt_bytes == [prompt.encode()]
    )


def test_correction_recovery_preserves_timeout_policy_then_new_correction_resets_it(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = "Keep timeout recovery correction bytes exact λ.\n"
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    clock = _Clock()
    clock.current = NOW
    source_backend = _InlineCodexBackend(["success", "timeout", "timeout", "timeout", "fail"])
    source = _submit(git_repo, scheduler_paths, max_reviews=4)
    start_run(source, db_path=scheduler_paths["db_path"])
    blocked = _exhaust_automatic_timeout_retries_into_generic_failure(
        _tick(git_repo, scheduler_paths, backend=source_backend, now_factory=clock),
        scheduler_paths,
        source,
        clock,
    )
    assert blocked.block_reason_kind == "codex_implementer_failure"

    successor, _ = _service(scheduler_paths).force(source)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
    assert record is not None
    assert record.turn_kind == "correction"
    assert record.timeout_automatic_retries_consumed == 3

    recovered_timeout_backend = _InlineCodexBackend(["timeout"])
    recovered_timeout = _drive_to_timeout(
        _tick(
            git_repo,
            scheduler_paths,
            backend=recovered_timeout_backend,
            owner="tick-correction-recovered-timeout",
            now_factory=clock,
        ),
        scheduler_paths,
        successor,
    )
    assert recovered_timeout.cursor.iteration == 2
    assert recovered_timeout.codex.implementer_timeout_continuation.automatic_retries == 3
    assert recovered_timeout.codex.implementer_timeout_continuation.wait_until is None

    manual = ImplementerRetryService(store, artifacts, now_factory=clock).retry(successor)
    assert manual.changed is True and manual.automatic_retries == 3
    later_backend = _InlineCodexBackend(["success", "timeout", "success"])
    later_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=later_backend,
        owner="tick-new-correction",
        now_factory=clock,
    )
    for _ in range(100):
        later_tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, successor)
        codex = getattr(state, "codex", None)
        continuation = codex.implementer_timeout_continuation if codex is not None else None
        if continuation is not None and continuation.iteration == 3:
            break
        assert state.kind != "blocked"
    else:
        raise AssertionError("new correction did not reach its first timeout")
    assert continuation.turn == "correction"
    assert continuation.automatic_retries == 1
    assert continuation.scheduled_generation == 1
    clock.advance(minutes=30)
    completed = _run_until(later_tick, successor, "completed")
    assert completed.codex.reviews_completed == 3
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.implementer_timeout_automatic_retries_consumed == 1
    assert recovered_timeout_backend.prompt_bytes[0] == later_backend.prompt_bytes[0]
    assert later_backend.prompt_bytes[1].endswith(fix.encode())


def test_v14_migration_adds_relation_without_rewriting_existing_runs(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    store.bootstrap()
    with store.begin_immediate() as conn:
        before = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_retention_policy")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_conversation_fences")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_disposals")
        conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
        conn.execute("DROP TABLE scheduler_planned_run_presentation")
        conn.execute("DROP TABLE scheduler_run_presentation")
        conn.execute("DROP TABLE scheduler_sequence_presentation")
        conn.execute("DROP TABLE scheduler_sequence_implementer_replacements")
        conn.execute(
            "DROP INDEX IF EXISTS idx_scheduler_sequence_implementer_replacements_sequence"
        )
        conn.execute("DROP TABLE scheduler_codex_implementer_recoveries")
        conn.execute("DROP INDEX IF EXISTS idx_scheduler_codex_implementer_recoveries_successor")
        conn.execute("DROP INDEX IF EXISTS idx_scheduler_codex_implementer_recoveries_status")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 15")
        conn.execute("PRAGMA user_version = 14")
    historical = SqliteSchedulerStore.open_readonly(db)
    with historical.begin_read() as conn:
        assert recovery_ancestor_edges(historical, conn, "historical-run") == ()
    store.bootstrap()
    with store.begin_read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == before
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
                ).fetchone()[0]
            )
            == 0
        )


def test_correction_recovery_keeps_existing_b_budget_patch_and_exact_fix_bytes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "correction-witness"
    fix = "Fix issue Ω exactly; do not normalize.\n"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="success,fail,success",
        review_sequence="findings,no_findings",
    )
    successor, changed = _service(scheduler_paths).force(source)
    assert changed
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
    assert record is not None
    assert record.turn_kind == "correction"
    assert record.reviewer.form == "bound"
    assert record.reviewer.session_id == REVIEWER_ID
    assert record.reviews_completed == 1
    assert record.effective_review_ceiling == 4
    assert record.raw_fix is not None and record.staged_patch is not None
    assert record.review_result is not None
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[-1] == prompts[-2]
    assert prompts[-1].endswith(fix.encode())


def test_extended_codex_budget_routes_correction_then_recovers_with_inherited_ceiling(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "extension-recovery-witness"
    fix = "Apply the post-extension correction byte-for-byte Ω.\n"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,fail,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "extended-recovery.txt")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    source = _submit(git_repo, scheduler_paths, max_reviews=1)
    start_run(source, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, now_factory=lambda: NOW)
    exhausted = _run_until(tick, source, "max_iterations_reached")
    assert exhausted.context.workflow.max_review_iterations == 1
    assert exhausted.codex.reviews_completed == 1
    assert exhausted.codex.reviewer_session_id == REVIEWER_ID

    extended = ReviewBudgetExtendService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).extend(source, target_total=2)
    assert extended.changed is True
    assert extended.previous_effective_total == 1
    assert extended.new_effective_total == 2
    with tick.store.begin_read() as conn:
        effect = conn.execute(
            """
            SELECT effects.effect_kind, effects.effect_payload
            FROM scheduler_effects AS effects
            JOIN scheduler_events AS events ON events.event_id = effects.source_event_id
            WHERE effects.run_id = ?
              AND events.event_kind = 'review_budget_extended'
            """,
            (source,),
        ).fetchone()
    assert effect is not None
    assert str(effect["effect_kind"]) == "codex.implementer_resume"
    assert json.loads(str(effect["effect_payload"]))["turn"] == "correction"

    failed = _run_to_blocked(tick, source)
    assert failed.block_reason_kind == "codex_implementer_failure"
    assert failed.context.workflow.max_review_iterations == 1
    successor, changed = _service(scheduler_paths).force(source)
    assert changed is True
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )
    assert completed.context.workflow.max_review_iterations == 1
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
        reviews_completed = authenticated_reviews_completed(
            store,
            artifacts,
            conn,
            successor,
        )
    assert record is not None
    assert record.reviews_completed == 1
    assert record.submitted_max_review_iterations == 1
    assert record.effective_review_ceiling == 2
    assert reviews_completed == 2
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[-1] == prompts[-2]
    assert prompts[-1].endswith(fix.encode())


def test_duplicate_concurrent_force_and_failed_successor_make_one_relation_each(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,fail,success",
    )
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def force() -> None:
        try:
            barrier.wait(timeout=5)
            service = CodexImplementerFailureRecoveryService(
                SqliteSchedulerStore(scheduler_paths["db_path"]),
                ProtectedArtifactStore(scheduler_paths["artifact_root"]),
                now_factory=lambda: NOW,
            )
            results.append(service.force(source)[0])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=force) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    assert len(set(results)) == 1
    first = results[0]
    failed = _run_to_blocked(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        first,
    )
    assert failed.block_reason_kind == "codex_implementer_failure"
    second = CodexImplementerFailureRecoveryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: NOW,
    ).force(first)[0]
    assert second != first
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        first_record = load_implementer_recovery_record(store, artifacts, conn, first)
        second_record = load_implementer_recovery_record(store, artifacts, conn, second)
    assert first_record is not None and second_record is not None
    assert second_record.parent_source_run_id == first_record.source_run_id == source
    assert second_record.parent_recovery_key == first_record.recovery_key
    assert (
        second_record.parent_record_sha256
        == hashlib.sha256(first_record.canonical_bytes()).hexdigest()
    )
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        second,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
        ).fetchone() == (2,)


@pytest.mark.parametrize("fault_point", ["after_candidate", "after_artifacts", "after_ready"])
def test_publication_fault_restart_converges_without_early_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,success",
    )
    with pytest.raises(OSError, match="injected implementer recovery fault"):
        _service(scheduler_paths, fault_point=fault_point).force(source)
    row = _relation(scheduler_paths, source)
    successor = str(row["successor_run_id"])
    if str(row["status"]) == "pending":
        with sqlite3.connect(scheduler_paths["db_path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=?",
                (successor,),
            ).fetchone() == (0,)
    _tick(git_repo, scheduler_paths, now_factory=lambda: NOW).run_once()
    assert str(_relation(scheduler_paths, source)["status"]) == "ready"
    replay, _ = _service(scheduler_paths).force(source)
    assert replay == successor
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=?",
            (successor,),
        ).fetchone() == (1,)


def test_empty_status_publication_is_protected_idempotent_and_tick_converges(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, blocked = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,success",
        edit_path=None,
    )
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    source_root = artifacts.run_root(source)
    source_before = tuple(
        (path.relative_to(source_root).as_posix(), path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    )
    with pytest.raises(OSError, match="after_artifacts"):
        _service(scheduler_paths, fault_point="after_artifacts").force(source)
    row = _relation(scheduler_paths, source)
    successor = str(row["successor_run_id"])
    assert str(row["status"]) == "pending"
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, source) is None
        held = store.get_reservation_for_run(conn, successor)
        assert held is not None and str(held["status"]) == "active"

    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "empty-status-recovery.txt")
    completed = _run_until(
        _tick(git_repo, scheduler_paths, now_factory=lambda: NOW),
        successor,
        "completed",
    )
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
        source_state, _, _ = store.load_validated_snapshot(conn, source)
    assert source_state.model_dump_json() == blocked.model_dump_json()
    assert record is not None
    empty_sha = hashlib.sha256(b"").hexdigest()
    assert record.partial_work_status.sha256 == empty_sha
    source_status = source_root / record.partial_work_status.relative_path
    successor_status = artifacts.run_root(successor) / record.partial_work_status.relative_path
    assert source_status.read_bytes() == successor_status.read_bytes() == b""
    if os.name != "nt":
        assert successor_status.stat().st_mode & 0o777 == 0o600
    assert source_before == tuple(
        (path.relative_to(source_root).as_posix(), path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    )
    replay, changed = _service(scheduler_paths).force(source)
    assert replay == successor and changed is False


def test_abort_pending_publication_cancels_without_dispatch_and_preserves_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, blocked = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,success",
    )
    source_payload = blocked.model_dump_json()
    with pytest.raises(OSError, match="after_candidate"):
        _service(scheduler_paths, fault_point="after_candidate").force(source)
    row = _relation(scheduler_paths, source)
    successor = str(row["successor_run_id"])
    aborted = SchedulerAbortService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        FakeAgentProcessBackend(),
        now_factory=lambda: NOW,
    ).abort_run(successor)
    assert aborted.state_kind == "aborted"
    with pytest.raises(SchedulerEngineError, match="aborted before publication"):
        _service(scheduler_paths).force(source)
    aborted_witness = _authority_witness(scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="blocked run"):
        _service(scheduler_paths).force(successor)
    assert _authority_witness(scheduler_paths) == aborted_witness
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT status FROM scheduler_codex_implementer_recoveries WHERE source_run_id=?",
            (source,),
        ).fetchone() == ("cancelled",)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=?",
            (successor,),
        ).fetchone() == (0,)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        current, _, _ = store.load_validated_snapshot(conn, source)
    assert current.model_dump_json() == source_payload


def test_sequence_leaf_force_adopts_successor_and_preserves_blocked_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "fail")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "sequence-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    prepared = prepare(git_repo, scheduler_paths, ("codex", "codex"))
    started = start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _sequence_tick(git_repo, scheduler_paths)
    for _ in range(50):
        tick.run_once()
        leaf = leaf_state(scheduler_paths, started.run_id)
        if leaf.kind == "blocked":
            break
    else:
        raise AssertionError("sequence leaf did not reach generic implementer block")
    assert leaf.block_reason_kind == "codex_implementer_failure"
    source_payload = leaf.model_dump_json()
    successor, changed = _service(scheduler_paths).force(started.run_id)
    assert changed is True
    sequence = sequence_state(scheduler_paths, prepared.sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_ordinal == 1
    assert sequence.current_run_id == successor
    assert leaf_state(scheduler_paths, started.run_id).model_dump_json() == source_payload
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_implementer_replacements "
            "WHERE adopted_at IS NOT NULL"
        ).fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone() == (2,)


def test_corrupt_failure_rejects_without_successor_publication(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail",
    )
    session = (
        ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(source)
        / "implementer/session.json"
    )
    session.chmod(0o600)
    session.write_text("{}\n", encoding="utf-8")
    session.chmod(0o600)
    witness = _authority_witness(scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="identity artifact"):
        _service(scheduler_paths).force(source)
    assert _authority_witness(scheduler_paths) == witness
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
        ).fetchone() == (0,)


def test_unbound_failure_rejects_without_successor_publication(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production ingest path classifies identity uncertainty before force
    # recovery can treat the failure as generic.
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "conflicting_identity")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    other = _submit(git_repo, scheduler_paths)
    start_run(other, db_path=scheduler_paths["db_path"])
    unbound = _run_to_blocked(_tick(git_repo, scheduler_paths), other)
    assert unbound.block_reason_kind == "codex_implementer_identity_uncertain"
    witness = _authority_witness(scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="not an eligible generic"):
        _service(scheduler_paths).force(other)
    assert _authority_witness(scheduler_paths) == witness
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
        ).fetchone() == (0,)


def test_capacity_wait_force_rejection_is_mutation_free(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A capacity wait is a real production path and must stay on its same-run policy.
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "fail")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    waiting = _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    before = waiting.model_dump_json()
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        counts = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("scheduler_runs", "scheduler_codex_implementer_recoveries")
        )
    witness = _authority_witness(scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="blocked run"):
        _service(scheduler_paths).force(run_id)
    assert _authority_witness(scheduler_paths) == witness
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        current, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn, run_id
        )
    assert current.model_dump_json() == before
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert counts == tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("scheduler_runs", "scheduler_codex_implementer_recoveries")
        )


def test_partial_work_content_drift_rejects_before_publication_without_claiming_reservation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _blocked(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail",
    )
    partial = git_repo / "phase27-5-recovered.txt"
    assert partial.is_file()
    partial.write_text("operator changed the same untracked path\n", encoding="utf-8")
    witness = _authority_witness(scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="partial work changed after"):
        _service(scheduler_paths).force(source)
    assert _authority_witness(scheduler_paths) == witness
