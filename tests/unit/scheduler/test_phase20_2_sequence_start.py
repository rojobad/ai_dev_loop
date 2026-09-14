"""Unit tests for Phase 20.2 scheduler sequence start."""

from __future__ import annotations

import json
import os
import threading
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import (
    CONTROLLER_SESSION,
    sample_agent_led_submitted_context,
    sample_fresh_submitted_context,
    sample_legacy_submitted_context,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_NOW,
    FIXED_RUN_IDS,
    _prepare_service,
    _two_phase_manifest,
    _write_manifest,
)

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    SchedulerEngineError,
    sequence_exposes_checkpoint_boundary,
)
from ai_dev_loop.scheduler.application.sequence_materializer import (
    SequenceRunMaterializer,
    build_sequence_run_context,
    frozen_entry_hash,
    sequence_run_idempotency_key,
)
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.application.sequence_start import SequenceStartService
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.start import StartService
from ai_dev_loop.scheduler.application.submission import SubmissionService, SubmitOptions
from ai_dev_loop.scheduler.domain.admission_contract import ADMISSION_STATUS_ARTIFACT
from ai_dev_loop.scheduler.domain.events import RunAbortedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
from ai_dev_loop.scheduler.domain.sequence import (
    ACTIVE_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    AuthorizedState,
    BlockedState,
    SequenceRunBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _prepare_sequence(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    result = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    return result.sequence_id


def _start_service(
    scheduler_paths: dict[str, Path],
    *,
    start_step_hook=None,
) -> SequenceStartService:
    return SequenceStartService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: FIXED_NOW,
        start_step_hook=start_step_hook,
    )


def test_sequence_bound_context_v4_validates() -> None:
    base = sample_agent_led_submitted_context()
    context = SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
        project_name=base.project_name,
        repository=base.repository,
        plan_prompt=base.plan_prompt,
        effective_config=base.effective_config,
        codex=base.codex,
        cursor=base.cursor,
        workflow=base.workflow,
        controller=base.controller,
        sequence=SequenceRunBinding(
            sequence_id="fixture-project-seq-abc",
            ordinal=1,
            total_phases=2,
            entry_hash="a" * 64,
        ),
    )
    assert context.schema_version == 4
    assert context.sequence is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ordinal", 0),
        ("total_phases", 0),
        ("ordinal", 3),
    ],
)
def test_sequence_binding_rejects_invalid_ordinals(field: str, value: int) -> None:
    with pytest.raises(PydanticValidationError):
        SequenceRunBinding(
            sequence_id="seq",
            ordinal=1 if field != "ordinal" else value,
            total_phases=2 if field != "total_phases" else value,
            entry_hash="a" * 64,
        )


def test_historical_submitted_context_versions_still_validate() -> None:
    assert sample_agent_led_submitted_context().schema_version == 3
    assert sample_legacy_submitted_context().schema_version == 1
    assert sample_fresh_submitted_context().schema_version == 2


def test_materializer_copies_exact_bytes_from_sequence_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(prepared, PreparedSequenceState)
    entry = prepared.definition.entries[0]
    materializer = SequenceRunMaterializer(artifacts)
    context, entry_hash = materializer.materialize_first_entry(
        sequence_id=sequence_id,
        definition=prepared.definition,
        entry=entry,
    )
    run_id = entry.planned_run_id
    assert context.plan_prompt.plan_sha256 == entry.plan_prompt.plan_sha256
    assert context.sequence is not None
    assert context.sequence.entry_hash == entry_hash
    source_paths = (
        entry.plan_prompt.plan_artifact_path,
        entry.plan_prompt.prompt_artifact_path,
        entry.effective_config.effective_config_artifact_path,
        entry.effective_config.source_config_artifact_path,
        entry.codex.binding_artifact_path,
    )
    run_paths = (
        context.plan_prompt.plan_artifact_path,
        context.plan_prompt.prompt_artifact_path,
        context.effective_config.effective_config_artifact_path,
        context.effective_config.source_config_artifact_path,
        context.codex.binding_artifact_path,
    )
    for source_path, run_path, expected_sha256 in zip(
        source_paths,
        run_paths,
        (
            entry.plan_prompt.plan_sha256,
            entry.plan_prompt.prompt_sha256,
            entry.effective_config.effective_config_sha256,
            entry.effective_config.source_config_sha256,
            entry.codex.binding_sha256,
        ),
        strict=True,
    ):
        copied = artifacts.read_verified_bytes(
            run_id,
            run_path,
            expected_sha256=expected_sha256,
        )
        source = artifacts.read_sequence_verified_bytes(
            sequence_id,
            source_path,
            expected_sha256=expected_sha256,
        )
        assert copied == source
    run_root = run_artifact_root(artifacts.artifact_root, run_id)
    assert not any(path.is_symlink() for path in run_root.rglob("*"))


