"""Phase 16.6 unit tests: direct argv Git/SSH write transport."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_write_helpers import SHA_A, SHA_B, GitCall, ScriptedGitRunner

from ai_dev_loop.pr_review_v2.application.github_read import GatewayBlockKind
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitProcessOutcome,
    GitTransportError,
    GitWriteTransport,
    build_minimal_git_env,
    extract_remote_nwo,
)

_REMOTE_URL = "git@github.com:acme/demo.git"
_DESTINATION = "git@github.com"


def _transport(
    runner: ScriptedGitRunner,
    *,
    env: dict[str, str] | None = None,
    cwd: str = "/tmp/repo",
) -> GitWriteTransport:
    return GitWriteTransport(
        repository_cwd=cwd,
        per_call_timeout_seconds=30.0,
        runner=runner,
        env=env if env is not None else {"PATH": "/usr/bin", "HOME": "/tmp"},
    )


def _make_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    assert stat.S_ISSOCK(path.stat().st_mode)


class _EffectiveAgentRunner(ScriptedGitRunner):
    """Scripted runner that gates ``ssh-add -l`` on the expected socket env value."""

    def __init__(self, *, required_sock: str | None = None) -> None:
        super().__init__()
        self.required_sock = required_sock

    def run(self, args, *, cwd, timeout, env, stdin_text=None):
        args = list(args)
        if args == ["ssh-add", "-l"]:
            self.calls.append(GitCall(args=args, stdin_text=stdin_text, env=dict(env)))
            sock = env.get("SSH_AUTH_SOCK")
            if self.required_sock is not None and sock != self.required_sock:
                return GitProcessOutcome(
                    returncode=2,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    argv=tuple(args),
                )
            for match, (rc, out, err, to) in self.scripts:
                if match == ("-l",):
                    return GitProcessOutcome(
                        returncode=rc, stdout=out, stderr=err, timed_out=to, argv=tuple(args)
                    )
            return GitProcessOutcome(
                returncode=0, stdout="", stderr="", timed_out=False, argv=tuple(args)
            )
        return super().run(args, cwd=cwd, timeout=timeout, env=env, stdin_text=stdin_text)


def test_minimal_env_allowlist_excludes_secrets() -> None:
    env = build_minimal_git_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "GH_TOKEN": "secret",
            "GITHUB_TOKEN": "secret2",
            "AWS_SECRET_ACCESS_KEY": "k",
        }
    )
    assert env["PATH"] == "/usr/bin"
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_read_head_sha_validates_full_sha() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-parse", "HEAD"), stdout=SHA_A + "\n")
    assert _transport(runner).read_head_sha() == SHA_A


def test_read_head_sha_rejects_short_sha() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-parse", "HEAD"), stdout="abc123\n")
    with pytest.raises(GitTransportError):
        _transport(runner).read_head_sha()


def test_read_current_branch() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-parse", "--abbrev-ref", "HEAD"), stdout="feature\n")
    assert _transport(runner).read_current_branch() == "feature"


def test_read_commit_parents_parses_rev_list() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-list", "--parents", "-n", "1", SHA_B), stdout=f"{SHA_B} {SHA_A}\n")
    assert _transport(runner).read_commit_parents(SHA_B) == (SHA_A,)


def test_read_remote_ref_single_entry() -> None:
    runner = ScriptedGitRunner()
    runner.add(
        ("ls-remote", "origin", "refs/heads/feature"),
        stdout=f"{SHA_A}\trefs/heads/feature\n",
    )
    obs = _transport(runner).read_remote_ref("origin", "feature")
    assert obs.sha == SHA_A
    assert obs.complete is True


def test_read_remote_ref_absent_returns_none() -> None:
    runner = ScriptedGitRunner()
    runner.add(("ls-remote", "origin", "refs/heads/feature"), stdout="")
    obs = _transport(runner).read_remote_ref("origin", "feature")
    assert obs.sha is None


def test_read_remote_ref_multiple_entries_blocks() -> None:
    runner = ScriptedGitRunner()
    runner.add(
        ("ls-remote", "origin", "refs/heads/feature"),
        stdout=f"{SHA_A}\trefs/heads/feature\n{SHA_B}\trefs/heads/feature\n",
    )
    with pytest.raises(GitTransportError):
        _transport(runner).read_remote_ref("origin", "feature")


def test_is_ancestor_true_false() -> None:
    runner = ScriptedGitRunner()
    runner.add(("merge-base", "--is-ancestor", SHA_A, SHA_B), returncode=0)
    assert _transport(runner).is_ancestor(SHA_A, SHA_B) is True
    runner2 = ScriptedGitRunner()
    runner2.add(("merge-base", "--is-ancestor", SHA_A, SHA_B), returncode=1)
    assert _transport(runner2).is_ancestor(SHA_A, SHA_B) is False


def test_is_ancestor_ambiguous_blocks() -> None:
    runner = ScriptedGitRunner()
    runner.add(("merge-base", "--is-ancestor", SHA_A, SHA_B), returncode=128)
    with pytest.raises(GitTransportError):
        _transport(runner).is_ancestor(SHA_A, SHA_B)


def test_commit_delivers_message_via_stdin() -> None:
    runner = ScriptedGitRunner()
    runner.add(("commit", "--no-verify", "--file", "-"), returncode=0)
    _transport(runner).commit_with_message_stdin("subject\n\nADL-Idempotency: x")
    call = runner.calls[-1]
    assert call.stdin_text == "subject\n\nADL-Idempotency: x"
    # The message never enters argv.
    assert "subject" not in " ".join(call.args)


def test_commit_rejects_empty_message() -> None:
    runner = ScriptedGitRunner()
    with pytest.raises(GitTransportError):
        _transport(runner).commit_with_message_stdin("   ")


def test_push_uses_explicit_non_force_refspec() -> None:
    runner = ScriptedGitRunner()
    runner.add(("push", "origin", f"{SHA_B}:refs/heads/feature"), returncode=0)
    _transport(runner).push_non_force(remote_name="origin", commit_sha=SHA_B, remote_ref="feature")
    args = runner.calls[-1].args
    assert "--force" not in args
    assert "--force-with-lease" not in args
    assert f"{SHA_B}:refs/heads/feature" in args


def test_timeout_raises_transient() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-parse", "HEAD"), timed_out=True)
    with pytest.raises(GitTransportError) as exc:
        _transport(runner).read_head_sha()
    assert exc.value.transient is not None


def test_nonzero_read_blocks() -> None:
    runner = ScriptedGitRunner()
    runner.add(("rev-parse", "HEAD"), returncode=1, stderr="fatal")
    with pytest.raises(GitTransportError) as exc:
        _transport(runner).read_head_sha()
    assert exc.value.block is not None


def test_extract_remote_nwo_ssh_forms() -> None:
    assert extract_remote_nwo("git@github.com:acme/demo.git") == "acme/demo"
    assert extract_remote_nwo("ssh://git@github.com/acme/demo.git") == "acme/demo"
    assert extract_remote_nwo("https://example.com/not-ssh") is None


def test_identity_agent_without_inherited_sock_prepares_and_pins_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate B regression: detached worker has no SSH_AUTH_SOCK; IdentityAgent wins."""

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    agent_sock = tmp_path / "effective.sock"
    _make_unix_socket(agent_sock)
    runner = _EffectiveAgentRunner(required_sock=str(agent_sock))
    runner.add(("-G", _DESTINATION), stdout=f"identityagent {agent_sock}\n")
    runner.add(("push", "origin", f"{SHA_B}:refs/heads/feature"), returncode=0)
    transport = _transport(runner, env={"PATH": "/usr/bin", "HOME": str(tmp_path)})

    before = dict(os.environ)
    transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    transport.push_non_force(remote_name="origin", commit_sha=SHA_B, remote_ref="feature")

    assert os.environ.get("SSH_AUTH_SOCK") == before.get("SSH_AUTH_SOCK")
    ssh_add = next(c for c in runner.calls if c.args == ["ssh-add", "-l"])
    assert ssh_add.env.get("SSH_AUTH_SOCK") == str(agent_sock)
    push = next(c for c in runner.calls if len(c.args) >= 2 and c.args[1] == "push")
    assert push.env.get("SSH_AUTH_SOCK") == str(agent_sock)
    for call in runner.calls:
        joined = " ".join(call.args)
        assert str(agent_sock) not in joined


