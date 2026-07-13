"""Integration tests for Phase 13.5 legacy Cursor usage-limit adoption."""

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

from ai_dev_loop.commands.recover import RecoveryAnalysis, recover_run
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, CursorUsageLimitError, ValidationError
from ai_dev_loop.recovery_planner import analyze_recovery
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.cursor_failure import SAFE_USAGE_LIMIT_SUMMARY
from ai_dev_loop.state import RunStatus, load_run_state, sha256_file

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

    repo = Path(str(prepared_run["repo"]))
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


def _strip_phase13_usage_limit_evidence(run_path: Path) -> None:
    metadata_path = run_path / "cursor/iterations/01/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("failure_code", None)
    metadata.pop("failure_summary", None)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    fingerprint = run_path / "git/cursor-output/01.usage-limit-failure.json"
    if fingerprint.is_file():
        fingerprint.unlink()


def _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch) -> dict[str, object]:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    _strip_phase13_usage_limit_evidence(failed["run_path"])
    failed["state"] = load_run_state(failed["run_path"] / "state.json")
    failed["source_fingerprint"] = _tree_fingerprint(failed["run_path"])
    return failed


def test_phase13_source_rejects_adoption_flag(prepared_run, fake_clis, monkeypatch) -> None:
    failed = _failed_usage_limit_run(prepared_run, fake_clis, monkeypatch)
    analysis = analyze_recovery(
        failed["state"],
        failed["run_path"],
        adopt_current_cursor_output=True,
    )
    assert analysis.checkpoint == "cursor"
    assert analysis.eligible is False
    assert "redundant_legacy_adoption_for_phase13_usage_limit" in analysis.blockers

    with pytest.raises(ValidationError, match="not recoverable"):
        recover_run(
            str(failed["run_id"]),
            adopt_current_cursor_output=True,
            cursor_model="auto",
        )


def test_legacy_source_prospective_without_adoption(prepared_run, fake_clis, monkeypatch) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    analysis = analyze_recovery(legacy["state"], legacy["run_path"])
    assert analysis.checkpoint == "cursor"
    assert analysis.reason_code == "cursor_usage_limit"
    assert analysis.eligible is False
    assert "legacy_cursor_usage_limit_requires_explicit_adoption" in analysis.blockers
    assert "cursor_turn_incomplete" not in analysis.blockers

    dry = recover_run(str(legacy["run_id"]), dry_run=True, cursor_model="auto")
    assert isinstance(dry, RecoveryAnalysis)
    assert dry.checkpoint == "cursor"
    assert dry.eligible is False

    # Legacy prospective dry-run may omit --cursor-model to inspect the adoption blocker.
    dry_without_model = recover_run(str(legacy["run_id"]), dry_run=True)
    assert isinstance(dry_without_model, RecoveryAnalysis)
    assert dry_without_model.checkpoint == "cursor"
    assert dry_without_model.eligible is False
    assert (
        "legacy_cursor_usage_limit_requires_explicit_adoption" in dry_without_model.blockers
    )
    assert dry_without_model.requested_cursor_model is None


def test_legacy_source_requires_both_flags_for_recovery(
    prepared_run, fake_clis, monkeypatch
) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)

    with pytest.raises(ValidationError, match="requires --cursor-model"):
        recover_run(
            str(legacy["run_id"]),
            adopt_current_cursor_output=True,
        )

    result = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    assert result.recovery_run_id != legacy["run_id"]

    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.recovery is not None
    assert successor.recovery.legacy_cursor_usage_limit_adopted is True
    assert successor.recovery.usage_limit_fingerprint_path.endswith(".usage-limit-adopted.json")
    assert successor.cursor.chat_id == legacy["chat_id"]
    assert successor.cursor.model == "auto"
    assert successor.codex.session_id == legacy["session_id"]

    adopted_path = successor_path / str(successor.recovery.usage_limit_fingerprint_path)
    assert adopted_path.is_file()
    adopted = json.loads(adopted_path.read_text(encoding="utf-8"))
    assert adopted["legacy_cursor_usage_limit_adopted"] is True
    assert "content_fingerprint" in adopted
    serialized = json.dumps(adopted)
    assert "ActionRequiredError" not in serialized
    assert legacy["initial_prompt"] not in serialized

    envelope_path = successor_path / str(successor.recovery.continuation_envelope_path)
    envelope = envelope_path.read_text(encoding="utf-8")
    assert legacy["initial_prompt"] in envelope

    if chmod_supported(successor_path):
        mode = stat.S_IMODE(adopted_path.stat().st_mode)
        assert mode == 0o600


