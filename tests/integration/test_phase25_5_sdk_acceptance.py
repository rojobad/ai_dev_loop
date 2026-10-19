"""Phase 25.5 both-provider production-path acceptance with fake transports."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase25_2_sdk_workflow import (
    OwnedProcessGroupBackend,
    _install_fake_sdk,
    _wait_for_path,
)
from tests.integration.test_phase25_3_sdk_recovery import (
    _bind_fake_sdk_run,
    _submit_sdk_run,
    _tick_until_kind,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _legacy_block_instead_of_retry,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.integration_api.dashboard_service import default_dashboard_read_service
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.token_usage_query import TokenUsageReadService
from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_IDS = itertools.count(1)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True)


def _install_fake_credential(isolated_xdg: Path, name: str) -> None:
    root = isolated_xdg / "config/ai_dev_loop/credentials"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    root.chmod(0o700)
    credential = root / name
    credential.write_text("phase25-5-fake-credential", encoding="utf-8")
    credential.chmod(0o600)


def _submission(
    repo: Path, db: Path, artifacts: Path, *, short_name: str, max_reviews: int | None = None
) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(
            SubmitOptions(
                short_name=short_name,
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts,
                max_review_iterations=max_reviews,
            )
        ).run_id


def _drive_run(tick: TickService, store: SqliteSchedulerStore, run_id: str) -> object:
    for _ in range(100):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "completed":
            return state
        assert state.kind != "blocked", getattr(state, "block_reason_summary", "")
    raise AssertionError("run did not complete")


def _run_checked(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 600,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {argv!r}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def test_sdk_blocked_review_successor_keeps_store_and_can_send_later_correction(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "0")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    run_id, _, artifacts_root, store, tick = _submit_sdk_run(
        git_repo, isolated_xdg, monkeypatch, tmp_path, short_name="SDK review recovery"
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    source = _tick_until_kind(tick, store, run_id, "blocked")
    artifacts = ProtectedArtifactStore(artifacts_root)
    result = ReviewRetryService(store, artifacts).retry(run_id)
    successor = result.run_id
    assert successor != run_id
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    # Start a new fake sequence after the failed bootstrap.
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(tmp_path / "successor-reviews"))
    completed = _drive_run(tick, store, successor)
    assert completed.cursor.iteration == 2
    assert completed.cursor.sdk.conversation_owner_run_id == run_id
    assert completed.codex.reviewer_session_id is not None
    with store.begin_read() as conn:
        unchanged, _, _ = store.load_validated_snapshot(conn, run_id)
    assert unchanged == source
    assert artifacts.read_verified_bytes(
        successor,
        "cursor/sdk-identity.json",
        expected_sha256=completed.cursor.sdk.identity_artifact_sha256,
    ) == artifacts.read_verified_bytes(
        run_id,
        "cursor/sdk-identity.json",
        expected_sha256=completed.cursor.sdk.identity_artifact_sha256,
    )


def test_sdk_native_replay_can_recover_blocked_review_without_resending_prompt(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    backend = OwnedProcessGroupBackend()
    markers = tmp_path / "review-recovery-native-replay"
    send_counter = tmp_path / "review-recovery-native-replay-sends"
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    run_id, _, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK replay then review recovery",
        backend=backend,
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)

    source_attempt = ""
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, run_id)
            if active is not None and int(active["iteration"] or 0) == 1:
                source_attempt = str(active["attempt_id"])
        if markers.joinpath("tree.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "tree.ready")
    assert source_attempt and send_counter.read_text(encoding="utf-8") == "1"
    worker = backend.processes[source_attempt]
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=3)
    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_TREE")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")

    source = None
    for _ in range(240):
        tick.run_once()
        with store.begin_read() as conn:
            source, _, _ = store.load_validated_snapshot(conn, run_id)
        if source.kind == "blocked":
            break
        time.sleep(0.01)
    assert source is not None and source.kind == "blocked"
    with store.begin_read() as conn:
        relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=source_attempt
        )
    assert relation is not None and relation["status"] == "ready"
    assert send_counter.read_text(encoding="utf-8") == "1"

    artifacts = ProtectedArtifactStore(artifacts_root)
    successor = ReviewRetryService(store, artifacts).retry(run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    completed = None
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            completed, _, _ = store.load_validated_snapshot(conn, successor)
        if completed.kind == "completed":
            break
        time.sleep(0.01)
    assert completed is not None and completed.kind == "completed"
    assert completed.cursor.iteration == 1
    assert completed.cursor.sdk.conversation_owner_run_id == run_id
    assert send_counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        unchanged, _, _ = store.load_validated_snapshot(conn, run_id)
    assert unchanged == source


def test_sdk_lost_send_response_replays_unique_run_then_recovers_review_without_resend(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    send_counter = tmp_path / "review-recovery-lost-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_UNCERTAIN", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_DISCOVER_UNIQUE_RUN", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    run_id, _, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK lost send then review recovery",
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)

    source = _tick_until_kind(tick, store, run_id, "blocked", limit=240)
    assert send_counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        relation = conn.execute(
            """
            SELECT * FROM scheduler_cursor_sdk_reconciliations
            WHERE run_id=? AND status='ready'
            """,
            (run_id,),
        ).fetchone()
    assert relation is not None

    artifacts = ProtectedArtifactStore(artifacts_root)
    reconciliation = json.loads(
        artifacts.read_verified_bytes(
            run_id,
            str(relation["result_artifact_path"]),
            expected_sha256=str(relation["result_sha256"]),
        )
    )
    assert reconciliation["outcome"] == "terminal_recovered"
    assert reconciliation["candidate_run_ids"] == [reconciliation["native_run_id"]]
    journal = artifacts.read_verified_bytes(
        run_id,
        reconciliation["source_journal_path"],
        expected_sha256=reconciliation["source_journal_sha256"],
    )
    assert [json.loads(line)["kind"] for line in journal.splitlines()] == [
        "run_inventory_baseline",
        "send_intent",
        "terminal",
    ]
    successor = ReviewRetryService(store, artifacts).retry(run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    completed = _tick_until_kind(tick, store, successor, "completed", limit=180)
    assert completed.cursor.iteration == 1
    assert completed.cursor.sdk.conversation_owner_run_id == run_id
    assert send_counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        unchanged, _, _ = store.load_validated_snapshot(conn, run_id)
    assert unchanged == source


@pytest.mark.parametrize("max_reviews", [None, 1])
def test_exact_source_sdk_config_runs_initial_correction_and_read_only_views(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    max_reviews: int | None,
) -> None:
    del fake_clis
    (git_repo / "ai_dev_loop.yaml").write_bytes((REPOSITORY_ROOT / "ai_dev_loop.yaml").read_bytes())
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _commit(git_repo, "phase25.5 exact sdk config")
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    fake_sdk = tmp_path / "fake-sdk"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _install_fake_credential(isolated_xdg, "cursor-default")
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
        "019abc00-1111-4222-8333-666666666666",
    )
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_USAGE_JSON",
        json.dumps(
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 3,
                "cache_write_tokens": 0,
                "total_tokens": 21,
                "reasoning_tokens": 2,
            }
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 2},
                {"input_tokens": 6, "cached_input_tokens": 2, "output_tokens": 3},
            ]
        ),
    )

    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts = state_root / "artifacts"
    run_id = _submission(
        git_repo, db, artifacts, short_name="SDK cutover acceptance", max_reviews=max_reviews
    )
    run_root = ProtectedArtifactStore(artifacts).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(run_root / "cursor/sdk-identity.json"))
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 18, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase25-5-sdk-{next(_ATTEMPT_IDS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_IDS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    if max_reviews == 1:
        exhausted = _tick_until_kind(tick, store, run_id, "max_iterations_reached")
        sdk = exhausted.cursor.sdk
        assert exhausted.cursor.iteration == 1
        ReviewBudgetExtendService(
            store,
            ProtectedArtifactStore(artifacts),
            now_factory=lambda: datetime(2026, 10, 18, 12, tzinfo=UTC),
        ).extend(run_id, target_total=2)
    state = _drive_run(tick, store, run_id)
    if max_reviews == 1:
        assert state.cursor.sdk.agent_id == sdk.agent_id
        assert state.context.workflow.max_review_iterations == 1

    assert state.context.schema_version == 7
    assert state.context.cursor.runtime == "sdk-local"
    assert state.context.cursor.model == "grok-4.7"
    assert state.context.cursor.parameters == {
        "context": "256k",
        "fast": False,
        "reasoning_effort": "high",
    }
    assert state.cursor.iteration == 2
    assert state.cursor.chat_id is None
    assert state.cursor.sdk is not None
    assert state.codex.reviewer_session_id == "019abc00-1111-4222-8333-666666666666"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
        == baseline
    )
    assert subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True
    ).strip()

    database_before = db.read_bytes()
    runs = IntegrationRunReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts
    ).list_runs(limit=20)
    assert [(item.run_id, item.short_name, item.implementer_provider) for item in runs.items] == [
        (run_id, "SDK cutover acceptance", "cursor")
    ]
    usage = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts
    ).run_report(run_id)
    assert usage.direct.coverage == "complete"
    assert usage.direct.known_total_tokens == 58
    assert {
        (item.provider, item.role, item.stage): item.known_total_tokens
        for item in usage.direct.breakdowns
    } == {
        ("cursor", "implementer", "initial"): 21,
        ("cursor", "implementer", "correction"): 21,
        ("codex", "reviewer", "review"): 16,
    }
    assert {(item.provider, item.role) for item in usage.direct.breakdowns} == {
        ("cursor", "implementer"),
        ("codex", "reviewer"),
    }
    submitted = datetime.fromisoformat(state.submitted_at.replace("Z", "+00:00"))
    dashboard = default_dashboard_read_service(db_path=db).runs(
        from_value=(submitted - timedelta(days=1)).isoformat(),
        to_value=(submitted + timedelta(days=1)).isoformat(),
        time_zone="UTC",
        limit=20,
    )
    assert [
        (item.run_id, item.short_name, item.implementer_provider) for item in dashboard.items
    ] == [(run_id, "SDK cutover acceptance", "cursor")]
    assert db.read_bytes() == database_before
    assert "phase25-5-fake-credential" not in json.dumps(state.model_dump(mode="json"))


def test_v1_config_codex_initial_and_correction_need_no_cursor_cli_or_sdk(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high", "xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "codex-v1.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "019def00-0000-0000-0000-000000000551")
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", "019def00-0000-0000-0000-000000000552")
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="Codex v1 preserved",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                implementer_provider="codex",
                implementer_command="codex",
                implementer_model="gpt-5.6-sol",
                implementer_reasoning_effort="xhigh",
                implementer_timeout_minutes=90,
                db_path=db,
                artifact_root=artifacts,
            )
        ).run_id
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts),
        BoundedGitAdmissionPort(),
        now_factory=lambda: datetime(2026, 10, 18, 13, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase25-5-codex-{next(_ATTEMPT_IDS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_IDS):032x}",
        attempt_backend=FakeAgentProcessBackend(),
    )
    state = _drive_run(tick, store, run_id)

    assert state.context.schema_version == 5
    assert state.context.implementer.provider == "codex"
    assert state.context.cursor.command == "agent"
    assert state.cursor.iteration == 2
    assert state.codex.implementer_session_id == "019def00-0000-0000-0000-000000000551"
    assert state.codex.reviewer_session_id == "019def00-0000-0000-0000-000000000552"
    assert state.codex.implementer_session_id != state.codex.reviewer_session_id
    assert not fake_clis["agent_log"].exists()
    assert "resume" in fake_clis["codex_log"].read_text(encoding="utf-8")


def test_python311_base_wheel_runs_fake_codex_flow_without_cursor_dependency(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python311 = shutil.which("python3.11")
    uv = shutil.which("uv")
    assert python311 is not None, "Phase 25.5 installed acceptance requires python3.11"
    assert uv is not None, "Phase 25.5 installed acceptance requires uv"
    (fake_clis["bin_dir"] / "agent").unlink()
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "installed-codex.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_SESSION_ID",
        "019def00-0000-0000-0004-000000000551",
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEWER_SESSION_ID",
        "019def00-0000-0000-0004-000000000552",
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    build_env = os.environ.copy()
    build_env.pop("PYTHONPATH", None)
    package_root = tmp_path / "installed-package"
    dist = package_root / "dist"
    package_root.mkdir()
    dist.mkdir()
    for name in ("home", "cache", "tmp"):
        (package_root / name).mkdir()
    build_env.update(
        {
            "HOME": str(package_root / "home"),
            "UV_CACHE_DIR": str(package_root / "cache"),
            "TMPDIR": str(package_root / "tmp"),
            "TMP": str(package_root / "tmp"),
            "TEMP": str(package_root / "tmp"),
        }
    )
    _run_checked(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=REPOSITORY_ROOT,
        env=build_env,
    )
    wheels = list(dist.glob("ai_dev_loop-*.whl"))
    assert len(wheels) == 1
    venv = package_root / "venv"
    _run_checked([uv, "venv", "--python", python311, str(venv)], cwd=package_root, env=build_env)
    _run_checked(
        [uv, "pip", "install", "--python", str(venv / "bin/python"), str(wheels[0])],
        cwd=package_root,
        env=build_env,
    )

    installed_script = r"""