def test_identity_agent_precedes_inherited_socket(tmp_path: Path) -> None:
    effective = tmp_path / "effective.sock"
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(effective)
    _make_unix_socket(inherited)
    runner = _EffectiveAgentRunner(required_sock=str(effective))
    runner.add(("-G", _DESTINATION), stdout=f"identityagent {effective}\n")
    transport = _transport(
        runner,
        env={"PATH": "/usr/bin", "HOME": str(tmp_path), "SSH_AUTH_SOCK": str(inherited)},
    )

    transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    ssh_add = next(c for c in runner.calls if c.args == ["ssh-add", "-l"])
    assert ssh_add.env.get("SSH_AUTH_SOCK") == str(effective)


def test_absent_identity_agent_falls_back_to_inherited(tmp_path: Path) -> None:
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(inherited)
    runner = _EffectiveAgentRunner(required_sock=str(inherited))
    runner.add(("-G", _DESTINATION), stdout="user git\nhostname github.com\n")
    transport = _transport(
        runner,
        env={"PATH": "/usr/bin", "HOME": str(tmp_path), "SSH_AUTH_SOCK": str(inherited)},
    )

    transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    ssh_add = next(c for c in runner.calls if c.args == ["ssh-add", "-l"])
    assert ssh_add.env.get("SSH_AUTH_SOCK") == str(inherited)


