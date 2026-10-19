"""Provider-neutral authorization for bounded implementer timeout continuation."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_implementer_outcome,
    verify_codex_implementer_invocation_evidence,
)
from ai_dev_loop.scheduler.application.contracts import (
    AppModel,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.cursor_evidence import verify_prompt_binding
from ai_dev_loop.scheduler.application.cursor_runtime_policy import (
    CURSOR_CLI_RETIRED_SUMMARY,
    context_uses_cursor_cli,
)
from ai_dev_loop.scheduler.application.cursor_timeout_retry import CursorTimeoutRetryService
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_IMPLEMENTER_TIMEOUT_MAX_AUTOMATIC_RETRIES,
    CODEX_IMPLEMENTER_TIMEOUT_RETRY_DELAY_SECONDS,
    CODEX_IMPLEMENTER_TIMEOUT_RETRY_POLICY_VERSION,
    IMPLEMENTER_ATTEMPT_EFFECT_KINDS,
    MAX_CODEX_IMPLEMENTER_TIMEOUT_INTENT_BYTES,
    RESUME_CODEX_IMPLEMENTER_EFFECT_ID,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    codex_implementer_timeout_intent_rel,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.events import CodexImplementerTimeoutRetryEvent
from ai_dev_loop.scheduler.domain.state import (
    CodexImplementerTimeoutContinuation,
    CodexWorkflowCheckpoint,
    PreflightCompleteState,
    WaitingForCursorFixState,
    resolved_implementer_provider,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    resolve_run_relative_path,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now

CodexTurnState = PreflightCompleteState | WaitingForCursorFixState
CODEX_TURN_STATES = (PreflightCompleteState, WaitingForCursorFixState)


class ImplementerRetryResult(AppModel):
    run_id: str
    provider: Literal["cursor", "codex"]
    changed: bool
    available_at: str | None
    automatic_retries: int


class CodexImplementerTimeoutEvidenceError(SchedulerEngineError):
    """Authenticated timeout evidence cannot safely authorize continuation."""

    def __init__(self, message: str) -> None:
        super().__init__(SchedulerEngineErrorKind.VALIDATION, message)


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def _evidence_invalid(message: str) -> CodexImplementerTimeoutEvidenceError:
    return CodexImplementerTimeoutEvidenceError(message)


def _authenticated_timeout_binding(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Authenticate the terminal attempt, invocation, outcome, and timeout class."""

    attempt = store.get_attempt_by_id(conn, attempt_id)
    if (
        attempt is None
        or str(attempt["run_id"]) != run_id
        or str(attempt["status"]) not in {"completed", "failed"}
        or not attempt["completion_envelope_sha256"]
        or attempt["exit_code"] != 124
        or attempt["termination_class"] != TerminationClass.TIMEOUT.value
    ):
        raise _evidence_invalid("Codex retry requires a confirmed terminal timeout")
    dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if dispatch is None or str(dispatch["effect_kind"]) not in IMPLEMENTER_ATTEMPT_EFFECT_KINDS:
        raise _evidence_invalid("Only Codex implementer timeouts can be retried")
    root = artifacts.run_root(run_id)
    effect_kind = str(dispatch["effect_kind"])
    evidence = verify_codex_implementer_invocation_evidence(
        root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=str(attempt["dispatch_id"]),
        unit_identity=str(attempt["unit_identity"]),
        launch_nonce=str(attempt["launch_nonce"]),
        launch_intent_sha256=str(attempt["launch_intent_sha256"]),
        effect_kind=effect_kind,
    )
    outcome = load_authenticated_codex_implementer_outcome(
        root,
        attempt_id=attempt_id,
        unit_identity=str(attempt["unit_identity"]),
        result_rel=str(attempt["result_artifact_path"]),
        stdout_rel=str(attempt["stdout_artifact_path"]),
        stderr_rel=str(attempt["stderr_artifact_path"]),
        observed_exit_code=124,
        observed_termination=TerminationClass.TIMEOUT,
        expected_envelope_sha256=str(attempt["completion_envelope_sha256"]),
        expected_dispatch_id=str(attempt["dispatch_id"]),
        expected_effect_kind=effect_kind,
    )
    if outcome.get("timed_out") is not True or outcome.get("output_incomplete") is True:
        raise _evidence_invalid("Codex timeout evidence is unsafe or incomplete")
    if outcome.get("turn") != evidence.get("turn"):
        raise _evidence_invalid("Codex timeout turn disagrees with invocation")
    if outcome.get("prompt_sha256") != evidence.get("prompt_sha256"):
        raise _evidence_invalid("Codex timeout prompt digest disagrees with invocation")
    if outcome.get("prompt_artifact_path") != evidence.get("prompt_path"):
        raise _evidence_invalid("Codex timeout prompt path disagrees with invocation")
    return evidence, outcome


