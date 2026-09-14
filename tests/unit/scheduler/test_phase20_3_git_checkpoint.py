"""Git checkpoint adapter tests in disposable repositories."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dev_loop.runners.git import (
    CheckpointGitDeadline,
    checkpoint_git_write_tree,
    git_add_all,
    git_diff_cached_patch_bytes,
    git_rev_parse,
    git_symbolic_ref,
    resolve_git_identity,
)
from ai_dev_loop.scheduler.application.git_checkpoint import (
    CheckpointFenceError,
    GitCheckpointError,
    ProductionGitCheckpointPort,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    GitIdentitySnapshot,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointTrustedTree,
)


@pytest.fixture
def disposable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "checkpoint@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Checkpoint User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", tracked], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tracked.write_text("baseline\nstaged change\n", encoding="utf-8")
    git_add_all(repo)
    return repo


def _intent_for_repo(repo: Path) -> tuple[SequenceCheckpointIntent, bytes]:
    parent_head = git_rev_parse(repo, "HEAD")
    branch_ref = git_symbolic_ref(repo, "HEAD")
    patch_bytes = git_diff_cached_patch_bytes(repo)
    identity = resolve_git_identity(repo)
    intent = SequenceCheckpointIntent(
        sequence_id="seq-test",
        sequence_version=2,
        predecessor_run_id="run-1",
        predecessor_run_version=4,
        predecessor_ordinal=1,
        successor_run_id="run-2",
        successor_ordinal=2,
        accepted_outcome="completed",
        branch_ref=branch_ref,
        parent_head=parent_head,
        reviewed_patch_sha256=__import__("hashlib").sha256(patch_bytes).hexdigest(),
        commit_message="checkpoint after phase one",
        git_identity=GitIdentitySnapshot(
            author_name=identity.author_name,
            author_email=identity.author_email,
            author_date=identity.author_date,
            committer_name=identity.committer_name,
            committer_email=identity.committer_email,
            committer_date=identity.committer_date,
        ),
        repository_root=str(repo.resolve()),
        git_common_dir=str((repo / ".git").resolve()),
        git_dir=str((repo / ".git").resolve()),
        staged_patch_artifact_path="git/diffs/01.patch",
        review_result_artifact_path="codex/reviews/01.json",
        review_result_sha256="a" * 64,
    )
    return intent, patch_bytes


def _noop_persist(_: str) -> None:
    return None


def _trusted_tree_for_intent(
    intent: SequenceCheckpointIntent,
    *,
    intent_sha256: str = "a" * 64,
    tree_sha: str | None = None,
) -> SequenceCheckpointTrustedTree:
    repo = Path(intent.repository_root)
    reviewed_tree_sha = tree_sha or checkpoint_git_write_tree(repo)
    return SequenceCheckpointTrustedTree(
        intent_sha256=intent_sha256,
        reviewed_tree_sha256=reviewed_tree_sha,
        reviewed_patch_sha256=intent.reviewed_patch_sha256,
        parent_head=intent.parent_head,
        recorded_at="2026-09-13T12:00:00.000000Z",
    )


def _checkpoint_deadline() -> CheckpointGitDeadline:
    return CheckpointGitDeadline.from_lease(
        datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
    )


def _now_factory() -> datetime:
    return datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _execute_checkpoint(
    port: ProductionGitCheckpointPort,
    intent: SequenceCheckpointIntent,
    *,
    patch_path: Path,
    evidence: SequenceCheckpointEvidence | None = None,
    persist_tree_sha=_noop_persist,
    persist_commit_sha=_noop_persist,
    mutation_fence=None,
    trusted_tree: SequenceCheckpointTrustedTree | None = None,
) -> object:
    resolved_tree = trusted_tree or _trusted_tree_for_intent(intent)
    return port.execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=resolved_tree,
        evidence=evidence,
        persist_tree_sha=persist_tree_sha,
        persist_commit_sha=persist_commit_sha,
        mutation_fence=mutation_fence,
        deadline=_checkpoint_deadline(),
        now_factory=_now_factory,
    )


def test_git_checkpoint_creates_unsigned_commit_without_hooks(
    disposable_repo: Path, tmp_path: Path
) -> None:
    hook = disposable_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    trusted_tree = _trusted_tree_for_intent(intent)
    commit = port.execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=_noop_persist,
        persist_commit_sha=_noop_persist,
        deadline=_checkpoint_deadline(),
        now_factory=_now_factory,
    )
    assert git_rev_parse(disposable_repo, "HEAD") == commit.commit_sha
    assert git_rev_parse(disposable_repo, "HEAD^{tree}") == commit.tree_sha
    assert commit.tree_sha == checkpoint_git_write_tree(disposable_repo)


def test_git_checkpoint_survives_reference_transaction_hook(
    disposable_repo: Path, tmp_path: Path
) -> None:
    hook = disposable_repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    trusted_tree = _trusted_tree_for_intent(intent)
    commit = port.execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=_noop_persist,
        persist_commit_sha=_noop_persist,
        deadline=_checkpoint_deadline(),
        now_factory=_now_factory,
    )
    assert git_rev_parse(disposable_repo, "HEAD") == commit.commit_sha


def test_git_checkpoint_survives_configured_hooks_path(
    disposable_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()
    failing_hook = external_hooks / "pre-commit"
    failing_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    failing_hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(external_hooks)],
        cwd=disposable_repo,
        check=True,
        capture_output=True,
    )
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    trusted_tree = _trusted_tree_for_intent(intent)
    commit = port.execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=_noop_persist,
        persist_commit_sha=_noop_persist,
        deadline=_checkpoint_deadline(),
        now_factory=_now_factory,
    )
    assert git_rev_parse(disposable_repo, "HEAD") == commit.commit_sha


def test_git_checkpoint_adopts_already_applied_commit_with_empty_staged_index(
    disposable_repo: Path, tmp_path: Path
) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    first = _execute_checkpoint(port, intent, patch_path=patch_path)
    trusted_tree = _trusted_tree_for_intent(intent, tree_sha=first.tree_sha)
    evidence = SequenceCheckpointEvidence(
        tree_sha256=first.tree_sha,
        commit_sha256=first.commit_sha,
    )
    adopted = _execute_checkpoint(
        port,
        intent,
        patch_path=patch_path,
        evidence=evidence,
        trusted_tree=trusted_tree,
    )
    assert adopted.already_applied is True
    assert adopted.commit_sha == first.commit_sha
    assert git_rev_parse(disposable_repo, "HEAD") == first.commit_sha


def test_git_checkpoint_rejects_parent_head_drift(disposable_repo: Path, tmp_path: Path) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    drifted = intent.model_copy(update={"parent_head": "0" * 40})
    port = ProductionGitCheckpointPort()
    with pytest.raises(GitCheckpointError, match="parent HEAD drift"):
        _execute_checkpoint(port, drifted, patch_path=patch_path)


def test_git_checkpoint_rejects_staged_patch_drift(disposable_repo: Path, tmp_path: Path) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    (disposable_repo / "tracked.txt").write_text("different staged content\n", encoding="utf-8")
    git_add_all(disposable_repo)
    port = ProductionGitCheckpointPort()
    with pytest.raises(GitCheckpointError, match="staged patch hash drift"):
        _execute_checkpoint(port, intent, patch_path=patch_path)


def test_git_checkpoint_persists_tree_before_commit(disposable_repo: Path, tmp_path: Path) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    persisted: list[str] = []

    def capture_tree(tree_sha: str) -> None:
        persisted.append(f"tree:{tree_sha}")

    def capture_commit(commit_sha: str) -> None:
        persisted.append(f"commit:{commit_sha}")

    port = ProductionGitCheckpointPort()
    commit = _execute_checkpoint(
        port,
        intent,
        patch_path=patch_path,
        persist_tree_sha=capture_tree,
        persist_commit_sha=capture_commit,
    )
    assert persisted[0].startswith("tree:")
    assert persisted[1].startswith("commit:")
    assert persisted.index(f"tree:{commit.tree_sha}") < persisted.index(
        f"commit:{commit.commit_sha}"
    )


def test_git_checkpoint_fence_blocks_commit_tree_before_mutation(
    disposable_repo: Path, tmp_path: Path
) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    parent_before = git_rev_parse(disposable_repo, "HEAD")

    def fence(boundary: str) -> None:
        if boundary == "pre_commit_tree":
            raise CheckpointFenceError("checkpoint_aborted")

    port = ProductionGitCheckpointPort()
    with pytest.raises(CheckpointFenceError, match="checkpoint_aborted"):
        _execute_checkpoint(
            port,
            intent,
            patch_path=patch_path,
            mutation_fence=fence,
        )
    assert git_rev_parse(disposable_repo, "HEAD") == parent_before


def test_git_checkpoint_rejects_extra_staged_changes_after_cas(
    disposable_repo: Path, tmp_path: Path
) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    first = _execute_checkpoint(port, intent, patch_path=patch_path)
    (disposable_repo / "tracked.txt").write_text("baseline\nextra after cas\n", encoding="utf-8")
    git_add_all(disposable_repo)
    evidence = SequenceCheckpointEvidence(
        tree_sha256=first.tree_sha,
        commit_sha256=first.commit_sha,
    )
    from ai_dev_loop.errors import ValidationError

    with pytest.raises(ValidationError, match="staged index must be empty"):
        _execute_checkpoint(
            port,
            intent,
            patch_path=patch_path,
            evidence=evidence,
            trusted_tree=_trusted_tree_for_intent(intent, tree_sha=first.tree_sha),
        )


def test_git_checkpoint_rejects_parent_drift_from_empty_commit(
    disposable_repo: Path, tmp_path: Path
) -> None:
    intent, patch_bytes = _intent_for_repo(disposable_repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    first = _execute_checkpoint(port, intent, patch_path=patch_path)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "unrelated empty"],
        cwd=disposable_repo,
        check=True,
        capture_output=True,
    )
    tracked = disposable_repo / "tracked.txt"
    tracked.write_text("baseline\nstaged change\n", encoding="utf-8")
    git_add_all(disposable_repo)
    phase_two_intent = intent.model_copy(
        update={
            "parent_head": first.commit_sha,
            "predecessor_ordinal": 2,
            "successor_ordinal": 3,
            "predecessor_run_id": "run-2",
        }
    )
    with pytest.raises(GitCheckpointError, match="parent HEAD drift"):
        _execute_checkpoint(port, phase_two_intent, patch_path=patch_path)


def test_checkpoint_git_read_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ai_dev_loop.runners import git as git_module

    def slow_run_process(*args, **kwargs):  # type: ignore[no-untyped-def]
        from ai_dev_loop.process import ProcessResult

        return ProcessResult(
            args=list(args[0]) if args else [],
            returncode=0,
            stdout="",
            stderr="",
            timed_out=True,
        )

    monkeypatch.setattr(git_module, "run_process", slow_run_process)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    with pytest.raises(Exception, match="timed out"):
        git_module.checkpoint_git_status_porcelain(repo)


def test_checkpoint_git_subprocess_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ai_dev_loop import process as process_module
    from ai_dev_loop.runners import git as git_module

    def slow_run_process(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = process_module.ProcessResult(
            args=list(args[0]) if args else [],
            returncode=0,
            stdout="",
            stderr="",
            timed_out=True,
        )
        return result

    monkeypatch.setattr(git_module, "run_process", slow_run_process)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(Exception, match="timed out"):
        git_module.checkpoint_git_write_tree(repo)
