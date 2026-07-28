"""LOCAL effect executor for PR review v2 (publication, adjudication, local fix)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Protocol

from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExternalAdjudicationResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import ClaimAuthorityGuard
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    ArtifactRef,
    EffectCompletionToken,
    ErrorSummary,
    FrozenThreadSet,
    LocalFixOutcomeKind,
    PauseReasonKind,
    SafeAction,
    SafeActionKind,
    ThreadDecisionRecord,
    TransientErrorKind,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
    PrReviewEffect,
    RunLocalFixEffect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    AdjudicationRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    LocalFixFinishedOutcome,
    PublicationTextPreparedOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    CodexLocalRunnerError,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    LocalFixAdapter,
    LocalFixAdapterError,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    ProtectedResultStore,
    ProtectedResultStoreError,
)
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    MAX_OBSERVATION_ARTIFACT_BYTES,
    ArtifactStoreError,
    ReviewArtifactStore,
)

LocalResult = EffectSucceeded | EffectRetryableFailure | EffectBlocked


class RunExecutionContextResolver(Protocol):
    """Resolve the frozen execution context for a run (from origin ref)."""

    def resolve(self, run_id: str) -> tuple[ExecutionContextArtifact, ArtifactRef]: ...


class LocalEffectExecutor:
    """Executes LOCAL effects under the same claim fence as other effect classes."""

    requires_authority = False

    def __init__(
        self,
        *,
        store: ProtectedResultStore,
        publication_runner: PublicationTextRunner,
        adjudication_runner: ThreadAdjudicationRunner,
        local_fix_adapter: LocalFixAdapter,
        context_resolver: RunExecutionContextResolver,
    ) -> None:
        self._store = store
        self._publication = publication_runner
        self._adjudication = adjudication_runner
        self._local_fix = local_fix_adapter
        self._context_resolver = context_resolver
        self._reader = InputArtifactReader(store.root)

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority: ClaimAuthorityGuard | None = None,
        claim: EffectClaim | None = None,
    ) -> LocalResult:
        del authority, claim
        try:
            if isinstance(effect, GeneratePublicationTextEffect):
                return self._generate_publication(effect, token, now=now)
            if isinstance(effect, AdjudicateThreadsEffect):
                return self._adjudicate(effect, token, now=now)
            if isinstance(effect, RunLocalFixEffect):
                return self._run_local_fix(effect, token, now=now)
        except CodexLocalRunnerError as exc:
            return self._retry_or_block(effect, token, now=now, detail=str(exc), retryable=True)
        except (LocalFixAdapterError, ProtectedResultStoreError, InputArtifactError) as exc:
            return self._retry_or_block(effect, token, now=now, detail=str(exc), retryable=False)
        except Exception:  # noqa: BLE001
            return self._retry_or_block(
                effect,
                token,
                now=now,
                detail="local effect failed",
                retryable=False,
            )
        return EffectBlocked(
            occurred_at=now,
            token=token,
            reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            safe_action=SafeAction(
                kind=SafeActionKind.INSPECT_ARTIFACTS,
                condition="unsupported local effect kind",
            ),
            safe_summary=f"unsupported local effect kind {effect.kind}",
        )

    def _generate_publication(
        self,
        effect: GeneratePublicationTextEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> LocalResult:
        cached = self._cached_publication(effect)
        if cached is not None:
            return EffectSucceeded(occurred_at=now, token=token, outcome=cached)

        context, _ref = self._context_resolver.resolve(effect.run_id)
        generated = self._publication.generate(
            run_id=effect.run_id,
            session_id=context.codex.session_id,
            repo_root=context.repository_root,
            execution_context=context,
            evidence_ref=effect.evidence_ref,
            patch_ref=effect.patch_ref,
            effect=effect,
        )
        self._store.persist_publication_generation(run_id=effect.run_id, artifact=generated)
        pub_ref, commit_ref = self._store.persist_publication_text_and_commit_message(
            run_id=effect.run_id,
            title=generated.title,
            body=generated.body,
            subject=generated.commit_subject,
            commit_body=generated.commit_body,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            evidence_ref_sha256=effect.evidence_ref.sha256,
            patch_ref_sha256=effect.patch_ref.sha256,
        )
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=PublicationTextPreparedOutcome(
                publication_text_ref=pub_ref,
                commit_message_ref=commit_ref,
            ),
        )

    def _adjudicate(
        self,
        effect: AdjudicateThreadsEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> LocalResult:
        cached = self._cached_adjudication(effect, now=now, token=token)
        if cached is not None:
            return cached

        context = self._store.read_execution_context(
            run_id=effect.run_id, ref=effect.execution_context_ref
        )
        try:
            observation = ReviewArtifactStore(self._store.root).read_and_verify(
                run_id=effect.run_id,
                ref=effect.snapshot_ref,
            )
        except ArtifactStoreError as exc:
            raise ProtectedResultStoreError(str(exc)) from exc
        if observation.cycle_number != effect.cycle_number:
            raise ProtectedResultStoreError("observation cycle does not match effect")
        if observation.head_sha.lower() != effect.bound_head_sha.lower():
            raise ProtectedResultStoreError("observation head SHA does not match effect")
        try:
            # Deliver exact on-disk hash-verified bytes (not a re-serialized copy).
            snapshot_bytes = self._reader.read_patch_bytes(
                run_id=effect.run_id,
                ref=effect.snapshot_ref,
                max_bytes=MAX_OBSERVATION_ARTIFACT_BYTES,
            )
        except InputArtifactError as exc:
            raise ProtectedResultStoreError(str(exc)) from exc
        trigger_marker = observation.trigger_marker
        result = self._adjudication.adjudicate(
            run_id=effect.run_id,
            session_id=context.codex.session_id,
            repo_root=context.repository_root,
            execution_context=context,
            effect=effect,
            snapshot_artifact_bytes_or_path=snapshot_bytes,
            frozen_thread_ids=effect.frozen_thread_ids,
        )
        result_ref = self._store.persist_external_adjudication(
            run_id=effect.run_id, artifact=result
        )
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=self._adjudication_outcome(
                effect=effect,
                result=result,
                result_ref=result_ref,
                trigger_marker=trigger_marker,
            ),
        )

    def _run_local_fix(
        self,
        effect: RunLocalFixEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> LocalResult:
        cached = self._cached_local_fix(effect, now=now, token=token)
        if cached is not None:
            return cached

        context = self._store.read_execution_context(
            run_id=effect.run_id, ref=effect.execution_context_ref
        )
        fix_prompt_text = self._reader.read_reply_text(
            run_id=effect.run_id, ref=effect.fix_prompt_ref
        )
        plan_bytes = self._store.read_source_plan_bytes(
            run_id=effect.run_id,
            expected_sha256=context.plan_prompt.plan_sha256,
        )
        prompt_bytes = self._store.read_source_prompt_bytes(
            run_id=effect.run_id,
            expected_sha256=context.plan_prompt.prompt_sha256,
        )
        outcome = self._local_fix.execute(
            run_id=effect.run_id,
            effect=effect,
            execution_context=context,
            fix_prompt_bytes=fix_prompt_text.encode("utf-8"),
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
        )
        assert isinstance(outcome, LocalFixFinishedOutcome)
        return EffectSucceeded(occurred_at=now, token=token, outcome=outcome)

    def _cached_publication(
        self, effect: GeneratePublicationTextEffect
    ) -> PublicationTextPreparedOutcome | None:
        cached = self._store.read_cached_publication_generation(effect)
        if cached is None:
            return None
        pub_ref, commit_ref = self._store.persist_publication_text_and_commit_message(
            run_id=effect.run_id,
            title=cached.title,
            body=cached.body,
            subject=cached.commit_subject,
            commit_body=cached.commit_body,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            evidence_ref_sha256=effect.evidence_ref.sha256,
            patch_ref_sha256=effect.patch_ref.sha256,
        )
        try:
            self._store.verify_publication_readable(
                run_id=effect.run_id,
                publication_ref=pub_ref,
                commit_ref=commit_ref,
            )
        except (ProtectedResultStoreError, InputArtifactError):
            return None
        return PublicationTextPreparedOutcome(
            publication_text_ref=pub_ref,
            commit_message_ref=commit_ref,
        )

    def _cached_adjudication(
        self,
        effect: AdjudicateThreadsEffect,
        *,
        now: datetime,
        token: EffectCompletionToken,
    ) -> EffectSucceeded | None:
        cached = self._store.read_cached_external_adjudication(effect)
        if cached is None:
            return None
        result_ref = self._store.resolve_cached_external_adjudication_ref(effect)
        if result_ref is None:
            return None
        try:
            observation = ReviewArtifactStore(self._store.root).read_and_verify(
                run_id=effect.run_id,
                ref=effect.snapshot_ref,
            )
            trigger_marker = observation.trigger_marker
        except ArtifactStoreError:
            trigger_marker = _trigger_marker_from_snapshot(
                self._reader.read_patch_bytes(
                    run_id=effect.run_id,
                    ref=effect.snapshot_ref,
                    max_bytes=MAX_OBSERVATION_ARTIFACT_BYTES,
                )
            )
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=self._adjudication_outcome(
                effect=effect,
                result=cached,
                result_ref=result_ref,
                trigger_marker=trigger_marker,
            ),
        )

    def _cached_local_fix(
        self,
        effect: RunLocalFixEffect,
        *,
        now: datetime,
        token: EffectCompletionToken,
    ) -> EffectSucceeded | None:
        cached = self._store.read_cached_local_fix_result(effect)
        if cached is None:
            return None
        accepted = cached.outcome in {
            LocalFixOutcomeKind.ACCEPTED,
            LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
        }
        patch_ref = None
        if accepted:
            if cached.accepted_patch_sha256 is None or cached.new_head_sha is None:
                return None
            patch_ref = ArtifactRef(
                relative_path=f"local/patches/{cached.accepted_patch_sha256}.patch",
                sha256=cached.accepted_patch_sha256,
            )
            try:
                self._reader.read_patch_bytes(run_id=effect.run_id, ref=patch_ref)
            except InputArtifactError:
                return None
        result_ref = self._store.resolve_cached_local_fix_result_ref(effect)
        if result_ref is None:
            return None
        if accepted:
            assert patch_ref is not None
            outcome = LocalFixFinishedOutcome(
                outcome=cached.outcome,
                accepted_patch_ref=patch_ref,
                new_head_sha=cached.new_head_sha,
                result_ref=result_ref,
            )
        elif cached.outcome is LocalFixOutcomeKind.ABORTED:
            outcome = LocalFixFinishedOutcome(
                outcome=cached.outcome,
                result_ref=result_ref,
            )
        else:
            pause_reason, safe_action = _pause_fields_from_outcome(cached.outcome)
            outcome = LocalFixFinishedOutcome(
                outcome=cached.outcome,
                result_ref=result_ref,
                pause_reason=pause_reason,
                safe_action=safe_action,
            )
        return EffectSucceeded(occurred_at=now, token=token, outcome=outcome)

    def _adjudication_outcome(
        self,
        *,
        effect: AdjudicateThreadsEffect,
        result: ExternalAdjudicationResultArtifact,
        result_ref: ArtifactRef,
        trigger_marker: str,
    ) -> AdjudicationRecordedOutcome:
        decisions: list[ThreadDecisionRecord] = []
        fix_prompt_ref: ArtifactRef | None = None
        if result.fix_prompt_text:
            fix_prompt_ref = self._store.persist_fix_prompt(
                run_id=effect.run_id, text=result.fix_prompt_text
            )
        for item in result.decisions:
            reply_ref = None
            if item.reply_body is not None:
                hint = hashlib.sha256(item.thread_id.encode()).hexdigest()[:16]
                reply_ref = self._store.persist_reply_text(
                    run_id=effect.run_id,
                    relative_hint=hint,
                    text=item.reply_body,
                )
            decisions.append(
                ThreadDecisionRecord(
                    thread_id=item.thread_id,
                    decision=item.decision,
                    safe_summary=operational_adjudication_summary(item.decision),
                    reply_ref=reply_ref,
                )
            )
        evidence = AdjudicationEvidence(
            frozen=FrozenThreadSet(
                thread_ids=effect.frozen_thread_ids,
                snapshot_ref=effect.snapshot_ref,
                head_sha=effect.bound_head_sha,
                cycle_number=effect.cycle_number,
                trigger_marker=trigger_marker,
            ),
            decisions=tuple(decisions),
            result_ref=result_ref,
            fix_prompt_ref=fix_prompt_ref,
        )
        return AdjudicationRecordedOutcome(evidence=evidence)

    def _retry_or_block(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        detail: str,
        retryable: bool,
    ) -> LocalResult:
        safe = (detail or "local effect failure")[:200]
        if retryable and effect.attempt < effect.max_attempts:
            return EffectRetryableFailure(
                occurred_at=now,
                token=token,
                error=ErrorSummary(
                    kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                    safe_summary=safe,
                ),
                failed_attempt=effect.attempt,
                next_attempt_at=now + timedelta(seconds=2**effect.attempt),
            )
        return EffectBlocked(
            occurred_at=now,
            token=token,
            reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            safe_action=SafeAction(
                kind=SafeActionKind.INSPECT_ARTIFACTS,
                condition="inspect local effect failure",
            ),
            safe_summary=safe,
        )


def operational_adjudication_summary(decision: AdjudicationDecisionKind) -> str:
    """Fixed operational summary for events/SQLite — never free-form model text."""

    mapping = {
        AdjudicationDecisionKind.ACTIONABLE: "thread_actionable",
        AdjudicationDecisionKind.NOT_APPLICABLE: "thread_not_applicable",
        AdjudicationDecisionKind.UNCERTAIN: "thread_uncertain",
    }
    return mapping.get(decision, f"thread_decision_{decision.value}")


def _trigger_marker_from_snapshot(snapshot_bytes: bytes) -> str:
    try:
        payload = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedResultStoreError("snapshot artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProtectedResultStoreError("snapshot artifact must be an object")
    marker = payload.get("trigger_marker")
    if not isinstance(marker, str) or not marker.strip():
        raise ProtectedResultStoreError("snapshot missing trigger_marker")
    return marker.strip()


def _pause_fields_from_outcome(
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


class StoreBackedContextResolver:
    """Resolve execution context from a known ArtifactRef supplier."""

    def __init__(
        self,
        store: ProtectedResultStore,
        *,
        ref_for_run: dict[str, ArtifactRef] | None = None,
        ref_lookup: RefLookup | None = None,
    ) -> None:
        self._store = store
        self._refs = ref_for_run or {}
        self._lookup = ref_lookup

    def resolve(self, run_id: str) -> tuple[ExecutionContextArtifact, ArtifactRef]:
        ref = self._refs.get(run_id)
        if ref is None and self._lookup is not None:
            ref = self._lookup(run_id)
        if ref is None:
            raise ProtectedResultStoreError("execution context ref not found for run")
        return self._store.read_execution_context(run_id=run_id, ref=ref), ref


class RefLookup(Protocol):
    def __call__(self, run_id: str) -> ArtifactRef: ...


__all__ = [
    "LocalEffectExecutor",
    "LocalResult",
    "RunExecutionContextResolver",
    "StoreBackedContextResolver",
    "operational_adjudication_summary",
]
