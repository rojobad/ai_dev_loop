"""Regression tests for Phase 17.2 tick fencing, admission, and controller read fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state
from tests.unit.scheduler.test_tick import (
    FakeGitAdmissionPort,
    _bootstrap_run,
    _claim_ids,
    _event_ids,
)

from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.scheduler.application.git_admission import (
    BoundedGitAdmissionPort,
    GitAdmissionResult,
)
from ai_dev_loop.scheduler.application.start import StartService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.admission_contract import ADMISSION_STATUS_ARTIFACT
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _tick(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    fake: FakeGitAdmissionPort,
    *,
    now: datetime | None = None,
    owner: str = "tick-owner-a",
    lease_ttl: int = 60,
) -> TickService:
    base = now or datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    return TickService(
        store,
        artifacts,
        fake,
        now_factory=lambda: base,
        tick_owner_factory=lambda: owner,
        event_id_factory=_event_ids(),
        claim_id_factory=_claim_ids(),
        lease_ttl_seconds=lease_ttl,
    )


def test_bounded_git_admission_returns_redacted_timeout() -> None:
    port = BoundedGitAdmissionPort(timeout_seconds=1.0)
    with patch(
        "ai_dev_loop.scheduler.application.git_admission.run_process",
        return_value=ProcessResult(
            args=["git", "rev-parse", "--show-toplevel"],
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=True,
        ),
    ):
        result = port.admit(repository_root="/tmp/repo", require_clean_worktree=True)
    assert result.ok is False
    assert result.failure_kind == "git_timeout"
    assert result.failure_summary == "git repository probe timed out"


def test_stale_admission_claim_blocks_without_git_rerun(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    fake = FakeGitAdmissionPort(resolved_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_immediate() as conn:
        first = store.acquire_global_tick_lease(
            conn,
            owner_id="interrupted-owner",
            now=now,
            ttl_seconds=60,
        )
        assert first is not None
        generation, _ = first
        assert store.acquire_admission_tick_claim(
            conn,
            claim_id="stale-admission-claim",
            run_id=run_id,
            tick_owner_id="interrupted-owner",
            tick_lease_generation=generation,
            expected_run_version=2,
            now=now,
        )
        store.release_admission_tick_claim(
            conn,
            claim_id="stale-admission-claim",
            now=now,
            stale=True,
            owner_id="interrupted-owner",
            lease_generation=generation,
        )
        store.release_global_tick_lease(
            conn,
            owner_id="interrupted-owner",
            generation=generation,
            now=now,
        )
    receipt = _tick(
        store,
        artifacts,
        fake,
        now=now + timedelta(seconds=1),
        owner="successor-owner",
    ).run_once()
    assert fake.calls == []
    assert any(
        item.action == "blocked" and item.detail == "admission_attempt_uncertain"
        for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"


def test_stale_capacity_holder_reconciled_before_effect(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    fake = FakeGitAdmissionPort(resolved_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_immediate() as conn:
        old = store.acquire_global_tick_lease(
            conn,
            owner_id="old-capacity-owner",
            now=now,
            ttl_seconds=60,
        )
        assert old is not None
        old_generation, _ = old
        now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = ?, holder_claim_id = ?, holder_tick_generation = ?, updated_at = ?
            WHERE capacity_name = 'global_active_agent'
            """,
            (run_id, "stale-capacity-claim", old_generation, now_text),
        )
        store.release_global_tick_lease(
            conn,
            owner_id="old-capacity-owner",
            generation=old_generation,
            now=now,
        )
    receipt = _tick(store, artifacts, fake, now=now + timedelta(seconds=1)).run_once()
    assert any(item.action == "synthetic_effect_completed" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_timer_stale_run_version_is_audited_not_applied(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_read() as conn:
        source = conn.execute(
            "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence ASC LIMIT 1",
            (run_id,),
        ).fetchone()
        assert source is not None
    with store.begin_immediate() as conn:
        store.insert_retry_timer(
            conn,
            timer_id="timer-stale-version",
            source_event_id=str(source[0]),
            run_id=run_id,
            due_at=now,
            target_effect_id="effect-1",
            expected_run_version=1,
            now=now,
        )
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner-a",
            now=now,
            ttl_seconds=60,
        )
        assert lease is not None
        generation, _ = lease
    tick = _tick(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        now=now,
    )
    receipt = tick._fire_due_timers("tick-owner-a", generation, run_id)
    assert any(item.action == "timer_stale" for item in receipt)
    with store.begin_read() as conn:
        timer = conn.execute(
            "SELECT status FROM scheduler_timers WHERE timer_id = ?",
            ("timer-stale-version",),
        ).fetchone()
        assert timer is not None
        assert timer[0] == "pending"
        events = conn.execute(
            """
            SELECT event_kind FROM scheduler_events
            WHERE run_id = ? AND event_kind = 'tick_stale_rejected'
            """,
            (run_id,),
        ).fetchall()
        assert events
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "authorized"


def test_stale_rejection_audit_does_not_block_authorized_run(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    base = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        acquired = store.acquire_global_tick_lease(
            conn,
            owner_id="owner-a",
            now=base,
            ttl_seconds=30,
        )
        assert acquired is not None
        generation, _ = acquired
    later = base + timedelta(seconds=60)
    tick = _tick(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        now=later,
    )
    receipts = tick._fire_due_timers("owner-a", generation, run_id)
    assert any(item.action == "timer_stale" for item in receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "authorized"


def test_existing_admission_artifact_blocks_without_git_rerun(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    artifacts.write_text(
        run_id,
        ADMISSION_STATUS_ARTIFACT,
        "branch=main\nhead=abc\nstatus_porcelain=\n",
        max_bytes=4096,
    )
    fake = FakeGitAdmissionPort(resolved_root=repo_root)
    receipt = _tick(store, artifacts, fake).run_once()
    assert fake.calls == []
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    assert any(
        item.action == "blocked" and item.detail == "admission_artifact_present"
        for item in receipt.run_receipts
    )


def test_admission_git_failure_does_not_skip_other_runs(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    repo_a = str(tmp_path / "repo-a")
    repo_b = str(tmp_path / "repo-b")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def insert_run(run_id: str, repo_root: str, idempotency_suffix: str) -> None:
        base = sample_submitted_state(run_id=run_id, repo_root=repo_root)
        state = base.model_copy(update={"idempotency_key": f"{idempotency_suffix}{'b' * 56}"})
        event = RunSubmittedEvent(
            run_id=state.run_id,
            idempotency_key=state.idempotency_key,
            worktree_key=state.context.repository.worktree_key,
            reused_existing=False,
        )
        with store.begin_immediate() as conn:
            store.insert_submitted_run(
                conn,
                run_id=state.run_id,
                state=state,
                event_id=f"evt-submit-{run_id}",
                event=event,
                now=now,
            )
        StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC)).start(
            run_id,
            CONTROLLER_SESSION,
        )

    insert_run("run-a", repo_a, "aaaaaaaa")
    insert_run("run-b", repo_b, "bbbbbbbb")

    class SelectiveFake(FakeGitAdmissionPort):
        def admit(
            self,
            *,
            repository_root: str,
            require_clean_worktree: bool,
        ) -> GitAdmissionResult:
            if repository_root == repo_a:
                return GitAdmissionResult(
                    ok=False,
                    failure_kind="dirty_worktree",
                    failure_summary="worktree is not clean",
                )
            return super().admit(
                repository_root=repository_root,
                require_clean_worktree=require_clean_worktree,
            )

    receipt = _tick(store, artifacts, SelectiveFake(resolved_root=repo_b)).run_once()
    actions = {item.run_id: item.action for item in receipt.run_receipts}
    run_b_actions = [item.action for item in receipt.run_receipts if item.run_id == "run-b"]
    assert actions["run-a"] == "blocked"
    assert "admitted" in run_b_actions


def test_admission_artifact_boundary_access_failure_blocks_and_continues_tick(
    tmp_path: Path,
) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    repo_a = str(tmp_path / "repo-a")
    repo_b = str(tmp_path / "repo-b")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def insert_run(run_id: str, repo_root: str, idempotency_suffix: str) -> None:
        base = sample_submitted_state(run_id=run_id, repo_root=repo_root)
        state = base.model_copy(update={"idempotency_key": f"{idempotency_suffix}{'b' * 56}"})
        event = RunSubmittedEvent(
            run_id=state.run_id,
            idempotency_key=state.idempotency_key,
            worktree_key=state.context.repository.worktree_key,
            reused_existing=False,
        )
        with store.begin_immediate() as conn:
            store.insert_submitted_run(
                conn,
                run_id=state.run_id,
                state=state,
                event_id=f"evt-submit-{run_id}",
                event=event,
                now=now,
            )
        StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC)).start(
            run_id,
            CONTROLLER_SESSION,
        )

    insert_run("run-a", repo_a, "aaaaaaaa")
    insert_run("run-b", repo_b, "bbbbbbbb")

    original_run_root = artifacts.run_root
    calls_for_run_a: list[str] = []

    def guarded_run_root(run_id: str) -> Path:
        if run_id == "run-a":
            calls_for_run_a.append(run_id)
            raise PermissionError(13, "Permission denied", str(tmp_path / "secret" / "artifacts"))
        return original_run_root(run_id)

    artifacts.run_root = guarded_run_root  # type: ignore[method-assign]
    fake = FakeGitAdmissionPort(resolved_root=repo_b)
    receipt = _tick(store, artifacts, fake).run_once()

    assert calls_for_run_a == ["run-a"]
    assert all(repository_root != repo_a for repository_root, _ in fake.calls)
    blocked = next(item for item in receipt.run_receipts if item.run_id == "run-a")
    assert blocked.action == "blocked"
    assert blocked.detail == "admission_artifact_access_failed"
    assert "secret" not in (blocked.detail or "")
    run_b_actions = [item.action for item in receipt.run_receipts if item.run_id == "run-b"]
    assert "admitted" in run_b_actions
    with store.begin_read() as conn:
        state_a, _, _ = store.load_validated_snapshot(conn, "run-a")
        assert state_a.kind == "blocked"


def test_protected_artifact_write_failure_blocks_and_preserves_artifact(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    original_write = artifacts.write_text

    def failing_write(
        run_id_arg: str,
        relative_path: str,
        text: str,
        *,
        max_bytes: int,
    ):
        if relative_path == ADMISSION_STATUS_ARTIFACT:
            raise ProtectedArtifactError("artifact write rejected")
        return original_write(run_id_arg, relative_path, text, max_bytes=max_bytes)

    artifacts.write_text = failing_write  # type: ignore[method-assign]
    receipt = _tick(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
    ).run_once()
    assert any(
        item.action == "blocked" and item.detail == "admission_artifact_write_failed"
        for item in receipt.run_receipts
    )
    assert not (artifacts.run_root(run_id) / ADMISSION_STATUS_ARTIFACT).exists()


def test_admission_port_failure_blocks_and_allows_single_git_call(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)

    class FailingPort(FakeGitAdmissionPort):
        def admit(
            self,
            *,
            repository_root: str,
            require_clean_worktree: bool,
        ) -> GitAdmissionResult:
            self.calls.append((repository_root, require_clean_worktree))
            raise RuntimeError("simulated admission port failure")

    fake = FailingPort(resolved_root=repo_root)
    first = _tick(store, artifacts, fake).run_once()
    assert len(fake.calls) == 1
    assert any(
        item.action == "blocked" and item.detail == "admission_port_failed"
        for item in first.run_receipts
    )
    second = _tick(store, artifacts, fake).run_once()
    assert len(fake.calls) == 1
    assert second.run_receipts == ()


def test_complete_claimed_effect_requires_exact_run_version(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    receipt = _tick(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
    ).run_once()
    assert any(item.action == "synthetic_effect_completed" for item in receipt.run_receipts)
    now = datetime(2026, 9, 4, 12, 3, tzinfo=UTC)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        advanced = state.model_copy(update={"version": version + 1})
        assert store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=advanced,
            now=now,
        )
        row = conn.execute(
            "SELECT dispatch_id, claim_id FROM scheduler_effects WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        dispatch_id = str(row[0])
        claim_id = str(row[1])
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = 'claimed', claim_id = ?, claim_owner_id = ?,
                claim_lease_generation = ?, claimed_run_version = ?, updated_at = ?
            WHERE dispatch_id = ?
            """,
            (
                claim_id,
                "replay-owner",
                5,
                version,
                now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                dispatch_id,
            ),
        )
        completed = store.complete_claimed_effect(
            conn,
            dispatch_id=dispatch_id,
            claim_id=claim_id,
            tick_owner_id="replay-owner",
            tick_lease_generation=5,
            expected_run_version=version,
            now=now,
        )
        assert completed is False


def test_admission_artifact_os_error_blocks_without_leaking_path(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    original_write = artifacts.write_text

    def failing_write(
        run_id_arg: str,
        relative_path: str,
        text: str,
        *,
        max_bytes: int,
    ):
        if relative_path == ADMISSION_STATUS_ARTIFACT:
            raise OSError(13, "Permission denied", str(tmp_path / "secret" / "path"))
        return original_write(run_id_arg, relative_path, text, max_bytes=max_bytes)

    artifacts.write_text = failing_write  # type: ignore[method-assign]
    receipt = _tick(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
    ).run_once()
    blocked = next(item for item in receipt.run_receipts if item.action == "blocked")
    assert blocked.detail == "admission_artifact_write_failed"
    assert "secret" not in (blocked.detail or "")
    assert not (artifacts.run_root(run_id) / ADMISSION_STATUS_ARTIFACT).exists()


def test_stale_owner_cannot_release_successor_capacity(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, _, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_immediate() as conn:
        first = store.acquire_global_tick_lease(
            conn,
            owner_id="owner-a",
            now=now,
            ttl_seconds=60,
        )
        assert first is not None
        gen_a, _ = first
        assert store.try_acquire_capacity(
            conn,
            run_id=run_id,
            claim_id="claim-a",
            tick_owner_id="owner-a",
            tick_lease_generation=gen_a,
            now=now,
        )
        store.release_global_tick_lease(
            conn,
            owner_id="owner-a",
            generation=gen_a,
            now=now,
        )
        second = store.acquire_global_tick_lease(
            conn,
            owner_id="owner-b",
            now=now + timedelta(seconds=1),
            ttl_seconds=60,
        )
        assert second is not None
        gen_b, _ = second
        assert store.try_acquire_capacity(
            conn,
            run_id=run_id,
            claim_id="claim-b",
            tick_owner_id="owner-b",
            tick_lease_generation=gen_b,
            now=now + timedelta(seconds=1),
        )
        released = store.release_capacity(
            conn,
            run_id=run_id,
            claim_id="claim-a",
            tick_owner_id="owner-a",
            tick_lease_generation=gen_a,
            now=now + timedelta(seconds=1),
        )
        assert released is False
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id
        assert int(capacity["holder_tick_generation"]) == gen_b


def test_controller_status_rejects_corrupt_scheduler_snapshot(
    tmp_path: Path,
    git_repo: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    repo_root = str(git_repo.resolve())
    state = sample_submitted_state(repo_root=repo_root)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
    StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC)).start(
        state.run_id,
        CONTROLLER_SESSION,
    )
    with store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_runs SET state_payload_sha256 = ? WHERE run_id = ?",
            ("0" * 64, state.run_id),
        )
    with patch(
        "ai_dev_loop.scheduler.application.controller_read.default_engine_db_path",
        return_value=db,
    ):
        status = controller_status(
            controller_session_id=CONTROLLER_SESSION,
            repo_path=git_repo,
            run_id=state.run_id,
        )
    assert status.read_failure is not None
    assert status.match_count == 0
    assert "corrupt" in status.next_safe_action.lower()


def test_controller_lookup_read_failure_on_tampered_controller_identity(
    tmp_path: Path,
    git_repo: Path,
) -> None:
    from io import StringIO
    from unittest.mock import patch

    from tests.conftest import FIXTURE_REPO
    from tests.integration.test_phase17_2_controller_lookup import _prepare_legacy

    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    repo_root = str(git_repo.resolve())
    state = sample_submitted_state(repo_root=repo_root)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
    StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC)).start(
        state.run_id,
        CONTROLLER_SESSION,
    )
    with store.begin_immediate() as conn:
        row = store.get_run_row(conn, state.run_id)
        loaded = store.load_state(row["state_payload"])
        tampered_context = loaded.context.model_copy(
            update={
                "controller": loaded.context.controller.model_copy(
                    update={"controller_session_id": "22222222-2222-2222-2222-222222222222"}
                )
            }
        )
        tampered = loaded.model_copy(update={"context": tampered_context})
        kind, payload, digest = store.dump_state(tampered)
        conn.execute(
            """
            UPDATE scheduler_runs
            SET state_payload = ?, state_payload_sha256 = ?
            WHERE run_id = ?
            """,
            (payload, digest, state.run_id),
        )

    legacy_root = tmp_path / "legacy_xdg"
    legacy_root.mkdir()
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with (
        patch.dict(
            "os.environ",
            {
                "XDG_STATE_HOME": str(legacy_root / "state"),
                "XDG_CONFIG_HOME": str(legacy_root / "config"),
                "XDG_CACHE_HOME": str(legacy_root / "cache"),
            },
        ),
        patch("sys.stdin", StringIO(prompt)),
    ):
        legacy = _prepare_legacy(git_repo)

    with patch(
        "ai_dev_loop.scheduler.application.controller_read.default_engine_db_path",
        return_value=db,
    ):
        status = controller_status(
            controller_session_id=CONTROLLER_SESSION,
            repo_path=git_repo,
        )
    assert status.read_failure is not None
    assert legacy.run_id
    assert status.match_count == 0
