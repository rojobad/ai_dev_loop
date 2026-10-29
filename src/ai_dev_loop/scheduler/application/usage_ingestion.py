"""Persist authenticated usage facts without changing operational authority."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from ai_dev_loop.integration_api.process_output_auth import (
    ProcessOutputIntegrityError,
    authenticate_process_attempt,
)
from ai_dev_loop.scheduler.application.token_usage_query import (
    _INFERENCE_EFFECTS,
    _attempt_from_authenticated,
    _historical_codex_observations,
    _load_published_observations,
    _published_contribution_complete,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.token_usage import UsageObservationV1
from ai_dev_loop.scheduler.domain.usage_facts import (
    USAGE_FACT_SCHEMA_VERSION,
    UsageBindingKind,
    UsageFactPayloadV1,
    UsageImportOrigin,
    UsageObservationCountersV1,
    UsageSettlement,
    UsageTerminalStatus,
    UsageWriteResult,
    canonical_json,
    canonical_sha256,
    observation_relation,
    require_relative_usage_path,
    require_sha256,
    safe_diagnostic_codes,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_RECONCILE_LIMIT = 32
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "uncertain"}
_WRITE_FAILED_DIAGNOSTIC = "usage_statistics_write_failed"


class UsageIngestionBarrier(Exception):
    """Deterministic crash barrier around the usage commit boundary."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


def bind_usage_artifacts(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore | None,
) -> None:
    if artifacts is not None:
        store.usage_artifacts = artifacts


@dataclass(frozen=True)
class _ObservationWrite:
    counters: UsageObservationCountersV1
    logical_attempt_id: str
    run_id: str
    provider: Literal["cursor", "codex"]
    role: Literal["implementer", "reviewer"]
    stage: Literal["initial", "correction", "review"]
    native_key: str
    publisher_attempt_id: str
    source_dispatch_id: str
    source_relative_path: str
    source_digest: str
    source_format: str
    source_event_ordinal: int
    source_event_offsets: tuple[str, ...]
    import_origin: UsageImportOrigin
    completion_fence_id: str | None
    binding_kind: UsageBindingKind
    binding_sha256: str


@dataclass(frozen=True)
class _FactWrite:
    payload: UsageFactPayloadV1
    source_paths: tuple[str, ...]
    source_digests: tuple[str, ...]
    publisher_attempt_id: str
    source_dispatch_id: str
    completion_fence_id: str | None
    binding_kind: UsageBindingKind
    binding_sha256: str
    import_origin: UsageImportOrigin


def settle_ingested_attempt_usage(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    now: datetime,
) -> None:
    """Record facts or a stats-only obligation before this ingest can be settled."""

    if not store.schema_supports_usage_statistics(conn):
        return
    wrote = False
    try:
        conn.execute("SAVEPOINT usage_statistics")
        wrote = _settle_inside(store, artifacts, conn, attempt_id=attempt_id, now=now)
        conn.execute("RELEASE SAVEPOINT usage_statistics")
    except UsageIngestionBarrier:
        _rollback_savepoint(conn, "usage_statistics")
        raise
    except sqlite3.Error:
        _rollback_savepoint(conn, "usage_statistics")
        with suppress(Exception):
            conn.execute("SAVEPOINT usage_statistics_obligation")
            wrote = _retain_statistics_write_obligation(
                store,
                conn,
                publisher_attempt_id=attempt_id,
                now=now,
            )
            conn.execute("RELEASE SAVEPOINT usage_statistics_obligation")
    except Exception:
        _rollback_savepoint(conn, "usage_statistics")
        with suppress(Exception):
            conn.execute("SAVEPOINT usage_statistics_debt")
            logical_attempt_id = _logical_attempt_for_publisher(store, conn, attempt_id)
            wrote = _write_ledger_obligation(
                store,
                conn,
                attempt_id=logical_attempt_id or attempt_id,
                now=now,
                settlement="pending",
                diagnostic="attempt_usage_evidence_unavailable",
                publisher_attempt_id=attempt_id,
            )
            conn.execute("RELEASE SAVEPOINT usage_statistics_debt")
    if wrote and store.usage_ingestion_barrier == "before_commit":
        store.usage_ingestion_barrier = None
        raise UsageIngestionBarrier("before_commit")
    if not wrote:
        return
    if store.usage_ingestion_barrier == "before_ack":
        store.usage_ingestion_barrier = None
        return
    store.schedule_after_commit(lambda: acknowledge_settled_usage(store, now))


