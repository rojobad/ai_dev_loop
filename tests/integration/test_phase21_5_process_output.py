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
    make_tick_service,
    run_tick_once,
    submit_sample_run,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.paths import SENSITIVE_FILE_MODE
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
def scheduler_paths(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
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
    release = tmp_path / "codex-release"
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
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDOUT_SENTINEL", CHILD_SENTINEL)
    monkeypatch.setenv("FAKE_CODEX_CHILD_RELEASE_FILE", str(release))
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
    release = tmp_path / "codex-stderr-release"
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
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDERR_SENTINEL", stderr_sentinel)
    monkeypatch.setenv("FAKE_CODEX_CHILD_RELEASE_FILE", str(release))
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