def test_materializer_retry_verifies_existing_run_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    service = _start_service(scheduler_paths)
    first = service.start(sequence_id)
    second = service.start(sequence_id)
    assert first.run_id == second.run_id
    assert second.idempotent_replay is True
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        rows = conn.execute("SELECT run_id FROM scheduler_runs").fetchall()
        events = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
        reservations = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchone()
    assert len(rows) == 1
    assert int(events[0]) == 2
    assert int(reservations[0]) == 1


def test_sequence_start_materializes_prepared_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    result = _start_service(scheduler_paths).start(sequence_id)
    assert result.changed is True
    assert result.run_id == FIXED_RUN_IDS[0]
    assert result.current_ordinal == 1
    assert result.sequence_state_kind == ACTIVE_SEQUENCE_STATE_KIND
    assert result.run_state_kind == "authorized"
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        run_state, version, _ = store.load_validated_snapshot(conn, result.run_id)
        reservation = store.get_reservation_for_run(conn, result.run_id)
        events = conn.execute(
            "SELECT sequence, event_kind FROM scheduler_events WHERE run_id = ? ORDER BY sequence",
            (result.run_id,),
        ).fetchall()
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == result.run_id
    assert run_state.kind == "authorized"
    assert version == 2
    assert reservation is not None
    assert [(int(row[0]), str(row[1])) for row in events] == [
        (1, "run_submitted"),
        (2, "run_authorized"),
    ]
    assert run_state.context.sequence is not None
    assert run_state.context.sequence.sequence_id == sequence_id


def test_sequence_start_is_idempotent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    service = _start_service(scheduler_paths)
    first = service.start(sequence_id)
    second = service.start(sequence_id)
    assert first.changed is True
    assert second.changed is False
    assert second.idempotent_replay is True


def test_sequence_start_rejects_missing_sequence(scheduler_paths: dict[str, Path]) -> None:
    with pytest.raises(SchedulerEngineError, match="not found"):
        _start_service(scheduler_paths).start("missing-sequence")