def acknowledge_settled_usage(store: SqliteSchedulerStore, now: datetime) -> None:
    with store.begin_immediate() as conn:
        store.acknowledge_classified_usage_facts(conn, now_text=encode_utc_instant(now))


def reconcile_unacknowledged_usage(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    now: datetime,
) -> None:
    """Finish stats-only obligations. This does not dispatch a prompt."""

    if store.usage_artifacts is None:
        store.usage_artifacts = artifacts
    with store.begin_immediate() as conn:
        if not store.schema_supports_usage_statistics(conn):
            return
        pending = store.list_unacknowledged_usage_facts(conn, limit=_RECONCILE_LIMIT)
        for row in pending:
            if str(row["settlement_status"]) != "pending":
                continue
            logical_attempt_id = str(row["logical_attempt_id"])
            publisher_attempt_id = str(row["publisher_attempt_id"])
            try:
                conn.execute("SAVEPOINT usage_statistics_retry")
                _settle_inside(
                    store,
                    artifacts,
                    conn,
                    attempt_id=publisher_attempt_id,
                    now=now,
                    logical_attempt_id=logical_attempt_id,
                )
                conn.execute("RELEASE SAVEPOINT usage_statistics_retry")
            except sqlite3.Error:
                _rollback_savepoint(conn, "usage_statistics_retry")
                with suppress(Exception):
                    _retain_statistics_write_obligation(
                        store,
                        conn,
                        publisher_attempt_id=publisher_attempt_id,
                        now=now,
                    )
                continue
            except Exception:
                _rollback_savepoint(conn, "usage_statistics_retry")
            refreshed = store.get_usage_attempt_fact(conn, logical_attempt_id)
            if refreshed is not None and str(refreshed["settlement_status"]) == "pending":
                _mark_debt_if_terminal(
                    store,
                    conn,
                    logical_attempt_id=logical_attempt_id,
                    now=now,
                )
        store.acknowledge_classified_usage_facts(conn, now_text=encode_utc_instant(now))


def merge_usage_snapshot(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    fact: _FactWrite,
    observations: tuple[_ObservationWrite, ...],
    now: datetime,
) -> UsageWriteResult:
    """Idempotent fact/observation write used by ingestion and store contracts."""

    now_text = encode_utc_instant(now)
    results: list[UsageWriteResult] = []
    kept_ids: list[str] = []
    conflict = False
    for observation in observations:
        result = _merge_observation(store, conn, observation, now_text=now_text)
        results.append(result)
        if result == "conflict":
            conflict = True
        else:
            kept_ids.append(observation.counters.observation_id)
    existing = store.get_usage_attempt_fact(conn, fact.payload.logical_attempt_id)
    if existing is not None:
        for item in json.loads(str(existing["fact_payload"])).get("observation_ids", []):
            if isinstance(item, str) and item not in kept_ids:
                kept_ids.append(item)
    payload = fact.payload.model_copy(update={"observation_ids": tuple(sorted(set(kept_ids)))})
    if conflict:
        codes = tuple(dict.fromkeys((*payload.diagnostic_codes, "usage_payload_conflict")))
        retained = _stored_coverage(None if existing is None else existing["coverage"])
        payload = payload.model_copy(
            update={
                "diagnostic_codes": codes,
                "settlement_status": "conflict",
                "coverage": retained,
            }
        )
    result = _merge_fact(store, conn, fact, payload, now_text=now_text)
    results.append(result)
    if "conflict" in results:
        return "conflict"
    if "enriched" in results:
        return "enriched"
    if "inserted" in results:
        return "inserted"
    return "replayed"


