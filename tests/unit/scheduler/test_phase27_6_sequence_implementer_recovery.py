"""Phase 27.6 current-leaf Codex implementer recovery contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick,
    leaf_state,
    manifest_payload,
    prepare,
    sequence_state,
)
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    IMPLEMENTER_ID,
    REVIEWER_ID,
)

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.process_output_service import (
    IntegrationProcessOutputReadService,
)
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService
from ai_dev_loop.integration_api.sequence_service import IntegrationSequenceReadService
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
    CodexImplementerFailureRecoveryService,
    load_implementer_recovery_record,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.presentation_metadata import set_run_short_name
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore

NOW = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _drive_sequence_leaf_to(
    tick: object,
    paths: dict[str, Path],
    sequence_id: str,
    target: str,
    *,
    limit: int = 120,
) -> object:
    for _ in range(limit):
        tick.run_once()  # type: ignore[attr-defined]
        sequence = sequence_state(paths, sequence_id)
        leaf = leaf_state(paths, sequence.current_run_id)
        if leaf.kind == target:
            return leaf
    raise AssertionError(
        f"current sequence leaf did not reach {target}: "
        f"sequence={type(sequence).__name__} leaf={leaf.kind} run={leaf.run_id}"
    )


def _drive_sequence_final(
    tick: object,
    paths: dict[str, Path],
    sequence_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int = 220,
) -> object:
    for _ in range(limit):
        sequence = sequence_state(paths, sequence_id)
        if isinstance(sequence, AwaitingFinalizationSequenceState):
            return sequence
        ordinal = sequence.current_ordinal
        monkeypatch.setenv(
            "FAKE_CODEX_REVIEWER_SESSION_ID",
            REVIEWER_ID if ordinal == 1 else f"019def00-0000-0000-0001-{ordinal:012x}",
        )
        tick.run_once()  # type: ignore[attr-defined]
    raise AssertionError("sequence did not reach awaiting_finalization")


def _blocked_codex_leaf(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    implementer_sequence: str = "fail,success",
    review_sequence: str = "no_findings,no_findings",
    payload: dict[str, object] | None = None,
) -> tuple[object, object, object]:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", implementer_sequence)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "phase27-6-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    prepared = prepare(repo, paths, ("codex", "codex"), payload=payload)
    started = start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    tick = _tick(repo, paths)
    blocked = _drive_sequence_leaf_to(tick, paths, prepared.sequence_id, "blocked")
    assert blocked.run_id == started.run_id
    assert blocked.block_reason_kind == "codex_implementer_failure"
    return prepared, tick, blocked


def _blocked_codex_correction_leaf(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, object, object]:
    payload = manifest_payload(("codex", "codex"))
    payload["phases"][0]["workflow"] = {"max_review_iterations": 1}
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,fail,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "phase27-6-race.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    prepared = prepare(repo, paths, ("codex", "codex"), payload=payload)
    started = start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    tick = _tick(repo, paths)
    _drive_sequence_leaf_to(tick, paths, prepared.sequence_id, "max_iterations_reached")
    ReviewBudgetExtendService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).extend(started.run_id, target_total=2)
    blocked = _drive_sequence_leaf_to(tick, paths, prepared.sequence_id, "blocked")
    assert blocked.block_reason_kind == "codex_implementer_failure"
    return prepared, tick, blocked


def _tick_with_claim_hook(
    repo: Path,
    paths: dict[str, Path],
    *,
    backend: FakeAgentProcessBackend,
    before_effect_claim: object,
) -> TickService:
    return TickService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        BoundedGitAdmissionPort(),
        now_factory=lambda: NOW,
        tick_owner_factory=lambda: "phase27-6-abort-race",
        attempt_backend=backend,
        before_effect_claim=before_effect_claim,  # type: ignore[arg-type]
    )


def test_schema_v16_intent_and_initial_recovery_continue_checkpoint_and_final_stage(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "sequence-recovery-prompts"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    blocked_payload = blocked.model_dump_json()
    head = _git(git_repo, "rev-parse", "HEAD")
    set_run_short_name(
        blocked.run_id,
        "Edited retry source",
        db_path=scheduler_paths["db_path"],
    )
    result = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(
        blocked.run_id, force=True
    )
    assert result.provider == "codex" and result.changed

    adopted = sequence_state(scheduler_paths, prepared.sequence_id)
    assert isinstance(adopted, ActiveSequenceState)
    assert adopted.current_ordinal == 1
    assert adopted.current_run_id == result.run_id
    assert leaf_state(scheduler_paths, blocked.run_id).model_dump_json() == blocked_payload
    successor = leaf_state(scheduler_paths, result.run_id)
    assert successor.context == blocked.context
    assert successor.codex.implementer_session_id == IMPLEMENTER_ID

    with tick.store.begin_read() as conn:
        row = tick.store.get_sequence_implementer_replacement_by_successor(conn, result.run_id)
        lineage = load_sequence_run_lineage(
            conn, prepared.sequence_id, definition=adopted.definition
        )
        record = load_implementer_recovery_record(tick.store, tick.artifacts, conn, result.run_id)
        assert tick.store.run_short_name(conn, blocked.run_id) == "Edited retry source"
        assert tick.store.run_short_name(conn, result.run_id) == "Edited retry source"
    assert SCHEMA_VERSION == 18
    assert row is not None and row["adopted_at"] is not None and row["cancelled_at"] is None
    assert [attempt.attempt_kind for attempt in lineage.phase_executions[0].attempts] == [
        "planned_run",
        "implementer_retry",
    ]
    assert record is not None
    assert record.turn_kind == "initial"
    assert record.exact_prompt.sha256 == blocked.context.plan_prompt.prompt_sha256
    schema = json.loads(
        schema_path("scheduler-sequence-implementer-replacement-intent-v1.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(json.loads(str(row["intent_payload"])))

    set_run_short_name(
        result.run_id,
        "Edited retry successor",
        db_path=scheduler_paths["db_path"],
    )
    replay = ImplementerRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    assert replay.run_id == result.run_id and not replay.changed
    with tick.store.begin_read() as conn:
        assert tick.store.run_short_name(conn, result.run_id) == "Edited retry successor"

    final = _drive_sequence_final(
        _tick(git_repo, scheduler_paths),
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    assert isinstance(final, AwaitingFinalizationSequenceState)
    assert _git(git_repo, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert _git(git_repo, "diff", "--cached", "--name-only")
    assert not _git(git_repo, "diff", "--name-only")
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[0] == prompts[1]
    with tick.store.begin_read() as conn:
        historical = load_implementer_recovery_record(
            tick.store,
            tick.artifacts,
            conn,
            result.run_id,
        )
    assert historical is not None and historical.recovery_key == record.recovery_key


def test_correction_recovery_preserves_reviewer_fix_and_extended_budget(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = "Preserve this exact sequence correction Ω.\r\nSecond line.\n"
    witness = scheduler_paths["db_path"].parent / "sequence-correction-prompts"
    payload = manifest_payload(("codex", "codex"))
    payload["phases"][0]["workflow"] = {"max_review_iterations": 1}
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", fix)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,fail,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "phase27-6-correction.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    prepared = prepare(git_repo, scheduler_paths, ("codex", "codex"), payload=payload)
    started = start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    exhausted = _drive_sequence_leaf_to(
        tick, scheduler_paths, prepared.sequence_id, "max_iterations_reached"
    )
    assert exhausted.run_id == started.run_id
    extended = ReviewBudgetExtendService(
        tick.store, tick.artifacts, now_factory=lambda: NOW
    ).extend(started.run_id, target_total=2)
    assert extended.changed and extended.new_effective_total == 2
    failed = _drive_sequence_leaf_to(tick, scheduler_paths, prepared.sequence_id, "blocked")
    assert failed.block_reason_kind == "codex_implementer_failure"

    recovered = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(
        failed.run_id, force=True
    )
    with tick.store.begin_read() as conn:
        record = load_implementer_recovery_record(
            tick.store, tick.artifacts, conn, recovered.run_id
        )
    assert record is not None
    assert record.turn_kind == "correction"
    assert record.reviewer.session_id == REVIEWER_ID  # type: ignore[union-attr]
    assert record.reviews_completed == 1
    assert record.submitted_max_review_iterations == 1
    assert record.effective_review_ceiling == 2
    assert (
        tick.artifacts.read_verified_bytes(
            recovered.run_id,
            record.raw_fix.relative_path,  # type: ignore[union-attr]
            expected_sha256=record.raw_fix.sha256,  # type: ignore[union-attr]
        )
        == fix.encode()
    )

    final = _drive_sequence_final(
        _tick(git_repo, scheduler_paths),
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    assert isinstance(final, AwaitingFinalizationSequenceState)
    completed = leaf_state(scheduler_paths, recovered.run_id)
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
    assert prompts[1] == prompts[2]
    assert prompts[2].endswith(fix.encode())


def test_publication_fault_then_sequence_abort_cancels_without_resurrection(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(
        git_repo, scheduler_paths, monkeypatch, implementer_sequence="fail"
    )
    service = CodexImplementerFailureRecoveryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
        run_id_factory=lambda _project, _now: "phase27-6-pending-successor",
        fault_point="after_artifacts",
    )
    with pytest.raises(OSError, match="after_artifacts"):
        service.force(blocked.run_id)
    with tick.store.begin_read() as conn:
        relation = tick.store.get_codex_implementer_recovery_by_successor(
            conn, successor_run_id="phase27-6-pending-successor"
        )
        replacement = tick.store.get_sequence_implementer_replacement_by_successor(
            conn, "phase27-6-pending-successor"
        )
        assert tick.store.get_reservation_for_run(conn, "phase27-6-pending-successor") is not None
    assert relation is not None and str(relation["status"]) == "pending"
    assert replacement is not None and replacement["adopted_at"] is None
    assert sequence_state(scheduler_paths, prepared.sequence_id).current_run_id == blocked.run_id

    aborted = SequenceAbortService(tick.store).abort_sequence(prepared.sequence_id)
    assert aborted.state_kind == "aborted"
    receipts = service.reconcile_pending()
    assert receipts == []
    for _ in range(4):
        _tick(git_repo, scheduler_paths).run_once()
    with tick.store.begin_read() as conn:
        relation = tick.store.get_codex_implementer_recovery_by_successor(
            conn, successor_run_id="phase27-6-pending-successor"
        )
        replacement = tick.store.get_sequence_implementer_replacement_by_successor(
            conn, "phase27-6-pending-successor"
        )
        effects = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            ("phase27-6-pending-successor",),
        ).fetchone()[0]
    assert relation is not None and str(relation["status"]) == "cancelled"
    assert replacement is not None and replacement["cancelled_at"] is not None
    assert effects == 0
    assert isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState)


def test_restart_tick_reconciles_interrupted_publication_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    head = _git(git_repo, "rev-parse", "HEAD")
    successor_run_id = "phase27-6-restarted-successor"
    interrupted = CodexImplementerFailureRecoveryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
        run_id_factory=lambda _project, _now: successor_run_id,
        fault_point="after_artifacts",
    )
    with pytest.raises(OSError, match="after_artifacts"):
        interrupted.force(blocked.run_id)
    with tick.store.begin_read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
                (successor_run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                "WHERE sequence_id = ? AND ordinal = 1",
                (prepared.sequence_id,),
            ).fetchone()[0]
            == 1
        )
    pending = sequence_state(scheduler_paths, prepared.sequence_id)
    assert pending.current_run_id == blocked.run_id

    restarted = _tick(git_repo, scheduler_paths)
    final = _drive_sequence_final(
        restarted,
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    assert isinstance(final, AwaitingFinalizationSequenceState)
    assert final.materialized_entries[0].run_id == successor_run_id
    assert _git(git_repo, "rev-list", "--count", f"{head}..HEAD") == "1"
    with restarted.store.begin_read() as conn:
        relation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries WHERE source_run_id = ?",
            (blocked.run_id,),
        ).fetchone()[0]
        replacement_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_implementer_replacements "
            "WHERE source_run_id = ?",
            (blocked.run_id,),
        ).fetchone()[0]
        lineage_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
            "WHERE sequence_id = ? AND ordinal = 1",
            (prepared.sequence_id,),
        ).fetchone()[0]
        effect_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ? "
            "AND effect_kind = 'codex.implementer_resume'",
            (successor_run_id,),
        ).fetchone()[0]
    assert (relation_count, replacement_count, lineage_count, effect_count) == (1, 1, 2, 1)


def test_synchronized_abort_wins_before_pending_publication_adoption(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail",
    )
    successor_run_id = "phase27-6-abort-wins-successor"
    interrupted = CodexImplementerFailureRecoveryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
        run_id_factory=lambda _project, _now: successor_run_id,
        fault_point="after_artifacts",
    )
    with pytest.raises(OSError, match="after_artifacts"):
        interrupted.force(blocked.run_id)

    reconciler = CodexImplementerFailureRecoveryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    )
    entered = threading.Event()
    release = threading.Event()
    original_mark_ready = reconciler._mark_ready

    def pause_before_adoption(intent: object) -> None:
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("publication adoption barrier timed out")
        original_mark_ready(intent)  # type: ignore[arg-type]

    monkeypatch.setattr(reconciler, "_mark_ready", pause_before_adoption)
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconciler.reconcile_pending()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    worker = threading.Thread(target=reconcile)
    worker.start()
    assert entered.wait(timeout=10)
    aborted = SequenceAbortService(tick.store).abort_sequence(prepared.sequence_id)
    assert aborted.state_kind == "aborted"
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive() and not errors
    with tick.store.begin_read() as conn:
        relation = tick.store.get_codex_implementer_recovery_by_successor(
            conn,
            successor_run_id=successor_run_id,
        )
        replacement = tick.store.get_sequence_implementer_replacement_by_successor(
            conn,
            successor_run_id,
        )
        effects = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()[0]
    assert relation is not None and str(relation["status"]) == "cancelled"
    assert replacement is not None and replacement["cancelled_at"] is not None
    assert replacement["adopted_at"] is None and effects == 0


def test_synchronized_adoption_wins_once_then_abort_fences_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application import sequence_implementer_recovery as module

    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    assert tick._sequence_abort is not None
    adoption_entered = threading.Event()
    abort_attempting = threading.Event()
    original_adopt = module.adopt_sequence_implementer_successor
    original_abort_blocked = tick._sequence_abort._abort_blocked

    def pause_adoption(*args: object, **kwargs: object) -> bool:
        adoption_entered.set()
        if not abort_attempting.wait(timeout=10):
            raise AssertionError("abort did not reach the blocked adoption race")
        return original_adopt(*args, **kwargs)  # type: ignore[arg-type]

    def signal_abort(*args: object, **kwargs: object) -> object:
        abort_attempting.set()
        return original_abort_blocked(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "adopt_sequence_implementer_successor", pause_adoption)
    monkeypatch.setattr(tick._sequence_abort, "_abort_blocked", signal_abort)
    recovery: list[object] = []
    aborts: list[object] = []
    errors: list[BaseException] = []

    def recover() -> None:
        try:
            recovery.append(
                ImplementerRetryService(
                    tick.store,
                    tick.artifacts,
                    now_factory=lambda: NOW,
                ).retry(blocked.run_id, force=True)
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def abort() -> None:
        try:
            aborts.append(tick._sequence_abort.abort_sequence(prepared.sequence_id))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    recovery_thread = threading.Thread(target=recover)
    recovery_thread.start()
    assert adoption_entered.wait(timeout=10)
    abort_thread = threading.Thread(target=abort)
    abort_thread.start()
    recovery_thread.join(timeout=10)
    abort_thread.join(timeout=10)
    assert not recovery_thread.is_alive() and not abort_thread.is_alive()
    assert not errors and len(recovery) == len(aborts) == 1
    successor_run_id = recovery[0].run_id  # type: ignore[union-attr]
    for _ in range(5):
        _tick(git_repo, scheduler_paths).run_once()
        if isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState):
            break
    assert isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState)
    with tick.store.begin_read() as conn:
        replacement = tick.store.get_sequence_implementer_replacement_by_successor(
            conn,
            successor_run_id,
        )
        ordinal_one = conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
            "WHERE sequence_id = ? AND ordinal = 1",
            (prepared.sequence_id,),
        ).fetchone()[0]
        later = conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
            "WHERE sequence_id = ? AND ordinal > 1",
            (prepared.sequence_id,),
        ).fetchone()[0]
        attempts = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()[0]
    assert replacement is not None and replacement["adopted_at"] is not None
    assert replacement["cancelled_at"] is None
    assert (ordinal_one, later, attempts) == (2, 0, 0)


def test_aborted_stale_leaf_rejects_without_recovery_relation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(
        git_repo, scheduler_paths, monkeypatch, implementer_sequence="fail"
    )
    SequenceAbortService(tick.store).abort_sequence(prepared.sequence_id)
    with pytest.raises(SchedulerEngineError, match="current blocked sequence leaf"):
        ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(
            blocked.run_id, force=True
        )
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduler_sequence_implementer_replacements"
        ).fetchone() == (0,)


@pytest.mark.parametrize("turn", ["initial", "correction"])
def test_durable_sequence_abort_fences_recovered_effect_claim_and_relaunch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    turn: str,
) -> None:
    if turn == "initial":
        prepared, tick, blocked = _blocked_codex_leaf(
            git_repo,
            scheduler_paths,
            monkeypatch,
        )
    else:
        prepared, tick, blocked = _blocked_codex_correction_leaf(
            git_repo,
            scheduler_paths,
            monkeypatch,
        )
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    backend = FakeAgentProcessBackend()
    abort_persisted = threading.Event()
    release_abort = threading.Event()
    abort_thread: list[threading.Thread] = []
    abort_errors: list[BaseException] = []
    race_tick: TickService

    def before_claim() -> None:
        if abort_thread:
            return

        def abort() -> None:
            try:
                assert race_tick._sequence_abort is not None
                race_tick._sequence_abort.abort_sequence(prepared.sequence_id)
            except BaseException as exc:  # pragma: no cover - surfaced below
                abort_errors.append(exc)

        worker = threading.Thread(target=abort)
        abort_thread.append(worker)
        worker.start()
        if not abort_persisted.wait(timeout=10):
            raise AssertionError("sequence abort did not become durable before effect claim")

    race_tick = _tick_with_claim_hook(
        git_repo,
        scheduler_paths,
        backend=backend,
        before_effect_claim=before_claim,
    )
    assert race_tick._sequence_abort is not None
    original_advance = race_tick._sequence_abort._advance_pending_run_abort

    def pause_after_durable_abort(state: AbortPendingSequenceState) -> object:
        abort_persisted.set()
        if not release_abort.wait(timeout=10):
            raise AssertionError("sequence abort continuation barrier timed out")
        return original_advance(state)

    monkeypatch.setattr(
        race_tick._sequence_abort,
        "_advance_pending_run_abort",
        pause_after_durable_abort,
    )
    try:
        receipt = race_tick.run_once()
        assert any(
            item.action == "codex_implementer_recovery_dispatch_fenced"
            for item in receipt.run_receipts
        )
        assert isinstance(
            sequence_state(scheduler_paths, prepared.sequence_id),
            AbortPendingSequenceState,
        )
        with race_tick.store.begin_read() as conn:
            assert race_tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
                    (recovered.run_id,),
                ).fetchone()[0]
                == 0
            )
        assert not backend.launch_calls
    finally:
        release_abort.set()
        for worker in abort_thread:
            worker.join(timeout=10)
    assert abort_thread and not abort_thread[0].is_alive() and not abort_errors
    for _ in range(8):
        _tick(git_repo, scheduler_paths).run_once()
        if isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState):
            break
    assert isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState)
    with race_tick.store.begin_read() as conn:
        assert race_tick.store.get_reservation_for_run(conn, recovered.run_id) is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                "WHERE sequence_id = ? AND ordinal > 1",
                (prepared.sequence_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("turn", ["initial", "correction"])
def test_direct_successor_abort_reconciles_termination_without_operator_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    turn: str,
) -> None:
    if turn == "initial":
        prepared, tick, blocked = _blocked_codex_leaf(
            git_repo,
            scheduler_paths,
            monkeypatch,
        )
    else:
        prepared, tick, blocked = _blocked_codex_correction_leaf(
            git_repo,
            scheduler_paths,
            monkeypatch,
        )
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    scenario = FakeAttemptScenario(active_ticks=100, stays_active_after_terminate=True)
    backend = FakeAgentProcessBackend(default_scenario=scenario)
    recovery_tick = _tick(git_repo, scheduler_paths, backend=backend)

    for _ in range(12):
        recovery_tick.run_once()
        if backend.launch_calls:
            break
    else:
        raise AssertionError("recovered sequence successor did not launch")
    assert len(backend.launch_calls) == 1
    request = backend.launch_calls[0]

    abort = SchedulerAbortService(
        recovery_tick.store,
        backend,
        artifacts=recovery_tick.artifacts,
        now_factory=lambda: NOW,
    ).abort_run(recovered.run_id)
    assert abort.termination_pending
    observations_after_abort = len(backend.observe_calls)
    with recovery_tick.store.begin_read() as conn:
        state, _, _ = recovery_tick.store.load_validated_snapshot(conn, recovered.run_id)
        assert state.kind == "aborted"
        assert recovery_tick.store.has_unresolved_abort_hold(conn, recovered.run_id)
        assert recovery_tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
        assert recovery_tick.store.get_capacity_row(conn)["holder_run_id"] == recovered.run_id

    pending = recovery_tick.run_once()
    assert len(backend.observe_calls) > observations_after_abort
    assert any(
        item.run_id == recovered.run_id and item.action == "attempt_abort_pending"
        for item in pending.run_receipts
    )
    with recovery_tick.store.begin_read() as conn:
        assert recovery_tick.store.has_unresolved_abort_hold(conn, recovered.run_id)
        assert recovery_tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
        assert recovery_tick.store.get_capacity_row(conn)["holder_run_id"] == recovered.run_id

    observations_while_uncertain = len(backend.observe_calls)
    scenario.stays_active_after_terminate = False
    for _ in range(8):
        recovery_tick.run_once()
        with recovery_tick.store.begin_read() as conn:
            hold = recovery_tick.store.has_unresolved_abort_hold(conn, recovered.run_id)
            reservation = recovery_tick.store.get_reservation_for_run(conn, recovered.run_id)
            capacity_holder = recovery_tick.store.get_capacity_row(conn)["holder_run_id"]
        if not hold and reservation is None and capacity_holder is None:
            break
    else:
        raise AssertionError("ordinary ticks did not finish recovered successor abort cleanup")

    assert len(backend.observe_calls) > observations_while_uncertain
    assert backend.observe_calls[-1] == (request.unit_identity, request.attempt_id)
    assert len(backend.launch_calls) == 1
    converged_sequence = sequence_state(scheduler_paths, prepared.sequence_id)
    assert isinstance(converged_sequence, BlockedSequenceState)
    assert converged_sequence.current_run_id == recovered.run_id
    with recovery_tick.store.begin_read() as conn:
        assert not recovery_tick.store.has_unresolved_abort_hold(conn, recovered.run_id)
        assert recovery_tick.store.get_reservation_for_run(conn, recovered.run_id) is None
        assert recovery_tick.store.get_capacity_row(conn)["holder_run_id"] is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
                (recovered.run_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                "WHERE sequence_id = ? AND ordinal > 1",
                (prepared.sequence_id,),
            ).fetchone()[0]
            == 0
        )


def test_restart_relaunch_rechecks_durable_sequence_abort(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)

    class RelaunchBackend(FakeAgentProcessBackend):
        launch_attempts = 0
        armed = False
        observe_barrier: object | None = None

        def launch(self, request: object) -> None:
            self.launch_attempts += 1
            super().launch(request)  # type: ignore[arg-type]

        def observe(self, *, unit_identity: str, attempt_id: str) -> object:
            if self.armed and self.observe_barrier is not None:
                self.armed = False
                self.observe_barrier()  # type: ignore[operator]
            return super().observe(unit_identity=unit_identity, attempt_id=attempt_id)

    backend = RelaunchBackend(default_scenario=FakeAttemptScenario(launch_raises=True))
    first_tick = _tick(git_repo, scheduler_paths, backend=backend)
    first = first_tick.run_once()
    assert any(item.action == "attempt_launch_failed" for item in first.run_receipts)
    assert backend.launch_attempts == 1
    with first_tick.store.begin_read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ? AND status = 'launching'",
                (recovered.run_id,),
            ).fetchone()[0]
            == 1
        )

    restart = _tick(git_repo, scheduler_paths, backend=backend)
    assert restart._sequence_abort is not None
    abort_persisted = threading.Event()
    release_abort = threading.Event()
    abort_errors: list[BaseException] = []
    abort_worker: list[threading.Thread] = []
    original_advance = restart._sequence_abort._advance_pending_run_abort

    def pause_after_abort(state: AbortPendingSequenceState) -> object:
        abort_persisted.set()
        if not release_abort.wait(timeout=10):
            raise AssertionError("restart abort barrier timed out")
        return original_advance(state)

    monkeypatch.setattr(restart._sequence_abort, "_advance_pending_run_abort", pause_after_abort)

    def start_abort_during_observe() -> None:
        def abort() -> None:
            try:
                assert restart._sequence_abort is not None
                restart._sequence_abort.abort_sequence(prepared.sequence_id)
            except BaseException as exc:  # pragma: no cover - surfaced below
                abort_errors.append(exc)

        worker = threading.Thread(target=abort)
        abort_worker.append(worker)
        worker.start()
        if not abort_persisted.wait(timeout=10):
            raise AssertionError("restart did not observe durable sequence abort")

    backend.observe_barrier = start_abort_during_observe
    backend.armed = True
    try:
        receipt = restart.run_once()
        assert any(
            item.action == "codex_implementer_recovery_dispatch_fenced"
            for item in receipt.run_receipts
        )
        assert backend.launch_attempts == 1
        with restart.store.begin_read() as conn:
            assert restart.store.get_reservation_for_run(conn, recovered.run_id) is not None
    finally:
        release_abort.set()
        for worker in abort_worker:
            worker.join(timeout=10)
    assert abort_worker and not abort_worker[0].is_alive() and not abort_errors
    for _ in range(8):
        _tick(git_repo, scheduler_paths, backend=backend).run_once()
        if isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState):
            break
    assert isinstance(sequence_state(scheduler_paths, prepared.sequence_id), AbortedSequenceState)


def test_tampered_adopted_intent_digest_rejects_dispatch_and_replay(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    retry = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    recovered = retry.retry(blocked.run_id, force=True)
    with tick.store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_sequence_implementer_replacements "
            "SET intent_payload_sha256 = ? WHERE successor_run_id = ?",
            ("0" * 64, recovered.run_id),
        )
    with pytest.raises(SchedulerEngineError, match="intent digest mismatch"):
        _tick(git_repo, scheduler_paths).run_once()
    with pytest.raises(SchedulerEngineError, match="intent digest mismatch"):
        retry.retry(blocked.run_id, force=True)
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
                (recovered.run_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "binding",
    [
        "source_run_id",
        "successor_run_id",
        "sequence_id",
        "ordinal",
        "generation",
        "entry_hash",
        "record_digest",
    ],
)
def test_canonical_adopted_intent_binding_tamper_rejects_production_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    _prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    with tick.store.begin_immediate() as conn:
        row = tick.store.get_sequence_implementer_replacement_by_successor(
            conn,
            recovered.run_id,
        )
        assert row is not None
        payload = json.loads(str(row["intent_payload"]))
        if binding == "source_run_id":
            payload[binding] = "tampered-source"
        elif binding == "successor_run_id":
            payload[binding] = "tampered-successor"
        elif binding == "sequence_id":
            payload[binding] = "tampered-sequence"
        elif binding == "ordinal":
            payload[binding] = 2
        elif binding == "generation":
            payload["source_generation"] = 2
            payload["successor_generation"] = 3
            conn.execute(
                "UPDATE scheduler_sequence_implementer_replacements "
                "SET source_generation = 2, successor_generation = 3 "
                "WHERE successor_run_id = ?",
                (recovered.run_id,),
            )
        else:
            payload[binding] = "0" * 64
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE scheduler_sequence_implementer_replacements "
            "SET intent_payload = ?, intent_payload_sha256 = ? "
            "WHERE successor_run_id = ?",
            (canonical, hashlib.sha256(canonical.encode()).hexdigest(), recovered.run_id),
        )

    with pytest.raises(SchedulerEngineError):
        _tick(git_repo, scheduler_paths).run_once()
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
                (recovered.run_id,),
            ).fetchone()[0]
            == 0
        )


def test_tampered_ancestor_adoption_rejects_descendant_recovery(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,fail",
    )
    first = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    descendant_source = _drive_sequence_leaf_to(
        tick,
        scheduler_paths,
        prepared.sequence_id,
        "blocked",
    )
    assert descendant_source.run_id == first.run_id
    with tick.store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_sequence_implementer_replacements "
            "SET intent_payload_sha256 = ? WHERE successor_run_id = ?",
            ("f" * 64, first.run_id),
        )
    with pytest.raises(SchedulerEngineError, match="intent digest mismatch"):
        ImplementerRetryService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: NOW,
        ).retry(descendant_source.run_id, force=True)
    with tick.store.begin_read() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                "WHERE sequence_id = ? AND ordinal = 1",
                (prepared.sequence_id,),
            ).fetchone()[0]
            == 2
        )


def test_duplicate_force_lineage_api_capability_and_redaction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    service = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    first = service.retry(blocked.run_id, force=True)
    replay = service.retry(blocked.run_id, force=True)
    assert first.run_id == replay.run_id
    assert first.changed and not replay.changed
    with tick.store.begin_read() as conn:
        recovery = tick.store.get_codex_implementer_recovery_by_successor(
            conn, successor_run_id=first.run_id
        )
        assert recovery is not None
        failed_attempt_id = str(recovery["failed_attempt_id"])
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries "
                    "WHERE source_run_id = ?",
                    (blocked.run_id,),
                ).fetchone()[0]
            )
            == 1
        )
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                    "WHERE sequence_id = ? AND ordinal = 1",
                    (prepared.sequence_id,),
                ).fetchone()[0]
            )
            == 2
        )

    info = build_integration_info_data()
    assert info.capabilities.codex_implementer_manual_recovery
    run_api = IntegrationRunReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    inspect = run_api.inspect_run(first.run_id).model_dump_json(by_alias=True)
    assert IMPLEMENTER_ID not in inspect and REVIEWER_ID not in inspect
    source_inspect = run_api.inspect_run(blocked.run_id)
    assert source_inspect.safe_next_action.kind == "scheduler_tick"
    status_action = (
        SchedulerStatusService(tick.store).get_status(blocked.run_id).summary.safe_next_action
    )
    assert status_action.command is not None
    assert "scheduler implementer-retry" in status_action.command
    history = run_api.list_history(first.run_id)
    assert "codex_implementer_recovery_successor_created" in {item.kind for item in history.items}
    output = IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).read_process_output(
        blocked.run_id,
        attempt_id=failed_attempt_id,
        stream="stderr",
    )
    assert output.available and output.complete
    assert output.component == "codex_implementer"
    sequence_api = IntegrationSequenceReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    phase_runs = sequence_api.list_phase_runs(prepared.sequence_id, ordinal=1, offset=0, limit=20)
    assert [item.attempt_kind for item in phase_runs.items] == [
        "planned_run",
        "implementer_retry",
    ]
    row = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    with row.begin_read() as conn:
        relation = row.get_sequence_implementer_replacement_by_successor(conn, first.run_id)
    assert relation is not None
    assert IMPLEMENTER_ID not in str(relation["intent_payload"])
    assert hashlib.sha256(str(relation["intent_payload"]).encode()).hexdigest() == str(
        relation["intent_payload_sha256"]
    )


def test_repeated_successor_failure_reuses_identity_prompt_and_current_ordinal(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = scheduler_paths["db_path"].parent / "repeated-recovery-prompts"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness))
    prepared, tick, first_source = _blocked_codex_leaf(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,fail,success",
    )
    retry = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    first = retry.retry(first_source.run_id, force=True)
    second_source = _drive_sequence_leaf_to(tick, scheduler_paths, prepared.sequence_id, "blocked")
    assert second_source.run_id == first.run_id
    assert second_source.block_reason_kind == "codex_implementer_failure"
    second = retry.retry(second_source.run_id, force=True)

    current = sequence_state(scheduler_paths, prepared.sequence_id)
    assert isinstance(current, ActiveSequenceState)
    assert current.current_ordinal == 1 and current.current_run_id == second.run_id
    assert second.run_id not in {first_source.run_id, first.run_id}
    with tick.store.begin_read() as conn:
        lineage = load_sequence_run_lineage(
            conn, prepared.sequence_id, definition=current.definition
        )
        record = load_implementer_recovery_record(tick.store, tick.artifacts, conn, second.run_id)
    attempts = lineage.phase_executions[0].attempts
    assert [attempt.attempt_kind for attempt in attempts] == [
        "planned_run",
        "implementer_retry",
        "implementer_retry",
    ]
    assert [attempt.generation for attempt in attempts] == [1, 2, 3]
    assert record is not None
    assert record.implementer.session_id == IMPLEMENTER_ID
    assert record.parent_source_run_id == first_source.run_id
    assert record.parent_recovery_key is not None and record.parent_record_sha256 is not None
    for _ in range(20):
        tick.run_once()
        prompts = [path.read_bytes() for path in sorted(witness.glob("*.bin"))]
        if len(prompts) == 3:
            break
    else:
        raise AssertionError("second recovery successor did not dispatch")
    assert len(prompts) == 3 and prompts[0] == prompts[1] == prompts[2]


def test_abort_before_checkpoint_ref_cas_prevents_commit_and_later_phase(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    head = _git(git_repo, "rev-parse", "HEAD")
    recovered = ImplementerRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(
        blocked.run_id, force=True
    )
    assert recovered.changed
    assert tick._sequence_handoff is not None
    assert tick._sequence_abort is not None
    observed: dict[str, object] = {}

    def abort_before_ref_cas(step: str) -> None:
        if step != "before_git_checkpoint" or observed:
            return
        assert tick._sequence_handoff is not None
        tick._sequence_handoff._handoff_step_hook = None
        result = tick._sequence_abort.abort_sequence(prepared.sequence_id)
        observed["result"] = result
        with tick.store.begin_read() as conn:
            observed["reservation_held"] = (
                tick.store.get_reservation_for_run(conn, recovered.run_id) is not None
            )

    tick._sequence_handoff._handoff_step_hook = abort_before_ref_cas
    for _ in range(160):
        tick.run_once()
        current = sequence_state(scheduler_paths, prepared.sequence_id)
        if isinstance(current, AbortedSequenceState):
            break
    else:
        raise AssertionError("checkpoint abort did not converge")

    assert observed and observed["reservation_held"] is False
    assert _git(git_repo, "rev-parse", "HEAD") == head
    with tick.store.begin_read() as conn:
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                    "WHERE sequence_id = ? AND ordinal = 2",
                    (prepared.sequence_id,),
                ).fetchone()[0]
            )
            == 0
        )
        assert tick.store.get_reservation_for_run(conn, recovered.run_id) is None


@pytest.mark.parametrize("cas_applied", [False, True])
def test_checkpoint_ref_cas_uncertainty_observation_aborts_without_second_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    cas_applied: bool,
) -> None:
    prepared, tick, blocked = _blocked_codex_leaf(git_repo, scheduler_paths, monkeypatch)
    base_head = _git(git_repo, "rev-parse", "HEAD")
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: NOW,
    ).retry(blocked.run_id, force=True)
    original_update_ref = git_checkpoint_module.checkpoint_git_update_ref_cas
    update_ref_calls = 0

    def ambiguous_update_ref(*args: object, **kwargs: object) -> object:
        nonlocal update_ref_calls
        update_ref_calls += 1
        if cas_applied:
            original_update_ref(*args, **kwargs)  # type: ignore[arg-type]
        raise ValidationError("injected ambiguous checkpoint ref-CAS outcome")

    with patch.object(
        git_checkpoint_module,
        "checkpoint_git_update_ref_cas",
        ambiguous_update_ref,
    ):
        for _ in range(160):
            receipt = tick.run_once()
            if any(
                item.action == "checkpoint_reconciliation_hold" for item in receipt.run_receipts
            ):
                break
        else:
            raise AssertionError("recovered sequence did not reach ref-CAS uncertainty")
        assert update_ref_calls == 1
        observed_head = _git(git_repo, "rev-parse", "HEAD")
        assert (observed_head != base_head) is cas_applied
        with tick.store.begin_read() as conn:
            hold = tick.store.get_checkpoint_reconciliation_hold_row(conn, recovered.run_id)
            assert hold is not None
            assert tick.store.get_reservation_for_run(conn, recovered.run_id) is not None

        assert tick._sequence_abort is not None
        abort = tick._sequence_abort.abort_sequence(prepared.sequence_id)
        assert abort.state_kind == "abort_pending"
        with tick.store.begin_read() as conn:
            assert (
                tick.store.get_checkpoint_reconciliation_hold_row(
                    conn,
                    recovered.run_id,
                )
                is not None
            )
            assert tick.store.get_reservation_for_run(conn, recovered.run_id) is not None

        for _ in range(80):
            tick.run_once()
            if isinstance(
                sequence_state(scheduler_paths, prepared.sequence_id),
                AbortedSequenceState,
            ):
                break
        else:
            raise AssertionError("uncertain checkpoint abort did not converge")

    assert update_ref_calls == 1
    assert _git(git_repo, "rev-parse", "HEAD") == observed_head
    assert _git(git_repo, "rev-list", "--count", f"{base_head}..HEAD") == (
        "1" if cas_applied else "0"
    )
    with tick.store.begin_read() as conn:
        assert tick.store.get_checkpoint_reconciliation_hold_row(conn, recovered.run_id) is None
        assert tick.store.get_reservation_for_run(conn, recovered.run_id) is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_sequence_run_attempts "
                "WHERE sequence_id = ? AND ordinal > 1",
                (prepared.sequence_id,),
            ).fetchone()[0]
            == 0
        )


def test_v15_migration_adds_sequence_replacement_without_rewriting_runs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(database)
    store.bootstrap()
    with store.begin_immediate() as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0])
        conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
        conn.execute("DROP TABLE scheduler_planned_run_presentation")
        conn.execute("DROP TABLE scheduler_run_presentation")
        conn.execute("DROP TABLE scheduler_sequence_presentation")
        conn.execute("DROP TABLE scheduler_sequence_implementer_replacements")
        conn.execute(
            "DROP INDEX IF EXISTS idx_scheduler_sequence_implementer_replacements_sequence"
        )
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 16")
        conn.execute("PRAGMA user_version = 15")
    store.bootstrap()
    with store.begin_read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        assert int(conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]) == before
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_sequence_implementer_replacements"
                ).fetchone()[0]
            )
            == 0
        )
        assert (
            conn.execute(
                "SELECT name FROM scheduler_schema_migrations WHERE version = 16"
            ).fetchone()[0]
            == "0016_sequence_implementer_replacement"
        )
