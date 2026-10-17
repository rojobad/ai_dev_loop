"""Owned provider-aware scheduler fixture for Phase 26.7 installed acceptance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.conftest import FIXTURE_REPO, create_fake_clis
from tests.integration.test_phase26_1_dashboard_queries import _insert_attempt, _insert_run
from tests.integration.test_phase27_7_recovery_acceptance import (
    FIX_PROMPT,
    IMPLEMENTER_ID,
    REVIEWER_ONE_ID,
    REVIEWER_TWO_ID,
    _drive_recovery_chain_to_blocked,
    _invoke,
    _prepare_sequence,
    _ScriptedImplementer,
    _sequence_state,
    _tick,
)
from typer.testing import CliRunner

import ai_dev_loop.cli as cli
from ai_dev_loop.scheduler.application.codex_capacity_probe import CodexCapacityStatus
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.sequence_start import SequenceStartService
from ai_dev_loop.scheduler.domain.codex_contract import (
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXTURE_PROJECT = "fixture-project"
FIXTURE_FROM_UTC = "2026-10-14T00:00:00Z"
FIXTURE_TO_UTC = "2026-10-17T00:00:00Z"
SYNTHETIC_RUNS = 24
SYNTHETIC_TIMELINE_ATTEMPTS = 22
EXPECTED_PROCESS_SECONDS = 14_400
EXPECTED_ACTIVITY: dict[str, dict[str, object]] = {
    "submissions": {
        "unit": "count",
        "days": [
            {"date": "2026-10-14", "value": 0},
            {"date": "2026-10-15", "value": 27},
            {"date": "2026-10-16", "value": 0},
        ],
        "total": 27,
        "displayTotal": "27",
        "displayDays": ["0", "27", "0"],
    },
    "review-rounds": {
        "unit": "count",
        "days": [
            {"date": "2026-10-14", "value": 0},
            {"date": "2026-10-15", "value": 3},
            {"date": "2026-10-16", "value": 0},
        ],
        "total": 3,
        "displayTotal": "3",
        "displayDays": ["0", "3", "0"],
    },
    "process-hours": {
        "unit": "process-seconds",
        "days": [
            {"date": "2026-10-14", "value": 0},
            {"date": "2026-10-15", "value": EXPECTED_PROCESS_SECONDS},
            {"date": "2026-10-16", "value": 0},
        ],
        "total": EXPECTED_PROCESS_SECONDS,
        "displayTotal": "04 h 00 m",
        "displayDays": ["00 h 00 m", "04 h 00 m", "00 h 00 m"],
    },
}


@dataclass(frozen=True)
class InstalledDashboardFixture:
    state_home: Path
    artifact_root: Path
    db_path: Path
    provider_bin: Path
    sequence_id: str
    source_run_id: str
    replacement_run_id: str
    final_cursor_run_id: str
    implementer_attempt_id: str
    active_attempt_id: str
    historical_review_attempt_id: str
    exact_review_attempt_id: str
    runs_started: int
    submissions: int
    review_rounds: int
    map_total: int
    timeline_total: int
    activity_expectations: dict[str, dict[str, object]]

    def public_payload(self) -> dict[str, object]:
        return {
            "projectName": FIXTURE_PROJECT,
            "fromUtc": FIXTURE_FROM_UTC,
            "toUtc": FIXTURE_TO_UTC,
            "sequenceId": self.sequence_id,
            "sourceRunId": self.source_run_id,
            "replacementRunId": self.replacement_run_id,
            "finalCursorRunId": self.final_cursor_run_id,
            "implementerAttemptId": self.implementer_attempt_id,
            "activeAttemptId": self.active_attempt_id,
            "historicalReviewAttemptId": self.historical_review_attempt_id,
            "exactReviewAttemptId": self.exact_review_attempt_id,
            "runsStarted": self.runs_started,
            "submissions": self.submissions,
            "reviewRounds": self.review_rounds,
            "mapTotal": self.map_total,
            "timelineTotal": self.timeline_total,
            "activityExpectations": self.activity_expectations,
        }


def _make_target_repo(root: Path) -> Path:
    repo = root / "fixture-repo"
    shutil.copytree(FIXTURE_REPO, repo)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "phase26-fixture@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Phase 26 Fixture"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "phase 26 installed acceptance fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _force_recovery(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
    source_run_id: str,
) -> str:
    import ai_dev_loop.scheduler.application.implementer_timeout_retry as retry_module

    def retry(run_id: str, force: bool = False):  # type: ignore[no-untyped-def]
        return ImplementerRetryService(
            SqliteSchedulerStore(paths["db_path"]),
            ProtectedArtifactStore(paths["artifact_root"]),
            now_factory=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        ).retry(run_id, force=force)

    monkeypatch.setattr(retry_module, "scheduler_implementer_retry", retry)
    response = _invoke(
        CliRunner(),
        ["scheduler", "implementer-retry", source_run_id, "--force", "--output", "json"],
    )
    return str(response["run_id"])


def _drive_sequence_to_final(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
    script: _ScriptedImplementer,
    sequence_id: str,
) -> AwaitingFinalizationSequenceState:
    tick = _tick(
        paths,
        script,
        owner="phase26-7-installed-fixture",
        capacity=CodexCapacityStatus.AVAILABLE,
    )
    monkeypatch.setattr(cli, "run_scheduler_tick", tick.run_once)
    runner = CliRunner()
    for _ in range(180):
        sequence = _sequence_state(paths, sequence_id)
        if isinstance(sequence, AwaitingFinalizationSequenceState):
            return sequence
        if sequence.current_ordinal == 2:
            monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_TWO_ID)
        _invoke(runner, ["scheduler", "tick", "--output", "json"])
    raise RuntimeError("fixture sequence did not reach awaiting_finalization")


def _add_dashboard_volume(
    store: SqliteSchedulerStore,
    *,
    replacement_run_id: str,
) -> None:
    base = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    with store.begin_read() as conn:
        event_row = conn.execute(
            "SELECT event_id FROM scheduler_events WHERE run_id=? ORDER BY sequence LIMIT 1",
            (replacement_run_id,),
        ).fetchone()
    if event_row is None:
        raise RuntimeError("replacement fixture run has no submitted event")
    event_id = str(event_row["event_id"])
    for index in range(SYNTHETIC_TIMELINE_ATTEMPTS):
        _insert_attempt(
            store,
            run_id=replacement_run_id,
            event_id=event_id,
            suffix=f"historical-chat-{index:02d}",
            component="cursor",
            effect_kind="cursor.create_chat",
            iteration=1,
            launch=base - timedelta(hours=2, minutes=index),
            completed=base - timedelta(hours=2, minutes=index) + timedelta(seconds=30),
        )

    for index in range(SYNTHETIC_RUNS):
        run_id = f"installed-map-run-{index:02d}"
        submitted = base + timedelta(minutes=index)
        synthetic_event = _insert_run(
            store,
            run_id,
            project_name=FIXTURE_PROJECT,
            submitted_at=submitted,
            provider="codex" if index % 2 else "cursor",
        )
        codex = index % 2 == 1
        _insert_attempt(
            store,
            run_id=run_id,
            event_id=synthetic_event,
            suffix="execution",
            component="codex_implementer" if codex else "cursor",
            effect_kind="codex.implementer_bootstrap" if codex else "cursor.run_turn",
            iteration=1,
            launch=submitted + timedelta(minutes=1),
            completed=submitted + timedelta(minutes=11),
        )


def build_installed_dashboard_fixture(root: Path) -> InstalledDashboardFixture:
    """Create a private fixture using only temporary Git/XDG state and fake providers."""

    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    monkeypatch = pytest.MonkeyPatch()
    try:
        home = root / "home"
        config_home = root / "xdg" / "config"
        state_home = root / "xdg" / "state"
        cache_home = root / "xdg" / "cache"
        for path in (home, config_home, state_home, cache_home):
            path.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                path.chmod(0o700)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
        for name in (
            "AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT",
            "AI_DEV_LOOP_REVIEW_PROMPT_REL",
            "AI_DEV_LOOP_REVIEW_EVIDENCE_REL",
        ):
            monkeypatch.delenv(name, raising=False)

        provider_root = root / "providers"
        provider_root.mkdir()
        provider_paths = create_fake_clis(provider_root, monkeypatch)
        provider_paths["codex_models_file"].write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "id": "gpt-5.6-sol",
                            "supported_reasoning_efforts": ["high", "xhigh"],
                        },
                        {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        repo = _make_target_repo(root)
        scheduler_root = state_home / "ai_dev_loop"
        paths = {
            "db_path": scheduler_root / "engine.sqlite3",
            "artifact_root": scheduler_root / "artifacts",
        }
        script = _ScriptedImplementer(["timeout", "quota", "failure", "success", "success"])
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ONE_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings,no_findings")
        monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", FIX_PROMPT)
        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

        runner = CliRunner()
        import ai_dev_loop.scheduler.application.sequence_start as sequence_start_module

        monkeypatch.setattr(
            sequence_start_module,
            "start_sequence",
            lambda sequence_id: SequenceStartService(
                SqliteSchedulerStore(paths["db_path"]),
                ProtectedArtifactStore(paths["artifact_root"]),
                now_factory=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
            ).start(sequence_id),
        )
        sequence_id, source_run_id = _prepare_sequence(runner, repo)
        _drive_recovery_chain_to_blocked(
            runner,
            monkeypatch,
            paths,
            script,
            source_run_id,
        )
        replacement_run_id = _force_recovery(monkeypatch, paths, source_run_id)
        sequence = _drive_sequence_to_final(monkeypatch, paths, script, sequence_id)
        final_cursor_run_id = sequence.final_run_id

        store = SqliteSchedulerStore(paths["db_path"])
        _add_dashboard_volume(store, replacement_run_id=replacement_run_id)
        with store.begin_read() as conn:
            replacement_attempts = conn.execute(
                """
                SELECT a.*, e.effect_kind
                FROM scheduler_attempts AS a
                JOIN scheduler_effects AS e ON e.dispatch_id=a.dispatch_id
                WHERE a.run_id=? ORDER BY a.created_at, a.attempt_id
                """,
                (replacement_run_id,),
            ).fetchall()
            source_attempts = conn.execute(
                """
                SELECT a.*, e.effect_kind
                FROM scheduler_attempts AS a
                JOIN scheduler_effects AS e ON e.dispatch_id=a.dispatch_id
                WHERE a.run_id=? ORDER BY a.created_at, a.attempt_id
                """,
                (source_run_id,),
            ).fetchall()
        implementer = next(
            row for row in replacement_attempts if row["component"] == "codex_implementer"
        )
        reviews = [row for row in replacement_attempts if row["component"] == "codex"]
        if len(reviews) < 2:
            raise RuntimeError("fixture replacement run did not produce two reviewer rounds")
        active = next(
            row for row in reversed(source_attempts) if row["component"] == "codex_implementer"
        )
        with store.begin_immediate() as conn:
            conn.execute(
                """
                UPDATE scheduler_attempts
                SET status='active', completed_at=NULL, completion_envelope_sha256=NULL,
                    ingested=0, updated_at=launch_requested_at
                WHERE attempt_id=?
                """,
                (str(active["attempt_id"]),),
            )

        historical = reviews[0]
        historical_root = ProtectedArtifactStore(paths["artifact_root"]).run_root(
            replacement_run_id
        )
        for relative_name in (
            codex_review_prompt_rel(int(historical["iteration"]), str(historical["attempt_id"])),
            codex_review_prompt_evidence_rel(
                int(historical["iteration"]), str(historical["attempt_id"])
            ),
        ):
            path = historical_root / relative_name
            if path.exists():
                path.unlink()

        with store.begin_read() as conn:
            timeline_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?",
                    (replacement_run_id,),
                ).fetchone()[0]
            )
            map_total = int(conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0])
        fixture = InstalledDashboardFixture(
            state_home=state_home,
            artifact_root=paths["artifact_root"],
            db_path=paths["db_path"],
            provider_bin=provider_paths["bin_dir"],
            sequence_id=sequence_id,
            source_run_id=source_run_id,
            replacement_run_id=replacement_run_id,
            final_cursor_run_id=final_cursor_run_id,
            implementer_attempt_id=str(implementer["attempt_id"]),
            active_attempt_id=str(active["attempt_id"]),
            historical_review_attempt_id=str(historical["attempt_id"]),
            exact_review_attempt_id=str(reviews[1]["attempt_id"]),
            runs_started=3 + SYNTHETIC_RUNS,
            submissions=3 + SYNTHETIC_RUNS,
            review_rounds=3,
            map_total=map_total,
            timeline_total=timeline_total,
            activity_expectations=EXPECTED_ACTIVITY,
        )
        metadata_path = root / "fixture.json"
        metadata_path.write_text(
            json.dumps(fixture.public_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            metadata_path.chmod(0o600)
        return fixture
    finally:
        monkeypatch.undo()
