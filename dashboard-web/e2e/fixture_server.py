"""Real loopback dashboard server over a deterministic temporary scheduler ledger."""

from __future__ import annotations

import argparse
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
from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (  # noqa: E402
    _frozen_sequence_entry,
)

from ai_dev_loop.dashboard.adapters.http import (  # noqa: E402
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.server import DashboardServer  # noqa: E402
from ai_dev_loop.integration_api.models import (  # noqa: E402
    IntegrationCodexCapacityData,
    IntegrationCodexCapacityLimit,
)
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService  # noqa: E402
from ai_dev_loop.scheduler.application.sequence_materializer import (  # noqa: E402
    frozen_entry_hash,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant, worktree_key  # noqa: E402
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
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

    artifact_root = xdg_state / "ai_dev_loop" / "artifacts"
    factories = replace(
        default_http_service_factories(db_path=db_path, artifact_root=artifact_root),
        capacity=lambda: FixtureCapacityService(),
    )
    base_run_factory = factories.run
    factories = replace(
        factories,
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
