"""Integration tests for Phase 21.6 Codex capacity command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.schemas import (
    CODEX_CAPACITY_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexAppServerCapacityProbe,
    CodexCapacityReason,
    CodexCapacityStatus,
)

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_SENTINEL = "INTEGRATION_PHASE21_6_SECRET_SENTINEL"


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


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (stat.st_mode, stat.st_size)
    return snapshot


def test_c06_info_reports_codex_capacity_capability() -> None:
    data = build_integration_info_data()
    assert data.capabilities.codex_capacity is True


def test_c01_cli_available_capacity_with_fake_app_server(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "primary_only")
    code, payload, stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["apiVersion"] == {"major": 1, "minor": 5}
    data = payload["data"]
    assert data["status"] == "available"
    assert data["reason"] is None
    assert data["limits"][0]["usedPercent"] == 10
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr
    validate_integration_instance(data, CODEX_CAPACITY_DATA_SCHEMA)


def test_c01_cli_unavailable_process_exit(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] in {"process_failure", "premature_eof"}
    assert payload["data"]["limits"] == []


def test_c01_cli_malformed_limits_json(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "invalid")
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "malformed_response"


def test_c01_cli_invalid_output_rejected(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, payload, _stderr = _invoke(["integration", "codex-capacity", "--output", "text"])
    assert code == 2
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_c01_subprocess_smoke(
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    env = {
        **dict(os.environ),
        "XDG_STATE_HOME": str(isolated_xdg / "state"),
        "XDG_CONFIG_HOME": str(isolated_xdg / "config"),
        "XDG_CACHE_HOME": str(isolated_xdg / "cache"),
    }
    proc = subprocess.run(
        ["uv", "run", "python", "-m", "ai_dev_loop.cli", "integration", "codex-capacity"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["data"]["status"] == "available"


def test_c02_capacity_cli_does_not_mutate_scheduler_ledger(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import FIXTURE_REPO
    from tests.unit.scheduler.helpers import CONTROLLER_SESSION

    from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run

    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with patch("sys.stdin", StringIO(prompt)):
        submit_run(options)
    db_before = scheduler_paths["db_path"].read_bytes()
    xdg_before = _tree_snapshot(isolated_xdg)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    code, _payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert scheduler_paths["db_path"].read_bytes() == db_before
    assert _tree_snapshot(isolated_xdg) == xdg_before


def test_c02_scheduler_probe_still_uses_frozen_executable(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    probe = CodexAppServerCapacityProbe()
    observation = probe.probe(str(fake_clis["bin_dir"] / "codex"))
    assert observation.status == CodexCapacityStatus.EXHAUSTED


def test_c01_cli_resets_at_unrepresentable_values_normalize_to_null(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = {
        "rateLimitsByLimitId": {
            "default": {
                "primary": {"usedPercent": 12, "resetsAt": 253402300800},
                "secondary": {"usedPercent": 3, "resetsAt": 2**62},
            }
        }
    }
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_LIMITS_JSON", json.dumps(limits))
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    data = payload["data"]
    assert data["status"] == "available"
    assert data["limits"][0]["usedPercent"] == 12
    assert data["limits"][0]["resetsAt"] is None
    assert data["limits"][0]["remainingPercent"] == 88
    assert data["limits"][1]["resetsAt"] is None


def _short_capacity_probe(**kwargs: object) -> CodexAppServerCapacityProbe:
    del kwargs
    return CodexAppServerCapacityProbe(timeout_seconds=1)


def test_c01_cli_timeout_unavailable_envelope(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_STALL_SECONDS", "2")
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.codex_capacity_probe.CodexAppServerCapacityProbe",
        _short_capacity_probe,
    )
    started = time.monotonic()
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    elapsed = time.monotonic() - started
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "timeout"
    assert payload["data"]["limits"] == []
    assert 0.8 <= elapsed < 3.0


def test_c01_cli_cleanup_failure_unavailable(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.codex_capacity_probe.CodexAppServerCapacityProbe",
        _short_capacity_probe,
    )
    with patch(
        "ai_dev_loop.scheduler.application.codex_capacity_probe._process_group_is_alive",
        lambda _pgid: True,
    ):
        code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "cleanup_failure"
    assert payload["data"]["limits"] == []


def test_c01_cli_premature_eof_unavailable(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_EOF_AFTER_INIT", "1")
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "premature_eof"
    assert payload["data"]["limits"] == []


def test_c01_cli_oversized_response_unavailable(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_OVERSIZED", "1")
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "output_limit"
    assert payload["data"]["limits"] == []


def test_c01_timeout_and_cleanup_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hang_script = tmp_path / "codex"
    hang_script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            import time

            if len(sys.argv) >= 2 and sys.argv[1] == "app-server":
                time.sleep(60)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    hang_script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    probe = CodexAppServerCapacityProbe(timeout_seconds=0.2)
    observation = probe.probe(str(hang_script))
    assert observation.status == CodexCapacityStatus.UNAVAILABLE
    assert observation.reason == CodexCapacityReason.TIMEOUT

    eof_script = tmp_path / "codex_eof"
    eof_script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            if len(sys.argv) >= 2 and sys.argv[1] == "app-server":
                sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    eof_script.chmod(0o755)
    observation = CodexAppServerCapacityProbe().probe(str(eof_script))
    assert observation.status == CodexCapacityStatus.UNAVAILABLE
    assert observation.reason == CodexCapacityReason.PREMATURE_EOF


def test_c05_scheduler_waiting_uses_frozen_codex_not_path_default(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    from datetime import UTC, datetime

    from tests.conftest import FIXTURE_REPO
    from tests.unit.scheduler.helpers import CONTROLLER_SESSION
    from tests.unit.scheduler.test_phase19_codex_capacity import _run_until
    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    frozen_marker = tmp_path / "frozen_capacity.txt"
    path_marker = tmp_path / "path_capacity.txt"

    def write_path_probe_script(marker: Path) -> Path:
        script = tmp_path / "codex_path_only.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import sys

                marker = {str(marker)!r}
                if len(sys.argv) >= 2 and sys.argv[1] == "app-server":
                    with open(marker, "a", encoding="utf-8") as handle:
                        handle.write("probe\\n")
                    for raw in sys.stdin:
                        line = raw.strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        method = msg.get("method")
                        if method == "initialize":
                            print(json.dumps({{"id": msg.get("id"), "result": {{}}}}), flush=True)
                        elif method == "initialized":
                            continue
                        elif method == "account/rateLimits/read":
                            print(json.dumps({{
                                "id": msg.get("id"),
                                "result": {{"rateLimitsByLimitId": {{"default": {{"primary": {{"usedPercent": 100}}}}}}}},
                            }}), flush=True)
                            sys.exit(0)
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    frozen_cmd = tmp_path / "codex_frozen"
    shutil.copy2(fake_clis["bin_dir"] / "codex", frozen_cmd)
    frozen_cmd.chmod(0o755)
    path_bin = tmp_path / "pathbin"
    path_bin.mkdir(exist_ok=True)
    path_cmd = write_path_probe_script(path_marker)
    path_cmd.rename(path_bin / "codex")
    monkeypatch.setenv("PATH", f"{path_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_PROBE_TOUCH_FILE", str(frozen_marker))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        codex_command=str(frozen_cmd),
    )
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(options).run_id
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-21-6-frozen-codex",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
    for _ in range(5):
        tick.run_once()
        if frozen_marker.exists():
            break
    assert frozen_marker.read_text(encoding="utf-8").count("probe") >= 1
    assert not path_marker.exists()
    monkeypatch.delenv("FAKE_CODEX_CAPACITY_PROBE_TOUCH_FILE", raising=False)
    code, _payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert path_marker.read_text(encoding="utf-8").count("probe") >= 1


def test_c01_missing_codex_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    code, payload, _stderr = _invoke(["integration", "codex-capacity"])
    assert code == 0
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["reason"] == "process_failure"
