"""Phase 30.3 one-file SQLite backup and restore of numeric statistics."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase20_3_sequence_handoff import BOOTSTRAP_ID
from tests.integration.test_phase25_3_sdk_recovery import _configure_v2, _submit_sdk_run
from tests.integration.test_phase30_2_usage_backfill import _retarget_one_observation
from tests.support.phase30_3_restore_snapshot import (
    conceal_external_roots,
    recorded_external_roots,
    reveal_roots,
)
from tests.unit.scheduler.test_phase27_2_codex_implementer import _run_until, _tick
from tests.unit.scheduler.test_phase27_4_implementer_timeout_retry import _Clock
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.cursor_sdk_retention import (
    apply_cursor_sdk_cleanup,
    preview_cursor_sdk_cleanup,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_RUNNER = CliRunner()
_IMPLEMENTER = {
    "input_tokens": 100,
    "cached_input_tokens": 60,
    "output_tokens": 10,
    "reasoning_output_tokens": 4,
}
_CORRECTION = {
    "input_tokens": 8,
    "cached_input_tokens": 0,
    "output_tokens": 2,
    "reasoning_output_tokens": 1,
}
_REVIEW = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}
_SECOND_REVIEW = {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 3}
_ZERO = {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
}
_ZERO_REVIEW = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
_CURSOR = {
    "input_tokens": 4820,
    "output_tokens": 139,
    "cache_read_tokens": 2560,
    "cache_write_tokens": 0,
    "total_tokens": 7519,
    "reasoning_tokens": 118,
}
_SUCCESSOR_IMPLEMENTER = {
    "input_tokens": 20,
    "cached_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 1,
}
_SUCCESSOR_REVIEW = {"input_tokens": 7, "cached_input_tokens": 1, "output_tokens": 2}
_REPLACEMENT_IMPLEMENTER = {
    "input_tokens": 40,
    "cached_input_tokens": 0,
    "output_tokens": 10,
    "reasoning_output_tokens": 0,
}
_REPLACEMENT_REVIEW = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 4}
_REPLACEMENT_SUCCESSOR_IMPLEMENTER = {
    "input_tokens": 16,
    "cached_input_tokens": 0,
    "output_tokens": 4,
    "reasoning_output_tokens": 0,
}
_REPLACEMENT_SUCCESSOR_REVIEW = {"input_tokens": 6, "cached_input_tokens": 0, "output_tokens": 2}
_PHASE_TWO_IMPLEMENTER = {
    "input_tokens": 12,
    "cached_input_tokens": 0,
    "output_tokens": 3,
    "reasoning_output_tokens": 0,
}
_PHASE_TWO_REVIEW = {"input_tokens": 4, "cached_input_tokens": 0, "output_tokens": 1}
_CORRECTION_TOTAL = 165
_RECOVERY_SOURCE_TOTAL = 140
_RECOVERY_SUCCESSOR_TOTAL = 34
_RECOVERY_LINEAGE_TOTAL = 174
_REPLACEMENT_SOURCE_TOTAL = 64
_REPLACEMENT_SUCCESSOR_TOTAL = 28
_PHASE_TWO_TOTAL = 20
_REPLACEMENT_SEQUENCE_TOTAL = 112
_SEQUENCE_TOTAL = 280
_SDK_TOTAL = 7549
_ALPHA_TOTAL = 731
_COHORT_TOTAL = 8280
_SNAPSHOT = Path(__file__).resolve().parents[1] / "support" / "phase30_3_restore_snapshot.py"
_SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high", "xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def test_a01_a02_one_file_restore_preserves_statistics_without_external_roots(
    git_repo: Path,
    isolated_xdg: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _rename_project(git_repo, "fixture-alpha")
    clones = {
        name: _clone(git_repo, tmp_path / name)
        for name in (
            "correction",
            "zero",
            "recovery",
            "cancel",
            "beta",
            "preflight",
            "replacement",
        )
    }
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "019def00-0000-0000-0000-0000000000aa")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "restored-statistics.txt")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON", json.dumps([_IMPLEMENTER]))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW]))
    sequence_id, phase_ids = _run_sequence(git_repo, scheduler_paths)
    correction_id = _complete_named(
        clones["correction"],
        scheduler_paths,
        monkeypatch,
        short_name="Alpha correction",
        implementer=[_IMPLEMENTER, _CORRECTION],
        review=[_REVIEW, _SECOND_REVIEW],
        review_sequence="findings,no_findings",
        implementer_sequence="success,success",
    )
    zero_id = _complete_named(
        clones["zero"],
        scheduler_paths,
        monkeypatch,
        short_name="Alpha known zero",
        implementer=[_ZERO],
        review=[_ZERO_REVIEW],
    )
    recovery_source_id, recovery_successor_id = _recover_successor(
        clones["recovery"],
        scheduler_paths,
        monkeypatch,
        tmp_path / "recovery-counters",
    )
    (
        replacement_id,
        replacement_source_id,
        replacement_successor_id,
        replacement_phase_two_id,
    ) = _adopt_replacement(
        clones["replacement"],
        scheduler_paths,
        monkeypatch,
        tmp_path / "replacement-counters",
    )
    _assert_recovery_rows(
        scheduler_paths["db_path"],
        sequence_id=sequence_id,
        replacement_id=replacement_id,
        recovery_source_id=recovery_source_id,
        recovery_successor_id=recovery_successor_id,
        replacement_source_id=replacement_source_id,
        replacement_successor_id=replacement_successor_id,
        replacement_phase_two_id=replacement_phase_two_id,
    )
    _clear_codex_sequences(monkeypatch)
    cancel_id = _cancel_active(clones["cancel"], scheduler_paths)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW]))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR))
    monkeypatch.delenv("FAKE_CURSOR_SDK_TURN_PREFLIGHT_FAILURE", raising=False)

    def _beta_configure(repo: Path) -> None:
        _configure_v2(repo)
        path = repo / "ai_dev_loop.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "name: fixture-project", "name: fixture-beta", 1
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "ai_dev_loop.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "beta project name"], cwd=repo, check=True)

    def _credential_again(root: Path) -> None:
        credentials = root / "config/ai_dev_loop/credentials"
        credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        credentials.parent.chmod(0o700)
        credentials.chmod(0o700)
        key = credentials / "cursor-test"
        key.write_text("fake-test-credential", encoding="utf-8")
        key.chmod(0o600)

    monkeypatch.setattr(
        "tests.integration.test_phase25_3_sdk_recovery._configure_v2",
        _beta_configure,
    )
    monkeypatch.setattr(
        "tests.integration.test_phase25_3_sdk_recovery._credential",
        _credential_again,
    )
    sdk_id, _db, _artifacts, store, tick = _submit_sdk_run(
        clones["beta"],
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="Beta SDK facts",
    )
    assert tick.store.db_path == scheduler_paths["db_path"]
    _tick_until(tick, sdk_id, "completed")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TURN_PREFLIGHT_FAILURE", "1")
    preflight_id, _db, _artifacts, _store, preflight_tick = _submit_sdk_run(
        clones["preflight"],
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="Beta skipped preflight",
    )
    _tick_until(preflight_tick, preflight_id, "blocked")
    _assert_cleanup_keeps_usage_and_attempts(store, sdk_id)
    runs = {
        "correction": correction_id,
        "zero": zero_id,
        "recovery_source": recovery_source_id,
        "recovery_successor": recovery_successor_id,
        "replacement_source": replacement_source_id,
        "replacement_successor": replacement_successor_id,
        "replacement_phase_two": replacement_phase_two_id,
        "cancel": cancel_id,
        "sdk": sdk_id,
        "preflight": preflight_id,
        "phase_one": phase_ids[0],
        "phase_two": phase_ids[1],
    }
    settled = _backup_file(scheduler_paths["db_path"], tmp_path / "settled.sqlite3")
    settled_home = _install_database(settled, tmp_path / "settled-home")
    settled_live = _snapshot(
        isolated_xdg / "state",
        tmp_path / "settled-live",
        runs,
        sequence_id,
        replacement_id,
        correction_id,
    )
    concealed, moved = _conceal_fixture_roots(scheduler_paths)
    try:
        settled_restored = _snapshot(
            settled_home,
            tmp_path / "settled-restored",
            runs,
            sequence_id,
            replacement_id,
            correction_id,
            isolate=True,
            unavailable_paths=concealed,
        )
    finally:
        reveal_roots(moved)
    _assert_same_statistics(settled_live, settled_restored)
    _assert_independent(
        settled_restored, zero_total=0, cancel_requires_backfill=False, null_model=False
    )
    _assert_restore_safety(settled_restored, settled_home)

    shutil.rmtree(run_artifact_root(scheduler_paths["artifact_root"], zero_id))
    _delete_facts(scheduler_paths["db_path"], zero_id)
    imported = _backfill(zero_id, apply=True)
    assert imported["job_status"] == "completed"
    assert "usage_source_missing" in imported["diagnostics"]
    _delete_facts(scheduler_paths["db_path"], cancel_id)
    preserved = _usage_total(correction_id)
    _retarget_one_observation(scheduler_paths, correction_id)
    conflict = _backfill(correction_id, apply=True)
    assert conflict["conflicting"] >= 1
    assert _usage_total(correction_id) == preserved == _CORRECTION_TOTAL
    pending = _backfill(None, check=True)
    assert pending["statistically_ready"] is False
    assert pending["unmaterialized"] >= 1
    assert pending["conflicting"] >= 1

    backup = tmp_path / "wal-backup.sqlite3"
    naive = tmp_path / "naive-main-only.sqlite3"
    _commit_name_in_wal(
        scheduler_paths["db_path"],
        correction_id,
        "Alpha WAL rename",
        backup,
        naive,
    )
    with sqlite3.connect(naive) as conn:
        stale = conn.execute(
            "SELECT short_name FROM scheduler_run_presentation WHERE run_id = ?",
            (correction_id,),
        ).fetchone()
    assert stale is not None
    assert stale[0] == "Alpha correction"
    difficult_live = _snapshot(
        isolated_xdg / "state",
        tmp_path / "difficult-live",
        runs,
        sequence_id,
        replacement_id,
        correction_id,
    )
    difficult_home = _install_database(backup, tmp_path / "difficult-home")
    concealed, moved = _conceal_fixture_roots(scheduler_paths)
    try:
        difficult = _snapshot(
            difficult_home,
            tmp_path / "difficult-restored",
            runs,
            sequence_id,
            replacement_id,
            correction_id,
            isolate=True,
            unavailable_paths=concealed,
        )
    finally:
        reveal_roots(moved)
    _assert_same_statistics(difficult_live, difficult)
    _assert_independent(difficult, zero_total=None, cancel_requires_backfill=True, null_model=True)
    _assert_restore_safety(difficult, difficult_home)
    assert difficult["inspected"]["correction"]["data"]["shortName"] == "Alpha WAL rename"
    assert not (difficult_home / "ai_dev_loop" / "artifacts").exists()
    _assert_invalid_review_file_does_not_rebuild_usage(
        scheduler_paths,
        correction_id,
        monkeypatch,
    )
    assert sequence_id


def _assert_independent(
    snapshot: dict[str, object],
    *,
    zero_total: int | None,
    cancel_requires_backfill: bool,
    null_model: bool,
) -> None:
    correction = _direct(snapshot, "correction")
    assert correction["known_total_tokens"] == _CORRECTION_TOTAL
    assert correction["coverage"] == "complete"
    assert _recovery(snapshot, "correction")["known_total_tokens"] == _CORRECTION_TOTAL
    initial = _breakdown(correction, provider="codex", role="implementer", stage="initial")
    assert initial["known_total_tokens"] == 110
    assert initial["categories"]["input_total"]["known_total"] == 100
    assert initial["categories"]["cache_read"]["known_total"] == 60
    assert initial["categories"]["output_total"]["known_total"] == 10
    assert initial["categories"]["reasoning_output"]["known_total"] == 4
    assert initial["known_total_tokens"] == (
        initial["categories"]["input_total"]["known_total"]
        + initial["categories"]["output_total"]["known_total"]
    )
    corrected = _breakdown(correction, provider="codex", role="implementer", stage="correction")
    assert corrected["known_total_tokens"] == 10
    assert corrected["categories"]["cache_read"]["known_total"] == 0
    reviewed = _breakdown(correction, provider="codex", role="reviewer", stage="review")
    assert reviewed["known_total_tokens"] == 45
    recovery_source = _direct(snapshot, "recovery_source")
    recovery_successor = _direct(snapshot, "recovery_successor")
    assert recovery_source["known_total_tokens"] == _RECOVERY_SOURCE_TOTAL
    assert recovery_source["coverage"] == "partial"
    assert recovery_successor["known_total_tokens"] == _RECOVERY_SUCCESSOR_TOTAL
    assert recovery_source["known_total_tokens"] != recovery_successor["known_total_tokens"]
    assert recovery_successor["coverage"] == "complete"
    successor_lineage = _recovery(snapshot, "recovery_successor")
    assert successor_lineage["known_total_tokens"] == _RECOVERY_LINEAGE_TOTAL
    assert successor_lineage["known_total_tokens"] != recovery_successor["known_total_tokens"]
    assert set(_ids(recovery_successor)) == {_run_id(snapshot, "recovery_successor")}
    assert set(_ids(successor_lineage)) == {
        _run_id(snapshot, "recovery_source"),
        _run_id(snapshot, "recovery_successor"),
    }
    assert _recovery(snapshot, "recovery_source")["known_total_tokens"] == _RECOVERY_SOURCE_TOTAL
    replacement_source = _direct(snapshot, "replacement_source")
    replacement_successor = _direct(snapshot, "replacement_successor")
    assert replacement_source["known_total_tokens"] == _REPLACEMENT_SOURCE_TOTAL
    assert replacement_source["coverage"] == "partial"
    assert replacement_successor["known_total_tokens"] == _REPLACEMENT_SUCCESSOR_TOTAL
    assert replacement_source["known_total_tokens"] != replacement_successor["known_total_tokens"]
    replacement = _scope(snapshot["replacement_sequence_usage"], "sequence")
    assert replacement["known_total_tokens"] == _REPLACEMENT_SEQUENCE_TOTAL
    assert replacement["known_total_tokens"] == (
        _REPLACEMENT_SOURCE_TOTAL + _REPLACEMENT_SUCCESSOR_TOTAL + _PHASE_TWO_TOTAL
    )
    replacement_ids = set(_ids(replacement))
    assert replacement_ids == {
        _run_id(snapshot, "replacement_source"),
        _run_id(snapshot, "replacement_successor"),
        _run_id(snapshot, "replacement_phase_two"),
    }
    assert len(_ids(replacement)) == len(replacement_ids)
    unrelated = {
        _run_id(snapshot, "correction"),
        _run_id(snapshot, "recovery_source"),
        _run_id(snapshot, "recovery_successor"),
        _run_id(snapshot, "phase_one"),
        _run_id(snapshot, "phase_two"),
        _run_id(snapshot, "sdk"),
    }
    assert replacement_ids.isdisjoint(unrelated)
    inspected_replacement = _mapping(_mapping(snapshot["replacement_sequence_inspect"])["data"])
    assert inspected_replacement["tokenUsage"]["sequence"]["knownTotalTokens"] == (
        _REPLACEMENT_SEQUENCE_TOTAL
    )
    assert set(inspected_replacement["tokenUsage"]["sequence"]["runIds"]) == replacement_ids
    replaced_phase = _mapping(inspected_replacement["phases"][0])
    following_phase = _mapping(inspected_replacement["phases"][1])
    replacement_runs = replaced_phase["runs"]
    following_runs = following_phase["runs"]
    assert isinstance(replacement_runs, list)
    assert isinstance(following_runs, list)
    generations = {
        int(_mapping(item)["generation"]): str(_mapping(item)["attemptKind"])
        for item in replacement_runs
    }
    assert generations == {1: "planned_run", 2: "implementer_retry"}
    assert {int(_mapping(item)["generation"]) for item in following_runs} == {1}
    adopted = next(item for item in replacement_runs if int(_mapping(item)["generation"]) == 2)
    assert _mapping(adopted)["sourceRunId"] == _run_id(snapshot, "replacement_source")
    assert _direct(snapshot, "replacement_phase_two")["known_total_tokens"] == _PHASE_TWO_TOTAL
    zero = _direct(snapshot, "zero")
    assert zero["known_total_tokens"] == zero_total
    if zero_total == 0:
        assert zero["coverage"] == "complete"
    else:
        assert zero["coverage"] == "unknown"
    sdk = _direct(snapshot, "sdk")
    assert sdk["known_total_tokens"] == _SDK_TOTAL
    assert sdk["coverage"] == "complete"
    assert _recovery(snapshot, "sdk")["known_total_tokens"] == _SDK_TOTAL
    cursor = _breakdown(sdk, provider="cursor", role="implementer", stage="initial")
    assert cursor["known_total_tokens"] == 7519
    assert cursor["categories"]["input_total"]["known_total"] == 7380
    assert cursor["categories"]["cache_read"]["known_total"] == 2560
    assert cursor["categories"]["cache_write"]["known_total"] == 0
    assert cursor["categories"]["output_total"]["known_total"] == 139
    assert cursor["categories"]["reasoning_output"]["known_total"] == 118
    assert cursor["known_total_tokens"] == (
        cursor["categories"]["input_total"]["known_total"]
        + cursor["categories"]["output_total"]["known_total"]
    )
    assert (
        _breakdown(sdk, provider="codex", role="reviewer", stage="review")["known_total_tokens"]
        == 30
    )
    preflight = _direct(snapshot, "preflight")
    assert preflight["coverage"] == "not_applicable"
    assert preflight["known_total_tokens"] is None
    cancel = _direct(snapshot, "cancel")
    assert cancel["known_total_tokens"] is None
    diagnostics = set(_usage(snapshot, "cancel")["direct"]["diagnostics"])
    if cancel_requires_backfill:
        assert "backfill_required" in diagnostics
    else:
        assert "backfill_required" not in diagnostics
    sequence = _scope(snapshot["sequence_usage"], "sequence")
    assert sequence["known_total_tokens"] == _SEQUENCE_TOTAL
    assert set(_ids(sequence)) == {
        _run_id(snapshot, "phase_one"),
        _run_id(snapshot, "phase_two"),
    }
    assert len(_ids(sequence)) == 2
    assert snapshot["sequence_inspect"]["data"]["tokenUsage"]["sequence"]["knownTotalTokens"] == (
        _SEQUENCE_TOTAL
    )
    dashboard = _mapping(snapshot["dashboard"])
    assert dashboard["tokens"]["source"]["knownTotalTokens"] == _COHORT_TOTAL
    assert dashboard["tokens"]["source"]["coverage"] == "partial"
    _assert_disjoint_flows(dashboard)
    assert dashboard["tokens_santiago"]["source"]["knownTotalTokens"] == _COHORT_TOTAL
    assert dashboard["tokens_outside"]["source"]["knownTotalTokens"] is None
    assert dashboard["tokens_outside"]["rows"] == []
    assert (
        dashboard["tokens_by_project"]["fixture-alpha"]["source"]["knownTotalTokens"]
        == _ALPHA_TOTAL
    )
    assert (
        dashboard["tokens_by_project"]["fixture-beta"]["source"]["knownTotalTokens"] == _SDK_TOTAL
    )
    models = {row["configuredModel"] for row in dashboard["tokens"]["rows"]}
    assert {"gpt-5.5", "gpt-5.6-sol", "grok-4.7", "gpt-6.1-sol"} <= models
    zero_rows = dashboard["tokens_by_run"]["zero"]["rows"]
    assert any(row["configuredModel"] is None for row in zero_rows) is null_model
    assert snapshot["statuses"]["correction"]["summary"]["review_iterations_completed"] == 2
    assert snapshot["statuses"]["correction"]["summary"]["state_kind"] == "completed"
    assert snapshot["statuses"]["cancel"]["summary"]["state_kind"] == "aborted"
    assert snapshot["statuses"]["cancel"]["summary"]["review_iterations_completed"] == 0
    assert snapshot["statuses"]["preflight"]["summary"]["state_kind"] == "blocked"
    assert snapshot["statuses"]["sdk"]["summary"]["project_name"] == "fixture-beta"
    assert snapshot["statuses"]["phase_one"]["summary"]["project_name"] == "fixture-alpha"
    assert (
        snapshot["inspected"]["sdk"]["data"]["tokenUsage"]["direct"]["knownTotalTokens"]
        == _SDK_TOTAL
    )
    assert snapshot["inspected"]["correction"]["data"]["projectName"] == "fixture-alpha"
    assert snapshot["sequence_status"]["state_kind"] == "awaiting_finalization"
    assert snapshot["sequence_inspect"]["data"]["shortName"] == "Restore sequence"
    projects = {item["projectName"]: item["runCount"] for item in dashboard["projects"]["items"]}
    assert projects["fixture-alpha"] == 10
    assert projects["fixture-beta"] == 2
    activity = dashboard["activity"]["UTC"]
    santiago = dashboard["activity"]["America/Santiago"]
    assert activity["submissions"]["total"] == 12
    assert santiago["submissions"]["total"] == 12
    assert activity["review-rounds"]["total"] == 11
    assert santiago["review-rounds"]["total"] == 11
    assert activity["process-hours"]["total"] == 0
    assert santiago["process-hours"]["total"] == 0
    readiness = _mapping(snapshot["readiness"])
    if cancel_requires_backfill:
        assert readiness["statistically_ready"] is False
        assert readiness["unmaterialized"] >= 1
        assert readiness["conflicting"] >= 1
    else:
        assert readiness["statistically_ready"] is True
        assert readiness["unmaterialized"] == 0


def _assert_disjoint_flows(dashboard: dict[str, object]) -> None:
    correction = _mapping(dashboard["tokens_by_run"]["correction"])
    initial = _token_row(correction, provider="codex", role="implementer", stage="initial")
    assert initial["knownTotalTokens"] == 110
    assert _flow(initial, "input_other") == 40
    assert _flow(initial, "cache_read") == 60
    assert _flow(initial, "cache_write") is None
    assert _flow(initial, "output_other") == 6
    assert _flow(initial, "reasoning_output") == 4
    assert _flow(initial, "output_unsplit") is None
    corrected = _token_row(correction, provider="codex", role="implementer", stage="correction")
    assert corrected["knownTotalTokens"] == 10
    assert _flow(corrected, "input_other") == 8
    assert _flow(corrected, "cache_read") == 0
    assert _flow(corrected, "output_other") == 1
    assert _flow(corrected, "reasoning_output") == 1
    reviewed = _token_row(correction, provider="codex", role="reviewer", stage="review")
    assert reviewed["knownTotalTokens"] == 45
    assert _flow(reviewed, "input_other") == 30
    assert _flow(reviewed, "cache_read") == 7
    assert _flow(reviewed, "output_unsplit") == 8
    assert _flow(reviewed, "reasoning_output") is None
    correction_source = _mapping(correction["source"])
    assert correction_source["knownTotalTokens"] == _CORRECTION_TOTAL
    assert _flow(correction_source, "input_other") == 78
    assert _flow(correction_source, "cache_read") == 67
    assert _flow(correction_source, "output_other") == 7
    assert _flow(correction_source, "reasoning_output") == 5
    assert _flow(correction_source, "output_unsplit") == 8
    assert _known_flow_sum(correction_source) == _CORRECTION_TOTAL
    sdk = _mapping(dashboard["tokens_by_run"]["sdk"])
    cursor = _token_row(sdk, provider="cursor", role="implementer", stage="initial")
    assert cursor["knownTotalTokens"] == 7519
    assert _flow(cursor, "input_other") == 4820
    assert _flow(cursor, "cache_read") == 2560
    assert _flow(cursor, "cache_write") == 0
    assert _flow(cursor, "output_other") == 21
    assert _flow(cursor, "reasoning_output") == 118
    sdk_source = _mapping(sdk["source"])
    assert sdk_source["knownTotalTokens"] == _SDK_TOTAL
    assert _flow(sdk_source, "input_other") == 4840
    assert _flow(sdk_source, "cache_read") == 2565
    assert _flow(sdk_source, "cache_write") == 0
    assert _flow(sdk_source, "output_other") == 21
    assert _flow(sdk_source, "reasoning_output") == 118
    assert _flow(sdk_source, "output_unsplit") == 5
    assert _known_flow_sum(sdk_source) == _SDK_TOTAL


def _assert_invalid_review_file_does_not_rebuild_usage(
    paths: dict[str, Path],
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT attempt.attempt_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.bootstrap_review'
            ORDER BY attempt.rowid
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    assert row is not None
    evidence = (
        run_artifact_root(paths["artifact_root"], run_id)
        / "attempts"
        / str(row[0])
        / "invocation-evidence.json"
    )
    original = evidence.read_bytes()
    evidence.write_bytes(b"{")
    evidence.chmod(0o600)

    def refuse_provider(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("text read launched a provider")

    monkeypatch.setattr(subprocess, "run", refuse_provider)
    try:
        completed = _RUNNER.invoke(
            app,
            [
                "integration",
                "run",
                "review-content",
                run_id,
                "--attempt",
                str(row[0]),
                "--kind",
                "prompt",
                "--output",
                "json",
            ],
        )
    finally:
        evidence.write_bytes(original)
        evidence.chmod(0o600)
    assert completed.exit_code == 5, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DATA_INTEGRITY"
    assert payload["data"] is None
    assert "contentBase64" not in completed.stdout
    assert _usage_total(run_id) == _CORRECTION_TOTAL


def _token_row(payload: dict[str, object], **expected: str) -> dict[str, object]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        if all(item.get(key) == value for key, value in expected.items()):
            return item
    raise AssertionError(expected)


def _flow(row: dict[str, object], name: str) -> int | None:
    flows = row["flows"]
    assert isinstance(flows, list)
    for item in flows:
        assert isinstance(item, dict)
        if item.get("name") == name:
            total = item.get("knownTotal")
            assert total is None or isinstance(total, int)
            return total
    raise AssertionError(name)


def _known_flow_sum(row: dict[str, object]) -> int:
    flows = row["flows"]
    assert isinstance(flows, list)
    return sum(
        int(item["knownTotal"])
        for item in flows
        if isinstance(item, dict) and isinstance(item.get("knownTotal"), int)
    )


def _assert_same_statistics(live: dict[str, object], restored: dict[str, object]) -> None:
    left = _statistics(live)
    right = _statistics(restored)
    diffs = _diff_paths(left, right)
    assert diffs == [], "\n".join(diffs[:40])
    assert live["schema_version"] == restored["schema_version"] == 22


def _diff_paths(left: object, right: object, prefix: str = "") -> list[str]:
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(f"{child}: missing")
            else:
                paths.extend(_diff_paths(left[key], right[key], child))
        return paths
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        paths = []
        for index, (item, other) in enumerate(zip(left, right, strict=True)):
            paths.extend(_diff_paths(item, other, f"{prefix}[{index}]"))
        return paths
    return [f"{prefix}: {left!r} != {right!r}"]


def _assert_restore_safety(snapshot: dict[str, object], state_home: Path) -> None:
    assert snapshot["opener_calls"] == 0
    assert snapshot["artifact_root_exists"] is False
    assert snapshot["database_sha256_before"] == snapshot["database_sha256_after"]
    assert snapshot["schema_version_after"] == 22
    review = _mapping(_mapping(snapshot["review_content"])["data"])
    assert review["available"] is False
    assert review["reason"] == "not_recorded"
    assert review.get("contentBase64") in {None, ""}
    initial_prompt = _mapping(snapshot["initial_prompt"])
    assert initial_prompt["exit_code"] == 5
    assert initial_prompt["ok"] is False
    assert initial_prompt["code"] == "DATA_INTEGRITY"
    assert initial_prompt["data"] is None
    inspect = _mapping(_mapping(snapshot["sequence_inspect"])["data"])
    phases = inspect["phases"]
    assert isinstance(phases, list)
    assert _mapping(_mapping(phases[0])["checkpoint"])["available"] is False
    status = _mapping(snapshot["sequence_status"])
    assert status["completion_report_sha256_prefix"] is None
    counts = _mapping(status["aggregate_counts"])
    assert counts["planned"] == 2
    assert counts["materialized"] == 2
    assert counts["checkpointed"] == 1
    replacement_counts = _mapping(
        _mapping(snapshot["replacement_sequence_status"])["aggregate_counts"]
    )
    assert replacement_counts["checkpointed"] == 1
    assert snapshot["forbidden_env"] == []
    assert snapshot["unavailable_present"] == []
    missing = snapshot["unavailable_missing"]
    assert isinstance(missing, list) and missing
    origin = Path(str(snapshot["package_origin"]))
    assert origin.is_file()
    assert origin.is_relative_to(_SOURCE / "src")
    timelines = _mapping(_mapping(snapshot["timelines"])["sdk"])
    entries = timelines["entries"]
    assert isinstance(entries, list)
    assert all(_mapping(entry)["sdk_wait_stage"] == "unknown" for entry in entries)
    assert any(
        _mapping(entry)["observed_duration_seconds"] not in {None, "unknown"} for entry in entries
    )
    assert not (state_home / "ai_dev_loop" / "artifacts").exists()


def _statistics(snapshot: dict[str, object]) -> object:
    body = {
        key: value
        for key, value in snapshot.items()
        if key
        not in {
            "database_sha256_before",
            "database_sha256_after",
            "artifact_root_exists",
            "opener_calls",
            "review_content",
            "initial_prompt",
            "schema_version_after",
            "forbidden_env",
            "unavailable_missing",
            "unavailable_present",
            "package_origin",
        }
    }
    readiness = dict(_mapping(body["readiness"]))
    for key in ("missing_sources", "corrupt_sources", "malformed_sources", "diagnostics"):
        readiness.pop(key, None)
    body["readiness"] = readiness
    return _without_observed(body)


def _without_observed(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_observed(item)
            for key, item in value.items()
            if key
            not in {
                "observedAt",
                "observed_at",
                "report",
                "checkpoint",
                "checkpoint_commit_sha256_prefix",
                "commitSha256Prefix",
                "completion_report_sha256_prefix",
                "sdk_wait_stage",
                "sdk_wait_elapsed_seconds",
                "sdk_wait_waiting_for_initial_response",
            }
        }
    if isinstance(value, list):
        return [_without_observed(item) for item in value]
    return value


def _snapshot(
    state_home: Path,
    work: Path,
    runs: dict[str, str],
    sequence_id: str,
    replacement_sequence_id: str,
    review_run_id: str,
    *,
    isolate: bool = False,
    unavailable_paths: list[Path] | None = None,
) -> dict[str, object]:
    work.mkdir(parents=True)
    config = work / "config.json"
    config.write_text(
        json.dumps(
            {
                "state_home": str(state_home),
                "isolate": isolate,
                "runs": runs,
                "sequence_id": sequence_id,
                "replacement_sequence_id": replacement_sequence_id,
                "review_run_id": review_run_id,
                "unavailable_paths": [str(path) for path in (unavailable_paths or [])],
            }
        ),
        encoding="utf-8",
    )
    trap = work / "trap"
    trap.mkdir()
    witness = work / "provider-witness"
    witness.write_text("", encoding="utf-8")
    script = '#!/bin/sh\nprintf \'%s\\n\' "$0" >> "$PROVIDER_WITNESS"\nexit 99\n'
    for name in ("codex", "agent", "cursor"):
        path = trap / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(work / "sentinel-codex-home")
    env["OPENAI_API_KEY"] = "sk-phase303-sentinel"
    env["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
    for key in list(env):
        if key.startswith(("FAKE_", "CURSOR_", "OPENAI_", "CODEX_", "ANTHROPIC_", "AZURE_OPENAI_")):
            env.pop(key)
    env.update(
        {
            "PATH": f"{trap}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "PROVIDER_WITNESS": str(witness),
            "HOME": str(work / "home"),
            "XDG_STATE_HOME": str(state_home),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "PYTHONPATH": os.pathsep.join(
                [str(_SOURCE / "src"), str(_SOURCE), env.get("PYTHONPATH", "")]
            ),
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(_SNAPSHOT), str(config)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert witness.read_text(encoding="utf-8") == ""
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _commit_name_in_wal(
    database: Path,
    run_id: str,
    new_name: str,
    backup: Path,
    naive: Path,
) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    live = sqlite3.connect(database)
    try:
        assert str(live.execute("PRAGMA journal_mode=WAL").fetchone()[0]) == "wal"
        live.execute("BEGIN IMMEDIATE")
        updated = live.execute(
            """
            UPDATE scheduler_run_presentation
            SET short_name = ?
            WHERE run_id = ?
            """,
            (new_name, run_id),
        ).rowcount
        live.commit()
        assert updated == 1
        wal = Path(f"{database}-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        shutil.copyfile(database, naive)
        dest = sqlite3.connect(backup)
        try:
            live.backup(dest)
        finally:
            dest.close()
    finally:
        live.close()
    with sqlite3.connect(backup) as conn:
        stored = conn.execute(
            "SELECT short_name FROM scheduler_run_presentation WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert stored is not None and stored[0] == new_name


def _backup_file(database: Path, dest: Path) -> Path:
    source = sqlite3.connect(database)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def _install_database(backup: Path, home: Path) -> Path:
    state = home / "state"
    root = state / "ai_dev_loop"
    root.mkdir(parents=True)
    target = root / "engine.sqlite3"
    shutil.copyfile(backup, target)
    assert not (root / "artifacts").exists()
    assert not Path(f"{target}-wal").exists()
    return state


def _backfill(run_id: str | None, *, apply: bool = False, check: bool = False) -> dict[str, object]:
    args = ["scheduler", "usage-backfill"]
    if run_id is None:
        args.append("--all")
    else:
        args.extend(["--run-id", run_id])
    args.append("--apply" if apply else "--check")
    args.extend(["--output", "json"])
    if check:
        args = ["scheduler", "usage-backfill", "--all", "--check", "--output", "json"]
    completed = _RUNNER.invoke(app, args)
    assert completed.exit_code == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _usage_total(run_id: str) -> int | None:
    completed = _RUNNER.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert completed.exit_code == 0, completed.stderr
    total = json.loads(completed.stdout)["direct"]["known_total_tokens"]
    assert total is None or isinstance(total, int)
    return total


def _delete_facts(database: Path, run_id: str) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM scheduler_usage_observations WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM scheduler_usage_attempt_facts WHERE run_id = ?", (run_id,))
        conn.commit()


def _assert_cleanup_keeps_usage_and_attempts(store: SqliteSchedulerStore, run_id: str) -> None:
    state_root = store.db_path.parent
    before_facts, before_attempts = _counts(store.db_path, run_id)
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    owned = [item for item in preview.conversations if run_id in item.related_run_ids]
    assert owned
    assert owned[0].manual_eligible is True
    assert owned[0].manual_reasons == ()
    result = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=datetime(2030, 1, 1, tzinfo=UTC),
        mode="manual",
        conversation_key=owned[0].conversation_key,
    )
    assert result.deleted is True
    assert _counts(store.db_path, run_id) == (before_facts, before_attempts)
    assert before_facts >= 1
    assert before_attempts >= 1


def _counts(database: Path, run_id: str) -> tuple[int, int]:
    with sqlite3.connect(database) as conn:
        facts = conn.execute(
            "SELECT COUNT(*) FROM scheduler_usage_attempt_facts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        attempts = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert facts is not None and attempts is not None
    return int(facts[0]), int(attempts[0])


def _run_sequence(repo: Path, paths: dict[str, Path]) -> tuple[str, tuple[str, str]]:
    manifest = repo / "restore-sequence.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "name": "restore sequence",
                "short_name": "Restore sequence",
                "phases": [
                    _phase("phase-one", "Phase one", commit=True),
                    _phase("phase-two", "Phase two", commit=False),
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    prepared = prepare_sequence(
        SequencePrepareOptions(
            repo_path=repo,
            manifest_path=manifest,
            db_path=paths["db_path"],
            artifact_root=paths["artifact_root"],
        )
    )
    start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    tick = _sequence_tick(repo, paths)
    state = None
    for _ in range(240):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state = tick.store.load_validated_sequence_state(conn, prepared.sequence_id)
        if isinstance(state, AwaitingFinalizationSequenceState):
            break
    assert isinstance(state, AwaitingFinalizationSequenceState)
    phase_ids = tuple(entry.run_id for entry in state.materialized_entries)
    assert len(phase_ids) == 2
    return prepared.sequence_id, (phase_ids[0], phase_ids[1])


def _phase(name: str, short_name: str, *, commit: bool) -> dict[str, object]:
    phase: dict[str, object] = {
        "name": name,
        "short_name": short_name,
        "plan_path": "docs/plans/sample-plan.md",
        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
        "implementer": {
            "provider": "codex",
            "command": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "timeout_minutes": 90,
        },
        "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
    }
    if commit:
        phase["commit_message"] = "checkpoint after phase one"
    return phase


def _sequence_tick(repo: Path, paths: dict[str, Path]) -> TickService:
    owners = itertools.count(1)
    return TickService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase30-3-sequence-{next(owners)}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def _complete_named(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    short_name: str,
    implementer: list[object],
    review: list[object],
    review_sequence: str = "no_findings",
    implementer_sequence: str | None = None,
) -> str:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON", json.dumps(implementer))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps(review))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    if implementer_sequence is None:
        monkeypatch.delenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", raising=False)
    else:
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", implementer_sequence)
    run_id = _submit_named(repo, paths, short_name)
    start_run(run_id, db_path=paths["db_path"])
    _run_until(_tick(repo, paths), run_id, "completed")
    return run_id


def _recover_successor(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    counters: Path,
) -> tuple[str, str]:
    _point_codex_counters(monkeypatch, counters)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,fail,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([_IMPLEMENTER, _SUCCESSOR_IMPLEMENTER]),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([_REVIEW, _SUCCESSOR_REVIEW]),
    )
    source_id = _submit_named(repo, paths, "Alpha recovery source")
    start_run(source_id, db_path=paths["db_path"])
    tick = _tick(repo, paths)
    blocked = _drive_until_blocked(tick, source_id)
    assert blocked.block_reason_kind == "codex_implementer_failure"
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    ).retry(source_id, force=True)
    assert recovered.changed
    assert recovered.run_id != source_id
    _run_until(tick, recovered.run_id, "completed", limit=160)
    return source_id, recovered.run_id


def _adopt_replacement(
    repo: Path,
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    counters: Path,
) -> tuple[str, str, str, str]:
    _point_codex_counters(monkeypatch, counters)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,fail,success,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                _REPLACEMENT_IMPLEMENTER,
                _REPLACEMENT_SUCCESSOR_IMPLEMENTER,
                _REPLACEMENT_SUCCESSOR_IMPLEMENTER,
                _PHASE_TWO_IMPLEMENTER,
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([_REPLACEMENT_REVIEW, _REPLACEMENT_SUCCESSOR_REVIEW, _PHASE_TWO_REVIEW]),
    )
    manifest = repo / "replacement-sequence.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "name": "restore replacement",
                "short_name": "Restore replacement",
                "phases": [
                    _phase("replacement", "Replacement phase", commit=True),
                    _phase("following", "Following phase", commit=False),
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    prepared = prepare_sequence(
        SequencePrepareOptions(
            repo_path=repo,
            manifest_path=manifest,
            db_path=paths["db_path"],
            artifact_root=paths["artifact_root"],
        )
    )
    start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    tick = _sequence_tick(repo, paths)
    _sequence, blocked = _drive_sequence_leaf_blocked(tick, prepared.sequence_id)
    assert blocked.block_reason_kind == "codex_implementer_failure"
    recovered = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    ).retry(blocked.run_id, force=True)
    assert recovered.changed
    assert recovered.run_id != blocked.run_id
    state = None
    for _ in range(240):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state = tick.store.load_validated_sequence_state(conn, prepared.sequence_id)
        if isinstance(state, AwaitingFinalizationSequenceState):
            break
    assert isinstance(state, AwaitingFinalizationSequenceState)
    assert state.final_run_id != recovered.run_id
    return prepared.sequence_id, blocked.run_id, recovered.run_id, state.final_run_id


def _point_codex_counters(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    directory.mkdir(parents=True)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_COUNTER", str(directory / "implementer"))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(directory / "review"))
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")


def _clear_codex_sequences(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FAKE_CODEX_IMPLEMENTER_SEQUENCE",
        "FAKE_CODEX_IMPLEMENTER_COUNTER",
        "FAKE_CODEX_REVIEW_COUNTER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")


def _drive_until_blocked(tick: TickService, run_id: str) -> object:
    for _ in range(160):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            return state
        if state.kind in {"completed", "completed_with_residual_risk", "aborted"}:
            raise AssertionError(state.kind)
    raise AssertionError(f"{run_id} did not block")


def _drive_sequence_leaf_blocked(tick: TickService, sequence_id: str) -> tuple[object, object]:
    sequence = None
    leaf = None
    for _ in range(240):
        tick.run_once()
        with tick.store.begin_read() as conn:
            sequence = tick.store.load_validated_sequence_state(conn, sequence_id)
            if isinstance(sequence, AwaitingFinalizationSequenceState):
                raise AssertionError("replacement sequence finished before the leaf blocked")
            run_id = getattr(sequence, "current_run_id", None)
            if not isinstance(run_id, str):
                continue
            leaf, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if leaf.kind == "blocked":
            return sequence, leaf
    raise AssertionError(f"{sequence_id} leaf did not block: {type(sequence).__name__}")


def _assert_recovery_rows(
    database: Path,
    *,
    sequence_id: str,
    replacement_id: str,
    recovery_source_id: str,
    recovery_successor_id: str,
    replacement_source_id: str,
    replacement_successor_id: str,
    replacement_phase_two_id: str,
) -> None:
    with sqlite3.connect(database) as conn:
        ready = conn.execute(
            """
            SELECT source_run_id, successor_run_id
            FROM scheduler_codex_implementer_recoveries
            WHERE status = 'ready'
            """
        ).fetchall()
        original = conn.execute(
            """
            SELECT generation FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ?
            """,
            (sequence_id,),
        ).fetchall()
        replacement = conn.execute(
            """
            SELECT ordinal, generation, attempt_kind, run_id, source_run_id
            FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ?
            ORDER BY ordinal, generation
            """,
            (replacement_id,),
        ).fetchall()
        adopted = conn.execute(
            """
            SELECT adopted_at FROM scheduler_sequence_implementer_replacements
            WHERE successor_run_id = ?
            """,
            (replacement_successor_id,),
        ).fetchone()
    pairs = {(str(row[0]), str(row[1])) for row in ready}
    assert (recovery_source_id, recovery_successor_id) in pairs
    assert (replacement_source_id, replacement_successor_id) in pairs
    assert original and {int(row[0]) for row in original} == {1}
    assert [(int(row[0]), int(row[1]), str(row[2])) for row in replacement] == [
        (1, 1, "planned_run"),
        (1, 2, "implementer_retry"),
        (2, 1, "planned_run"),
    ]
    assert str(replacement[1][3]) == replacement_successor_id
    assert str(replacement[1][4]) == replacement_source_id
    assert str(replacement[2][3]) == replacement_phase_two_id
    assert adopted is not None and adopted[0] is not None


def _conceal_fixture_roots(paths: dict[str, Path]) -> tuple[list[Path], list[tuple[Path, Path]]]:
    state_root = paths["db_path"].parent
    targets = recorded_external_roots(paths["db_path"])
    targets.append(paths["artifact_root"])
    targets.append(state_root / "cursor-sdk")
    disposals = state_root / "cursor-sdk-disposals"
    if disposals.exists():
        targets.append(disposals)
    return conceal_external_roots(targets, checkout=_SOURCE)


def _cancel_active(repo: Path, paths: dict[str, Path]) -> str:
    clock = _Clock()
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=40))
    run_id = _submit_named(repo, paths, "Alpha cancelled")
    start_run(run_id, db_path=paths["db_path"])
    tick = _tick(repo, paths, backend=backend, now_factory=clock)
    active = False
    for _ in range(20):
        tick.run_once()
        with tick.store.begin_read() as conn:
            row = conn.execute(
                """
                SELECT attempt.status
                FROM scheduler_attempts AS attempt
                JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
                WHERE attempt.run_id = ?
                  AND effect.effect_kind = 'codex.implementer_bootstrap'
                """,
                (run_id,),
            ).fetchone()
        if row is not None and str(row["status"]) in {"launching", "active"}:
            active = True
            break
    assert active
    result = SchedulerAbortService(
        tick.store,
        backend,
        artifacts=ProtectedArtifactStore(paths["artifact_root"]),
        now_factory=clock,
    ).abort_run(run_id)
    assert result.state_kind == "aborted"
    for _ in range(8):
        tick.run_once()
    return run_id


def _submit_named(repo: Path, paths: dict[str, Path], short_name: str) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(
            SubmitOptions(
                short_name=short_name,
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
                codex_review_model="gpt-5.6-sol",
                codex_review_reasoning_effort="high",
                max_review_iterations=3,
                db_path=paths["db_path"],
                artifact_root=paths["artifact_root"],
            )
        ).run_id


def _tick_until(tick: TickService, run_id: str, kind: str) -> None:
    for _ in range(160):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == kind:
            return
        if state.kind == "blocked" and kind != "blocked":
            raise AssertionError(f"{state.block_reason_kind}: {state.block_reason_summary}")
    raise AssertionError(f"{run_id} did not reach {kind}")


def _rename_project(repo: Path, name: str) -> None:
    path = repo / "ai_dev_loop.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("name: fixture-project", f"name: {name}", 1),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "ai_dev_loop.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "rename project"], cwd=repo, check=True)


def _clone(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, symlinks=True)
    return destination


def _run_id(snapshot: dict[str, object], name: str) -> str:
    return str(_usage(snapshot, name)["run_id"])


def _ids(scope: dict[str, object]) -> list[str]:
    values = scope["run_ids"]
    assert isinstance(values, list)
    return [str(item) for item in values]


def _usage(snapshot: dict[str, object], name: str) -> dict[str, object]:
    usage = _mapping(snapshot["usage"])
    return _mapping(usage[name])


def _direct(snapshot: dict[str, object], name: str) -> dict[str, object]:
    return _scope(_usage(snapshot, name), "direct")


def _recovery(snapshot: dict[str, object], name: str) -> dict[str, object]:
    return _scope(_usage(snapshot, name), "recovery_lineage")


def _scope(payload: object, name: str) -> dict[str, object]:
    return _mapping(_mapping(payload)[name])


def _breakdown(scope: dict[str, object], **expected: str) -> dict[str, object]:
    rows = scope["breakdowns"]
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        if all(item[key] == value for key, value in expected.items()):
            return item
    raise AssertionError(expected)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
