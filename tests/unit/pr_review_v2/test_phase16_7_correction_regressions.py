"""Correction-turn regressions for Phase 16.7 findings 1-9."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
    PublicationGenerationResultArtifact,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    ArtifactRef,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    MAX_ADJUDICATION_SNAPSHOT_BYTES,
    CodexLocalRunnerError,
    _adjudication_wrapper_prompt,
)
from ai_dev_loop.pr_review_v2.infrastructure.existing_pr_discovery import ExistingPrDiscoverer
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.local_executor import operational_adjudication_summary
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    validate_launcher_ownership,
    validate_launcher_ownership_against_os,
)

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
SHA_A = "a" * 40
SHA_B = "b" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64
HASH_3 = "3" * 64
RUN_ID = "run-corr-1"


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
            prompt_path="prompts/p.txt",
            prompt_sha256=HASH_2,
            accepted_patch_sha256=HASH_1,
        ),
        repository_root="/tmp/repo",
    )


def test_operational_summary_never_uses_model_text() -> None:
    assert operational_adjudication_summary(AdjudicationDecisionKind.ACTIONABLE) == (
        "thread_actionable"
    )
    assert "secret findings about auth" not in operational_adjudication_summary(
        AdjudicationDecisionKind.ACTIONABLE
    )


def test_launcher_ownership_refuses_stale_starttime(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = SupervisorLauncherMetadata(
        schema_version=1,
        run_id=RUN_ID,
        token="abc",
        pid=12345,
        pgid=12345,
        process_start_time="999",
        executable="/usr/bin/python3",
        created_at=T0.isoformat(),
    )
    assert validate_launcher_ownership(meta, run_id=RUN_ID)
    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.workers.supervisor.is_process_alive", lambda _pid: True
    )
    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.workers.supervisor.read_process_pgid", lambda _pid: 12345
    )
    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.workers.supervisor.read_process_starttime", lambda _pid: 1
    )
    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.workers.supervisor._read_process_executable",
        lambda _pid: "/usr/bin/python3",
    )
    assert not validate_launcher_ownership_against_os(meta, run_id=RUN_ID)


def test_publication_cache_is_effect_bound(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path)
    effect1 = GeneratePublicationTextEffect(
        effect_id="effect-pub-1",
        idempotency_key="idem-1",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="e1.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="p1.patch", sha256=HASH_2),
    )
    effect2 = GeneratePublicationTextEffect(
        effect_id="effect-pub-2",
        idempotency_key="idem-2",
        run_id=RUN_ID,
        cycle_number=2,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_B,
        evidence_ref=ArtifactRef(relative_path="e2.json", sha256=HASH_2),
        patch_ref=ArtifactRef(relative_path="p2.patch", sha256=HASH_3),
    )
    gen1 = PublicationGenerationResultArtifact(
        title="Initial",
        body="Body1",
        commit_subject="Initial subject",
        commit_body="c1",
        run_id=RUN_ID,
        cycle_number=1,
        effect_id=effect1.effect_id,
        bound_head_sha=SHA_A,
        evidence_ref_sha256=HASH_1,
        patch_ref_sha256=HASH_2,
    )
    gen2 = PublicationGenerationResultArtifact(
        title="Fix",
        body="Body2",
        commit_subject="Fix subject",
        commit_body="c2",
        run_id=RUN_ID,
        cycle_number=2,
        effect_id=effect2.effect_id,
        bound_head_sha=SHA_B,
        evidence_ref_sha256=HASH_2,
        patch_ref_sha256=HASH_3,
    )
    store.persist_publication_generation(run_id=RUN_ID, artifact=gen1)
    store.persist_publication_generation(run_id=RUN_ID, artifact=gen2)
    cached1 = store.read_cached_publication_generation(effect1)
    cached2 = store.read_cached_publication_generation(effect2)
    assert cached1 is not None and cached1.title == "Initial"
    assert cached2 is not None and cached2.title == "Fix"
    assert cached1.title != cached2.title
    drifted = effect1.model_copy(update={"bound_head_sha": SHA_B})
    assert store.read_cached_publication_generation(drifted) is None
    pub_ref, commit_ref = store.persist_publication_text_and_commit_message(
        run_id=RUN_ID,
        title=cached1.title,
        body=cached1.body,
        subject=cached1.commit_subject,
        commit_body=cached1.commit_body,
        effect_id=effect1.effect_id,
        cycle_number=effect1.cycle_number,
        bound_head_sha=effect1.bound_head_sha,
        evidence_ref_sha256=effect1.evidence_ref.sha256,
        patch_ref_sha256=effect1.patch_ref.sha256,
    )
    pub2_ref, _commit2 = store.persist_publication_text_and_commit_message(
        run_id=RUN_ID,
        title=cached2.title,
        body=cached2.body,
        subject=cached2.commit_subject,
        commit_body=cached2.commit_body,
        effect_id=effect2.effect_id,
        cycle_number=effect2.cycle_number,
        bound_head_sha=effect2.bound_head_sha,
        evidence_ref_sha256=effect2.evidence_ref.sha256,
        patch_ref_sha256=effect2.patch_ref.sha256,
    )
    assert pub_ref.relative_path != pub2_ref.relative_path
    store.verify_publication_readable(run_id=RUN_ID, publication_ref=pub_ref, commit_ref=commit_ref)


def test_adjudication_delivers_snapshot_content_and_fails_closed_on_drift() -> None:
    from ai_dev_loop.pr_review_v2.domain.common import PullRequestBinding

    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    snapshot = {
        "schema_version": 1,
        "sanitization_version": 1,
        "repository": "acme/demo",
        "pr_number": 7,
        "head_branch": "feature",
        "base_branch": "main",
        "head_sha": SHA_A,
        "cycle_number": 1,
        "poll_sequence": 1,
        "trigger_marker": "marker-1",
        "observed_at": T0.isoformat(),
        "evidence_kind": "eligible_threads",
        "trigger": {
            "comment_id": "c1",
            "author_login": "bot",
            "created_at": T0.isoformat(),
            "body_sha256": HASH_1,
            "marker": "marker-1",
        },
        "eligible_threads": [
            {
                "thread_id": "PRRT_1",
                "author_login": "rev",
                "created_at": T0.isoformat(),
                "commit_sha": SHA_A,
                "root_comment_id": "rc1",
                "root_body_sha256": HASH_2,
                "sanitized_root_body": "fix the auth check",
            }
        ],
        "reaction_ids": [],
        "source_hashes": {},
    }
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    import hashlib

    effect = AdjudicateThreadsEffect(
        effect_id="effect-adj-1",
        idempotency_key="idem-adj",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=binding,
        frozen_thread_ids=("PRRT_1",),
        snapshot_ref=ArtifactRef(
            relative_path="observations/x.json", sha256=hashlib.sha256(raw).hexdigest()
        ),
        execution_context_ref=ArtifactRef(relative_path="local/ctx.json", sha256=HASH_1),
    )
    prompt = _adjudication_wrapper_prompt(
        execution_context=_ctx(),
        effect=effect,
        frozen_thread_ids=("PRRT_1",),
        snapshot_artifact_bytes_or_path=raw,
    )
    assert "fix the auth check" in prompt
    assert "<<<SANITIZED_REVIEW_SNAPSHOT>>>" in prompt

    with pytest.raises(CodexLocalRunnerError, match="hash drift"):
        _adjudication_wrapper_prompt(
            execution_context=_ctx(),
            effect=effect,
            frozen_thread_ids=("PRRT_1",),
            snapshot_artifact_bytes_or_path=raw + b" ",
        )

    huge = b"x" * (MAX_ADJUDICATION_SNAPSHOT_BYTES + 1)
    huge_effect = effect.model_copy(
        update={
            "snapshot_ref": ArtifactRef(
                relative_path="observations/huge.json",
                sha256=hashlib.sha256(huge).hexdigest(),
            )
        }
    )
    with pytest.raises(CodexLocalRunnerError, match="size bound"):
        _adjudication_wrapper_prompt(
            execution_context=_ctx(),
            effect=huge_effect,
            frozen_thread_ids=("PRRT_1",),
            snapshot_artifact_bytes_or_path=huge,
        )


def test_existing_pr_discovery_rejects_missing_head_repo() -> None:
    class _Runner:
        def run(self, argv, *, cwd, timeout):
            del argv, cwd, timeout
            return (
                0,
                json.dumps(
                    {
                        "state": "open",
                        "number": 3,
                        "head_ref": "feature",
                        "base_ref": "main",
                        "head_sha": SHA_A,
                        "head_repo": None,
                    }
                ),
                "",
            )

    with pytest.raises(Exception, match="head_repo"):
        ExistingPrDiscoverer(runner=_Runner()).discover(owner_repo="acme/demo", pr_number=3)


def test_existing_pr_discovery_rejects_fork() -> None:
    class _Runner:
        def run(self, argv, *, cwd, timeout):
            del argv, cwd, timeout
            return (
                0,
                json.dumps(
                    {
                        "state": "open",
                        "number": 3,
                        "head_ref": "feature",
                        "base_ref": "main",
                        "head_sha": SHA_A,
                        "head_repo": "other/demo",
                    }
                ),
                "",
            )

    with pytest.raises(Exception, match="head repository"):
        ExistingPrDiscoverer(runner=_Runner()).discover(owner_repo="acme/demo", pr_number=3)


def test_prepare_cli_calls_handle_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_prepare(**kwargs):
        del kwargs
        calls.append("prepare")
        return "pr-review-v2 created prepared run x\norigin: existing_pr\npr: 1\nnext: start\n"

    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review_v2.prepare_existing_pr",
        fake_prepare,
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "pr-review-v2",
            "prepare",
            "--repo",
            "acme/demo",
            "--pr",
            "1",
            "--codex-session-id",
            SESSION,
            "--plan",
            "plan.md",
            "--prompt",
            "prompt.txt",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["prepare"]
    assert result.output.count("pr-review-v2 created") == 1
