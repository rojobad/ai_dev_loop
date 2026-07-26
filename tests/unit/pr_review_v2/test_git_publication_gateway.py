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


# -- SSH agent preflight (Gate B effective IdentityAgent) -------------------


def _ssh_gateway(
    tmp_path: Path,
    *,
    ssh_ok: bool,
    remote_url: str = "git@github.com:acme/demo.git",
) -> tuple[GitPublicationGateway, object]:
    from tests.unit.pr_review_v2.write_helpers import SHA_COMMIT, FakeGitTransport

    transport = FakeGitTransport(
        root=str(tmp_path / "work"),
        head=SHA_COMMIT,
        remote_url=remote_url,
        remote_shas={"refs/heads/feature": None},
        ssh_ok=ssh_ok,
    )
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    policy = GitWritePolicy(
        repository_cwd=str(tmp_path / "work"),
        remote_scheme=GitRemoteScheme.SSH,
        require_ssh_agent_identity=True,
    )
    gateway = GitPublicationGateway(
        policy=policy,
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(tmp_path / "art"),
        remote_name="origin",
    )
    return gateway, transport


def test_gateway_push_fails_closed_when_ssh_agent_unusable(tmp_path: Path) -> None:
    from tests.unit.pr_review_v2.write_helpers import SHA_COMMIT

    gateway, transport = _ssh_gateway(tmp_path, ssh_ok=False)
    push = push_effect(commit_sha=SHA_COMMIT, remote_ref="feature")
    with pytest.raises(GitTransportError) as exc:
        gateway.push(push, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    assert exc.value.block is not None
    assert "no usable SSH agent identity for push" in str(exc.value)
    assert transport.ssh_prepare_urls == [transport.remote_url]
    assert transport.pushes == []
    assert "SSH_AUTH_SOCK" not in str(exc.value)


def test_gateway_push_prepares_effective_agent_before_push(tmp_path: Path) -> None:
    from tests.unit.pr_review_v2.write_helpers import SHA_COMMIT

    gateway, transport = _ssh_gateway(tmp_path, ssh_ok=True)
    push = push_effect(commit_sha=SHA_COMMIT, remote_ref="feature")
    auth = _Authorizer()
    result = gateway.push(push, run_id=RUN_ID, now=T0, authorize=auth)
    assert auth.calls == 1
    assert result.already_applied is False
    assert transport.ssh_prepare_urls == [transport.remote_url]
    assert transport.pushes == [SHA_COMMIT]


def test_gateway_identity_agent_regression_through_real_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway preflight + push use IdentityAgent when inherited sock is absent."""

    import socket
    import stat

    from tests.unit.pr_review_v2.test_git_write_transport import _EffectiveAgentRunner

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    agent_sock = tmp_path / "effective.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(agent_sock))
    finally:
        sock.close()
    assert stat.S_ISSOCK(agent_sock.stat().st_mode)

    commit_sha = "b" * 40

    class _Runner(_EffectiveAgentRunner):
        def __init__(self) -> None:
            super().__init__(required_sock=str(agent_sock))
            self._ls_remote_calls = 0

        def run(self, args, *, cwd, timeout, env, stdin_text=None):
            args = list(args)
            if len(args) >= 4 and args[1:4] == ["ls-remote", "origin", "refs/heads/feature"]:
                from tests.unit.pr_review_v2.github_write_helpers import GitCall

                from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
                    GitProcessOutcome,
                )

                self.calls.append(GitCall(args=args, stdin_text=stdin_text, env=dict(env)))
                self._ls_remote_calls += 1
                if self._ls_remote_calls == 1:
                    return GitProcessOutcome(
                        returncode=0, stdout="", stderr="", timed_out=False, argv=tuple(args)
                    )
                return GitProcessOutcome(
                    returncode=0,
                    stdout=f"{commit_sha}\trefs/heads/feature\n",
                    stderr="",
                    timed_out=False,
                    argv=tuple(args),
                )
            return super().run(args, cwd=cwd, timeout=timeout, env=env, stdin_text=stdin_text)

    runner = _Runner()
    runner.add(("rev-parse", "--show-toplevel"), stdout=f"{work}\n")
    runner.add(("rev-parse", "HEAD"), stdout=f"{commit_sha}\n")
    runner.add(("rev-parse", "--abbrev-ref", "HEAD"), stdout="feature\n")
    runner.add(("remote", "get-url", "origin"), stdout="git@github.com:acme/demo.git\n")
    runner.add(("-G", "git@github.com"), stdout=f"identityagent {agent_sock}\n")
    runner.add(("push", "origin", f"{commit_sha}:refs/heads/feature"), returncode=0)

    transport = GitWriteTransport(
        repository_cwd=str(work),
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    policy = GitWritePolicy(
        repository_cwd=str(work),
        remote_scheme=GitRemoteScheme.SSH,
        require_ssh_agent_identity=True,
    )
    gateway = GitPublicationGateway(
        policy=policy,
        transport=transport,
        input_reader=InputArtifactReader(tmp_path / "art"),
        remote_name="origin",
    )
    push = push_effect(commit_sha=commit_sha, remote_ref="feature")
    result = gateway.push(push, run_id=RUN_ID, now=T0, authorize=_Authorizer())
    assert result.already_applied is False

    ssh_add = next(c for c in runner.calls if c.args == ["ssh-add", "-l"])
    assert ssh_add.env.get("SSH_AUTH_SOCK") == str(agent_sock)
    push_calls = [c for c in runner.calls if len(c.args) >= 2 and c.args[1] == "push"]
    assert len(push_calls) == 1
    assert push_calls[0].env.get("SSH_AUTH_SOCK") == str(agent_sock)
    for call in runner.calls:
        assert str(agent_sock) not in " ".join(call.args)
