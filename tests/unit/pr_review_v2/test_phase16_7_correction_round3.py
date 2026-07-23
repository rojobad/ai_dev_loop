"""Phase 16.7 correction round-3 regressions (findings 1-7)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A

from ai_dev_loop.abort_control import register_active_process
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.local_review_loop import LocalReviewFixResult, LocalReviewOutcome
from ai_dev_loop.paths import DIR_MODE, ensure_dir, run_dir
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import AbortProcessAction
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
    AdjudicationEvidence,
    ArtifactRef,
    FrozenThreadSet,
    LocalFixOutcomeKind,
    PullRequestBinding,
    RepositoryIdentity,
    ThreadDecisionRecord,
    TriggerEvidence,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
    RunLocalFixEffect,
)
from ai_dev_loop.pr_review_v2.domain.events import LocalFixFinishedOutcome
from ai_dev_loop.pr_review_v2.domain.state import RunningLocalFixState, active_effect
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    MAX_CODEX_RESULT_FILE_BYTES,
    CodexLocalRunnerError,
    FakeCodexProcessRunner,
    PublicationTextRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import extract_remote_nwo
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CarrierSeed,
    LocalFixAdapter,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    LOCAL_FIX_RESULT_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.runtime_factory import (
    ProcessCodexRunner,
    build_minimal_codex_env,
)
from ai_dev_loop.pr_review_v2.workers.local_executor import LocalEffectExecutor
from ai_dev_loop.pr_review_v2.workers.owned_children import (
    OwnedChildMetadata,
    OwnedChildStore,
    register_owned_child,
    signal_owned_child,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import SupervisorLauncherStore
from ai_dev_loop.pr_review_v2_carrier import CARRIER_PROJECT, FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    save_run_state,
    sha256_bytes,
    utc_now,
)

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-corr3-1"
PLAN_BYTES = b"frozen-plan-for-corr3\n"
PROMPT_BYTES = b"frozen-prompt-for-corr3\n"
PLAN_SHA = sha256_bytes(PLAN_BYTES)
PROMPT_SHA = sha256_bytes(PROMPT_BYTES)
THREAD_ID = "PRRT_thread_1"
CHAT_CREATED = "chat-created-by-first-fix"


def _ctx(
    *,
    repo_root: str,
    chat_id: str | None = "chat-1",
    cursor_sandbox: str = "disabled",
    codex_sandbox: str = "workspace-write",
    output_format: str = "stream-json",
    force: bool = True,
    trust_workspace: bool = True,
    cursor_timeout: int = 90,
    codex_timeout: int = 90,
) -> ExecutionContextArtifact:
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
            chat_id=chat_id,
            model="composer-2.5-fast",
            command="agent",
            output_format=output_format,
            force=force,
            trust_workspace=trust_workspace,
            sandbox=cursor_sandbox,
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
            review_model="gpt-5.5",
            review_reasoning_effort="medium",
            command="codex",
            sandbox=codex_sandbox,
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=cursor_timeout,
            codex_timeout_minutes=codex_timeout,
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


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
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
    return repo, head.lower()


def test_terminal_carrier_replay_without_resume(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    repo, head = _git_repo(tmp_path)
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root=str(repo.resolve()))
    ctx = ctx.model_copy(
        update={
            "run_binding": ctx.run_binding.model_copy(
                update={"expected_head_sha": head, "head_branch": "main"}
            )
        }
    )
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_prompt = b"Codex adjudicated fix: update app.py\n"
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=fix_prompt.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-corr3-1:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-corr3-1:cycle:01:run_local_fix",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=head,
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="main",
            base_branch="main",
            head_sha=head,
        ),
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    adapter = LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)
    first = adapter.execute(
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert first.outcome is LocalFixOutcomeKind.ACCEPTED
    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    _path, state = load_run(carrier_id)
    assert state.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_RESIDUAL_RISK}

    from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root

    result_dir = run_artifact_root(store.root, RUN_ID) / LOCAL_FIX_RESULT_DIR
    removed = 0
    for path in result_dir.glob("*.json"):
        path.unlink()
        removed += 1
    assert removed >= 1
    assert store.read_cached_local_fix_result(effect) is None

    calls = {"n": 0}

    def _boom(request):  # noqa: ANN001
        calls["n"] += 1
        raise AssertionError("RESUME must not run for terminal-carrier replay")

    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter.run_local_review_fix",
        _boom,
    )
    second = adapter.execute(
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert calls["n"] == 0
    assert second.outcome is LocalFixOutcomeKind.ACCEPTED
    assert second.accepted_patch_ref is not None
    cached = store.read_cached_local_fix_result(effect)
    assert cached is not None
    assert cached.cursor_chat_id == state.cursor.chat_id


def test_carrier_seed_uses_nondefault_frozen_context(tmp_path: Path, isolated_xdg) -> None:
    repo, head = _git_repo(tmp_path)
    seed = CarrierSeed(
        v2_run_id=RUN_ID,
        cycle_number=1,
        effect_id="eff-1",
        repository_root=str(repo.resolve()),
        plan_path="plans/x.md",
        prompt_path="prompts/prompt.txt",
        plan_sha256=PLAN_SHA,
        prompt_sha256=PROMPT_SHA,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        cursor_chat_id="chat-1",
        cursor_model="composer-2.5-fast",
        cursor_command="agent",
        cursor_output_format="json",
        cursor_force=False,
        cursor_trust_workspace=False,
        cursor_sandbox="enabled",
        codex_session_id=SESSION,
        codex_command="codex",
        review_model="gpt-5",
        review_reasoning_effort="medium",
        review_skill="review-staged-cursor-execution",
        codex_sandbox="read-only",
        max_local_iterations=3,
        cursor_timeout_minutes=17,
        codex_timeout_minutes=23,
        bound_head_sha=head,
        expected_branch="main",
        fix_prompt_bytes=b"fix-nondefault",
    )
    cid = carrier_run_id(RUN_ID, 1, "eff-1")
    FilesystemLocalCarrierRuntime().ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    _path, state = load_run(cid)
    assert state.cursor.sandbox == "enabled"
    assert state.cursor.output_format == "json"
    assert state.cursor.force is False
    assert state.cursor.trust_workspace is False
    assert state.codex.sandbox == "read-only"
    assert state.workflow.cursor_timeout_minutes == 17
    assert state.workflow.codex_timeout_minutes == 23


def test_content_addressed_fix_prompts_and_replies_no_collision(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    a = store.persist_fix_prompt(run_id=RUN_ID, text="fix cycle 1 distinct body")
    b = store.persist_fix_prompt(run_id=RUN_ID, text="fix cycle 2 different body")
    assert a.relative_path != b.relative_path
    assert a.sha256 != b.sha256
    assert a.relative_path.startswith("local/fix-prompts/")
    hint = hashlib.sha256(b"same-thread").hexdigest()[:16]
    r1 = store.persist_reply_text(run_id=RUN_ID, relative_hint=hint, text="reply-1")
    r2 = store.persist_reply_text(run_id=RUN_ID, relative_hint=hint, text="reply-2")
    assert r1.relative_path != r2.relative_path

    ctx = _ctx(repo_root="/tmp/repo")
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    snap = store.persist_patch_bytes(run_id=RUN_ID, data=b'{"eligible_threads":[]}\n')
    executor = LocalEffectExecutor(
        store=store,
        publication_runner=object(),  # type: ignore[arg-type]
        adjudication_runner=object(),  # type: ignore[arg-type]
        local_fix_adapter=object(),  # type: ignore[arg-type]
        context_resolver=object(),  # type: ignore[arg-type]
    )
    effect = AdjudicateThreadsEffect(
        effect_id="pr-review:run-corr3-1:cycle:01:adjudicate_threads",
        idempotency_key="pr-review:run-corr3-1:cycle:01:adjudicate_threads",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="feature",
            base_branch="main",
            head_sha=SHA_A,
        ),
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref=snap,
        execution_context_ref=ctx_ref,
    )
    artifact = ExternalAdjudicationResultArtifact(
        decisions=(
            ExternalAdjudicationDecision(
                thread_id=THREAD_ID,
                decision=AdjudicationDecisionKind.ACTIONABLE,
                safe_summary="needs fix",
                reply_body=None,
            ),
        ),
        fix_prompt_text="fix cycle 1 distinct body",
        run_id=RUN_ID,
        cycle_number=1,
        effect_id=effect.effect_id,
        bound_head_sha=SHA_A,
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref_sha256=snap.sha256,
        execution_context_ref_sha256=ctx_ref.sha256,
    )
    out1 = executor._adjudication_outcome(  # noqa: SLF001
        effect=effect,
        result=artifact,
        result_ref=ArtifactRef(relative_path="x.json", sha256=HASH_2),
        trigger_marker="m1",
    )
    effect2 = effect.model_copy(
        update={
            "cycle_number": 2,
            "effect_id": "pr-review:run-corr3-1:cycle:02:adjudicate_threads",
            "idempotency_key": "pr-review:run-corr3-1:cycle:02:adjudicate_threads",
        }
    )
    artifact2 = artifact.model_copy(
        update={
            "cycle_number": 2,
            "effect_id": effect2.effect_id,
            "fix_prompt_text": "fix cycle 2 different body",
        }
    )
    out2 = executor._adjudication_outcome(  # noqa: SLF001
        effect=effect2,
        result=artifact2,
        result_ref=ArtifactRef(relative_path="y.json", sha256=HASH_1),
        trigger_marker="m2",
    )
    assert out1.evidence.fix_prompt_ref is not None
    assert out2.evidence.fix_prompt_ref is not None
    assert out1.evidence.fix_prompt_ref.relative_path != out2.evidence.fix_prompt_ref.relative_path
    # Distinct reply bodies under the same thread-id hint must not collide.
    assert r1.sha256 != r2.sha256
    assert r1.relative_path != r2.relative_path


def test_chat_continuity_from_prior_accepted_result(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    artifact = LocalFixResultArtifact(
        outcome=LocalFixOutcomeKind.ACCEPTED,
        accepted_patch_sha256=HASH_1,
        new_head_sha=SHA_A,
        carrier_run_id="prv2c-abc",
        cursor_chat_id=CHAT_CREATED,
        codex_session_id=SESSION,
        iteration_count=1,
        result_message_safe="ok",
        needs_external_continuation=True,
        run_id=RUN_ID,
        cycle_number=1,
        effect_id="eff-1",
        bound_head_sha=SHA_A,
        fix_prompt_ref_sha256=HASH_1,
        execution_context_ref_sha256=HASH_2,
    )
    store.persist_local_fix_result(run_id=RUN_ID, artifact=artifact)
    assert store.latest_accepted_cursor_chat_id(run_id=RUN_ID, before_cycle=2) == CHAT_CREATED
    assert store.latest_accepted_cursor_chat_id(run_id=RUN_ID, before_cycle=1) is None

    seeds: list = []

    class _Carrier:
        def ensure_seeded_carrier(self, *, carrier_run_id: str, seed) -> None:
            seeds.append(seed)

        def carrier_exists(self, carrier_run_id: str) -> bool:
            return False

        def carrier_has_progress(self, carrier_run_id: str) -> bool:
            return False

        def current_head_sha(self, repo_root: str) -> str:
            return SHA_A

        def read_verified_staged_patch_bytes(
            self, carrier_run_id: str, relative_path: str
        ) -> bytes:
            return b"diff"

        def read_terminal_acceptance(self, carrier_run_id: str, *, expected_session_id: str):
            return None

        def verify_carrier_bindings(self, *, carrier_run_id: str, seed) -> None:
            return None

    ctx = _ctx(repo_root="/tmp/repo", chat_id=None)
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text="next fix")
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-corr3-1:cycle:02:run_local_fix",
        idempotency_key="pr-review:run-corr3-1:cycle:02:run_local_fix",
        run_id=RUN_ID,
        cycle_number=2,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="feature",
            base_branch="main",
            head_sha=SHA_A,
        ),
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    adapter = LocalFixAdapter(runtime=_Carrier(), store=store)

    import ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter as adapter_mod

    def _ok(request):  # noqa: ANN001
        return LocalReviewFixResult(
            run_id=request.run_id,
            status="completed",
            chat_id=CHAT_CREATED,
            iteration_count=1,
            latest_staged_diff_path="git/diffs/01.patch",
            latest_review_path=None,
            result_message="ok",
            outcome=LocalReviewOutcome.ACCEPTED,
            needs_external_continuation=True,
        )

    original = adapter_mod.run_local_review_fix
    adapter_mod.run_local_review_fix = _ok  # type: ignore[assignment]
    try:
        outcome = adapter.execute(
            run_id=RUN_ID,
            effect=effect,
            execution_context=ctx,
            fix_prompt_bytes=b"next fix",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )
    finally:
        adapter_mod.run_local_review_fix = original  # type: ignore[assignment]
    assert isinstance(outcome, LocalFixFinishedOutcome)
    assert seeds and seeds[0].cursor_chat_id == CHAT_CREATED


def test_reject_https_and_malformed_publication_remotes() -> None:
    from ai_dev_loop.commands.pr_review_v2 import _owner_repo_from_remote

    assert extract_remote_nwo("git@github.com:acme/demo.git") == "acme/demo"
    assert extract_remote_nwo("ssh://git@github.com/acme/demo.git") == "acme/demo"
    assert extract_remote_nwo("https://github.com/acme/demo.git") is None
    assert extract_remote_nwo("not-a-remote") is None
    with pytest.raises(ValidationError, match="SSH"):
        _owner_repo_from_remote("https://github.com/acme/demo.git")
    with pytest.raises(ValidationError, match="SSH"):
        _owner_repo_from_remote("git@github.com:acme")
    assert _owner_repo_from_remote("git@github.com:acme/demo.git") == "acme/demo"


def test_process_codex_runner_minimal_env_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_DEV_LOOP_SENTINEL_SECRET", "should-not-leak")
    env = build_minimal_codex_env()
    assert "AI_DEV_LOOP_SENTINEL_SECRET" not in env
    assert "PATH" in env

    runner = ProcessCodexRunner(
        artifact_root=tmp_path / "artifacts",
        run_id=RUN_ID,
        max_capture_bytes=64,
        term_grace_seconds=0.05,
    )
    with pytest.raises(AiDevLoopError, match="capture bound"):
        runner.run(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*200)"],
            cwd=str(tmp_path),
            stdin_text="",
            timeout_seconds=5,
        )
    children = OwnedChildStore(tmp_path / "artifacts")
    assert children.read(RUN_ID, "codex") is None

    timed = ProcessCodexRunner(
        artifact_root=tmp_path / "artifacts",
        run_id=RUN_ID,
        term_grace_seconds=0.05,
    )
    result = timed.run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdin_text="",
        timeout_seconds=0.2,
    )
    assert result.timed_out is True

    refuse = ProcessCodexRunner(
        artifact_root=tmp_path / "artifacts",
        run_id=f"{RUN_ID}-kill",
        term_grace_seconds=0.05,
    )
    result2 = refuse.run(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        cwd=str(tmp_path),
        stdin_text="",
        timeout_seconds=0.2,
    )
    assert result2.timed_out is True

    class _Writer(FakeCodexProcessRunner):
        def run(self, argv, *, cwd, stdin_text, timeout_seconds):  # noqa: ANN001
            index = argv.index("--output-last-message")
            path = Path(argv[index + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{" + b"x" * (MAX_CODEX_RESULT_FILE_BYTES + 10))
            from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
                CodexProcessResult,
            )

            self.last_argv = list(argv)
            return CodexProcessResult(returncode=0, stdout_bytes=b"", stderr_bytes=b"")

    pub = PublicationTextRunner(
        artifact_root=tmp_path / "arts",
        process_runner=_Writer(result_payload={}),
        timeout_seconds=5,
    )
    ctx = _ctx(repo_root=str(tmp_path))
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-corr3-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-corr3-1:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="e.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="p.patch", sha256=HASH_2),
    )
    with pytest.raises(CodexLocalRunnerError, match="size bound"):
        pub.generate(
            run_id=RUN_ID,
            session_id=SESSION,
            repo_root=str(tmp_path),
            execution_context=ctx,
            evidence_ref=effect.evidence_ref,
            patch_ref=effect.patch_ref,
            effect=effect,
        )


def test_abort_during_local_fix_signals_cursor_and_codex(tmp_path: Path, isolated_xdg) -> None:
    clock = FakeClock(T0)
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "engine.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="ab3"),
        lease_ttl=timedelta(seconds=60),
    )
    arts = ProtectedResultStore(tmp_path / "artifacts")
    prep = PreparationService(engine, arts, clock=clock)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ctx = _ctx(repo_root=str(repo_root))
    ctx = ctx.model_copy(
        update={"run_binding": ctx.run_binding.model_copy(update={"source_run_id": "src-ab3"})}
    )
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-ab3",
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
    fix_ref = arts.persist_fix_prompt(run_id=created.run_id, text="abort-fix")
    with engine.store.begin_read() as conn:
        prepared, version, _updated = engine.store.load_validated_snapshot(conn, created.run_id)
    ctx_ref = prepared.origin.execution_context_ref
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    effect = RunLocalFixEffect(
        effect_id=f"pr-review:{created.run_id}:cycle:01:run_local_fix",
        idempotency_key=f"pr-review:{created.run_id}:cycle:01:run_local_fix",
        run_id=created.run_id,
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
    snap_ref = ArtifactRef(relative_path="local/snap.json", sha256=HASH_1)
    adj = AdjudicationEvidence(
        frozen=FrozenThreadSet(
            thread_ids=(THREAD_ID,),
            snapshot_ref=snap_ref,
            head_sha=SHA_A,
            cycle_number=1,
            trigger_marker="marker-1",
        ),
        decisions=(
            ThreadDecisionRecord(
                thread_id=THREAD_ID,
                decision=AdjudicationDecisionKind.ACTIONABLE,
                safe_summary="thread_actionable",
                reply_ref=None,
            ),
        ),
        result_ref=ArtifactRef(relative_path="local/adj.json", sha256=HASH_2),
        fix_prompt_ref=fix_ref,
    )
    running = RunningLocalFixState(
        run_id=created.run_id,
        cycle_number=1,
        origin=prepared.origin,
        limits=WorkflowLimits(max_external_cycles=8, max_local_iterations=3),
        binding=binding,
        entered_at=T0,
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        active_effect=effect,
        trigger_evidence=TriggerEvidence(
            marker="marker-1",
            comment_ref=ArtifactRef(relative_path="local/trig.json", sha256=HASH_1),
            head_sha=SHA_A,
        ),
        adjudication=adj,
    )
    with engine.store.begin_immediate() as conn:
        engine.store.cas_update_snapshot(
            conn,
            run_id=created.run_id,
            observed_version=version,
            new_state=running,
            now=T0,
        )

    carrier_id = carrier_run_id(created.run_id, 1, effect.effect_id)
    carrier_dir = run_dir(CARRIER_PROJECT, carrier_id)
    ensure_dir(carrier_dir, mode=DIR_MODE)
    now = utc_now()
    save_run_state(
        carrier_dir,
        RunState(
            run_id=carrier_id,
            project=ProjectRef(name=CARRIER_PROJECT),
            status=RunStatus.RUNNING_CURSOR,
            created_at=now,
            updated_at=now,
            repository=RepositoryState(
                root=str(repo_root),
                git_common_dir=str(repo_root / ".git"),
                git_dir=str(repo_root / ".git"),
                branch="main",
                initial_head=SHA_A,
                baseline_status_path="git/baseline-status.txt",
            ),
            plan=PlanState(
                repository_path="plans/x.md",
                snapshot_path="plan/plan.md",
                sha256=PLAN_SHA,
            ),
            prompt=PromptState(
                source_repository_path="prompts/prompt.txt",
                snapshot_path="prompts/cursor-initial.txt",
                sha256=PROMPT_SHA,
            ),
            codex=CodexState(
                command="codex",
                session_id=SESSION,
                session_model="gpt-5",
                session_reasoning_effort="medium",
                review_model="gpt-5",
                review_reasoning_effort="medium",
                review_model_source="explicit",
                review_reasoning_source="explicit",
                review_skill="review-staged-cursor-execution",
                sandbox="workspace-write",
            ),
            cursor=CursorState(
                command="agent",
                model="composer-2.5-fast",
                output_format="stream-json",
                force=True,
                trust_workspace=True,
                sandbox="disabled",
                chat_id="chat-abort",
            ),
            workflow=WorkflowState(
                max_review_iterations=3,
                current_review_iteration=0,
                stage_mode="all",
                cursor_timeout_minutes=90,
                codex_timeout_minutes=90,
            ),
        ),
    )

    cursor_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    codex_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        register_active_process(
            carrier_dir,
            run_id=carrier_id,
            component="cursor",
            iteration=1,
            pid=cursor_proc.pid,
            pgid=os.getpgid(cursor_proc.pid),
            parent_pid=os.getpid(),
            cwd=str(repo_root),
            argv_redacted=["agent", "resume"],
        )
        children = OwnedChildStore(arts.root)
        register_owned_child(
            children,
            run_id=created.run_id,
            component="codex",
            pid=codex_proc.pid,
            pgid=os.getpgid(codex_proc.pid),
            executable=str(Path(sys.executable).resolve()),
        )
        bad = OwnedChildMetadata(
            schema_version=1,
            run_id="other-run",
            component="codex",
            pid=codex_proc.pid,
            pgid=os.getpgid(codex_proc.pid),
            process_start_time=read_process_starttime(codex_proc.pid) or "1",
            executable=str(Path(sys.executable).resolve()),
            parent_pid=os.getpid(),
            created_at=T0.isoformat(),
        )
        assert signal_owned_child(bad, run_id=created.run_id) == "refused"

        control = ControlPlaneService(
            engine,
            artifact_store=arts,
            launcher_store=SupervisorLauncherStore(arts.root),
            spawner=lambda _rid: "spawned",
        )
        with engine.store.begin_read() as conn:
            pre, _, _ = engine.store.load_validated_snapshot(conn, created.run_id)
        assert isinstance(active_effect(pre), RunLocalFixEffect)

        result = control.abort(created.run_id)
        assert result.abort_persisted is True
        assert result.state_kind == "aborted"
        assert result.process_action is AbortProcessAction.TERMINATED
        deadline = time.time() + 5
        while time.time() < deadline and (cursor_proc.poll() is None or codex_proc.poll() is None):
            time.sleep(0.05)
        assert cursor_proc.poll() is not None
        assert codex_proc.poll() is not None
        assert (carrier_dir / "locks" / "abort-request.json").is_file()
    finally:
        for proc in (cursor_proc, codex_proc):
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
