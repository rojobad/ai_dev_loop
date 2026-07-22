"""Phase 16.6 unit tests: direct argv Git/SSH write transport."""

from __future__ import annotations

import pytest
from tests.unit.pr_review_v2.github_write_helpers import SHA_A, SHA_B, ScriptedGitRunner

from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitTransportError,
    GitWriteTransport,
    build_minimal_git_env,
    extract_remote_nwo,
)


def _transport(runner: ScriptedGitRunner) -> GitWriteTransport:
    return GitWriteTransport(
        repository_cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
    )


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


def test_check_ssh_agent_true_false() -> None:
    # check_ssh_agent runs ["ssh-add", "-l"], so the argv tail is ("-l",).
    runner2 = ScriptedGitRunner()
    runner2.add(("-l",), returncode=0)
    assert _transport(runner2).check_ssh_agent() is True
    runner3 = ScriptedGitRunner()
    runner3.add(("-l",), returncode=1)
    assert _transport(runner3).check_ssh_agent() is False
