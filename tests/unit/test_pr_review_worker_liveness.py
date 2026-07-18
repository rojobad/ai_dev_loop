"""Unit coverage for PR-review worker launcher liveness (Phase 15.9)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai_dev_loop.commands.pr_review import (
    _spawn_pr_review_worker,
    probe_pr_review_worker_liveness,
    render_pr_review_status,
)
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.state import (
    GithubPrReviewState,
    RunStatus,
    atomic_write_json,
    load_run_state,
    save_run_state,
)


def _write_launcher(
    run_directory: Path,
    *,
    run_id: str,
    pid: int,
    pid_starttime: int | None = None,
) -> Path:
    path = run_directory / "locks" / "pr-review-worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "worker_token": "deadbeef",
        "pid": pid,
        "started_at": "2026-07-18T12:00:00+00:00",
        "argv_redacted": [
            "python",
            "-m",
            "ai_dev_loop.pr_review_worker",
            run_id,
            "<worker-token>",
        ],
    }
    if pid_starttime is not None:
        payload["pid_starttime"] = pid_starttime
    atomic_write_json(path, payload, sensitive=True)
    return path


def test_probe_absent_without_launcher(tmp_path: Path) -> None:
    result = probe_pr_review_worker_liveness(tmp_path, "run-1")
    assert result.status == "absent"
    assert result.launcher_present is False
    assert result.launcher_safe is True


def test_probe_live_pid_and_spawn_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "demo-20260718T120000Z-1"
    starttime = read_process_starttime(os.getpid())
    assert starttime is not None
    _write_launcher(tmp_path, run_id=run_id, pid=os.getpid(), pid_starttime=starttime)
    result = probe_pr_review_worker_liveness(tmp_path, run_id)
    assert result.status == "live"
    assert result.launcher_safe is True

    def fail_popen(*_a, **_k):
        raise AssertionError("must not spawn when worker is live")

    monkeypatch.setattr("ai_dev_loop.commands.pr_review.subprocess.Popen", fail_popen)
    assert _spawn_pr_review_worker(tmp_path, run_id) is False


def test_probe_stale_allows_single_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "demo-20260718T120000Z-2"
    _write_launcher(tmp_path, run_id=run_id, pid=2_147_000_001, pid_starttime=123)
    result = probe_pr_review_worker_liveness(tmp_path, run_id)
    assert result.status == "stale"
    assert result.launcher_safe is True

    class FakeProc:
        pid = 4242

        def poll(self) -> int | None:
            return None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return ("", "")

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.subprocess.Popen",
        lambda *_a, **_k: FakeProc(),
    )
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.read_process_starttime",
        lambda _pid: 424242,
    )
    assert _spawn_pr_review_worker(tmp_path, run_id) is True
    payload = json.loads((tmp_path / "locks/pr-review-worker.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == run_id
    assert payload["pid"] == 4242
    assert payload["pid_starttime"] == 424242
    assert payload["worker_token"] != "deadbeef"
    assert "<worker-token>" in payload["argv_redacted"]


def test_probe_wrong_run_id_fails_spawn(tmp_path: Path) -> None:
    starttime = read_process_starttime(os.getpid())
    assert starttime is not None
    _write_launcher(
        tmp_path,
        run_id="other-run",
        pid=os.getpid(),
        pid_starttime=starttime,
    )
    result = probe_pr_review_worker_liveness(tmp_path, "selected-run")
    assert result.launcher_safe is False
    assert result.status == "absent"
    with pytest.raises(ValidationError, match="run_id does not match"):
        _spawn_pr_review_worker(tmp_path, "selected-run")


def test_probe_malformed_launcher_fails_spawn(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "pr-review-worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    result = probe_pr_review_worker_liveness(tmp_path, "run-1")
    assert result.launcher_safe is False
    with pytest.raises(ValidationError, match="unreadable or malformed"):
        _spawn_pr_review_worker(tmp_path, "run-1")


def test_probe_ambiguous_pid_fails_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "demo-20260718T120000Z-3"
    _write_launcher(tmp_path, run_id=run_id, pid=999, pid_starttime=1)

    def raise_permission(pid: int, _sig: int) -> None:
        raise PermissionError("ambiguous")

    monkeypatch.setattr("ai_dev_loop.commands.pr_review.os.kill", raise_permission)
    result = probe_pr_review_worker_liveness(tmp_path, run_id)
    assert result.launcher_safe is False
    assert "ambiguous" in (result.detail or "")
    with pytest.raises(ValidationError, match="ambiguous"):
        _spawn_pr_review_worker(tmp_path, run_id)


def test_probe_pid_reuse_mismatch_is_unsafe_not_live(tmp_path: Path) -> None:
    run_id = "demo-20260718T120000Z-reuse"
    _write_launcher(
        tmp_path,
        run_id=run_id,
        pid=os.getpid(),
        pid_starttime=(read_process_starttime(os.getpid()) or 0) + 999_999,
    )
    result = probe_pr_review_worker_liveness(tmp_path, run_id)
    assert result.launcher_safe is False
    assert result.status == "absent"
    assert "identity mismatch" in (result.detail or "")
    with pytest.raises(ValidationError, match="identity mismatch"):
        _spawn_pr_review_worker(tmp_path, run_id)


def test_probe_live_pid_without_starttime_is_unsafe(tmp_path: Path) -> None:
    run_id = "demo-20260718T120000Z-legacy"
    _write_launcher(tmp_path, run_id=run_id, pid=os.getpid(), pid_starttime=None)
    result = probe_pr_review_worker_liveness(tmp_path, run_id)
    assert result.launcher_safe is False
    assert result.status == "absent"
    assert "identity is unavailable" in (result.detail or "")


def test_spawn_terminates_child_when_launcher_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "demo-20260718T120000Z-write-fail"
    cleanup = {"killpg": 0, "communicate": 0}
    fake_proc = MagicMock()
    fake_proc.pid = 424242
    fake_proc.poll.return_value = None
    fake_proc.communicate.return_value = ("", "")

    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.subprocess.Popen",
        lambda *_a, **_k: fake_proc,
    )
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.read_process_starttime",
        lambda _pid: 111,
    )
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.os.getpgid",
        lambda _pid: 424242,
    )

    def fake_killpg(pgid: int, sig: int) -> None:
        cleanup["killpg"] += 1
        fake_proc.poll.return_value = -sig

    def fail_write(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("ai_dev_loop.commands.pr_review.os.killpg", fake_killpg)
    monkeypatch.setattr("ai_dev_loop.commands.pr_review.atomic_write_json", fail_write)

    with pytest.raises(OSError, match="disk full"):
        _spawn_pr_review_worker(tmp_path, run_id)

    assert cleanup["killpg"] >= 1
    fake_proc.communicate.assert_called()
    assert not (tmp_path / "locks/pr-review-worker.json").exists()


def test_spawn_terminates_child_when_starttime_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "demo-20260718T120000Z-no-starttime"
    fake_proc = MagicMock()
    fake_proc.pid = 424243
    fake_proc.poll.return_value = None
    fake_proc.communicate.return_value = ("", "")

    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.subprocess.Popen",
        lambda *_a, **_k: fake_proc,
    )
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.read_process_starttime",
        lambda _pid: None,
    )
    monkeypatch.setattr(
        "ai_dev_loop.commands.pr_review.os.getpgid",
        lambda _pid: 424243,
    )
    killpg_calls: list[int] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append(sig)
        fake_proc.poll.return_value = -sig

    monkeypatch.setattr("ai_dev_loop.commands.pr_review.os.killpg", fake_killpg)

    with pytest.raises(AiDevLoopError, match="process identity"):
        _spawn_pr_review_worker(tmp_path, run_id)

    assert killpg_calls
    fake_proc.communicate.assert_called()
    assert not (tmp_path / "locks/pr-review-worker.json").exists()


def test_status_reports_liveness_without_secrets(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha="c" * 40,
        request_comment_id="12",
        request_marker="ai_dev_loop-pr-review:marker",
        request_created_at="2026-07-18T12:00:00+00:00",
    )
    save_run_state(run_path, state)
    _write_launcher(run_path, run_id=state.run_id, pid=2_147_000_002, pid_starttime=99)

    text = render_pr_review_status(state.run_id, output="text")
    payload = json.loads(render_pr_review_status(state.run_id, output="json"))
    assert payload["worker_liveness"] == "stale"
    assert payload["worker_launcher_safe"] is True
    assert "pr-review resume" in payload["next_safe_action"]
    assert "Worker: stale" in text
    assert "deadbeef" not in text
    assert "deadbeef" not in json.dumps(payload)
    assert str(2_147_000_002) not in text
    assert "pid" not in payload
    assert "pid_starttime" not in json.dumps(payload)
    assert "worker_token" not in json.dumps(payload)
    if state.codex.session_id:
        assert state.codex.session_id not in text
        assert state.codex.session_id not in json.dumps(payload)
