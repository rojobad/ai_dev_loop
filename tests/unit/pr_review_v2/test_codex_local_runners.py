"""Unit tests for Codex local resume runners."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.paths import schema_path
from ai_dev_loop.pr_review_v2.application.control_contracts import OriginKind
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
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    FakeCodexProcessRunner,
    PublicationTextRunner,
    build_codex_resume_argv,
)

RUN_ID = "prv2-codex-001"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


def _execution_context() -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=OriginKind.EXISTING_PR,
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION_ID,
            review_model="gpt-5",
            review_reasoning_effort="high",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command="gh",
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=("chatgpt-codex-connector",),
            review_trigger_body="@codex review",
            user_mention="operator",
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
            plan_path="docs/plans/plan.md",
            plan_sha256=HASH_1,
            prompt_path="docs/plans/prompt.txt",
            prompt_sha256=HASH_2,
        ),
        repository_root="/tmp/repo",
    )


def _publication_effect() -> GeneratePublicationTextEffect:
    return GeneratePublicationTextEffect(
        effect_id="effect-publication-001",
        idempotency_key="idem-publication-001",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="local/evidence.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="local/patch.patch", sha256=HASH_2),
    )


def test_build_codex_resume_argv_contains_resume_and_session_id() -> None:
    argv = build_codex_resume_argv(
        codex_command="codex",
        repo_root="/tmp/repo",
        sandbox="workspace-write",
        session_id=SESSION_ID,
        review_model="gpt-5",
        review_reasoning_effort="high",
        schema_path_value=schema_path("pr-review-v2-publication-generation-v1.json"),
        result_path=Path("/tmp/result.json"),
    )
    assert "resume" in argv
    assert SESSION_ID in argv
    assert "--last" not in argv


def test_publication_runner_uses_resume_argv(tmp_path: Path) -> None:
    fake = FakeCodexProcessRunner(
        result_payload={
            "title": "Title",
            "body": "Body",
            "commit_subject": "Subject",
            "commit_body": "Commit body",
        }
    )
    runner = PublicationTextRunner(
        artifact_root=tmp_path / "art",
        process_runner=fake,
        timeout_seconds=30.0,
    )
    effect = _publication_effect()
    result = runner.generate(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        repo_root="/tmp/repo",
        execution_context=_execution_context(),
        evidence_ref=effect.evidence_ref,
        patch_ref=effect.patch_ref,
        effect=effect,
    )
    assert fake.last_argv is not None
    assert "resume" in fake.last_argv
    assert SESSION_ID in fake.last_argv
    assert "--last" not in fake.last_argv
    assert result.title == "Title"
    assert result.commit_subject == "Subject"


def test_publication_runner_rejects_session_mismatch(tmp_path: Path) -> None:
    runner = PublicationTextRunner(
        artifact_root=tmp_path / "art",
        process_runner=FakeCodexProcessRunner(),
        timeout_seconds=30.0,
    )
    with pytest.raises(Exception, match="session identity mismatch"):
        runner.generate(
            run_id=RUN_ID,
            session_id="33333333-3333-4333-8333-333333333333",
            repo_root="/tmp/repo",
            execution_context=_execution_context(),
            evidence_ref=_publication_effect().evidence_ref,
            patch_ref=_publication_effect().patch_ref,
            effect=_publication_effect(),
        )