def _settle_inside(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    now: datetime,
    logical_attempt_id: str | None = None,
) -> bool:
    attempt = store.get_attempt_by_id(conn, attempt_id)
    if attempt is None:
        return False
    effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if effect is None:
        return False
    effect_kind = str(effect["effect_kind"])
    if (
        effect_kind not in _INFERENCE_EFFECTS
        and effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
    ):
        return False
    status = str(attempt["status"])
    if status not in _TERMINAL_STATUSES:
        return False
    readonly = ReadOnlyProtectedArtifactStore(artifacts.artifact_root)
    try:
        run_root = readonly.run_root(str(attempt["run_id"]))
        authenticated = authenticate_process_attempt(run_root, attempt, effect_kind=effect_kind)
    except (ProcessOutputIntegrityError, ValueError, OSError, TypeError, KeyError):
        return _write_ledger_obligation(
            store,
            conn,
            attempt_id=logical_attempt_id or attempt_id,
            now=now,
            settlement="pending",
            diagnostic="attempt_usage_evidence_unavailable",
        )
    invocation = authenticated.invocation
    outcome = authenticated.outcome
    source_attempt_id = logical_attempt_id or _logical_attempt_id(
        effect_kind, attempt_id, invocation
    )
    logical = (
        attempt
        if source_attempt_id == attempt_id
        else store.get_attempt_by_id(conn, source_attempt_id)
    )
    if logical is None:
        return False
    logical_effect = (
        effect
        if source_attempt_id == attempt_id
        else store.get_effect_by_dispatch_id(conn, str(logical["dispatch_id"]))
    )
    if logical_effect is None or str(logical_effect["effect_kind"]) not in _INFERENCE_EFFECTS:
        return False
    if (
        effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        and outcome is not None
        and outcome.get("failure_kind") == "cursor_sdk_preflight_failed"
        and not outcome.get("native_run_id")
    ):
        return _write_classified_without_counters(
            store,
            conn,
            attempt=logical,
            publisher_attempt_id=attempt_id,
            now=now,
            coverage="not_applicable",
            settlement="settled",
            diagnostic=None,
            import_origin="ingestion",
            configured_model=None,
        )
    try:
        logical_root = readonly.run_root(str(logical["run_id"]))
        logical_auth = (
            authenticated
            if source_attempt_id == attempt_id
            else authenticate_process_attempt(
                logical_root,
                logical,
                effect_kind=str(logical_effect["effect_kind"]),
            )
        )
    except (ProcessOutputIntegrityError, ValueError, OSError, TypeError, KeyError):
        return _write_ledger_obligation(
            store,
            conn,
            attempt_id=source_attempt_id,
            now=now,
            settlement="pending",
            diagnostic="attempt_usage_evidence_unavailable",
        )
    projected = _attempt_from_authenticated(
        logical,
        logical_auth.invocation,
        logical_auth.outcome,
        effect_kind=str(logical_effect["effect_kind"]),
    )
    if projected is None:
        return False
    diagnostics = list(safe_diagnostic_codes((outcome or {}).get("usage_diagnostics", [])))
    observations, incomplete = _load_published_observations(
        artifacts=readonly,
        publisher=projected if effect_kind in _INFERENCE_EFFECTS else None,
        publisher_attempt_id=attempt_id,
        publisher_effect_kind=effect_kind,
        publisher_invocation=invocation,
        run_id=str(logical["run_id"]),
        outcome=outcome or {},
        attempts={projected.attempt_id: projected},
        diagnostics=diagnostics,
    )
    raw_bindings = (outcome or {}).get("usage_observations", [])
    bound_count = (
        sum(1 for item in raw_bindings if isinstance(item, dict))
        if isinstance(raw_bindings, list)
        else 0
    )
    binding_gap = bool(incomplete) or bound_count > len(observations)
    if projected.provider == "codex" and (not observations or binding_gap):
        historical, historical_complete = _historical_codex_observations(
            artifacts=readonly,
            attempt=projected,
            diagnostics=diagnostics,
        )
    else:
        historical, historical_complete = [], None
    seen = {item.observation_id for item in observations}
    for item in historical:
        if item.observation_id not in seen:
            observations.append(item)
            seen.add(item.observation_id)
    recovered_gap = False
    published_gap_closed = (
        binding_gap
        and bound_count > 0
        and len(historical) >= bound_count
        and {item.observation_id for item in historical} <= seen
    )
    if published_gap_closed:
        binding_gap = False
        recovered_gap = True
        incomplete = set()
        diagnostics = [code for code in diagnostics if code != "usage_observation_integrity_error"]
    if binding_gap and not observations:
        return _write_classified_without_counters(
            store,
            conn,
            attempt=logical,
            publisher_attempt_id=attempt_id,
            now=now,
            coverage=None,
            settlement="debt",
            diagnostic="usage_source_debt",
            import_origin=_origin(effect_kind),
            configured_model=projected.configured_model,
            extra_diagnostics=tuple(diagnostics),
        )
    binding_kind, binding_sha = _binding(logical)
    fence = _fence(logical)
    origin = _origin(effect_kind)
    writes: list[_ObservationWrite] = []
    for item in observations:
        try:
            writes.append(
                _observation_write(
                    item,
                    publisher_attempt_id=attempt_id,
                    import_origin=origin,
                    completion_fence_id=fence,
                    binding_kind=binding_kind,
                    binding_sha256=binding_sha,
                )
            )
        except ValueError:
            diagnostics.append("usage_observation_integrity_error")
    if binding_gap and not writes:
        return _write_classified_without_counters(
            store,
            conn,
            attempt=logical,
            publisher_attempt_id=attempt_id,
            now=now,
            coverage=None,
            settlement="debt",
            diagnostic="usage_source_debt",
            import_origin=origin,
            configured_model=projected.configured_model,
            extra_diagnostics=tuple(diagnostics),
        )
    if binding_gap:
        coverage = None
        settlement_status: UsageSettlement = "debt"
        diagnostics.append("usage_source_debt")
    else:
        contribution_complete = _published_contribution_complete(outcome)
        if recovered_gap and historical_complete:
            contribution_complete = True
        elif contribution_complete is None:
            contribution_complete = historical_complete
        coverage = _coverage(
            observations=writes,
            contribution_complete=bool(contribution_complete) and not incomplete,
            incomplete=bool(incomplete),
        )
        settlement_status = "settled"
    payload = UsageFactPayloadV1(
        logical_attempt_id=str(logical["attempt_id"]),
        run_id=str(logical["run_id"]),
        provider=projected.provider,
        role=projected.role,
        stage=projected.stage,
        configured_model=projected.configured_model,
        terminal_status=_terminal_status(str(logical["status"])),
        coverage=coverage,
        settlement_status=settlement_status,
        diagnostic_codes=tuple(dict.fromkeys(safe_diagnostic_codes(diagnostics))),
        import_origin=origin,
    )
    paths = tuple(item.source_relative_path for item in writes)
    digests = tuple(item.source_digest for item in writes)
    merge_usage_snapshot(
        store,
        conn,
        fact=_FactWrite(
            payload=payload,
            source_paths=paths,
            source_digests=digests,
            publisher_attempt_id=attempt_id,
            source_dispatch_id=str(logical["dispatch_id"]),
            completion_fence_id=fence,
            binding_kind=binding_kind,
            binding_sha256=binding_sha,
            import_origin=origin,
        ),
        observations=tuple(writes),
        now=now,
    )
    return True


