"""End-to-end acceptance test with fake Cursor/Codex CLIs and disposable fixtures."""

from __future__ import annotations

import json
import re
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import write_session_rollout
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.integrations.codex import hooks_json
from ai_dev_loop.integrations.codex import install as integration_install
from ai_dev_loop.integrations.codex import paths as integration_paths
from ai_dev_loop.integrations.codex.target import CodexIntegrationTarget
from ai_dev_loop.state import RunStatus, load_run_state

runner = CliRunner()
FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
CODEX_SESSION_ID = "019abc00-0000-0000-0000-000000000000"
CURSOR_CHAT_ID = "019abc00-1111-2222-3333-444444444444"


def _orchestrator_artifact_names(repo: Path) -> list[str]:
    """Return paths under repo that look like orchestrator run artifacts."""

    suspicious = (
        "state.json",
        "manifest.json",
        "effective-config.yaml",
        "source-config.yaml",
    )
    found: list[str] = []
    for path in repo.rglob("*"):
        if path.is_file() and path.name in suspicious:
            found.append(str(path.relative_to(repo)))
    return found


def test_e2e_acceptance_with_fake_clis_and_disposable_fixtures(
    hermetic_tmp_path: Path,
    hermetic_home: Path,
    hermetic_xdg: Path,
    hermetic_codex_env: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full acceptance path: integrations, prepare, bounded loop, artifact checks."""

    # --- disposable Git repository ---
    repo = hermetic_tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    for relative in (
        "ai_dev_loop.yaml",
        "docs/plans/sample-plan.md",
        "docs/plans/prompt_sample-plan.txt",
    ):
        source = FIXTURE_REPO / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    # --- WSL CLI integrations into isolated home ---
    wsl_result = integration_install.install_integrations(home=hermetic_home)
    assert wsl_result.skill.path.is_file()
    assert integration_paths.hook_script_path(hermetic_home).is_file()

    # --- Desktop target into hermetic Windows-style home (no SQLite access) ---
    windows_codex = hermetic_tmp_path / "mnt" / "c" / "Users" / "WinUser" / ".codex"
    desktop_sessions = windows_codex / "sessions"
    desktop_sessions.mkdir(parents=True)
    (
        desktop_sessions / "rollout-2026-07-07T12-00-00-019abc00-1111-2222-3333-444444444444.jsonl"
    ).write_text("{}", encoding="utf-8")
    assert not any(windows_codex.glob("*.sqlite"))

    desktop_result = integration_install.install_integrations(
        home=hermetic_home,
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=windows_codex,
        wsl_distro="Ubuntu-22.04",
        install_session_bridge=True,
    )
    assert desktop_result.target == CodexIntegrationTarget.CODEX_DESKTOP_WSL
    bridge = hermetic_codex_env / "sessions" / "from-desktop"
    assert bridge.is_symlink()
    assert bridge.resolve() == desktop_sessions.resolve()

    sessions_status = runner.invoke(
        app,
        [
            "integrations",
            "sessions",
            "status",
            "--wsl-codex-home",
            str(hermetic_codex_env),
            "--windows-codex-home",
            str(windows_codex),
            "--output",
            "json",
        ],
    )
    assert sessions_status.exit_code == 0
    assert json.loads(sessions_status.stdout)["bridge_present"] is True

    # --- prepare with prompt on stdin ---
    write_session_rollout(hermetic_codex_env / "sessions", session_id=CODEX_SESSION_ID)
    prompt_text = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT_ID)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    with patch("sys.stdin", StringIO(prompt_text)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=CODEX_SESSION_ID,
                max_review_iterations=3,
            )
        )

    from ai_dev_loop.paths import run_dir

    run_path = run_dir("fixture-project", prepared.run_id)
    assert (run_path / "state.json").is_file()
    assert _orchestrator_artifact_names(repo) == []

    # --- start: stage-review-fix-review with fake CLIs ---
    result = start_run(prepared.run_id)
    assert result.status == "completed"
    assert result.iteration_count == 2

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.COMPLETED
    assert state.cursor.chat_id == CURSOR_CHAT_ID
    assert state.codex.session_id == CODEX_SESSION_ID
    assert len(state.iterations) == 2
    assert state.iterations[0]["kind"] == "initial_implementation"
    assert state.iterations[1]["kind"] == "cursor_correction"
    assert (run_path / "prompts/fixes/01.txt").is_file()
    assert (run_path / "cursor/chat.json").is_file()
    assert (run_path / "codex/reviews/01.json").is_file()
    assert (run_path / "codex/reviews/02.json").is_file()
    assert (run_path / "git/diffs/01.patch").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    create_chat_ids = re.findall(r"CREATE_CHAT:([^\n]+)", agent_log)
    assert len(create_chat_ids) == 1
    assert create_chat_ids[0] == CURSOR_CHAT_ID
    resume_ids = re.findall(r"'--resume', '([^']+)'", agent_log)
    assert len(resume_ids) >= 2
    assert set(resume_ids) == {CURSOR_CHAT_ID}

    codex_log = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert codex_log.count(CODEX_SESSION_ID) >= 2
    assert "--last" not in codex_log

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.strip()
    assert _orchestrator_artifact_names(repo) == []

    # --- desktop uninstall preserves bridge and unrelated hooks ---
    hooks_path = windows_codex / "hooks.json"
    document = hooks_json.load_hooks_document(hooks_path)
    document["hooks"]["PreToolUse"] = [
        {"matcher": ".*", "hooks": [{"type": "command", "command": "echo keep"}]}
    ]
    hooks_json.write_hooks_document(hooks_path, document, backup=False)

    integration_install.uninstall_integrations(
        home=hermetic_home,
        target=CodexIntegrationTarget.CODEX_DESKTOP_WSL,
        windows_codex_home=windows_codex,
        wsl_distro="Ubuntu-22.04",
    )
    assert bridge.is_symlink()
    remaining = hooks_json.load_hooks_document(hooks_path)
    assert "PreToolUse" in remaining["hooks"]
    assert hooks_json.registration_status(remaining).present is False

    integration_install.uninstall_integrations(home=hermetic_home)
    assert not integration_paths.skill_path(hermetic_home).is_file()
