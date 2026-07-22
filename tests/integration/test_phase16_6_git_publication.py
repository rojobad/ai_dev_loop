"""Phase 16.6 integration: real-git commit + push idempotency and reconciliation.

These tests drive ``GitPublicationGateway`` against a real ``git`` binary using a
disposable working repository and a local bare remote (``GitRemoteScheme.LOCAL``).
They assert that commit and push are idempotent, that a second attempt reports
``already_applied`` without a new mutation, and that FIND_COMMIT_AT_HEAD /
FIND_REMOTE_REF reconciliation prove APPLIED after the write.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2 import write_helpers as H

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitRemoteScheme,
    GitWritePolicy,
    WriteProofKind,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, RepositoryIdentity
from ai_dev_loop.pr_review_v2.domain.effects import CommitPatchEffect, PushCommitEffect
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader

REPO = RepositoryIdentity(name_with_owner="acme/demo")
NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo: Path, *args: str) -> str:
    import os

    env = dict(os.environ)
    env.update(_GIT_ENV)
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="git binary is required")


@pytest.fixture
def hermetic_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=feature")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=feature")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "feature")
    return work, remote


def _policy(work: Path) -> GitWritePolicy:
    return GitWritePolicy(
        repository_cwd=str(work),
        remote_scheme=GitRemoteScheme.LOCAL,
        require_ssh_agent_identity=False,
    )


def _transport(work: Path) -> GitWriteTransport:
    return GitWriteTransport(repository_cwd=str(work), per_call_timeout_seconds=30.0)


def _gateway(work: Path, artifact_root: Path) -> GitPublicationGateway:
    return GitPublicationGateway(
        policy=_policy(work),
        transport=_transport(work),
        input_reader=InputArtifactReader(artifact_root),
    )


def _stage_change_and_artifacts(
    work: Path, artifact_root: Path, run_id: str
) -> tuple[ArtifactRef, ArtifactRef, str, bytes]:
    (work / "feature.txt").write_text("new feature line\n")
    _git(work, "add", "feature.txt")
    transport = _transport(work)
    staged = transport.read_staged_patch_bytes()
    patch_ref = H.write_artifact(artifact_root, run_id, "artifacts/patch.bin", staged)
    message_ref = H.write_commit_message(artifact_root, run_id, "Add feature", "body")
    head = transport.read_head_sha()
    return patch_ref, message_ref, head, staged


def test_commit_then_reconcile_is_idempotent(hermetic_state):
    tmp_path = hermetic_state
    artifact_root = tmp_path / "art"
    run_id = "run-16-6-commit"
    work, _remote = _init_repo(tmp_path)
    patch_ref, message_ref, parent, _staged = _stage_change_and_artifacts(
        work, artifact_root, run_id
    )
    effect = CommitPatchEffect(
        effect_id="pr-review:run:cycle:01:commit_patch",
        idempotency_key="pr-review:run:cycle:01:commit_patch",
        run_id=run_id,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=parent,
        patch_ref=patch_ref,
        expected_head_sha=parent,
        expected_branch="feature",
        commit_message_ref=message_ref,
    )
    gateway = _gateway(work, artifact_root)
    calls: list[str] = []
    success = gateway.commit(effect, run_id=run_id, now=NOW, authorize=lambda: calls.append("a"))
    assert success.already_applied is False
    assert calls == ["a"]
    assert success.outcome.kind == "commit_recorded"
    new_head = success.outcome.new_head_sha
    assert new_head != parent

    # Second commit attempt is idempotent: HEAD is already the owned commit.
    again = gateway.commit(
        effect, run_id=run_id, now=NOW, authorize=lambda: pytest.fail("no second commit")
    )
    assert again.already_applied is True
    assert again.outcome.new_head_sha == new_head

    # Reconciliation proves the commit applied at HEAD.
    proof = gateway.reconcile_commit(effect, run_id=run_id, now=NOW)
    assert proof.proof is WriteProofKind.APPLIED
    assert proof.confirmed_outcome.new_head_sha == new_head


def test_commit_rejects_head_drift(hermetic_state):
    tmp_path = hermetic_state
    artifact_root = tmp_path / "art"
    run_id = "run-16-6-drift"
    work, _remote = _init_repo(tmp_path)
    patch_ref, message_ref, parent, _staged = _stage_change_and_artifacts(
        work, artifact_root, run_id
    )
    effect = CommitPatchEffect(
        effect_id="pr-review:run:cycle:01:commit_patch",
        idempotency_key="pr-review:run:cycle:01:commit_patch",
        run_id=run_id,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=parent,
        patch_ref=patch_ref,
        expected_head_sha="0" * 40,  # not the real parent
        expected_branch="feature",
        commit_message_ref=message_ref,
    )
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError

    gateway = _gateway(work, artifact_root)
    with pytest.raises(GitTransportError):
        gateway.commit(effect, run_id=run_id, now=NOW, authorize=lambda: pytest.fail("no write"))


def test_push_then_reconcile_is_idempotent(hermetic_state):
    tmp_path = hermetic_state
    artifact_root = tmp_path / "art"
    run_id = "run-16-6-push"
    work, remote = _init_repo(tmp_path)
    patch_ref, message_ref, parent, _staged = _stage_change_and_artifacts(
        work, artifact_root, run_id
    )
    commit_effect = CommitPatchEffect(
        effect_id="pr-review:run:cycle:01:commit_patch",
        idempotency_key="pr-review:run:cycle:01:commit_patch",
        run_id=run_id,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=parent,
        patch_ref=patch_ref,
        expected_head_sha=parent,
        expected_branch="feature",
        commit_message_ref=message_ref,
    )
    gateway = _gateway(work, artifact_root)
    committed = gateway.commit(commit_effect, run_id=run_id, now=NOW, authorize=lambda: None)
    new_head = committed.outcome.new_head_sha
    baseline = committed.outcome.expected_remote_sha_before_push

    push = PushCommitEffect(
        effect_id="pr-review:run:cycle:01:push_commit",
        idempotency_key="pr-review:run:cycle:01:push_commit",
        run_id=run_id,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=new_head,
        commit_sha=new_head,
        remote_ref="feature",
        expected_remote_sha_before_push=baseline,
    )
    calls: list[str] = []
    pushed = gateway.push(push, run_id=run_id, now=NOW, authorize=lambda: calls.append("p"))
    assert pushed.already_applied is False
    assert calls == ["p"]
    assert pushed.outcome.kind == "push_confirmed"

    # Remote now carries the commit; a second push is idempotent.
    again = gateway.push(
        push, run_id=run_id, now=NOW, authorize=lambda: pytest.fail("no second push")
    )
    assert again.already_applied is True

    # Reconciliation proves the push applied at the remote ref.
    proof = gateway.reconcile_push(push, run_id=run_id, now=NOW)
    assert proof.proof is WriteProofKind.APPLIED

    remote_head = _git(remote, "rev-parse", "feature").strip()
    assert remote_head == new_head


def test_authority_rejection_blocks_commit_with_zero_writes(hermetic_state):
    tmp_path = hermetic_state
    artifact_root = tmp_path / "art"
    run_id = "run-16-6-auth"
    work, _remote = _init_repo(tmp_path)
    patch_ref, message_ref, parent, _staged = _stage_change_and_artifacts(
        work, artifact_root, run_id
    )
    effect = CommitPatchEffect(
        effect_id="pr-review:run:cycle:01:commit_patch",
        idempotency_key="pr-review:run:cycle:01:commit_patch",
        run_id=run_id,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=parent,
        patch_ref=patch_ref,
        expected_head_sha=parent,
        expected_branch="feature",
        commit_message_ref=message_ref,
    )
    from ai_dev_loop.pr_review_v2.application.write_contracts import AuthorityLostError

    gateway = _gateway(work, artifact_root)

    def deny() -> None:
        raise AuthorityLostError("authority rejected")

    with pytest.raises(AuthorityLostError):
        gateway.commit(effect, run_id=run_id, now=NOW, authorize=deny)

    # HEAD is unchanged: zero writes occurred.
    assert _transport(work).read_head_sha() == parent