def _write_ledger_obligation(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    now: datetime,
    settlement: UsageSettlement,
    diagnostic: str,
    publisher_attempt_id: str | None = None,
) -> bool:
    attempt = store.get_attempt_by_id(conn, attempt_id)
    if attempt is None:
        return False
    effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if effect is None or str(effect["effect_kind"]) not in _INFERENCE_EFFECTS:
        return False
    existing = store.get_usage_attempt_fact(conn, attempt_id)
    if existing is not None and str(existing["settlement_status"]) in {
        "settled",
        "conflict",
        "debt",
    }:
        return False
    return _write_classified_without_counters(
        store,
        conn,
        attempt=attempt,
        publisher_attempt_id=publisher_attempt_id or attempt_id,
        now=now,
        coverage=None,
        settlement=settlement,
        diagnostic=diagnostic,
        import_origin="ingestion",
        configured_model=None
        if existing is None or existing["configured_model"] is None
        else str(existing["configured_model"]),
    )


def _retain_statistics_write_obligation(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    publisher_attempt_id: str,
    now: datetime,
) -> bool:
    logical_attempt_id = _logical_attempt_for_publisher(store, conn, publisher_attempt_id)
    if logical_attempt_id is None:
        return False
    attempt = store.get_attempt_by_id(conn, logical_attempt_id)
    if attempt is None:
        return False
    publisher = store.get_attempt_by_id(conn, publisher_attempt_id)
    if publisher is None:
        return False
    publisher_effect = store.get_effect_by_dispatch_id(conn, str(publisher["dispatch_id"]))
    origin: UsageImportOrigin = (
        "reconciliation"
        if publisher_effect is not None
        and str(publisher_effect["effect_kind"]) == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
        else "ingestion"
    )
    existing = store.get_usage_attempt_fact(conn, logical_attempt_id)
    if existing is None:
        return _write_classified_without_counters(
            store,
            conn,
            attempt=attempt,
            publisher_attempt_id=publisher_attempt_id,
            now=now,
            coverage=None,
            settlement="pending",
            diagnostic=_WRITE_FAILED_DIAGNOSTIC,
            import_origin=origin,
            configured_model=None,
        )
    payload = UsageFactPayloadV1.model_validate_json(str(existing["fact_payload"]))
    payload = payload.model_copy(
        update={
            "coverage": None,
            "settlement_status": "pending",
            "import_origin": origin,
            "diagnostic_codes": tuple(
                dict.fromkeys((*payload.diagnostic_codes, _WRITE_FAILED_DIAGNOSTIC))
            ),
        }
    )
    body = payload.model_dump(mode="json")
    try:
        provenance = json.loads(str(existing["provenance_json"]))
    except json.JSONDecodeError:
        provenance = {}
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["publisher_attempt_id"] = publisher_attempt_id
    provenance["import_origin"] = origin
    store.reopen_usage_statistics_obligation(
        conn,
        logical_attempt_id=logical_attempt_id,
        publisher_attempt_id=publisher_attempt_id,
        fact_payload=canonical_json(body),
        payload_sha256=canonical_sha256(body),
        diagnostic_codes=canonical_json(list(payload.diagnostic_codes)),
        import_origin=origin,
        provenance_json=canonical_json(provenance),
        now_text=encode_utc_instant(now),
    )
    return True


