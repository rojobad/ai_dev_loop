"""Subordinate Phase 16.2 local-fix adapter for PR review v2 LOCAL effects.

Imports only ``local_review_loop`` public contracts from the shared package.
Carrier RunState creation/seeding is injected via ``LocalCarrierRuntime``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.local_review_loop import (
    AcceptedFinalizationResult,
    AcceptedReviewDelivery,
    LocalReviewFixRequest,
    LocalReviewFixResult,
    LocalReviewOperation,
    LocalReviewOutcome,
    ScheduledCursorTurn,
    run_local_review_fix,
)
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    LocalFixResultArtifact,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    LocalFixOutcomeKind,
    PauseReasonKind,
    SafeAction,
    SafeActionKind,
)
from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
from ai_dev_loop.pr_review_v2.domain.events import LocalFixFinishedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    ProtectedResultStore,
    ProtectedResultStoreError,
)
from ai_dev_loop.state import RunStatus, save_run_state, transition_status

CARRIER_FIX_PROMPT_RELATIVE = "prompts/fixes/01.txt"

CARRIER_CHAT_CREATE_FAILURE_SAFE = (
    "local fix carrier failed before cursor chat persistence; inspect protected artifacts"
)

_PRE_CHAT_CARRIER_FAILURE_MESSAGES = frozenset(
    {
        "Cursor chat creation timed out before a chat ID was received; "
        "inspect protected create-chat artifacts and prepare a new run",
        "Cursor chat creation failed; inspect protected create-chat artifacts and prepare a new run",
        "Cursor chat creation returned an invalid chat ID",
    }
)


class LocalFixAdapterError(Exception):
    def __init__(self, message: str = "local fix adapter failed") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class CarrierSeed:
    v2_run_id: str
    cycle_number: int
    effect_id: str
    repository_root: str
    plan_path: str
    prompt_path: str
    plan_sha256: str
    prompt_sha256: str
    plan_bytes: bytes
    prompt_bytes: bytes
    cursor_chat_id: str | None
    cursor_model: str
    cursor_command: str
    cursor_output_format: str
    cursor_force: bool
    cursor_trust_workspace: bool
    cursor_sandbox: str
    codex_session_id: str
    codex_command: str
    review_model: str
    review_reasoning_effort: str
    review_skill: str
    codex_sandbox: str
    max_local_iterations: int
    cursor_timeout_minutes: int
    codex_timeout_minutes: int
    bound_head_sha: str
    expected_branch: str
    fix_prompt_bytes: bytes


@dataclass(frozen=True)
class TerminalCarrierAcceptance:
    """Hash-verified evidence from a terminal completed carrier (no agent reopen)."""

    status: str
    chat_id: str
    codex_session_id: str
    iteration_count: int
    staged_diff_path: str
    patch_bytes: bytes
    residual_risk: bool


class LocalCarrierRuntime(Protocol):
    def ensure_seeded_carrier(self, *, carrier_run_id: str, seed: CarrierSeed) -> None: ...

    def carrier_exists(self, carrier_run_id: str) -> bool: ...

    def carrier_has_progress(self, carrier_run_id: str) -> bool: ...

    def current_head_sha(self, repo_root: str) -> str: ...

    def read_verified_staged_patch_bytes(
        self, carrier_run_id: str, relative_path: str
    ) -> bytes: ...

    def read_terminal_acceptance(
        self, carrier_run_id: str, *, expected_session_id: str
    ) -> TerminalCarrierAcceptance | None: ...

    def verify_carrier_bindings(self, *, carrier_run_id: str, seed: CarrierSeed) -> None: ...


def carrier_run_id(v2_run_id: str, cycle_number: int, effect_id: str) -> str:
    digest = hashlib.sha256(f"{v2_run_id}:{cycle_number}:{effect_id}".encode()).hexdigest()[:32]
    return f"prv2c-{digest}"


def _is_pre_chat_carrier_failure(carrier_run_id: str, exc: AiDevLoopError) -> bool:
    from ai_dev_loop.run_discovery import load_run

    if str(exc) not in _PRE_CHAT_CARRIER_FAILURE_MESSAGES:
        return False
    try:
        _run_directory, carrier_state = load_run(carrier_run_id)
    except Exception:  # noqa: BLE001
        return True
    if carrier_state.cursor.chat_id:
        return False
    if (_run_directory / "cursor" / "chat.json").is_file():
        return False
    return not carrier_state.iterations


class LocalFixAdapter:
    """Maps ``RunLocalFixEffect`` onto a deterministic subordinate local carrier."""

    def __init__(
        self,
        *,
        runtime: LocalCarrierRuntime,
        store: ProtectedResultStore,
    ) -> None:
        self._runtime = runtime
        self._store = store

    def execute(
        self,
        *,
        run_id: str,
        effect: RunLocalFixEffect,
        execution_context: ExecutionContextArtifact,
        fix_prompt_bytes: bytes,
        plan_bytes: bytes,
        prompt_bytes: bytes,
    ) -> LocalFixFinishedOutcome:
        carrier_id = carrier_run_id(run_id, effect.cycle_number, effect.effect_id)
        recovery_successor_id = getattr(self._runtime, "recovery_successor_id", None)
        if callable(recovery_successor_id):
            try:
                successor_id = recovery_successor_id(carrier_id)
            except Exception as exc:  # noqa: BLE001
                raise LocalFixAdapterError(
                    "carrier recovery successor verification failed"
                ) from exc
            if successor_id is not None:
                carrier_id = successor_id

        if hashlib.sha256(plan_bytes).hexdigest() != execution_context.plan_prompt.plan_sha256:
            raise LocalFixAdapterError("protected plan bytes hash mismatch")
        if hashlib.sha256(prompt_bytes).hexdigest() != execution_context.plan_prompt.prompt_sha256:
            raise LocalFixAdapterError("protected prompt bytes hash mismatch")
        if not plan_bytes:
            raise LocalFixAdapterError("protected plan bytes missing")
        if not prompt_bytes.strip():
            raise LocalFixAdapterError("protected prompt bytes missing")
        if not fix_prompt_bytes.strip():
            raise LocalFixAdapterError("protected fix prompt bytes missing")

        pre_commit_head = self._runtime.current_head_sha(execution_context.repository_root)
        if pre_commit_head != effect.bound_head_sha:
            raise LocalFixAdapterError("pre-commit HEAD does not match effect binding")
        if effect.binding.head_branch != execution_context.run_binding.head_branch:
            raise LocalFixAdapterError(
                "effect binding head_branch does not match frozen execution context"
            )

        chat_id = self._resolve_cursor_chat_id(
            run_id=run_id,
            effect=effect,
            execution_context=execution_context,
        )
        seed = CarrierSeed(
            v2_run_id=run_id,
            cycle_number=effect.cycle_number,
            effect_id=effect.effect_id,
            repository_root=execution_context.repository_root,
            plan_path=execution_context.plan_prompt.plan_path,
            prompt_path=execution_context.plan_prompt.prompt_path,
            plan_sha256=execution_context.plan_prompt.plan_sha256,
            prompt_sha256=execution_context.plan_prompt.prompt_sha256,
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
            cursor_chat_id=chat_id,
            cursor_model=execution_context.cursor.model,
            cursor_command=execution_context.cursor.command,
            cursor_output_format=execution_context.cursor.output_format,
            cursor_force=execution_context.cursor.force,
            cursor_trust_workspace=execution_context.cursor.trust_workspace,
            cursor_sandbox=execution_context.cursor.sandbox,
            codex_session_id=execution_context.codex.session_id,
            codex_command=execution_context.codex.command,
            review_model=execution_context.codex.review_model,
            review_reasoning_effort=execution_context.codex.review_reasoning_effort,
            review_skill=execution_context.codex.review_skill,
            codex_sandbox=execution_context.codex.sandbox,
            max_local_iterations=execution_context.workflow.max_local_iterations,
            cursor_timeout_minutes=execution_context.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=execution_context.workflow.codex_timeout_minutes,
            bound_head_sha=effect.bound_head_sha,
            expected_branch=effect.binding.head_branch,
            fix_prompt_bytes=fix_prompt_bytes,
        )

        # Recoverable boundary: terminal carrier + missing v2 result → reconstruct, no RESUME.
        if self._runtime.carrier_exists(carrier_id):
            try:
                self._runtime.verify_carrier_bindings(carrier_run_id=carrier_id, seed=seed)
            except Exception as exc:  # noqa: BLE001
                raise LocalFixAdapterError("carrier binding verification failed") from exc
            try:
                terminal = self._runtime.read_terminal_acceptance(
                    carrier_id, expected_session_id=execution_context.codex.session_id
                )
            except Exception as exc:  # noqa: BLE001
                raise LocalFixAdapterError(
                    "terminal carrier review evidence incomplete or invalid"
                ) from exc
            if terminal is not None:
                cached = self._store.read_cached_local_fix_result(effect)
                if cached is not None:
                    return self._outcome_from_cached_artifact(
                        effect=effect, artifact=cached, store_run_id=run_id
                    )
                return self._persist_from_terminal_carrier(
                    run_id=run_id,
                    effect=effect,
                    execution_context=execution_context,
                    carrier_id=carrier_id,
                    terminal=terminal,
                    pre_commit_head=pre_commit_head,
                )

        had_progress = False
        if self._runtime.carrier_exists(carrier_id):
            had_progress = self._runtime.carrier_has_progress(carrier_id)

        try:
            self._runtime.ensure_seeded_carrier(carrier_run_id=carrier_id, seed=seed)
        except Exception as exc:  # noqa: BLE001
            raise LocalFixAdapterError("carrier seed failed") from exc

        # Cursor/Codex children live in independent process groups. After a carrier
        # worker crash, fence any live owned child via production ownership metadata
        # before START/RESUME can launch a duplicate invocation.
        self._fence_orphaned_carrier_children(carrier_id)

        operation = LocalReviewOperation.RESUME if had_progress else LocalReviewOperation.START
        scheduled = ScheduledCursorTurn(
            iteration_number=1,
            prompt_path=CARRIER_FIX_PROMPT_RELATIVE,
        )

        def _on_accepted(delivery: AcceptedReviewDelivery) -> AcceptedFinalizationResult:
            # Runs under Phase 16.2 RunLocks held by execute_local_review_fix.
            from ai_dev_loop.pr_review_v2.infrastructure.paths import resolve_run_relative_path
            from ai_dev_loop.state import sha256_bytes

            review = delivery.review
            state = delivery.state
            result_message = delivery.result_message
            if review.tests_status in {
                "failed",
                "blocked_environment",
                "skipped_findings_present",
            }:
                transition_status(state.status, RunStatus.COMPLETED_WITH_RESIDUAL_RISK)
                state.status = RunStatus.COMPLETED_WITH_RESIDUAL_RISK
            else:
                transition_status(state.status, RunStatus.COMPLETED)
                state.status = RunStatus.COMPLETED
            state.result = result_message
            state.last_error = None
            # Durably record staged-patch digest for terminal-carrier recovery.
            if not state.iterations:
                raise LocalFixAdapterError("accepted carrier missing iteration evidence")
            latest = max(state.iterations, key=lambda entry: int(entry.get("number", 0)))
            git_section = latest.get("git")
            if not isinstance(git_section, dict):
                raise LocalFixAdapterError("accepted carrier missing git iteration evidence")
            staged_rel = git_section.get("staged_diff_path")
            if not isinstance(staged_rel, str) or not staged_rel:
                raise LocalFixAdapterError("accepted carrier missing staged patch path")
            try:
                patch_path = resolve_run_relative_path(delivery.run_directory, staged_rel)
            except ValueError as exc:
                raise LocalFixAdapterError("accepted carrier staged patch path is unsafe") from exc
            if not patch_path.is_file() or patch_path.is_symlink():
                raise LocalFixAdapterError("accepted carrier staged patch missing")
            patch_digest = sha256_bytes(patch_path.read_bytes())
            updated_git = {**git_section, "staged_diff_sha256": patch_digest}
            updated_latest = {**latest, "git": updated_git}
            state.iterations = [
                updated_latest
                if int(entry.get("number", 0)) == int(latest.get("number", 0))
                else entry
                for entry in state.iterations
            ]
            save_run_state(delivery.run_directory, state)
            return AcceptedFinalizationResult(
                needs_external_continuation=True,
                result_message=result_message,
            )

        request = LocalReviewFixRequest(
            run_id=carrier_id,
            operation=operation,
            scheduled_first_cursor_turn=scheduled if not had_progress else None,
            on_accepted=_on_accepted,
        )
        # Sole Phase 16.2 correction boundary — never the legacy PR-review adapter.
        try:
            result = run_local_review_fix(request)
        except AiDevLoopError as exc:
            if _is_pre_chat_carrier_failure(carrier_id, exc):
                raise LocalFixAdapterError(CARRIER_CHAT_CREATE_FAILURE_SAFE) from exc
            raise LocalFixAdapterError("local fix carrier failed") from exc
        return self._map_result(
            run_id=run_id,
            effect=effect,
            result=result,
            pre_commit_head=pre_commit_head,
            carrier_id=carrier_id,
            execution_context=execution_context,
            carried_chat_id=chat_id,
        )

    def _resolve_cursor_chat_id(
        self,
        *,
        run_id: str,
        effect: RunLocalFixEffect,
        execution_context: ExecutionContextArtifact,
    ) -> str | None:
        if execution_context.cursor.chat_id:
            return execution_context.cursor.chat_id
        # ExistingPrOrigin: carry exact chat from prior accepted protected results.
        prior = self._store.latest_accepted_cursor_chat_id(
            run_id=run_id, before_cycle=effect.cycle_number
        )
        return prior

    def _persist_from_terminal_carrier(
        self,
        *,
        run_id: str,
        effect: RunLocalFixEffect,
        execution_context: ExecutionContextArtifact,
        carrier_id: str,
        terminal: TerminalCarrierAcceptance,
        pre_commit_head: str,
    ) -> LocalFixFinishedOutcome:
        if terminal.codex_session_id != execution_context.codex.session_id:
            raise LocalFixAdapterError("terminal carrier Codex session mismatch")
        if not terminal.chat_id:
            raise LocalFixAdapterError("terminal carrier missing cursor chat id")
        if not terminal.patch_bytes:
            raise LocalFixAdapterError("terminal carrier missing staged patch")
        outcome_kind = (
            LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK
            if terminal.residual_risk
            else LocalFixOutcomeKind.ACCEPTED
        )
        try:
            patch_ref = self._store.persist_patch_bytes(run_id=run_id, data=terminal.patch_bytes)
            artifact = LocalFixResultArtifact(
                outcome=outcome_kind,
                accepted_patch_sha256=patch_ref.sha256,
                new_head_sha=pre_commit_head,
                carrier_run_id=carrier_id,
                cursor_chat_id=terminal.chat_id,
                codex_session_id=execution_context.codex.session_id,
                iteration_count=max(1, terminal.iteration_count),
                result_message_safe="local fix accepted (terminal carrier replay)",
                needs_external_continuation=True,
                run_id=run_id,
                cycle_number=effect.cycle_number,
                effect_id=effect.effect_id,
                bound_head_sha=effect.bound_head_sha,
                fix_prompt_ref_sha256=effect.fix_prompt_ref.sha256,
                execution_context_ref_sha256=effect.execution_context_ref.sha256,
            )
            result_ref = self._store.persist_local_fix_result(run_id=run_id, artifact=artifact)
        except ProtectedResultStoreError as exc:
            raise LocalFixAdapterError("failed to persist local fix result") from exc
        return LocalFixFinishedOutcome(
            outcome=outcome_kind,
            accepted_patch_ref=patch_ref,
            new_head_sha=pre_commit_head,
            result_ref=result_ref,
        )

    def _outcome_from_cached_artifact(
        self,
        *,
        effect: RunLocalFixEffect,
        artifact: LocalFixResultArtifact,
        store_run_id: str,
    ) -> LocalFixFinishedOutcome:
        del store_run_id
        if artifact.outcome in {
            LocalFixOutcomeKind.ACCEPTED,
            LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
        }:
            if artifact.accepted_patch_sha256 is None or artifact.new_head_sha is None:
                raise LocalFixAdapterError("cached accepted local fix missing fields")
            patch_ref = ArtifactRef(
                relative_path=f"local/patches/{artifact.accepted_patch_sha256}.patch",
                sha256=artifact.accepted_patch_sha256,
            )
            result_ref = self._store.persist_local_fix_result(
                run_id=effect.run_id, artifact=artifact
            )
            return LocalFixFinishedOutcome(
                outcome=artifact.outcome,
                accepted_patch_ref=patch_ref,
                new_head_sha=artifact.new_head_sha,
                result_ref=result_ref,
            )
        raise LocalFixAdapterError("cached local fix is not an accepted terminal result")

    def _fence_orphaned_carrier_children(self, carrier_id: str) -> None:
        """Terminate live owned Cursor/Codex process groups left by a crashed worker.

        Does not write an abort request: SIGTERM without a durable abort must not be
        classified as user abort. Clears stale active-process metadata after a safe
        signal/not-live outcome so resume can register a new child.
        """

        from ai_dev_loop.abort_control import (
            ProcessSignalOutcome,
            clear_active_process,
            is_stale_live_process_signal,
            read_active_process,
            signal_active_process_group,
        )
        from ai_dev_loop.run_discovery import find_run_directory

        try:
            carrier_dir = find_run_directory(carrier_id)
        except Exception:  # noqa: BLE001
            return
        if read_active_process(carrier_dir) is None:
            return
        result = signal_active_process_group(carrier_dir, run_id=carrier_id)
        if is_stale_live_process_signal(result):
            raise LocalFixAdapterError(
                "ambiguous live active-process metadata blocks local-fix resume"
            )
        if result.outcome in {
            ProcessSignalOutcome.SIGNALED,
            ProcessSignalOutcome.NOT_LIVE,
            ProcessSignalOutcome.STALE,
            ProcessSignalOutcome.SKIPPED,
        }:
            clear_active_process(carrier_dir)

    def _map_result(
        self,
        *,
        run_id: str,
        effect: RunLocalFixEffect,
        result: LocalReviewFixResult,
        pre_commit_head: str,
        carrier_id: str,
        execution_context: ExecutionContextArtifact,
        carried_chat_id: str | None,
    ) -> LocalFixFinishedOutcome:
        outcome_kind = _map_outcome(result.outcome)
        chat_id = result.chat_id or carried_chat_id or execution_context.cursor.chat_id
        try:
            if outcome_kind in {
                LocalFixOutcomeKind.ACCEPTED,
                LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
            }:
                if not result.latest_staged_diff_path:
                    raise LocalFixAdapterError("accepted local fix missing staged patch")
                if not chat_id:
                    raise LocalFixAdapterError("accepted local fix missing cursor chat id")
                try:
                    patch_bytes = self._runtime.read_verified_staged_patch_bytes(
                        carrier_id, result.latest_staged_diff_path
                    )
                except Exception as exc:  # noqa: BLE001
                    raise LocalFixAdapterError("accepted staged patch verification failed") from exc
                patch_ref = self._store.persist_patch_bytes(run_id=run_id, data=patch_bytes)
                artifact = LocalFixResultArtifact(
                    outcome=outcome_kind,
                    accepted_patch_sha256=patch_ref.sha256,
                    new_head_sha=pre_commit_head,
                    carrier_run_id=carrier_id,
                    cursor_chat_id=chat_id,
                    codex_session_id=execution_context.codex.session_id,
                    iteration_count=max(1, result.iteration_count),
                    result_message_safe="local fix accepted",
                    needs_external_continuation=True,
                    run_id=run_id,
                    cycle_number=effect.cycle_number,
                    effect_id=effect.effect_id,
                    bound_head_sha=effect.bound_head_sha,
                    fix_prompt_ref_sha256=effect.fix_prompt_ref.sha256,
                    execution_context_ref_sha256=effect.execution_context_ref.sha256,
                )
                result_ref = self._store.persist_local_fix_result(run_id=run_id, artifact=artifact)
                return LocalFixFinishedOutcome(
                    outcome=outcome_kind,
                    accepted_patch_ref=patch_ref,
                    new_head_sha=pre_commit_head,
                    result_ref=result_ref,
                )

            pause_reason, safe_action = _pause_fields(outcome_kind)
            artifact = LocalFixResultArtifact(
                outcome=outcome_kind,
                accepted_patch_sha256=None,
                new_head_sha=None,
                carrier_run_id=carrier_id,
                cursor_chat_id=chat_id,
                codex_session_id=execution_context.codex.session_id,
                iteration_count=max(1, result.iteration_count),
                result_message_safe=f"local fix ended with {outcome_kind.value}",
                needs_external_continuation=False,
                run_id=run_id,
                cycle_number=effect.cycle_number,
                effect_id=effect.effect_id,
                bound_head_sha=effect.bound_head_sha,
                fix_prompt_ref_sha256=effect.fix_prompt_ref.sha256,
                execution_context_ref_sha256=effect.execution_context_ref.sha256,
            )
            result_ref = self._store.persist_local_fix_result(run_id=run_id, artifact=artifact)
            if outcome_kind is LocalFixOutcomeKind.ABORTED:
                return LocalFixFinishedOutcome(
                    outcome=outcome_kind,
                    result_ref=result_ref,
                )
            return LocalFixFinishedOutcome(
                outcome=outcome_kind,
                result_ref=result_ref,
                pause_reason=pause_reason,
                safe_action=safe_action,
            )
        except ProtectedResultStoreError as exc:
            raise LocalFixAdapterError("failed to persist local fix result") from exc


def _map_outcome(outcome: LocalReviewOutcome) -> LocalFixOutcomeKind:
    mapping = {
        LocalReviewOutcome.ACCEPTED: LocalFixOutcomeKind.ACCEPTED,
        LocalReviewOutcome.ACCEPTED_WITH_RESIDUAL_RISK: LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
        LocalReviewOutcome.MAX_ITERATIONS_REACHED: LocalFixOutcomeKind.MAX_ITERATIONS_REACHED,
        LocalReviewOutcome.INTERRUPTED: LocalFixOutcomeKind.PAUSED,
        LocalReviewOutcome.WAITING_FOR_CURSOR_FIX: LocalFixOutcomeKind.PAUSED,
        LocalReviewOutcome.FAILED: LocalFixOutcomeKind.FAILED,
        LocalReviewOutcome.ABORTED: LocalFixOutcomeKind.ABORTED,
    }
    return mapping.get(outcome, LocalFixOutcomeKind.FAILED)


def _pause_fields(
    outcome: LocalFixOutcomeKind,
) -> tuple[PauseReasonKind, SafeAction]:
    if outcome is LocalFixOutcomeKind.MAX_ITERATIONS_REACHED:
        return (
            PauseReasonKind.LOCAL_FIX_LIMIT_REACHED,
            SafeAction(
                kind=SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
                condition="local iteration limit reached",
            ),
        )
    if outcome is LocalFixOutcomeKind.PAUSED:
        return (
            PauseReasonKind.LOCAL_FIX_PAUSED,
            SafeAction(
                kind=SafeActionKind.RESUME_SAME_EFFECT,
                condition="resume local fix carrier",
            ),
        )
    return (
        PauseReasonKind.LOCAL_FIX_FAILED,
        SafeAction(
            kind=SafeActionKind.INSPECT_ARTIFACTS,
            condition="inspect local fix failure artifacts",
        ),
    )


__all__ = [
    "CARRIER_FIX_PROMPT_RELATIVE",
    "CarrierSeed",
    "LocalCarrierRuntime",
    "LocalFixAdapter",
    "LocalFixAdapterError",
    "TerminalCarrierAcceptance",
    "carrier_run_id",
]