def test_identity_agent_none_without_inherited_fails_closed(tmp_path: Path) -> None:
    runner = ScriptedGitRunner()
    runner.add(("-G", _DESTINATION), stdout="identityagent none\n")
    transport = _transport(runner, env={"PATH": "/usr/bin", "HOME": str(tmp_path)})

    with pytest.raises(GitTransportError) as exc:
        transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.AUTHENTICATION
    assert "no usable SSH agent identity for push" in str(exc.value)
    assert not any(c.args[:1] == ["ssh-add"] for c in runner.calls)


def test_ssh_g_failure_fails_closed_without_push(tmp_path: Path) -> None:
    runner = ScriptedGitRunner()
    runner.add(("-G", _DESTINATION), returncode=1, stderr="config error")
    transport = _transport(runner, env={"PATH": "/usr/bin", "HOME": str(tmp_path)})

    with pytest.raises(GitTransportError) as exc:
        transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    message = str(exc.value)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.AUTHENTICATION
    assert "config error" not in message
    assert "SSH_AUTH_SOCK" not in message
    assert not any(c.args[:1] == ["ssh-add"] for c in runner.calls)


def test_ambiguous_identity_agent_fails_closed(tmp_path: Path) -> None:
    runner = ScriptedGitRunner()
    runner.add(
        ("-G", _DESTINATION),
        stdout="identityagent /tmp/a.sock\nidentityagent /tmp/b.sock\n",
    )
    transport = _transport(runner, env={"PATH": "/usr/bin", "HOME": str(tmp_path)})

    with pytest.raises(GitTransportError) as exc:
        transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    message = str(exc.value)
    assert "/tmp/a.sock" not in message
    assert "/tmp/b.sock" not in message
    assert not any(c.args[:1] == ["ssh-add"] for c in runner.calls)


def test_unusable_identity_agent_does_not_fall_back(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sock"
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(inherited)
    runner = ScriptedGitRunner()
    runner.add(("-G", _DESTINATION), stdout=f"identityagent {missing}\n")
    transport = _transport(
        runner,
        env={"PATH": "/usr/bin", "HOME": str(tmp_path), "SSH_AUTH_SOCK": str(inherited)},
    )

    with pytest.raises(GitTransportError) as exc:
        transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    message = str(exc.value)
    assert str(missing) not in message
    assert str(inherited) not in message
    assert not any(c.args[:1] == ["ssh-add"] for c in runner.calls)


def test_ssh_add_no_identity_fails_closed(tmp_path: Path) -> None:
    agent_sock = tmp_path / "agent.sock"
    _make_unix_socket(agent_sock)
    runner = ScriptedGitRunner()
    runner.add(("-G", _DESTINATION), stdout=f"identityagent {agent_sock}\n")
    runner.add(("-l",), returncode=1)
    transport = _transport(runner, env={"PATH": "/usr/bin", "HOME": str(tmp_path)})

    with pytest.raises(GitTransportError) as exc:
        transport.prepare_ssh_agent_for_remote(_REMOTE_URL)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.AUTHENTICATION
    assert str(agent_sock) not in str(exc.value)
    # Failed prepare must not leave the socket pinned for a later push.
    runner.add(("push", "origin", f"{SHA_B}:refs/heads/feature"), returncode=0)
    transport.push_non_force(remote_name="origin", commit_sha=SHA_B, remote_ref="feature")
    push = next(c for c in runner.calls if len(c.args) >= 2 and c.args[1] == "push")
    assert push.env.get("SSH_AUTH_SOCK") != str(agent_sock)
