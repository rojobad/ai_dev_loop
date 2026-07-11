"""Phase 12 integration: Cursor index mutations and staging recovery."""

from __future__ import annotations

import json
import re
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
from ai_dev_loop.state import RunStatus, load_run_state

runner = CliRunner()


def _agent_prompt_bodies(agent_log: Path) -> list[str]:
    text = agent_log.read_text(encoding="utf-8")
    bodies: list[str] = []
    for match in re.finditer(r"ARGS:(\[.*\])", text):
        args = eval(match.group(1), {"__builtins__": {}})  # noqa: S307 - test log from fixture
        if isinstance(args, list) and args:
            bodies.append(str(args[-1]))
    return bodies


def test_correction_cursor_git_add_continues_to_review(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    from ai_dev_loop import workflow_engine

    original_cursor = workflow_engine._run_cursor_turn

    def wrap_cursor(*args, **kwargs):
        if kwargs.get("iteration_number") == 2:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction_stage")
        else:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        return original_cursor(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_run_cursor_turn", wrap_cursor)

    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"
    run_path = prepared_run["run_path"]
    assert (run_path / "git/cursor-output/01.json").is_file()
    assert (run_path / "git/cursor-output/02.json").is_file()
    assert (run_path / "git/diffs/02.patch").is_file()
    prompts = _agent_prompt_bodies(fake_clis["agent_log"])
    assert any("This is an ai_dev_loop correction turn." in body for body in prompts)
    fix = (run_path / "prompts/fixes/01.txt").read_text(encoding="utf-8")
    assert any(fix.rstrip("\n") in body for body in prompts)


def test_correction_partial_stage_normalized_by_git_add_a(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    from ai_dev_loop import workflow_engine

    original_cursor = workflow_engine._run_cursor_turn

    def wrap_cursor(*args, **kwargs):
        if kwargs.get("iteration_number") == 2:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction_partial_stage")
        else:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        return original_cursor(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_run_cursor_turn", wrap_cursor)
    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"
    patch = (prepared_run["run_path"] / "git/diffs/02.patch").read_text(encoding="utf-8")
    assert "correction_staged.txt" in patch
    assert "ai_dev_loop.yaml" in patch


def test_forbidden_head_change_fails_before_review(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    from ai_dev_loop import workflow_engine

    original_cursor = workflow_engine._run_cursor_turn

    def wrap_cursor(*args, **kwargs):
        if kwargs.get("iteration_number") == 2:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction_commit_forbidden")
        else:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        return original_cursor(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_run_cursor_turn", wrap_cursor)
    with pytest.raises(AiDevLoopError, match="HEAD changed"):
        start_run(prepared_run["run_id"])
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert not (prepared_run["run_path"] / "codex/reviews/02.json").exists()


def _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.runners import staging as staging_mod

    original_staging = staging_mod.run_git_staging
    original_cursor = workflow_engine._run_cursor_turn

    def wrap_cursor(*args, **kwargs):
        if kwargs.get("iteration_number") == 2:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction_stage")
        else:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        return original_cursor(*args, **kwargs)

    def wrap_staging(*args, **kwargs):
        iteration_number = kwargs.get("iteration_number")
        if iteration_number == 2:
            raise ValidationError("simulated correction staging failure")
        return original_staging(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_run_cursor_turn", wrap_cursor)
    monkeypatch.setattr(staging_mod, "run_git_staging", wrap_staging)
    monkeypatch.setattr(workflow_engine, "run_git_staging", wrap_staging)
    with pytest.raises(AiDevLoopError, match="simulated correction staging failure"):
        start_run(prepared_run["run_id"])
    # Restore staging so successor resume can normalize the index.
    monkeypatch.setattr(staging_mod, "run_git_staging", original_staging)
    monkeypatch.setattr(workflow_engine, "run_git_staging", original_staging)
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert (run_path / "cursor/iterations/02/metadata.json").is_file()
    assert (run_path / "git/cursor-output/02.json").is_file()
    assert not (run_path / "git/diffs/02.patch").is_file()
    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": prepared_run["repo"],
        "state": state,
        "agent_log": fake_clis["agent_log"],
        "codex_log": fake_clis["codex_log"],
    }


def test_staging_recovery_after_git_add_before_artifacts(
    prepared_run, fake_clis, monkeypatch
) -> None:
    """Recover when git add -A succeeded but staged artifact persistence failed."""

    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.runners import staging as staging_mod

    original_cursor = workflow_engine._run_cursor_turn
    original_capture = staging_mod.capture_staging_normalization_fingerprint

    def wrap_cursor(*args, **kwargs):
        if kwargs.get("iteration_number") == 2:
            # Leave staged + unstaged work so git add -A changes the fingerprint.
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction_partial_stage")
        else:
            monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        return original_cursor(*args, **kwargs)

    fail_once = {"done": False}

    def wrap_capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        if kwargs.get("iteration_number") == 2 and not fail_once["done"]:
            fail_once["done"] = True
            raise ValidationError("simulated failure after normalization fingerprint")
        return result

    monkeypatch.setattr(workflow_engine, "_run_cursor_turn", wrap_cursor)
    monkeypatch.setattr(staging_mod, "capture_staging_normalization_fingerprint", wrap_capture)
    with pytest.raises(AiDevLoopError, match="simulated failure after normalization"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert (run_path / "git/cursor-output/02.json").is_file()
    assert (run_path / "git/cursor-output/02.post-normalization.json").is_file()
    assert not (run_path / "git/diffs/02.patch").is_file()
    post_cursor = json.loads((run_path / "git/cursor-output/02.json").read_text(encoding="utf-8"))
    post_norm = json.loads(
        (run_path / "git/cursor-output/02.post-normalization.json").read_text(encoding="utf-8")
    )
    assert post_cursor["aggregate_sha256"] != post_norm["aggregate_sha256"]

    dry = runner.invoke(app, ["recover", prepared_run["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout)
    assert payload["eligible"] is True
    assert payload["checkpoint"] == "staging"
    assert payload["verified_staging_state"] == "post_normalization"

    agent_before = fake_clis["agent_log"].read_text(encoding="utf-8")
    result = recover_run(prepared_run["run_id"])
    successor_path = run_dir("fixture-project", result.recovery_run_id)
    assert (successor_path / "git/cursor-output/02.post-normalization.json").is_file()

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert fake_clis["agent_log"].read_text(encoding="utf-8") == agent_before
    assert (successor_path / "git/diffs/02.patch").is_file()


def test_staging_recovery_resume_skips_cursor(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch)
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout)
    assert payload["eligible"] is True
    assert payload["checkpoint"] == "staging"
    assert payload["iteration"] == 2

    agent_before = failed["agent_log"].read_text(encoding="utf-8")
    result = recover_run(failed["run_id"])
    assert result.checkpoint == "staging"
    assert result.legacy_cursor_output_adopted is False
    successor_path = run_dir("fixture-project", result.recovery_run_id)
    assert (successor_path / "git/cursor-output/02.json").is_file()
    assert not (successor_path / "git/diffs/02.patch").is_file()

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert fake_clis["agent_log"].read_text(encoding="utf-8") == agent_before
    assert (successor_path / "git/diffs/02.patch").is_file()
    assert (successor_path / "codex/reviews/02.json").is_file()


def test_fingerprint_drift_blocks_staging_recovery(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch)
    (failed["repo"] / "drift.txt").write_text("drift\n", encoding="utf-8")
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 4
    payload = json.loads(dry.stdout)
    assert payload["eligible"] is False
    assert "cursor_output_fingerprint_drift" in payload["blockers"]


def test_legacy_adoption_requires_flag_and_status_match(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch)
    fingerprint = failed["run_path"] / "git/cursor-output/02.json"
    fingerprint.unlink()
    # Without fingerprint and without adoption flag.
    dry = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert dry.exit_code == 4
    payload = json.loads(dry.stdout)
    assert "post_cursor_fingerprint_missing" in payload["blockers"]

    # Adoption with matching after-cursor status.
    agent_before = failed["agent_log"].read_text(encoding="utf-8")
    repo_before = subprocess.check_output(
        ["git", "status", "--porcelain=v2"],
        cwd=failed["repo"],
        text=True,
    )
    result = recover_run(failed["run_id"], adopt_current_cursor_output=True)
    assert result.legacy_cursor_output_adopted is True
    repo_after = subprocess.check_output(
        ["git", "status", "--porcelain=v2"],
        cwd=failed["repo"],
        text=True,
    )
    assert repo_before == repo_after
    successor_path = run_dir("fixture-project", result.recovery_run_id)
    assert (successor_path / "git/cursor-output/02.json").is_file()

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert fake_clis["agent_log"].read_text(encoding="utf-8") == agent_before


def test_legacy_adoption_rejects_status_mismatch(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch)
    (failed["run_path"] / "git/cursor-output/02.json").unlink()
    after = failed["run_path"] / "git/status/02-after-cursor.txt"
    after.write_text("1 .M N... 100644 100644 100644 h h ai_dev_loop.yaml\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="after_cursor_status_mismatch|not recoverable"):
        recover_run(failed["run_id"], adopt_current_cursor_output=True)


def test_staging_recovery_idempotent(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _fail_after_correction_cursor(prepared_run, fake_clis, monkeypatch)
    first = recover_run(failed["run_id"])
    second = recover_run(failed["run_id"])
    assert first.recovery_run_id == second.recovery_run_id
    assert second.reused_existing_successor is True
