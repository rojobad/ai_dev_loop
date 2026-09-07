"""Phase 16.1 explicit A/B local-loop regression barrier.

Observational characterization only: no production behavior changes.
Uses disposable Git repos, isolated HOME/XDG, sanitized session rollouts,
and hermetic fake ``agent`` / ``codex`` / ``gh`` CLIs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import textwrap
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.abort_control import ACTIVE_PROCESS_REL_PATH
from ai_dev_loop.commands.abort import abort_run
from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.commands.extend import extend_review_iterations
from ai_dev_loop.commands.inspect import render_inspect
from ai_dev_loop.commands.launch import launch_run
from ai_dev_loop.commands.logs import render_logs
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run, render_prepare_output
from ai_dev_loop.commands.recover import recover_run
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.commands.status import render_status
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.fresh_codex_reviewer import FRESH_REVIEWER_BOOTSTRAP_UNCERTAINTY_ARTIFACT
from ai_dev_loop.launcher import (
    process_identity_matches,
    read_launcher_record,
    validate_launcher_record,
)
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, load_run_state

CONTROLLER_A = "019abc00-aaaa-7000-8000-0000000000aa"
REVIEWER_B = "019abc00-bbbb-7000-8000-0000000000bb"
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING = "high"
CURSOR_CHAT = "019abc00-cccc-7000-8000-0000000000cc"
WRONG_CONTROLLER = "019abc00-dddd-7000-8000-0000000000dd"

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "completed_with_residual_risk",
        "max_iterations_reached",
        "failed",
        "interrupted",
        "aborted",
    }
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_fake_gh(bin_dir: Path, log_path: Path) -> Path:
    """Fail-fast fake ``gh`` that records every invocation."""

    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {log_path.as_posix()!r}
        echo "fake gh must not be invoked during local A/B loops" >&2
        exit 97
        """
    )
    path = bin_dir / "gh"
    _write_executable(path, script)
    log_path.write_text("", encoding="utf-8")
    return path


def _assert_no_github_side_effects(
    *,
    run_directory: Path,
    repo: Path,
    gh_log: Path,
    initial_commit_count: int,
) -> None:
    assert not (run_directory / "github").exists()
    assert gh_log.read_text(encoding="utf-8") == ""

    effective = (run_directory / "effective-config.yaml").read_text(encoding="utf-8")
    if "github:" in effective:
        github_block = effective.split("github:", 1)[1]
        # Stop at the next top-level key (unindented line with a colon).
        block_lines: list[str] = []
        for line in github_block.splitlines()[1:]:
            if line and not line.startswith((" ", "\t")):
                break
            block_lines.append(line)
        block = "\n".join(block_lines)
        assert "enabled: true" not in block

    commits = subprocess.check_output(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        text=True,
    ).strip()
    assert int(commits) == initial_commit_count

    # No push remotes or publication artifacts from the local loop.
    assert not any(run_directory.rglob("*publish*"))
    remotes = subprocess.check_output(["git", "remote"], cwd=repo, text=True).strip()
    assert remotes == ""


def _prepare_ab_run(
    git_repo: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_review_iterations: int = 3,
    github_disabled_explicit: bool = False,
) -> object:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", REVIEWER_B)

    if github_disabled_explicit:
        config_path = git_repo / "ai_dev_loop.yaml"
        text = config_path.read_text(encoding="utf-8")
        if "github:" not in text:
            config_path.write_text(
                text.rstrip() + "\n\ngithub:\n  enabled: false\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "ai_dev_loop.yaml"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "explicitly disable github"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )

    prompt = (git_repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_A,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
                max_review_iterations=max_review_iterations,
                output="json",
            )
        )


def _commit_count(repo: Path) -> int:
    return int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()
    )


