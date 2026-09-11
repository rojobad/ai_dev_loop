"""Scheduler Codex review workflow for Phase 17.5."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from ai_dev_loop.iterations import correction_execution_envelope_path
from ai_dev_loop.review_result import CodexReviewResult, completion_status_for_review
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_outcome,
    load_validated_review_result,
    validate_codex_review_outcome_semantics,
)
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_ID,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_ID,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
    SCHEDULER_CODEX_BINDING_ARTIFACT,
    SCHEDULER_CODEX_UNCERTAINTY_ARTIFACT,
)
from ai_dev_loop.scheduler.domain.events import (
    CodexBootstrapUncertainEvent,
    CodexReviewBlockedEvent,
    CodexReviewerBoundEvent,
    CodexReviewScheduledEvent,
    MaxIterationsReachedEvent,
    RunCompletedEvent,
    RunCompletedWithResidualRiskEvent,
    WaitingForCursorFixEnteredEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_codex_bootstrap_uncertain,
    apply_codex_review_blocked,
    apply_codex_reviewer_bound,
    apply_max_iterations_reached,
    apply_run_completed,
    apply_run_completed_with_residual_risk,
    apply_waiting_for_cursor_fix_entered,
)
from ai_dev_loop.scheduler.domain.state import AwaitingCodexReviewState, BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_file, utc_now


class CodexWorkflowService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def process_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> list[TickRunReceipt]:
        receipts: list[TickRunReceipt] = []
        scheduled = self._maybe_schedule_codex_review_effect(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if scheduled is not None:
            receipts.append(scheduled)
        ingested = self._maybe_ingest_completed_codex_attempt(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if ingested is not None:
            receipts.append(ingested)
        return receipts

    def _maybe_schedule_codex_review_effect(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return None
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            codex_effects = [
                row
                for row in effects
                if str(row["effect_kind"])
                in {BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND, RESUME_CODEX_REVIEW_EFFECT_KIND}
            ]
            if codex_effects:
                return None
            review_iteration = state.cursor.iteration
            existing = conn.execute(
                """
                SELECT 1 FROM scheduler_effects
                WHERE run_id = ?
                  AND effect_kind IN (?, ?)
                  AND json_extract(effect_payload, '$.review_iteration') = ?
                LIMIT 1
                """,
                (
                    run_id,
                    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
                    RESUME_CODEX_REVIEW_EFFECT_KIND,
                    review_iteration,
                ),
            ).fetchone()
            if existing is not None:
                return None

        with self.store.begin_immediate() as conn:
            commit_now = self._now_factory()
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=commit_now,
            ):
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return None
            review_iteration = state.cursor.iteration
            if state.codex.reviewer_session_id:
                effect_id = RESUME_CODEX_REVIEW_EFFECT_ID
                effect_kind = RESUME_CODEX_REVIEW_EFFECT_KIND
            else:
                effect_id = BOOTSTRAP_CODEX_REVIEW_EFFECT_ID
                effect_kind = BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
            schedule_event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=schedule_event_id,
                run_id=run_id,
                sequence=sequence,
                event=CodexReviewScheduledEvent(
                    run_id=run_id,
                    review_iteration=review_iteration,
                    effect_kind=effect_kind,
                ),
                now=commit_now,
            )
            dispatch_id = self._dispatch_id_factory()
            self.store.insert_effect(
                conn,
                dispatch_id=dispatch_id,
                source_event_id=schedule_event_id,
                run_id=run_id,
                effect_id=effect_id,
                effect_kind=effect_kind,
                effect_payload={"review_iteration": review_iteration},
                available_at=commit_now,
                claimed_run_version=version,
                now=commit_now,
            )
        return TickRunReceipt(run_id=run_id, action="codex_review_scheduled", detail=effect_kind)

    def _maybe_ingest_completed_codex_attempt(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return None
            attempt = self.store.get_latest_completed_codex_attempt(conn, run_id)
            if attempt is None:
                return None
            if int(attempt["ingested"]) == 1:
                return None
        return self._ingest_codex_review(run_id, attempt)

    def _authenticated_outcome(self, run_id: str, attempt: object) -> dict[str, object]:
        attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
        unit_identity = str(attempt["unit_identity"])  # type: ignore[index]
        result_rel = str(attempt["result_artifact_path"])  # type: ignore[index]
        stdout_rel = str(attempt["stdout_artifact_path"])  # type: ignore[index]
        stderr_rel = str(attempt["stderr_artifact_path"])  # type: ignore[index]
        exit_code = attempt["exit_code"]  # type: ignore[index]
        observed_exit = int(exit_code) if exit_code is not None else None
        dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
        envelope_sha = attempt["completion_envelope_sha256"]  # type: ignore[index]
        with self.store.begin_read() as conn:
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
        expected_effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else None
        return load_authenticated_codex_outcome(
            self.artifacts.run_root(run_id),
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=observed_exit,
            expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
            expected_dispatch_id=dispatch_id,
            expected_effect_kind=expected_effect_kind,
        )

    def _ingest_codex_review(self, run_id: str, attempt: object) -> TickRunReceipt:
        attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
        run_root = self.artifacts.run_root(run_id)
        try:
            outcome = self._authenticated_outcome(run_id, attempt)
        except (CodexEvidenceError, ValueError, TypeError, KeyError, OSError):
            return self._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind="outcome_evidence_invalid",
                summary="authenticated codex outcome failed validation",
            )

        uncertainty = str(outcome.get("bootstrap_uncertainty_reason", "")).strip()
        if uncertainty:
            return self._handle_bootstrap_uncertainty(
                run_id,
                attempt_id=attempt_id,
                uncertainty_reason=uncertainty,
                outcome=outcome,
            )

        if outcome.get("timed_out"):
            return self._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind="codex_review_timeout",
                summary="codex review timed out before producing a valid schema result",
            )

        review_block_reason = str(outcome.get("review_block_reason", "")).strip()
        if review_block_reason:
            summary = (
                "codex review output truncated before a valid schema result"
                if review_block_reason == "codex_review_output_truncated"
                else "codex review blocked before producing a valid schema result"
            )
            return self._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind=review_block_reason,
                summary=summary,
            )

        failure_kind = str(outcome.get("failure_kind", "")).strip()
        if failure_kind or outcome.get("parse_ok") is False:
            stderr_rel = str(attempt["stderr_artifact_path"])  # type: ignore[index]
            stderr_path = run_root / stderr_rel
            stderr_summary = ""
            if stderr_path.is_file():
                stderr_summary = stderr_path.read_text(encoding="utf-8").strip()[:240]
            return self._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind=failure_kind or "codex_attempt_failed",
                summary=stderr_summary or "codex attempt failed before review outcome",
            )

        dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_ingest_state_changed")
            bound_session = state.codex.reviewer_session_id
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            if dispatch is None:
                return TickRunReceipt(run_id=run_id, action="codex_ingest_dispatch_missing")
            payload = dispatch["effect_payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                return self._block_review(
                    run_id,
                    attempt_id=attempt_id,
                    reason_kind="codex_dispatch_payload_invalid",
                    summary="codex effect payload is not a JSON object",
                )
            review_iteration = int(payload.get("review_iteration", state.cursor.iteration))
            effect_kind = str(outcome.get("effect_kind", ""))

        output_truncated = bool(outcome.get("stdout_truncated") or outcome.get("stderr_truncated"))
        bootstrap_session_id = str(outcome.get("bootstrap_session_id", "")).strip()
        if effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND and bootstrap_session_id:
            with self.store.begin_read() as conn:
                state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, AwaitingCodexReviewState):
                    return TickRunReceipt(run_id=run_id, action="codex_ingest_state_changed")
            bound = self._persist_reviewer_binding(
                run_id,
                state,
                bootstrap_session_id=bootstrap_session_id,
                outcome=outcome,
            )
            if isinstance(bound, TickRunReceipt):
                return bound
            bound_session = bound

        try:
            validate_codex_review_outcome_semantics(
                outcome,
                expected_review_iteration=review_iteration,
                expected_effect_kind=effect_kind,
                bound_session_id=bound_session,
            )
            review = load_validated_review_result(run_root, outcome)
        except CodexEvidenceError as exc:
            if outcome.get("timed_out"):
                return self._block_review(
                    run_id,
                    attempt_id=attempt_id,
                    reason_kind="codex_review_timeout",
                    summary="codex review timed out before producing a valid schema result",
                )
            if output_truncated:
                return self._block_review(
                    run_id,
                    attempt_id=attempt_id,
                    reason_kind="codex_review_output_truncated",
                    summary="codex review output truncated before a valid schema result",
                )
            return self._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind="codex_review_outcome_invalid",
                summary=str(exc)[:240],
            )

        return self._apply_review_decision(
            run_id,
            attempt_id=attempt_id,
            review_iteration=review_iteration,
            review=review,
            outcome=outcome,
        )

    def _persist_reviewer_binding(
        self,
        run_id: str,
        state: AwaitingCodexReviewState,
        *,
        bootstrap_session_id: str,
        outcome: dict[str, object],
    ) -> TickRunReceipt | str:
        if state.codex.reviewer_session_id:
            if state.codex.reviewer_session_id != bootstrap_session_id:
                return self._block_review(
                    run_id,
                    attempt_id=str(outcome.get("attempt_id", "")),
                    reason_kind="reviewer_identity_conflict",
                    summary="late bootstrap identity conflicts with bound reviewer",
                )
            return state.codex.reviewer_session_id
        events_rel = str(outcome.get("events_path", ""))
        events_sha = sha256_file(self.artifacts.run_root(run_id) / events_rel) if events_rel else ""
        now_text = self._now_factory().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        bound_at = now_text
        binding_path = self.artifacts.run_root(run_id) / SCHEDULER_CODEX_BINDING_ARTIFACT
        if binding_path.is_file():
            try:
                existing = json.loads(binding_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict):
                existing_prefix = str(existing.get("bootstrap_session_id_prefix", ""))
                existing_events_sha = str(existing.get("bootstrap_events_sha256", ""))
                if existing_prefix and existing_prefix != bootstrap_session_id[:8]:
                    return self._block_review(
                        run_id,
                        attempt_id=str(outcome.get("attempt_id", "")),
                        reason_kind="reviewer_binding_artifact_conflict",
                        summary="reviewer binding artifact conflicts with bootstrap identity",
                    )
                if existing_events_sha and events_sha and existing_events_sha != events_sha:
                    return self._block_review(
                        run_id,
                        attempt_id=str(outcome.get("attempt_id", "")),
                        reason_kind="reviewer_binding_artifact_conflict",
                        summary="reviewer binding artifact conflicts with bootstrap events",
                    )
                if existing_events_sha == events_sha:
                    bound_at = str(existing.get("bootstrap_bound_at", "") or now_text)
        binding_payload = {
            "review_model": state.context.codex.review_model,
            "review_reasoning_effort": state.context.codex.review_reasoning_effort,
            "bootstrap_session_id_prefix": bootstrap_session_id[:8],
            "bootstrap_events_sha256": events_sha,
            "bootstrap_bound_at": bound_at,
        }
        try:
            stored = self.artifacts.write_text_or_verify(
                run_id,
                SCHEDULER_CODEX_BINDING_ARTIFACT,
                json.dumps(binding_payload, indent=2, sort_keys=True) + "\n",
                max_bytes=16_384,
            )
        except ProtectedArtifactError as exc:
            return self._block_review(
                run_id,
                attempt_id=str(outcome.get("attempt_id", "")),
                reason_kind="reviewer_binding_artifact_conflict",
                summary=str(exc)[:240],
            )
        with self.store.begin_immediate() as conn:
            current_state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(current_state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_bind_state_changed")
            if current_state.codex.reviewer_session_id:
                if current_state.codex.reviewer_session_id != bootstrap_session_id:
                    return self._block_review(
                        run_id,
                        attempt_id=str(outcome.get("attempt_id", "")),
                        reason_kind="reviewer_identity_conflict",
                        summary="concurrent bootstrap identity conflict",
                    )
                return current_state.codex.reviewer_session_id
            event = CodexReviewerBoundEvent(
                run_id=run_id,
                reviewer_session_id_prefix=bootstrap_session_id[:8],
                binding_artifact_path=stored.relative_path,
                binding_artifact_sha256=stored.sha256,
            )
            new_state = apply_codex_reviewer_bound(
                current_state,
                event,
                now_text=now_text,
                reviewer_session_id=bootstrap_session_id,
            )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=self._now_factory(),
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=new_state,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="codex_bind_cas_lost")
        return bootstrap_session_id

    def _handle_bootstrap_uncertainty(
        self,
        run_id: str,
        *,
        attempt_id: str,
        uncertainty_reason: str,
        outcome: dict[str, object],
    ) -> TickRunReceipt:
        now = self._now_factory()
        artifact = {
            "reason": uncertainty_reason,
            "events_path": outcome.get("events_path"),
            "recorded_at": now.astimezone(UTC).isoformat(),
        }
        self.artifacts.write_text(
            run_id,
            SCHEDULER_CODEX_UNCERTAINTY_ARTIFACT,
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            max_bytes=16_384,
        )
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_uncertain_state_changed")
            event = CodexBootstrapUncertainEvent(
                run_id=run_id,
                uncertainty_reason=uncertainty_reason,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_codex_bootstrap_uncertain(state, event, now_text=now_text)
            self._append_block(
                conn, run_id=run_id, event=event, new_state=new_state, version=version, now=now
            )
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(run_id=run_id, action="blocked", detail="codex_bootstrap_uncertain")

    def _apply_review_decision(
        self,
        run_id: str,
        *,
        attempt_id: str,
        review_iteration: int,
        review: CodexReviewResult,
        outcome: dict[str, object],
    ) -> TickRunReceipt:
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result_path = str(outcome["review_result_path"])
        result_sha = str(outcome["review_result_sha256"])
        completion = completion_status_for_review(review)

        with self.store.begin_read() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_decision_state_changed")
            max_reviews = state.context.workflow.max_review_iterations

        if review.has_actionable_findings and review_iteration >= max_reviews:
            max_event = MaxIterationsReachedEvent(run_id=run_id, review_iteration=review_iteration)
            with self.store.begin_immediate() as conn:
                state, version, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, AwaitingCodexReviewState):
                    return TickRunReceipt(run_id=run_id, action="codex_decision_state_changed")
                max_state = apply_max_iterations_reached(state, max_event, now_text=now_text)
                self._append_terminal(
                    conn,
                    run_id=run_id,
                    event=max_event,
                    new_state=max_state,
                    version=version,
                    now=now,
                    attempt_id=attempt_id,
                )
            return TickRunReceipt(run_id=run_id, action="max_iterations_reached")

        if completion == "waiting_for_cursor_fix":
            fix_path = str(outcome.get("fix_prompt_path", "")).strip()
            fix_sha = str(outcome.get("fix_prompt_sha256", "")).strip()
            envelope_path = str(outcome.get("execution_envelope_path", "")).strip()
            envelope_sha = str(outcome.get("execution_envelope_sha256", "")).strip()
            if not fix_path or not fix_sha:
                return self._block_review(
                    run_id,
                    attempt_id=attempt_id,
                    reason_kind="fix_prompt_missing",
                    summary="actionable findings require persisted fix prompt",
                )
            if not envelope_path or not envelope_sha:
                return self._block_review(
                    run_id,
                    attempt_id=attempt_id,
                    reason_kind="fix_prompt_missing",
                    summary="actionable findings require persisted correction envelope",
                )
            fix_event = WaitingForCursorFixEnteredEvent(
                run_id=run_id,
                review_iteration=review_iteration,
                fix_prompt_path=fix_path,
                fix_prompt_sha256=fix_sha,
                correction_envelope_path=envelope_path,
                correction_envelope_sha256=envelope_sha,
            )
            with self.store.begin_immediate() as conn:
                state, version, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, AwaitingCodexReviewState):
                    return TickRunReceipt(run_id=run_id, action="codex_decision_state_changed")
                fix_state = apply_waiting_for_cursor_fix_entered(
                    state, fix_event, now_text=now_text
                )
                event_id = self._event_id_factory()
                sequence = self.store.next_event_sequence(conn, run_id)
                self.store.append_event(
                    conn,
                    event_id=event_id,
                    run_id=run_id,
                    sequence=sequence,
                    event=fix_event,
                    now=now,
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=fix_state,
                    now=now,
                ):
                    return TickRunReceipt(run_id=run_id, action="codex_decision_cas_lost")
                self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
                from ai_dev_loop.scheduler.domain.cursor_contract import (
                    RUN_CURSOR_TURN_EFFECT_ID,
                    RUN_CURSOR_TURN_EFFECT_KIND,
                )

                next_dispatch = self._dispatch_id_factory()
                envelope_path = correction_execution_envelope_path(review_iteration)
                self.store.insert_effect(
                    conn,
                    dispatch_id=next_dispatch,
                    source_event_id=event_id,
                    run_id=run_id,
                    effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                    effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                    effect_payload={
                        "iteration": review_iteration + 1,
                        "prompt_path": envelope_path,
                    },
                    available_at=now,
                    claimed_run_version=fix_state.version,
                    now=now,
                )
            return TickRunReceipt(run_id=run_id, action="waiting_for_cursor_fix")

        if completion == "completed_with_residual_risk":
            residual_event = RunCompletedWithResidualRiskEvent(
                run_id=run_id,
                review_iteration=review_iteration,
            )
            with self.store.begin_immediate() as conn:
                state, version, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, AwaitingCodexReviewState):
                    return TickRunReceipt(run_id=run_id, action="codex_decision_state_changed")
                residual_state = apply_run_completed_with_residual_risk(
                    state, residual_event, now_text=now_text
                )
                residual_state = residual_state.model_copy(
                    update={
                        "codex": residual_state.codex.model_copy(
                            update={
                                "latest_review_result_path": result_path,
                                "latest_review_result_sha256": result_sha,
                            }
                        )
                    }
                )
                self._append_terminal(
                    conn,
                    run_id=run_id,
                    event=residual_event,
                    new_state=residual_state,
                    version=version,
                    now=now,
                    attempt_id=attempt_id,
                )
            return TickRunReceipt(run_id=run_id, action="completed_with_residual_risk")

        completed_event = RunCompletedEvent(run_id=run_id, review_iteration=review_iteration)
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_decision_state_changed")
            completed_state = apply_run_completed(state, completed_event, now_text=now_text)
            completed_state = completed_state.model_copy(
                update={
                    "codex": completed_state.codex.model_copy(
                        update={
                            "latest_review_result_path": result_path,
                            "latest_review_result_sha256": result_sha,
                        }
                    )
                }
            )
            self._append_terminal(
                conn,
                run_id=run_id,
                event=completed_event,
                new_state=completed_state,
                version=version,
                now=now,
                attempt_id=attempt_id,
            )
        return TickRunReceipt(run_id=run_id, action="completed")

    def block_launch_guard_failure(
        self,
        run_id: str,
        *,
        attempt_id: str,
        reason_kind: str,
        summary: str,
    ) -> TickRunReceipt:
        return self._block_review(
            run_id,
            attempt_id=attempt_id,
            reason_kind=reason_kind,
            summary=summary,
        )

    def _block_review(
        self,
        run_id: str,
        *,
        attempt_id: str,
        reason_kind: str,
        summary: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return TickRunReceipt(run_id=run_id, action="codex_block_state_changed")
            event = CodexReviewBlockedEvent(
                run_id=run_id,
                block_reason_kind=reason_kind,
                block_reason_summary=summary,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_codex_review_blocked(state, event, now_text=now_text)
            self._append_block(
                conn, run_id=run_id, event=event, new_state=new_state, version=version, now=now
            )
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(run_id=run_id, action="blocked", detail=reason_kind)

    def _append_block(
        self,
        conn: object,
        *,
        run_id: str,
        event: object,
        new_state: BlockedState,
        version: int,
        now: datetime,
    ) -> None:
        event_id = self._event_id_factory()
        sequence = self.store.next_event_sequence(conn, run_id)  # type: ignore[arg-type]
        self.store.append_event(
            conn,  # type: ignore[arg-type]
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            event=event,  # type: ignore[arg-type]
            now=now,
        )
        self.store.compare_and_swap_state(
            conn,  # type: ignore[arg-type]
            run_id=run_id,
            expected_version=version,
            new_state=new_state,
            now=now,
        )
        self.store.release_reservation(
            conn,  # type: ignore[arg-type]
            worktree_key=new_state.context.repository.worktree_key,
            now=now,
        )

    def _append_terminal(
        self,
        conn: object,
        *,
        run_id: str,
        event: object,
        new_state: object,
        version: int,
        now: datetime,
        attempt_id: str,
    ) -> None:
        event_id = self._event_id_factory()
        sequence = self.store.next_event_sequence(conn, run_id)  # type: ignore[arg-type]
        self.store.append_event(
            conn,  # type: ignore[arg-type]
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            event=event,  # type: ignore[arg-type]
            now=now,
        )
        self.store.compare_and_swap_state(
            conn,  # type: ignore[arg-type]
            run_id=run_id,
            expected_version=version,
            new_state=new_state,  # type: ignore[arg-type]
            now=now,
        )
        self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)  # type: ignore[arg-type]
        self.store.release_reservation(
            conn,  # type: ignore[arg-type]
            worktree_key=new_state.context.repository.worktree_key,  # type: ignore[attr-defined]
            now=now,
        )
