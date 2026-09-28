"""Integration tests for Phase 21.5 process output inspection."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.integration.phase21_4_helpers import (
    codex_runner_attempt_ids,
    cursor_turn_attempt_ids,
    make_tick_service,
    run_tick_once,
    submit_sample_run,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.runners.cursor import CREATE_CHAT_STDOUT_REL
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.attempt_backend import (
    ObserveResult,
    TerminationClass,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    validate_attempt_id,
    validate_unit_identity,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

runner = CliRunner()
CHILD_SENTINEL = "CHILD-PROCESS-OUTPUT-SENTINEL-21-5"
ENVELOPE_MARKER = '"effect_kind"'
SECRET_SENTINEL = "INTEGRATION_PHASE21_5_SECRET_SENTINEL"


@dataclass
class DetachedCursorChildBackend(FakeAgentProcessBackend):
    """Keep cursor runner children alive across observe calls (no blocking subprocess.run)."""

    _detached: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)

    def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
        validate_attempt_id(attempt_id)
        validate_unit_identity(unit_identity)
        self.observe_calls.append((unit_identity, attempt_id))
        scenario = self.scenarios.get(attempt_id, self.default_scenario)
        if scenario.observation_unavailable:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                owned=False,
                absence_proven=False,
            )
        if scenario.stale_identity:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=False,
                absence_proven=False,
            )
        if not scenario.owned:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=False,
                absence_proven=False,
            )
        if attempt_id not in self._launched:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=True,
                absence_proven=True,
            )
        if scenario.stays_active_after_terminate and any(
            call_attempt_id == attempt_id for _, call_attempt_id in self.terminate_calls
        ):
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=True,
                absence_proven=False,
            )
        request = self._launched[attempt_id]
        if attempt_id in self._completed:
            return self._completed[attempt_id]
        if scenario.missing_after_launch and attempt_id not in self._completed:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=True,
                absence_proven=True,
            )
        count = self._observe_counts.get(attempt_id, 0) + 1
        self._observe_counts[attempt_id] = count
        if count <= scenario.active_ticks:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=True,
                absence_proven=False,
            )
        if self._is_agent_attempt(request):
            proc = self._detached.get(attempt_id)
            if proc is None:
                proc = subprocess.Popen(
                    request.agent_argv,
                    cwd=request.working_directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self._detached[attempt_id] = proc
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.ACTIVE,
                    owned=True,
                    absence_proven=False,
                )
            exit_code = proc.poll()
            if exit_code is None:
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.ACTIVE,
                    owned=True,
                    absence_proven=False,
                )
            stdout_bytes, stderr_bytes = proc.communicate()
            if stdout_bytes:
                request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
                request.stdout_path.write_bytes(stdout_bytes)
            if stderr_bytes:
                request.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                request.stderr_path.write_bytes(stderr_bytes)
            if exit_code == 0:
                termination = TerminationClass.SUCCESS
                lifecycle = UnitLifecycleState.INACTIVE
            elif exit_code == 124:
                termination = TerminationClass.TIMEOUT
                lifecycle = UnitLifecycleState.FAILED
            else:
                termination = TerminationClass.NONZERO_EXIT
                lifecycle = UnitLifecycleState.FAILED
            envelope_path = request.result_envelope_path
            stdout_path = request.stdout_path
            stderr_path = request.stderr_path
            stdout_rel = attempt_stdout_rel(attempt_id)
            stderr_rel = attempt_stderr_rel(attempt_id)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            if not stdout_path.is_file():
                stdout_path.write_text("", encoding="utf-8")
            if not stderr_path.is_file():
                stderr_path.write_text("", encoding="utf-8")
            stdout_sha = sha256_file(stdout_path)
            stderr_sha = sha256_file(stderr_path)
            envelope = build_result_envelope(
                attempt_id=attempt_id,
                unit_identity=unit_identity,
                exit_code=exit_code,
                termination_class=termination,
                stdout_artifact_path=stdout_rel,
                stdout_sha256=stdout_sha,
                stderr_artifact_path=stderr_rel,
                stderr_sha256=stderr_sha,
            )
            envelope_path.parent.mkdir(parents=True, exist_ok=True)
            envelope_path.write_bytes(envelope)
            if os.name != "nt":
                os.chmod(envelope_path, SENSITIVE_FILE_MODE)
            _ = envelope_sha256(envelope)
            result = ObserveResult(
                lifecycle_state=lifecycle,
                owned=True,
                absence_proven=False,
                exit_code=exit_code,
                termination_class=termination,
                result_envelope_path=envelope_path,
            )
            self._completed[attempt_id] = result
            self._detached.pop(attempt_id, None)
            return result
        return super().observe(unit_identity=unit_identity, attempt_id=attempt_id)

    def terminate(self, *, unit_identity: str, attempt_id: str) -> None:
        proc = self._detached.get(attempt_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        super().terminate(unit_identity=unit_identity, attempt_id=attempt_id)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _invoke(args: list[str]) -> tuple[int, dict[str, object], str]:
    result = runner.invoke(app, args)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result.stderr


def test_c06_info_reports_process_output_capability() -> None:
    data = build_integration_info_data()
    assert data.capabilities.process_output is True


def test_c01_output_wrong_run_is_not_found(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    code, payload, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            "other-run-id",
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert payload["ok"] is False
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr


def test_c01_codex_child_stdout_distinct_from_runner_envelope(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(120):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    code, payload, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert payload["ok"] is True
    data = payload["data"]
    assert data["available"] is True
    decoded = base64.b64decode(str(data["contentBase64"])).decode("utf-8")
    assert CHILD_SENTINEL in decoded
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    envelope_stdout = (run_root / "attempts" / attempt_id / "stdout.txt").read_text(
        encoding="utf-8"
    )
    assert CHILD_SENTINEL not in envelope_stdout
    assert ENVELOPE_MARKER in envelope_stdout
    assert SECRET_SENTINEL not in stderr
    assert data["complete"] is True


def test_f01_cli_rejects_tampered_invocation_binding(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    evidence_path = run_root / "attempts" / attempt_id / "invocation-evidence.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["run_id"] = "tampered-run-id"
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["ok"] is False
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in stderr


def test_c03_live_codex_stdout_visible_before_child_release(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    release = tmp_path / "codex-release"
    monkeypatch.setenv("FAKE_CODEX_CHILD_RELEASE_FILE", str(release))
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(40):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "awaiting_codex_review":
                break
    tick_error: list[BaseException] = []
    stop_ticks = threading.Event()

    def _drive_ticks_until_release() -> None:
        try:
            while not stop_ticks.is_set():
                run_tick_once(tick)
                time.sleep(0.02)
        except BaseException as exc:
            tick_error.append(exc)
        finally:
            stop_ticks.set()

    driver = threading.Thread(target=_drive_ticks_until_release, daemon=True)
    driver.start()
    attempt_id: str | None = None
    seen_live = False
    deadline = time.monotonic() + 45.0
    try:
        while time.monotonic() < deadline:
            launches = codex_runner_attempt_ids(backend)
            if launches:
                attempt_id = launches[-1]
                code, payload, _ = _invoke(
                    [
                        "integration",
                        "run",
                        "output",
                        run_id,
                        "--attempt",
                        attempt_id,
                        "--stream",
                        "stdout",
                        "--output",
                        "json",
                    ]
                )
                if code == 0 and payload.get("ok"):
                    data = payload["data"]
                    if data.get("available"):
                        decoded = base64.b64decode(str(data["contentBase64"])).decode("utf-8")
                        if CHILD_SENTINEL in decoded:
                            seen_live = True
                            assert data["complete"] is False
                            release.write_text("go", encoding="utf-8")
                            break
            time.sleep(0.05)
    finally:
        stop_ticks.set()
        if not release.is_file():
            release.write_text("go", encoding="utf-8")
        driver.join(timeout=10.0)
        assert not driver.is_alive()
    for _ in range(80):
        if tick_error:
            break
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    if tick_error:
        raise tick_error[0]
    assert attempt_id is not None
    assert seen_live
    code, payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert payload["data"]["complete"] is True


def _cursor_run_turn_attempt_ids(tick, run_id: str) -> list[str]:
    matched: list[str] = []
    with tick.store.begin_read() as conn:
        rows = conn.execute(
            "SELECT attempt_id, dispatch_id FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        for row in rows:
            dispatch = tick.store.get_effect_by_dispatch_id(conn, str(row["dispatch_id"]))
            if dispatch is not None and str(dispatch["effect_kind"]) == RUN_CURSOR_TURN_EFFECT_KIND:
                matched.append(str(row["attempt_id"]))
    return matched


def _attempt_ids_for_effect_kind(tick, run_id: str, effect_kind: str) -> list[str]:
    matched: list[str] = []
    with tick.store.begin_read() as conn:
        rows = conn.execute(
            "SELECT attempt_id, dispatch_id FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        for row in rows:
            dispatch = tick.store.get_effect_by_dispatch_id(conn, str(row["dispatch_id"]))
            if dispatch is not None and str(dispatch["effect_kind"]) == effect_kind:
                matched.append(str(row["attempt_id"]))
    return matched


def _wait_until_ingested(tick, attempt_id: str, *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with tick.store.begin_read() as conn:
            row = tick.store.get_attempt_by_id(conn, attempt_id)
            if row is not None and int(row["ingested"]) == 1:
                return
        run_tick_once(tick)
        time.sleep(0.02)
    pytest.fail(f"attempt {attempt_id} was not ingested")


def _drive_live_output_while_child_blocked(
    *,
    tick,
    backend,
    run_id: str,
    release: Path,
    attempt_ids: Callable[[FakeAgentProcessBackend], list[str]],
    stream: str,
    sentinel: str,
    release_on_sentinel: bool = True,
) -> str:
    tick_error: list[BaseException] = []
    stop_ticks = threading.Event()

    def _drive_ticks() -> None:
        try:
            while not stop_ticks.is_set():
                run_tick_once(tick)
                time.sleep(0.02)
        except BaseException as exc:
            tick_error.append(exc)
        finally:
            stop_ticks.set()

    driver = threading.Thread(target=_drive_ticks, daemon=True)
    driver.start()
    attempt_id: str | None = None
    seen_live = False
    deadline = time.monotonic() + 45.0
    try:
        while time.monotonic() < deadline:
            launches = attempt_ids(backend)
            if launches:
                attempt_id = launches[-1]
                code, payload, _ = _invoke(
                    [
                        "integration",
                        "run",
                        "output",
                        run_id,
                        "--attempt",
                        attempt_id,
                        "--stream",
                        stream,
                        "--output",
                        "json",
                    ]
                )
                if code == 0 and payload.get("ok"):
                    data = payload["data"]
                    if data.get("available"):
                        decoded = base64.b64decode(str(data["contentBase64"])).decode(
                            "utf-8", errors="replace"
                        )
                        if sentinel in decoded:
                            seen_live = True
                            assert data["complete"] is False
                            if release_on_sentinel:
                                release.write_text("go", encoding="utf-8")
                            break
            time.sleep(0.05)
    finally:
        stop_ticks.set()
        if release_on_sentinel and not release.is_file():
            release.write_text("go", encoding="utf-8")
        driver.join(timeout=10.0)
        assert not driver.is_alive()
    if tick_error:
        raise tick_error[0]
    assert attempt_id is not None
    assert seen_live
    return attempt_id


def test_c03_live_cursor_stdout_visible_before_child_release(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    release = tmp_path / "cursor-release"
    monkeypatch.setenv("FAKE_AGENT_CHILD_RELEASE_FILE", str(release))
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)

    def _cursor_turns(backend: FakeAgentProcessBackend) -> list[str]:
        return cursor_turn_attempt_ids(
            backend, artifact_root=scheduler_paths["artifact_root"], run_id=run_id
        )

    _drive_live_output_while_child_blocked(
        tick=tick,
        backend=backend,
        run_id=run_id,
        release=release,
        attempt_ids=_cursor_turns,
        stream="stdout",
        sentinel=CHILD_SENTINEL,
    )


def test_c01_cursor_child_stdout_after_turn_complete(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "awaiting_codex_review":
                break
    attempt_id = cursor_turn_attempt_ids(
        backend, artifact_root=scheduler_paths["artifact_root"], run_id=run_id
    )[-1]
    code, payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["complete"] is True
    decoded = base64.b64decode(str(data["contentBase64"])).decode("utf-8")
    assert CHILD_SENTINEL in decoded


def test_c03_live_cursor_stderr_visible_before_child_release(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stderr_sentinel = "CURSOR-STDERR-SENTINEL-21-5"
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_CHILD_STDERR_SENTINEL", stderr_sentinel)
    release = tmp_path / "cursor-stderr-release"
    monkeypatch.setenv("FAKE_AGENT_CHILD_RELEASE_FILE", str(release))
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    _drive_live_output_while_child_blocked(
        tick=tick,
        backend=backend,
        run_id=run_id,
        release=release,
        attempt_ids=lambda backend: cursor_turn_attempt_ids(
            backend, artifact_root=scheduler_paths["artifact_root"], run_id=run_id
        ),
        stream="stderr",
        sentinel=stderr_sentinel,
    )


def test_c03_live_codex_stderr_visible_before_child_release(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stderr_sentinel = "CODEX-STDERR-SENTINEL-21-5"
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDERR_SENTINEL", stderr_sentinel)
    release = tmp_path / "codex-stderr-release"
    monkeypatch.setenv("FAKE_CODEX_CHILD_RELEASE_FILE", str(release))
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(40):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "awaiting_codex_review":
                break
    _drive_live_output_while_child_blocked(
        tick=tick,
        backend=backend,
        run_id=run_id,
        release=release,
        attempt_ids=codex_runner_attempt_ids,
        stream="stderr",
        sentinel=stderr_sentinel,
    )


def test_f06_cli_rejects_broken_completion_envelope(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    (run_root / "attempts" / attempt_id / "result.json").write_bytes(b"not-an-envelope")
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in stderr


def test_f09_cli_rejects_escaping_invocation_evidence_symlink(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    evidence = run_root / "attempts" / attempt_id / "invocation-evidence.json"
    payload = evidence.read_bytes()
    evidence.unlink()
    outside = tmp_path / "outside-evidence.json"
    outside.write_bytes(payload)
    evidence.symlink_to(outside)
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in stderr


def test_c01_cross_run_attempt_ownership_denied(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_a = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_a, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_a)
            if state.kind == "completed":
                break
    attempt_a = codex_runner_attempt_ids(backend)[0]
    run_b = submit_sample_run(
        git_repo,
        scheduler_paths,
        prompt_text="Second run for cross-run output denial.\n",
    )
    assert run_a != run_b
    code, payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_b,
            "--attempt",
            attempt_a,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert payload["ok"] is False


def test_c06_privacy_sentinel_not_in_reviews_or_history(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", SECRET_SENTINEL)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    for args in (
        ["integration", "run", "reviews", run_id, "--output", "json"],
        ["integration", "run", "history", run_id, "--output", "json"],
    ):
        code, payload, stderr = _invoke(args)
        assert code == 0
        blob = json.dumps(payload) + stderr
        assert SECRET_SENTINEL not in blob


def test_c06_privacy_sentinel_not_in_default_inspect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", SECRET_SENTINEL)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    code, payload, stderr = _invoke(["integration", "run", "inspect", run_id, "--output", "json"])
    assert code == 0
    blob = json.dumps(payload) + stderr
    assert SECRET_SENTINEL not in blob


def test_c06_privacy_sentinel_not_in_output_integrity_error(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", SECRET_SENTINEL)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    attempt_id = codex_runner_attempt_ids(backend)[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    (run_root / "attempts" / attempt_id / "result.json").write_bytes(b"broken")
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in json.dumps(body) + stderr


def test_f03_cancelled_cursor_output_pending_then_complete_after_reconciliation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = tmp_path / "abort-cursor-release"
    child_ready = tmp_path / "abort-cursor-child-ready"
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    monkeypatch.setenv("FAKE_AGENT_CHILD_READY_FILE", str(child_ready))
    monkeypatch.setenv("FAKE_AGENT_CHILD_RELEASE_FILE", str(release))
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = DetachedCursorChildBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=0,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    tick_error: list[BaseException] = []
    stop_ticks = threading.Event()

    def _drive_ticks() -> None:
        try:
            while not stop_ticks.is_set():
                run_tick_once(tick)
                time.sleep(0.02)
        except BaseException as exc:
            tick_error.append(exc)

    driver = threading.Thread(target=_drive_ticks, daemon=True)
    driver.start()
    attempt_id: str | None = None
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if not child_ready.is_file():
                time.sleep(0.05)
                continue
            turns = cursor_turn_attempt_ids(
                backend, artifact_root=scheduler_paths["artifact_root"], run_id=run_id
            )
            if turns:
                attempt_id = turns[-1]
                break
            time.sleep(0.05)
        assert attempt_id is not None
        assert child_ready.is_file()
        abort = scheduler_abort_run(run_id, db_path=scheduler_paths["db_path"], backend=backend)
        assert abort.abort_persisted is True
        pending_code, pending_payload, _ = _invoke(
            [
                "integration",
                "run",
                "output",
                run_id,
                "--attempt",
                attempt_id,
                "--stream",
                "stdout",
                "--output",
                "json",
            ]
        )
        assert pending_code == 0
        pending = pending_payload["data"]
        assert pending["processState"] == "cancelled"
        assert pending["complete"] is False
        assert pending["hasMore"] is False
        pending_decoded = base64.b64decode(str(pending["contentBase64"])).decode("utf-8")
        assert CHILD_SENTINEL in pending_decoded
        with tick.store.begin_read() as conn:
            row = tick.store.get_attempt_by_id(conn, attempt_id)
            assert row is not None
            assert str(row["status"]) == "cancelled"
            assert int(row["ingested"]) == 0
        release.write_text("go", encoding="utf-8")
        stop_ticks.set()
        driver.join(timeout=30.0)
        assert not driver.is_alive()
        backend.set_scenario(
            attempt_id,
            FakeAttemptScenario(active_ticks=0, stays_active_after_terminate=False, exit_code=0),
        )
        _wait_until_ingested(tick, attempt_id)
        with tick.store.begin_read() as conn:
            row = tick.store.get_attempt_by_id(conn, attempt_id)
            assert row is not None
            assert int(row["ingested"]) == 1
        final_code, final_payload, _ = _invoke(
            [
                "integration",
                "run",
                "output",
                run_id,
                "--attempt",
                attempt_id,
                "--stream",
                "stdout",
                "--output",
                "json",
            ]
        )
        assert final_code == 0
        final = final_payload["data"]
        assert final["available"] is True
        assert final["complete"] is True
        assert final["hasMore"] is False
        assert final["nextOffset"] is None
        final_decoded = base64.b64decode(str(final["contentBase64"])).decode("utf-8")
        assert CHILD_SENTINEL in final_decoded
        assert final["byteOffset"] == 0
    finally:
        stop_ticks.set()
        if not release.is_file():
            release.write_text("go", encoding="utf-8")
        if driver.is_alive():
            driver.join(timeout=15.0)
    if tick_error:
        raise tick_error[0]


def test_c01_same_iteration_two_cursor_attempts_isolated(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from tests.integration.test_cursor_timeout_retry import ShortTimeoutBackend

    from ai_dev_loop.scheduler.application.cursor_timeout_retry import CursorTimeoutRetryService

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    first_marker = "CURSOR-ATTEMPT-ONE-ISOLATION-21-5"
    second_marker = "CURSOR-ATTEMPT-TWO-ISOLATION-21-5"
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "sleep,success")
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "30")
    monkeypatch.setenv("FAKE_AGENT_CHILD_STDOUT_SENTINELS", f"{first_marker},{second_marker}")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = ShortTimeoutBackend(default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0))
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    timeout_attempt: str | None = None
    for _ in range(60):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if getattr(state, "cursor", None) and state.cursor.timeout_attempt_id:
                timeout_attempt = str(state.cursor.timeout_attempt_id)
                break
    assert timeout_attempt is not None
    retry_service = CursorTimeoutRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: datetime(2026, 9, 19, 12, tzinfo=UTC),
    )
    assert retry_service.retry(run_id).changed
    for _ in range(120):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "awaiting_codex_review":
                break
    attempt_ids = _cursor_run_turn_attempt_ids(tick, run_id)
    assert len(attempt_ids) >= 2
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    iterations = {
        json.loads((run_root / invocation_evidence_rel(aid)).read_text(encoding="utf-8"))[
            "iteration"
        ]
        for aid in attempt_ids
    }
    assert iterations == {1}
    first_id, second_id = attempt_ids[0], attempt_ids[1]
    first_code, first_payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            first_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    second_code, second_payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            second_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert first_code == 0 and first_payload.get("ok")
    assert second_code == 0 and second_payload.get("ok")
    first_data = first_payload["data"]
    second_data = second_payload["data"]
    assert first_data["available"] is True
    assert second_data["available"] is True
    first_decoded = base64.b64decode(str(first_data["contentBase64"])).decode("utf-8")
    second_decoded = base64.b64decode(str(second_data["contentBase64"])).decode("utf-8")
    assert first_marker in first_decoded
    assert second_marker in second_decoded
    assert second_marker not in first_decoded
    assert first_marker not in second_decoded


def test_f06_cli_create_chat_unbound_is_not_captured(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
    from ai_dev_loop.scheduler.application.attempt_envelope import (
        attempt_result_rel,
        attempt_stderr_rel,
        attempt_stdout_rel,
        build_result_envelope,
        envelope_sha256,
        sha256_file,
    )

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    create_ids: list[str] = []
    for _ in range(40):
        run_tick_once(tick)
        create_ids = _attempt_ids_for_effect_kind(tick, run_id, CREATE_CHAT_EFFECT_KIND)
        if create_ids:
            with tick.store.begin_read() as conn:
                row = tick.store.get_attempt_by_id(conn, create_ids[0])
                if row and row["completion_envelope_sha256"]:
                    break
    assert create_ids
    attempt_id = create_ids[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = run_root / stdout_rel
    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    outcome.pop("create_chat_stdout_sha256", None)
    outcome.pop("create_chat_stderr_sha256", None)
    outcome.pop("create_chat_stdout_artifact_path", None)
    outcome.pop("create_chat_stderr_artifact_path", None)
    stdout_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")
    with tick.store.begin_read() as conn:
        row = tick.store.get_attempt_by_id(conn, attempt_id)
        unit_identity = str(row["unit_identity"])
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    digest = envelope_sha256(envelope)
    with tick.store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_attempts SET completion_envelope_sha256 = ? WHERE attempt_id = ?",
            (digest, attempt_id),
        )
    code, payload, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert payload["data"]["reason"] == "not_captured"


def test_f06_cli_create_chat_bound_deletion_is_data_integrity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    create_ids: list[str] = []
    for _ in range(40):
        run_tick_once(tick)
        create_ids = _attempt_ids_for_effect_kind(tick, run_id, CREATE_CHAT_EFFECT_KIND)
        if create_ids:
            with tick.store.begin_read() as conn:
                row = tick.store.get_attempt_by_id(conn, create_ids[0])
                if row and row["completion_envelope_sha256"]:
                    break
    attempt_id = create_ids[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    capture = run_root / CREATE_CHAT_STDOUT_REL
    assert capture.is_file()
    capture.unlink()
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in stderr


def test_f06_cli_create_chat_symlink_capture_is_data_integrity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    create_ids: list[str] = []
    for _ in range(40):
        run_tick_once(tick)
        create_ids = _attempt_ids_for_effect_kind(tick, run_id, CREATE_CHAT_EFFECT_KIND)
        if create_ids:
            with tick.store.begin_read() as conn:
                row = tick.store.get_attempt_by_id(conn, create_ids[0])
                if row and row["completion_envelope_sha256"]:
                    break
    attempt_id = create_ids[0]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    capture = run_root / CREATE_CHAT_STDOUT_REL
    payload = capture.read_bytes()
    capture.unlink()
    outside = tmp_path / "outside-chat.txt"
    outside.write_bytes(payload)
    capture.symlink_to(outside)
    code, body, _ = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"


def test_f09_cli_rejects_escaping_cursor_prompt_symlink(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    for _ in range(80):
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "awaiting_codex_review":
                break
    attempt_id = cursor_turn_attempt_ids(
        backend, artifact_root=scheduler_paths["artifact_root"], run_id=run_id
    )[-1]
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel

    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    prompt_rel = str(evidence["prompt_path"])
    prompt_path = run_root / prompt_rel
    prompt_bytes = prompt_path.read_bytes()
    prompt_path.unlink()
    outside = tmp_path / "outside-prompt.txt"
    outside.write_bytes(prompt_bytes)
    prompt_path.symlink_to(outside)
    code, body, stderr = _invoke(
        [
            "integration",
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            "stdout",
            "--output",
            "json",
        ]
    )
    assert code == 5
    assert body["error"]["code"] == "DATA_INTEGRITY"
    assert SECRET_SENTINEL not in stderr
