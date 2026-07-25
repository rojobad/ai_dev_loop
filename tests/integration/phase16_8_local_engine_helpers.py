"""Shared Phase 16.8 LOCAL engine/executor helpers for production-boundary tests."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from tests.integration.phase16_4_matrix_helpers import make_engine
from tests.integration.phase16_8_checkpoint_helpers import wait_for_path
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import MARKER
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A

from ai_dev_loop.pr_review_v2.application.contracts import (
    EventDisposition,
    EventSubmission,
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
from ai_dev_loop.pr_review_v2.application.github_read import (
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedReviewThread,
    ObservedTriggerComment,
)
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.domain import StartRequested
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    MAX_CODEX_RESULT_FILE_BYTES,
    CodexProcessResult,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ReviewArtifactStore,
    sha256_text,
)
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.local_executor import (
    LocalEffectExecutor,
    StoreBackedContextResolver,
)
from ai_dev_loop.state import sha256_bytes

RUN_ID = "run-p168-local-engine"
SESSION = "11111111-1111-1111-1111-111111111111"
PROMPT_SENTINEL = "SENTINEL_PROMPT_BODY_DO_NOT_LEAK_987654321"
THREAD_ID = "PRRT_thread_1"
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)

EffectKind = Literal["publication", "adjudication"]
LocalCodexCrashCase = Literal[
    "pre_spawn_failure",
    "active_stream_timeout",
    "malformed_json",
    "cache_replay",
    "nonzero_exit",
    "schema_mismatch",
    "oversized_output",
    "execution_context_drift",
    "head_sha_drift",
    "cycle_drift",
    "thread_set_drift",
    "persist_before_complete_reopen",
    "lease_loss",
    "expired_claim_resume",
    "term_refusal_kill",
]

LOCAL_CODEX_CRASH_CASES: tuple[LocalCodexCrashCase, ...] = (
    "pre_spawn_failure",
    "active_stream_timeout",
    "malformed_json",
    "cache_replay",
    "nonzero_exit",
    "schema_mismatch",
    "oversized_output",
    "execution_context_drift",
    "head_sha_drift",
    "cycle_drift",
    "thread_set_drift",
    "persist_before_complete_reopen",
    "lease_loss",
    "expired_claim_resume",
    "term_refusal_kill",
)


class _PreSpawnFailureRunner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult:
        del cwd, stdin_text, timeout_seconds
        raise FileNotFoundError(f"executable not found: {argv[0]}")


class _CapturingRunner:
    def __init__(
        self,
        *,
        result_payload: dict[str, object] | None = None,
        malformed: bool = False,
        returncode: int = 0,
        oversized: bool = False,
    ) -> None:
        self.result_payload = result_payload or {}
        self.malformed = malformed
        self.returncode = returncode
        self.oversized = oversized
        self.invocations = 0
        self.last_stdin: str | None = None
        self.last_argv: list[str] | None = None

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult:
        del cwd, timeout_seconds
        self.invocations += 1
        self.last_argv = list(argv)
        self.last_stdin = stdin_text
        if "--output-last-message" not in argv:
            raise AssertionError("missing --output-last-message")
        index = argv.index("--output-last-message")
        result_path = Path(argv[index + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if self.malformed:
            result_path.write_text("{not-valid-json", encoding="utf-8")
        elif self.oversized:
            result_path.write_text(
                "{" + ("x" * (MAX_CODEX_RESULT_FILE_BYTES + 16)) + "}", encoding="utf-8"
            )
        else:
            result_path.write_text(
                json.dumps(self.result_payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        return CodexProcessResult(
            returncode=self.returncode,
            stdout_bytes=b"",
            stderr_bytes=b"" if self.returncode == 0 else b"failed",
        )


class _BlockingCodexProcessRunner:
    def __init__(
        self,
        *,
        codex_path: Path,
        ready_path: Path,
        proceed_path: Path,
        result_payload: dict[str, object],
        timeout_seconds: float,
        release_after_ready: bool = False,
    ) -> None:
        self._codex_path = codex_path
        self._ready_path = ready_path
        self._proceed_path = proceed_path
        self._result_payload = result_payload
        self._timeout_seconds = timeout_seconds
        self._release_after_ready = release_after_ready
        self.last_argv: list[str] | None = None
        self.last_stdin: str | None = None
        self.invocations = 0

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult:
        del timeout_seconds
        self.last_argv = list(argv)
        self.last_stdin = stdin_text
        self.invocations += 1
        if "--output-last-message" not in argv:
            raise AssertionError("missing --output-last-message")
        index = argv.index("--output-last-message")
        result_path = Path(argv[index + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(self._result_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if self._proceed_path.is_file():
            self._proceed_path.unlink()
        proc = subprocess.Popen(
            [str(self._codex_path), "exec", "resume", SESSION, "-"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        def _wait_ready() -> None:
            wait_for_path(self._ready_path, timeout_seconds=5.0)

        waiter = threading.Thread(target=_wait_ready)
        waiter.start()
        waiter.join(timeout=5.0)
        if self._release_after_ready:
            self._proceed_path.write_text("go\n", encoding="utf-8")
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=self._timeout_seconds)
            returncode = proc.returncode or 0
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return CodexProcessResult(
                returncode=1,
                stdout_bytes=b"",
                stderr_bytes=b"timed out",
                timed_out=True,
            )
        return CodexProcessResult(
            returncode=returncode,
            stdout_bytes=stdout.encode(),
            stderr_bytes=stderr.encode(),
        )


class _FakeCarrier:
    def ensure_seeded_carrier(self, *, carrier_run_id: str, seed) -> None:
        return None

    def carrier_exists(self, carrier_run_id: str) -> bool:
        return False

    def carrier_has_progress(self, carrier_run_id: str) -> bool:
        return False

    def current_head_sha(self, repo_root: str) -> str:
        return SHA_A

    def read_verified_staged_patch_bytes(self, carrier_run_id: str, relative_path: str) -> bytes:
        return b"diff"

    def read_terminal_acceptance(self, carrier_run_id: str, *, expected_session_id: str):
        return None

    def verify_carrier_bindings(self, *, carrier_run_id: str, seed) -> None:
        return None


class ArtifactRefHolder:
    ctx_ref: object = None


def ctx(*, repo_root: str) -> ExecutionContextArtifact:
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
            plan_sha256=HASH_1,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=HASH_2,
        ),
        repository_root=repo_root,
    )


def publication_payload() -> dict[str, object]:
    return {
        "title": "Publication title",
        "body": "Publication body",
        "commit_subject": "Publish feature",
        "commit_body": "Body",
    }


def adjudication_payload() -> dict[str, object]:
    return {
        "decisions": [
            {
                "thread_id": THREAD_ID,
                "decision": "actionable",
                "safe_summary": "needs fix",
                "reply_body": None,
            }
        ],
        "fix_prompt_text": "Please fix",
    }


def local_fixture(
    tmp_path: Path,
    *,
    effect_kind: EffectKind,
) -> tuple[
    ProtectedResultStore,
    ExecutionContextArtifact,
    GeneratePublicationTextEffect | AdjudicateThreadsEffect,
    EffectCompletionToken,
    ArtifactRefHolder,
]:
    from ai_dev_loop.pr_review_v2.application.execution_context import LocalFixResultArtifact

    store = ProtectedResultStore(tmp_path / "artifacts")
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    ctx_artifact = ctx(repo_root=str(repo))
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx_artifact)
    store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n")
    store.persist_source_plan_bytes(run_id=RUN_ID, data=f"plan {PROMPT_SENTINEL}\n".encode())
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=f"{PROMPT_SENTINEL}\n".encode())
    evidence = store.persist_local_fix_result(
        run_id=RUN_ID,
        artifact=LocalFixResultArtifact(
            outcome="accepted",
            accepted_patch_sha256=HASH_1,
            new_head_sha=SHA_A,
            carrier_run_id="prv2c-abc",
            cursor_chat_id="chat-1",
            codex_session_id=SESSION,
            iteration_count=1,
            result_message_safe="ok",
            needs_external_continuation=True,
            run_id=RUN_ID,
            cycle_number=1,
            effect_id="e1",
            bound_head_sha=SHA_A,
            fix_prompt_ref_sha256=HASH_1,
            execution_context_ref_sha256=HASH_2,
        ),
    )
    pr_binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=1,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    token_holder = ArtifactRefHolder()

    if effect_kind == "publication":
        effect = GeneratePublicationTextEffect(
            effect_id="pr-review:run-p168-local-codex:cycle:01:generate_publication_text",
            idempotency_key="pr-review:run-p168-local-codex:cycle:01:generate_publication_text",
            run_id=RUN_ID,
            cycle_number=1,
            attempt=1,
            max_attempts=6,
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            bound_head_sha=SHA_A,
            evidence_ref=evidence,
            patch_ref=store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n"),
        )
    else:
        snapshot = ObservationSnapshot(
            binding=pr_binding,
            cycle_number=1,
            poll_sequence=1,
            trigger_marker=MARKER,
            observed_at=T0,
            evidence_kind=ObservationEvidenceKind.ELIGIBLE_THREADS,
            trigger=ObservedTriggerComment(
                comment_id="101",
                author_login="orchestrator",
                created_at=T0,
                body_sha256=sha256_text(MARKER),
            ),
            eligible_threads=(
                ObservedReviewThread(
                    thread_id=THREAD_ID,
                    is_resolved=False,
                    author_login="chatgpt-codex-connector",
                    created_at=T0,
                    commit_sha=SHA_A,
                    root_comment_id="RC_1",
                    root_body_sha256=sha256_text(PROMPT_SENTINEL),
                    sanitized_root_body="please fix",
                ),
            ),
        )
        snap_ref = ReviewArtifactStore(store.root).persist_observation_for_run(
            run_id=RUN_ID, snapshot=snapshot
        )
        effect = AdjudicateThreadsEffect(
            effect_id="pr-review:run-p168-local-codex:cycle:01:adjudicate_threads",
            idempotency_key="pr-review:run-p168-local-codex:cycle:01:adjudicate_threads",
            run_id=RUN_ID,
            cycle_number=1,
            attempt=1,
            max_attempts=6,
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            bound_head_sha=SHA_A,
            binding=pr_binding,
            frozen_thread_ids=(THREAD_ID,),
            snapshot_ref=snap_ref,
            execution_context_ref=ctx_ref,
        )
    token = EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=1,
        bound_head_sha=SHA_A,
    )
    token_holder.ctx_ref = ctx_ref
    return store, ctx_artifact, effect, token, token_holder


def make_executor(
    store: ProtectedResultStore,
    *,
    ctx_ref,
    process_runner,
    run_id: str = RUN_ID,
    timeout_seconds: float = 30,
) -> LocalEffectExecutor:
    pub = PublicationTextRunner(
        artifact_root=store.root, process_runner=process_runner, timeout_seconds=timeout_seconds
    )
    adj = ThreadAdjudicationRunner(
        artifact_root=store.root, process_runner=process_runner, timeout_seconds=timeout_seconds
    )
    adapter = LocalFixAdapter(runtime=_FakeCarrier(), store=store)
    return LocalEffectExecutor(
        store=store,
        publication_runner=pub,
        adjudication_runner=adj,
        local_fix_adapter=adapter,
        context_resolver=StoreBackedContextResolver(store, ref_for_run={run_id: ctx_ref}),
    )


def assert_safe_fields_exclude_sentinels(result: object, forbidden: tuple[str, ...]) -> None:
    if hasattr(result, "model_dump"):
        text = json.dumps(result.model_dump(mode="json"), default=str)
    else:
        text = str(result)
    for needle in forbidden:
        assert needle not in text, f"{needle!r} leaked into operational fields"


def augment_stdin(stdin_text: str) -> str:
    return f"{stdin_text}\n{PROMPT_SENTINEL}\n{SESSION}\n"


def assert_stdin_contains_sentinels(runner: _CapturingRunner | _BlockingCodexProcessRunner) -> None:
    assert runner.last_stdin is not None
    assert runner.last_argv is not None
    assert SESSION in runner.last_argv
    assert "--last" not in runner.last_argv


def assert_exact_session_argv(runner: _CapturingRunner | _BlockingCodexProcessRunner) -> None:
    assert runner.last_argv is not None
    assert SESSION in runner.last_argv
    assert "--last" not in runner.last_argv


def build_prepared_local_engine(
    tmp_path: Path,
    clock: FakeClock,
    *,
    repo_root: str,
    prefix: str = "p168-local-eng",
) -> tuple[PrReviewEngine, str, ProtectedResultStore, object]:
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix=prefix)
    store = ProtectedResultStore(tmp_path / "artifacts")
    prep = PreparationService(engine, store, clock=clock)
    source_run_id = "src-local-eng"
    plan_bytes = f"plan {PROMPT_SENTINEL}\n".encode()
    prompt_bytes = f"{PROMPT_SENTINEL}\n".encode()
    ctx_artifact = ctx(repo_root=repo_root)
    ctx_artifact = ctx_artifact.model_copy(
        update={
            "run_binding": ctx_artifact.run_binding.model_copy(
                update={"source_run_id": source_run_id}
            ),
            "plan_prompt": ctx_artifact.plan_prompt.model_copy(
                update={
                    "plan_sha256": sha256_bytes(plan_bytes),
                    "prompt_sha256": sha256_bytes(prompt_bytes),
                }
            ),
        }
    )
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id=source_run_id,
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
            execution_context=ctx_artifact,
        )
    )
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="start-local-eng",
            run_id=created.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    with engine.store.begin_read() as conn:
        state, _version, _updated = engine.store.load_validated_snapshot(conn, created.run_id)
    ctx_ref = state.origin.execution_context_ref
    return engine, created.run_id, store, ctx_ref


def build_local_worker(
    engine: PrReviewEngine,
    store: ProtectedResultStore,
    ctx_ref,
    process_runner,
    *,
    run_id: str,
) -> EffectWorker:
    executor = make_executor(store, ctx_ref=ctx_ref, process_runner=process_runner, run_id=run_id)
    executor._context_resolver = StoreBackedContextResolver(store, ref_for_run={run_id: ctx_ref})  # noqa: SLF001
    return EffectWorker(engine, executor, owner_id="owner-local")


def complete_publication_worker_step(
    engine: PrReviewEngine,
    worker: EffectWorker,
    run_id: str,
) -> None:
    step = worker.run_once(run_id)
    assert step.completed is True
    assert step.effect_kind == "generate_publication_text"
