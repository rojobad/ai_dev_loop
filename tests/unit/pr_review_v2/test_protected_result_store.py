"""Unit tests for ProtectedResultStore."""

from __future__ import annotations

import json
import os
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
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_TEXT_BYTES,
    PublicationTextArtifact,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    run_artifact_root,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    MAX_FIX_PROMPT_BYTES,
    MAX_REPLY_TEXT_BYTES,
    MAX_SOURCE_PLAN_BYTES,
    MAX_SOURCE_PROMPT_BYTES,
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
    with pytest.raises(ProtectedResultStoreError, match="missing|unsafe"):
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


@pytest.mark.parametrize(
    ("kind", "max_bytes"),
    (("plan", MAX_SOURCE_PLAN_BYTES), ("prompt", MAX_SOURCE_PROMPT_BYTES)),
)
def test_source_snapshot_size_boundary(
    tmp_path: Path,
    kind: str,
    max_bytes: int,
) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    accepted = b"x" * max_bytes
    if kind == "plan":
        ref = store.persist_source_plan_bytes(run_id=RUN_ID, data=accepted)
        loaded = store.read_source_plan_bytes(run_id=RUN_ID, expected_sha256=ref.sha256)
        persist = store.persist_source_plan_bytes
    else:
        ref = store.persist_source_prompt_bytes(run_id=RUN_ID, data=accepted)
        loaded = store.read_source_prompt_bytes(run_id=RUN_ID, expected_sha256=ref.sha256)
        persist = store.persist_source_prompt_bytes
    assert loaded == accepted

    with pytest.raises(ProtectedResultStoreError, match="maximum size"):
        persist(run_id=f"{RUN_ID}-oversized-{kind}", data=accepted + b"x")


@pytest.mark.parametrize(
    ("kind", "max_bytes"),
    (("reply", MAX_REPLY_TEXT_BYTES), ("fix", MAX_FIX_PROMPT_BYTES)),
)
def test_plain_text_artifact_size_boundary(
    tmp_path: Path,
    kind: str,
    max_bytes: int,
) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    accepted = "x" * max_bytes
    if kind == "reply":
        ref = store.persist_reply_text(run_id=RUN_ID, relative_hint="thread", text=accepted)
        loaded = InputArtifactReader(tmp_path / "art").read_reply_text(run_id=RUN_ID, ref=ref)
        assert loaded == accepted
        persist = lambda run_id, text: store.persist_reply_text(  # noqa: E731
            run_id=run_id, relative_hint="thread", text=text
        )
    else:
        ref = store.persist_fix_prompt(run_id=RUN_ID, text=accepted)
        assert ref.sha256
        persist = store.persist_fix_prompt

    with pytest.raises(ProtectedResultStoreError, match="maximum size"):
        persist(run_id=f"{RUN_ID}-oversized-{kind}", text=accepted + "x")


def test_publication_json_size_boundary_includes_serialization_overhead(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "art")
    empty = PublicationTextArtifact(title="t", body="").model_dump(mode="json")
    overhead = len(
        (
            json.dumps(empty, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
    )
    accepted_body = "x" * (DEFAULT_MAX_TEXT_BYTES - overhead)

    publication_ref, _commit_ref = store.persist_publication_text_and_commit_message(
        run_id=RUN_ID,
        title="t",
        body=accepted_body,
        subject="s",
        commit_body="",
    )
    loaded = InputArtifactReader(tmp_path / "art").read_publication_text(
        run_id=RUN_ID, ref=publication_ref
    )
    assert loaded.body == accepted_body

    with pytest.raises(ProtectedResultStoreError, match="maximum size"):
        store.persist_publication_text_and_commit_message(
            run_id=f"{RUN_ID}-oversized-publication",
            title="t",
            body=accepted_body + "x",
            subject="s",
            commit_body="",
        )


def test_binding_valid_post_finalization_cache_mutation_fails_closed(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.application.execution_context import (
        PublicationGenerationResultArtifact,
    )
    from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, RepositoryIdentity
    from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect
    from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root

    store = ProtectedResultStore(tmp_path / "art")
    effect = GeneratePublicationTextEffect(
        effect_id="effect-pub-mut",
        idempotency_key="idem-mut",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="e.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="p.patch", sha256=HASH_2),
    )
    artifact = PublicationGenerationResultArtifact(
        title="Initial",
        body="Body",
        commit_subject="Subject",
        commit_body="c",
        run_id=RUN_ID,
        cycle_number=1,
        effect_id=effect.effect_id,
        bound_head_sha=SHA_A,
        evidence_ref_sha256=HASH_1,
        patch_ref_sha256=HASH_2,
    )
    ref = store.persist_publication_generation(run_id=RUN_ID, artifact=artifact)
    assert store.read_cached_publication_generation(effect) is not None
    # Binding-valid content mutation: keep binding fields, change body text.
    mutated = artifact.model_copy(update={"body": "Mutated body still binding-valid"})
    canonical = (
        json.dumps(
            mutated.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    target = run_artifact_root(store.root, RUN_ID) / ref.relative_path
    target.write_bytes(canonical)
    os.chmod(target, 0o600)
    assert store.read_cached_publication_generation(effect) is None
