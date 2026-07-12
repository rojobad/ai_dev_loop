"""Integration tests for Phase 13 Cursor usage-limit recovery."""

from __future__ import annotations

import ast
import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from tests.conftest import chmod_supported
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.recover import RecoveryAnalysis, recover_run
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, CursorUsageLimitError, ValidationError
from ai_dev_loop.recovery_planner import analyze_recovery
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.cursor_failure import SAFE_USAGE_LIMIT_SUMMARY
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


def _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "partial_both")
    with pytest.raises((CursorUsageLimitError, AiDevLoopError), match=SAFE_USAGE_LIMIT_SUMMARY):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error == SAFE_USAGE_LIMIT_SUMMARY
    assert "Billing" not in (state.last_error or "")
    assert "reset" not in (state.last_error or "").lower()

    metadata = json.loads(
        (run_path / "cursor/iterations/01/metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["failure_code"] == "cursor_usage_limit"
    assert (run_path / "git/cursor-output/01.usage-limit-failure.json").is_file()
    assert (run_path / "git/status/01-after-cursor.txt").is_file()

    repo = Path(str(prepared_run["repo"]))
    yaml_path = next(path for path in repo.rglob("ai_dev_loop.yaml") if path.is_file())
    assert "# partial tracked" in yaml_path.read_text(encoding="utf-8")
    assert (repo / "partial_untracked.txt").is_file()

    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": repo,
        "state": state,
        "chat_id": state.cursor.chat_id,
        "session_id": state.codex.session_id,
        "source_fingerprint": _tree_fingerprint(run_path),
        "repo_status": subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=repo,
            text=True,
        ),
        "agent_log": fake_clis["agent_log"],
        "codex_log": fake_clis["codex_log"],
        "initial_prompt": (run_path / "prompts/cursor-initial.txt").read_text(encoding="utf-8"),
    }


