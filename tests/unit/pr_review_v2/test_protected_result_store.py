"""Unit tests for ProtectedResultStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    run_artifact_root,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    ProtectedResultStore,
    ProtectedResultStoreError,
)

RUN_ID = "prv2-store-001"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


def _sample_execution_context() -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=OriginKind.SOURCE_RUN,
            source_run_id="source-run-001",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            chat_id="chat-001",
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


def test_execution_context_roundtrip(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    artifact = _sample_execution_context()
    ref = store.persist_execution_context(run_id=RUN_ID, artifact=artifact)
    loaded = store.read_execution_context(run_id=RUN_ID, ref=ref)
    assert loaded == artifact


def test_hash_mismatch_on_read_rejected(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    ref = store.persist_execution_context(run_id=RUN_ID, artifact=_sample_execution_context())
    bad_ref = ArtifactRef(relative_path=ref.relative_path, sha256="0" * 64)
    with pytest.raises(ProtectedResultStoreError, match="hash mismatch"):
        store.read_execution_context(run_id=RUN_ID, ref=bad_ref)


def test_symlink_artifact_rejected_on_read(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    ref = store.persist_execution_context(run_id=RUN_ID, artifact=_sample_execution_context())
    run_root = run_artifact_root(tmp_path / "art", RUN_ID)
    target = run_root / ref.relative_path
    link = run_root / "local/link-context.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    link.symlink_to(outside)
    bad_ref = ArtifactRef(
        relative_path="local/link-context.json",
        sha256=ref.sha256,
    )
    with pytest.raises(ProtectedResultStoreError, match="missing or unsafe"):
        store.read_execution_context(run_id=RUN_ID, ref=bad_ref)


def test_publication_persist_readable_by_input_artifact_reader(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    pub_ref, commit_ref = store.persist_publication_text_and_commit_message(
        run_id=RUN_ID,
        title="Fix review feedback",
        body="Details",
        subject="Fix review feedback",
        commit_body="Details",
    )
    store.verify_publication_readable(
        run_id=RUN_ID,
        publication_ref=pub_ref,
        commit_ref=commit_ref,
    )
    reader = InputArtifactReader(tmp_path / "art")
    publication = reader.read_publication_text(run_id=RUN_ID, ref=pub_ref)
    commit = reader.read_commit_message(run_id=RUN_ID, ref=commit_ref)
    assert publication.title == "Fix review feedback"
    assert commit.subject == "Fix review feedback"


def test_copy_verified_artifact(tmp_path: Path) -> None:
    source_root = tmp_path / "source-art"
    dest_root = tmp_path / "dest-art"
    source_store = ProtectedResultStore(source_root)
    dest_store = ProtectedResultStore(dest_root)
    source_ref = source_store.persist_execution_context(
        run_id="source-run",
        artifact=_sample_execution_context(),
    )
    copied = dest_store.copy_verified_artifact(
        source_run_root=ensure_run_artifact_root(source_root, "source-run"),
        source_ref=source_ref,
        dest_run_id=RUN_ID,
        dest_relative="local/copied-context.json",
    )
    loaded = dest_store.read_execution_context(run_id=RUN_ID, ref=copied)
    assert loaded.schema_name == "ai_dev_loop.pr_review_v2.execution_context"


def test_canonical_json_has_trailing_newline(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    ref = store.persist_execution_context(run_id=RUN_ID, artifact=_sample_execution_context())
    run_root = run_artifact_root(tmp_path / "art", RUN_ID)
    raw = (run_root / ref.relative_path).read_bytes()
    assert raw.endswith(b"\n")
    json.loads(raw.decode("utf-8"))
