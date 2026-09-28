"""Integration tests for Phase 21.4 review inspection commands."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run

runner = CliRunner()
SECRET_SENTINEL = "INTEGRATION_PHASE21_4_SECRET_SENTINEL"


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


def test_c06_info_reports_review_inspection_capability() -> None:
    data = build_integration_info_data()
    assert data.capabilities.review_inspection is True


def test_c03_reviews_command_requires_existing_run(scheduler_paths: dict[str, Path]) -> None:
    code, payload, stderr = _invoke(
        [
            "integration",
            "run",
            "reviews",
            "missing-run",
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert payload["ok"] is False
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr


def test_c03_review_wrong_run_denied(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    code, payload, _stderr = _invoke(
        [
            "integration",
            "run",
            "review",
            "other-run-id",
            "--attempt",
            "att-nonexistent",
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert payload["error"]["code"] == "NOT_FOUND"


def _submit_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    max_reviews: int = 3,
) -> str:
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
        max_review_iterations=max_reviews,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def test_f09_f08_cli_lists_authenticated_scheduler_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
    counter = itertools.count()

    def _next_attempt_id() -> str:
        return f"att-{next(counter):032x}"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-21-4-{next(counter)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    else:
        pytest.fail("run did not complete")

    code, payload, stderr = _invoke(
        [
            "integration",
            "run",
            "reviews",
            run_id,
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert payload["ok"] is True
    items = payload["data"]["items"]
    assert len(items) >= 1
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr
    attempt_id = items[0]["attemptId"]
    assert items[0]["content"]["prompt"]["available"] is True

    code, detail, _ = _invoke(
        [
            "integration",
            "run",
            "review",
            run_id,
            "--attempt",
            attempt_id,
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert detail["data"]["resultState"] == "valid"
    assert detail["data"]["reviewerSessionRef"] is not None

    code, chunk, _ = _invoke(
        [
            "integration",
            "run",
            "review-content",
            run_id,
            "--attempt",
            attempt_id,
            "--kind",
            "prompt",
            "--offset",
            "0",
            "--limit",
            "4096",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert chunk["data"]["availableBytes"] > 0
    assert chunk["data"]["sha256"]


def test_f08_later_iteration_reviews_and_invalid_json_via_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
    counter = itertools.count()

    def _next_attempt_id() -> str:
        return f"att-{next(counter):032x}"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,invalid_json,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-21-4b-{next(counter)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_codex_review_retry":
                break
    else:
        pytest.fail("expected retryable invalid JSON review")

    code, payload, _ = _invoke(["integration", "run", "reviews", run_id, "--output", "json"])
    assert code == 0
    assert len(payload["data"]["items"]) >= 2
    iterations = {item["iteration"] for item in payload["data"]["items"]}
    assert 1 in iterations
    session_refs = {
        item["reviewerSessionRef"]
        for item in payload["data"]["items"]
        if item["reviewerSessionRef"]
    }
    assert len(session_refs) == 1
    invalid_detail = None
    for item in payload["data"]["items"]:
        code, detail, _ = _invoke(
            [
                "integration",
                "run",
                "review",
                run_id,
                "--attempt",
                item["attemptId"],
                "--output",
                "json",
            ]
        )
        assert code == 0
        if detail["data"]["resultState"] == "invalid":
            invalid_detail = detail["data"]
    assert invalid_detail is not None
    assert invalid_detail["response"] is None
    assert invalid_detail["content"]["response"]["available"] is True


def test_f03_cli_data_integrity_on_corrupt_prompt_evidence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.domain.codex_contract import codex_review_prompt_rel
    from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
    counter = itertools.count()

    def _next_attempt_id() -> str:
        return f"att-{next(counter):032x}"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-f03-{next(counter)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    else:
        pytest.fail("run did not complete")
    with store.begin_read() as conn:
        rows, _ = store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=10)
        attempt_id = str(rows[0]["attempt_id"])
        iteration = int(rows[0]["iteration"])
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    prompt_path.unlink()

    for cmd in (
        ["integration", "run", "reviews", run_id, "--output", "json"],
        [
            "integration",
            "run",
            "review",
            run_id,
            "--attempt",
            attempt_id,
            "--output",
            "json",
        ],
        [
            "integration",
            "run",
            "review-content",
            run_id,
            "--attempt",
            attempt_id,
            "--kind",
            "prompt",
            "--output",
            "json",
        ],
    ):
        code, payload, stderr = _invoke(cmd)
        assert code == 5
        assert payload["error"]["code"] == "DATA_INTEGRITY"
        assert SECRET_SENTINEL not in json.dumps(payload)
        assert SECRET_SENTINEL not in stderr


def test_f12_cli_cancelled_codex_attempt_prompt_without_completion(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
    counter = itertools.count()

    def _next_attempt_id() -> str:
        return f"att-{next(counter):032x}"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-f12-{next(counter)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    else:
        pytest.fail("run did not complete")
    with store.begin_read() as conn:
        rows, _ = store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=10)
        attempt_id = str(rows[0]["attempt_id"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_attempts
               SET status = 'cancelled', completion_envelope_sha256 = NULL
             WHERE attempt_id = ?
            """,
            (attempt_id,),
        )
    code, detail, _ = _invoke(
        [
            "integration",
            "run",
            "review",
            run_id,
            "--attempt",
            attempt_id,
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert detail["data"]["content"]["prompt"]["available"] is True
    assert detail["data"]["resultState"] == "not_produced"
    assert detail["data"]["response"] is None
    code, chunk, _ = _invoke(
        [
            "integration",
            "run",
            "review-content",
            run_id,
            "--attempt",
            attempt_id,
            "--kind",
            "prompt",
            "--offset",
            "0",
            "--limit",
            "8192",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert chunk["data"]["availableBytes"] > 0


def test_f08_fake_codex_witnesses_prompt_before_stdin(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hashlib
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.start import start_run
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.domain.codex_contract import codex_review_prompt_rel
    from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
    counter = itertools.count()
    witness_path = tmp_path / "stdin-sha256.txt"

    def _next_attempt_id() -> str:
        return f"att-{next(counter):032x}"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_STDIN_SHA256_WITNESS", str(witness_path))
    run_id = _submit_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-c01-{next(counter)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
    else:
        pytest.fail("run did not complete")
    assert witness_path.is_file()
    with store.begin_read() as conn:
        rows, _ = store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=5)
        attempt_id = str(rows[0]["attempt_id"])
        iteration = int(rows[0]["iteration"])
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    on_disk = (run_root / codex_review_prompt_rel(iteration, attempt_id)).read_bytes()
    assert witness_path.read_text(encoding="utf-8").strip() == hashlib.sha256(on_disk).hexdigest()