def _timeout_binding(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        return _authenticated_timeout_binding(store, artifacts, conn, run_id, attempt_id)
    except CodexImplementerTimeoutEvidenceError:
        raise
    except (CodexEvidenceError, ValueError, KeyError, TypeError, OSError) as exc:
        raise _evidence_invalid(
            "Codex timeout evidence is missing, conflicting, or invalid"
        ) from exc


class CodexImplementerTimeoutRetryService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory
        self.event_id = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self.dispatch_id = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def record_timeout(
        self,
        run_id: str,
        attempt_id: str,
        *,
        binding_artifact_path: str,
        binding_artifact_sha256: str,
    ) -> TickRunReceipt:
        now = self.now()
        try:
            with self.store.begin_read() as conn:
                state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                attempt = self.store.get_attempt_by_id(conn, attempt_id)
                if attempt is None or int(attempt["ingested"]):
                    return TickRunReceipt(run_id=run_id, action="implementer_result_stale")
                if not isinstance(state, CODEX_TURN_STATES):
                    return TickRunReceipt(run_id=run_id, action="implementer_ingest_state_changed")
                evidence, outcome = _timeout_binding(
                    self.store, self.artifacts, conn, run_id, attempt_id
                )
                session_id = self._check_current_binding(state, evidence, outcome)
                self._verify_implementer_binding(
                    state,
                    session_id=session_id,
                    binding_artifact_path=binding_artifact_path,
                    binding_artifact_sha256=binding_artifact_sha256,
                )
                self._require_idle(conn, run_id)
                intent_bytes = self._intent_bytes(
                    state,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    evidence=evidence,
                    binding_artifact_path=binding_artifact_path,
                    binding_artifact_sha256=binding_artifact_sha256,
                )
        except CodexImplementerTimeoutEvidenceError:
            raise
        except (CodexEvidenceError, ValueError, TypeError) as exc:
            raise _evidence_invalid(
                "Codex timeout evidence is missing, conflicting, or invalid"
            ) from exc

        try:
            stored = self.artifacts.publish_or_verify_bytes(
                run_id,
                codex_implementer_timeout_intent_rel(attempt_id),
                intent_bytes,
                max_bytes=MAX_CODEX_IMPLEMENTER_TIMEOUT_INTENT_BYTES,
            )
        except ProtectedArtifactError as exc:
            raise _evidence_invalid(
                "Codex timeout evidence is missing, conflicting, or invalid"
            ) from exc

        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            attempt = self.store.get_attempt_by_id(conn, attempt_id)
            if attempt is None or int(attempt["ingested"]):
                return TickRunReceipt(run_id=run_id, action="implementer_result_stale")
            if not isinstance(state, CODEX_TURN_STATES):
                return TickRunReceipt(run_id=run_id, action="implementer_ingest_state_changed")
            evidence, outcome = _timeout_binding(
                self.store, self.artifacts, conn, run_id, attempt_id
            )
            session_id = self._check_current_binding(state, evidence, outcome)
            self._verify_implementer_binding(
                state,
                session_id=session_id,
                binding_artifact_path=binding_artifact_path,
                binding_artifact_sha256=binding_artifact_sha256,
            )
            self._require_idle(conn, run_id)
            prior = self._codex(state).implementer_timeout_continuation
            count = self._codex(state).implementer_timeout_automatic_retries_consumed
            generation = 1
            if prior is not None:
                if prior.turn != evidence["turn"] or prior.iteration != state.cursor.iteration:
                    raise _evidence_invalid(
                        "Codex timeout continuation belongs to a different turn"
                    )
                count = prior.automatic_retries
                generation = prior.retry_generation + 1
            automatic = count < CODEX_IMPLEMENTER_TIMEOUT_MAX_AUTOMATIC_RETRIES
            due = (
                now + timedelta(seconds=CODEX_IMPLEMENTER_TIMEOUT_RETRY_DELAY_SECONDS)
                if automatic
                else None
            )
            continuation = CodexImplementerTimeoutContinuation(
                turn=str(evidence["turn"]),  # type: ignore[arg-type]
                iteration=state.cursor.iteration,
                prompt_sha256=str(evidence["prompt_sha256"]),
                prompt_artifact_path=str(evidence["prompt_path"]),
                session_id=session_id,
                source_attempt_id=attempt_id,
                intent_path=stored.relative_path,
                intent_sha256=stored.sha256,
                retry_generation=generation,
                automatic_retries=count + int(automatic),
                scheduled_generation=generation if automatic else None,
                wait_until=encode_utc_instant(due) if due else None,
            )
            event = self._event(
                state,
                continuation,
                action="automatic" if automatic else "exhausted",
            )
            self._persist(
                conn,
                state,
                version,
                event,
                continuation,
                now,
                schedule=automatic,
                binding_artifact_path=binding_artifact_path,
                binding_artifact_sha256=binding_artifact_sha256,
            )
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(
            run_id=run_id,
            action=(
                "codex_implementer_timeout_retry_scheduled"
                if automatic
                else "codex_implementer_timeout_retry_exhausted"
            ),
        )

    def authenticate_dispatch(
        self,
        conn: sqlite3.Connection,
        state: CodexTurnState,
        *,
        effect_payload: dict[str, object],
    ) -> CodexImplementerTimeoutContinuation:
        """Re-authenticate a durable timeout immediately before retry dispatch."""

        continuation = self._codex(state).implementer_timeout_continuation
        if continuation is None:
            raise _evidence_invalid("Codex timeout continuation is missing")
        attempt = self.store.get_attempt_by_id(conn, continuation.source_attempt_id)
        if attempt is None or not int(attempt["ingested"]):
            raise _evidence_invalid("Codex timeout source attempt was not durably ingested")
        evidence, outcome = _timeout_binding(
            self.store,
            self.artifacts,
            conn,
            state.run_id,
            continuation.source_attempt_id,
        )
        if self._check_current_binding(state, evidence, outcome) != continuation.session_id:
            raise _evidence_invalid("Codex timeout session no longer matches the current binding")
        binding_path = self._codex(state).implementer_binding_artifact_path
        binding_sha = self._codex(state).implementer_binding_artifact_sha256
        if binding_path is None or binding_sha is None:
            raise _evidence_invalid("Codex timeout implementer binding is missing")
        self._verify_implementer_binding(
            state,
            session_id=continuation.session_id,
            binding_artifact_path=binding_path,
            binding_artifact_sha256=binding_sha,
        )
        self._verify_intent(state.run_id, continuation)
        capacity = self._codex(state).capacity_continuation
        if capacity is None or capacity.role != "implementer":
            expected_payload = {
                "timeout_retry_generation": continuation.retry_generation,
                "timeout_retry_of": continuation.source_attempt_id,
                "prompt_path": continuation.prompt_artifact_path,
            }
            if any(effect_payload.get(key) != value for key, value in expected_payload.items()):
                raise _evidence_invalid("Codex timeout effect does not match durable continuation")
        return continuation

    def retry(self, run_id: str) -> ImplementerRetryResult:
        now = self.now()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, CODEX_TURN_STATES):
                raise _invalid("Run has no retryable Codex implementer timeout")
            self._require_idle(conn, run_id)
            continuation = self._codex(state).implementer_timeout_continuation
            if continuation is None:
                raise _invalid("Run has no retryable Codex implementer timeout")
            evidence, outcome = _timeout_binding(
                self.store,
                self.artifacts,
                conn,
                run_id,
                continuation.source_attempt_id,
            )
            if self._check_current_binding(state, evidence, outcome) != continuation.session_id:
                raise _invalid("Codex timeout session no longer matches the current binding")
            binding_path = self._codex(state).implementer_binding_artifact_path
            binding_sha = self._codex(state).implementer_binding_artifact_sha256
            if binding_path is None or binding_sha is None:
                raise _invalid("Codex timeout implementer binding is missing")
            self._verify_implementer_binding(
                state,
                session_id=continuation.session_id,
                binding_artifact_path=binding_path,
                binding_artifact_sha256=binding_sha,
            )
            self._verify_intent(run_id, continuation)
            live = conn.execute(
                "SELECT * FROM scheduler_effects WHERE run_id=? "
                "AND status IN ('pending','claimed') AND effect_kind=?",
                (run_id, RESUME_CODEX_IMPLEMENTER_EFFECT_KIND),
            ).fetchall()
            if len(live) > 1:
                raise _invalid("Multiple live Codex timeout continuations")
            if live:
                row = live[0]
                if str(row["status"]) == "claimed" or str(
                    row["available_at"]
                ) <= encode_utc_instant(now):
                    return self._result(run_id, continuation, changed=False)
                automatic_retries = max(continuation.automatic_retries - 1, 0)
                advanced = continuation.model_copy(
                    update={
                        "automatic_retries": automatic_retries,
                        "wait_until": encode_utc_instant(now),
                    }
                )
                event = self._event(state, advanced, action="manual")
                self._persist(
                    conn,
                    state,
                    version,
                    event,
                    advanced,
                    now,
                    schedule=False,
                    binding_artifact_path=self._codex(state).implementer_binding_artifact_path,
                    binding_artifact_sha256=self._codex(state).implementer_binding_artifact_sha256,
                )
                conn.execute(
                    "UPDATE scheduler_effects SET available_at=?, claimed_run_version=?, updated_at=? "
                    "WHERE dispatch_id=? AND status='pending'",
                    (
                        encode_utc_instant(now),
                        version + 1,
                        encode_utc_instant(now),
                        row["dispatch_id"],
                    ),
                )
                return self._result(run_id, advanced, changed=True)
            if continuation.scheduled_generation is not None:
                return self._result(run_id, continuation, changed=False)
            authorized = continuation.model_copy(
                update={
                    "scheduled_generation": continuation.retry_generation,
                    "wait_until": encode_utc_instant(now),
                }
            )
            event = self._event(state, authorized, action="manual")
            self._persist(
                conn,
                state,
                version,
                event,
                authorized,
                now,
                schedule=True,
                binding_artifact_path=self._codex(state).implementer_binding_artifact_path,
                binding_artifact_sha256=self._codex(state).implementer_binding_artifact_sha256,
            )
            return self._result(run_id, authorized, changed=True)

    def _require_idle(self, conn: sqlite3.Connection, run_id: str) -> None:
        if self.store.get_reservation_for_run(conn, run_id) is None:
            raise _invalid("Implementer retry requires the run's repository reservation")
        if (
            self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None
            or self.store.has_abort_requested_for_run(conn, run_id)
            or self.store.has_unresolved_abort_hold(conn, run_id)
            or self.store.has_checkpoint_reconciliation_hold(conn, run_id)
        ):
            raise _invalid("Implementer retry requires confirmed termination and no pending abort")

    @staticmethod
    def _codex(state: CodexTurnState) -> CodexWorkflowCheckpoint:
        return state.codex if state.codex is not None else CodexWorkflowCheckpoint()

    def _verify_implementer_binding(
        self,
        state: CodexTurnState,
        *,
        session_id: str,
        binding_artifact_path: str,
        binding_artifact_sha256: str,
    ) -> None:
        implementer = state.context.implementer
        if implementer is None or implementer.provider != "codex":
            raise _invalid("Codex implementer settings are missing")
        try:
            path = resolve_run_relative_path(
                self.artifacts.run_root(state.run_id), binding_artifact_path
            )
            content = path.read_bytes()
            payload = json.loads(content.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _evidence_invalid("Codex implementer binding artifact is invalid") from exc
        if hashlib.sha256(content).hexdigest() != binding_artifact_sha256:
            raise _evidence_invalid("Codex implementer binding artifact digest mismatch")
        if not isinstance(payload, dict) or any(
            payload.get(key) != value
            for key, value in {
                "role": "implementer",
                "purpose": "implementation",
                "session_id": session_id,
                "model": implementer.model,
                "reasoning_effort": implementer.reasoning_effort,
            }.items()
        ):
            raise _evidence_invalid("Codex implementer binding artifact conflicts with retry")

    def _check_current_binding(
        self,
        state: CodexTurnState,
        evidence: dict[str, object],
        outcome: dict[str, object],
    ) -> str:
        implementer = state.context.implementer
        if implementer is None or implementer.provider != "codex":
            raise _evidence_invalid("Run does not have frozen Codex implementer settings")
        turn = "correction" if isinstance(state, WaitingForCursorFixState) else "initial"
        expected_prompt_path = (
            state.codex.latest_correction_envelope_path
            if isinstance(state, WaitingForCursorFixState)
            else state.context.plan_prompt.prompt_artifact_path
        )
        expected_prompt_sha = (
            state.codex.latest_correction_envelope_sha256
            if isinstance(state, WaitingForCursorFixState)
            else state.context.plan_prompt.prompt_sha256
        )
        existing = self._codex(state).implementer_timeout_continuation
        capacity = self._codex(state).capacity_continuation
        if capacity is not None and capacity.role == "implementer":
            expected_prompt_path = capacity.prompt_artifact_path
            expected_prompt_sha = capacity.prompt_sha256
        elif existing is not None:
            expected_prompt_path = existing.prompt_artifact_path
            expected_prompt_sha = existing.prompt_sha256
        exact = {
            "turn": turn,
            "iteration": state.cursor.iteration,
            "prompt_path": expected_prompt_path,
            "prompt_sha256": expected_prompt_sha,
            "codex_command": implementer.command,
            "model": implementer.model,
            "reasoning_effort": implementer.reasoning_effort,
            "timeout_seconds": implementer.timeout_minutes * 60,
            "codex_sandbox": implementer.sandbox,
            "approval_policy": implementer.approval_policy,
        }
        for key, value in exact.items():
            if evidence.get(key) != value:
                raise _evidence_invalid(f"Codex timeout changed frozen {key}")
        try:
            verify_prompt_binding(
                self.artifacts.run_root(state.run_id),
                prompt_path=str(expected_prompt_path),
                prompt_sha256=str(expected_prompt_sha),
            )
        except (ValueError, OSError) as exc:
            raise _evidence_invalid("Codex timeout prompt binding is invalid") from exc
        session_id = str(outcome.get("session_id", "")).strip()
        bound_session = self._codex(state).implementer_session_id
        if not session_id or (bound_session is not None and session_id != bound_session):
            raise _evidence_invalid("Codex timeout lacks the exact bound implementer identity")
        evidence_session = str(evidence.get("session_id", "")).strip()
        if evidence.get("effect_kind") == RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
            if evidence_session != session_id:
                raise _evidence_invalid("Codex timeout resume identity conflicts with invocation")
        elif evidence_session:
            raise _evidence_invalid("Codex timeout bootstrap invocation carried a session")
        return session_id

    @staticmethod
    def _intent_bytes(
        state: CodexTurnState,
        *,
        attempt_id: str,
        session_id: str,
        evidence: dict[str, object],
        binding_artifact_path: str,
        binding_artifact_sha256: str,
    ) -> bytes:
        payload = {
            "schema_version": 1,
            "policy_version": CODEX_IMPLEMENTER_TIMEOUT_RETRY_POLICY_VERSION,
            "run_id": state.run_id,
            "source_attempt_id": attempt_id,
            "source_dispatch_id": evidence["dispatch_id"],
            "source_effect_kind": evidence["effect_kind"],
            "turn": evidence["turn"],
            "iteration": state.cursor.iteration,
            "session_id": session_id,
            "prompt_artifact_path": evidence["prompt_path"],
            "prompt_sha256": evidence["prompt_sha256"],
            "codex_command": evidence["codex_command"],
            "model": evidence["model"],
            "reasoning_effort": evidence["reasoning_effort"],
            "timeout_seconds": evidence["timeout_seconds"],
            "codex_sandbox": evidence["codex_sandbox"],
            "approval_policy": evidence["approval_policy"],
            "implementer_binding_artifact_path": binding_artifact_path,
            "implementer_binding_artifact_sha256": binding_artifact_sha256,
        }
        return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def _verify_intent(
        self, run_id: str, continuation: CodexImplementerTimeoutContinuation
    ) -> None:
        try:
            path = resolve_run_relative_path(
                self.artifacts.run_root(run_id), continuation.intent_path
            )
            content = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise _evidence_invalid("Codex timeout intent is missing") from exc
        if hashlib.sha256(content).hexdigest() != continuation.intent_sha256:
            raise _evidence_invalid("Codex timeout intent digest mismatch")

    @staticmethod
    def _event(
        state: CodexTurnState,
        continuation: CodexImplementerTimeoutContinuation,
        *,
        action: Literal["automatic", "manual", "exhausted"],
    ) -> CodexImplementerTimeoutRetryEvent:
        return CodexImplementerTimeoutRetryEvent(
            run_id=state.run_id,
            attempt_id=continuation.source_attempt_id,
            iteration=continuation.iteration,
            turn=continuation.turn,
            retry_generation=continuation.retry_generation,
            automatic_retries=continuation.automatic_retries,
            action=action,
            available_at=continuation.wait_until,
        )

    def _persist(
        self,
        conn: sqlite3.Connection,
        state: CodexTurnState,
        version: int,
        event: CodexImplementerTimeoutRetryEvent,
        continuation: CodexImplementerTimeoutContinuation,
        now: datetime,
        *,
        schedule: bool,
        binding_artifact_path: str | None,
        binding_artifact_sha256: str | None,
    ) -> None:
        codex = self._codex(state).model_copy(
            update={
                "implementer_session_id": continuation.session_id,
                "implementer_binding_artifact_path": binding_artifact_path,
                "implementer_binding_artifact_sha256": binding_artifact_sha256,
                "implementer_timeout_continuation": continuation,
                "implementer_timeout_automatic_retries_consumed": (continuation.automatic_retries),
                "capacity_continuation": None,
                "capacity_evidence_source": None,
                "inferred_operational_failure_kind": None,
            }
        )
        updated = state.model_copy(
            update={"codex": codex, "version": version + 1, "updated_at": encode_utc_instant(now)}
        )
        event_id = self.event_id()
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=state.run_id,
            sequence=self.store.next_event_sequence(conn, state.run_id),
            event=event,
            now=now,
        )
        if not self.store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=version,
            new_state=updated,
            now=now,
        ):
            raise _invalid("Implementer retry lost concurrent state update")
        if schedule:
            assert continuation.wait_until is not None
            self.store.insert_effect(
                conn,
                dispatch_id=self.dispatch_id(),
                source_event_id=event_id,
                run_id=state.run_id,
                effect_id=RESUME_CODEX_IMPLEMENTER_EFFECT_ID,
                effect_kind=RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
                effect_payload={
                    "iteration": continuation.iteration,
                    "turn": continuation.turn,
                    "prompt_path": continuation.prompt_artifact_path,
                    "timeout_retry_generation": continuation.retry_generation,
                    "timeout_retry_of": continuation.source_attempt_id,
                },
                available_at=datetime.fromisoformat(continuation.wait_until.replace("Z", "+00:00")),
                claimed_run_version=updated.version,
                now=now,
            )

    @staticmethod
    def _result(
        run_id: str,
        continuation: CodexImplementerTimeoutContinuation,
        *,
        changed: bool,
    ) -> ImplementerRetryResult:
        return ImplementerRetryResult(
            run_id=run_id,
            provider="codex",
            changed=changed,
            available_at=continuation.wait_until,
            automatic_retries=continuation.automatic_retries,
        )


