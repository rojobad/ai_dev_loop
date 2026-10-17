"""Phase 27.2 standalone Codex implementer workflow contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort

from ai_dev_loop.scheduler.application.codex_capacity_probe import CodexCapacityProbePort
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

IMPLEMENTER_ID = "019def00-0000-0000-0000-0000000000aa"
REVIEWER_ID = "019def00-0000-0000-0000-0000000000bb"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit(
    repo: Path,
    paths: dict[str, Path],
    *,
    max_reviews: int = 3,
    resubmission_id: str | None = None,
    prompt: str | None = None,
) -> str:
    if prompt is None:
        prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        short_name="Test run",
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        implementer_provider="codex",
        implementer_model="gpt-5.5",
        implementer_reasoning_effort="xhigh",
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        max_review_iterations=max_reviews,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
        resubmission_id=resubmission_id,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def _tick(
    repo: Path,
    paths: dict[str, Path],
    *,
    backend: FakeAgentProcessBackend | None = None,
    owner: str = "tick-phase-27-2",
    now_factory: Callable[[], datetime] | None = None,
    capacity_probe: CodexCapacityProbePort | None = None,
    before_effect_claim: Callable[[], None] | None = None,
    lease_ttl_seconds: int = 30,
) -> TickService:
    effective_now_factory = now_factory or (lambda: datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    return TickService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(repo.resolve())),
        now_factory=effective_now_factory,
        tick_owner_factory=lambda: owner,
        attempt_backend=backend or FakeAgentProcessBackend(),
        codex_capacity_probe=capacity_probe,
        before_effect_claim=before_effect_claim,
        lease_ttl_seconds=lease_ttl_seconds,
    )


def _run_until(tick: TickService, run_id: str, target: str, *, limit: int = 60) -> object:
    for _ in range(limit):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == target:
            return state
        if state.kind == "blocked":
            raise AssertionError(
                f"run blocked: {state.block_reason_kind}: {state.block_reason_summary}"
            )
    raise AssertionError(f"run did not reach {target} after {limit} ticks")


def _effect_kinds(paths: dict[str, Path], run_id: str) -> list[str]:
    with sqlite3.connect(paths["db_path"]) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT effect_kind FROM scheduler_effects WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            )
        ]


def test_standalone_codex_implementation_stages_and_uses_distinct_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "implemented-by-codex.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])

    state = _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")

    assert state.codex.implementer_session_id == IMPLEMENTER_ID
    assert state.codex.reviewer_session_id == REVIEWER_ID
    assert state.codex.implementer_session_id != state.codex.reviewer_session_id
    assert state.context.implementer.model == "gpt-5.5"
    assert state.context.implementer.reasoning_effort == "xhigh"
    assert state.context.codex.review_model == "gpt-5.6-sol"
    assert state.context.codex.review_reasoning_effort == "high"
    assert (git_repo / "implemented-by-codex.txt").is_file()
    assert (
        _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    )
    assert _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND) == 1
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        components = {
            str(row[0])
            for row in conn.execute(
                "SELECT component FROM scheduler_attempts WHERE run_id = ?",
                (run_id,),
            )
        }
    assert {"codex_implementer", "codex"} <= components


def test_findings_resume_exact_implementer_with_exact_correction_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "corrected-by-codex.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])

    state = _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")

    assert state.codex.implementer_session_id == IMPLEMENTER_ID
    kinds = _effect_kinds(scheduler_paths, run_id)
    assert kinds.count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    run_root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
    exact_fix = "Fix issue ALPHA from first findings review."
    assert (run_root / "prompts/fixes/01.txt").read_text(encoding="utf-8") == exact_fix
    correction = (run_root / "prompts/fixes/01.execution-envelope.txt").read_text(encoding="utf-8")
    assert correction.endswith(exact_fix)
    log_text = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert "'resume', '--model', 'gpt-5.5'" in log_text
    assert IMPLEMENTER_ID in log_text
    assert "--last" not in log_text


def test_reviewer_identity_collision_blocks_instead_of_sharing_implementer_session(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "identity-collision.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", IMPLEMENTER_ID)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)

    for _ in range(50):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            break
    else:
        raise AssertionError("reviewer identity collision did not block")

    assert state.block_reason_kind == "agent_identity_collision"
