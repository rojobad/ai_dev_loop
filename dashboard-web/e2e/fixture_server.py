"""Real loopback dashboard server over a deterministic temporary scheduler ledger."""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from tests.integration.test_phase26_1_dashboard_queries import (  # noqa: E402
    FIXED,
    _fixture_ledger,
    _insert_attempt,
    _insert_run,
)
from tests.unit.scheduler.helpers import sample_submitted_state  # noqa: E402
from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (  # noqa: E402
    _frozen_sequence_entry,
)

from ai_dev_loop.dashboard.adapters.http import (  # noqa: E402
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.server import DashboardServer  # noqa: E402
from token_fixture import FixtureTokenDashboard  # noqa: E402
from ai_dev_loop.integration_api.errors import IntegrationApiError  # noqa: E402
from ai_dev_loop.integration_api.models import (  # noqa: E402
    IntegrationCodexCapacityData,
    IntegrationCodexCapacityLimit,
    IntegrationContentAvailability,
    IntegrationFrozenArtifactChunk,
    IntegrationProcessOutputAvailability,
    IntegrationProcessOutputChunk,
    IntegrationProcessStreamAvailability,
    IntegrationReviewContentAvailability,
    IntegrationReviewContentChunk,
    IntegrationReviewDetailData,
)
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService  # noqa: E402
from ai_dev_loop.scheduler.application.sequence_materializer import (  # noqa: E402
    frozen_entry_hash,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant, worktree_key  # noqa: E402
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent  # noqa: E402
from ai_dev_loop.scheduler.domain.sequence import (  # noqa: E402
    ActiveSequenceState,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (  # noqa: E402
    SequenceRunAttempt,
)
from ai_dev_loop.scheduler.domain.state import (  # noqa: E402
    SUBMITTED_CONTEXT_SCHEMA_VERSION_IMPLEMENTER_SEQUENCE,
    ControllerBinding,
    ImplementerBinding,
    RepositoryTargetBinding,
    SequenceRunBinding,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore  # noqa: E402

FIXTURE_DIGEST = "a" * 64
MAP_SEQUENCE_ID = "seq-map-replacements"


class HoldingRunReadService:
    """Test-only service wrapper that can keep both production worker slots occupied."""

    def __init__(self, delegate: IntegrationRunReadService, control_root: Path) -> None:
        self._delegate = delegate
        self._control_root = control_root

    def _hold(self, operation: str, run_id: str) -> None:
        if run_id != "older-map-run" or not (self._control_root / "hold-service-jobs").exists():
            return
        (self._control_root / f"held-{operation}-started").write_text("started\n", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not (self._control_root / "release-service-jobs").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture service hold was not released")
            time.sleep(0.01)

    def inspect_run(self, run_id: str):  # type: ignore[no-untyped-def]
        self._hold("inspect", run_id)
        return self._delegate.inspect_run(run_id)

    def list_timeline(self, run_id: str, *, offset: int, limit: int):  # type: ignore[no-untyped-def]
        self._hold("timeline", run_id)
        return self._delegate.list_timeline(run_id, offset=offset, limit=limit)

    def read_initial_prompt_chunk(
        self,
        run_id: str,
        *,
        byte_offset: int,
        limit: int,
    ) -> IntegrationFrozenArtifactChunk:
        content = b"fixture initial run prompt (context only)\n"
        if byte_offset > len(content):
            raise IntegrationApiError.invalid_argument("byteOffset is beyond the artifact size.")
        chunk = content[byte_offset : byte_offset + limit]
        end = byte_offset + len(chunk)
        has_more = end < len(content)
        return IntegrationFrozenArtifactChunk(
            available=True,
            reason=None,
            media_type="text/plain",
            byte_offset=byte_offset,
            returned_bytes=len(chunk),
            next_offset=end if has_more else None,
            available_bytes=len(content),
            has_more=has_more,
            content_base64=base64.b64encode(chunk).decode("ascii"),
            sha256=hashlib.sha256(content).hexdigest(),
            run_id=run_id,
            artifact_kind="initial_prompt",
            source_repository_path="archive/fixture/prompt.txt",
        )


class FixtureReviewReadService:
    """Typed reviewer-only fixture; implementer attempts are rejected."""

    @staticmethod
    def _require_reviewer(run_id: str, attempt_id: str) -> None:
        if run_id != "older-map-run" or attempt_id != "attempt-older-map-run-review-retry":
            raise IntegrationApiError.invalid_argument("Attempt is not a Codex review.")

    def inspect_review(self, run_id: str, *, attempt_id: str) -> IntegrationReviewDetailData:
        self._require_reviewer(run_id, attempt_id)
        available = IntegrationContentAvailability(available=True, reason=None)
        streams = IntegrationProcessOutputAvailability(
            stdout=IntegrationProcessStreamAvailability(
                stream="stdout", available=True, reason=None
            ),
            stderr=IntegrationProcessStreamAvailability(
                stream="stderr", available=True, reason=None
            ),
        )
        return IntegrationReviewDetailData(
            run_id=run_id,
            attempt_id=attempt_id,
            iteration=1,
            phase_attempt=1,
            status="completed",
            review_mode="resume",
            review_model="gpt-6.1-sol",
            reasoning_effort="high",
            created_at="2026-01-03T04:00:00Z",
            launch_requested_at="2026-01-03T04:00:00Z",
            completed_at="2026-01-03T04:15:00Z",
            findings_count=1,
            highest_severity="P1",
            tests_status="passed",
            reviewer_session_ref="reviewer-fixture",
            result_state="valid",
            response=None,
            content=IntegrationReviewContentAvailability(
                prompt=available,
                response=available,
                review_markdown=available,
                cursor_fix_prompt=available,
            ),
            process_output=streams,
        )

    def read_review_content(
        self,
        run_id: str,
        *,
        attempt_id: str,
        content_kind: str,
        byte_offset: int,
        limit: int,
    ) -> IntegrationReviewContentChunk:
        self._require_reviewer(run_id, attempt_id)
        values = {
            "prompt": b"exact reviewer prompt sentinel\n",
            "cursor-fix-prompt": b"review-owned fix prompt sentinel\n",
            "response": b"review response sentinel\n",
            "review-markdown": b"review markdown sentinel\n",
        }
        content = values[content_kind]
        chunk = content[byte_offset : byte_offset + limit]
        end = byte_offset + len(chunk)
        has_more = end < len(content)
        return IntegrationReviewContentChunk(
            available=True,
            reason=None,
            media_type="text/plain",
            byte_offset=byte_offset,
            returned_bytes=len(chunk),
            next_offset=end if has_more else None,
            available_bytes=len(content),
            has_more=has_more,
            content_base64=base64.b64encode(chunk).decode("ascii"),
            sha256=hashlib.sha256(content).hexdigest(),
            run_id=run_id,
            attempt_id=attempt_id,
            content_kind=content_kind,
        )


class FixtureProcessOutputReadService:
    """Bounded fake provider output behind the real HTTP adapter."""

    def __init__(self, control_root: Path) -> None:
        self._control_root = control_root

    def read_process_output(
        self,
        run_id: str,
        *,
        attempt_id: str,
        stream: str,
        byte_offset: int,
        limit: int,
    ) -> IntegrationProcessOutputChunk:
        if (self._control_root / "hold-content-response").exists():
            (self._control_root / "content-response-started").write_text(
                "started\n", encoding="utf-8"
            )
            deadline = time.monotonic() + 10
            while not (self._control_root / "release-content-response").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("fixture content hold was not released")
                time.sleep(0.01)
        if byte_offset > 0 and (self._control_root / "hold-paging-response").exists():
            (self._control_root / "paging-response-started").write_text(
                "started\n", encoding="utf-8"
            )
            deadline = time.monotonic() + 10
            while not (self._control_root / "release-paging-response").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("fixture paging hold was not released")
                time.sleep(0.01)
        component = (
            "codex"
            if attempt_id == "attempt-older-map-run-review-retry"
            else "codex_implementer"
            if "execution" in attempt_id or "active-correction" in attempt_id
            else "cursor"
        )
        if (self._control_root / "paged-content").exists():
            content = b"x" * 65_536 + b"terminal page sentinel\n"
        else:
            content = f"{stream} sentinel for {attempt_id}\n".encode()
        chunk = content[byte_offset : byte_offset + limit]
        end = byte_offset + len(chunk)
        has_more = end < len(content)
        active = "active-correction" in attempt_id
        return IntegrationProcessOutputChunk(
            available=True,
            reason=None,
            media_type="application/x-ndjson" if stream == "stdout" else "text/plain",
            byte_offset=byte_offset,
            returned_bytes=len(chunk),
            next_offset=end if has_more or active else None,
            available_bytes=len(content),
            has_more=has_more,
            content_base64=base64.b64encode(chunk).decode("ascii"),
            sha256=None,
            run_id=run_id,
            attempt_id=attempt_id,
            component=component,
            stream=stream,
            stream_format="jsonl" if stream == "stdout" else "text",
            process_state="active" if active else "completed",
            complete=not active,
            truncated_at_source=None if active else False,
            stored_bytes=None if active else len(content),
        )


def _codex_implementer() -> ImplementerBinding:
    return ImplementerBinding(
        provider="codex",
        command="codex",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        timeout_minutes=90,
        sandbox="workspace-write",
        approval_policy="never",
    )


def _insert_map_sequence(store: SqliteSchedulerStore, now: datetime) -> SequenceRunBinding:
    base_first = _frozen_sequence_entry(
        ordinal=1,
        phase_name="replacement-phase",
        planned_run_id="older-map-run",
        commit_message="replacement fixture checkpoint",
    )
    base_future = _frozen_sequence_entry(
        ordinal=2,
        phase_name="future-unmaterialized-phase",
        planned_run_id="future-map-run",
        commit_message=None,
    )
    implementer = _codex_implementer()

    def provider_entry(base):  # type: ignore[no-untyped-def]
        payload = base.model_dump(mode="python")
        payload.update({"schema_version": 2, "implementer": implementer})
        return type(base).model_validate(payload)

    first = provider_entry(base_first)
    future = provider_entry(base_future)
    repository_root = "/tmp/map-sequence"
    definition = PreparedSequenceDefinition(
        schema_version=2,
        sequence_id=MAP_SEQUENCE_ID,
        name="Authoritative replacement sequence",
        project_name="map-project",
        repository=RepositoryTargetBinding(
            root=repository_root,
            worktree_key=worktree_key(repository_root),
        ),
        manifest_original_artifact_path="sequence/manifest.original.yaml",
        manifest_original_sha256=FIXTURE_DIGEST,
        manifest_resolved_artifact_path="sequence/manifest.resolved.yaml",
        manifest_resolved_sha256=FIXTURE_DIGEST,
        controller=ControllerBinding(),
        entries=(first, future),
    )
    now_text = encode_utc_instant(now)
    prepared = PreparedSequenceState(
        sequence_id=MAP_SEQUENCE_ID,
        version=1,
        prepared_at=now_text,
        updated_at=now_text,
        idempotency_key="b" * 64,
        definition=definition,
    )
    active = ActiveSequenceState(
        sequence_id=MAP_SEQUENCE_ID,
        version=2,
        prepared_at=now_text,
        updated_at=now_text,
        started_at=now_text,
        idempotency_key=prepared.idempotency_key,
        definition=definition,
        current_ordinal=1,
        current_run_id="current-map-run",
        materialized_entries=(
            MaterializedSequenceEntry(
                ordinal=1,
                run_id="current-map-run",
                entry_hash=frozen_entry_hash(first),
                materialized_at=now_text,
            ),
        ),
    )
    state_kind, payload, payload_digest = store.dump_sequence_state(active)
    with store.begin_immediate() as conn:
        store.insert_prepared_sequence(conn, state=prepared, now=now)
        conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?, version = ?, updated_at = ?
            WHERE sequence_id = ?
            """,
            (state_kind, payload, payload_digest, active.version, now_text, MAP_SEQUENCE_ID),
        )
        store.insert_sequence_run_attempt(
            conn,
            sequence_id=MAP_SEQUENCE_ID,
            ordinal=1,
            planned_run_id="older-map-run",
            attempt=SequenceRunAttempt(
                schema_version=2,
                generation=1,
                run_id="older-map-run",
                source_run_id=None,
                attempt_kind="planned_run",
                materialized_at=now_text,
                terminal_outcome="blocked",
                resolved_at=now_text,
            ),
        )
        store.insert_sequence_run_attempt(
            conn,
            sequence_id=MAP_SEQUENCE_ID,
            ordinal=1,
            planned_run_id="older-map-run",
            attempt=SequenceRunAttempt(
                schema_version=2,
                generation=2,
                run_id="current-map-run",
                source_run_id="older-map-run",
                attempt_kind="implementer_retry",
                materialized_at=now_text,
                terminal_outcome=None,
                resolved_at=None,
            ),
        )
    return SequenceRunBinding(
        sequence_id=MAP_SEQUENCE_ID,
        ordinal=1,
        total_phases=2,
        entry_hash=frozen_entry_hash(first),
    )


def _bind_run_to_sequence(
    store: SqliteSchedulerStore,
    run_id: str,
    binding: SequenceRunBinding,
) -> None:
    with store.begin_immediate() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        context = state.context.model_copy(
            update={
                "schema_version": SUBMITTED_CONTEXT_SCHEMA_VERSION_IMPLEMENTER_SEQUENCE,
                "sequence": binding,
            }
        )
        bound = state.model_copy(update={"context": context})
        state_kind, payload, payload_digest = store.dump_state(bound)
        conn.execute(
            """
            UPDATE scheduler_runs
            SET state_kind = ?, state_payload = ?, state_payload_sha256 = ?
            WHERE run_id = ?
            """,
            (state_kind, payload, payload_digest, run_id),
        )


class FixtureCapacityService:
    """Deterministic fake provider boundary; no model or account process is invoked."""

    def observe(self) -> IntegrationCodexCapacityData:
        return IntegrationCodexCapacityData(
            status="available",
            reason=None,
            limits=(
                IntegrationCodexCapacityLimit(
                    id="primary",
                    window="5-hour window",
                    usedPercent=36,
                    remainingPercent=64,
                    windowDurationMinutes=300,
                    resetsAt="2026-01-03T02:00:00Z",
                ),
                IntegrationCodexCapacityLimit(
                    id="weekly",
                    window="Weekly window",
                    usedPercent=73,
                    remainingPercent=27,
                    windowDurationMinutes=None,
                    resetsAt=None,
                ),
            ),
        )


def _insert_large_cohort(store: SqliteSchedulerStore, count: int) -> None:
    """Insert the browser paging fixture in one isolated transaction."""

    base = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        for index in range(count):
            run_id = f"large-fixture-run-{index:04d}"
            event_id = f"event-{run_id}"
            submitted_at = base + timedelta(seconds=index)
            state = sample_submitted_state(run_id=run_id, repo_root=f"/tmp/{run_id}")
            context = state.context.model_copy(update={"project_name": "large-project"})
            state = state.model_copy(
                update={
                    "context": context,
                    "idempotency_key": hashlib.sha256(run_id.encode()).hexdigest(),
                    "submitted_at": encode_utc_instant(submitted_at),
                    "updated_at": encode_utc_instant(submitted_at),
                }
            )
            store.insert_submitted_run(
                conn,
                run_id=run_id,
                state=state,
                event_id=event_id,
                event=RunSubmittedEvent(
                    run_id=run_id,
                    idempotency_key=state.idempotency_key,
                    worktree_key=state.context.repository.worktree_key,
                    reused_existing=False,
                ),
                now=submitted_at,
                short_name=f"Large alias {index:04d} {'a' * 18}",
            )
            launch = base + timedelta(hours=1, seconds=index)
            completed = launch + timedelta(minutes=1)
            dispatch_id = f"dispatch-{run_id}"
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?, ?, 'cursor.run_turn', '{}', ?, 'succeeded', ?, ?, ?)
                """,
                (
                    dispatch_id,
                    event_id,
                    run_id,
                    f"effect-{run_id}",
                    f"{run_id}:turn",
                    "d" * 64,
                    encode_utc_instant(launch),
                    encode_utc_instant(launch),
                    encode_utc_instant(launch),
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at, ingested
                ) VALUES (?, ?, ?, 'cursor', 1, 'completed', ?, ?, ?, ?, 1)
                """,
                (
                    f"attempt-{run_id}",
                    run_id,
                    dispatch_id,
                    encode_utc_instant(launch),
                    encode_utc_instant(completed),
                    encode_utc_instant(launch),
                    encode_utc_instant(completed),
                ),
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--large-cohort", type=int, default=0)
    args = parser.parse_args()

    xdg_state = args.state_root / "state"
    db_path = _fixture_ledger(xdg_state)
    store = SqliteSchedulerStore(db_path)
    malicious_project = '<img src=x onerror="window.__dashboard_xss=1">'
    event_id = _insert_run(
        store,
        "literal-label-run",
        project_name=malicious_project,
        submitted_at=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
    )
    _insert_attempt(
        store,
        run_id="literal-label-run",
        event_id=event_id,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED + timedelta(hours=1, minutes=10),
        completed=FIXED + timedelta(hours=1, minutes=20),
    )

    map_start = FIXED + timedelta(days=2)
    map_sequence_binding = _insert_map_sequence(store, map_start)
    for index in range(28):
        run_id = (
            "older-map-run"
            if index == 0
            else "current-map-run"
            if index == 1
            else f"map-run-{index:02d}"
        )
        event_id = _insert_run(
            store,
            run_id,
            project_name="map-project",
            submitted_at=map_start + timedelta(minutes=index),
            provider="codex" if index in {0, 1} else "cursor",
        )
        if index in {0, 1}:
            _bind_run_to_sequence(store, run_id, map_sequence_binding)
        _insert_attempt(
            store,
            run_id=run_id,
            event_id=event_id,
            suffix="execution",
            component="codex_implementer" if index in {0, 1} else "cursor",
            effect_kind=("codex.implementer_bootstrap" if index in {0, 1} else "cursor.run_turn"),
            iteration=1,
            launch=map_start + timedelta(hours=1, minutes=index),
            completed=map_start + timedelta(hours=1, minutes=index + 10),
        )
        if index != 0:
            continue
        for attempt_index in range(24):
            _insert_attempt(
                store,
                run_id=run_id,
                event_id=event_id,
                suffix=f"non-execution-{attempt_index:02d}",
                component="cursor",
                effect_kind="cursor.create_chat",
                iteration=1,
                launch=map_start + timedelta(hours=2, minutes=attempt_index),
                completed=map_start + timedelta(hours=2, minutes=attempt_index + 1),
            )
        _insert_attempt(
            store,
            run_id=run_id,
            event_id=event_id,
            suffix="review-retry",
            component="codex",
            effect_kind="codex.resume_review",
            iteration=1,
            launch=map_start + timedelta(hours=4),
            completed=map_start + timedelta(hours=4, minutes=15),
        )
        _insert_attempt(
            store,
            run_id=run_id,
            event_id=event_id,
            suffix="active-correction",
            component="codex_implementer",
            effect_kind="codex.implementer_resume",
            iteration=2,
            launch=map_start + timedelta(hours=5),
            completed=None,
            status="active",
            ingested=False,
        )

    if args.large_cohort:
        _insert_large_cohort(store, args.large_cohort)

    artifact_root = xdg_state / "ai_dev_loop" / "artifacts"
    factories = replace(
        default_http_service_factories(db_path=db_path, artifact_root=artifact_root),
        capacity=lambda: FixtureCapacityService(),
        review=lambda: FixtureReviewReadService(),
        process_output=lambda: FixtureProcessOutputReadService(args.state_root),
    )
    base_run_factory = factories.run
    base_dashboard_factory = factories.dashboard
    factories = replace(
        factories,
        dashboard=lambda: FixtureTokenDashboard(base_dashboard_factory(), args.state_root),
        run=lambda: HoldingRunReadService(base_run_factory(), args.state_root),
    )
    dispatcher = DashboardHttpDispatcher(factories)
    server = DashboardServer(args.port, dispatcher=dispatcher)
    try:
        server.serve_forever()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
