"""Phase 15.13: typed ssh-agent identity failure during publication."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.commands.pr_review import (
    _apply_worker_failure,
    _classify_worker_outcome,
)
from ai_dev_loop.errors import SshAgentNoIdentityError, ValidationError
from ai_dev_loop.runners.publish import verify_ssh_push_ready
from ai_dev_loop.state import GithubPrReviewState, RunStatus, load_run_state, save_run_state


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_fake_ssh(bin_dir: Path, *, identity_agent: str = "none") -> Path:
    path = bin_dir / "ssh"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-G" ]; then\n'
        f'  printf "identityagent {identity_agent}\\n"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_fake_ssh_add(bin_dir: Path, *, exit_code: int) -> Path:
    path = bin_dir / "ssh-add"
    path.write_text(
        f'#!/bin/sh\nif [ "$1" = "-l" ]; then\n  exit {exit_code}\nfi\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _install_ssh_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ssh_add_exit: int
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_ssh(bin_dir, identity_agent="none")
    _write_fake_ssh_add(bin_dir, exit_code=ssh_add_exit)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)


def test_verify_ssh_push_ready_raises_typed_error_without_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/demo.git")
    _install_ssh_fakes(tmp_path, monkeypatch, ssh_add_exit=1)

    with pytest.raises(SshAgentNoIdentityError, match="ssh-agent has no usable keys"):
        verify_ssh_push_ready(repo, "origin")


def test_verify_ssh_push_ready_http_remote_remains_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
    _install_ssh_fakes(tmp_path, monkeypatch, ssh_add_exit=0)

    with pytest.raises(ValidationError, match="SSH remote URL"):
        verify_ssh_push_ready(repo, "origin")


def test_classify_typed_ssh_error_interrupts_only_during_publication() -> None:
    gpr = GithubPrReviewState(
        source_run_id="src",
        lifecycle="publishing_external_fix",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch="feature",
        bound_head_sha="a" * 40,
        publication_phase="pre_commit",
    )
    status, outcome = _classify_worker_outcome(
        SshAgentNoIdentityError("ssh-agent has no usable keys"),
        gpr=gpr,
    )
    assert status == "interrupted"
    assert outcome == "ssh_agent_no_identity"

    status_failed, outcome_failed = _classify_worker_outcome(
        SshAgentNoIdentityError("ssh-agent has no usable keys"),
        gpr=None,
    )
    assert status_failed == "failed"
    assert outcome_failed == "ssh_agent_no_identity"


def test_generic_validation_error_stays_terminal_during_publication() -> None:
    gpr = GithubPrReviewState(
        source_run_id="src",
        lifecycle="publishing_external_fix",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch="feature",
        bound_head_sha="a" * 40,
        publication_phase="pre_commit",
    )
    status, outcome = _classify_worker_outcome(
        ValidationError("staged patch changed during publication"),
        gpr=gpr,
    )
    assert status == "failed"
    assert outcome == "validation_error"


def test_apply_worker_failure_preserves_pre_commit_on_typed_ssh(
    prepared_run: dict[str, Path | str],
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="src",
        lifecycle="publishing_external_fix",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=state.repository.initial_head,
        publication_phase="pre_commit",
        publication_text_path="github/cycles/02/publication-text.json",
        staged_patch_sha256="b" * 64,
    )
    save_run_state(run_path, state)

    _apply_worker_failure(
        run_path,
        SshAgentNoIdentityError("ssh-agent has no usable keys; preload the SSH key"),
    )
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review is not None
    assert final.github_pr_review.publication_phase == "pre_commit"
    assert final.github_pr_review.lifecycle == "publishing_external_fix"
    assert final.github_pr_review.worker_outcome == "ssh_agent_no_identity"
    assert "ssh-add" not in (final.last_error or "").lower()


def test_publish_with_missing_ssh_identity_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.runners.publish import PublicationText, publish_accepted_staged_patch

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
    _install_ssh_fakes(tmp_path, monkeypatch, ssh_add_exit=1)
    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.resolve_upstream",
        lambda *_a, **_k: ("origin", "master"),
    )
    _git(repo, "remote", "add", "origin", "git@github.com:acme/demo.git")

    with pytest.raises(SshAgentNoIdentityError):
        publish_accepted_staged_patch(
            repo,
            branch="master",
            text=PublicationText("subject", "body", "title", "pr body"),
        )
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "diff", "--cached", "--name-only") == "a.txt"


def test_apply_worker_failure_validation_error_marks_failed(
    prepared_run: dict[str, Path | str],
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="src",
        lifecycle="publishing_external_fix",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=state.repository.initial_head,
        publication_phase="pre_commit",
        publication_text_path="github/cycles/02/publication-text.json",
        staged_patch_sha256="b" * 64,
    )
    save_run_state(run_path, state)

    _apply_worker_failure(run_path, ValidationError("PR head SHA does not match published commit"))
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.FAILED
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "failed"
    assert final.github_pr_review.publication_phase == "pre_commit"
    assert final.github_pr_review.worker_outcome == "validation_error"