class ImplementerRetryService:
    """Dispatch the shared CLI contract without changing provider-specific policy."""

    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory

    def retry(self, run_id: str, *, force: bool = False) -> ImplementerRetryResult:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            provider = resolved_implementer_provider(state.context)
            if context_uses_cursor_cli(state.context):
                raise _invalid(f"scheduler implementer retry: {CURSOR_CLI_RETIRED_SUMMARY}")
        if force:
            if provider == "cursor":
                from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
                    CursorInitialRecoveryService,
                )

                recovery_result = CursorInitialRecoveryService(
                    self.store,
                    self.artifacts,
                    now_factory=self.now,
                ).force(run_id)
                return ImplementerRetryResult(
                    run_id=recovery_result.successor_run_id,
                    provider="cursor",
                    changed=recovery_result.changed,
                    available_at=None,
                    automatic_retries=0,
                )
            from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                CodexImplementerFailureRecoveryService,
            )

            successor_run_id, changed = CodexImplementerFailureRecoveryService(
                self.store,
                self.artifacts,
                now_factory=self.now,
            ).force(run_id)
            return ImplementerRetryResult(
                run_id=successor_run_id,
                provider="codex",
                changed=changed,
                available_at=None,
                automatic_retries=0,
            )
        if provider == "cursor":
            timeout_result = CursorTimeoutRetryService(
                self.store, self.artifacts, now_factory=self.now
            ).retry(run_id)
            return ImplementerRetryResult(
                run_id=timeout_result.run_id,
                provider="cursor",
                changed=timeout_result.changed,
                available_at=timeout_result.available_at,
                automatic_retries=timeout_result.automatic_retries,
            )
        return CodexImplementerTimeoutRetryService(
            self.store, self.artifacts, now_factory=self.now
        ).retry(run_id)


def scheduler_implementer_retry(
    run_id: str,
    *,
    force: bool = False,
    db_path: Path | None = None,
) -> ImplementerRetryResult:
    return ImplementerRetryService(
        SqliteSchedulerStore(db_path or default_engine_db_path()),
        ProtectedArtifactStore(default_artifact_root()),
    ).retry(run_id, force=force)
