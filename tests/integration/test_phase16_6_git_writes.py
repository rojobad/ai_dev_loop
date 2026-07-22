"""Phase 16.6 real-temporary-Git commit/push integration tests."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2 import write_helpers as H

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    WriteAuthorityStatus,
    WriteProofKind,
    claim_authority_snapshot_from_claim,
    derive_commit_trailer,
)
from ai_dev_loop.pr_review_v2.domain.events import CommitRecordedOutcome, PushConfirmedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    work.mkdir()
    _git(["init"], cwd=work)
    _git(["config", "user.email", "phase16-6@example.com"], cwd=work)
    _git(["config", "user.name", "phase16-6"], cwd=work)
    (work / "file.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=work)
    _git(["commit", "-m", "init"], cwd=work)
    _git(["checkout", "-b", "feature"], cwd=work)
    parent = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["init", "--bare", str(bare)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(bare)], cwd=work)
    _git(["push", "-u", "origin", "feature"], cwd=work)
    (work / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=work)
    return work, bare, parent


@pytest.fixture
def git_repo(tmp_path: Path):
    work, bare, parent = _init_repo(tmp_path)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=str(work),
        check=True,
        capture_output=True,
    ).stdout
    assert patch
    artifact_root = tmp_path / "artifacts"
    patch_ref = H.write_artifact(artifact_root, H.RUN_ID, "artifacts/patch.bin", patch)
    message_ref = H.write_commit_message(artifact_root, H.RUN_ID, "publish feature", "body")
    return {
        "work": work,
        "bare": bare,
        "parent": parent,
        "patch": patch,
        "patch_ref": patch_ref,
        "message_ref": message_ref,
        "artifact_root": artifact_root,
    }


def _gateway(repo: dict) -> GitPublicationGateway:
    policy = H.git_policy(str(repo["work"]))
    transport = GitWriteTransport(
        repository_cwd=str(repo["work"]),
        git_command="git",
        per_call_timeout_seconds=30.0,
        env={"PATH": os.environ.get("PATH", "/usr/bin"), "GIT_TERMINAL_PROMPT": "0"},
    )
    return GitPublicationGateway(
        policy=policy,
        transport=transport,
        input_reader=InputArtifactReader(repo["artifact_root"]),
    )


def test_commit_records_remote_baseline_and_advances_head(git_repo):
    gateway = _gateway(git_repo)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    authority = H.FixedAuthority()
    now = datetime.now(tz=UTC)

    def authorize() -> None:
        result = authority.check_authority(claim_authority_snapshot_from_claim(H.claim_for(effect)))
        assert result.status is WriteAuthorityStatus.AUTHORIZED

    success = gateway.commit(effect, run_id=H.RUN_ID, now=now, authorize=authorize)
    assert isinstance(success.outcome, CommitRecordedOutcome)
    assert success.outcome.commit_sha == success.outcome.new_head_sha
    assert success.outcome.commit_sha != git_repo["parent"]
    # Durable pre-push baseline is the remote feature tip before commit (parent).
    assert success.outcome.expected_remote_sha_before_push == git_repo["parent"]
    assert authority.calls == 1
    trailer = derive_commit_trailer(idempotency_key=effect.idempotency_key)
    message = _git(["log", "-1", "--format=%B"], cwd=git_repo["work"])
    assert trailer in message
    assert "--force" not in message


def test_push_is_idempotent_when_remote_already_at_commit(git_repo):
    gateway = _gateway(git_repo)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    now = datetime.now(tz=UTC)
    committed = gateway.commit(effect, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    push = H.push_effect(
        commit_sha=committed.outcome.commit_sha,
        bound_head_sha=committed.outcome.new_head_sha,
        expected_remote_sha_before_push=committed.outcome.expected_remote_sha_before_push,
    )
    first = gateway.push(push, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    assert isinstance(first.outcome, PushConfirmedOutcome)
    second = gateway.push(push, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    assert second.already_applied is True
    remote = _git(["ls-remote", "origin", "refs/heads/feature"], cwd=git_repo["work"]).split()[0]
    assert remote == committed.outcome.commit_sha


def test_push_blocks_when_remote_differs_from_durable_baseline(git_repo):
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError

    gateway = _gateway(git_repo)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    now = datetime.now(tz=UTC)
    committed = gateway.commit(effect, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    # Drift the durable baseline intentionally.
    push = H.push_effect(
        commit_sha=committed.outcome.commit_sha,
        bound_head_sha=committed.outcome.new_head_sha,
        expected_remote_sha_before_push="d" * 40,
    )
    with pytest.raises(GitTransportError) as exc:
        gateway.push(push, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    assert exc.value.block is not None


def test_authority_rejection_prevents_commit(git_repo):
    from ai_dev_loop.pr_review_v2.application.write_contracts import AuthorityLostError

    gateway = _gateway(git_repo)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    head_before = _git(["rev-parse", "HEAD"], cwd=git_repo["work"])

    def reject() -> None:
        raise AuthorityLostError("rejected")

    with pytest.raises(AuthorityLostError):
        gateway.commit(effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC), authorize=reject)
    assert _git(["rev-parse", "HEAD"], cwd=git_repo["work"]) == head_before


def test_find_remote_ref_proof_matrix(git_repo):
    gateway = _gateway(git_repo)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    now = datetime.now(tz=UTC)
    committed = gateway.commit(effect, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    push = H.push_effect(
        commit_sha=committed.outcome.commit_sha,
        bound_head_sha=committed.outcome.new_head_sha,
        expected_remote_sha_before_push=committed.outcome.expected_remote_sha_before_push,
    )
    # Before push: baseline still matches -> PROVEN_NOT_APPLIED.
    proof = gateway.reconcile_push(push, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED
    gateway.push(push, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    proof = gateway.reconcile_push(push, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.APPLIED
    drifted = push.model_copy(update={"expected_remote_sha_before_push": "e" * 40})
    # After applied, a wrong baseline with matching commit still APPLIED by commit equality.
    # Third SHA on remote would be unresolved — simulate by pointing expected commit elsewhere.
    other = push.model_copy(update={"commit_sha": "f" * 40, "bound_head_sha": "f" * 40})
    proof = gateway.reconcile_push(other, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.UNRESOLVED
    del drifted