def _logical_attempt_for_publisher(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    publisher_attempt_id: str,
) -> str | None:
    attempt = store.get_attempt_by_id(conn, publisher_attempt_id)
    if attempt is None:
        return None
    effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if effect is None:
        return None
    effect_kind = str(effect["effect_kind"])
    if effect_kind in _INFERENCE_EFFECTS:
        return publisher_attempt_id
    if effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
        return None
    try:
        payload = json.loads(str(effect["effect_payload"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    recovery_key = payload.get("recovery_key")
    if not isinstance(recovery_key, str) or not recovery_key:
        return None
    row = store.get_cursor_sdk_reconciliation_by_key(conn, recovery_key=recovery_key)
    if row is None:
        return None
    source_attempt_id = row["source_attempt_id"]
    if not isinstance(source_attempt_id, str) or not source_attempt_id:
        return None
    return source_attempt_id


def _diagnostic_codes(row: sqlite3.Row) -> tuple[str, ...]:
    try:
        value = json.loads(str(row["diagnostic_codes"]))
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mark_debt_if_terminal(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    logical_attempt_id: str,
    now: datetime,
) -> None:
    attempt = store.get_attempt_by_id(conn, logical_attempt_id)
    if attempt is None or str(attempt["status"]) not in {"completed", "failed", "cancelled"}:
        return
    existing = store.get_usage_attempt_fact(conn, logical_attempt_id)
    if existing is None or str(existing["settlement_status"]) != "pending":
        return
    if _WRITE_FAILED_DIAGNOSTIC in _diagnostic_codes(existing):
        return
    _write_classified_without_counters(
        store,
        conn,
        attempt=attempt,
        publisher_attempt_id=str(existing["publisher_attempt_id"]),
        now=now,
        coverage=None,
        settlement="debt",
        diagnostic="usage_source_debt",
        import_origin=cast(UsageImportOrigin, str(existing["import_origin"])),
        configured_model=None
        if existing["configured_model"] is None
        else str(existing["configured_model"]),
    )


def _write_classified_without_counters(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    attempt: sqlite3.Row,
    publisher_attempt_id: str,
    now: datetime,
    coverage: Literal["complete", "partial", "unknown", "not_applicable"] | None,
    settlement: UsageSettlement,
    diagnostic: str | None,
    import_origin: UsageImportOrigin,
    configured_model: str | None,
    extra_diagnostics: tuple[str, ...] = (),
) -> bool:
    if str(attempt["status"]) not in _TERMINAL_STATUSES:
        return False
    effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if effect is None:
        return False
    binding_kind, binding_sha = _binding(attempt)
    codes = tuple(dict.fromkeys(code for code in (*extra_diagnostics, diagnostic) if code))
    payload = UsageFactPayloadV1(
        logical_attempt_id=str(attempt["attempt_id"]),
        run_id=str(attempt["run_id"]),
        provider=_provider_for_effect(str(effect["effect_kind"])),
        role=_role_for_effect(str(effect["effect_kind"])),
        stage=_stage_for_effect(effect),
        configured_model=configured_model,
        terminal_status=_terminal_status(str(attempt["status"])),
        coverage=coverage,
        settlement_status=settlement,
        diagnostic_codes=safe_diagnostic_codes(list(codes)),
        import_origin=import_origin,
    )
    merge_usage_snapshot(
        store,
        conn,
        fact=_FactWrite(
            payload=payload,
            source_paths=(),
            source_digests=(),
            publisher_attempt_id=publisher_attempt_id,
            source_dispatch_id=str(attempt["dispatch_id"]),
            completion_fence_id=_fence(attempt),
            binding_kind=binding_kind,
            binding_sha256=binding_sha,
            import_origin=import_origin,
        ),
        observations=(),
        now=now,
    )
    return True


def _merge_observation(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    observation: _ObservationWrite,
    *,
    now_text: str,
) -> UsageWriteResult:
    existing = store.get_usage_observation(conn, observation.counters.observation_id)
    if existing is None:
        store.insert_usage_observation(conn, _observation_values(observation, now_text=now_text))
        return "inserted"
    current = UsageObservationCountersV1.model_validate_json(_observation_semantic_json(existing))
    relation = observation_relation(current, observation.counters)
    if relation == "replayed":
        return "replayed"
    if relation == "conflict":
        return "conflict"
    store.update_usage_observation(
        conn,
        observation_id=observation.counters.observation_id,
        values={
            "capture_complete": 1 if observation.counters.capture_complete else 0,
            "availability": observation.counters.availability,
            "native_payload": observation.counters.native.model_dump_json(),
            "normalized_payload": observation.counters.normalized.model_dump_json(),
            "payload_sha256": canonical_sha256(observation.counters.semantic_payload()),
            "source_event_offsets": canonical_json(list(observation.source_event_offsets)),
            "verified_at": now_text,
            "updated_at": now_text,
        },
    )
    return "enriched"


def _merge_fact(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    fact: _FactWrite,
    payload: UsageFactPayloadV1,
    *,
    now_text: str,
) -> UsageWriteResult:
    body = payload.model_dump(mode="json")
    text = canonical_json(body)
    digest = canonical_sha256(body)
    existing = store.get_usage_attempt_fact(conn, payload.logical_attempt_id)
    if existing is None:
        provenance = _provenance(fact, now_text=now_text)
        store.insert_usage_attempt_fact(
            conn,
            {
                "logical_attempt_id": payload.logical_attempt_id,
                "run_id": payload.run_id,
                "provider": payload.provider,
                "role": payload.role,
                "stage": payload.stage,
                "configured_model": payload.configured_model,
                "terminal_status": payload.terminal_status,
                "coverage": payload.coverage,
                "settlement_status": payload.settlement_status,
                "diagnostic_codes": canonical_json(list(payload.diagnostic_codes)),
                "fact_payload": text,
                "payload_sha256": digest,
                "schema_version": USAGE_FACT_SCHEMA_VERSION,
                "normalization_version": payload.normalization_version,
                "import_origin": fact.import_origin,
                "source_paths": canonical_json(list(fact.source_paths)),
                "source_digests": canonical_json(list(fact.source_digests)),
                "publisher_attempt_id": fact.publisher_attempt_id,
                "source_dispatch_id": fact.source_dispatch_id,
                "completion_fence_id": fact.completion_fence_id,
                "binding_kind": fact.binding_kind,
                "binding_sha256": fact.binding_sha256,
                "acknowledgement": "pending",
                "provenance_json": provenance,
                "verified_at": None if payload.settlement_status == "pending" else now_text,
                "created_at": now_text,
                "updated_at": now_text,
            },
        )
        return "inserted"
    if str(existing["payload_sha256"]) == digest and str(existing["fact_payload"]) == text:
        return "replayed"
    if not _fact_enrichment_allowed(existing, payload):
        codes = safe_diagnostic_codes(
            json.loads(str(existing["diagnostic_codes"])) + ["usage_payload_conflict"]
        )
        conflict_payload = UsageFactPayloadV1.model_validate_json(str(existing["fact_payload"]))
        conflict_payload = conflict_payload.model_copy(
            update={"diagnostic_codes": codes, "settlement_status": "conflict"}
        )
        store.update_usage_attempt_fact(
            conn,
            logical_attempt_id=payload.logical_attempt_id,
            values={
                "configured_model": existing["configured_model"],
                "coverage": conflict_payload.coverage,
                "settlement_status": "conflict",
                "diagnostic_codes": canonical_json(list(codes)),
                "fact_payload": canonical_json(conflict_payload.model_dump(mode="json")),
                "payload_sha256": canonical_sha256(conflict_payload.model_dump(mode="json")),
                "source_paths": str(existing["source_paths"]),
                "source_digests": str(existing["source_digests"]),
                "acknowledgement": "pending",
                "verified_at": str(existing["verified_at"]) if existing["verified_at"] else None,
                "updated_at": now_text,
            },
        )
        return "conflict"
    stored_model = existing["configured_model"]
    store.update_usage_attempt_fact(
        conn,
        logical_attempt_id=payload.logical_attempt_id,
        values={
            "configured_model": payload.configured_model
            if payload.configured_model is not None
            else stored_model,
            "coverage": payload.coverage,
            "settlement_status": payload.settlement_status,
            "diagnostic_codes": canonical_json(list(payload.diagnostic_codes)),
            "fact_payload": text,
            "payload_sha256": digest,
            "source_paths": canonical_json(list(fact.source_paths))
            if fact.source_paths
            else str(existing["source_paths"]),
            "source_digests": canonical_json(list(fact.source_digests))
            if fact.source_digests
            else str(existing["source_digests"]),
            "acknowledgement": "pending",
            "verified_at": None if payload.settlement_status == "pending" else now_text,
            "updated_at": now_text,
        },
    )
    return "enriched"


def _fact_enrichment_allowed(existing: sqlite3.Row, candidate: UsageFactPayloadV1) -> bool:
    if str(existing["run_id"]) != candidate.run_id:
        return False
    if str(existing["provider"]) != candidate.provider or str(existing["role"]) != candidate.role:
        return False
    if str(existing["stage"]) != candidate.stage:
        return False
    existing_model = existing["configured_model"]
    if existing_model is not None and candidate.configured_model not in {None, str(existing_model)}:
        return False
    previous = str(existing["settlement_status"])
    if previous == "conflict":
        return False
    if previous in {"settled", "debt"} and candidate.settlement_status == "pending":
        return False
    if previous == "debt" and candidate.settlement_status != "debt":
        return candidate.coverage in {"partial", "complete"} and bool(candidate.observation_ids)
    previous_coverage = existing["coverage"]
    if previous_coverage is None:
        return True
    if candidate.coverage is None:
        return False
    ranks = {"unknown": 0, "not_applicable": 0, "partial": 1, "complete": 2}
    return ranks[candidate.coverage] >= ranks[str(previous_coverage)]


def _observation_values(observation: _ObservationWrite, *, now_text: str) -> dict[str, object]:
    counters = observation.counters
    return {
        "observation_id": counters.observation_id,
        "logical_attempt_id": observation.logical_attempt_id,
        "run_id": observation.run_id,
        "provider": observation.provider,
        "role": observation.role,
        "stage": observation.stage,
        "native_key": observation.native_key,
        "publisher_attempt_id": observation.publisher_attempt_id,
        "source_dispatch_id": observation.source_dispatch_id,
        "source_relative_path": observation.source_relative_path,
        "source_digest": observation.source_digest,
        "source_format": observation.source_format[:80],
        "source_event_ordinal": observation.source_event_ordinal,
        "source_event_offsets": canonical_json(list(observation.source_event_offsets)),
        "capture_complete": 1 if counters.capture_complete else 0,
        "availability": counters.availability,
        "native_payload": counters.native.model_dump_json(),
        "normalized_payload": counters.normalized.model_dump_json(),
        "payload_sha256": canonical_sha256(counters.semantic_payload()),
        "schema_version": USAGE_FACT_SCHEMA_VERSION,
        "normalization_version": counters.normalization_version,
        "import_origin": observation.import_origin,
        "completion_fence_id": observation.completion_fence_id,
        "binding_kind": observation.binding_kind,
        "binding_sha256": observation.binding_sha256,
        "provenance_json": canonical_json(
            {
                "binding_kind": observation.binding_kind,
                "binding_sha256": observation.binding_sha256,
                "import_origin": observation.import_origin,
                "publisher_attempt_id": observation.publisher_attempt_id,
                "source_digest": observation.source_digest,
                "source_relative_path": observation.source_relative_path,
                "verified_at": now_text,
            }
        ),
        "verified_at": now_text,
        "created_at": now_text,
        "updated_at": now_text,
    }


def _observation_semantic_json(row: sqlite3.Row) -> str:
    native = json.loads(str(row["native_payload"]))
    normalized = json.loads(str(row["normalized_payload"]))
    return canonical_json(
        {
            "schema_version": 1,
            "normalization_version": str(row["normalization_version"]),
            "observation_id": str(row["observation_id"]),
            "availability": str(row["availability"]),
            "capture_complete": bool(row["capture_complete"]),
            "native": native,
            "normalized": normalized,
        }
    )


def _observation_write(
    observation: UsageObservationV1,
    *,
    publisher_attempt_id: str,
    import_origin: UsageImportOrigin,
    completion_fence_id: str | None,
    binding_kind: UsageBindingKind,
    binding_sha256: str,
) -> _ObservationWrite:
    return _ObservationWrite(
        counters=UsageObservationCountersV1(
            observation_id=observation.observation_id,
            availability=observation.availability,
            capture_complete=observation.capture_complete,
            native=observation.native,
            normalized=observation.normalized,
        ),
        logical_attempt_id=observation.scheduler_attempt_id,
        run_id=observation.run_id,
        provider=observation.provider,
        role=observation.role,
        stage=observation.stage,
        native_key=observation.native_key,
        publisher_attempt_id=publisher_attempt_id,
        source_dispatch_id=observation.source_dispatch_id,
        source_relative_path=require_relative_usage_path(observation.source_artifact_path),
        source_digest=require_sha256(observation.source_artifact_sha256),
        source_format=observation.source_format,
        source_event_ordinal=observation.source_event_ordinal,
        source_event_offsets=observation.source_event_offsets,
        import_origin=import_origin,
        completion_fence_id=completion_fence_id,
        binding_kind=binding_kind,
        binding_sha256=binding_sha256,
    )


def _provenance(fact: _FactWrite, *, now_text: str) -> str:
    return canonical_json(
        {
            "binding_kind": fact.binding_kind,
            "binding_sha256": fact.binding_sha256,
            "completion_fence_id": fact.completion_fence_id,
            "import_origin": fact.import_origin,
            "publisher_attempt_id": fact.publisher_attempt_id,
            "source_digests": list(fact.source_digests),
            "source_paths": list(fact.source_paths),
            "verified_at": now_text,
        }
    )


def _coverage(
    *,
    observations: list[_ObservationWrite],
    contribution_complete: bool,
    incomplete: bool,
) -> Literal["complete", "partial", "unknown"]:
    if not observations:
        return "unknown"
    if (
        contribution_complete
        and not incomplete
        and all(item.counters.availability == "known" for item in observations)
    ):
        return "complete"
    return "partial"


def _logical_attempt_id(
    effect_kind: str,
    attempt_id: str,
    invocation: Mapping[str, object],
) -> str:
    if effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
        source = invocation.get("source_attempt_id")
        if isinstance(source, str) and source:
            return source
    return attempt_id


def _origin(effect_kind: str) -> UsageImportOrigin:
    if effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
        return "reconciliation"
    return "ingestion"


def _binding(attempt: sqlite3.Row) -> tuple[UsageBindingKind, str]:
    envelope = attempt["completion_envelope_sha256"]
    if isinstance(envelope, str) and len(envelope) == 64:
        return "completion_envelope", envelope
    import hashlib

    fence = attempt["completion_fence_id"]
    raw = f"ledger\0{attempt['attempt_id']}\0{attempt['status']}\0{fence or ''}".encode()
    return "ledger_attempt", hashlib.sha256(raw).hexdigest()


def _fence(attempt: sqlite3.Row) -> str | None:
    fence = attempt["completion_fence_id"]
    if fence is None:
        return None
    text = str(fence)
    return text or None


def _terminal_status(status: str) -> UsageTerminalStatus:
    if status in {"completed", "failed", "cancelled", "uncertain"}:
        return cast(UsageTerminalStatus, status)
    return "uncertain"


def _provider_for_effect(effect_kind: str) -> Literal["cursor", "codex"]:
    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        return "cursor"
    return "codex"


def _role_for_effect(effect_kind: str) -> Literal["implementer", "reviewer"]:
    from ai_dev_loop.scheduler.domain.codex_contract import CODEX_ATTEMPT_EFFECT_KINDS

    if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
        return "reviewer"
    return "implementer"


def _stage_for_effect(effect: sqlite3.Row) -> Literal["initial", "correction", "review"]:
    from ai_dev_loop.scheduler.domain.codex_contract import CODEX_ATTEMPT_EFFECT_KINDS

    effect_kind = str(effect["effect_kind"])
    if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
        return "review"
    try:
        payload = json.loads(str(effect["effect_payload"]))
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        turn = payload.get("turn")
        if turn in {"initial", "correction"}:
            return cast(Literal["initial", "correction"], turn)
        iteration = payload.get("iteration")
        if isinstance(iteration, int) and not isinstance(iteration, bool) and iteration > 1:
            return "correction"
    return "initial"


def _stored_coverage(
    value: object,
) -> Literal["complete", "partial", "unknown", "not_applicable"] | None:
    if value in {"complete", "partial", "unknown", "not_applicable"}:
        return cast(Literal["complete", "partial", "unknown", "not_applicable"], value)
    return None


def _rollback_savepoint(conn: sqlite3.Connection, name: str) -> None:
    with suppress(sqlite3.Error):
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
    with suppress(sqlite3.Error):
        conn.execute(f"RELEASE SAVEPOINT {name}")
