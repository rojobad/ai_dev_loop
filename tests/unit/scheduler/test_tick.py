"""Unit tests for scheduler tick service and Git admission port."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state

from ai_dev_loop.scheduler.application.git_admission import GitAdmissionEvidence, GitAdmissionResult
from ai_dev_loop.scheduler.application.start import StartService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.admission_contract import ADMISSION_STATUS_ARTIFACT
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class FakeGitAdmissionPort:
    def __init__(
        self,
        *,
        ok: bool = True,
        resolved_root: str | None = None,
        require_clean: bool = True,
        status_porcelain: str = "",
        staged: bool = False,
    ) -> None:
        self.calls: list[tuple[str, bool]] = []
        self._ok = ok
        self._resolved_root = resolved_root
        self._status = status_porcelain
        self._staged = staged
        self._require_clean = require_clean

    def admit(
        self,
        *,
        repository_root: str,
        require_clean_worktree: bool,
    ) -> GitAdmissionResult:
        self.calls.append((repository_root, require_clean_worktree))
        root = self._resolved_root or repository_root
        evidence = GitAdmissionEvidence(
            resolved_root=root,
            branch="main",
            head="abc123",
            status_porcelain=self._status,
        )
        if not self._ok or root != repository_root:
            return GitAdmissionResult(
                ok=False,
                evidence=evidence,
                failure_kind="repository_root_mismatch",
                failure_summary="resolved repository root does not match submitted target",
            )
        if require_clean_worktree and self._staged:
            return GitAdmissionResult(
                ok=False,
                evidence=evidence,
                failure_kind="dirty_worktree",
                failure_summary="worktree has pre-existing staged changes",
            )
        if require_clean_worktree and self._status.strip():
            return GitAdmissionResult(
                ok=False,
                evidence=evidence,
                failure_kind="dirty_worktree",
                failure_summary="worktree is not clean",
            )
        text = f"branch=main\nhead=abc123\nstatus_porcelain={self._status}\n"
        return GitAdmissionResult(ok=True, evidence=evidence, artifact_text=text)


def _bootstrap_run(
    tmp_path: Path,
    *,
    repo_root: str = "/tmp/repo",
    require_clean: bool = True,
) -> tuple[SqliteSchedulerStore, ProtectedArtifactStore, str]:
    db = tmp_path / "engine.sqlite3"
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    store = SqliteSchedulerStore(db)
    state = sample_submitted_state(repo_root=repo_root)
    if not require_clean:
        state = state.model_copy(
            update={
                "context": state.context.model_copy(
                    update={
                        "workflow": state.context.workflow.model_copy(
                            update={"require_clean_worktree": False}
                        )
                    }
                )
            }
        )
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
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
    return store, artifacts, state.run_id


def test_tick_service_has_no_subprocess_or_sleep() -> None:
    source = inspect.getsource(TickService)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "sleep":
                raise AssertionError("TickService must not call sleep")
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"Popen", "run", "call"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                raise AssertionError("TickService must not invoke subprocess")


def test_authorized_tick_admits_with_fake_git(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    fake = FakeGitAdmissionPort(resolved_root=repo_root)
    tick = TickService(
        store,
        artifacts,
        fake,
        now_factory=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-owner-a",
        event_id_factory=_event_ids(),
        claim_id_factory=_claim_ids(),
    )
    receipt = tick.run_once()
    assert receipt.lease_acquired is True
    assert any(item.action == "admitted" for item in receipt.run_receipts)
    artifact_path = artifacts.run_root(run_id) / ADMISSION_STATUS_ARTIFACT
    assert artifact_path.is_file()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "admitted"


def test_admission_blocks_dirty_worktree(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    fake = FakeGitAdmissionPort(resolved_root=repo_root, status_porcelain="?? dirty.txt")
    tick = TickService(
        store,
        artifacts,
        fake,
        now_factory=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-owner-a",
        event_id_factory=_event_ids(),
        claim_id_factory=_claim_ids(),
    )
    receipt = tick.run_once()
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"


def test_admission_allows_dirty_when_not_required(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(
        tmp_path,
        repo_root=repo_root,
        require_clean=False,
    )
    fake = FakeGitAdmissionPort(
        resolved_root=repo_root,
        status_porcelain="?? dirty.txt",
    )
    tick = TickService(
        store,
        artifacts,
        fake,
        now_factory=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-owner-a",
        event_id_factory=_event_ids(),
        claim_id_factory=_claim_ids(),
    )
    receipt = tick.run_once()
    assert any(item.action == "admitted" for item in receipt.run_receipts)


def test_empty_tick_on_queued_run_only(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state()
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
    tick = TickService(
        store,
        ProtectedArtifactStore(tmp_path / "artifacts"),
        FakeGitAdmissionPort(),
        now_factory=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-owner-a",
    )
    receipt = tick.run_once()
    assert receipt.visited_runs == 1
    assert receipt.run_receipts == ()


def test_tick_lease_contention_while_active(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_immediate() as conn:
        first = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner-a",
            now=now,
            ttl_seconds=60,
        )
        assert first is not None
        second = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner-b",
            now=now,
            ttl_seconds=60,
        )
        assert second is None


def test_synthetic_effect_claims_and_releases_capacity(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    fake = FakeGitAdmissionPort(resolved_root=repo_root)
    tick = TickService(
        store,
        artifacts,
        fake,
        now_factory=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-owner-a",
        event_id_factory=_event_ids(),
        claim_id_factory=_claim_ids(),
    )
    receipt = tick.run_once()
    assert any(item.action == "admitted" for item in receipt.run_receipts)
    assert any(item.action == "synthetic_effect_completed" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def _event_ids():
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"evt-{counter['n']}"

    return factory


def _claim_ids():
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"clm-{counter['n']}"

    return factory
