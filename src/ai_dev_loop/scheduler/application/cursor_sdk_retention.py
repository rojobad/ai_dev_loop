"""Operator and automatic disposal of app-owned Cursor SDK conversation roots."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import ValidationError as ModelValidationError

from ai_dev_loop.paths import (
    DIR_MODE,
    CursorSdkPathError,
    cursor_conversation_key,
    set_sensitive_file_mode,
    validate_cursor_conversation_key,
)
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkSuccessorIntentV1,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_retention import (
    CURSOR_SDK_RETENTION_DECISION_AT,
    CURSOR_SDK_RETENTION_DECISION_SOURCE,
    CURSOR_SDK_RETENTION_HOURS,
    CURSOR_SDK_RETENTION_POLICY_ID,
    CursorSdkDisposalIntentV1,
    CursorSdkDisposalReceiptV1,
    CursorSdkRetentionPolicyV1,
)
from ai_dev_loop.scheduler.domain.events import (
    CURSOR_SDK_RECONCILED_EVENT_KIND,
    CURSOR_SDK_RECONCILED_V2_EVENT_KIND,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FENCE_TTL_SECONDS = 30
MAINTENANCE_BATCH_LIMIT = 8
_RETENTION_WINDOW = timedelta(hours=CURSOR_SDK_RETENTION_HOURS)
_DEFINITIVE_KINDS = frozenset({"completed", "completed_with_residual_risk", "aborted"})
_LIVE_ATTEMPT_STATUSES = frozenset({"launching", "active", "uncertain"})
_LIVE_EFFECT_STATUSES = frozenset({"pending", "claimed", "retry_wait", "uncertain", "blocked"})
_OPEN_RECOVERY_STATUSES = frozenset({"pending", "blocked"})
_STATUS_COLUMNS = {
    "scheduler_cursor_sdk_reconciliations": frozenset({"run_id"}),
    "scheduler_cursor_initial_recoveries": frozenset({"source_run_id", "successor_run_id"}),
    "scheduler_codex_implementer_recoveries": frozenset({"source_run_id"}),
}
_REPLACEMENT_TABLES = frozenset(
    {
        "scheduler_sequence_cursor_replacements",
        "scheduler_sequence_implementer_replacements",
    }
)
_T = TypeVar("_T")
CleanupMode = Literal["manual", "automatic"]
_RECONCILED_OUTCOMES = frozenset(
    {
        "terminal_recovered",
        "continuation_ready",
        "abort_reconciled",
        "prompt_unsent",
    }
)
_RECONCILED_EVENT_KINDS = (
    CURSOR_SDK_RECONCILED_EVENT_KIND,
    CURSOR_SDK_RECONCILED_V2_EVENT_KIND,
)
_BLOCKING_KIND_REASONS = {
    "max_iterations_reached": "max_iterations_extendable",
    "waiting_codex_capacity": "capacity_wait",
    "checkpoint_pending": "sequence_handoff",
    "pending_sequence_review_recovery": "recovery_publication",
}


@dataclass(frozen=True)
class ConversationEligibility:
    """Authenticated disposal eligibility for one shared conversation owner."""

    conversation_key: str
    conversation_owner_run_id: str
    store_backend: str
    native_store_layout_version: int
    manual_eligible: bool
    automatic_eligible: bool
    definitively_finished_at: str | None
    manual_reasons: tuple[str, ...]
    automatic_reasons: tuple[str, ...]
    related_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class CleanupPreview:
    """Read-only view of policy and conversation eligibility."""

    policy: CursorSdkRetentionPolicyV1 | None
    conversations: tuple[ConversationEligibility, ...]


@dataclass(frozen=True)
class CleanupApplyResult:
    """Result of one explicit or automatic disposal attempt."""

    action: str
    conversation_key: str | None
    receipt_id: str | None
    deleted: bool
    reasons: tuple[str, ...]
    observed: str


@dataclass(frozen=True)
class MaintenanceResult:
    """Bounded automatic disposal batch."""

    deleted_keys: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DispatchOpenResult:
    """Whether dispatch may create or open a native conversation root."""

    observed: str
    value: object | None = None


@dataclass
class _PreparedDisposal:
    action: str | None = None
    reasons: tuple[str, ...] = ()
    observed: str = ""
    generation: int | None = None
    created_intent: bool = False
    intent: CursorSdkDisposalIntentV1 | None = None
    finished_at: str | None = None
    backend: str | None = None
    already_deleted: CursorSdkDisposalReceiptV1 | None = None
    receipt_payload: str | None = None


def load_cursor_sdk_retention_policy(
    store: SqliteSchedulerStore,
) -> CursorSdkRetentionPolicyV1 | None:
    """Return the explicit application policy, or None when it is not stored."""

    with store.begin_read() as conn:
        return _policy_from_connection(conn)


def set_cursor_sdk_retention_enabled(
    store: SqliteSchedulerStore,
    *,
    enabled: bool,
    now: datetime,
) -> CursorSdkRetentionPolicyV1:
    """Enable or disable automatic cleanup without changing the approved window."""

    updated_at = encode_utc_instant(now)
    with store.begin_immediate() as conn:
        current = _policy_from_connection(conn)
        if current is None:
            conn.execute(
                """
                INSERT INTO scheduler_cursor_sdk_retention_policy(
                    policy_id, schema_version, automatic_cleanup_enabled,
                    retention_hours, decision_source, decision_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    CURSOR_SDK_RETENTION_POLICY_ID,
                    1 if enabled else 0,
                    CURSOR_SDK_RETENTION_HOURS,
                    CURSOR_SDK_RETENTION_DECISION_SOURCE,
                    CURSOR_SDK_RETENTION_DECISION_AT,
                    updated_at,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE scheduler_cursor_sdk_retention_policy
                   SET automatic_cleanup_enabled = ?, updated_at = ?
                 WHERE policy_id = ?
                """,
                (1 if enabled else 0, updated_at, CURSOR_SDK_RETENTION_POLICY_ID),
            )
        stored = _policy_from_connection(conn)
    if stored is None:
        raise CursorSdkPathError("retention_policy_unavailable")
    return stored


def preview_cursor_sdk_cleanup(
    store: SqliteSchedulerStore,
    *,
    state_root: Path,
    now: datetime,
    conversation_key: str | None = None,
    conversation_owner_run_id: str | None = None,
) -> CleanupPreview:
    """Read eligibility. This path does not write the ledger or the filesystem."""

    with store.begin_read() as conn:
        policy, conversations = _assess(conn, now=now)
    selected = _select_conversations(
        conversations,
        conversation_key=conversation_key,
        conversation_owner_run_id=conversation_owner_run_id,
    )
    if (conversation_key or conversation_owner_run_id) and not selected:
        key = conversation_key or ""
        owner = conversation_owner_run_id or ""
        selected = (
            ConversationEligibility(
                conversation_key=key,
                conversation_owner_run_id=owner,
                store_backend="",
                native_store_layout_version=0,
                manual_eligible=False,
                automatic_eligible=False,
                definitively_finished_at=None,
                manual_reasons=("unauthenticated_binding",),
                automatic_reasons=("unauthenticated_binding",),
                related_run_ids=(),
            ),
        )
    del state_root
    return CleanupPreview(policy=policy, conversations=selected)


def apply_cursor_sdk_cleanup(
    store: SqliteSchedulerStore,
    *,
    state_root: Path,
    now: datetime,
    mode: CleanupMode,
    conversation_key: str | None = None,
    conversation_owner_run_id: str | None = None,
    after_intent_hook: Callable[[], None] | None = None,
    holder_id: str | None = None,
) -> CleanupApplyResult:
    """Delete one authenticated conversation root, or converge an existing intent."""

    holder = holder_id or f"cleanup-{secrets.token_hex(8)}"
    with store.begin_immediate() as conn:
        prepared = _prepare_disposal(
            conn,
            now=now,
            mode=mode,
            holder_id=holder,
            conversation_key=conversation_key,
            conversation_owner_run_id=conversation_owner_run_id,
        )
    if prepared.already_deleted is not None:
        _ensure_tombstone(
            state_root,
            prepared.already_deleted,
            payload_text=prepared.receipt_payload,
        )
        return CleanupApplyResult(
            action="already_deleted",
            conversation_key=prepared.already_deleted.conversation_key,
            receipt_id=prepared.already_deleted.receipt_id,
            deleted=False,
            reasons=(),
            observed="disposal_receipt",
        )
    if prepared.action is not None or prepared.intent is None or prepared.generation is None:
        return CleanupApplyResult(
            action=prepared.action or "ineligible",
            conversation_key=conversation_key,
            receipt_id=prepared.intent.receipt_id if prepared.intent is not None else None,
            deleted=False,
            reasons=prepared.reasons,
            observed=prepared.observed or prepared.action or "ineligible",
        )
    intent = prepared.intent
    generation = prepared.generation
    try:
        if after_intent_hook is not None:
            after_intent_hook()
        with store.begin_immediate() as conn:
            if not _fence_generation_is_current(conn, intent.conversation_key, generation):
                stored = _load_receipt_payload(conn, intent.conversation_key)
                if stored is None:
                    return CleanupApplyResult(
                        action="fence_busy",
                        conversation_key=intent.conversation_key,
                        receipt_id=intent.receipt_id,
                        deleted=False,
                        reasons=("fence_busy",),
                        observed="fence_busy",
                    )
                stored_receipt, stored_payload = stored
                _publish_stored = (stored_receipt, stored_payload)
            else:
                _publish_stored = None
        if _publish_stored is not None:
            stored_receipt, stored_payload = _publish_stored
            _ensure_tombstone(
                state_root,
                stored_receipt,
                payload_text=stored_payload,
            )
            return CleanupApplyResult(
                action="already_deleted",
                conversation_key=stored_receipt.conversation_key,
                receipt_id=stored_receipt.receipt_id,
                deleted=False,
                reasons=(),
                observed="disposal_receipt",
            )
        _assert_tombstone_publishable(state_root, intent.conversation_key)
        removed = _remove_conversation_root(
            state_root,
            conversation_key=intent.conversation_key,
        )
    except CursorSdkPathError as exc:
        if prepared.created_intent:
            _abandon_unremoved_intent(
                store,
                intent,
                generation=prepared.generation,
                now=now,
            )
        else:
            _release_fence(store, intent.conversation_key, prepared.generation, now)
        return CleanupApplyResult(
            action="refused",
            conversation_key=intent.conversation_key,
            receipt_id=intent.receipt_id,
            deleted=False,
            reasons=(exc.code,),
            observed=exc.code,
        )
    except Exception:
        _release_fence(store, intent.conversation_key, prepared.generation, now)
        raise
    finalized = _finalize_receipt(
        store,
        intent=intent,
        generation=generation,
        now=now,
    )
    if finalized is None:
        return CleanupApplyResult(
            action="fence_busy",
            conversation_key=intent.conversation_key,
            receipt_id=intent.receipt_id,
            deleted=False,
            reasons=("fence_busy",),
            observed="fence_busy",
        )
    receipt, payload_text, adopted = finalized
    _ensure_tombstone(state_root, receipt, payload_text=payload_text)
    if adopted:
        return CleanupApplyResult(
            action="already_deleted",
            conversation_key=receipt.conversation_key,
            receipt_id=receipt.receipt_id,
            deleted=False,
            reasons=(),
            observed="disposal_receipt",
        )
    return CleanupApplyResult(
        action="already_absent" if not removed else "deleted",
        conversation_key=intent.conversation_key,
        receipt_id=receipt.receipt_id,
        deleted=removed,
        reasons=(),
        observed="disposal_receipt",
    )


def maintain_cursor_sdk_conversations(
    store: SqliteSchedulerStore,
    *,
    state_root: Path,
    now: datetime,
    batch_limit: int = MAINTENANCE_BATCH_LIMIT,
) -> MaintenanceResult:
    """Delete age-eligible conversations through the manual disposal path."""

    if batch_limit < 1:
        raise CursorSdkPathError("invalid_cleanup_batch")
    with store.begin_read() as conn:
        _policy, conversations = _assess(conn, now=now)
    deleted: list[str] = []
    skipped: list[tuple[str, str]] = []
    visits = 0
    for item in sorted(conversations, key=lambda candidate: candidate.conversation_key):
        if not item.automatic_eligible:
            continue
        if visits >= batch_limit:
            break
        visits += 1
        try:
            result = apply_cursor_sdk_cleanup(
                store,
                state_root=state_root,
                now=now,
                mode="automatic",
                conversation_key=item.conversation_key,
                conversation_owner_run_id=item.conversation_owner_run_id,
            )
        except Exception:
            skipped.append((item.conversation_key, "removal_interrupted"))
            continue
        if result.action in {"deleted", "already_absent"}:
            deleted.append(item.conversation_key)
        elif result.action == "already_deleted":
            continue
        else:
            reason = result.reasons[0] if result.reasons else result.action
            skipped.append((item.conversation_key, reason))
    _repair_unpublished_tombstones(store, state_root, limit=batch_limit)
    return MaintenanceResult(deleted_keys=tuple(deleted), skipped=tuple(skipped))


def try_dispatch_open(
    store: SqliteSchedulerStore,
    *,
    conversation_key: str,
    conversation_owner_run_id: str,
    now: datetime,
    opener: Callable[[], _T],
    hold_hook: Callable[[], None] | None = None,
    holder_id: str | None = None,
) -> DispatchOpenResult:
    """Hold the conversation fence across native store creation or opening."""

    key = _authenticated_key(conversation_key, conversation_owner_run_id)
    holder = holder_id or f"dispatch-{secrets.token_hex(8)}"
    generation: int | None = None
    with store.begin_immediate() as conn:
        observed = _disposal_observation(conn, key)
        if observed is not None:
            return DispatchOpenResult(observed=observed, value=None)
        generation = _acquire_fence(
            conn,
            conversation_key=key,
            holder_id=holder,
            purpose="dispatch",
            now=now,
        )
        if generation is None:
            observed = _disposal_observation(conn, key) or "fence_busy"
            return DispatchOpenResult(observed=observed, value=None)
        observed = _disposal_observation(conn, key)
        if observed is not None:
            _release_fence_on(conn, key, generation, now)
            return DispatchOpenResult(observed=observed, value=None)
    try:
        if hold_hook is not None:
            hold_hook()
        value = opener()
    finally:
        if generation is not None:
            _release_fence(store, key, generation, now)
    return DispatchOpenResult(observed="opened", value=value)


def open_dispatch_store(
    *,
    conversation_key: str,
    conversation_owner_run_id: str,
    opener: Callable[[], _T],
    now: datetime | None = None,
    db_path: Path | None = None,
) -> _T:
    """Open a native store unless a durable disposal intent or receipt exists."""

    path = default_engine_db_path() if db_path is None else db_path
    if not path.is_file():
        return opener()
    store = SqliteSchedulerStore(path)
    result = try_dispatch_open(
        store,
        conversation_key=conversation_key,
        conversation_owner_run_id=conversation_owner_run_id,
        now=now or datetime.now(UTC),
        opener=opener,
    )
    if result.observed != "opened" or result.value is None:
        raise CursorSdkPathError("conversation_disposed")
    return cast(_T, result.value)


def render_cleanup_preview(preview: CleanupPreview, *, output: str) -> str:
    payload = {
        "schema_version": 1,
        "policy": None if preview.policy is None else preview.policy.model_dump(mode="json"),
        "conversations": [
            {
                "conversation_key": item.conversation_key,
                "conversation_owner_run_id": item.conversation_owner_run_id,
                "store_backend": item.store_backend,
                "manual_eligible": item.manual_eligible,
                "automatic_eligible": item.automatic_eligible,
                "definitively_finished_at": item.definitively_finished_at,
                "manual_reasons": list(item.manual_reasons),
                "automatic_reasons": list(item.automatic_reasons),
            }
            for item in preview.conversations
        ],
        "dry_run": True,
    }
    if output == "json":
        return json.dumps(payload, sort_keys=True, indent=2) + "\n"
    lines = ["Cursor SDK store cleanup preview"]
    if preview.policy is None:
        lines.append("Policy: unavailable")
    else:
        state = "enabled" if preview.policy.automatic_cleanup_enabled else "disabled"
        lines.append(
            f"Policy: automatic {state}, {preview.policy.retention_hours}h, "
            f"{preview.policy.decision_source}"
        )
    if not preview.conversations:
        lines.append("No authenticated Cursor SDK conversations.")
    for item in preview.conversations:
        lines.append(
            f"- {item.conversation_key} owner={item.conversation_owner_run_id} "
            f"manual={item.manual_eligible} automatic={item.automatic_eligible} "
            f"reasons={','.join(item.manual_reasons + item.automatic_reasons) or 'none'}"
        )
    return "\n".join(lines) + "\n"


def render_cleanup_apply(result: CleanupApplyResult, *, output: str) -> str:
    payload = {
        "schema_version": 1,
        "action": result.action,
        "conversation_key": result.conversation_key,
        "receipt_id": result.receipt_id,
        "deleted": result.deleted,
        "reasons": list(result.reasons),
        "observed": result.observed,
        "dry_run": False,
    }
    if output == "json":
        return json.dumps(payload, sort_keys=True, indent=2) + "\n"
    return (
        "Cursor SDK store cleanup "
        f"{result.action} key={result.conversation_key or '-'} "
        f"receipt={result.receipt_id or '-'} "
        f"reasons={','.join(result.reasons) or 'none'}\n"
    )


def render_retention_policy(policy: CursorSdkRetentionPolicyV1 | None, *, output: str) -> str:
    payload = {
        "schema_version": 1,
        "policy": None if policy is None else policy.model_dump(mode="json"),
    }
    if output == "json":
        return json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if policy is None:
        return "Cursor SDK retention policy is not stored.\n"
    state = "enabled" if policy.automatic_cleanup_enabled else "disabled"
    return (
        f"Cursor SDK retention policy: automatic {state}, "
        f"{policy.retention_hours} hours from durable completion, "
        f"decision {policy.decision_source} at {policy.decision_at}.\n"
    )


def _policy_from_connection(conn: sqlite3.Connection) -> CursorSdkRetentionPolicyV1 | None:
    if not _has_table(conn, "scheduler_cursor_sdk_retention_policy"):
        return None
    row = conn.execute(
        """
        SELECT policy_id, schema_version, automatic_cleanup_enabled, retention_hours,
               decision_source, decision_at, updated_at
          FROM scheduler_cursor_sdk_retention_policy
         WHERE policy_id = ?
        """,
        (CURSOR_SDK_RETENTION_POLICY_ID,),
    ).fetchone()
    if row is None:
        return None
    try:
        return CursorSdkRetentionPolicyV1.model_validate(
            {
                "automatic_cleanup_enabled": bool(row["automatic_cleanup_enabled"]),
                "retention_hours": int(row["retention_hours"]),
                "decision_source": str(row["decision_source"]),
                "decision_at": str(row["decision_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
    except (ModelValidationError, TypeError, ValueError):
        return None


def _assess(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> tuple[CursorSdkRetentionPolicyV1 | None, tuple[ConversationEligibility, ...]]:
    policy = _policy_from_connection(conn)
    damaged, groups = _conversation_groups(conn)
    conversations: list[ConversationEligibility] = []
    for key in sorted(groups):
        conversations.append(
            _assess_group(
                conn,
                groups[key],
                key=key,
                now=now,
                policy=policy,
                damaged=damaged,
            )
        )
    return policy, tuple(conversations)


def _conversation_groups(
    conn: sqlite3.Connection,
) -> tuple[bool, dict[str, list[tuple[str, str, dict[str, object]]]]]:
    damaged = False
    groups: dict[str, list[tuple[str, str, dict[str, object]]]] = {}
    authenticated: dict[str, tuple[str, dict[str, object]]] = {}
    rows = conn.execute(
        """
        SELECT run_id, state_kind, state_payload, state_payload_sha256
          FROM scheduler_runs
         ORDER BY run_id
        """
    ).fetchall()
    for row in rows:
        payload_text = str(row["state_payload"])
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if digest != str(row["state_payload_sha256"]):
            damaged = True
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            damaged = True
            continue
        if not isinstance(payload, dict):
            damaged = True
            continue
        kind = payload.get("kind")
        if kind != row["state_kind"]:
            damaged = True
            continue
        authenticated[str(row["run_id"])] = (str(kind), payload)
        cursor = payload.get("cursor")
        sdk = cursor.get("sdk") if isinstance(cursor, dict) else None
        if not isinstance(sdk, dict):
            continue
        owner = sdk.get("conversation_owner_run_id")
        key = sdk.get("conversation_key")
        if not isinstance(owner, str) or not isinstance(key, str):
            damaged = True
            continue
        groups.setdefault(key, []).append((str(row["run_id"]), str(kind), payload))
    if _bind_reconciled_conversations(conn, authenticated=authenticated, groups=groups):
        damaged = True
    return damaged, groups


def _assess_group(
    conn: sqlite3.Connection,
    members: list[tuple[str, str, dict[str, object]]],
    *,
    key: str,
    now: datetime,
    policy: CursorSdkRetentionPolicyV1 | None,
    damaged: bool,
) -> ConversationEligibility:
    reasons: list[str] = []
    if damaged:
        reasons.append("ledger_evidence_unavailable")
    if _has_table(conn, "scheduler_cursor_sdk_disposals"):
        disposal = conn.execute(
            "SELECT status FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
            (key,),
        ).fetchone()
        if disposal is not None and str(disposal["status"]) == "deleted":
            reasons.append("already_disposed")
    owner, backend, layout, binding_reasons = _authenticate_group(members, key)
    reasons.extend(binding_reasons)
    run_ids = tuple(sorted(run_id for run_id, _kind, _payload in members))
    stamps: list[datetime] = []
    superseded = _superseded_blocked_runs(conn, key=key, members=members)
    if not binding_reasons:
        for run_id, kind, payload in members:
            if run_id in superseded:
                continue
            if kind not in _DEFINITIVE_KINDS:
                reasons.append(_BLOCKING_KIND_REASONS.get(kind, "continuable_state"))
                continue
            raw_stamp = (
                payload.get("aborted_at") if kind == "aborted" else payload.get("updated_at")
            )
            if not isinstance(raw_stamp, str):
                reasons.append("missing_completion_evidence")
                continue
            try:
                stamps.append(_parse_utc(raw_stamp))
            except ValueError:
                reasons.append("missing_completion_evidence")
        reconciliation_stamps, reconciliation_reasons = _reconciliation_completion_stamps(
            conn, run_ids
        )
        stamps.extend(reconciliation_stamps)
        reasons.extend(reconciliation_reasons)
        reasons.extend(_continuation_reasons(conn, run_ids, members))
    manual_reasons = tuple(dict.fromkeys(reasons))
    finished: datetime | None = max(stamps) if stamps and not manual_reasons else None
    finished_text = encode_utc_instant(finished) if finished is not None else None
    automatic_reasons: list[str] = []
    if manual_reasons:
        automatic_reasons.extend(manual_reasons)
    else:
        if policy is None:
            automatic_reasons.append("policy_unavailable")
        elif not policy.automatic_cleanup_enabled:
            automatic_reasons.append("automatic_cleanup_disabled")
        if finished is None:
            automatic_reasons.append("missing_completion_evidence")
        elif now < finished + _RETENTION_WINDOW:
            automatic_reasons.append("retention_age")
    return ConversationEligibility(
        conversation_key=key,
        conversation_owner_run_id=owner,
        store_backend=backend,
        native_store_layout_version=layout,
        manual_eligible=not manual_reasons,
        automatic_eligible=not manual_reasons and not automatic_reasons,
        definitively_finished_at=finished_text,
        manual_reasons=manual_reasons,
        automatic_reasons=tuple(dict.fromkeys(automatic_reasons)),
        related_run_ids=run_ids,
    )


def _bind_reconciled_conversations(
    conn: sqlite3.Connection,
    *,
    authenticated: dict[str, tuple[str, dict[str, object]]],
    groups: dict[str, list[tuple[str, str, dict[str, object]]]],
) -> bool:
    """Attach runs whose checkpoint dropped the SDK binding to its authenticated intent."""

    if not _has_table(conn, "scheduler_cursor_sdk_reconciliations"):
        return False
    damaged = False
    rows = conn.execute(
        """
        SELECT run_id, intent_payload, intent_payload_sha256
          FROM scheduler_cursor_sdk_reconciliations
         ORDER BY run_id
        """
    ).fetchall()
    for row in rows:
        run_id = str(row["run_id"])
        state = authenticated.get(run_id)
        if state is None:
            damaged = True
            continue
        payload_text = str(row["intent_payload"])
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if digest != str(row["intent_payload_sha256"]):
            damaged = True
            continue
        try:
            intent = CursorSdkReconciliationIntentV1.model_validate_json(payload_text)
        except (ModelValidationError, ValueError):
            damaged = True
            continue
        if cursor_conversation_key(intent.conversation_owner_run_id) != intent.conversation_key:
            damaged = True
            continue
        binding = _sdk_binding_from_reconciliation_intent(intent)
        if binding is None:
            damaged = True
            continue
        kind, payload = state
        placed = _group_for_run(groups, run_id)
        if placed is not None and placed != intent.conversation_key:
            damaged = True
            continue
        if placed == intent.conversation_key:
            continue
        augmented = dict(payload)
        cursor = augmented.get("cursor")
        cursor_body = dict(cursor) if isinstance(cursor, dict) else {}
        cursor_body["sdk"] = binding
        augmented["cursor"] = cursor_body
        groups.setdefault(intent.conversation_key, []).append((run_id, kind, augmented))
    return damaged


def _sdk_binding_from_reconciliation_intent(
    intent: CursorSdkReconciliationIntentV1,
) -> dict[str, object] | None:
    source = intent.source_state.get("cursor")
    sdk = source.get("sdk") if isinstance(source, dict) else None
    if not isinstance(sdk, dict):
        return None
    if (
        sdk.get("conversation_owner_run_id") != intent.conversation_owner_run_id
        or sdk.get("conversation_key") != intent.conversation_key
    ):
        return None
    binding: dict[str, object] = {
        "conversation_owner_run_id": intent.conversation_owner_run_id,
        "conversation_key": intent.conversation_key,
    }
    schema_version = sdk.get("schema_version", 1)
    if isinstance(schema_version, int) and not isinstance(schema_version, bool):
        binding["schema_version"] = schema_version
    if "store_backend" in sdk:
        binding["store_backend"] = sdk["store_backend"]
    if "native_store_layout_version" in sdk:
        binding["native_store_layout_version"] = sdk["native_store_layout_version"]
    backend, layout = _backend_of(binding)
    if backend is None or layout is None:
        return None
    return binding


def _group_for_run(
    groups: dict[str, list[tuple[str, str, dict[str, object]]]],
    run_id: str,
) -> str | None:
    for key, members in groups.items():
        if any(member_id == run_id for member_id, _kind, _payload in members):
            return key
    return None


def _superseded_blocked_runs(
    conn: sqlite3.Connection,
    *,
    key: str,
    members: list[tuple[str, str, dict[str, object]]],
) -> set[str]:
    """Blocked ancestors replaced by an authenticated ready successor chain.

    Historical ``blocked`` bytes stay unchanged. A blocked run is finished only
    when one ready, hash-authenticated successor edge leads, directly or through
    further superseded ancestors, to a definitive member of this conversation.
    """

    if not members or not _has_table(conn, "scheduler_cursor_initial_recoveries"):
        return set()
    kinds = {run_id: kind for run_id, kind, _payload in members}
    placeholders, values = _placeholders(tuple(kinds))
    rows = conn.execute(
        f"""
        SELECT source_run_id, successor_run_id, status, intent_payload,
               intent_payload_sha256, record_sha256
          FROM scheduler_cursor_initial_recoveries
         WHERE source_run_id IN ({placeholders})
        """,
        values,
    ).fetchall()
    edges: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        source = str(row["source_run_id"])
        successor = str(row["successor_run_id"])
        if kinds.get(source) != "blocked" or successor not in kinds:
            continue
        if not _ready_successor_edge(row, key):
            continue
        previous = edges.get(source)
        if previous is not None and previous != successor:
            ambiguous.add(source)
            continue
        edges[source] = successor
    for source in ambiguous:
        edges.pop(source, None)
    finished: set[str] = set()

    def chain_finished(run_id: str, seen: set[str]) -> bool:
        if run_id in finished:
            return True
        if run_id in seen or len(seen) > 128:
            return False
        if kinds.get(run_id) in _DEFINITIVE_KINDS:
            finished.add(run_id)
            return True
        successor = edges.get(run_id)
        if successor is None:
            return False
        if chain_finished(successor, seen | {run_id}):
            finished.add(run_id)
            return True
        return False

    return {source for source in edges if chain_finished(source, set())}


def _ready_successor_edge(row: sqlite3.Row, conversation_key: str) -> bool:
    if str(row["status"]) != "ready":
        return False
    payload_text = str(row["intent_payload"])
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    if digest != str(row["intent_payload_sha256"]):
        return False
    try:
        intent = CursorSdkSuccessorIntentV1.model_validate_json(payload_text)
    except (ModelValidationError, ValueError):
        return False
    record_sha = str(row["record_sha256"] or "")
    try:
        owner_key = cursor_conversation_key(intent.conversation_owner_run_id)
    except CursorSdkPathError:
        return False
    return bool(
        intent.status == "ready"
        and intent.source_run_id == str(row["source_run_id"])
        and intent.successor_run_id == str(row["successor_run_id"])
        and intent.conversation_key == conversation_key
        and owner_key == conversation_key
        and intent.record_sha256 == record_sha
        and record_sha != ""
    )


def _reconciliation_completion_stamps(
    conn: sqlite3.Connection,
    run_ids: tuple[str, ...],
) -> tuple[list[datetime], list[str]]:
    if not run_ids or not _has_table(conn, "scheduler_cursor_sdk_reconciliations"):
        return [], []
    placeholders, values = _placeholders(run_ids)
    rows = conn.execute(
        f"""
        SELECT run_id, status, result_sha256
          FROM scheduler_cursor_sdk_reconciliations
         WHERE run_id IN ({placeholders})
        """,
        values,
    ).fetchall()
    stamps: list[datetime] = []
    reasons: list[str] = []
    for row in rows:
        if str(row["status"]) != "ready":
            continue
        stamp = _authenticated_reconciliation_stamp(
            conn,
            run_id=str(row["run_id"]),
            result_sha256=str(row["result_sha256"] or ""),
        )
        if stamp is None:
            reasons.append("missing_completion_evidence")
        else:
            stamps.append(stamp)
    return stamps, reasons


def _authenticated_reconciliation_stamp(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    result_sha256: str,
) -> datetime | None:
    if not result_sha256:
        return None
    placeholders, values = _placeholders(_RECONCILED_EVENT_KINDS)
    events = conn.execute(
        f"""
        SELECT event_payload, event_payload_sha256, created_at
          FROM scheduler_events
         WHERE run_id = ? AND event_kind IN ({placeholders})
         ORDER BY sequence
        """,
        (run_id, *values),
    ).fetchall()
    for event in events:
        payload_text = str(event["event_payload"])
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if digest != str(event["event_payload_sha256"]):
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("record_sha256") != result_sha256:
            continue
        if payload.get("outcome") not in _RECONCILED_OUTCOMES:
            continue
        try:
            return _parse_utc(str(event["created_at"]))
        except ValueError:
            return None
    return None


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
         WHERE type = 'table' AND name = ?
         LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _authenticate_group(
    members: list[tuple[str, str, dict[str, object]]],
    key: str,
) -> tuple[str, str, int, tuple[str, ...]]:
    owners: set[str] = set()
    backends: set[str] = set()
    layouts: set[int] = set()
    reasons: list[str] = []
    try:
        validate_cursor_conversation_key(key)
    except CursorSdkPathError:
        reasons.append("unauthenticated_binding")
    for _run_id, _kind, payload in members:
        cursor = payload.get("cursor")
        sdk = cursor.get("sdk") if isinstance(cursor, dict) else None
        if not isinstance(sdk, dict):
            reasons.append("unauthenticated_binding")
            continue
        owner = sdk.get("conversation_owner_run_id")
        stored_key = sdk.get("conversation_key")
        if not isinstance(owner, str) or stored_key != key:
            reasons.append("unauthenticated_binding")
            continue
        try:
            if cursor_conversation_key(owner) != key:
                reasons.append("unauthenticated_binding")
        except CursorSdkPathError:
            reasons.append("unauthenticated_binding")
            continue
        owners.add(owner)
        backend, layout = _backend_of(sdk)
        if backend is None or layout is None:
            reasons.append("unauthenticated_binding")
            continue
        backends.add(backend)
        layouts.add(layout)
    if len(owners) != 1 or len(backends) != 1 or len(layouts) != 1:
        reasons.append("unauthenticated_binding")
    owner = next(iter(owners)) if len(owners) == 1 else ""
    backend = next(iter(backends)) if len(backends) == 1 else ""
    layout = next(iter(layouts)) if len(layouts) == 1 else 0
    return owner, backend, layout, tuple(dict.fromkeys(reasons))


def _backend_of(sdk: dict[str, object]) -> tuple[str | None, int | None]:
    schema_version = sdk.get("schema_version", 1)
    backend = sdk.get("store_backend")
    layout = sdk.get("native_store_layout_version")
    if schema_version == 1 and backend is None and layout is None:
        return "jsonl", 1
    if backend in {"jsonl", "sqlite"} and layout == 1:
        return str(backend), 1
    return None, None


def _continuation_reasons(
    conn: sqlite3.Connection,
    run_ids: tuple[str, ...],
    members: list[tuple[str, str, dict[str, object]]],
) -> tuple[str, ...]:
    if not run_ids:
        return ()
    reasons: list[str] = []
    placeholders, values = _placeholders(run_ids)
    attempt = conn.execute(
        f"""
        SELECT 1 FROM scheduler_attempts
         WHERE run_id IN ({placeholders}) AND status IN ('launching', 'active', 'uncertain')
         LIMIT 1
        """,
        values,
    ).fetchone()
    if attempt is not None:
        reasons.append("live_or_uncertain_attempt")
    reservation = conn.execute(
        f"""
        SELECT 1 FROM scheduler_repository_reservations
         WHERE run_id IN ({placeholders}) AND status = 'active'
         LIMIT 1
        """,
        values,
    ).fetchone()
    if reservation is not None:
        reasons.append("active_reservation")
    claim = conn.execute(
        f"""
        SELECT 1 FROM scheduler_run_tick_claims
         WHERE run_id IN ({placeholders}) AND status = 'active'
         LIMIT 1
        """,
        values,
    ).fetchone()
    if claim is not None:
        reasons.append("dispatch_claim")
    effect_statuses = ",".join("?" for _ in _LIVE_EFFECT_STATUSES)
    effect = conn.execute(
        f"""
        SELECT 1 FROM scheduler_effects
         WHERE run_id IN ({placeholders}) AND status IN ({effect_statuses})
         LIMIT 1
        """,
        (*values, *tuple(sorted(_LIVE_EFFECT_STATUSES))),
    ).fetchone()
    if effect is not None:
        reasons.append("pending_dispatch")
    hold = conn.execute(
        f"SELECT 1 FROM scheduler_checkpoint_holds WHERE run_id IN ({placeholders}) LIMIT 1",
        values,
    ).fetchone()
    if hold is not None:
        reasons.append("sequence_handoff")
    capacity = conn.execute(
        f"SELECT 1 FROM scheduler_capacity WHERE holder_run_id IN ({placeholders}) LIMIT 1",
        values,
    ).fetchone()
    if capacity is not None:
        reasons.append("capacity_wait")
    if _open_status_exists(
        conn,
        "scheduler_cursor_sdk_reconciliations",
        "run_id",
        run_ids,
    ):
        reasons.append("abort_reconciliation")
    if _open_status_exists(conn, "scheduler_cursor_initial_recoveries", "source_run_id", run_ids):
        reasons.append("recovery_publication")
    if _open_status_exists(
        conn,
        "scheduler_cursor_initial_recoveries",
        "successor_run_id",
        run_ids,
    ):
        reasons.append("recovery_publication")
    if _open_status_exists(
        conn,
        "scheduler_codex_implementer_recoveries",
        "source_run_id",
        run_ids,
    ):
        reasons.append("recovery_publication")
    if _unadopted_replacement(conn, "scheduler_sequence_cursor_replacements", run_ids):
        reasons.append("sequence_handoff")
    if _unadopted_replacement(conn, "scheduler_sequence_implementer_replacements", run_ids):
        reasons.append("recovery_publication")
    successor_rows = conn.execute(
        f"""
        SELECT successor_run_id FROM scheduler_review_recovery_successors
         WHERE source_run_id IN ({placeholders})
        """,
        values,
    ).fetchall()
    known = {run_id for run_id, _kind, _payload in members}
    for successor in successor_rows:
        successor_id = str(successor["successor_run_id"])
        if successor_id not in known:
            reasons.append("recovery_publication")
    aborted = [run_id for run_id, kind, _payload in members if kind == "aborted"]
    if aborted and (
        "live_or_uncertain_attempt" in reasons
        or "abort_reconciliation" in reasons
        or "sequence_handoff" in reasons
    ):
        reasons.append("abort_reconciliation")
    return tuple(dict.fromkeys(reasons))


def _open_status_exists(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    run_ids: tuple[str, ...],
) -> bool:
    placeholders, values = _placeholders(run_ids)
    status_holders = ",".join("?" for _ in _OPEN_RECOVERY_STATUSES)
    columns = _STATUS_COLUMNS.get(table)
    if columns is None or column not in columns:
        raise CursorSdkPathError("invalid_cleanup_query")
    row = conn.execute(
        f"""
        SELECT 1 FROM {table}
         WHERE {column} IN ({placeholders}) AND status IN ({status_holders})
         LIMIT 1
        """,
        (*values, *tuple(sorted(_OPEN_RECOVERY_STATUSES))),
    ).fetchone()
    return row is not None


def _unadopted_replacement(
    conn: sqlite3.Connection,
    table: str,
    run_ids: tuple[str, ...],
) -> bool:
    if table not in _REPLACEMENT_TABLES:
        raise CursorSdkPathError("invalid_cleanup_query")
    placeholders, values = _placeholders(run_ids)
    row = conn.execute(
        f"""
        SELECT 1 FROM {table}
         WHERE source_run_id IN ({placeholders})
           AND adopted_at IS NULL AND cancelled_at IS NULL
         LIMIT 1
        """,
        values,
    ).fetchone()
    return row is not None


def _prepare_disposal(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    mode: CleanupMode,
    holder_id: str,
    conversation_key: str | None,
    conversation_owner_run_id: str | None,
) -> _PreparedDisposal:
    policy, conversations = _assess(conn, now=now)
    selected = _select_conversations(
        conversations,
        conversation_key=conversation_key,
        conversation_owner_run_id=conversation_owner_run_id,
    )
    prepared = _PreparedDisposal()
    if len(selected) != 1:
        prepared.action = "ineligible"
        prepared.observed = "unauthenticated_binding"
        prepared.reasons = ("unauthenticated_binding",)
        return prepared
    item = selected[0]
    existing = _load_receipt_payload(conn, item.conversation_key)
    if existing is not None:
        prepared.already_deleted, prepared.receipt_payload = existing
        return prepared
    eligible = item.manual_eligible if mode == "manual" else item.automatic_eligible
    reasons = item.manual_reasons if mode == "manual" else item.automatic_reasons
    intent_row = _load_intent_row(conn, item.conversation_key)
    if not eligible and intent_row is None:
        prepared.action = "ineligible"
        prepared.reasons = reasons
        prepared.observed = reasons[0] if reasons else "ineligible"
        return prepared
    if not eligible and intent_row is not None:
        prepared.action = "ineligible"
        prepared.reasons = reasons
        prepared.observed = "disposal_intent"
        prepared.intent = _intent_from_row(intent_row)
        return prepared
    if item.definitively_finished_at is None or not item.store_backend:
        prepared.action = "ineligible"
        prepared.reasons = ("missing_completion_evidence",)
        prepared.observed = "missing_completion_evidence"
        return prepared
    if mode == "automatic" and (policy is None or not policy.automatic_cleanup_enabled):
        prepared.action = "ineligible"
        prepared.reasons = (
            ("policy_unavailable",) if policy is None else ("automatic_cleanup_disabled",)
        )
        prepared.observed = prepared.reasons[0]
        return prepared
    generation = _acquire_fence(
        conn,
        conversation_key=item.conversation_key,
        holder_id=holder_id,
        purpose="disposal",
        now=now,
    )
    if generation is None:
        prepared.action = "fence_busy"
        prepared.observed = _disposal_observation(conn, item.conversation_key) or "fence_busy"
        prepared.reasons = (prepared.observed,)
        return prepared
    if intent_row is None:
        intent = CursorSdkDisposalIntentV1(
            intent_id=f"intent-{secrets.token_hex(16)}",
            receipt_id=f"receipt-{secrets.token_hex(16)}",
            conversation_key=item.conversation_key,
            conversation_owner_run_id=item.conversation_owner_run_id,
            store_backend=item.store_backend,  # type: ignore[arg-type]
            native_store_layout_version=1,
            mode=mode,
            definitively_finished_at=item.definitively_finished_at,
            requested_at=encode_utc_instant(now),
        )
        intent_sha = canonical_json_sha256(intent.model_dump(mode="json"))
        conn.execute(
            """
            INSERT INTO scheduler_cursor_sdk_disposals(
                conversation_key, conversation_owner_run_id, store_backend,
                native_store_layout_version, intent_id, receipt_id, status, mode,
                definitively_finished_at, requested_at, deleted_at, fence_generation,
                intent_sha256, receipt_sha256, receipt_payload, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, 'intent', ?, ?, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (
                intent.conversation_key,
                intent.conversation_owner_run_id,
                intent.store_backend,
                intent.intent_id,
                intent.receipt_id,
                intent.mode,
                intent.definitively_finished_at,
                intent.requested_at,
                generation,
                intent_sha,
                encode_utc_instant(now),
            ),
        )
        prepared.created_intent = True
    else:
        intent = _intent_from_row(intent_row)
        conn.execute(
            """
            UPDATE scheduler_cursor_sdk_disposals
               SET fence_generation = ?, updated_at = ?
             WHERE conversation_key = ? AND status = 'intent'
            """,
            (generation, encode_utc_instant(now), item.conversation_key),
        )
    prepared.generation = generation
    prepared.intent = intent
    prepared.observed = "disposal_intent"
    return prepared


def _select_conversations(
    conversations: tuple[ConversationEligibility, ...],
    *,
    conversation_key: str | None,
    conversation_owner_run_id: str | None,
) -> tuple[ConversationEligibility, ...]:
    if conversation_key is None and conversation_owner_run_id is None:
        return conversations
    selected = []
    for item in conversations:
        if conversation_key is not None and item.conversation_key != conversation_key:
            continue
        if (
            conversation_owner_run_id is not None
            and item.conversation_owner_run_id != conversation_owner_run_id
        ):
            continue
        selected.append(item)
    return tuple(selected)


def _acquire_fence(
    conn: sqlite3.Connection,
    *,
    conversation_key: str,
    holder_id: str,
    purpose: str,
    now: datetime,
) -> int | None:
    now_text = encode_utc_instant(now)
    expires = encode_utc_instant(now + timedelta(seconds=FENCE_TTL_SECONDS))
    row = conn.execute(
        """
        SELECT generation, status, expires_at
          FROM scheduler_cursor_sdk_conversation_fences
         WHERE conversation_key = ?
        """,
        (conversation_key,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO scheduler_cursor_sdk_conversation_fences(
                conversation_key, generation, holder_id, purpose, status,
                acquired_at, expires_at, updated_at
            ) VALUES (?, 1, ?, ?, 'active', ?, ?, ?)
            """,
            (conversation_key, holder_id, purpose, now_text, expires, now_text),
        )
        return 1
    generation = int(row["generation"])
    expired = True
    if row["expires_at"] is not None:
        try:
            expired = _parse_utc(str(row["expires_at"])) <= now
        except ValueError:
            expired = True
    if str(row["status"]) == "active" and not expired:
        return None
    updated = conn.execute(
        """
        UPDATE scheduler_cursor_sdk_conversation_fences
           SET generation = ?, holder_id = ?, purpose = ?, status = 'active',
               acquired_at = ?, expires_at = ?, updated_at = ?
         WHERE conversation_key = ? AND generation = ?
        """,
        (
            generation + 1,
            holder_id,
            purpose,
            now_text,
            expires,
            now_text,
            conversation_key,
            generation,
        ),
    )
    if updated.rowcount != 1:
        return None
    return generation + 1


def _abandon_unremoved_intent(
    store: SqliteSchedulerStore,
    intent: CursorSdkDisposalIntentV1,
    *,
    generation: int,
    now: datetime,
) -> None:
    with store.begin_immediate() as conn:
        conn.execute(
            """
            DELETE FROM scheduler_cursor_sdk_disposals
             WHERE conversation_key = ? AND receipt_id = ? AND status = 'intent'
            """,
            (intent.conversation_key, intent.receipt_id),
        )
        _release_fence_on(conn, intent.conversation_key, generation, now)


def _release_fence(
    store: SqliteSchedulerStore,
    conversation_key: str,
    generation: int,
    now: datetime,
) -> None:
    with store.begin_immediate() as conn:
        _release_fence_on(conn, conversation_key, generation, now)


def _release_fence_on(
    conn: sqlite3.Connection,
    conversation_key: str,
    generation: int,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE scheduler_cursor_sdk_conversation_fences
           SET status = 'inactive', purpose = 'inactive', holder_id = NULL,
               expires_at = NULL, updated_at = ?
         WHERE conversation_key = ? AND generation = ? AND status = 'active'
        """,
        (encode_utc_instant(now), conversation_key, generation),
    )


def _disposal_observation(conn: sqlite3.Connection, conversation_key: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    if row is None:
        return None
    if str(row["status"]) == "deleted":
        return "disposal_receipt"
    if str(row["status"]) == "intent":
        return "disposal_intent"
    return "disposal_intent"


def _remove_conversation_root(state_root: Path, *, conversation_key: str) -> bool:
    key = validate_cursor_conversation_key(conversation_key)
    sdk_root = state_root / "cursor-sdk"
    root = sdk_root / key
    if root.is_symlink() or sdk_root.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    if not root.exists():
        return False
    if not root.is_dir():
        raise CursorSdkPathError("unowned_root")
    resolved_root = root.resolve(strict=True)
    resolved_sdk = sdk_root.resolve(strict=True)
    if resolved_root.parent != resolved_sdk or resolved_root.name != key:
        raise CursorSdkPathError("path_escape")
    _reject_uncontained_tree(resolved_root)
    shutil.rmtree(resolved_root)
    return True


def _reject_uncontained_tree(root: Path) -> None:
    owner_uid = os.getuid() if os.name != "nt" else None
    if owner_uid is not None and root.stat().st_uid != owner_uid:
        raise CursorSdkPathError("unowned_root")
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            candidate = Path(current) / name
            if candidate.is_symlink():
                raise CursorSdkPathError("symlink_rejected")
            if owner_uid is not None and candidate.stat().st_uid != owner_uid:
                raise CursorSdkPathError("unowned_root")
            try:
                candidate.resolve(strict=True).relative_to(root)
            except ValueError as exc:
                raise CursorSdkPathError("path_escape") from exc


def _fence_generation_is_current(
    conn: sqlite3.Connection,
    conversation_key: str,
    generation: int,
) -> bool:
    row = conn.execute(
        """
        SELECT generation, status
          FROM scheduler_cursor_sdk_conversation_fences
         WHERE conversation_key = ?
        """,
        (conversation_key,),
    ).fetchone()
    return (
        row is not None and int(row["generation"]) == generation and str(row["status"]) == "active"
    )


def _finalize_receipt(
    store: SqliteSchedulerStore,
    *,
    intent: CursorSdkDisposalIntentV1,
    generation: int,
    now: datetime,
) -> tuple[CursorSdkDisposalReceiptV1, str, bool] | None:
    deleted_at = encode_utc_instant(now)
    with store.begin_immediate() as conn:
        if not _fence_generation_is_current(conn, intent.conversation_key, generation):
            return _adopt_completed_receipt(conn, intent.conversation_key)
        row = _load_intent_row(conn, intent.conversation_key)
        if row is None or str(row["status"]) == "deleted":
            return _adopt_completed_receipt(conn, intent.conversation_key)
        stored_intent = _intent_from_row(row)
        if stored_intent.receipt_id != intent.receipt_id:
            raise CursorSdkPathError("disposal_receipt_conflict")
        if int(row["fence_generation"]) != generation:
            return _adopt_completed_receipt(conn, intent.conversation_key)
        intent_sha = str(row["intent_sha256"])
        body = {
            **stored_intent.model_dump(mode="json"),
            "status": "deleted",
            "deleted_at": deleted_at,
            "intent_sha256": intent_sha,
        }
        receipt_sha = canonical_json_sha256(body)
        receipt_body = {**body, "receipt_sha256": receipt_sha}
        receipt = CursorSdkDisposalReceiptV1.model_validate(receipt_body)
        payload = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        updated = conn.execute(
            """
            UPDATE scheduler_cursor_sdk_disposals
               SET status = 'deleted', deleted_at = ?, receipt_sha256 = ?,
                   receipt_payload = ?, updated_at = ?
             WHERE conversation_key = ? AND receipt_id = ? AND status = 'intent'
               AND fence_generation = ?
            """,
            (
                deleted_at,
                receipt_sha,
                payload,
                deleted_at,
                intent.conversation_key,
                intent.receipt_id,
                generation,
            ),
        )
        if updated.rowcount != 1:
            return _adopt_completed_receipt(conn, intent.conversation_key)
        _release_fence_on(conn, intent.conversation_key, generation, now)
    return receipt, payload, False


def _adopt_completed_receipt(
    conn: sqlite3.Connection,
    conversation_key: str,
) -> tuple[CursorSdkDisposalReceiptV1, str, bool] | None:
    stored = _load_receipt_payload(conn, conversation_key)
    if stored is None:
        return None
    receipt, payload = stored
    return receipt, payload, True


def _repair_unpublished_tombstones(
    store: SqliteSchedulerStore,
    state_root: Path,
    *,
    limit: int,
) -> None:
    """Republish authenticated receipts whose tombstone is missing.

    This pass does not delete native data and does not consume the deletion
    batch, so unfinished publication cannot starve ordinary maintenance.
    """

    if limit < 1:
        return
    with store.begin_read() as conn:
        if not _has_table(conn, "scheduler_cursor_sdk_disposals"):
            return
        keys = [
            str(row["conversation_key"])
            for row in conn.execute(
                """
                SELECT conversation_key
                  FROM scheduler_cursor_sdk_disposals
                 WHERE status = 'deleted'
                 ORDER BY conversation_key
                """
            ).fetchall()
        ]
    repaired = 0
    for key in keys:
        if repaired >= limit:
            return
        with store.begin_read() as conn:
            stored = _load_receipt_payload(conn, key)
        if stored is None:
            continue
        receipt, payload_text = stored
        target = state_root / "cursor-sdk-disposals" / f"{key}.json"
        if target.is_symlink():
            continue
        if target.is_file():
            try:
                current = target.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == payload_text:
                continue
        try:
            _ensure_tombstone(state_root, receipt, payload_text=payload_text)
        except Exception:
            repaired += 1
            continue
        repaired += 1


def _assert_tombstone_publishable(state_root: Path, conversation_key: str) -> None:
    key = validate_cursor_conversation_key(conversation_key)
    _owned_real_directory(state_root)
    directory = state_root / "cursor-sdk-disposals"
    if directory.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    if directory.exists():
        _owned_real_directory(directory)
    temporary = directory / f".{key}.json.tmp"
    target = directory / f"{key}.json"
    if temporary.is_symlink() or target.is_symlink():
        raise CursorSdkPathError("symlink_rejected")


def _ensure_tombstone(
    state_root: Path,
    receipt: CursorSdkDisposalReceiptV1,
    *,
    payload_text: str | None = None,
) -> None:
    _assert_tombstone_publishable(state_root, receipt.conversation_key)
    directory = state_root / "cursor-sdk-disposals"
    _ensure_disposals_directory(directory)
    target = directory / f"{receipt.conversation_key}.json"
    if target.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    payload = (
        payload_text
        if payload_text is not None
        else json.dumps(receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    )
    temporary = directory / (
        f".{receipt.conversation_key}.{secrets.token_hex(8)}.{os.getpid()}.json.tmp"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        if temporary.is_symlink():
            raise CursorSdkPathError("symlink_rejected") from exc
        raise CursorSdkPathError("path_escape") from exc
    try:
        os.fchmod(descriptor, 0o600)
        pending = memoryview(payload.encode("utf-8"))
        while pending:
            written = os.write(descriptor, pending)
            if written < 1:
                raise OSError("Cursor SDK disposal tombstone write made no progress")
            pending = pending[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise
    else:
        os.close(descriptor)
    if target.is_symlink() or temporary.is_symlink():
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise CursorSdkPathError("symlink_rejected")
    os.replace(temporary, target)
    if target.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    set_sensitive_file_mode(target)


def _ensure_disposals_directory(directory: Path) -> None:
    """Create the publication directory, or accept a raced legitimate creator.

    ``mkdir`` can lose to a publisher that already committed the same receipt.
    The resulting path is used only when it is a real directory owned by this
    user. A symlink or a non-directory is rejected without being followed.
    """

    if directory.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    attempted = False
    if not directory.exists():
        attempted = True
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, DIR_MODE)
    if directory.is_symlink():
        raise CursorSdkPathError("symlink_rejected")
    if not directory.is_dir():
        raise CursorSdkPathError("unowned_root")
    _owned_real_directory(directory)
    if not attempted:
        return
    os.chmod(directory, DIR_MODE)
    if directory.is_symlink() or not directory.is_dir():
        raise CursorSdkPathError("symlink_rejected")
    _owned_real_directory(directory)


def _owned_real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CursorSdkPathError("symlink_rejected")
    if os.name != "nt" and path.stat().st_uid != os.getuid():
        raise CursorSdkPathError("unowned_root")


def _load_receipt_payload(
    conn: sqlite3.Connection,
    conversation_key: str,
) -> tuple[CursorSdkDisposalReceiptV1, str] | None:
    row = _load_intent_row(conn, conversation_key)
    if row is None or str(row["status"]) != "deleted" or row["receipt_payload"] is None:
        return None
    payload_text = str(row["receipt_payload"])
    payload = json.loads(payload_text)
    if not isinstance(payload, dict):
        raise CursorSdkPathError("disposal_receipt_invalid")
    digest = canonical_json_sha256(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )
    if digest != payload.get("receipt_sha256") or digest != row["receipt_sha256"]:
        raise CursorSdkPathError("disposal_receipt_invalid")
    return CursorSdkDisposalReceiptV1.model_validate(payload), payload_text


def _load_intent_row(conn: sqlite3.Connection, conversation_key: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    if row is None:
        return None
    return cast(sqlite3.Row, row)


def _intent_from_row(row: sqlite3.Row) -> CursorSdkDisposalIntentV1:
    intent = CursorSdkDisposalIntentV1(
        intent_id=str(row["intent_id"]),
        receipt_id=str(row["receipt_id"]),
        conversation_key=str(row["conversation_key"]),
        conversation_owner_run_id=str(row["conversation_owner_run_id"]),
        store_backend=str(row["store_backend"]),  # type: ignore[arg-type]
        native_store_layout_version=int(row["native_store_layout_version"]),  # type: ignore[arg-type]
        mode=str(row["mode"]),  # type: ignore[arg-type]
        definitively_finished_at=str(row["definitively_finished_at"]),
        requested_at=str(row["requested_at"]),
    )
    digest = canonical_json_sha256(intent.model_dump(mode="json"))
    if digest != str(row["intent_sha256"]):
        raise CursorSdkPathError("disposal_intent_invalid")
    return intent


def _authenticated_key(conversation_key: str, conversation_owner_run_id: str) -> str:
    key = validate_cursor_conversation_key(conversation_key)
    if cursor_conversation_key(conversation_owner_run_id) != key:
        raise CursorSdkPathError("unauthenticated_binding")
    return key


def _placeholders(values: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    return ",".join("?" for _ in values), values


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
