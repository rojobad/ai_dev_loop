"""Phase 23.1 cursor recovery evidence unit and schema tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from tests.integration.test_phase17_4_cursor_workflow import _run_until, _submit, _tick_service

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_recovery_check import (
    scheduler_cursor_recovery_check,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    CursorRecoveryCheckReceipt,
    analyze_cursor_recovery_evidence,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
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
    jsonschema.Draft202012Validator(schema).validate(json.loads(receipt.model_dump_json()))
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


def test_dual_failure_replay_from_committed_capture(tmp_path: Path) -> None:
    import hashlib
    import shutil

    manifest_path = FIXTURES / "dual_failure_replay_manifest.json"
    assert manifest_path.is_file(), "missing dual_failure_replay_manifest.json fixture"
    manifest_before = manifest_path.read_bytes()
    manifest = json.loads(manifest_before.decode("utf-8"))
    engine_src = FIXTURES / str(manifest["engine_sqlite"])
    artifact_src = FIXTURES / str(manifest["artifact_bundle"])
    assert engine_src.is_file(), "missing committed dual-failure engine capture"
    assert artifact_src.is_dir(), "missing committed dual-failure artifact capture"
    engine_before = engine_src.read_bytes()
    for sidecar_name in (engine_src.name + "-wal", engine_src.name + "-shm"):
        sidecar = engine_src.parent / sidecar_name
        if sidecar.is_file():
            shutil.copy(sidecar, tmp_path / sidecar.name)
    db_dst = tmp_path / "engine.sqlite3"
    shutil.copy(engine_src, db_dst)
    shutil.copytree(artifact_src, tmp_path / "artifacts")
    run_id = str(manifest["run_id"])
    _normalize_run_artifact_permissions(tmp_path / "artifacts", run_id)
    store = SqliteSchedulerStore.open_readonly(db_dst)
    artifacts = ReadOnlyProtectedArtifactStore(tmp_path / "artifacts")
    with store.begin_read() as conn:
        events = list(store.list_events_for_run(conn, run_id, limit=500, newest_first=False))
        attempt_count = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        effect_count = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduler_effects WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert events
    assert int(attempt_count["n"]) > 0
    assert int(effect_count["n"]) > 0
    for row in events:
        payload = str(row["event_payload"]).encode("utf-8")
        assert hashlib.sha256(payload).hexdigest() == str(row["event_payload_sha256"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "insufficient"
    assert analysis.receipt.reason_code == "insufficient_evidence_ambiguous_failure"
    assert manifest["expected_resolution"]["detail"] == "ambiguous_failed_attempts"
    assert manifest_path.read_bytes() == manifest_before
    assert engine_src.read_bytes() == engine_before


def test_dual_failure_capture_authenticates_retained_attempt_artifacts(tmp_path: Path) -> None:
    """Ambiguity stays unresolved, while each captured attempt's own bytes authenticate.

    The replay above proves the ledger digest and the ambiguous receipt. It returns
    before any invocation or outcome read, so a missing artifact bundle would still pass.
    """

    import shutil

    from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
    from ai_dev_loop.scheduler.application.cursor_evidence import (
        CursorEvidenceError,
        load_authenticated_cursor_outcome,
        verify_cursor_invocation_evidence,
    )
    from ai_dev_loop.scheduler.domain.cursor_contract import (
        RUN_CURSOR_TURN_EFFECT_KIND,
        invocation_evidence_rel,
    )

    manifest_path = FIXTURES / "dual_failure_replay_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    engine_src = FIXTURES / str(manifest["engine_sqlite"])
    artifact_src = FIXTURES / str(manifest["artifact_bundle"])
    engine_before = engine_src.read_bytes()
    source_bytes = {
        path.relative_to(artifact_src): path.read_bytes()
        for path in artifact_src.rglob("*")
        if path.is_file()
    }
    shutil.copy(engine_src, tmp_path / "engine.sqlite3")
    shutil.copytree(artifact_src, tmp_path / "artifacts")
    run_id = str(manifest["run_id"])
    _normalize_run_artifact_permissions(tmp_path / "artifacts", run_id)
    store = SqliteSchedulerStore.open_readonly(tmp_path / "engine.sqlite3")
    artifacts = ReadOnlyProtectedArtifactStore(tmp_path / "artifacts")
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "insufficient"
    assert analysis.receipt.reason_code == "insufficient_evidence_ambiguous_failure"
    run_root = artifacts.run_root(run_id)

    def authenticate(attempt: object) -> None:
        with store.begin_read() as conn:
            dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
        assert dispatch is not None
        effect_kind = str(dispatch["effect_kind"])
        assert effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        binding = verify_cursor_invocation_evidence(
            run_root,
            attempt_id=str(attempt["attempt_id"]),
            run_id=run_id,
            dispatch_id=str(attempt["dispatch_id"]),
            unit_identity=str(attempt["unit_identity"]),
            launch_nonce=str(attempt["launch_nonce"]),
            launch_intent_sha256=str(attempt["launch_intent_sha256"]),
            effect_kind=effect_kind,
        )
        outcome = load_authenticated_cursor_outcome(
            run_root,
            attempt_id=str(attempt["attempt_id"]),
            unit_identity=str(attempt["unit_identity"]),
            result_rel=str(attempt["result_artifact_path"]),
            stdout_rel=str(attempt["stdout_artifact_path"]),
            stderr_rel=str(attempt["stderr_artifact_path"]),
            observed_exit_code=int(attempt["exit_code"])
            if attempt["exit_code"] is not None
            else None,
            observed_termination=TerminationClass(str(attempt["termination_class"]))
            if attempt["termination_class"]
            else None,
            expected_envelope_sha256=str(attempt["completion_envelope_sha256"])
            if attempt["completion_envelope_sha256"]
            else None,
            expected_dispatch_id=str(attempt["dispatch_id"]),
            expected_effect_kind=effect_kind,
        )
        assert str(binding.get("prompt_path", "")).strip()
        assert str(binding.get("prompt_sha256", "")).strip()
        assert str(outcome.get("dispatch_id", "")) == str(attempt["dispatch_id"])

    with store.begin_read() as conn:
        attempts = [
            row
            for row in conn.execute(
                """
                SELECT * FROM scheduler_attempts
                WHERE run_id = ? AND component = 'cursor' AND status = 'failed'
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
        ]
    assert len(attempts) == 2
    for attempt in attempts:
        for relative in (
            str(attempt["result_artifact_path"]),
            str(attempt["stdout_artifact_path"]),
            str(attempt["stderr_artifact_path"]),
            invocation_evidence_rel(str(attempt["attempt_id"])),
        ):
            assert (run_root / relative).is_file(), relative
        authenticate(attempt)
    missing = run_root / str(attempts[0]["result_artifact_path"])
    missing.unlink()
    with pytest.raises((CursorEvidenceError, OSError)):
        authenticate(attempts[0])
    invocation = run_root / invocation_evidence_rel(str(attempts[1]["attempt_id"]))
    invocation.write_bytes(invocation.read_bytes() + b"x")
    os.chmod(invocation, 0o600)
    with pytest.raises(CursorEvidenceError):
        authenticate(attempts[1])
    assert engine_src.read_bytes() == engine_before
    assert {
        path.relative_to(artifact_src): path.read_bytes()
        for path in artifact_src.rglob("*")
        if path.is_file()
    } == source_bytes


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
