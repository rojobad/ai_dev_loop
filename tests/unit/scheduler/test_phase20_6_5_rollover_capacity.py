"""Capacity parsing and auto-resume tests for Phase 20.6.5 rollover."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexAppServerCapacityProbe,
    CodexCapacityStatus,
    capacity_from_rate_limits_payload,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import ProductionRolloverWorktreePort
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.state import WaitingCodexCapacityState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _submit(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
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
        max_review_iterations=3,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-rollover-cap-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_COUNTER):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def _run_until(tick: TickService, run_id: str, *, target_kind: str, max_ticks: int = 80) -> None:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
            if state.kind == "blocked":
                raise AssertionError(
                    f"run blocked: {getattr(state, 'block_reason_kind', '')}: "
                    f"{getattr(state, 'block_reason_summary', '')}"
                )
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


class TestNullReachedMarkerParsing:
    def test_missing_reached_marker_with_available_windows_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"primary": {"usedPercent": 0}},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_null_reached_marker_without_windows_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"rateLimitReachedType": None},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_null_reached_marker_with_available_windows_is_available(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "rateLimitReachedType": None,
                    "primary": {"usedPercent": 0},
                    "secondary": {"usedPercent": 10},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_empty_string_reached_marker_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"rateLimitReachedType": ""},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_malformed_marker_with_exhausted_window_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "rateLimitReachedType": None,
                    "primary": {"usedPercent": 100},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED


class TestCapacityAutoResume:
    def test_null_marker_probe_reports_available(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FAKE_CODEX_CAPACITY_LIMITS_JSON",
            json.dumps(
                {
                    "rateLimitsByLimitId": {
                        "default": {
                            "rateLimitReachedType": None,
                            "primary": {"usedPercent": 0},
                        }
                    }
                }
            ),
        )
        observation = CodexAppServerCapacityProbe().probe("codex")
        assert observation.status == CodexCapacityStatus.AVAILABLE

    def test_waiting_codex_capacity_auto_resumes_on_tick_with_null_marker(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 15, 18, 0, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexCapacityState)
        reviewer_id = state.codex.reviewer_session_id
        monkeypatch.setenv(
            "FAKE_CODEX_CAPACITY_LIMITS_JSON",
            json.dumps(
                {
                    "rateLimitsByLimitId": {
                        "default": {
                            "rateLimitReachedType": None,
                            "primary": {"usedPercent": 0},
                        }
                    }
                }
            ),
        )
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
        tick.run_once()
        _run_until(tick, run_id, target_kind="completed", max_ticks=60)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == reviewer_id

    def test_rollover_start_preserves_fresh_rollover_lineage(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_run_id, store, artifacts, _ = maxed_rollover_fixture(
            git_repo,
            scheduler_paths,
            monkeypatch,
        )
        prepared = RolloverPrepareService(store, artifacts).prepare(
            source_run_id,
            commit_message="capacity lineage rollover commit",
        )
        started = RolloverStartService(
            store,
            artifacts,
            worktree_port=ProductionRolloverWorktreePort(),
        ).start(prepared.rollover_id)
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, started.rollover_run_id)
        assert state.fresh_rollover is not None
        assert state.fresh_rollover.rollover_id == prepared.rollover_id
        assert state.fresh_rollover.source_run_id == source_run_id