def test_sequence_start_reservation_conflict_leaves_sequence_prepared(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    submission = SubmissionService(
        store,
        artifacts,
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
    )
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with patch("sys.stdin", StringIO(prompt)):
        submission.submit(options)
    with pytest.raises(SchedulerEngineError, match="reservation"):
        _start_service(scheduler_paths).start(sequence_id)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        runs = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert isinstance(sequence, PreparedSequenceState)
    assert int(runs[0]) == 1


def test_planned_run_id_not_addressable_before_materialization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    _prepare_sequence(git_repo, scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="not found"):
        StartService(SqliteSchedulerStore(scheduler_paths["db_path"])).start(FIXED_RUN_IDS[0])


def test_concurrent_sequence_start_creates_one_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(_start_service(scheduler_paths).start(sequence_id))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert len({result.run_id for result in results}) == 1
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert int(count[0]) == 1


def test_sequence_start_has_no_subprocess_side_effects(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("sequence start must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    _start_service(scheduler_paths).start(sequence_id)
    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after


def test_sequence_status_shows_active_run_without_sensitive_content(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(sequence_id)
    rendered = json.dumps(status.model_dump(mode="json"))
    assert status.state_kind == ACTIVE_SEQUENCE_STATE_KIND
    assert status.current_run_id == start.run_id
    assert "cursor-initial" not in rendered
    assert "sample-plan" not in rendered
    assert CONTROLLER_SESSION not in rendered


def test_submission_idempotency_includes_sequence_binding() -> None:
    base = sample_agent_led_submitted_context()
    plain = SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
        project_name=base.project_name,
        repository=base.repository,
        plan_prompt=base.plan_prompt,
        effective_config=base.effective_config,
        codex=base.codex,
        cursor=base.cursor,
        workflow=base.workflow,
        controller=base.controller,
    )
    bound = plain.model_copy(
        update={
            "schema_version": SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
            "sequence": SequenceRunBinding(
                sequence_id="seq",
                ordinal=1,
                total_phases=2,
                entry_hash="c" * 64,
            ),
        }
    )
    assert sequence_run_idempotency_key(plain) != sequence_run_idempotency_key(bound)


def test_checkpoint_boundary_applies_only_to_successful_terminal_states() -> None:
    assert sequence_exposes_checkpoint_boundary(
        run_state_kind="completed",
        current_ordinal=1,
        total_phases=2,
    )
    assert sequence_exposes_checkpoint_boundary(
        run_state_kind="completed_with_residual_risk",
        current_ordinal=1,
        total_phases=2,
    )
    assert not sequence_exposes_checkpoint_boundary(
        run_state_kind="blocked",
        current_ordinal=1,
        total_phases=2,
    )
    assert not sequence_exposes_checkpoint_boundary(
        run_state_kind="aborted",
        current_ordinal=1,
        total_phases=2,
    )
    assert not sequence_exposes_checkpoint_boundary(
        run_state_kind="max_iterations_reached",
        current_ordinal=1,
        total_phases=2,
    )


def _transition_sequence_run(
    scheduler_paths: dict[str, Path],
    sequence_id: str,
    *,
    new_state: object,
) -> str:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        run_id = sequence.current_run_id
        _, version, _ = store.load_validated_snapshot(conn, run_id)
    with store.begin_immediate() as conn:
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=new_state,
            now=FIXED_NOW,
        )
    return run_id


def test_sequence_status_preserves_blocked_run_safe_action(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        run_state, _, _ = store.load_validated_snapshot(conn, sequence.current_run_id)
        assert isinstance(run_state, AuthorizedState)
    blocked = BlockedState(
        run_id=run_state.run_id,
        version=run_state.version + 1,
        idempotency_key=run_state.idempotency_key,
        submitted_at=run_state.submitted_at,
        updated_at="2026-09-12T12:05:00.000000Z",
        context=run_state.context,
        blocked_at="2026-09-12T12:05:00.000000Z",
        block_reason_kind="dirty_worktree",
        block_reason_summary="worktree is not clean",
    )
    _transition_sequence_run(scheduler_paths, sequence_id, new_state=blocked)
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(sequence_id)
    assert status.safe_next_action.kind == SafeNextActionKind.INSPECT_BLOCKED
    assert "Phase 20.3" not in (status.safe_next_action.command or "")
    assert "dirty_worktree" in (status.safe_next_action.command or "")


def test_sequence_start_replay_preserves_aborted_run_safe_action(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        run_state, _, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(run_state, AuthorizedState)
    aborted = apply_run_aborted(
        run_state,
        RunAbortedEvent(
            run_id=start.run_id,
            reason="user_requested_abort",
            prior_state_kind="authorized",
        ),
        now_text="2026-09-12T12:05:00.000000Z",
    )
    _transition_sequence_run(scheduler_paths, sequence_id, new_state=aborted)
    replay = _start_service(scheduler_paths).start(sequence_id)
    assert replay.idempotent_replay is True
    assert replay.safe_next_action.kind == SafeNextActionKind.SCHEDULER_TICK
    assert "abort" in (replay.safe_next_action.command or "").lower()
    assert "Phase 20.3" not in (replay.safe_next_action.command or "")


def test_materializer_rejects_unexpected_orphan_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(prepared, PreparedSequenceState)
    run_id = prepared.definition.entries[0].planned_run_id
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    root = run_artifact_root(artifacts.artifact_root, run_id)
    root.mkdir(parents=True)
    (root / "cursor").mkdir()
    (root / "cursor" / "chat.json").write_text("{}", encoding="utf-8")
    materializer = SequenceRunMaterializer(artifacts)
    with pytest.raises(ProtectedArtifactError, match="unexpected run artifacts"):
        materializer.materialize_first_entry(
            sequence_id=sequence_id,
            definition=prepared.definition,
            entry=prepared.definition.entries[0],
        )
    assert (root / "cursor" / "chat.json").exists()


def test_materializer_rejects_conflicting_orphan_content(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(prepared, PreparedSequenceState)
    entry = prepared.definition.entries[0]
    run_id = entry.planned_run_id
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    root = run_artifact_root(artifacts.artifact_root, run_id)
    (root / "plan").mkdir(parents=True)
    (root / "plan" / "plan.md").write_text("wrong plan", encoding="utf-8")
    materializer = SequenceRunMaterializer(artifacts)
    with pytest.raises(ProtectedArtifactError, match="materializing sequence entry"):
        materializer.materialize_first_entry(
            sequence_id=sequence_id,
            definition=prepared.definition,
            entry=entry,
        )


def test_sequence_start_recovers_after_artifact_only_interruption(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)

    def crash_after_artifacts(step: str) -> None:
        if step == "artifacts_materialized":
            raise RuntimeError("simulated crash after artifact publication")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _start_service(scheduler_paths, start_step_hook=crash_after_artifacts).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert isinstance(sequence, PreparedSequenceState)
    assert int(run_count[0]) == 0
    result = _start_service(scheduler_paths).start(sequence_id)
    assert result.changed is True
    with store.begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        events = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
        reservations = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchone()
    assert int(run_count[0]) == 1
    assert int(events[0]) == 2
    assert int(reservations[0]) == 1


def test_sequence_start_cas_loss_rolls_back_without_active_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = SequenceStartService(
        store,
        artifacts,
        now_factory=lambda: FIXED_NOW,
    )
    original_cas = store.compare_and_swap_sequence_state

    def fail_first_cas(*args: object, **kwargs: object) -> bool:
        if not hasattr(fail_first_cas, "called"):
            fail_first_cas.called = True  # type: ignore[attr-defined]
            return False
        return original_cas(*args, **kwargs)  # type: ignore[arg-type]

    store.compare_and_swap_sequence_state = fail_first_cas  # type: ignore[method-assign]
    with pytest.raises(SchedulerEngineError, match="concurrent sequence update"):
        service.start(sequence_id)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations"
        ).fetchone()
    assert isinstance(sequence, PreparedSequenceState)
    assert int(run_count[0]) == 0
    assert int(reservation_count[0]) == 0


def test_concurrent_sequence_start_waits_through_ledger_commit(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    before_commit = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def hook(step: str) -> None:
        if step == "before_db_insert":
            before_commit.set()
            release_first.wait()

    first_service = _start_service(scheduler_paths, start_step_hook=hook)
    first_results: list[object] = []
    second_results: list[object] = []
    errors: list[Exception] = []

    def first_worker() -> None:
        try:
            first_results.append(first_service.start(sequence_id))
        except Exception as exc:
            errors.append(exc)

    def second_worker() -> None:
        try:
            second_results.append(_start_service(scheduler_paths).start(sequence_id))
        except Exception as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=first_worker)
    first_thread.start()
    assert before_commit.wait()
    second_thread = threading.Thread(target=second_worker)
    second_thread.start()
    while not second_thread.is_alive():
        pass
    assert not second_finished.is_set()
    release_first.set()
    first_thread.join()
    assert second_finished.wait()
    assert not errors
    assert len(first_results) == 1
    assert len(second_results) == 1
    assert first_results[0].run_id == second_results[0].run_id
    assert second_results[0].idempotent_replay is True
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchone()
        events = conn.execute(
            "SELECT sequence, event_kind FROM scheduler_events ORDER BY sequence"
        ).fetchall()
    assert int(run_count[0]) == 1
    assert int(event_count[0]) == 2
    assert int(reservation_count[0]) == 1
    assert [(int(row[0]), str(row[1])) for row in events] == [
        (1, "run_submitted"),
        (2, "run_authorized"),
    ]


def test_concurrent_start_replays_after_precommit_admission_artifact(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    run_id = FIXED_RUN_IDS[0]
    before_commit = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def hook(step: str) -> None:
        if step == "before_db_insert":
            before_commit.set()
            release_first.wait()

    first_service = _start_service(scheduler_paths, start_step_hook=hook)
    first_results: list[object] = []
    second_results: list[object] = []
    errors: list[Exception] = []

    def first_worker() -> None:
        try:
            first_results.append(first_service.start(sequence_id))
        except Exception as exc:
            errors.append(exc)

    def second_worker() -> None:
        try:
            second_results.append(_start_service(scheduler_paths).start(sequence_id))
        except Exception as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=first_worker)
    first_thread.start()
    assert before_commit.wait()
    admission_path = run_artifact_root(scheduler_paths["artifact_root"], run_id) / (
        ADMISSION_STATUS_ARTIFACT
    )
    admission_path.parent.mkdir(parents=True, exist_ok=True)
    admission_path.write_text("branch=main\n", encoding="utf-8")
    second_thread = threading.Thread(target=second_worker)
    second_thread.start()
    while not second_thread.is_alive():
        pass
    assert not second_finished.is_set()
    release_first.set()
    first_thread.join()
    assert second_finished.wait()
    assert not errors
    assert len(first_results) == 1
    assert len(second_results) == 1
    assert first_results[0].run_id == second_results[0].run_id
    assert second_results[0].idempotent_replay is True
    assert admission_path.is_file()
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchone()
        events = conn.execute(
            "SELECT sequence, event_kind FROM scheduler_events ORDER BY sequence"
        ).fetchall()
    assert int(run_count[0]) == 1
    assert int(event_count[0]) == 2
    assert int(reservation_count[0]) == 1
    assert [(int(row[0]), str(row[1])) for row in events] == [
        (1, "run_submitted"),
        (2, "run_authorized"),
    ]


def test_concurrent_sequence_start_overlaps_publication_temp_file(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    first_holding_temp = threading.Event()
    release_first_writer = threading.Event()
    second_finished = threading.Event()
    original_replace = os.replace

    def gated_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".part") and not first_holding_temp.is_set():
            first_holding_temp.set()
            release_first_writer.wait()
        original_replace(src, dst)

    first_results: list[object] = []
    second_results: list[object] = []
    errors: list[Exception] = []

    def first_worker() -> None:
        try:
            with patch(
                "ai_dev_loop.scheduler.infrastructure.protected_artifacts.os.replace",
                side_effect=gated_replace,
            ):
                first_results.append(_start_service(scheduler_paths).start(sequence_id))
        except Exception as exc:
            errors.append(exc)

    def second_worker() -> None:
        try:
            second_results.append(_start_service(scheduler_paths).start(sequence_id))
        except Exception as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=first_worker)
    first_thread.start()
    assert first_holding_temp.wait()
    second_thread = threading.Thread(target=second_worker)
    second_thread.start()
    while not second_thread.is_alive():
        pass
    assert not second_finished.is_set()
    release_first_writer.set()
    first_thread.join()
    assert second_finished.wait()
    assert not errors
    assert len(first_results) == 1
    assert len(second_results) == 1
    assert first_results[0].run_id == second_results[0].run_id
    assert second_results[0].idempotent_replay is True
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchone()
        events = conn.execute(
            "SELECT sequence, event_kind FROM scheduler_events ORDER BY sequence"
        ).fetchall()
    assert int(run_count[0]) == 1
    assert int(event_count[0]) == 2
    assert int(reservation_count[0]) == 1
    assert [(int(row[0]), str(row[1])) for row in events] == [
        (1, "run_submitted"),
        (2, "run_authorized"),
    ]


def test_abandoned_publication_temp_blocks_sequence_start_without_deletion(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(prepared, PreparedSequenceState)
    run_id = prepared.definition.entries[0].planned_run_id
    root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    (root / "plan").mkdir(parents=True)
    abandoned = root / "plan" / ".tmp-abandoned.part"
    abandoned.write_text("stale", encoding="utf-8")
    with pytest.raises(SchedulerEngineError, match="materialization failed"):
        _start_service(scheduler_paths).start(sequence_id)
    assert abandoned.exists()
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert isinstance(sequence, PreparedSequenceState)
    assert int(run_count[0]) == 0


def test_build_sequence_run_context_uses_run_relative_artifact_paths(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        prepared = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_sequence_state(
            conn,
            sequence_id,
        )
    assert isinstance(prepared, PreparedSequenceState)
    entry = prepared.definition.entries[0]
    context = build_sequence_run_context(
        definition=prepared.definition,
        entry=entry,
        entry_hash=frozen_entry_hash(entry),
    )
    assert context.plan_prompt.plan_artifact_path == "plan/plan.md"
    assert context.plan_prompt.prompt_artifact_path == "prompts/cursor-initial.txt"
    assert "entries/" not in context.plan_prompt.plan_artifact_path