def _repo_fingerprint(repo: Path) -> tuple[str, str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        text=True,
    )
    staged = subprocess.check_output(["git", "diff", "--cached"], cwd=repo, text=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return status, staged, head


def _capture_live_launcher_identity(
    run_directory: Path, *, run_id: str
) -> tuple[str, str, int, int, int] | None:
    """Return (run_id, worker_token, pid, pid_starttime, pgid) when identity is unambiguous.

    Refuses missing records, run_id mismatch, non-positive pid/pgid, null
    ``pid_starttime``, or stale live-process identity (reused PID/PGID).
    """

    validation = validate_launcher_record(run_directory, run_id=run_id)
    if validation is None:
        return None
    record = validation.record
    if record.run_id != run_id:
        return None
    if not record.worker_token:
        return None
    if record.pid <= 0 or record.pgid <= 0:
        return None
    if record.pid_starttime is None:
        return None
    identity_ok, _reason = process_identity_matches(record)
    if not identity_ok:
        return None
    return (
        record.run_id,
        record.worker_token,
        record.pid,
        record.pid_starttime,
        record.pgid,
    )


def _terminate_run_worker(run_directory: Path, *, run_id: str) -> None:
    """Best-effort cleanup that signals only one freshly validated launcher identity."""

    identity = _capture_live_launcher_identity(run_directory, run_id=run_id)
    if identity is None:
        return
    _run_id, _worker_token, _pid, _pid_starttime, pgid = identity
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        current = _capture_live_launcher_identity(run_directory, run_id=run_id)
        if current is None or current != identity:
            # Process exited, or launcher record was replaced/ambiguous — do not SIGKILL.
            return
        time.sleep(0.05)
    # Require the exact same live identity tuple before escalating to SIGKILL.
    current = _capture_live_launcher_identity(run_directory, run_id=run_id)
    if current is None or current != identity:
        return
    try:
        os.killpg(current[4], signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _wait_for_codex_bootstrap_ready(marker_path: Path, *, timeout: float = 45.0) -> None:
    """Wait until fake Codex emits bootstrap identity after ``thread.started``."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker_path.is_file() and marker_path.read_text(encoding="utf-8").strip() == "ready":
            return
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for codex bootstrap ready marker at {marker_path.as_posix()}"
    )


def _wait_for_active_component(
    run_directory: Path,
    *,
    run_id: str,
    component: str,
    timeout: float = 45.0,
    min_iteration: int | None = None,
) -> None:
    """Wait until active-process metadata matches this disposable run and component."""

    deadline = time.monotonic() + timeout
    path = run_directory / ACTIVE_PROCESS_REL_PATH
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            iteration = payload.get("iteration") if isinstance(payload, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("run_id") == run_id
                and payload.get("component") == component
                and (
                    min_iteration is None
                    or (isinstance(iteration, int) and iteration >= min_iteration)
                )
            ):
                return
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for active-process metadata run_id={run_id!r} component={component!r}"
    )


def _wait_for_detached_terminal(
    run_directory: Path,
    *,
    run_id: str,
    timeout: float = 90.0,
    accept: frozenset[str] | None = None,
) -> object:
    """Poll durable state until a terminal (or accepted) status; always cleanup on failure."""

    accepted = accept or TERMINAL_STATUSES
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    launcher_outcome = None
    try:
        while time.monotonic() < deadline:
            state = load_run_state(run_directory / "state.json")
            last_status = state.status.value
            launcher = read_launcher_record(run_directory)
            launcher_outcome = None if launcher is None else launcher.outcome.value
            if state.status.value in accepted and (
                launcher is None or launcher.outcome.value != "running"
            ):
                return state
            if (
                launcher is not None
                and launcher.outcome.value != "running"
                and state.status.value in TERMINAL_STATUSES
            ):
                return state
            time.sleep(0.2)
        raise AssertionError(
            "timed out waiting for detached worker terminal state; "
            f"last_status={last_status} launcher_outcome={launcher_outcome}"
        )
    except Exception:
        _terminate_run_worker(run_directory, run_id=run_id)
        raise


def _agent_resume_chat_ids(agent_log: Path) -> list[str]:
    text = agent_log.read_text(encoding="utf-8")
    return re.findall(r"'--resume', '([^']+)'", text)


def _codex_bootstrap_invocations(codex_log: Path) -> int:
    if not codex_log.is_file():
        return 0
    text = codex_log.read_text(encoding="utf-8")
    return sum(
        1
        for line in text.splitlines()
        if line.startswith("ARGS:") and "'resume'" not in line and '"resume"' not in line
    )


def _codex_resume_session_ids(codex_log: Path) -> list[str]:
    text = codex_log.read_text(encoding="utf-8")
    # Fake codex logs ARGS as a Python repr list; session id is the final positional.
    sessions: list[str] = []
    for line in text.splitlines():
        if not line.startswith("ARGS:"):
            continue
        if "'resume'" not in line and '"resume"' not in line:
            continue
        assert "--last" not in line
        assert CONTROLLER_A not in line
        # Trailing session UUID before the stdin placeholder '-'.
        match = re.search(
            r"['\"]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})['\"]\s*,\s*['\"]-['\"]\s*\]",
            line,
        )
        if match:
            sessions.append(match.group(1))
    return sessions


def _assert_privacy_surfaces(
    *,
    run_directory: Path,
    rendered_outputs: list[str],
) -> None:
    for surface in rendered_outputs:
        assert CONTROLLER_A not in surface
        assert REVIEWER_B not in surface

    events = run_directory / "logs" / "events.jsonl"
    if events.is_file():
        event_text = events.read_text(encoding="utf-8")
        # Structured events must not embed full controller identity as a field value.
        assert f'"controller_session_id": "{CONTROLLER_A}"' not in event_text


# ---------------------------------------------------------------------------
# 1. A/B prepare with GitHub absent / disabled
# ---------------------------------------------------------------------------


def test_ab_prepare_github_absent_persists_identities_without_agents(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    initial_commits = _commit_count(git_repo)

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    state = load_run_state(prepared.run_directory / "state.json")

    assert state.status == RunStatus.PREPARED
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert state.codex.session_id is None
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.review_model == REVIEW_MODEL
    assert state.codex.fresh_reviewer.review_reasoning_effort == REVIEW_REASONING
    assert state.cursor.chat_id is None
    assert state.codex.review_model == REVIEW_MODEL
    assert state.codex.review_reasoning_effort == REVIEW_REASONING
    assert state.codex.review_model_source == "explicit"
    assert state.codex.review_reasoning_source == "explicit"
    assert (prepared.run_directory / "plan" / "plan.md").is_file()
    assert (prepared.run_directory / "prompts" / "cursor-initial.txt").is_file()
    assert state.plan.snapshot_path == "plan/plan.md"
    assert state.prompt.snapshot_path == "prompts/cursor-initial.txt"
    assert state.plan.sha256
    assert state.prompt.sha256
    assert prepared.launch_command is not None
    assert "--controller-session-id" in prepared.launch_command
    assert prepared.requires_codex_exit is False
    assert prepared.reviewer_must_remain_inactive is False

    rendered = render_prepare_output(prepared, output="json")
    payload = json.loads(rendered)
    assert payload["launch_command"] == prepared.launch_command
    assert payload["controller_session_id_present"] is True
    # JSON must not expose a dedicated full-ID field; launch_command intentionally
    # embeds the controller id so A can copy-paste an executable command.
    assert "controller_session_id" not in payload
    assert REVIEWER_B not in rendered
    assert CONTROLLER_A in payload["launch_command"]
    assert CONTROLLER_A not in rendered.replace(payload["launch_command"], "")

    agent_log_path = Path(fake_clis["agent_log"])
    agent_log = agent_log_path.read_text(encoding="utf-8") if agent_log_path.is_file() else ""
    codex_log_path = Path(fake_clis["codex_log"])
    codex_log = codex_log_path.read_text(encoding="utf-8") if codex_log_path.is_file() else ""
    assert "CREATE_CHAT" not in agent_log
    assert "ARGS:" not in agent_log
    assert "ARGS:" not in codex_log
    assert not str(prepared.run_directory).startswith(str(git_repo))
    _assert_no_github_side_effects(
        run_directory=prepared.run_directory,
        repo=git_repo,
        gh_log=gh_log,
        initial_commit_count=initial_commits,
    )


def test_ab_prepare_github_explicitly_disabled(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    prepared = _prepare_ab_run(
        git_repo,
        isolated_home,
        monkeypatch,
        github_disabled_explicit=True,
    )
    effective = (prepared.run_directory / "effective-config.yaml").read_text(encoding="utf-8")
    assert "github:" in effective
    assert "enabled: false" in effective
    assert gh_log.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 2. Detached launch multi-iteration local loop
# ---------------------------------------------------------------------------


def test_ab_detached_launch_multi_iteration_completed_without_github(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    initial_commits = _commit_count(git_repo)
    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    launched = launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
    assert launched.already_running is False

    try:
        state = _wait_for_detached_terminal(
            prepared.run_directory,
            run_id=prepared.run_id,
            accept=frozenset({"completed"}),
        )
    finally:
        _terminate_run_worker(prepared.run_directory, run_id=prepared.run_id)

    assert state.status == RunStatus.COMPLETED
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert state.codex.session_id == REVIEWER_B
    assert state.cursor.chat_id == CURSOR_CHAT
    assert len(state.iterations) == 2
    assert state.iterations[0]["kind"] == "initial_implementation"
    assert state.iterations[1]["kind"] == "cursor_correction"

    agent_log = Path(fake_clis["agent_log"])
    create_count = agent_log.read_text(encoding="utf-8").count("CREATE_CHAT")
    assert create_count == 1
    resume_ids = _agent_resume_chat_ids(agent_log)
    assert len(resume_ids) >= 1
    assert set(resume_ids) == {CURSOR_CHAT}

    codex_sessions = _codex_resume_session_ids(Path(fake_clis["codex_log"]))
    assert len(codex_sessions) >= 1
    assert set(codex_sessions) == {REVIEWER_B}
    assert _codex_bootstrap_invocations(Path(fake_clis["codex_log"])) == 1
    assert "--last" not in Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert CONTROLLER_A not in Path(fake_clis["codex_log"]).read_text(encoding="utf-8")

    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_repo,
        text=True,
    ).strip()
    assert staged
    assert not str(prepared.run_directory.resolve()).startswith(str(git_repo.resolve()))
    assert not (git_repo / "state.json").exists()
    assert not (git_repo / "manifest.json").exists()

    status_text = render_status(prepared.run_id, output="text")
    logs_text = render_logs(prepared.run_id)
    inspect_text = render_inspect(prepared.run_id, output="text")
    _assert_privacy_surfaces(
        run_directory=prepared.run_directory,
        rendered_outputs=[status_text, logs_text, inspect_text],
    )
    _assert_no_github_side_effects(
        run_directory=prepared.run_directory,
        repo=git_repo,
        gh_log=gh_log,
        initial_commit_count=initial_commits,
    )


# ---------------------------------------------------------------------------
# 3. Wrong controller + duplicate live launch
# ---------------------------------------------------------------------------


def test_ab_wrong_controller_and_duplicate_launch_are_fail_safe(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "30")

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)

    with pytest.raises(ValidationError, match="mismatched"):
        launch_run(prepared.run_id, controller_session_id=WRONG_CONTROLLER)

    first = launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
    assert first.already_running is False
    assert first.launcher_pid is not None

    try:
        # Wait until launcher record is durable and worker has started.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            record = read_launcher_record(prepared.run_directory)
            if record is not None and record.outcome.value == "running":
                break
            time.sleep(0.05)

        second = launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
        assert second.already_running is True
        assert second.launcher_pid == first.launcher_pid

        # Only one launcher record / worker token for the run.
        launcher_files = list((prepared.run_directory / "locks").glob("launcher*.json"))
        assert len(launcher_files) <= 1 or read_launcher_record(prepared.run_directory) is not None
        abort_run(prepared.run_id)
        _wait_for_detached_terminal(
            prepared.run_directory,
            run_id=prepared.run_id,
            timeout=30.0,
            accept=frozenset({"aborted", "failed", "completed"}),
        )
    finally:
        _terminate_run_worker(prepared.run_directory, run_id=prepared.run_id)

    assert gh_log.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 4. max_iterations_reached -> extend -> detached launch
# ---------------------------------------------------------------------------


def test_ab_max_iterations_extend_then_detached_launch_completes(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    initial_commits = _commit_count(git_repo)
    prepared = _prepare_ab_run(
        git_repo,
        isolated_home,
        monkeypatch,
        max_review_iterations=3,
    )

    # Drive to max via in-process start for deterministic setup, then extend + launch.
    first = start_run(prepared.run_id)
    assert first.status == "max_iterations_reached"
    state = load_run_state(prepared.run_directory / "state.json")
    assert state.cursor.chat_id == CURSOR_CHAT
    assert state.codex.session_id == REVIEWER_B
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert (prepared.run_directory / "prompts/fixes/03.txt").is_file()
    agent_calls_before = Path(fake_clis["agent_log"]).read_text(encoding="utf-8").count("-p")
    assert agent_calls_before == 3

    extension = extend_review_iterations(prepared.run_id, additional_review_iterations=1)
    assert extension.status == "waiting_for_cursor_fix"
    assert extension.max_review_iterations == 4
    assert extension.current_review_iteration == 3

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    # Reset sequence counter so the next review uses no_findings.
    counter = Path(fake_clis.get("codex_review_counter", Path("/nonexistent")))
    if counter.is_file():
        counter.write_text("0", encoding="utf-8")

    launched = launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
    assert launched.already_running is False
    try:
        state = _wait_for_detached_terminal(
            prepared.run_directory,
            run_id=prepared.run_id,
            accept=frozenset({"completed"}),
        )
    finally:
        _terminate_run_worker(prepared.run_directory, run_id=prepared.run_id)

    assert state.status == RunStatus.COMPLETED
    assert state.workflow.current_review_iteration == 4
    assert state.cursor.chat_id == CURSOR_CHAT
    assert state.codex.session_id == REVIEWER_B
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert len(state.iterations) == 4
    assert (prepared.run_directory / "cursor/iterations/04/events.jsonl").is_file()
    assert (prepared.run_directory / "codex/reviews/04.json").is_file()

    agent_log = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT") == 1
    assert agent_log.count("-p") == 4
    assert set(_agent_resume_chat_ids(Path(fake_clis["agent_log"]))) == {CURSOR_CHAT}
    assert set(_codex_resume_session_ids(Path(fake_clis["codex_log"]))) == {REVIEWER_B}
    _assert_no_github_side_effects(
        run_directory=prepared.run_directory,
        repo=git_repo,
        gh_log=gh_log,
        initial_commit_count=initial_commits,
    )


# ---------------------------------------------------------------------------
# 5. Local recover successor preserves A/B identities
# ---------------------------------------------------------------------------


def test_ab_recover_successor_preserves_identities_and_skips_completed_cursor(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")

    initial_commits = _commit_count(git_repo)
    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)

    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(prepared.run_id)

    source = load_run_state(prepared.run_directory / "state.json")
    assert source.status == RunStatus.FAILED
    assert source.cursor.chat_id == CURSOR_CHAT
    assert source.codex.session_id == REVIEWER_B
    assert source.controller is not None
    assert source.controller.controller_session_id == CONTROLLER_A
    source_agent = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    source_create = source_agent.count("CREATE_CHAT")
    # Capture repository fingerprint after the failed local loop, before recover.
    repo_after_failure = _repo_fingerprint(git_repo)

    # recover must not invoke agents or mutate Git relative to the failed checkpoint.
    dry = recover_run(prepared.run_id, dry_run=True)
    assert dry.eligible is True
    assert dry.checkpoint == "reviewing"
    assert _repo_fingerprint(git_repo) == repo_after_failure

    first = recover_run(prepared.run_id)
    second = recover_run(prepared.run_id)
    assert second.recovery_run_id == first.recovery_run_id
    assert second.reused_existing_successor is True
    assert Path(fake_clis["agent_log"]).read_text(encoding="utf-8") == source_agent
    assert _repo_fingerprint(git_repo) == repo_after_failure
    assert _commit_count(git_repo) == initial_commits

    # Source remains immutable terminal failed.
    source_after = load_run_state(prepared.run_directory / "state.json")
    assert source_after.status == RunStatus.FAILED
    assert source_after.controller is not None
    assert source_after.controller.controller_session_id == CONTROLLER_A

    successor_path, successor = load_run(first.recovery_run_id)
    assert successor.status == RunStatus.INTERRUPTED
    # Local recover preserves controller A on the successor so controller status
    # and A-owned control remain available after recovery.
    assert successor.controller is not None
    assert successor.controller.controller_session_id == CONTROLLER_A
    assert successor.codex.session_id == REVIEWER_B
    assert successor.cursor.chat_id == CURSOR_CHAT
    assert successor.codex.review_model == source.codex.review_model
    assert successor.codex.review_reasoning_effort == source.codex.review_reasoning_effort
    assert successor.recovery is not None
    assert successor.recovery.source_run_id == prepared.run_id
    assert successor.recovery.recovered_checkpoint == "reviewing"

    # Local recover successors continue via resume (not launch): launch only accepts
    # prepared | waiting_for_cursor_fix. Controller can still inspect the successor.
    with pytest.raises(ValidationError, match="prepared or waiting_for_cursor_fix"):
        launch_run(first.recovery_run_id, controller_session_id=CONTROLLER_A)

    status = controller_status(
        controller_session_id=CONTROLLER_A,
        repo_path=git_repo,
        run_id=first.recovery_run_id,
        include_terminal=True,
    )
    assert status.run_id == first.recovery_run_id

    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    resumed = resume_run(first.recovery_run_id)
    assert resumed.status == "completed"
    assert resumed.chat_id == CURSOR_CHAT

    agent_after = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    assert agent_after.count("CREATE_CHAT") == source_create
    # Proven-complete Cursor turn must not repeat.
    assert agent_after.count("-p") == source_agent.count("-p")
    assert set(_codex_resume_session_ids(Path(fake_clis["codex_log"]))) == {REVIEWER_B}
    assert "--last" not in Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert gh_log.read_text(encoding="utf-8") == ""
    assert not (successor_path / "github").exists()


# ---------------------------------------------------------------------------
# 6. Abort during detached Cursor / Codex boundaries
# ---------------------------------------------------------------------------


def test_ab_abort_during_detached_cursor_preserves_repo_and_identities(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "45")

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    repo_before = _repo_fingerprint(git_repo)
    head_before = repo_before[2]

    launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
    try:
        _wait_for_active_component(
            prepared.run_directory,
            run_id=prepared.run_id,
            component="cursor",
            timeout=30.0,
            min_iteration=1,
        )

        abort_result = abort_run(prepared.run_id)
        assert abort_result.abort_requested is True
        state = _wait_for_detached_terminal(
            prepared.run_directory,
            run_id=prepared.run_id,
            timeout=40.0,
            accept=frozenset({"aborted"}),
        )
    finally:
        _terminate_run_worker(prepared.run_directory, run_id=prepared.run_id)

    assert state.status == RunStatus.ABORTED
    assert (prepared.run_directory / "locks" / "abort-request.json").is_file()
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert state.codex.session_id is None
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.bootstrap_uncertainty_reason is None
    assert state.codex.fresh_reviewer.bootstrap_session_id is None
    assert not (prepared.run_directory / FRESH_REVIEWER_BOOTSTRAP_UNCERTAINTY_ARTIFACT).exists()
    assert _codex_bootstrap_invocations(Path(fake_clis["codex_log"])) == 0

    status_after, staged_after, head_after = _repo_fingerprint(git_repo)
    assert head_after == head_before
    assert gh_log.read_text(encoding="utf-8") == ""

    status_text = render_status(prepared.run_id, output="text")
    assert CONTROLLER_A not in status_text
    assert REVIEWER_B not in status_text
    _ = (status_after, staged_after)


def test_ab_abort_during_detached_codex_preserves_staged_work(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "sleep")
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "45")

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    bootstrap_ready = prepared.run_directory / "locks" / "codex-bootstrap-ready.txt"
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_READY_PATH", str(bootstrap_ready))
    launch_run(prepared.run_id, controller_session_id=CONTROLLER_A)
    try:
        _wait_for_active_component(
            prepared.run_directory,
            run_id=prepared.run_id,
            component="codex",
            timeout=45.0,
        )
        _wait_for_codex_bootstrap_ready(bootstrap_ready, timeout=45.0)

        staged_before = subprocess.check_output(
            ["git", "diff", "--cached"],
            cwd=git_repo,
            text=True,
        )
        assert staged_before.strip(), "expected staged work before Codex abort"

        abort_result = abort_run(prepared.run_id)
        assert abort_result.abort_requested is True
        state = _wait_for_detached_terminal(
            prepared.run_directory,
            run_id=prepared.run_id,
            timeout=40.0,
            accept=frozenset({"aborted"}),
        )
    finally:
        _terminate_run_worker(prepared.run_directory, run_id=prepared.run_id)

    assert state.status == RunStatus.ABORTED
    staged_after = subprocess.check_output(["git", "diff", "--cached"], cwd=git_repo, text=True)
    assert staged_after == staged_before
    assert state.cursor.chat_id == CURSOR_CHAT
    assert state.codex.session_id == REVIEWER_B
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.bootstrap_session_id == REVIEWER_B
    assert state.codex.fresh_reviewer.bootstrap_uncertainty_reason is None
    assert _codex_bootstrap_invocations(Path(fake_clis["codex_log"])) == 1
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert gh_log.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# Result matrix: residual risk, failed, interrupted (application boundary)
# ---------------------------------------------------------------------------


def test_ab_result_completed_with_residual_risk(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "blocked_environment")

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    result = start_run(prepared.run_id)
    assert result.status == "completed_with_residual_risk"
    state = load_run_state(prepared.run_directory / "state.json")
    assert state.status == RunStatus.COMPLETED_WITH_RESIDUAL_RISK
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert state.codex.session_id == REVIEWER_B
    assert state.cursor.chat_id == CURSOR_CHAT
    assert gh_log.read_text(encoding="utf-8") == ""


def test_ab_result_failed_on_codex_nonzero(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(prepared.run_id)
    state = load_run_state(prepared.run_directory / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "codex/events/" in state.last_error
    assert "exit code 2" in state.last_error
    # Raw stderr dump must not be embedded in last_error.
    assert "codex review failed\n" not in state.last_error
    human = (prepared.run_directory / "logs" / "ai_dev_loop.log").read_text(encoding="utf-8")
    assert CONTROLLER_A not in human
    assert REVIEWER_B not in human
    assert gh_log.read_text(encoding="utf-8") == ""


def test_ab_result_interrupted_on_codex_timeout(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.process import StreamingProcessResult

    gh_log = Path(fake_clis["bin_dir"]).parent / "gh.log"
    _install_fake_gh(Path(fake_clis["bin_dir"]), gh_log)
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    def timed_out_streaming(args, **kwargs):  # type: ignore[no-untyped-def]
        stdout = json.dumps({"type": "thread.started", "thread_id": REVIEWER_B}) + "\n"
        stdout_path = kwargs.get("stdout_path")
        if isinstance(stdout_path, Path):
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text(stdout, encoding="utf-8")
        return StreamingProcessResult(
            args=list(args),
            returncode=124,
            stdout=stdout,
            stderr="",
            timed_out=True,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr("ai_dev_loop.runners.codex.run_process_streaming", timed_out_streaming)

    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    with pytest.raises(AiDevLoopError, match="timed out"):
        start_run(prepared.run_id)
    state = load_run_state(prepared.run_directory / "state.json")
    assert state.status == RunStatus.INTERRUPTED
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_A
    assert state.codex.session_id == REVIEWER_B
    assert gh_log.read_text(encoding="utf-8") == ""


def test_ab_status_logs_inspect_expose_loop_without_github_fields(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_CHAT_ID", CURSOR_CHAT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)
    start_run(prepared.run_id)

    status_json = json.loads(render_status(prepared.run_id, output="json"))
    assert status_json["status"] == "completed"
    inspect_json = json.loads(render_inspect(prepared.run_id, output="json"))
    assert inspect_json["status"] == "completed"
    logs = render_logs(prepared.run_id, component="codex")
    assert "# Review" not in logs
    assert "Fix the sample issue" not in logs
    assert CONTROLLER_A not in logs
    assert REVIEWER_B not in logs