import importlib.util
import json
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

assert importlib.util.find_spec("cursor_sdk") is None
repo = Path(sys.argv[1])
state_root = Path(sys.argv[2])
db = state_root / "engine.sqlite3"
artifacts = state_root / "artifacts"
prompt_path = repo / "docs/plans/prompt_sample-plan.txt"
with patch("sys.stdin", StringIO(prompt_path.read_text(encoding="utf-8"))):
    result = submit_run(SubmitOptions(
        short_name="Installed Codex without Cursor",
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        codex_review_model="gpt-6.1-sol",
        codex_review_reasoning_effort="high",
        implementer_provider="codex",
        implementer_command="codex",
        implementer_model="gpt-5.6-sol",
        implementer_reasoning_effort="xhigh",
        implementer_timeout_minutes=90,
        db_path=db,
        artifact_root=artifacts,
    ))
start_run(result.run_id, db_path=db)
store = SqliteSchedulerStore(db)
tick = TickService(
    store,
    ProtectedArtifactStore(artifacts),
    BoundedGitAdmissionPort(),
    now_factory=lambda: datetime(2026, 10, 18, 15, tzinfo=UTC),
    tick_owner_factory=lambda: "phase25-5-installed-owner",
    attempt_backend=FakeAgentProcessBackend(),
)
for _ in range(100):
    tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, result.run_id)
    if state.kind == "completed":
        break
    assert state.kind != "blocked", getattr(state, "block_reason_summary", "")
