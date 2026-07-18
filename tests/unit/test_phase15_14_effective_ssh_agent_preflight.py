"""Phase 15.14: effective OpenSSH IdentityAgent resolution for publish preflight."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.errors import SshAgentNoIdentityError, ValidationError
from ai_dev_loop.runners.publish import (
    PublicationText,
    _parse_identity_agent_from_ssh_g,
    publish_accepted_staged_patch,
    resolve_effective_ssh_auth_sock,
    ssh_destination_from_remote_url,
    verify_ssh_push_ready,
)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


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


def _write_fake_ssh(bin_dir: Path, *, identity_agent_lines: list[str], exit_code: int = 0) -> Path:
    path = bin_dir / "ssh"
    # Emit only allowlisted identityagent lines; record argv destination safely.
    marker = bin_dir / "ssh-g-destination.txt"
    body = "\n".join(identity_agent_lines) + ("\n" if identity_agent_lines else "")
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-G" ]; then\n'
        f'  printf "%s" "$2" > "{marker}"\n'
        f"  cat <<'EOF'\n{body}EOF\n"
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_fake_ssh_add(bin_dir: Path, *, exit_code: int, record_path: Path) -> Path:
    path = bin_dir / "ssh-add"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-l" ]; then\n'
        f'  if [ -n "$SSH_AUTH_SOCK" ]; then\n'
        f'    printf "set\\n" > "{record_path}"\n'
        f"  else\n"
        f'    printf "empty\\n" > "{record_path}"\n'
        f"  fi\n"
        # Record only whether the sock path equals the expected marker file name
        # via a companion file written by the test helper when needed.
        f'  printf "%s" "$SSH_AUTH_SOCK" > "{record_path}.sock"\n'
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _install_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity_agent_lines: list[str],
    ssh_add_exit: int = 0,
    ssh_exit: int = 0,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = bin_dir / "ssh-add-env.txt"
    _write_fake_ssh(bin_dir, identity_agent_lines=identity_agent_lines, exit_code=ssh_exit)
    _write_fake_ssh_add(bin_dir, exit_code=ssh_add_exit, record_path=record)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir, record


def _init_ssh_repo(tmp_path: Path, remote_url: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", remote_url)
    return repo


def test_empty_ssh_auth_sock_uses_identity_agent_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_sock = tmp_path / "agent" / "effective.sock"
    _make_unix_socket(agent_sock)
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[f"identityagent {agent_sock}"],
        ssh_add_exit=0,
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    verify_ssh_push_ready(repo, "origin")

    assert record.read_text(encoding="utf-8") == "set\n"
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(agent_sock)
    destination = (_bin / "ssh-g-destination.txt").read_text(encoding="utf-8")
    assert destination == "git@github.com"


def test_identity_agent_precedes_inherited_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effective = tmp_path / "effective.sock"
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(effective)
    _make_unix_socket(inherited)
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[f"identityagent {effective}"],
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", str(inherited))

    verify_ssh_push_ready(repo, "origin")
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(effective)


def test_absent_identity_agent_falls_back_to_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(inherited)
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=["user git", "hostname github.com"],
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", str(inherited))

    verify_ssh_push_ready(repo, "origin")
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(inherited)


def test_identity_agent_none_falls_back_to_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = tmp_path / "inherited.sock"
    _make_unix_socket(inherited)
    repo = _init_ssh_repo(tmp_path, "ssh://git@github.com/acme/demo.git")
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=["identityagent none"],
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", str(inherited))

    verify_ssh_push_ready(repo, "origin")
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(inherited)
    destination = (_bin / "ssh-g-destination.txt").read_text(encoding="utf-8")
    assert destination == "git@github.com"


def test_ssh_url_with_port_preserves_alias_destination() -> None:
    assert ssh_destination_from_remote_url("ssh://git@gh-work:22/acme/demo.git") == "git@gh-work"


def test_scp_destination_preserves_alias() -> None:
    assert ssh_destination_from_remote_url("git@my-alias:acme/demo.git") == "git@my-alias"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/demo.git",
        "http://github.com/acme/demo.git",
        "git@github.com",
        "git@:acme/demo.git",
        "git@host:/absolute/path.git",
        "ssh://git@github.com",
        "ssh:///acme/demo.git",
        "ssh://-oProxyCommand=evil/acme/demo.git",
        "git@-oProxyCommand=evil:acme/demo.git",
        "file:///tmp/repo.git",
    ],
)
def test_remote_url_rejections(url: str) -> None:
    with pytest.raises(ValidationError):
        ssh_destination_from_remote_url(url)


def test_ambiguous_identity_agent_is_validation_error() -> None:
    with pytest.raises(ValidationError, match="ambiguous IdentityAgent"):
        _parse_identity_agent_from_ssh_g("identityagent /tmp/a.sock\nidentityagent /tmp/b.sock\n")


def test_relative_identity_agent_is_validation_error() -> None:
    with pytest.raises(ValidationError, match="absolute socket path"):
        _parse_identity_agent_from_ssh_g("identityagent relative.sock\n")


def test_missing_identity_agent_socket_is_validation_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sock"
    with pytest.raises(ValidationError, match="usable SSH agent socket"):
        _parse_identity_agent_from_ssh_g(f"identityagent {missing}\n")


def test_non_socket_identity_agent_is_validation_error(tmp_path: Path) -> None:
    regular = tmp_path / "not-a-socket"
    regular.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="usable SSH agent socket"):
        _parse_identity_agent_from_ssh_g(f"identityagent {regular}\n")


def test_no_socket_raises_typed_identity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _install_fakes(tmp_path, monkeypatch, identity_agent_lines=["identityagent none"])
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    with pytest.raises(SshAgentNoIdentityError, match="ssh-agent has no usable keys"):
        resolve_effective_ssh_auth_sock(repo, "git@github.com:acme/demo.git")


def test_ssh_add_failure_is_typed_and_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_sock = tmp_path / "agent.sock"
    _make_unix_socket(agent_sock)
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[f"identityagent {agent_sock}"],
        ssh_add_exit=1,
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    with pytest.raises(SshAgentNoIdentityError) as exc_info:
        verify_ssh_push_ready(repo, "origin")
    message = str(exc_info.value)
    assert str(agent_sock) not in message
    assert "SSH_AUTH_SOCK" not in message
    assert "identityagent" not in message.lower()
    assert "stdout" not in message.lower()


def test_ssh_g_failure_is_validation_error_not_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_ssh_repo(tmp_path, "git@github.com:acme/demo.git")
    _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[],
        ssh_exit=1,
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    with pytest.raises(ValidationError, match="unable to resolve effective SSH"):
        verify_ssh_push_ready(repo, "origin")


def test_publish_does_not_commit_when_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/demo.git")
    _install_fakes(tmp_path, monkeypatch, identity_agent_lines=["identityagent none"])
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.resolve_upstream",
        lambda *_a, **_k: ("origin", "master"),
    )

    with pytest.raises(SshAgentNoIdentityError):
        publish_accepted_staged_patch(
            repo,
            branch="master",
            text=PublicationText("subject", "body", "title", "pr body"),
        )
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_publish_proceeds_with_effective_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_sock = tmp_path / "agent.sock"
    _make_unix_socket(agent_sock)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/demo.git")
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[f"identityagent {agent_sock}"],
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.resolve_upstream",
        lambda *_a, **_k: ("origin", "master"),
    )
    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.expected_remote_head",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.push_branch_non_force",
        lambda *_a, **_k: None,
    )

    result = publish_accepted_staged_patch(
        repo,
        branch="master",
        text=PublicationText("subject", "body", "title", "pr body"),
    )
    assert len(result.commit_sha) == 40
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(agent_sock)


def test_github_doctor_uses_shared_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    from ai_dev_loop.commands.pr_review import github_doctor

    agent_sock = tmp_path / "agent.sock"
    _make_unix_socket(agent_sock)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/demo.git")
    (repo / "ai_dev_loop.yaml").write_text(
        "version: 1\n"
        "project:\n  name: fixture-project\n"
        "cursor:\n  command: agent\n  model: m\n  output_format: stream-json\n"
        "  force: true\n  trust_workspace: true\n  sandbox: disabled\n"
        "codex:\n  command: codex\n  review_skill: review-staged-cursor-execution\n"
        "  sandbox: workspace-write\n"
        "workflow:\n  max_review_iterations: 3\n  require_clean_worktree: true\n"
        "  stage_mode: all\n  cursor_timeout_minutes: 90\n  codex_timeout_minutes: 90\n"
        "prompt:\n  directory: docs/plans\n"
        "  filename_template: prompt_{plan_stem}.txt\n"
        "github:\n  enabled: true\n",
        encoding="utf-8",
    )
    _bin, record = _install_fakes(
        tmp_path,
        monkeypatch,
        identity_agent_lines=[f"identityagent {agent_sock}"],
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.check_gh_auth",
        lambda *_a, **_k: type(
            "Auth",
            (),
            {"authenticated": True, "detail": "ok", "login_redacted": "ac***"},
        )(),
    )

    text = github_doctor(repo_path=repo, output="json")
    payload = json.loads(text)
    ssh_checks = [item for item in payload["checks"] if item["name"] == "ssh_push_ready"]
    assert ssh_checks
    assert ssh_checks[0]["ok"] is True
    assert Path(f"{record}.sock").read_text(encoding="utf-8") == str(agent_sock)
    assert str(agent_sock) not in text
    assert "SSH_AUTH_SOCK" not in text
    assert (_bin / "ssh-g-destination.txt").read_text(encoding="utf-8") == "git@github.com"
