"""Integration tests for Phase 11 failed-run recovery successors."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from tests.conftest import chmod_supported, write_session_rollout
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.recover import RecoveryAnalysis, recover_run, render_recovery_result
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.paths import run_dir
from ai_dev_loop.recovery_planner import analyze_recovery
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, load_run_state, save_run_state

runner = CliRunner()


def _tree_fingerprint(path: Path) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(path))
        # Lock control files are rewritten when recover acquires the source run lock.
        if rel.startswith("locks/"):
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        fingerprint[rel] = digest
    return fingerprint


def _failed_review_run(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(prepared_run["run_id"])
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.cursor.chat_id
    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": prepared_run["repo"],
        "state": state,
        "chat_id": state.cursor.chat_id,
        "session_id": state.codex.session_id,
        "source_fingerprint": _tree_fingerprint(run_path),
        "repo_status": subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=prepared_run["repo"],
            text=True,
        ),
        "staged_patch": subprocess.check_output(
            ["git", "diff", "--cached"],
            cwd=prepared_run["repo"],
            text=True,
        ),
        "agent_log": fake_clis["agent_log"],
        "codex_log": fake_clis["codex_log"],
    }


def test_recover_dry_run_is_read_only(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    before = failed["source_fingerprint"]
    result = runner.invoke(app, ["recover", failed["run_id"], "--dry-run", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["eligible"] is True
    assert payload["checkpoint"] == "reviewing"
    assert _tree_fingerprint(failed["run_path"]) == before
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=failed["repo"],
            text=True,
        )
        == failed["repo_status"]
    )


def test_recover_creates_successor_and_resume_skips_cursor(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    analysis = recover_run(str(failed["run_id"]), dry_run=True)
    assert isinstance(analysis, RecoveryAnalysis)
    assert analysis.eligible is True

    result = recover_run(str(failed["run_id"]))
    assert result.recovery_run_id != failed["run_id"]
    assert result.reused_existing_successor is False
    assert result.resume_command == f"ai_dev_loop resume {result.recovery_run_id}"

    assert _tree_fingerprint(Path(failed["run_path"])) == failed["source_fingerprint"]
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=failed["repo"],
            text=True,
        )
        == failed["repo_status"]
    )
    assert (
        subprocess.check_output(
            ["git", "diff", "--cached"],
            cwd=failed["repo"],
            text=True,
        )
        == failed["staged_patch"]
    )

    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.source_run_id == failed["run_id"]
    assert successor.recovery.recovered_checkpoint == "reviewing"
    assert successor.cursor.chat_id == failed["chat_id"]
    assert successor.codex.session_id == failed["session_id"]
    assert not (successor_path / "codex/events/01.jsonl").exists()
    assert (successor_path / "cursor/iterations/01/metadata.json").is_file()
    assert (successor_path / "git/diffs/01.patch").is_file()
    if chmod_supported(successor_path):
        mode = stat.S_IMODE((successor_path / "state.json").stat().st_mode)
        assert mode == 0o600

    with pytest.raises(AiDevLoopError, match="terminal status failed"):
        resume_run(str(failed["run_id"]))

    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    agent_after = Path(failed["agent_log"]).read_text(encoding="utf-8")
    assert agent_after == agent_before
    codex_log = Path(failed["codex_log"]).read_text(encoding="utf-8")
    assert str(failed["session_id"]) in codex_log
    assert "--last" not in codex_log

    final = load_run_state(successor_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert final.cursor.chat_id == failed["chat_id"]


def test_recover_findings_resume_same_chat(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    result = recover_run(str(failed["run_id"]))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert resumed.chat_id == failed["chat_id"]
    agent_after = Path(failed["agent_log"]).read_text(encoding="utf-8")
    assert agent_after != agent_before
    assert agent_after.count("CREATE_CHAT") == create_before
    assert "--resume" in agent_after or str(failed["chat_id"]) in agent_after


def test_recover_idempotent_reuses_active_successor(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    second = recover_run(str(failed["run_id"]))
    assert second.recovery_run_id == first.recovery_run_id
    assert second.reused_existing_successor is True


def test_recover_reuse_survives_missing_or_changed_rollout(
    prepared_run, fake_clis, monkeypatch
) -> None:
    import os
    import shutil

    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(Path(str(failed["run_path"])) / "state.json")
    state.codex.session_model = None
    state.codex.session_reasoning_effort = None
    state.codex.review_model = None
    state.codex.review_reasoning_effort = None
    state.codex.review_model_source = None
    state.codex.review_reasoning_source = None
    save_run_state(Path(str(failed["run_path"])), state)

    first = recover_run(str(failed["run_id"]))
    assert first.runtime_migration == "phase9_session_capture"
    frozen_model = first.session_model
    frozen_effort = first.session_reasoning_effort

    sessions_dir = Path(os.environ["CODEX_HOME"]) / "sessions"
    write_session_rollout(
        sessions_dir,
        session_id=str(failed["session_id"]),
        model="gpt-5.5",
        reasoning_effort="low",
    )
    changed = recover_run(str(failed["run_id"]))
    assert changed.reused_existing_successor is True
    assert changed.recovery_run_id == first.recovery_run_id
    assert changed.session_model == frozen_model
    assert changed.session_reasoning_effort == frozen_effort

    shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True)
    missing = recover_run(str(failed["run_id"]))
    assert missing.reused_existing_successor is True
    assert missing.recovery_run_id == first.recovery_run_id
    assert missing.session_model == frozen_model

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert isinstance(dry, RecoveryAnalysis)
    assert dry.eligible is True
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is True
    assert dry.resolved_runtime is not None
    assert dry.resolved_runtime.session_model == frozen_model


def test_recover_reuse_rejects_new_untracked_files(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    (Path(str(failed["repo"])) / "late-untracked.txt").write_text("x\n", encoding="utf-8")

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is False
    assert "untracked_files" in dry.blockers
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False

    with pytest.raises(ValidationError, match="untracked_files"):
        recover_run(str(failed["run_id"]))


def test_recover_reuse_rejects_new_unstaged_changes(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    repo = Path(str(failed["repo"]))
    tracked = next(path for path in repo.rglob("*.yaml") if path.is_file())
    tracked.write_text(tracked.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is False
    assert "unstaged_tracked_changes" in dry.blockers
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False
    with pytest.raises(ValidationError, match="unstaged_tracked_changes"):
        recover_run(str(failed["run_id"]))


def test_recover_reuse_rejects_staged_patch_drift(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    repo = Path(str(failed["repo"]))
    (repo / "extra-drift.txt").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "add", "extra-drift.txt"], cwd=repo, check=True, capture_output=True)

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is False
    assert "staged_patch_drift" in dry.blockers
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False
    with pytest.raises(ValidationError, match="staged_patch_drift"):
        recover_run(str(failed["run_id"]))


def test_recover_reuse_rejects_missing_successor_artifacts(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    successor_path, _successor = load_run(first.recovery_run_id)
    (successor_path / "git/diffs/01.patch").unlink()

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is True
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False
    assert any("existing_successor_not_reusable" in warning for warning in dry.warnings)

    with pytest.raises(ValidationError, match="missing completed staging artifacts"):
        recover_run(str(failed["run_id"]))


def test_recover_reuse_rejects_changed_successor_session_id(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    successor_path, successor = load_run(first.recovery_run_id)
    successor.codex.session_id = "019ffff0-0000-0000-0000-000000000099"
    save_run_state(successor_path, successor)

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is True
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False
    assert any(
        "Codex session id does not match the failed source" in warning for warning in dry.warnings
    )
    with pytest.raises(ValidationError, match="Codex session id does not match the failed source"):
        recover_run(str(failed["run_id"]))


def test_recover_reuse_rejects_changed_successor_chat_identity(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    successor_path, successor = load_run(first.recovery_run_id)
    new_chat_id = "019ffff0-1111-2222-3333-444444444499"
    successor.cursor.chat_id = new_chat_id
    save_run_state(successor_path, successor)
    chat_path = successor_path / "cursor" / "chat.json"
    chat_payload = json.loads(chat_path.read_text(encoding="utf-8"))
    chat_payload["chat_id"] = new_chat_id
    chat_path.write_text(json.dumps(chat_payload, indent=2) + "\n", encoding="utf-8")

    dry = recover_run(str(failed["run_id"]), dry_run=True)
    assert dry.eligible is True
    assert dry.existing_successor_run_id == first.recovery_run_id
    assert dry.reused_existing_successor is False
    assert any(
        "Cursor chat id does not match the failed source" in warning for warning in dry.warnings
    )
    with pytest.raises(ValidationError, match="Cursor chat id does not match the failed source"):
        recover_run(str(failed["run_id"]))


def test_recover_rejects_toctou_dirty_worktree_under_lock(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    recover_run(str(failed["run_id"]))

    original_analyze = analyze_recovery
    calls = {"count": 0}

    def flaky_analyze(state, run_directory, *, resolve_runtime=True):
        result = original_analyze(state, run_directory, resolve_runtime=resolve_runtime)
        calls["count"] += 1
        # After the unlocked analysis succeeds, dirty the repo before locked revalidation.
        if calls["count"] == 1 and result.eligible:
            (Path(str(failed["repo"])) / "toctou-untracked.txt").write_text("x\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        "ai_dev_loop.commands.recover.analyze_recovery",
        flaky_analyze,
    )
    with pytest.raises(ValidationError, match="untracked_files"):
        recover_run(str(failed["run_id"]))


def test_recover_phase9_model_only_override(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(Path(str(failed["run_path"])) / "state.json")
    state.codex.session_model = None
    state.codex.session_reasoning_effort = None
    state.codex.review_model = "o4-mini"
    state.codex.review_reasoning_effort = None
    state.codex.review_model_source = None
    state.codex.review_reasoning_source = None
    save_run_state(Path(str(failed["run_path"])), state)

    result = recover_run(str(failed["run_id"]))
    assert result.runtime_migration == "phase9_session_capture"
    _, successor = load_run(result.recovery_run_id)
    assert successor.codex.review_model == "o4-mini"
    assert successor.codex.review_model_source == "explicit"
    assert successor.codex.review_reasoning_effort == "high"
    assert successor.codex.review_reasoning_source == "session"


def test_recover_phase9_reasoning_only_override(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(Path(str(failed["run_path"])) / "state.json")
    state.codex.session_model = None
    state.codex.session_reasoning_effort = None
    state.codex.review_model = None
    state.codex.review_reasoning_effort = "xhigh"
    state.codex.review_model_source = None
    state.codex.review_reasoning_source = None
    save_run_state(Path(str(failed["run_path"])), state)

    result = recover_run(str(failed["run_id"]))
    assert result.runtime_migration == "phase9_session_capture"
    _, successor = load_run(result.recovery_run_id)
    assert successor.codex.review_model == "gpt-5.6-sol"
    assert successor.codex.review_model_source == "session"
    assert successor.codex.review_reasoning_effort == "xhigh"
    assert successor.codex.review_reasoning_source == "explicit"


def test_recover_failed_successor_requires_chain(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        resume_run(first.recovery_run_id)
    _, successor = load_run(first.recovery_run_id)
    assert successor.status == RunStatus.FAILED

    with pytest.raises(ValidationError, match="itself failed"):
        recover_run(str(failed["run_id"]))

    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    grandchild = recover_run(first.recovery_run_id)
    assert grandchild.source_run_id == first.recovery_run_id
    assert grandchild.recovery_run_id != first.recovery_run_id


def test_recover_rejects_staged_patch_drift(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    repo = Path(str(failed["repo"]))
    (repo / "extra-drift.txt").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "add", "extra-drift.txt"], cwd=repo, check=True, capture_output=True)
    with pytest.raises(ValidationError, match="not recoverable"):
        recover_run(str(failed["run_id"]))
    analysis = recover_run(str(failed["run_id"]), dry_run=True)
    assert "staged_patch_drift" in analysis.blockers


def test_recover_rejects_untracked_files(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    (Path(str(failed["repo"])) / "untracked-file.txt").write_text("x\n", encoding="utf-8")
    analysis = recover_run(str(failed["run_id"]), dry_run=True)
    assert analysis.eligible is False
    assert "untracked_files" in analysis.blockers


def test_recover_rejects_completed_status(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    analysis = recover_run(prepared_run["run_id"], dry_run=True)
    assert analysis.eligible is False
    assert "source_status_not_failed" in analysis.blockers


def test_recover_phase9_runtime_migration(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(Path(str(failed["run_path"])) / "state.json")
    state.codex.session_model = None
    state.codex.session_reasoning_effort = None
    state.codex.review_model = None
    state.codex.review_reasoning_effort = None
    state.codex.review_model_source = None
    state.codex.review_reasoning_source = None
    save_run_state(Path(str(failed["run_path"])), state)

    result = recover_run(str(failed["run_id"]))
    assert result.runtime_migration == "phase9_session_capture"
    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.codex.review_model == "gpt-5.6-sol"
    assert successor.codex.review_reasoning_effort == "high"
    assert successor.codex.review_model_source == "session"
    runtime = json.loads(
        (successor_path / "codex/session-runtime.json").read_text(encoding="utf-8")
    )
    assert "session_id" not in runtime
    assert runtime["session_id_prefix"] == str(failed["session_id"])[:8]
    assert str(failed["run_path"]) not in (successor_path / "state.json").read_text(
        encoding="utf-8"
    )

    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    Path(failed["codex_log"]).write_text("", encoding="utf-8")
    resume_run(result.recovery_run_id)
    codex_log = Path(failed["codex_log"]).read_text(encoding="utf-8")
    assert "--model" in codex_log
    assert "gpt-5.6-sol" in codex_log
    assert 'model_reasoning_effort="high"' in codex_log


def test_recover_phase10_preserves_frozen_runtime(prepared_run, fake_clis, monkeypatch) -> None:
    import os

    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    sessions_dir = Path(os.environ["CODEX_HOME"]) / "sessions"
    write_session_rollout(
        sessions_dir,
        session_id=str(failed["session_id"]),
        model="gpt-5.5",
        reasoning_effort="low",
    )
    result = recover_run(str(failed["run_id"]))
    assert result.runtime_migration == "none"
    _, successor = load_run(result.recovery_run_id)
    source_state = failed["state"]
    assert successor.codex.review_model == source_state.codex.review_model
    assert successor.codex.review_reasoning_effort == source_state.codex.review_reasoning_effort
    assert successor.codex.session_model == source_state.codex.session_model


def test_recover_process_review_checkpoint(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    review = {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "tests_status": "passed",
        "summary": "ok",
        "review_markdown": "SENSITIVE_REVIEW_MARKDOWN",
        "cursor_fix_prompt": None,
    }
    (Path(str(failed["run_path"])) / "codex/reviews/01.json").write_text(
        json.dumps(review), encoding="utf-8"
    )
    analysis = recover_run(str(failed["run_id"]), dry_run=True)
    assert analysis.checkpoint == "process_review"
    assert analysis.reason_code == "codex_review_processing_failed"
    result = recover_run(str(failed["run_id"]))
    assert result.checkpoint == "process_review"
    assert (run_dir("fixture-project", result.recovery_run_id) / "codex/reviews/01.json").is_file()

    Path(failed["codex_log"]).write_text("", encoding="utf-8")
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"
    assert Path(failed["codex_log"]).read_text(encoding="utf-8") == ""


def test_recover_output_privacy(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    result = recover_run(str(failed["run_id"]))
    text = render_recovery_result(result, output="text")
    assert failed["session_id"] not in text
    assert failed["chat_id"] not in text
    assert "SENSITIVE" not in text
    cli = runner.invoke(app, ["recover", str(failed["run_id"]), "--output", "text"])
    assert cli.exit_code == 0
    assert failed["session_id"] not in cli.stdout
    status = runner.invoke(app, ["status", result.recovery_run_id])
    assert "Recovery successor of" in status.stdout
    listed = runner.invoke(app, ["list", "--project", "fixture-project"])
    assert "successor" in listed.stdout


def test_recover_never_invokes_agents(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_review_run(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    codex_before = Path(failed["codex_log"]).read_text(encoding="utf-8")
    recover_run(str(failed["run_id"]))
    assert Path(failed["agent_log"]).read_text(encoding="utf-8") == agent_before
    assert Path(failed["codex_log"]).read_text(encoding="utf-8") == codex_before