else:
    raise AssertionError("installed Codex run did not complete")
assert state.context.schema_version == 5
assert state.context.implementer.provider == "codex"
assert state.cursor.iteration == 2
assert state.codex.implementer_session_id != state.codex.reviewer_session_id
print(json.dumps({
    "kind": state.kind,
    "implementer": state.context.implementer.provider,
    "iterations": state.cursor.iteration,
}))
"""
    installed_env = os.environ.copy()
    installed_env.pop("PYTHONPATH", None)
    installed_env.update(
        {
            "XDG_CONFIG_HOME": str(isolated_xdg / "config"),
            "XDG_STATE_HOME": str(isolated_xdg / "state"),
            "XDG_CACHE_HOME": str(isolated_xdg / "cache"),
            "TMPDIR": str(package_root / "tmp"),
            "TMP": str(package_root / "tmp"),
            "TEMP": str(package_root / "tmp"),
        }
    )
    installed = _run_checked(
        [
            str(venv / "bin/python"),
            "-c",
            installed_script,
            str(git_repo),
            str(isolated_xdg / "state/installed-ai-dev-loop"),
        ],
        cwd=git_repo,
        env=installed_env,
    )
    assert json.loads(installed.stdout) == {
        "implementer": "codex",
        "iterations": 2,
        "kind": "completed",
    }
    assert not fake_clis["agent_log"].exists()
    assert (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True
        ).strip()
        == "installed-codex.txt"
    )
    assert not subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=git_repo, text=True
    ).strip()


@pytest.mark.parametrize("providers", [("cursor", "codex"), ("codex", "cursor")])
def test_named_manifest_v4_mixed_provider_sequence_preserves_checkpoint_and_final_staging(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    providers: tuple[str, str],
) -> None:
    del fake_clis
    (git_repo / "ai_dev_loop.yaml").write_bytes((REPOSITORY_ROOT / "ai_dev_loop.yaml").read_bytes())
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _commit(git_repo, "phase25.5 mixed baseline")
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()
    fake_sdk = tmp_path / "fake-sdk-mixed"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _install_fake_credential(isolated_xdg, "cursor-default")
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")

    cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    phases = []
    for ordinal, provider in enumerate(providers, start=1):
        phases.append(
            {
                "name": f"phase-{ordinal}",
                "short_name": f"Mixed leaf {ordinal}",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "commit_message": "accepted phase one" if ordinal == 1 else None,
                "cursor": cursor,
                "codex": {
                    "review_model": "gpt-6.1-sol",
                    "review_reasoning_effort": "high",
                },
                "implementer": (
                    {"provider": "cursor"}
                    if provider == "cursor"
                    else {
                        "provider": "codex",
                        "command": "codex",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "xhigh",
                        "timeout_minutes": 90,
                    }
                ),
            }
        )
    manifest = tmp_path / "mixed-v4.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": f"mixed-{'-'.join(providers)}",
                "short_name": f"Mixed {' then '.join(providers)}",
                "phases": phases,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    prepared = prepare_sequence(
        SequencePrepareOptions(manifest_path=manifest, repo_path=git_repo, **paths)
    )
    start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    store = SqliteSchedulerStore(paths["db_path"])
    tick = TickService(
        store,
        ProtectedArtifactStore(paths["artifact_root"]),
        BoundedGitAdmissionPort(),
        now_factory=lambda: datetime(2026, 10, 18, 14, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase25-5-mixed-{next(_ATTEMPT_IDS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_IDS):032x}",
        attempt_backend=FakeAgentProcessBackend(),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(180):
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if isinstance(sequence, AwaitingFinalizationSequenceState):
            break
        ordinal = sequence.current_ordinal
        monkeypatch.setenv(
            "FAKE_CODEX_IMPLEMENTER_SESSION_ID",
            f"019def00-0000-0000-0002-{ordinal:012x}",
        )
        monkeypatch.setenv(
            "FAKE_CODEX_REVIEWER_SESSION_ID",
            f"019def00-0000-0000-0003-{ordinal:012x}",
        )
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", f"codex-{ordinal}.txt")
        tick.run_once()
    else:
        raise AssertionError("mixed v4 sequence did not reach final staging")

    assert sequence.definition.schema_version == 3
    assert [entry.implementer.provider for entry in sequence.definition.entries] == list(providers)
    assert sequence.definition.entries[0].cursor.runtime == "sdk-local"
    assert sequence.definition.entries[1].cursor.runtime == "sdk-local"
    assert len(sequence.materialized_entries) == 2
    assert (
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{baseline}..HEAD"], cwd=git_repo, text=True
        ).strip()
        == "1"
    )
    assert subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True
    ).strip()
    assert not subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=git_repo, text=True
    ).strip()

    read = IntegrationRunReadService(
        SqliteSchedulerStore.open_readonly(paths["db_path"]),
        artifact_root=paths["artifact_root"],
    ).list_runs(kind="sequence", limit=20)
    by_ordinal = sorted(
        read.items,
        key=lambda item: item.sequence_binding.ordinal if item.sequence_binding is not None else 0,
    )
    assert [item.short_name for item in by_ordinal] == [
        "Mixed leaf 1",
        "Mixed leaf 2",
    ]
    assert [item.sequence_short_name for item in read.items] == [
        f"Mixed {' then '.join(providers)}",
        f"Mixed {' then '.join(providers)}",
    ]
