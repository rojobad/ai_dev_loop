"""Phase 16.6 unit tests: frozen write policies, DTOs, markers, and proof shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    SHA_A,
    commit_effect,
    write_artifact,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ContentBoundMarker,
    GitHubWritePolicy,
    GitRemoteScheme,
    GitWritePolicy,
    ReconciliationProof,
    WriteProofKind,
    append_owned_marker,
    assert_no_force_argv,
    build_reconciliation_identity,
    claim_authority_snapshot_from_claim,
    derive_commit_trailer,
    derive_content_bound_marker,
    html_comment_marker,
    resolution_from_proof,
    sha256_hex,
    unmarked_body_hash,
    validate_branch_name,
    validate_remote_ref,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    PUBLIC_MARKER_VERSION,
    ReconciliationResolutionKind,
    ReconciliationStrategyKind,
    build_opaque_trigger_marker,
)
from ai_dev_loop.pr_review_v2.domain.events import PushConfirmedOutcome

# -- policies ---------------------------------------------------------------


def test_git_write_policy_defaults_ssh_and_rejects_bad_timeout() -> None:
    policy = GitWritePolicy(repository_cwd="/tmp/repo")
    assert policy.remote_scheme is GitRemoteScheme.SSH
    assert policy.enforce_remote_identity is True
    with pytest.raises(ValidationError):
        GitWritePolicy(
            repository_cwd="/tmp/repo",
            per_call_timeout_seconds=100.0,
            overall_timeout_seconds=10.0,
        )


def test_git_write_policy_local_scheme_disables_identity() -> None:
    policy = GitWritePolicy(repository_cwd="/tmp/repo", remote_scheme=GitRemoteScheme.LOCAL)
    assert policy.enforce_remote_identity is False


def test_git_write_policy_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GitWritePolicy(repository_cwd="/tmp/repo", nope=1)  # type: ignore[call-arg]


def test_github_write_policy_default_review_command_and_bounds() -> None:
    policy = GitHubWritePolicy(repository_cwd="/tmp/repo")
    assert policy.review_command_body == "@codex review"
    with pytest.raises(ValidationError):
        GitHubWritePolicy(
            repository_cwd="/tmp/repo",
            per_call_timeout_seconds=100.0,
            overall_timeout_seconds=1.0,
        )


def test_write_policies_accept_overall_timeout_at_two_hour_cap() -> None:
    assert (
        GitWritePolicy(
            repository_cwd="/tmp/repo", overall_timeout_seconds=7200
        ).overall_timeout_seconds
        == 7200
    )
    assert (
        GitHubWritePolicy(
            repository_cwd="/tmp/repo", overall_timeout_seconds=7200
        ).overall_timeout_seconds
        == 7200
    )
    with pytest.raises(ValidationError):
        GitWritePolicy(repository_cwd="/tmp/repo", overall_timeout_seconds=7201)
    with pytest.raises(ValidationError):
        GitHubWritePolicy(repository_cwd="/tmp/repo", overall_timeout_seconds=7201)


# -- branch / ref validation ------------------------------------------------


@pytest.mark.parametrize("value", ["feature", "release/1.0", "refs/heads/main"])
def test_validate_remote_ref_accepts_safe(value: str) -> None:
    assert validate_remote_ref(value) == value


@pytest.mark.parametrize("value", ["", "/bad", "bad/", "a//b", "..", "a..b", "-x; rm"])
def test_validate_branch_name_rejects_unsafe(value: str) -> None:
    with pytest.raises(ValueError):
        validate_branch_name(value)


# -- no-force push guard ----------------------------------------------------


def test_assert_no_force_argv_allows_plain_push() -> None:
    assert_no_force_argv(["git", "push", "origin", f"{SHA_A}:refs/heads/feature"])


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "--force", "origin", "feature"],
        ["git", "push", "--force-with-lease", "origin", "feature"],
        ["git", "push", "origin", ":"],
    ],
)
def test_assert_no_force_argv_rejects_force_and_delete(argv: list[str]) -> None:
    with pytest.raises(ValueError):
        assert_no_force_argv(argv)


# -- opaque + content-bound markers ----------------------------------------


def test_trigger_marker_is_opaque_and_excludes_run_id() -> None:
    marker = build_opaque_trigger_marker(run_id="run-16-6", cycle_number=1)
    assert marker.startswith(f"adl-{PUBLIC_MARKER_VERSION}:")
    assert "run-16-6" not in marker
    # Deterministic + stable.
    assert marker == build_opaque_trigger_marker(run_id="run-16-6", cycle_number=1)


def test_content_bound_marker_excludes_identity_and_binds_content() -> None:
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="artifact:" + ("d" * 64),
        canonical_content="hello body",
    )
    assert isinstance(marker, ContentBoundMarker)
    assert marker.content_sha256 == sha256_hex("hello body")
    for banned in ("run-16-6", "owner-a", "claim-1", "session"):
        assert banned not in marker.marker_text
    # Stable across identical inputs; differs by content.
    again = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="artifact:" + ("d" * 64),
        canonical_content="hello body",
    )
    assert again.marker_text == marker.marker_text
    other = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="artifact:" + ("d" * 64),
        canonical_content="different body",
    )
    assert other.marker_text != marker.marker_text


def test_append_and_unmarked_body_hash_roundtrip() -> None:
    marker = derive_content_bound_marker(
        operation="post_thread_reply",
        target_kind="thread_reply",
        idempotency_key="artifact:" + ("e" * 64),
        canonical_content="reply text",
    )
    body = append_owned_marker("reply text", marker.marker_text)
    assert html_comment_marker(marker.marker_text) in body
    assert unmarked_body_hash(body, marker.marker_text) == marker.content_sha256
    # Appending twice is rejected (exactly one owned marker).
    with pytest.raises(ValueError):
        append_owned_marker(body, marker.marker_text)


def test_unmarked_body_hash_detects_duplicate_markers() -> None:
    marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="artifact:" + ("f" * 64),
        canonical_content="x",
    )
    needle = html_comment_marker(marker.marker_text)
    with pytest.raises(ValueError):
        unmarked_body_hash(f"x\n{needle}\n{needle}\n", marker.marker_text)


def test_commit_trailer_is_deterministic_digest() -> None:
    trailer = derive_commit_trailer(idempotency_key="pr-review:run:commit")
    assert trailer.startswith("ADL-Idempotency: ")
    assert "run" not in trailer.split(": ", 1)[1]
    assert trailer == derive_commit_trailer(idempotency_key="pr-review:run:commit")


def test_content_bound_commit_identities_differ_and_retry_preserves() -> None:
    from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, RepositoryIdentity
    from ai_dev_loop.pr_review_v2.domain.effects import (
        CommitPatchEffect,
        commit_patch_effect_target,
        stable_effect_ids,
        with_attempt,
    )

    repo = RepositoryIdentity(name_with_owner="acme/demo")
    patch_a = ArtifactRef(relative_path="local/patches/a.patch", sha256="1" * 64)
    patch_b = ArtifactRef(relative_path="local/patches/b.patch", sha256="2" * 64)
    parent_a = "a" * 40
    parent_b = "b" * 40
    id_initial, key_initial = stable_effect_ids(
        run_id="run-1",
        cycle_number=1,
        operation="commit_patch",
        target=commit_patch_effect_target(expected_head_sha=parent_a, patch_sha256=patch_a.sha256),
    )
    id_fix, key_fix = stable_effect_ids(
        run_id="run-1",
        cycle_number=1,
        operation="commit_patch",
        target=commit_patch_effect_target(expected_head_sha=parent_b, patch_sha256=patch_b.sha256),
    )
    assert id_initial != id_fix
    assert key_initial != key_fix
    assert derive_commit_trailer(idempotency_key=key_initial) != derive_commit_trailer(
        idempotency_key=key_fix
    )

    msg = ArtifactRef(relative_path="local/messages/m.json", sha256="3" * 64)
    effect = CommitPatchEffect(
        effect_id=id_initial,
        idempotency_key=key_initial,
        run_id="run-1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=repo,
        bound_head_sha=parent_a,
        patch_ref=patch_a,
        expected_head_sha=parent_a,
        expected_branch="feature",
        commit_message_ref=msg,
    )
    retried = with_attempt(effect, 2)
    assert retried.effect_id == id_initial
    assert retried.idempotency_key == key_initial
    assert derive_commit_trailer(idempotency_key=retried.idempotency_key) == derive_commit_trailer(
        idempotency_key=key_initial
    )


# -- reconciliation identity + proof shapes --------------------------------


def test_reconciliation_identity_is_generation_independent() -> None:
    ident = build_reconciliation_identity(
        run_id="run-16-6",
        effect_id="e1",
        attempt=3,
        strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
    )
    assert "run-16-6" not in ident
    assert ident == build_reconciliation_identity(
        run_id="run-16-6",
        effect_id="e1",
        attempt=3,
        strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
    )


def test_applied_proof_requires_confirmed_outcome() -> None:
    with pytest.raises(ValidationError):
        ReconciliationProof(
            proof=WriteProofKind.APPLIED,
            strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
            safe_summary="x",
        )
    ok = ReconciliationProof(
        proof=WriteProofKind.APPLIED,
        strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
        confirmed_outcome=PushConfirmedOutcome(commit_sha="b" * 40, remote_ref="feature"),
        safe_summary="applied",
    )
    assert ok.confirmed_outcome is not None


def test_proven_not_applied_requires_next_attempt_at(now=None) -> None:
    from datetime import UTC, datetime

    when = datetime(2026, 7, 21, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ReconciliationProof(
            proof=WriteProofKind.PROVEN_NOT_APPLIED,
            strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
            safe_summary="x",
        )
    ok = ReconciliationProof(
        proof=WriteProofKind.PROVEN_NOT_APPLIED,
        strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
        next_attempt_at=when,
        safe_summary="retry",
    )
    assert ok.next_attempt_at == when


def test_unresolved_forbids_outcome_and_next_attempt() -> None:
    proof = ReconciliationProof(
        proof=WriteProofKind.UNRESOLVED,
        strategy=ReconciliationStrategyKind.FIND_REMOTE_REF,
        safe_summary="unresolved",
    )
    assert proof.confirmed_outcome is None
    assert proof.next_attempt_at is None


def test_resolution_from_proof_maps_all_kinds() -> None:
    assert resolution_from_proof(WriteProofKind.APPLIED) is ReconciliationResolutionKind.APPLIED
    assert (
        resolution_from_proof(WriteProofKind.PROVEN_NOT_APPLIED)
        is ReconciliationResolutionKind.PROVEN_NOT_APPLIED
    )
    assert (
        resolution_from_proof(WriteProofKind.UNRESOLVED) is ReconciliationResolutionKind.UNRESOLVED
    )


def test_claim_authority_snapshot_carries_only_safe_identity(tmp_path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import claim_for

    patch = write_artifact(tmp_path / "art", RUN_ID, "artifacts/p.patch", b"data")
    msg = write_artifact(tmp_path / "art", RUN_ID, "artifacts/m.json", b"{}")
    effect = commit_effect(patch_ref=patch, commit_message_ref=msg, expected_head_sha=SHA_A)
    claim = claim_for(effect)
    snap = claim_authority_snapshot_from_claim(claim)
    assert snap.effect_id == effect.effect_id
    assert snap.bound_head_sha == effect.bound_head_sha
    assert snap.attempt == effect.attempt


def test_push_effect_forbids_force_flag() -> None:
    from tests.unit.pr_review_v2.github_write_helpers import REPO

    from ai_dev_loop.pr_review_v2.domain.effects import PushCommitEffect

    with pytest.raises(ValidationError):
        PushCommitEffect(
            effect_id="e",
            idempotency_key="e",
            run_id="run-16-6",
            cycle_number=1,
            attempt=1,
            max_attempts=6,
            repository=REPO,
            bound_head_sha="b" * 40,
            commit_sha="b" * 40,
            remote_ref="feature",
            expected_remote_sha_before_push=None,
            force=True,  # type: ignore[arg-type]
        )
