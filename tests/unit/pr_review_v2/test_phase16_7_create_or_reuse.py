"""Unit tests for Phase 16.7 create-or-reuse and control-plane basics."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import HASH_1, SHA_A

from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    ControlError,
    ControlErrorKind,
    SafeNextAction,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
)
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.supervisor import SupervisorLauncherStore

PLAN_BYTES = b"frozen-plan-for-tests\n"
PROMPT_BYTES = b"frozen-prompt-for-tests\n"
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()
PROMPT_SHA = hashlib.sha256(PROMPT_BYTES).hexdigest()
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"


def _ctx(*, source_run_id: str = "src-1", head_branch: str = "feature") -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id=source_run_id,
            repository="acme/demo",
            head_branch=head_branch,
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            chat_id="chat-1",
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
            review_model="gpt-5",
            review_reasoning_effort="medium",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command="gh",
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=("chatgpt-codex-connector",),
            review_trigger_body="@codex review",
            user_mention="rojobad",
            poll_interval_seconds=60,
            max_external_cycles=8,
            per_call_timeout_seconds=60,
            overall_timeout_seconds=180,
            max_pages=20,
            max_items=500,
            max_server_directed_wait_seconds=3600,
            no_findings_enabled=False,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
            accepted_patch_sha256=HASH_1,
        ),
        repository_root="/tmp/repo",
    )


@pytest.fixture
def engine(tmp_path: Path) -> PrReviewEngine:
    store = SqlitePrReviewStore(tmp_path / "engine.sqlite3")
    return PrReviewEngine(store, clock=FakeClock(T0), ids=SequenceIdFactory(prefix="t"))


def test_create_or_reuse_identical_prepared_identity(
    engine: PrReviewEngine, tmp_path: Path
) -> None:
    arts = ProtectedResultStore(tmp_path / "artifacts")
    prep = PreparationService(engine, arts, clock=FakeClock(T0))
    snap = SourceRunSnapshot(
        source_run_id="src-1",
        repository="acme/demo",
        head_branch="feature",
        base_branch="main",
        expected_head_sha=SHA_A,
        accepted_patch_bytes=b"diff --git a/x b/x\n",
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        execution_context=_ctx(),
    )
    first = prep.create_from_source(snap)
    second = prep.create_from_source(snap)
    assert first.reused is False
    assert second.reused is True
    assert first.run_id == second.run_id
    assert first.next_action is SafeNextAction.START


def test_create_or_reuse_rejects_source_run_conflict(
    engine: PrReviewEngine, tmp_path: Path
) -> None:
    arts = ProtectedResultStore(tmp_path / "artifacts")
    prep = PreparationService(engine, arts, clock=FakeClock(T0))
    snap = SourceRunSnapshot(
        source_run_id="src-1",
        repository="acme/demo",
        head_branch="feature",
        base_branch="main",
        expected_head_sha=SHA_A,
        accepted_patch_bytes=b"diff --git a/x b/x\n",
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        execution_context=_ctx(),
    )
    prep.create_from_source(snap)
    conflict = SourceRunSnapshot(
        source_run_id="src-1",
        repository="acme/demo",
        head_branch="other",
        base_branch="main",
        expected_head_sha=SHA_A,
        accepted_patch_bytes=b"diff --git a/y b/y\n",
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        execution_context=_ctx(head_branch="other"),
    )
    with pytest.raises(ControlError) as exc:
        prep.create_from_source(conflict)
    assert exc.value.kind is ControlErrorKind.CONFLICT


def test_start_requires_prepared_and_is_idempotent_repair(
    engine: PrReviewEngine, tmp_path: Path
) -> None:
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(tmp_path / "artifacts")
    calls: list[str] = []

    def spawner(run_id: str) -> str:
        calls.append(run_id)
        return "spawned"

    control = ControlPlaneService(
        engine, artifact_store=arts, launcher_store=launchers, spawner=spawner
    )
    prep = PreparationService(engine, arts, clock=FakeClock(T0))
    result = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx(),
        )
    )
    started = control.start(result.run_id)
    assert started.transition_applied is True
    assert started.supervisor_action == "spawned"
    assert len(calls) == 1
    # Second start on non-prepared repairs/reuses without duplicating domain start.
    again = control.start(result.run_id)
    assert again.transition_applied is False
    assert again.supervisor_action in {"reused", "repaired", "spawned"}


def test_resume_prepared_requires_start(engine: PrReviewEngine, tmp_path: Path) -> None:
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(tmp_path / "artifacts")
    control = ControlPlaneService(engine, artifact_store=arts, launcher_store=launchers)
    prep = PreparationService(engine, arts, clock=FakeClock(T0))
    result = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx(),
        )
    )
    with pytest.raises(ControlError) as exc:
        control.resume(result.run_id)
    assert exc.value.kind is ControlErrorKind.NOT_RESUMABLE
    assert exc.value.next_action == SafeNextAction.START.value


def test_history_is_bounded(engine: PrReviewEngine, tmp_path: Path) -> None:
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(tmp_path / "artifacts")
    control = ControlPlaneService(
        engine, artifact_store=arts, launcher_store=launchers, spawner=lambda r: "spawned"
    )
    prep = PreparationService(engine, arts, clock=FakeClock(T0))
    result = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx(),
        )
    )
    control.start(result.run_id)
    hist = control.history(result.run_id, limit=1, order="newest")
    assert len(hist.entries) == 1
    assert hist.truncated is True or hist.entries[0].event_kind == "start_requested"
