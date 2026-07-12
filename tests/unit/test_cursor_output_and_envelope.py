"""Unit tests for post-Cursor fingerprints and correction envelopes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    build_correction_execution_envelope,
    build_usage_limit_continuation_envelope,
    read_cursor_prompt,
)
from ai_dev_loop.runners.cursor_output import (
    capture_cursor_output_fingerprint,
    recompute_cursor_output_fingerprint,
)
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RecoveryState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    sha256_file,
    utc_now,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _state(repo: Path) -> RunState:
    now = utc_now()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return RunState(
        run_id="fixture-project-20260704T134512Z-abc123",
        project=ProjectRef(name="fixture-project"),
        status=RunStatus.RUNNING_CURSOR,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(repo),
            git_common_dir=str(repo / ".git"),
            git_dir=str(repo / ".git"),
            branch=branch,
            initial_head=head,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256=sha256_file(repo / "docs/plans/sample-plan.md"),
        ),
        prompt=PromptState(
            source_repository_path="docs/plans/prompt_sample-plan.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/plans/sample-plan.md").write_text("# plan\n", encoding="utf-8")
    (root / "docs/plans/prompt_sample-plan.txt").write_text("prompt\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    return root


def test_usage_limit_continuation_envelope_embeds_exact_source_prompt() -> None:
    exact = "Implement the approved plan exactly.\nPreserve guardrails.\n"
    envelope = build_usage_limit_continuation_envelope(exact)
    assert exact in envelope
    assert envelope.endswith(exact)
    assert "Previous orchestrator prompt follows verbatim:" in envelope
    assert "usage limit" in envelope.lower()


def test_usage_limit_continuation_envelope_preserves_crlf_and_missing_trailing_newline() -> None:
    exact = "Implement the plan.\r\nKeep tests green."
    assert not exact.endswith("\n")
    envelope = build_usage_limit_continuation_envelope(exact)
    assert exact in envelope
    assert "\r\n" in envelope
    assert envelope.endswith(exact)


def test_correction_envelope_embeds_exact_fix_prompt(tmp_path: Path) -> None:
    exact = "Fix finding A.\nAlso fix finding B.\n"
    envelope = build_correction_execution_envelope(exact)
    assert exact in envelope
    assert envelope.endswith(exact)
    assert "git add -A" in envelope
    assert "Do not commit" in envelope

    run_dir = tmp_path / "run"
    (run_dir / "prompts/fixes").mkdir(parents=True)
    (run_dir / "prompts/fixes/01.txt").write_bytes(exact.encode("utf-8"))
    now = utc_now()
    state = RunState(
        run_id="r",
        project=ProjectRef(name="p"),
        status=RunStatus.WAITING_FOR_CURSOR_FIX,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(tmp_path),
            git_common_dir=str(tmp_path),
            git_dir=str(tmp_path),
            branch="main",
            initial_head="abc",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(repository_path="p.md", snapshot_path="plan/plan.md", sha256="a" * 64),
        prompt=PromptState(
            source_repository_path="p.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )
    sent = read_cursor_prompt(state, run_dir, 2)
    assert exact in sent
    assert sent.endswith(exact)
    persisted = (run_dir / "prompts/fixes/01.execution-envelope.txt").read_bytes().decode("utf-8")
    assert persisted == sent
    assert (run_dir / "prompts/fixes/01.txt").read_bytes() == exact.encode("utf-8")


def test_correction_envelope_preserves_crlf_and_missing_trailing_newline(tmp_path: Path) -> None:
    exact = "Fix finding A.\r\nAlso fix finding B."
    assert not exact.endswith("\n")
    envelope = build_correction_execution_envelope(exact)
    assert exact in envelope
    assert "\r\n" in envelope
    assert envelope.endswith(exact)

    run_dir = tmp_path / "run"
    (run_dir / "prompts/fixes").mkdir(parents=True)
    (run_dir / "prompts/fixes/01.txt").write_bytes(exact.encode("utf-8"))
    now = utc_now()
    state = RunState(
        run_id="r",
        project=ProjectRef(name="p"),
        status=RunStatus.WAITING_FOR_CURSOR_FIX,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(tmp_path),
            git_common_dir=str(tmp_path),
            git_dir=str(tmp_path),
            branch="main",
            initial_head="abc",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(repository_path="p.md", snapshot_path="plan/plan.md", sha256="a" * 64),
        prompt=PromptState(
            source_repository_path="p.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )
    sent = read_cursor_prompt(state, run_dir, 2)
    assert exact in sent
    assert sent.encode("utf-8").endswith(exact.encode("utf-8"))
    assert (run_dir / "prompts/fixes/01.txt").read_bytes() == exact.encode("utf-8")


def test_read_cursor_prompt_verifies_continuation_envelope_hash(tmp_path: Path) -> None:
    exact = "Continue from the exact prior prompt.\n"
    run_dir = tmp_path / "run"
    envelope_rel = "prompts/cursor-recovery/02.usage-limit-continuation.txt"
    envelope_path = run_dir / envelope_rel
    envelope_path.parent.mkdir(parents=True)
    envelope_path.write_text(f"envelope\n{exact}", encoding="utf-8")
    envelope_hash = sha256_file(envelope_path)

    now = utc_now()
    state = RunState(
        run_id="successor",
        project=ProjectRef(name="p"),
        status=RunStatus.INTERRUPTED,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(tmp_path),
            git_common_dir=str(tmp_path),
            git_dir=str(tmp_path),
            branch="main",
            initial_head="abc",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(repository_path="p.md", snapshot_path="plan/plan.md", sha256="a" * 64),
        prompt=PromptState(
            source_repository_path="p.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="auto",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
            chat_id="019abc00-1111-2222-3333-444444444444",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=2,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        recovery=RecoveryState(
            source_run_id="source-run",
            source_status=RunStatus.FAILED.value,
            source_iteration=2,
            recovered_checkpoint="cursor",
            created_at=now,
            runtime_migration="none",
            reason_code="cursor_usage_limit",
            cursor_model_fallback="auto",
            source_cursor_model="composer-2.5-fast",
            source_prompt_path="prompts/fixes/01.txt",
            source_prompt_sha256="c" * 64,
            usage_limit_fingerprint_path="git/cursor-output/02.usage-limit-failure.json",
            usage_limit_fingerprint_sha256="d" * 64,
            continuation_envelope_path=envelope_rel,
            continuation_envelope_sha256=envelope_hash,
        ),
    )
    assert exact in read_cursor_prompt(state, run_dir, 2)

    envelope_path.write_text("tampered envelope\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="continuation envelope hash mismatch"):
        read_cursor_prompt(state, run_dir, 2)


def test_fingerprint_deterministic_and_changes_with_content(repo: Path, tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    state = _state(repo)
    (repo / "feature.txt").write_text("one\n", encoding="utf-8")
    first = capture_cursor_output_fingerprint(state, run_directory, iteration_number=1)
    again = recompute_cursor_output_fingerprint(state, iteration_number=1)
    assert first.aggregate_sha256 == again.aggregate_sha256
    payload = json.loads((run_directory / first.relative_path).read_text(encoding="utf-8"))
    assert "content" not in json.dumps(payload)
    assert "one\n" not in json.dumps(payload)
    assert payload["untracked_files"]

    (repo / "feature.txt").write_text("two\n", encoding="utf-8")
    changed = recompute_cursor_output_fingerprint(state, iteration_number=1)
    assert changed.aggregate_sha256 != first.aggregate_sha256


def test_fingerprint_supports_binary_untracked(repo: Path, tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    state = _state(repo)
    binary = repo / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\xff")
    result = capture_cursor_output_fingerprint(state, run_directory, iteration_number=2)
    payload = json.loads((run_directory / result.relative_path).read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in payload["untracked_files"]]
    assert "blob.bin" in paths
    assert b"\x00\x01\x02\xff".decode("latin1") not in json.dumps(payload)


def test_fingerprint_rejects_symlink(repo: Path, tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    state = _state(repo)
    target = repo / "real.txt"
    target.write_text("real\n", encoding="utf-8")
    link = repo / "link.txt"
    link.symlink_to("real.txt")
    with pytest.raises(ValidationError, match="unsupported symlink"):
        capture_cursor_output_fingerprint(state, run_directory, iteration_number=1)
