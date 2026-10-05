"""Phase 23.1 cursor recovery evidence unit and schema tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from tests.integration.test_phase17_4_cursor_workflow import _run_until, _submit, _tick_service
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_evidence import (
    verify_correction_envelope_binding,
)
from ai_dev_loop.scheduler.application.cursor_recovery_check import (
    analyze_cursor_recovery_for_inspection,
    scheduler_cursor_recovery_check,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    CursorRecoveryCheckReceipt,
    _attempt_row_matches_completion,
    analyze_cursor_recovery_evidence,
    resolve_decisive_failed_cursor_attempt,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_recovery import _parse_event_row
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256
from ai_dev_loop.scheduler.domain.events import (
    ATTEMPT_COMPLETED_EVENT_KIND,
    STAGING_COMPLETED_EVENT_KIND,
    WAITING_FOR_CURSOR_FIX_EVENT_KIND,
    AttemptCompletedEvent,
    StagingCompletedEvent,
    WaitingForCursorFixEnteredEvent,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "phase23_1_historical"


def _normalize_run_artifact_permissions(artifact_root: Path, run_id: str) -> None:
    root = run_artifact_root(artifact_root, run_id)
    for path in root.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _blocked_initial_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    return run_id


def test_receipt_schema_and_strict_integers() -> None:
    receipt = CursorRecoveryCheckReceipt(
        run_id="run-test",
        evidence_status="authenticated",
        turn_kind="initial",
        reason_code="authenticated_cursor_failure",
        safe_summary="summary",
        sequence_id=None,
        ordinal=None,
    )
    schema = json.loads(
        schema_path("scheduler-cursor-recovery-check-receipt-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(
        json.loads(receipt.model_dump_json())
    )
    with pytest.raises(ValueError):
        CursorRecoveryCheckReceipt(
            run_id="run-test",
            evidence_status="authenticated",
            turn_kind="initial",
            reason_code="authenticated_cursor_failure",
            safe_summary="summary",
            sequence_id=None,
            ordinal=True,
        )
    with pytest.raises(ValueError):
        CursorRecoveryCheckReceipt(
            run_id="run-test",
            evidence_status="authenticated",
            turn_kind="initial",
            reason_code="authenticated_cursor_failure",
            safe_summary="summary",
            sequence_id=None,
            ordinal=None,
            schema_version=1.0,
        )


def test_resolve_decisive_attempt_ambiguous(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        attempt_id, detail, _block_seq = resolve_decisive_failed_cursor_attempt(
            store, conn, run_id, events
        )
    assert attempt_id is not None
    assert detail == "ok"
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.receipt.turn_kind == "initial"
    assert analysis.receipt.recovery_supported is False
    assert analysis.evidence is not None


def test_incomplete_history_pagination(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])

    with patch(
        "ai_dev_loop.scheduler.application.cursor_recovery_evidence._load_all_verified_events",
        return_value=([], False),
    ):
        analysis = analyze_cursor_recovery_evidence(
            store, artifacts, run_id, event_page_size=5
        )
    assert analysis.receipt.evidence_status == "insufficient"
    assert analysis.receipt.reason_code == "incomplete_event_history"


def test_ineligible_when_block_reason_is_not_cursor_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "ineligible"
    assert analysis.receipt.reason_code == "ineligible_block_reason"


def test_check_service_readonly(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    db_path = scheduler_paths["db_path"]
    before_db = db_path.read_bytes()
    receipt = scheduler_cursor_recovery_check(
        run_id,
        db_path=db_path,
        artifact_root=scheduler_paths["artifact_root"],
    )
    assert receipt.evidence_status == "authenticated"
    assert db_path.read_bytes() == before_db


def _blocked_correction_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    return run_id


def test_correction_envelope_embeds_raw_fix_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    fix: WaitingForCursorFixEnteredEvent | None = None
    with store.begin_read() as conn:
        for row in store.list_events_for_run(conn, run_id, limit=500, newest_first=False):
            if str(row["event_kind"]) != WAITING_FOR_CURSOR_FIX_EVENT_KIND:
                continue
            parsed = _parse_event_row(row)
            if isinstance(parsed, WaitingForCursorFixEnteredEvent):
                fix = parsed
    assert fix is not None
    root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    verify_correction_envelope_binding(
        root,
        envelope_path=fix.correction_envelope_path,
        envelope_sha256=fix.correction_envelope_sha256,
        fix_prompt_path=fix.fix_prompt_path,
        fix_prompt_sha256=fix.fix_prompt_sha256,
    )


def _blocked_second_correction_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=60)
    for _ in range(100):
        with tick.store.begin_read() as conn:
            wait_count = sum(
                1
                for row in tick.store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
                if str(row["event_kind"]) == WAITING_FOR_CURSOR_FIX_EVENT_KIND
            )
        if wait_count >= 2:
            break
        tick.run_once()
    else:
        raise AssertionError("run did not reach a second waiting_for_cursor_fix boundary")
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    return run_id


def test_authenticated_second_correction_failure_counts_two_reviews(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_second_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.evidence is not None
    assert analysis.evidence.reviews_completed == 2


def test_extended_correction_failure_authenticates_bundle(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=2)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="max_iterations_reached", max_ticks=120)
    extend = ReviewBudgetExtendService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    extend.extend(run_id, target_total=3)
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=120)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=30)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    analysis = analyze_cursor_recovery_for_inspection(
        run_id,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.evidence is not None
    assert analysis.evidence.iteration == 3
    assert analysis.evidence.reviews_completed == 2
    assert analysis.evidence.effective_review_ceiling == 3


def test_authenticated_correction_failure_counts_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.evidence is not None
    assert analysis.evidence.reviews_completed == 1
    assert analysis.receipt.recovery_supported is False


def test_real_pagination_beyond_first_page(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    artifacts = ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id, event_page_size=3)
    assert analysis.receipt.evidence_status == "authenticated"


def _dual_failed_cursor_usage_limit_retry_fail(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    counter = fake_clis["agent_log"].parent / "phase23-dual-fail-counter.txt"
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "120")
    monkeypatch.setenv("FAKE_AGENT_RUN_COUNTER", str(counter))
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(git_repo, scheduler_paths, now=NOW, backend=backend)
    _run_until(tick, run_id, target_kind="waiting_usage_limit", max_ticks=25)
    tick_late = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW + timedelta(seconds=121),
        backend=backend,
    )
    _run_until(tick_late, run_id, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    return run_id


def test_dual_failure_live_run_reports_ambiguous_insufficient(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _dual_failed_cursor_usage_limit_retry_fail(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        events = list(store.list_events_for_run(conn, run_id, limit=500, newest_first=False))
        attempt_id, detail, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
    assert attempt_id is None
    assert detail == "ambiguous_failed_attempts"
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "insufficient"
    assert analysis.receipt.reason_code == "insufficient_evidence_ambiguous_failure"


def test_dual_failure_replay_from_committed_capture(tmp_path: Path) -> None:
    import shutil

    manifest_path = FIXTURES / "dual_failure_replay_manifest.json"
    assert manifest_path.is_file(), "missing dual_failure_replay_manifest.json fixture"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    engine_src = FIXTURES / str(manifest["engine_sqlite"])
    artifact_src = FIXTURES / str(manifest["artifact_bundle"])
    assert engine_src.is_file(), "missing committed dual-failure engine capture"
    assert artifact_src.is_dir(), "missing committed dual-failure artifact capture"
    db_dst = tmp_path / "engine.sqlite3"
    shutil.copy(engine_src, db_dst)
    shutil.copytree(artifact_src, tmp_path / "artifacts")
    run_id = str(manifest["run_id"])
    store = SqliteSchedulerStore.open_readonly(db_dst)
    artifacts = ReadOnlyProtectedArtifactStore(tmp_path / "artifacts")
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "insufficient"
    assert analysis.receipt.reason_code == "insufficient_evidence_ambiguous_failure"
    assert manifest["expected_resolution"]["detail"] == "ambiguous_failed_attempts"


@pytest.mark.skipif(
    os.environ.get("PHASE23_CAPTURE_FIXTURES") != "1",
    reason="operator capture only",
)
def test_capture_dual_failure_historical_events(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    run_id = _dual_failed_cursor_usage_limit_retry_fail(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = list(store.list_events_for_run(conn, run_id, limit=500, newest_first=False))
        attempt_id, detail, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        failed_ids = []
        for row in events:
            if str(row["event_kind"]) != ATTEMPT_COMPLETED_EVENT_KIND:
                continue
            parsed = _parse_event_row(row)
            if not isinstance(parsed, AttemptCompletedEvent):
                continue
            attempt = store.get_attempt_by_id(conn, parsed.attempt_id)
            if attempt is None:
                continue
            if str(attempt["component"]) != "cursor" or str(attempt["status"]) != "failed":
                continue
            failed_ids.append(parsed.attempt_id)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "event_id": str(row["event_id"]),
            "run_id": str(row["run_id"]),
            "sequence": int(row["sequence"]),
            "event_kind": str(row["event_kind"]),
            "event_payload": str(row["event_payload"]),
            "event_payload_sha256": str(row["event_payload_sha256"]),
            "created_at": str(row["created_at"]),
        }
        for row in events
    ]
    (FIXTURES / "dual_failure_usage_limit_retry_events.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    scenario = {
        "captured_from": "FAKE_AGENT_RUN_SEQUENCE=usage_limit_retry,fail",
        "run_id": run_id,
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
        ).strip(),
        "expected_resolution": {"attempt_id": attempt_id, "detail": detail},
        "failed_attempt_ids_in_order": failed_ids,
        "events_artifact": "dual_failure_usage_limit_retry_events.json",
    }
    (FIXTURES / "dual_failure_usage_limit_retry_scenario.json").write_text(
        json.dumps(scenario, indent=2) + "\n",
        encoding="utf-8",
    )
    import shutil

    bundle_dir = FIXTURES / "dual_failure_artifact_bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    shutil.copytree(scheduler_paths["artifact_root"], bundle_dir)
    shutil.copy(scheduler_paths["db_path"], FIXTURES / "dual_failure_engine.sqlite3")
    replay_manifest = {
        "captured_from": scenario["captured_from"],
        "git_head": scenario["git_head"],
        "run_id": run_id,
        "engine_sqlite": "dual_failure_engine.sqlite3",
        "artifact_bundle": "dual_failure_artifact_bundle",
        "expected_resolution": scenario["expected_resolution"],
    }
    (FIXTURES / "dual_failure_replay_manifest.json").write_text(
        json.dumps(replay_manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def test_dispatch_payload_digest_mismatch_yields_corrupt_receipt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        attempt_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        assert attempt_id is not None
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        dispatch_id = str(attempt["dispatch_id"])
    with store.begin_immediate() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
        assert dispatch is not None
        payload = json.loads(str(dispatch["effect_payload"]))
        payload["iteration"] = int(payload["iteration"]) + 1
        conn.execute(
            "UPDATE scheduler_effects SET effect_payload = ? WHERE dispatch_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), dispatch_id),
        )
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.reason_code == "corrupt_dispatch_evidence"


def test_dispatch_iteration_rebind_with_digest_still_rejects_invocation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        attempt_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        assert attempt_id is not None
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        dispatch_id = str(attempt["dispatch_id"])
    with store.begin_immediate() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
        assert dispatch is not None
        payload = json.loads(str(dispatch["effect_payload"]))
        payload["iteration"] = int(payload["iteration"]) + 1
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        conn.execute(
            """
            UPDATE scheduler_effects
            SET effect_payload = ?, effect_payload_sha256 = ?
            WHERE dispatch_id = ?
            """,
            (payload_text, canonical_json_sha256(payload), dispatch_id),
        )
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.reason_code == "corrupt_dispatch_evidence"


def test_correction_missing_resumed_codex_invocation_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_second_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        codex_attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
    assert codex_attempt is not None
    from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel

    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    invocation_path = run_root / invocation_evidence_rel(str(codex_attempt["attempt_id"]))
    assert invocation_path.is_file(), "expected codex invocation artifact for resumed review"
    invocation_path.unlink()
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.turn_kind == "correction"


def test_correction_resume_session_full_id_mismatch_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_second_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        codex_attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
    assert codex_attempt is not None
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    stdout_path = run_root / str(codex_attempt["stdout_artifact_path"])
    outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    session = str(outcome.get("resume_session_id", "")).strip()
    assert session
    tampered = session[:-1] + ("0" if session[-1] != "0" else "1")
    outcome["resume_session_id"] = tampered
    stdout_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.turn_kind == "correction"


def test_correction_review_iteration_mismatch_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        codex_attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
    assert codex_attempt is not None
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    stdout_path = run_root / str(codex_attempt["stdout_artifact_path"])
    outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    outcome["review_iteration"] = 99
    stdout_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.turn_kind == "correction"


def test_tampered_bootstrap_events_digest_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    binding_path = run_artifact_root(scheduler_paths["artifact_root"], run_id) / (
        "codex/fresh-reviewer-binding.json"
    )
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["bootstrap_events_sha256"] = "0" * 64
    binding_path.write_text(json.dumps(binding, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(binding_path, 0o600)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.turn_kind == "correction"


def _blocked_recovery_successor_multi_correction_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
        _blocked_recovery_fixture,
    )

    tick, source_id, artifacts, store = _blocked_recovery_fixture(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    service = ReviewRetryService(store, artifacts)
    successor_id = service.retry(source_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    _run_until(tick, successor_id, target_kind="waiting_for_cursor_fix", max_ticks=80)
    for _ in range(100):
        with tick.store.begin_read() as conn:
            wait_count = sum(
                1
                for row in tick.store.list_events_for_run(
                    conn, successor_id, limit=500, newest_first=False
                )
                if str(row["event_kind"]) == WAITING_FOR_CURSOR_FIX_EVENT_KIND
            )
        if wait_count >= 2:
            break
        tick.run_once()
    else:
        raise AssertionError("successor did not reach second correction boundary")
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, successor_id, target_kind="blocked", max_ticks=40)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], successor_id)
    return source_id, successor_id


def test_review_recovery_successor_second_correction_failure_authenticated(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_id, successor_id = _blocked_recovery_successor_multi_correction_failure(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, successor_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.evidence is not None
    assert analysis.evidence.reviews_completed >= 2


def test_review_recovery_successor_correction_failure_authenticated(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
        _blocked_recovery_fixture,
    )

    tick, source_id, artifacts, store = _blocked_recovery_fixture(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    service = ReviewRetryService(store, artifacts)
    recovery = service.retry(source_id)
    successor_id = recovery.run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    _run_until(tick, successor_id, target_kind="waiting_for_cursor_fix", max_ticks=80)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, successor_id, target_kind="blocked", max_ticks=30)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], successor_id)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, successor_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.reason_code
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.evidence is not None
    assert analysis.evidence.reviews_completed >= 1


def test_tampered_stderr_yields_corrupt_receipt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        attempt_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        assert attempt_id is not None
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        stderr_rel = str(attempt["stderr_artifact_path"])
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    (run_root / stderr_rel).write_text("tampered stderr\n", encoding="utf-8")
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.reason_code in {
        "corrupt_artifacts",
        "corrupt_artifacts_tampered",
    }


def test_missing_run_artifact_root_is_bounded(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    empty_root = scheduler_paths["artifact_root"].parent / "empty-artifacts"
    empty_root.mkdir()
    artifacts = ReadOnlyProtectedArtifactStore(empty_root)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status in {"insufficient", "corrupt"}
    assert analysis.receipt.reason_code in {
        "insufficient_artifact_store",
        "corrupt_run_artifacts_missing",
        "corrupt_artifacts_missing",
    }


def test_completion_claim_must_match_capacity_claim_id(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_initial_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        decisive_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        assert decisive_id is not None
        completed_row = next(
            row
            for row in events
            if str(row["event_kind"]) == ATTEMPT_COMPLETED_EVENT_KIND
            and decisive_id in str(row["event_payload"])
        )
        completed = _parse_event_row(completed_row)
        assert isinstance(completed, AttemptCompletedEvent)
        assert _attempt_row_matches_completion(store, conn, run_id, completed)
        mismatched = completed.model_copy(update={"claim_id": "claim-does-not-match-capacity"})
        assert not _attempt_row_matches_completion(store, conn, run_id, mismatched)
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "authenticated"


def test_correction_tampered_staged_patch_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    staging: StagingCompletedEvent | None = None
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        for row in events:
            if str(row["event_kind"]) != STAGING_COMPLETED_EVENT_KIND:
                continue
            parsed = _parse_event_row(row)
            if isinstance(parsed, StagingCompletedEvent):
                staging = parsed
    assert staging is not None
    patch_path = run_artifact_root(scheduler_paths["artifact_root"], run_id) / staging.staged_patch_path
    patch_path.write_text("tampered staged patch\n", encoding="utf-8")
    os.chmod(patch_path, 0o600)
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status == "corrupt"
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.receipt.reason_code in {
        "corrupt_correction_evidence",
        "corrupt_artifacts_tampered",
    }


def test_correction_missing_codex_review_result_yields_corrupt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        codex_attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
    assert codex_attempt is not None
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    stdout_path = run_root / str(codex_attempt["stdout_artifact_path"])
    outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    review_rel = str(outcome.get("review_result_path", "")).strip()
    assert review_rel
    (run_root / review_rel).unlink()
    analysis = analyze_cursor_recovery_evidence(
        store,
        ReadOnlyProtectedArtifactStore(scheduler_paths["artifact_root"]),
        run_id,
    )
    assert analysis.receipt.evidence_status in {"corrupt", "insufficient"}
    assert analysis.receipt.turn_kind == "correction"


def test_unknown_run_on_missing_database() -> None:
    missing_db = Path("/tmp/ai-dev-loop-phase23-missing-engine.sqlite3")
    if missing_db.is_file():
        missing_db.unlink()
    with pytest.raises(SchedulerEngineError):
        scheduler_cursor_recovery_check(
            "unknown-run",
            db_path=missing_db,
            artifact_root=Path("/tmp/unused-artifacts"),
        )
