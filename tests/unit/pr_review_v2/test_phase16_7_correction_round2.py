"""Phase 16.7 correction round-2 regressions (findings 1-6)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_3, SHA_A

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
    ExternalAdjudicationDecision,
    ExternalAdjudicationResultArtifact,
    LocalFixResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    ArtifactRef,
    EffectCompletionToken,
    LocalFixOutcomeKind,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    RunLocalFixEffect,
)
from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded, LocalFixFinishedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    FakeCodexProcessRunner,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CARRIER_FIX_PROMPT_RELATIVE,
    LocalFixAdapter,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.local_executor import (
    LocalEffectExecutor,
    StoreBackedContextResolver,
)
from ai_dev_loop.pr_review_v2.workers.owned_children import (
    OwnedChildMetadata,
    OwnedChildStore,
    signal_owned_child,
    validate_owned_child_against_os,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    PrReviewV2Supervisor,
)
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, sha256_bytes

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-corr2-1"
PLAN_BYTES = b"frozen-plan-for-corr2\n"
PROMPT_BYTES = b"frozen-prompt-for-corr2\n"
PLAN_SHA = sha256_bytes(PLAN_BYTES)
PROMPT_SHA = sha256_bytes(PROMPT_BYTES)
THREAD_ID = "PRRT_thread_1"


def _ctx(*, repo_root: str) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="feature",
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
            review_model="gpt-5.5",
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
        repository_root=repo_root,
    )


def test_local_fix_adapter_real_boundary_marks_carrier_completed(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 1: real Phase 16.2 boundary — do not monkeypatch run_local_review_fix."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "plans").mkdir()
    (repo / "plans" / "x.md").write_bytes(PLAN_BYTES)
    (repo / "prompts").mkdir()
    (repo / "prompts" / "prompt.txt").write_bytes(PROMPT_BYTES)
    subprocess.check_call(["git", "add", "app.py", "plans", "prompts"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo, stdout=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root=str(repo.resolve()))
    # Align expected head/branch with live repo.
    ctx = ctx.model_copy(
        update={
            "run_binding": ctx.run_binding.model_copy(
                update={"expected_head_sha": head.lower(), "head_branch": "main"}
            )
        }
    )
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_prompt = b"Codex adjudicated fix: update app.py\n"
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=fix_prompt.decode())

    effect = RunLocalFixEffect(
        effect_id="pr-review:run-corr2-1:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-corr2-1:cycle:01:run_local_fix",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=head.lower(),
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="main",
            base_branch="main",
            head_sha=head.lower(),
        ),
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    adapter = LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)
    outcome = adapter.execute(
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert isinstance(outcome, LocalFixFinishedOutcome)
    assert outcome.outcome is LocalFixOutcomeKind.ACCEPTED
    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    _path, state = load_run(carrier_id)
    assert state.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_RESIDUAL_RISK}
    assert (_path / CARRIER_FIX_PROMPT_RELATIVE).read_bytes() == fix_prompt
    cached = store.read_cached_local_fix_result(effect)
    assert cached is not None
    assert cached.needs_external_continuation is True


def test_adjudication_and_local_fix_cached_replay(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root="/tmp/repo")
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    snap_bytes = b'{"trigger_marker":"m1"}\n'
    snap_ref = store.persist_patch_bytes(run_id=RUN_ID, data=snap_bytes)
    adj_artifact = ExternalAdjudicationResultArtifact(
        decisions=(
            ExternalAdjudicationDecision(
                thread_id=THREAD_ID,
                decision=AdjudicationDecisionKind.ACTIONABLE,
                safe_summary="needs fix",
                reply_body=None,
            ),
        ),
        fix_prompt_text="Please fix",
        run_id=RUN_ID,
        cycle_number=1,
        effect_id="pr-review:run-corr2-1:cycle:01:adjudicate_threads",
        bound_head_sha=SHA_A,
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref_sha256=snap_ref.sha256,
        execution_context_ref_sha256=ctx_ref.sha256,
    )
    store.persist_external_adjudication(run_id=RUN_ID, artifact=adj_artifact)
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=1,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    effect = AdjudicateThreadsEffect(
        effect_id=adj_artifact.effect_id,
        idempotency_key=adj_artifact.effect_id,
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=binding,
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref=snap_ref,
        execution_context_ref=ctx_ref,
    )
    assert store.read_cached_external_adjudication(effect) is not None
    miss = store.read_cached_external_adjudication(
        AdjudicateThreadsEffect(
            effect_id=effect.effect_id,
            idempotency_key=effect.idempotency_key,
            run_id=RUN_ID,
            cycle_number=1,
            attempt=1,
            max_attempts=6,
            repository=effect.repository,
            bound_head_sha=SHA_A,
            binding=binding,
            frozen_thread_ids=(THREAD_ID,),
            snapshot_ref=ArtifactRef(
                relative_path="local/patches/" + HASH_3 + ".patch", sha256=HASH_3
            ),
            execution_context_ref=ctx_ref,
        )
    )
    assert miss is None

    real_patch = store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n")
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text="fix me")
    local_art = LocalFixResultArtifact(
        outcome=LocalFixOutcomeKind.ACCEPTED,
        accepted_patch_sha256=real_patch.sha256,
        new_head_sha=SHA_A,
        carrier_run_id="prv2c-abc",
        cursor_chat_id="chat-1",
        codex_session_id=SESSION,
        iteration_count=1,
        result_message_safe="ok",
        needs_external_continuation=True,
        run_id=RUN_ID,
        cycle_number=1,
        effect_id="pr-review:run-corr2-1:cycle:01:run_local_fix",
        bound_head_sha=SHA_A,
        fix_prompt_ref_sha256=fix_ref.sha256,
        execution_context_ref_sha256=ctx_ref.sha256,
    )
    store.persist_local_fix_result(run_id=RUN_ID, artifact=local_art)
    local_effect = RunLocalFixEffect(
        effect_id=local_art.effect_id,
        idempotency_key=local_art.effect_id,
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=binding,
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    assert store.read_cached_local_fix_result(local_effect) is not None

    calls = {"local": 0}

    class _CountingAdapter:
        def execute(self, **kwargs):
            calls["local"] += 1
            raise AssertionError("must not reopen carrier on cache hit")

    fake = FakeCodexProcessRunner(result_payload={})
    executor = LocalEffectExecutor(
        store=store,
        publication_runner=PublicationTextRunner(
            artifact_root=store.root, process_runner=fake, timeout_seconds=5
        ),
        adjudication_runner=ThreadAdjudicationRunner(
            artifact_root=store.root, process_runner=fake, timeout_seconds=5
        ),
        local_fix_adapter=_CountingAdapter(),  # type: ignore[arg-type]
        context_resolver=StoreBackedContextResolver(store, ref_for_run={RUN_ID: ctx_ref}),
    )
    token = EffectCompletionToken(
        effect_id=local_effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=1,
        bound_head_sha=SHA_A,
    )
    result = executor.execute(local_effect, token, now=T0)
    assert isinstance(result, EffectSucceeded)
    assert calls["local"] == 0


def test_owned_child_abort_refuses_mismatched_ownership(tmp_path: Path) -> None:
    store = OwnedChildStore(tmp_path / "artifacts")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.05)
        pgid = os.getpgid(proc.pid)
        with open(f"/proc/{proc.pid}/stat", encoding="utf-8") as handle:
            start = handle.read().split()[21]
        exe = str(Path(f"/proc/{proc.pid}/exe").resolve())
        good = OwnedChildMetadata(
            schema_version=1,
            run_id=RUN_ID,
            component="codex",
            pid=proc.pid,
            pgid=pgid,
            process_start_time=str(start),
            executable=exe,
            parent_pid=os.getpid(),
            created_at=T0.isoformat(),
        )
        store.write(good)
        assert validate_owned_child_against_os(good, run_id=RUN_ID)
        assert signal_owned_child(good, run_id=RUN_ID) == "terminated"
        bad = good.__class__(
            schema_version=1,
            run_id=RUN_ID,
            component="cursor",
            pid=proc.pid,
            pgid=pgid,
            process_start_time="1",
            executable=exe,
            parent_pid=os.getpid(),
            created_at=T0.isoformat(),
        )
        assert signal_owned_child(bad, run_id=RUN_ID) == "refused"
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)


def test_supervisor_waits_for_future_retry_timer(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    sleeps: list[float] = []

    class _Engine:
        def __init__(self) -> None:
            self._kind = "waiting_retry"
            self._due = T0 + timedelta(seconds=5)
            self.fired = 0

        def fire_due_timers_for_run(self, run_id: str, *, limit: int = 100) -> list:
            del run_id, limit
            self.fired += 1
            if clock.now() >= self._due:
                self._kind = "paused"
            return []

        def get_status(self, run_id: str):
            del run_id

            class S:
                state_kind = self._kind
                next_eligible_at = self._due if self._kind == "waiting_retry" else None
                lease_active = False
                active_effect_kind = None

            return S()

    class _Worker:
        def run_once(self, run_id: str):
            del run_id

            class R:
                claimed = False

            return R()

    engine = _Engine()
    cancel = {"n": 0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        cancel["n"] += 1

    supervisor = PrReviewV2Supervisor(
        engine,  # type: ignore[arg-type]
        _Worker(),  # type: ignore[arg-type]
        run_id=RUN_ID,
        idle_poll_seconds=1.0,
        clock=clock,
        cancel_check=lambda: cancel["n"] > 10,
        sleep=sleep,
    )
    kind = supervisor.run_until_idle()
    assert kind in {"paused", "waiting_retry"}
    assert sleeps
    assert all(s <= 1.0 for s in sleeps)
    assert engine.fired >= 1


def test_source_plan_prompt_exact_copy_and_drift(tmp_path: Path) -> None:
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "engine.sqlite3"),
        clock=FakeClock(T0),
        ids=SequenceIdFactory(prefix="prep"),
    )
    store = ProtectedResultStore(tmp_path / "artifacts")
    prep = PreparationService(engine, store, clock=FakeClock(T0))
    ctx = _ctx(repo_root="/tmp/repo")
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    plan = store.read_source_plan_bytes(run_id=created.run_id, expected_sha256=PLAN_SHA)
    prompt = store.read_source_prompt_bytes(run_id=created.run_id, expected_sha256=PROMPT_SHA)
    assert plan == PLAN_BYTES
    assert prompt == PROMPT_BYTES
    with pytest.raises(Exception, match="hash mismatch|missing"):
        store.read_source_plan_bytes(run_id=created.run_id, expected_sha256=HASH_1)
    with pytest.raises(Exception, match="missing"):
        store.read_source_prompt_bytes(run_id="missing-run", expected_sha256=PROMPT_SHA)
