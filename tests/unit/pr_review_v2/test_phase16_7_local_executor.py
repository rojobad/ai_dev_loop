"""LocalEffectExecutor routing and publication caching tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A, artifact

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
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    EffectCompletionToken,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect
from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded, PublicationTextPreparedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    FakeCodexProcessRunner,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.effect_executor_router import (
    EffectExecutorRouter,
    UnsupportedRoutedEffectError,
)
from ai_dev_loop.pr_review_v2.workers.local_executor import (
    LocalEffectExecutor,
    StoreBackedContextResolver,
)

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-local-1"


def _ctx() -> ExecutionContextArtifact:
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
            accepted_patch_sha256=HASH_1,
        ),
        repository_root="/tmp/repo",
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
        del carrier_run_id, expected_session_id
        return None

    def verify_carrier_bindings(self, *, carrier_run_id: str, seed) -> None:
        del carrier_run_id, seed


def test_router_without_local_executor_still_refuses(tmp_path: Path) -> None:

    # Minimal stubs — constructor may require real deps; skip if unavailable.
    # Prefer exercising UnsupportedRoutedEffectError with None local executor.
    class _R:
        def execute(self, *a, **k):
            raise AssertionError("read")

    class _W:
        requires_authority = True

        def execute(self, *a, **k):
            raise AssertionError("write")

    class _C:
        requires_authority = True

        def execute(self, *a, **k):
            raise AssertionError("reconcile")

    router = EffectExecutorRouter(
        read_executor=_R(),  # type: ignore[arg-type]
        write_executor=_W(),  # type: ignore[arg-type]
        reconcile_executor=_C(),  # type: ignore[arg-type]
        local_executor=None,
    )
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-local-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-local-1:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=artifact("e.json"),
        patch_ref=artifact("p.patch"),
    )
    token = EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=1,
        bound_head_sha=SHA_A,
    )
    with pytest.raises(UnsupportedRoutedEffectError):
        router.execute(effect, token, now=T0, authority=None, claim=None)  # type: ignore[arg-type]


def test_publication_executor_uses_exact_session_and_caches(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx()
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n")
    evidence = store.persist_local_fix_result(
        run_id=RUN_ID,
        artifact=__import__(
            "ai_dev_loop.pr_review_v2.application.execution_context",
            fromlist=["LocalFixResultArtifact"],
        ).LocalFixResultArtifact(
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
    fake = FakeCodexProcessRunner(
        result_payload={
            "title": "Title",
            "body": "Body",
            "commit_subject": "Subject",
            "commit_body": "Commit body",
        }
    )
    pub = PublicationTextRunner(
        artifact_root=tmp_path / "artifacts", process_runner=fake, timeout_seconds=30
    )
    adj = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "artifacts", process_runner=fake, timeout_seconds=30
    )
    adapter = LocalFixAdapter(runtime=_FakeCarrier(), store=store)
    executor = LocalEffectExecutor(
        store=store,
        publication_runner=pub,
        adjudication_runner=adj,
        local_fix_adapter=adapter,
        context_resolver=StoreBackedContextResolver(store, ref_for_run={RUN_ID: ctx_ref}),
    )
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-local-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-local-1:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=evidence,
        patch_ref=ArtifactRef(relative_path="local/patches/" + HASH_1 + ".patch", sha256=HASH_1)
        if False
        else store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n"),
    )
    token = EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=1,
        bound_head_sha=SHA_A,
    )
    result = executor.execute(effect, token, now=T0)
    assert isinstance(result, EffectSucceeded)
    assert isinstance(result.outcome, PublicationTextPreparedOutcome)
    assert fake.last_argv is not None
    assert "resume" in fake.last_argv
    assert SESSION in fake.last_argv
    assert "--last" not in fake.last_argv

    # Cached replay
    fake2 = FakeCodexProcessRunner(result_payload={"title": "Other"})
    pub2 = PublicationTextRunner(
        artifact_root=tmp_path / "artifacts", process_runner=fake2, timeout_seconds=30
    )
    executor2 = LocalEffectExecutor(
        store=store,
        publication_runner=pub2,
        adjudication_runner=adj,
        local_fix_adapter=adapter,
        context_resolver=StoreBackedContextResolver(store, ref_for_run={RUN_ID: ctx_ref}),
    )
    again = executor2.execute(effect, token, now=T0)
    assert isinstance(again, EffectSucceeded)
    assert fake2.last_argv is None  # cache hit — runner not invoked
