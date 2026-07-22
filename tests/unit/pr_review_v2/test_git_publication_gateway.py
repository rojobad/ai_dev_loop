"""Phase 16.6 unit tests: commit/push preflight, confirmation, reconciliation.

These use real disposable temporary Git repositories and a local bare remote for
authentic Git semantics. No SSH, credentials, or external network are used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    T0,
    commit_effect,
    make_temp_git_repo,
    push_effect,
    stage_change,
    write_commit_message,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitRemoteScheme,
    GitWritePolicy,
    WriteProofKind,
)
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitTransportError,
    GitWriteTransport,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader


class _Authorizer:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _gateway(repo, tmp_path: Path) -> tuple[GitPublicationGateway, Path]:
    art_root = tmp_path / "art"
    policy = GitWritePolicy(
        repository_cwd=str(repo.work),
        remote_scheme=GitRemoteScheme.LOCAL,
        require_ssh_agent_identity=False,
    )
    transport = GitWriteTransport(
        repository_cwd=str(repo.work),
        per_call_timeout_seconds=30.0,
    )
    gateway = GitPublicationGateway(
        policy=policy,
        transport=transport,
        input_reader=InputArtifactReader(art_root),
        remote_name="origin",
    )
    return gateway, art_root


@pytest.fixture(autouse=True)
def _xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


def _commit_effect_for(repo, art_root: Path):
    patch_bytes = repo.staged_patch_bytes()
    assert patch_bytes
    from tests.unit.pr_review_v2.github_write_helpers import write_artifact

    patch_ref = write_artifact(art_root, RUN_ID, "artifacts/p.patch", patch_bytes)
    msg_ref = write_commit_message(art_root, RUN_ID, subject="Apply staged change")
    return commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=repo.head_sha(),
        expected_branch=repo.branch,
    )


def test_commit_records_new_commit_with_trailer(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    effect = _commit_effect_for(repo, art_root)
    auth = _Authorizer()

    result = gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=auth)

    assert auth.calls == 1
    assert result.already_applied is False
    assert result.outcome.kind == "commit_recorded"
    assert result.outcome.new_head_sha == repo.head_sha()
    assert result.outcome.new_head_sha != effect.expected_head_sha
    assert result.outcome.expected_remote_sha_before_push is None


def test_commit_is_idempotent_when_head_already_owned(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    effect = _commit_effect_for(repo, art_root)
    gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=_Authorizer())

    auth = _Authorizer()
    again = gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=auth)
    assert again.already_applied is True
    assert auth.calls == 0  # no second mutating commit
    assert again.outcome.new_head_sha == repo.head_sha()


def test_commit_blocks_on_branch_drift(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    effect = commit_effect(
        patch_ref=_commit_effect_for(repo, art_root).patch_ref,
        commit_message_ref=write_commit_message(art_root, RUN_ID, subject="x"),
        expected_head_sha=repo.head_sha(),
        expected_branch="wrong-branch",
    )
    with pytest.raises(GitTransportError):
        gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=_Authorizer())


def test_commit_blocks_on_staged_patch_drift(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    from tests.unit.pr_review_v2.github_write_helpers import write_artifact

    # Patch artifact does not match the actual staged diff bytes.
    patch_ref = write_artifact(art_root, RUN_ID, "artifacts/p.patch", b"not the real diff\n")
    effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=write_commit_message(art_root, RUN_ID, subject="x"),
        expected_head_sha=repo.head_sha(),
        expected_branch=repo.branch,
    )
    with pytest.raises(GitTransportError):
        gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=_Authorizer())


def test_reconcile_commit_applied_and_proven_not_applied(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    effect = _commit_effect_for(repo, art_root)

    # Before commit: staged patch is still exact and HEAD is expected parent.
    proof_before = gateway.reconcile_commit(effect, run_id=RUN_ID, now=T0)
    assert proof_before.proof is WriteProofKind.PROVEN_NOT_APPLIED

    gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    proof_after = gateway.reconcile_commit(effect, run_id=RUN_ID, now=T0)
    assert proof_after.proof is WriteProofKind.APPLIED
    assert proof_after.confirmed_outcome is not None


# -- push -------------------------------------------------------------------


def _commit_then_effect(repo, tmp_path: Path):
    stage_change(repo)
    gateway, art_root = _gateway(repo, tmp_path)
    effect = _commit_effect_for(repo, art_root)
    result = gateway.commit(effect, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    return gateway, result.outcome


def test_push_confirms_remote_ref(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    gateway, commit_outcome = _commit_then_effect(repo, tmp_path)
    push = push_effect(
        commit_sha=commit_outcome.commit_sha,
        remote_ref=repo.branch,
        expected_remote_sha_before_push=commit_outcome.expected_remote_sha_before_push,
    )
    auth = _Authorizer()
    result = gateway.push(push, run_id=RUN_ID, now=T0, authorize=auth)
    assert auth.calls == 1
    assert result.already_applied is False
    assert repo.remote_sha(repo.branch) == commit_outcome.commit_sha


def test_push_idempotent_when_remote_already_exact(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    gateway, commit_outcome = _commit_then_effect(repo, tmp_path)
    push = push_effect(
        commit_sha=commit_outcome.commit_sha,
        remote_ref=repo.branch,
        expected_remote_sha_before_push=commit_outcome.expected_remote_sha_before_push,
    )
    gateway.push(push, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    auth = _Authorizer()
    again = gateway.push(push, run_id=RUN_ID, now=T0, authorize=auth)
    assert again.already_applied is True
    assert auth.calls == 0


def test_reconcile_push_applied_and_proven_not_applied(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    gateway, commit_outcome = _commit_then_effect(repo, tmp_path)
    push = push_effect(
        commit_sha=commit_outcome.commit_sha,
        remote_ref=repo.branch,
        expected_remote_sha_before_push=commit_outcome.expected_remote_sha_before_push,
    )
    before = gateway.reconcile_push(push, run_id=RUN_ID, now=T0)
    assert before.proof is WriteProofKind.PROVEN_NOT_APPLIED

    gateway.push(push, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    after = gateway.reconcile_push(push, run_id=RUN_ID, now=T0)
    assert after.proof is WriteProofKind.APPLIED


def test_push_blocks_when_remote_drifts_to_third_value(tmp_path: Path) -> None:
    repo = make_temp_git_repo(tmp_path)
    gateway, commit_outcome = _commit_then_effect(repo, tmp_path)
    # Baseline claims a bogus prior remote SHA that does not match the empty ref.
    push = push_effect(
        commit_sha=commit_outcome.commit_sha,
        remote_ref=repo.branch,
        expected_remote_sha_before_push="d" * 40,
    )
    with pytest.raises(GitTransportError):
        gateway.push(push, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    # No push happened.
    assert repo.remote_sha(repo.branch) is None
