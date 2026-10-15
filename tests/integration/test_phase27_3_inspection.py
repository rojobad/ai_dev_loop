"""Phase 27.3 provider-aware, private and authenticated integration inspection."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick,
    drive,
    prepare,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    scheduler_paths as _scheduler_paths,
)

from ai_dev_loop.commands.scheduler import render_sequence_status_output, render_status_output
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.models import IntegrationRunInspectData, IntegrationRunListData
from ai_dev_loop.integration_api.process_output_service import IntegrationProcessOutputReadService
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService
from ai_dev_loop.integration_api.schemas import validate_integration_instance
from ai_dev_loop.integration_api.sequence_service import IntegrationSequenceReadService
from ai_dev_loop.scheduler.application.history import SchedulerHistoryService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.application.timeline import SchedulerTimelineService
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

scheduler_paths = _scheduler_paths


@pytest.mark.parametrize("turn", ["initial", "correction"])
def test_inspection_wait_provider_role_privacy_and_mutation_free_reads(
    git_repo, scheduler_paths, monkeypatch, turn
):
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_SEQUENCE",
        "usage_limit,success" if turn == "initial" else "success,usage_limit,success",
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_SEQUENCE", "no_findings" if turn == "initial" else "findings,no_findings"
    )
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    waiting = drive(
        _tick(git_repo, scheduler_paths),
        scheduler_paths,
        result.sequence_id,
        monkeypatch,
        target="waiting_codex_capacity",
    )
    before = scheduler_paths["db_path"].read_bytes()
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    runs = IntegrationRunReadService(store, artifact_root=scheduler_paths["artifact_root"])
    seqs = IntegrationSequenceReadService(store, artifact_root=scheduler_paths["artifact_root"])
    inspection = runs.inspect_run(waiting.run_id)
    wire = inspection.model_dump(mode="json", by_alias=True)
    assert wire["implementerProvider"] == "codex"
    assert wire["codexPendingRole"] == "implementer" and wire["codexPendingTurn"] == turn
    assert wire["codexCapacityReason"] and wire["safeNextAction"]["kind"] == "scheduler_tick"
    validate_integration_instance(wire, "integration-api-run-inspect-data-v1.json")
    seq = seqs.inspect_sequence(result.sequence_id)
    assert seq.current_implementer_provider == "codex"
    assert seq.codex_pending_role == "implementer" and seq.codex_pending_turn == turn
    assert [p.implementer_provider for p in seq.phases] == ["codex", "cursor"]
    validate_integration_instance(
        seq.model_dump(mode="json", by_alias=True), "integration-api-sequence-inspect-data-v1.json"
    )
    assert len(runs.list_runs(kind="sequence").items) == 1
    assert not runs.list_runs(kind="standalone").items
    history = SchedulerHistoryService(store).get_history(waiting.run_id)
    assert any(f"role=implementer turn={turn}" in entry.safe_detail for entry in history.entries)
    timeline = SchedulerTimelineService(store).get_timeline(waiting.run_id)
    assert any(e.phase == "codex_implementer" for e in timeline.entries)
    status = SchedulerStatusService(store).get_status(waiting.run_id)
    seqstatus = SequenceStatusService(store).get_status(result.sequence_id)
    summary_text = "\n".join(
        [
            json.dumps(wire),
            seq.model_dump_json(),
            history.model_dump_json(),
            timeline.model_dump_json(),
            render_status_output(status, output="text"),
            render_status_output(status, output="json"),
            render_sequence_status_output(seqstatus, output="text"),
            render_sequence_status_output(seqstatus, output="json"),
            runs.list_history(waiting.run_id).model_dump_json(),
            runs.list_attempts(waiting.run_id).model_dump_json(),
        ]
    )
    assert waiting.codex.implementer_session_id not in summary_text
    if waiting.codex.reviewer_session_id:
        assert waiting.codex.reviewer_session_id not in summary_text
    assert "Fix issue ALPHA" not in summary_text
    assert "execution-envelope" not in summary_text and "thread.started" not in summary_text
    assert scheduler_paths["db_path"].read_bytes() == before
    caps = build_integration_info_data().capabilities
    assert caps.dual_implementers and caps.mixed_implementer_sequences
    assert caps.codex_implementer_manual_recovery


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_codex_implementer_content_is_attempt_owned_bounded_and_authenticated(
    git_repo, scheduler_paths, monkeypatch, stream
):
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    monkeypatch.setenv("FAKE_CODEX_CHILD_STDERR_SENTINEL", "private diagnostic bytes")
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    waiting = drive(
        tick, scheduler_paths, result.sequence_id, monkeypatch, target="waiting_codex_capacity"
    )
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        attempt = store.get_attempt_by_id(
            conn, waiting.codex.capacity_continuation.terminated_attempt_id
        )
    assert attempt is not None
    attempt_id = str(attempt["attempt_id"])
    reader = IntegrationProcessOutputReadService(
        store, artifact_root=scheduler_paths["artifact_root"]
    )
    before = scheduler_paths["db_path"].read_bytes()
    chunk = reader.read_process_output(
        waiting.run_id, attempt_id=attempt_id, stream=stream, limit=7
    )
    assert chunk.available and chunk.complete and chunk.has_more
    assert chunk.component == "codex_implementer"
    assert chunk.returned_bytes == 7 and chunk.next_offset == 7
    assert chunk.truncated_at_source is False
    assert b'"role": "implementer"' not in base64.b64decode(chunk.content_base64)
    validate_integration_instance(
        chunk.model_dump(mode="json", by_alias=True), "integration-api-process-output-chunk-v1.json"
    )
    with pytest.raises(IntegrationApiError):
        reader.read_process_output(
            waiting.run_id, attempt_id=attempt_id, stream=stream, byte_offset=-1
        )
    with pytest.raises(IntegrationApiError):
        reader.read_process_output("other-run", attempt_id=attempt_id, stream=stream)
    root = tick.artifacts.run_root(waiting.run_id)
    side = "events_path" if stream == "stdout" else "stderr_path"
    outcome = json.loads((root / str(attempt["stdout_artifact_path"])).read_text())
    path = root / outcome[side]
    exact = path.read_bytes()
    all_bytes = bytearray()
    offset = 0
    while True:
        part = reader.read_process_output(
            waiting.run_id, attempt_id=attempt_id, stream=stream, byte_offset=offset, limit=13
        )
        all_bytes.extend(base64.b64decode(part.content_base64))
        if not part.has_more:
            break
        offset = part.next_offset
    assert bytes(all_bytes) == exact
    path.write_bytes(exact + b"tampered")
    with pytest.raises(IntegrationApiError) as err:
        reader.read_process_output(waiting.run_id, attempt_id=attempt_id, stream=stream)
    assert err.value.code == "DATA_INTEGRITY"
    assert scheduler_paths["db_path"].read_bytes() == before


def test_historical_consumers_absent_provider_and_additive_fields():
    fixtures = Path(__file__).parents[1] / "fixtures/integration_api/v1_1"
    inspect = IntegrationRunInspectData.model_validate_json(
        (fixtures / "run_inspect_data_minimal.json").read_text()
    )
    assert inspect.implementer_provider == "cursor" and inspect.codex_pending_role is None
    payload = json.loads((fixtures / "run_list_data_minimal.json").read_text())
    assert IntegrationRunListData.model_validate_json(json.dumps(payload)).items == ()