def test_usage_limit_start_persists_recoverable_failure(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    analysis = analyze_recovery(failed["state"], failed["run_path"])
    assert analysis.eligible is True
    assert analysis.checkpoint == "cursor"
    assert analysis.reason_code == "cursor_usage_limit"


def test_analyze_recovery_rejects_content_drift(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    repo = Path(str(failed["repo"]))
    yaml_path = next(path for path in repo.rglob("ai_dev_loop.yaml") if path.is_file())
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    analysis = analyze_recovery(failed["state"], failed["run_path"])
    assert analysis.eligible is False
    assert "usage_limit_fingerprint_drift" in analysis.blockers


def test_analyze_recovery_rejects_branch_change(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    repo = Path(str(failed["repo"]))
    subprocess.run(
        ["git", "checkout", "-b", "other-branch"], cwd=repo, check=True, capture_output=True
    )

    analysis = analyze_recovery(failed["state"], failed["run_path"])
    assert analysis.eligible is False
    assert "branch_mismatch" in analysis.blockers


def test_analyze_recovery_rejects_missing_fingerprint(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    fingerprint = failed["run_path"] / "git/cursor-output/01.usage-limit-failure.json"
    fingerprint.unlink()

    analysis = analyze_recovery(failed["state"], failed["run_path"])
    assert analysis.eligible is False
    assert "usage_limit_fingerprint_missing" in analysis.blockers


def test_analyze_recovery_rejects_ordinary_cursor_fail(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    with pytest.raises(AiDevLoopError, match="Cursor execution failed"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED

    analysis = analyze_recovery(state, run_path)
    assert analysis.eligible is False
    assert "cursor_turn_incomplete" in analysis.blockers


def test_recover_dry_run_cursor_checkpoint_is_read_only(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    before = failed["source_fingerprint"]
    analysis = recover_run(str(failed["run_id"]), dry_run=True, cursor_model="auto")
    assert isinstance(analysis, RecoveryAnalysis)
    assert analysis.eligible is True
    assert analysis.checkpoint == "cursor"
    assert _tree_fingerprint(failed["run_path"]) == before
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=failed["repo"],
            text=True,
        )
        == failed["repo_status"]
    )


def test_recover_without_cursor_model_raises_validation_error(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    with pytest.raises(ValidationError, match="requires --cursor-model"):
        recover_run(str(failed["run_id"]))


def test_recover_cursor_model_rejected_for_reviewing_failure(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(prepared_run["run_id"])

    with pytest.raises(ValidationError, match="only valid for cursor usage-limit"):
        recover_run(str(prepared_run["run_id"]), cursor_model="auto")


def test_recover_creates_successor_and_resume_continues_same_chat(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")

    result = recover_run(str(failed["run_id"]), cursor_model="auto")
    assert result.recovery_run_id != failed["run_id"]
    assert result.reused_existing_successor is False

    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.source_run_id == failed["run_id"]
    assert successor.recovery.recovered_checkpoint == "cursor"
    assert successor.recovery.reason_code == "cursor_usage_limit"
    assert successor.recovery.source_staged_patch_sha256 is None
    assert successor.recovery.cursor_model_fallback == "auto"
    assert successor.recovery.source_cursor_model == failed["state"].cursor.model
    assert successor.cursor.model == "auto"
    assert successor.cursor.chat_id == failed["chat_id"]
    assert successor.codex.session_id == failed["session_id"]

    envelope_path = successor_path / str(successor.recovery.continuation_envelope_path)
    assert envelope_path.is_file()
    envelope = envelope_path.read_text(encoding="utf-8")
    assert failed["initial_prompt"] in envelope
    assert envelope.endswith(str(failed["initial_prompt"]))

    if chmod_supported(successor_path):
        mode = stat.S_IMODE((successor_path / "state.json").stat().st_mode)
        assert mode == 0o600

    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_MODE", raising=False)
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"

    agent_after = Path(failed["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == create_before
    assert "--resume" in agent_after
    assert str(failed["chat_id"]) in agent_after
    assert "--model" in agent_after
    assert _args_contains_model_auto(agent_after)

    final = load_run_state(successor_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert (successor_path / "git/diffs/01.patch").is_file()
    assert (successor_path / "codex/reviews/01.json").is_file()

    repo = Path(str(failed["repo"]))
    staged = subprocess.check_output(["git", "diff", "--cached"], cwd=repo, text=True)
    assert "# partial tracked" in staged or "partial tracked" in staged
    assert "partial_untracked.txt" in staged or "partial untracked" in staged

    codex_log = Path(failed["codex_log"]).read_text(encoding="utf-8")
    assert str(failed["session_id"]) in codex_log
    assert "--last" not in codex_log


def _args_contains_model_auto(agent_log: str) -> bool:
    for line in agent_log.splitlines():
        if line.startswith("ARGS:"):
            args = ast.literal_eval(line.removeprefix("ARGS:"))
            if "--model" in args:
                index = args.index("--model")
                if index + 1 < len(args) and args[index + 1] == "auto":
                    return True
    return False


def test_recover_idempotent_reuses_successor(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]), cursor_model="auto")
    second = recover_run(str(failed["run_id"]), cursor_model="auto")
    assert second.recovery_run_id == first.recovery_run_id
    assert second.reused_existing_successor is True


def test_recover_refuses_different_cursor_model_with_active_successor(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]), cursor_model="auto")
    assert first.recovery_run_id

    with pytest.raises(ValidationError, match="different fallback model"):
        recover_run(str(failed["run_id"]), cursor_model="composer-2.5-fast")


def test_recover_rejects_tampered_continuation_envelope_on_reuse(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    first = recover_run(str(failed["run_id"]), cursor_model="auto")
    successor_path, successor = load_run(first.recovery_run_id)
    envelope_path = successor_path / str(successor.recovery.continuation_envelope_path)
    envelope_path.write_text("tampered envelope\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="continuation envelope no longer matches"):
        recover_run(str(failed["run_id"]), cursor_model="auto")


def test_usage_limit_structured_error_metadata_omits_billing(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "usage_limit_structured")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "partial_both")
    with pytest.raises((CursorUsageLimitError, AiDevLoopError), match=SAFE_USAGE_LIMIT_SUMMARY):
        start_run(prepared_run["run_id"])

    metadata = json.loads(
        (prepared_run["run_path"] / "cursor/iterations/01/metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["failure_code"] == "cursor_usage_limit"
    assert metadata["failure_summary"] == SAFE_USAGE_LIMIT_SUMMARY
    assert metadata["errors"] == []
    serialized = json.dumps(metadata)
    assert "Billing" not in serialized
    assert "2026-08-01" not in serialized


def _failed_correction_usage_limit_run(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success,usage_limit")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_SEQUENCE", "tracked,partial_both")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    with pytest.raises((CursorUsageLimitError, AiDevLoopError), match=SAFE_USAGE_LIMIT_SUMMARY):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert len(state.iterations) >= 2
    assert state.iterations[0]["review"]["has_actionable_findings"] is True
    assert (run_path / "prompts/fixes/01.txt").is_file()
    assert (run_path / "cursor/iterations/02/metadata.json").is_file()
    assert (run_path / "git/cursor-output/02.usage-limit-failure.json").is_file()

    repo = Path(str(prepared_run["repo"]))
    yaml_path = next(path for path in repo.rglob("ai_dev_loop.yaml") if path.is_file())
    assert "# partial tracked" in yaml_path.read_text(encoding="utf-8")
    assert (repo / "partial_untracked.txt").is_file()

    fix_prompt = (run_path / "prompts/fixes/01.txt").read_text(encoding="utf-8")
    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": repo,
        "state": state,
        "chat_id": state.cursor.chat_id,
        "session_id": state.codex.session_id,
        "fix_prompt": fix_prompt,
        "agent_log": fake_clis["agent_log"],
    }


def test_correction_usage_limit_recover_and_resume_with_partial_work(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_correction_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")

    result = recover_run(str(failed["run_id"]), cursor_model="auto")
    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "cursor"
    assert successor.recovery.source_iteration == 2
    assert successor.cursor.chat_id == failed["chat_id"]

    envelope_path = successor_path / str(successor.recovery.continuation_envelope_path)
    envelope = envelope_path.read_text(encoding="utf-8")
    assert failed["fix_prompt"] in envelope

    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"

    agent_after = Path(failed["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == create_before
    assert "--resume" in agent_after
    assert str(failed["chat_id"]) in agent_after

    final = load_run_state(successor_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert (successor_path / "git/diffs/02.patch").is_file()
    assert (successor_path / "codex/reviews/02.json").is_file()


def _failed_correction_usage_limit_run_with_partial_index(
    prepared_run, fake_clis, monkeypatch
) -> dict[str, object]:
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success,usage_limit")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_SEQUENCE", "tracked,correction_stage")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    with pytest.raises((CursorUsageLimitError, AiDevLoopError), match=SAFE_USAGE_LIMIT_SUMMARY):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    repo = Path(str(prepared_run["repo"]))
    previous_patch = (run_path / "git/diffs/01.patch").read_text(encoding="utf-8")
    current_staged = subprocess.check_output(
        ["git", "diff", "--cached"],
        cwd=repo,
        text=True,
    )
    assert current_staged != previous_patch
    assert "correction_feature.txt" in current_staged
    assert (repo / "correction_feature.txt").is_file()
    assert (run_path / "git/cursor-output/02.usage-limit-failure.json").is_file()

    fix_prompt = (run_path / "prompts/fixes/01.txt").read_text(encoding="utf-8")
    return {
        "run_id": prepared_run["run_id"],
        "run_path": run_path,
        "repo": repo,
        "state": state,
        "chat_id": state.cursor.chat_id,
        "session_id": state.codex.session_id,
        "fix_prompt": fix_prompt,
        "agent_log": fake_clis["agent_log"],
        "previous_patch": previous_patch,
        "current_staged": current_staged,
    }


def test_correction_usage_limit_recover_with_partial_index_staging(
    prepared_run, fake_clis, monkeypatch
) -> None:
    failed = _failed_correction_usage_limit_run_with_partial_index(
        prepared_run, fake_clis, monkeypatch
    )
    assert failed["current_staged"] != failed["previous_patch"]

    agent_before = Path(failed["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")

    result = recover_run(str(failed["run_id"]), cursor_model="auto")
    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "cursor"
    assert successor.recovery.source_iteration == 2

    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"

    agent_after = Path(failed["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == create_before
    assert "--resume" in agent_after

    final = load_run_state(successor_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert (successor_path / "git/diffs/02.patch").is_file()


def test_non_tty_start_prints_recover_command_without_successor(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "partial_both")

    before_successors = [
        state.run_id
        for _path, state in list_run_directories(project="fixture-project")
        if state.recovery is not None and state.recovery.source_run_id == prepared_run["run_id"]
    ]

    result = runner.invoke(app, ["start", str(prepared_run["run_id"])])
    assert result.exit_code != 0
    assert SAFE_USAGE_LIMIT_SUMMARY in result.output
    assert f"ai_dev_loop recover {prepared_run['run_id']} --cursor-model auto" in result.output
    assert "ai_dev_loop resume <recovery-run-id>" in result.output

    after_successors = [
        state.run_id
        for _path, state in list_run_directories(project="fixture-project")
        if state.recovery is not None and state.recovery.source_run_id == prepared_run["run_id"]
    ]
    assert after_successors == before_successors
