"""Phase 14.5 integration: initial index staging and recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.recover import recover_run
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.paths import run_dir
from ai_dev_loop.recovery_planner import analyze_recovery
from ai_dev_loop.state import RunStatus, load_run_state

runner = CliRunner()


def _tree_fingerprint(path: Path) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(path))
        if rel.startswith("locks/"):
            continue
        fingerprint[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return fingerprint


def _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "stage_self")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.runners import staging as staging_mod

    original_staging = staging_mod.run_git_staging

    def wrap_staging(*args, **kwargs):
        if kwargs.get("iteration_number") == 1:
            raise ValidationError("simulated initial staging failure")
        return original_staging(*args, **kwargs)

    monkeypatch.setattr(staging_mod, "run_git_staging", wrap_staging)
    monkeypatch.setattr(workflow_engine, "run_git_staging", wrap_staging)
    with pytest.raises(AiDevLoopError, match="simulated initial staging failure"):
        start_run(prepared_run["run_id"])
    monkeypatch.setattr(staging_mod, "run_git_staging", original_staging)
    monkeypatch.setattr(workflow_engine, "run_git_staging", original_staging)

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert (run_path / "cursor/iterations/01/metadata.json").is_file()
    assert (run_path / "git/cursor-output/01.json").is_file()
    assert not (run_path / "git/diffs/01.patch").is_file()
    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": prepared_run["repo"],
        "state": state,
        "agent_log": fake_clis["agent_log"],
        "codex_log": fake_clis["codex_log"],
        "source_fingerprint": _tree_fingerprint(run_path),
        "repo_status": subprocess.check_output(
            ["git", "status", "--porcelain=v2"],
            cwd=prepared_run["repo"],
            text=True,
        ),
    }


def test_initial_staging_recovery_dry_run_eligible(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout)
    assert payload["eligible"] is True
    assert payload["checkpoint"] == "staging"
    assert payload["iteration"] == 1
    assert payload["reason_code"] == "initial_staging_failed"
    assert payload["staged_patch_sha256"] is None
    assert payload["previous_staged_patch_sha256"] is None
    assert payload["cursor_output_fingerprint_sha256"]


def test_initial_staging_recovery_resume_skips_cursor(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    before_source = failed["source_fingerprint"]
    before_status = failed["repo_status"]
    agent_before = failed["agent_log"].read_text(encoding="utf-8")

    result = recover_run(failed["run_id"])
    assert result.checkpoint == "staging"
    assert result.reason_code == "initial_staging_failed"
    assert result.staged_patch_sha256 is None
    assert _tree_fingerprint(failed["run_path"]) == before_source
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain=v2"],
            cwd=failed["repo"],
            text=True,
        )
        == before_status
    )

    successor_path = run_dir("fixture-project", result.recovery_run_id)
    successor = load_run_state(successor_path / "state.json")
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.reason_code == "initial_staging_failed"
    assert successor.recovery.source_iteration == 1
    assert successor.recovery.source_staged_patch_sha256 is None
    assert successor.recovery.previous_staged_patch_sha256 is None
    assert (successor_path / "git/cursor-output/01.json").is_file()
    assert not (successor_path / "git/diffs/01.patch").is_file()

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert fake_clis["agent_log"].read_text(encoding="utf-8") == agent_before
    assert (successor_path / "git/diffs/01.patch").is_file()
    assert (successor_path / "codex/reviews/01.json").is_file()
    patch = (successor_path / "git/diffs/01.patch").read_text(encoding="utf-8")
    assert "staged_by_agent.txt" in patch


def test_initial_staging_recovery_rejects_fingerprint_drift(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    (failed["repo"] / "drift.txt").write_text("drift\n", encoding="utf-8")
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 4
    payload = json.loads(dry.stdout)
    assert payload["eligible"] is False
    assert "cursor_output_fingerprint_drift" in payload["blockers"]


def test_initial_staging_recovery_rejects_missing_fingerprint(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    fingerprint = failed["run_path"] / "git/cursor-output/01.json"
    fingerprint.unlink()
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 4
    payload = json.loads(dry.stdout)
    assert "post_cursor_fingerprint_missing" in payload["blockers"]

    adopted = runner.invoke(
        app,
        [
            "recover",
            failed["run_id"],
            "--dry-run",
            "--adopt-current-cursor-output",
            "--output",
            "json",
        ],
    )
    assert adopted.exit_code == 4
    adopted_payload = json.loads(adopted.stdout)
    assert "initial_staging_does_not_support_adoption" in adopted_payload["blockers"]


def test_initial_staging_recovery_rejects_identity_drift(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    subprocess.run(
        ["git", "checkout", "-b", "drift-branch"],
        cwd=failed["repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    analysis = analyze_recovery(failed["state"], failed["run_path"])
    assert analysis.eligible is False
    assert "branch_mismatch" in analysis.blockers


def test_initial_staging_recovery_idempotent_reuse(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_initial_cursor(prepared_run, fake_clis, monkeypatch)
    first = recover_run(failed["run_id"])
    second = recover_run(failed["run_id"])
    assert second.reused_existing_successor is True
    assert second.recovery_run_id == first.recovery_run_id
