"""Preparation services for PR review v2 create/prepare (no external effects)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol

from ai_dev_loop.pr_review_v2.application.contracts import Clock, IdFactory, PrReviewEngineError
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    ControlError,
    ControlErrorKind,
    OriginKind,
    PrepareCreateResult,
    PreparedOwnershipKeys,
    SafeNextAction,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import ExecutionContextArtifact
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_TEXT_BYTES,
    AdoptedExistingPrPreimageArtifact,
    reject_prohibited_controls,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ExistingPrOrigin,
    PullRequestBinding,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.state import PreparedState
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    ProtectedResultStore,
    ProtectedResultStoreError,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory, SystemClock


class SourceRunSnapshot:
    """Caller-supplied read-only snapshot of a completed A/B source run."""

    __slots__ = (
        "source_run_id",
        "repository",
        "head_branch",
        "base_branch",
        "expected_head_sha",
        "accepted_patch_bytes",
        "plan_bytes",
        "prompt_bytes",
        "execution_context",
    )

    def __init__(
        self,
        *,
        source_run_id: str,
        repository: str,
        head_branch: str,
        base_branch: str,
        expected_head_sha: str,
        accepted_patch_bytes: bytes,
        plan_bytes: bytes,
        prompt_bytes: bytes,
        execution_context: ExecutionContextArtifact,
    ) -> None:
        self.source_run_id = source_run_id
        self.repository = repository
        self.head_branch = head_branch
        self.base_branch = base_branch
        self.expected_head_sha = expected_head_sha
        self.accepted_patch_bytes = accepted_patch_bytes
        self.plan_bytes = plan_bytes
        self.prompt_bytes = prompt_bytes
        self.execution_context = execution_context


class ExistingPrSnapshot:
    """Caller-supplied read-only identity for an already-open PR."""

    __slots__ = (
        "binding",
        "execution_context",
        "accepted_patch_bytes",
        "plan_bytes",
        "prompt_bytes",
        "title",
        "body",
    )

    def __init__(
        self,
        *,
        binding: PullRequestBinding,
        execution_context: ExecutionContextArtifact,
        plan_bytes: bytes,
        prompt_bytes: bytes,
        title: str,
        body: str = "",
        accepted_patch_bytes: bytes | None = None,
    ) -> None:
        self.binding = binding
        self.execution_context = execution_context
        self.plan_bytes = plan_bytes
        self.prompt_bytes = prompt_bytes
        self.title = title
        self.body = body
        self.accepted_patch_bytes = accepted_patch_bytes


class SourceRunReader(Protocol):
    def load(self, source_run_id: str) -> SourceRunSnapshot: ...


class ExistingPrReader(Protocol):
    def discover(self, *, owner_repo: str, pr_number: int) -> ExistingPrSnapshot: ...


def deterministic_run_id(*, kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}:{identity}".encode()).hexdigest()[:32]
    return f"prv2-{digest}"


class PreparationService:
    """Read-only preparation ending in durable PreparedState (never starts workers)."""

    def __init__(
        self,
        engine: PrReviewEngine,
        artifact_store: ProtectedResultStore,
        *,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._engine = engine
        self._store = artifact_store
        self._clock = clock or SystemClock()
        self._ids = ids or SequenceIdFactory(prefix="prep")

    def create_from_source(self, snapshot: SourceRunSnapshot) -> PrepareCreateResult:
        ctx = snapshot.execution_context
        if ctx.run_binding.prepared_from is not OriginKind.SOURCE_RUN:
            raise ControlError(
                ControlErrorKind.VALIDATION,
                "execution context prepared_from must be source_run",
            )
        if ctx.run_binding.source_run_id != snapshot.source_run_id:
            raise ControlError(
                ControlErrorKind.VALIDATION,
                "execution context source_run_id mismatch",
            )
        run_id = deterministic_run_id(
            kind="source_run",
            identity=(
                f"{snapshot.source_run_id}|{snapshot.repository}|"
                f"{snapshot.head_branch}|{snapshot.expected_head_sha}|"
                f"{ctx.codex.session_id}|{ctx.plan_prompt.prompt_sha256}"
            ),
        )
        try:
            patch_ref = self._store.persist_patch_bytes(
                run_id=run_id, data=snapshot.accepted_patch_bytes
            )
            plan_ref = self._store.persist_source_plan_bytes(
                run_id=run_id, data=snapshot.plan_bytes
            )
            prompt_ref = self._store.persist_source_prompt_bytes(
                run_id=run_id, data=snapshot.prompt_bytes
            )
            if plan_ref.sha256 != ctx.plan_prompt.plan_sha256:
                raise ProtectedResultStoreError("source plan hash mismatch")
            if prompt_ref.sha256 != ctx.plan_prompt.prompt_sha256:
                raise ProtectedResultStoreError("source prompt hash mismatch")
            context_ref = self._store.persist_execution_context(run_id=run_id, artifact=ctx)
        except ProtectedResultStoreError as exc:
            raise ControlError(ControlErrorKind.INTERNAL, str(exc)) from exc

        origin = SourceRunOrigin(
            source_run_id=snapshot.source_run_id,
            repository=RepositoryIdentity(name_with_owner=snapshot.repository),
            head_branch=snapshot.head_branch,
            base_branch=snapshot.base_branch,
            expected_head_sha=snapshot.expected_head_sha,
            accepted_patch=patch_ref,
            execution_context_ref=context_ref,
        )
        return self._commit_prepared(
            run_id=run_id,
            origin=origin,
            origin_kind=OriginKind.SOURCE_RUN,
            ownership=PreparedOwnershipKeys(
                source_run_id=snapshot.source_run_id,
                repository=snapshot.repository,
                pr_number=None,
                head_branch=snapshot.head_branch,
            ),
            limits=WorkflowLimits(
                max_external_cycles=ctx.pr_review_v2.max_external_cycles,
                max_local_iterations=ctx.workflow.max_local_iterations,
            ),
            context_sha=context_ref.sha256,
            repository=snapshot.repository,
            pr_number=None,
            now=self._clock.now(),
        )

    def prepare_existing_pr(self, snapshot: ExistingPrSnapshot) -> PrepareCreateResult:
        ctx = snapshot.execution_context
        binding = snapshot.binding
        if ctx.run_binding.prepared_from is not OriginKind.EXISTING_PR:
            raise ControlError(
                ControlErrorKind.VALIDATION,
                "execution context prepared_from must be existing_pr",
            )
        try:
            preimage = _adopted_preimage_from_snapshot(snapshot)
        except ValueError as exc:
            raise ControlError(
                ControlErrorKind.VALIDATION, "existing PR preimage is invalid"
            ) from exc
        run_id = deterministic_run_id(
            kind="existing_pr",
            identity=(
                f"{binding.repository.name_with_owner}|{binding.pr_number}|"
                f"{binding.head_sha}|{ctx.codex.session_id}|{ctx.plan_prompt.prompt_sha256}"
            ),
        )
        try:
            plan_ref = self._store.persist_source_plan_bytes(
                run_id=run_id, data=snapshot.plan_bytes
            )
            prompt_ref = self._store.persist_source_prompt_bytes(
                run_id=run_id, data=snapshot.prompt_bytes
            )
            if plan_ref.sha256 != ctx.plan_prompt.plan_sha256:
                raise ProtectedResultStoreError("source plan hash mismatch")
            if prompt_ref.sha256 != ctx.plan_prompt.prompt_sha256:
                raise ProtectedResultStoreError("source prompt hash mismatch")
            context_ref = self._store.persist_execution_context(run_id=run_id, artifact=ctx)
            preimage_ref = self._store.persist_adopted_existing_pr_preimage(
                run_id=run_id, artifact=preimage
            )
            if snapshot.accepted_patch_bytes is not None:
                self._store.persist_patch_bytes(run_id=run_id, data=snapshot.accepted_patch_bytes)
        except ProtectedResultStoreError as exc:
            raise ControlError(ControlErrorKind.INTERNAL, str(exc)) from exc

        origin = ExistingPrOrigin(
            binding=binding,
            execution_context_ref=context_ref,
            adopted_preimage_ref=preimage_ref,
        )
        return self._commit_prepared(
            run_id=run_id,
            origin=origin,
            origin_kind=OriginKind.EXISTING_PR,
            ownership=PreparedOwnershipKeys(
                source_run_id=None,
                repository=binding.repository.name_with_owner,
                pr_number=binding.pr_number,
                head_branch=binding.head_branch,
            ),
            limits=WorkflowLimits(
                max_external_cycles=ctx.pr_review_v2.max_external_cycles,
                max_local_iterations=ctx.workflow.max_local_iterations,
            ),
            context_sha=context_ref.sha256,
            repository=binding.repository.name_with_owner,
            pr_number=binding.pr_number,
            now=self._clock.now(),
        )

    def _commit_prepared(
        self,
        *,
        run_id: str,
        origin: SourceRunOrigin | ExistingPrOrigin,
        origin_kind: OriginKind,
        ownership: PreparedOwnershipKeys,
        limits: WorkflowLimits,
        context_sha: str,
        repository: str,
        pr_number: int | None,
        now: datetime,
    ) -> PrepareCreateResult:
        state = PreparedState(
            run_id=run_id,
            origin=origin,
            limits=limits,
            entered_at=now,
        )
        try:
            status, reused = self._engine.create_or_reuse_prepared_run(
                run_id=run_id,
                state=state,
                ownership=ownership,
            )
        except PrReviewEngineError as exc:
            kind = ControlErrorKind.CONFLICT
            if exc.kind.value == "validation":
                kind = ControlErrorKind.VALIDATION
            raise ControlError(kind, exc.safe_message) from exc
        return PrepareCreateResult(
            run_id=status.run_id,
            origin_kind=origin_kind,
            reused=reused,
            repository=repository,
            pr_number=pr_number,
            cycle_number=status.cycle_number,
            next_action=SafeNextAction.START,
            execution_context_sha256=context_sha,
        )


def _adopted_preimage_from_snapshot(
    snapshot: ExistingPrSnapshot,
) -> AdoptedExistingPrPreimageArtifact:
    binding = snapshot.binding
    title = reject_prohibited_controls(snapshot.title, field_name="adopted existing PR preimage")
    body = reject_prohibited_controls(snapshot.body, field_name="adopted existing PR preimage")
    if not title.strip():
        raise ValueError("adopted existing PR title must be non-empty")
    if len(title.encode("utf-8")) > DEFAULT_MAX_TEXT_BYTES:
        raise ValueError("adopted existing PR title exceeds maximum size")
    if len(body.encode("utf-8")) > DEFAULT_MAX_TEXT_BYTES:
        raise ValueError("adopted existing PR body exceeds maximum size")
    return AdoptedExistingPrPreimageArtifact(
        repository=binding.repository.name_with_owner,
        pr_number=binding.pr_number,
        head_branch=binding.head_branch,
        base_branch=binding.base_branch,
        head_sha=binding.head_sha,
        title=title.strip(),
        body=body,
    )


__all__ = [
    "ExistingPrReader",
    "ExistingPrSnapshot",
    "PreparationService",
    "SourceRunReader",
    "SourceRunSnapshot",
    "deterministic_run_id",
]