def test_legacy_stderr_classifications(prepared_run, fake_clis, monkeypatch) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    run_path = legacy["run_path"]

    analysis = analyze_recovery(
        legacy["state"],
        run_path,
        adopt_current_cursor_output=True,
    )
    assert analysis.eligible is True

    stderr_path = run_path / "cursor/iterations/01/stderr.txt"
    stderr_path.write_text("generic failure\n", encoding="utf-8")
    analysis = analyze_recovery(
        load_run_state(run_path / "state.json"),
        run_path,
        adopt_current_cursor_output=True,
    )
    assert "cursor_turn_incomplete" in analysis.blockers

    stderr_path.unlink()
    analysis = analyze_recovery(
        load_run_state(run_path / "state.json"),
        run_path,
        adopt_current_cursor_output=True,
    )
    assert "legacy_usage_limit_stderr_missing" in analysis.blockers


def test_legacy_rejects_status_mismatch(prepared_run, fake_clis, monkeypatch) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    repo = Path(str(legacy["repo"]))
    (repo / "new_untracked_for_status.txt").write_text("changes status\n", encoding="utf-8")

    analysis = analyze_recovery(
        legacy["state"],
        legacy["run_path"],
        adopt_current_cursor_output=True,
    )
    assert analysis.eligible is False
    assert "after_cursor_status_mismatch" in analysis.blockers


def test_legacy_recover_dry_run_is_read_only(prepared_run, fake_clis, monkeypatch) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    before = legacy["source_fingerprint"]
    analysis = recover_run(
        str(legacy["run_id"]),
        dry_run=True,
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    assert isinstance(analysis, RecoveryAnalysis)
    assert _tree_fingerprint(legacy["run_path"]) == before


def test_legacy_resume_rejects_content_mutation_without_status_change(
    prepared_run, fake_clis, monkeypatch
) -> None:
    """Adopted initial-turn successors must re-verify fingerprint before Cursor."""

    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(legacy["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")
    resume_before = agent_before.count("--resume")

    result = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )

    repo = Path(str(legacy["repo"]))
    status_before = subprocess.check_output(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        text=True,
    )
    yaml_path = next(path for path in repo.rglob("ai_dev_loop.yaml") if path.is_file())
    # Mutate content of an already-dirty path so porcelain status stays identical.
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8") + "# silent content drift\n",
        encoding="utf-8",
    )
    status_after = subprocess.check_output(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        text=True,
    )
    assert status_after == status_before

    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_MODE", raising=False)
    with pytest.raises(AiDevLoopError, match="content drifted"):
        resume_run(result.recovery_run_id)

    successor_path, successor = load_run(result.recovery_run_id)
    assert successor.status == RunStatus.FAILED
    assert "content drifted" in (successor.last_error or "")
    agent_after = Path(legacy["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == create_before
    assert agent_after.count("--resume") == resume_before
    assert not (successor_path / "git/diffs/01.patch").is_file()


def test_legacy_resume_continues_same_chat_and_reviews(
    prepared_run, fake_clis, monkeypatch
) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    agent_before = Path(legacy["agent_log"]).read_text(encoding="utf-8")
    create_before = agent_before.count("CREATE_CHAT")

    result = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_MODIFY_MODE", raising=False)
    resumed = resume_run(result.recovery_run_id)
    assert resumed.status == "completed"

    agent_after = Path(legacy["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == create_before
    assert "--resume" in agent_after
    assert str(legacy["chat_id"]) in agent_after
    assert _args_contains_model_auto(agent_after)

    successor_path, _successor = load_run(result.recovery_run_id)
    assert (successor_path / "git/diffs/01.patch").is_file()
    assert (successor_path / "codex/reviews/01.json").is_file()


def _args_contains_model_auto(agent_log: str) -> bool:
    for line in agent_log.splitlines():
        if line.startswith("ARGS:"):
            args = ast.literal_eval(line.removeprefix("ARGS:"))
            if "--model" in args:
                index = args.index("--model")
                if index + 1 < len(args) and args[index + 1] == "auto":
                    return True
    return False


def test_legacy_adoption_idempotent_and_conflicting_model(
    prepared_run, fake_clis, monkeypatch
) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    first = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    second = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    assert second.recovery_run_id == first.recovery_run_id
    assert second.reused_existing_successor is True

    with pytest.raises(ValidationError, match="different fallback model"):
        recover_run(
            str(legacy["run_id"]),
            adopt_current_cursor_output=True,
            cursor_model="composer-2.5-fast",
        )


def test_legacy_adoption_artifact_manifest_entry(prepared_run, fake_clis, monkeypatch) -> None:
    legacy = _legacy_usage_limit_source(prepared_run, fake_clis, monkeypatch)
    result = recover_run(
        str(legacy["run_id"]),
        adopt_current_cursor_output=True,
        cursor_model="auto",
    )
    successor_path, successor = load_run(result.recovery_run_id)
    manifest = json.loads((successor_path / "manifest.json").read_text(encoding="utf-8"))
    adopted_rel = str(successor.recovery.usage_limit_fingerprint_path)
    paths = {item["path"]: item["sha256"] for item in manifest["artifacts"]}
    assert adopted_rel in paths
    assert paths[adopted_rel] == sha256_file(successor_path / adopted_rel)
