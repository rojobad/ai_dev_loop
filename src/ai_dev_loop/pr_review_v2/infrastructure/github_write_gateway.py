"""GitHub mutation gateway: PR create/update, trigger, reply, text, resolve.

Every mutation searches for the exact opaque content-bound (or, for the trigger,
already-opaque) marker before writing: exactly one existing match is idempotent
success, zero permits a single write, and duplicates block. The worker-supplied
authority guard is called immediately before each mutating API call. Reconciliation
reads classify APPLIED / PROVEN_NOT_APPLIED / UNRESOLVED per the Phase 16.6 proof
table. No generic GraphQL/REST surface is exposed and no SQLite is touched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from ai_dev_loop.errors import LockError
from ai_dev_loop.locking import FileLock, LockMetadata
from ai_dev_loop.paths import repository_lock_path
from ai_dev_loop.pr_review_v2.application.github_read import (
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    AuthorityLostError,
    ContentBoundMarker,
    GitHubWritePolicy,
    ReconciliationProof,
    TriggerEvidenceArtifact,
    WriteGatewaySuccess,
    WriteProofKind,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    html_comment_marker,
    opaque_repository_lock_id,
    opaque_run_lock_id,
    parse_single_owned_preimage,
    repository_lock_contention_transient,
    sha256_hex,
    unmarked_body_hash,
    validate_branch_name,
    verify_owned_preimage_content,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import (
    proven_not_applied_proof,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    PullRequestBinding,
    ReconciliationStrategyKind,
    TransientErrorKind,
    TriggerEvidence,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    CreateOrUpdatePrEffect,
    PostThreadReplyEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    UpdatePrTextEffect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    ReviewTriggerConfirmedOutcome,
    ThreadReplyConfirmedOutcome,
    ThreadResolutionConfirmedOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadTransport
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import (
    WriteEvidenceStore,
    WriteEvidenceStoreError,
)

AuthorizeCallback = Callable[[], None]
_T = TypeVar("_T")


def _block(kind: GatewayBlockKind, detail: str) -> GhTransportError:
    return GhTransportError(block=block_for_kind(kind, detail=detail))


def _timeout_error() -> GhTransportError:
    return GhTransportError(
        transient=GatewayTransient(
            kind=GatewayTransientKind.TIMEOUT,
            safe_summary="GitHub write overall deadline exceeded",
            transient_kind=TransientErrorKind.TIMEOUT,
        )
    )


def _split_nwo(name_with_owner: str) -> tuple[str, str]:
    owner, name = name_with_owner.split("/", 1)
    return owner, name


def _as_dict(value: object, detail: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, detail)
    return value


def _as_list(value: object, detail: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, detail)


def _require_int(value: object, detail: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, detail)
    return value


def _require_str(value: object, detail: str) -> str:
    if not isinstance(value, str) or not value:
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, detail)
    return value


def _require_int_field(data: dict[str, Any], key: str, detail: str) -> int:
    if key not in data:
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, detail)
    return _require_int(data[key], detail)


def _safe_branch_or_block(value: str, detail: str) -> str:
    try:
        return validate_branch_name(value)
    except ValueError as exc:
        raise _block(GatewayBlockKind.HTTP_VALIDATION_REJECTION, detail) from exc


class GitHubWriteGateway:
    def __init__(
        self,
        *,
        policy: GitHubWritePolicy,
        write_transport: GhWriteTransport,
        read_transport: GitHubReadTransport,
        input_reader: InputArtifactReader,
        write_evidence: WriteEvidenceStore,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._writes = write_transport
        self._reads = read_transport
        self._reader = input_reader
        self._evidence = write_evidence
        self._monotonic = monotonic
        self._deadline: float | None = None

    def _repo_lock(self) -> FileLock:
        return FileLock(repository_lock_path(Path(self._policy.repository_cwd)))

    def _safe_lock_metadata(self, *, run_id: str, now: datetime) -> LockMetadata:
        return LockMetadata(
            pid=os.getpid(),
            run_id=opaque_run_lock_id(run_id),
            repository_path=opaque_repository_lock_id(self._policy.repository_cwd),
            started_at=now,
        )

    def _operation(self, *, run_id: str, now: datetime, callback: Callable[[], _T]) -> _T:
        lock = self._repo_lock()
        try:
            lock.acquire(self._safe_lock_metadata(run_id=run_id, now=now))
        except LockError as exc:
            raise GhTransportError(transient=repository_lock_contention_transient()) from exc
        deadline = self._monotonic() + self._policy.overall_timeout_seconds
        self._deadline = deadline
        try:
            return callback()
        finally:
            self._deadline = None
            lock.release()

    def _call_timeout(self, deadline: float | None = None) -> float:
        current = self._deadline if deadline is None else deadline
        if current is None:
            current = self._monotonic() + self._policy.overall_timeout_seconds
        remaining = current - self._monotonic()
        if remaining <= 0:
            raise _timeout_error()
        return min(self._policy.per_call_timeout_seconds, remaining)

    def _after_authorize(self, action: Callable[[], _T]) -> _T:
        """No transport or confirmation failure is retry-safe after authorization."""

        try:
            return action()
        except AmbiguousWriteError:
            raise
        except AuthorityLostError:
            raise
        except GhTransportError as exc:
            raise AmbiguousWriteError("GitHub write outcome uncertain after authorization") from exc
        except Exception as exc:
            # Malformed/partial envelopes and unexpected parser faults after dispatch
            # are write-uncertain; never ordinary retry.
            raise AmbiguousWriteError("GitHub write outcome uncertain after authorization") from exc

    # Public methods lock a repository-wide critical section so preflight, the
    # authority fence, one mutation, and confirmation observe one consistent view.
    def create_or_update_pr(
        self,
        effect: CreateOrUpdatePrEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        def _run() -> WriteGatewaySuccess:
            return self._create_or_update_pr(effect, run_id=run_id, now=now, authorize=authorize)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def reconcile_create_or_update_pr(
        self, effect: CreateOrUpdatePrEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        def _run() -> ReconciliationProof:
            return self._reconcile_create_or_update_pr(effect, run_id=run_id, now=now)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def update_pr_text(
        self,
        effect: UpdatePrTextEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        def _run() -> WriteGatewaySuccess:
            return self._update_pr_text(effect, run_id=run_id, now=now, authorize=authorize)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def reconcile_update_pr_text(
        self, effect: UpdatePrTextEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        def _run() -> ReconciliationProof:
            return self._reconcile_update_pr_text(effect, run_id=run_id, now=now)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def request_review(
        self,
        effect: RequestBotReviewEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        def _run() -> WriteGatewaySuccess:
            return self._request_review(effect, run_id=run_id, now=now, authorize=authorize)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def reconcile_request_review(
        self, effect: RequestBotReviewEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        def _run() -> ReconciliationProof:
            return self._reconcile_request_review(effect, run_id=run_id, now=now)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def post_thread_reply(
        self,
        effect: PostThreadReplyEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        def _run() -> WriteGatewaySuccess:
            return self._post_thread_reply(effect, run_id=run_id, now=now, authorize=authorize)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def reconcile_post_thread_reply(
        self, effect: PostThreadReplyEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        def _run() -> ReconciliationProof:
            return self._reconcile_post_thread_reply(effect, run_id=run_id, now=now)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def resolve_thread(
        self,
        effect: ResolveThreadEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        def _run() -> WriteGatewaySuccess:
            return self._resolve_thread(effect, run_id=run_id, now=now, authorize=authorize)

        return self._operation(run_id=run_id, now=now, callback=_run)

    def reconcile_resolve_thread(
        self, effect: ResolveThreadEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        def _run() -> ReconciliationProof:
            return self._reconcile_resolve_thread(effect, run_id=run_id, now=now)

        return self._operation(run_id=run_id, now=now, callback=_run)

    # -- create / update PR ---------------------------------------------

    def _create_or_update_pr(
        self,
        effect: CreateOrUpdatePrEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        del now
        head_branch = _safe_branch_or_block(effect.head_branch, "PR head_branch is unsafe")
        base_branch = _safe_branch_or_block(effect.base_branch, "PR base_branch is unsafe")
        owner, name = _split_nwo(effect.repository.name_with_owner)
        title, body, marker = self._pr_marker(
            run_id=run_id,
            ref=effect.publication_text_ref,
            operation="create_or_update_pr",
            idempotency_key=effect.idempotency_key,
        )
        body_with_marker = append_owned_marker(body, marker.marker_text)
        candidates = self._list_pr_candidates(
            owner=owner, name=name, head=head_branch, base=base_branch
        )
        if len(candidates) > 1:
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "multiple head/base PRs")
        if candidates:
            pr = candidates[0]
            self._validate_binding(
                PullRequestBinding(
                    repository=effect.repository,
                    pr_number=_require_int_field(pr, "number", "PR number malformed"),
                    head_branch=head_branch,
                    base_branch=base_branch,
                    head_sha=effect.bound_head_sha,
                )
            )
            existing = self._pr_marker_state(pr, marker, title)
            if existing == "applied":
                return WriteGatewaySuccess(outcome=self._pr_bound(effect, pr), already_applied=True)
            if not self._eligible_pr_preimage(pr):
                raise _block(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    "existing PR lacks an intact owned preimage for idempotent update",
                )
            pr_number = _require_int_field(pr, "number", "PR number malformed")
            authorize()

            def update() -> PrBoundOutcome:
                updated = self._writes.update_pull_request(
                    owner=owner,
                    name=name,
                    number=pr_number,
                    title=title,
                    body=body_with_marker,
                    timeout_seconds=self._call_timeout(),
                )
                confirmed = self._confirm_pr(updated.body_json, marker, title, effect)
                self._validate_binding(confirmed.binding)
                return confirmed

            confirmed = self._after_authorize(update)
            return WriteGatewaySuccess(outcome=confirmed, already_applied=False)

        self._require_head_branch_sha(
            owner=owner,
            name=name,
            branch=head_branch,
            expected_sha=effect.bound_head_sha,
        )
        authorize()

        def create() -> PrBoundOutcome:
            created = self._writes.create_pull_request(
                owner=owner,
                name=name,
                head=head_branch,
                base=base_branch,
                title=title,
                body=body_with_marker,
                timeout_seconds=self._call_timeout(),
            )
            confirmed = self._confirm_pr(created.body_json, marker, title, effect)
            self._validate_binding(confirmed.binding)
            return confirmed

        confirmed = self._after_authorize(create)
        return WriteGatewaySuccess(outcome=confirmed, already_applied=False)

    def _reconcile_create_or_update_pr(
        self, effect: CreateOrUpdatePrEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_PR_BY_HEAD_BASE
        try:
            head_branch = validate_branch_name(effect.head_branch)
            base_branch = validate_branch_name(effect.base_branch)
        except ValueError:
            return _unresolved(strategy, "PR head/base branch is unsafe")
        owner, name = _split_nwo(effect.repository.name_with_owner)
        title, _body, marker = self._pr_marker(
            run_id=run_id,
            ref=effect.publication_text_ref,
            operation="create_or_update_pr",
            idempotency_key=effect.idempotency_key,
        )
        try:
            self._require_head_branch_sha(
                owner=owner,
                name=name,
                branch=head_branch,
                expected_sha=effect.bound_head_sha,
            )
        except GhTransportError:
            return _unresolved(strategy, "head branch/SHA ownership incomplete")
        try:
            candidates = self._list_pr_candidates(
                owner=owner, name=name, head=head_branch, base=base_branch
            )
        except GhTransportError:
            return _unresolved(strategy, "PR candidate ownership/identity incomplete")
        if len(candidates) > 1:
            return _unresolved(strategy, "multiple head/base PRs")
        if not candidates:
            return proven_not_applied_proof(
                strategy=strategy,
                occurred_at=now,
                failed_attempt=effect.attempt,
                safe_summary="no PR exists for head/base",
            )
        pr = candidates[0]
        try:
            self._validate_binding(
                PullRequestBinding(
                    repository=effect.repository,
                    pr_number=_require_int_field(pr, "number", "PR number malformed"),
                    head_branch=head_branch,
                    base_branch=base_branch,
                    head_sha=effect.bound_head_sha,
                )
            )
        except GhTransportError:
            return _unresolved(strategy, "PR ownership/identity incomplete")
        return self._pr_proof(strategy, pr, marker, title, effect, now)

    # -- update PR text -------------------------------------------------

    def _update_pr_text(
        self,
        effect: UpdatePrTextEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        del now
        owner, name = _split_nwo(effect.binding.repository.name_with_owner)
        number = effect.binding.pr_number
        title, body, marker = self._pr_marker(
            run_id=run_id,
            ref=effect.publication_text_ref,
            operation="update_pr_text",
            idempotency_key=effect.idempotency_key,
        )
        body_with_marker = append_owned_marker(body, marker.marker_text)
        current = self._fetch_pr(owner=owner, name=name, number=number)
        self._validate_binding(effect.binding)
        state = self._pr_marker_state(current, marker, title)
        if state == "applied":
            return WriteGatewaySuccess(
                outcome=PrTextUpdatedOutcome(
                    binding=effect.binding, publication_text_ref=effect.publication_text_ref
                ),
                already_applied=True,
            )
        if state == "ambiguous" or not self._eligible_pr_preimage(current):
            raise _block(
                GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                "bound PR lacks an intact owned preimage for text update",
            )
        authorize()

        def update_text() -> None:
            updated = self._writes.update_pr_text(
                owner=owner,
                name=name,
                number=number,
                title=title,
                body=body_with_marker,
                timeout_seconds=self._call_timeout(),
            )
            pr = _as_dict(updated.body_json, "pr text update response malformed")
            if self._pr_marker_state(pr, marker, title) != "applied":
                raise AmbiguousWriteError("pr text update could not be confirmed")
            self._validate_binding(effect.binding)

        self._after_authorize(update_text)
        return WriteGatewaySuccess(
            outcome=PrTextUpdatedOutcome(
                binding=effect.binding, publication_text_ref=effect.publication_text_ref
            ),
            already_applied=False,
        )

    def _reconcile_update_pr_text(
        self, effect: UpdatePrTextEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_PR_TEXT
        owner, name = _split_nwo(effect.binding.repository.name_with_owner)
        try:
            self._validate_binding(effect.binding)
        except GhTransportError:
            return _unresolved(strategy, "PR ownership/identity incomplete")
        title, _body, marker = self._pr_marker(
            run_id=run_id,
            ref=effect.publication_text_ref,
            operation="update_pr_text",
            idempotency_key=effect.idempotency_key,
        )
        try:
            current = self._fetch_pr(owner=owner, name=name, number=effect.binding.pr_number)
        except GhTransportError:
            return _unresolved(strategy, "PR ownership/identity incomplete")
        state = self._pr_marker_state(current, marker, title)
        if state == "applied":
            return ReconciliationProof(
                proof=WriteProofKind.APPLIED,
                strategy=strategy,
                confirmed_outcome=PrTextUpdatedOutcome(
                    binding=effect.binding, publication_text_ref=effect.publication_text_ref
                ),
                safe_summary="pr text carries the exact owned marker and content",
            )
        if state != "ambiguous" and self._eligible_pr_preimage(current):
            return proven_not_applied_proof(
                strategy=strategy,
                occurred_at=now,
                failed_attempt=effect.attempt,
                safe_summary="bound PR has an owned preimage without the intended content",
            )
        return _unresolved(strategy, "pr text marker/content absent or ambiguous")

    # -- request review trigger -----------------------------------------

    def _request_review(
        self,
        effect: RequestBotReviewEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        owner, name = _split_nwo(effect.binding.repository.name_with_owner)
        self._validate_binding(effect.binding)
        marker_comment = html_comment_marker(effect.marker)
        needle = marker_comment
        existing = self._find_marked_issue_comments(
            owner=owner, name=name, number=effect.binding.pr_number, needle=needle
        )
        if len(existing) > 1:
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "duplicate trigger comments")
        if len(existing) == 1:
            comment = existing[0]
            return WriteGatewaySuccess(
                outcome=self._trigger_outcome(effect, comment, run_id=run_id, now=now),
                already_applied=True,
            )
        authorize()
        body = f"{marker_comment}\n{self._policy.review_command_body}\n"

        def trigger() -> ReviewTriggerConfirmedOutcome:
            created = self._writes.create_issue_comment(
                owner=owner,
                name=name,
                number=effect.binding.pr_number,
                body=body,
                timeout_seconds=self._call_timeout(),
            )
            comment = _as_dict(created.body_json, "issue comment response malformed")
            if not str(comment.get("id") or comment.get("databaseId") or "") or needle not in str(
                comment.get("body") or ""
            ):
                raise AmbiguousWriteError("trigger comment could not be confirmed")
            self._validate_binding(effect.binding)
            return self._trigger_outcome(effect, comment, run_id=run_id, now=now)

        outcome = self._after_authorize(trigger)
        return WriteGatewaySuccess(outcome=outcome, already_applied=False)

    def _reconcile_request_review(
        self, effect: RequestBotReviewEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_REVIEW_MARKER
        owner, name = _split_nwo(effect.binding.repository.name_with_owner)
        try:
            self._validate_binding(effect.binding)
        except GhTransportError:
            return _unresolved(strategy, "PR ownership/identity incomplete")
        needle = html_comment_marker(effect.marker)
        existing = self._find_marked_issue_comments(
            owner=owner, name=name, number=effect.binding.pr_number, needle=needle
        )
        if len(existing) > 1:
            return _unresolved(strategy, "duplicate trigger comments")
        if not existing:
            return proven_not_applied_proof(
                strategy=strategy,
                occurred_at=now,
                failed_attempt=effect.attempt,
                safe_summary="trigger comment absent",
            )
        outcome = self._trigger_outcome(effect, existing[0], run_id=run_id, now=now)
        return ReconciliationProof(
            proof=WriteProofKind.APPLIED,
            strategy=strategy,
            confirmed_outcome=outcome,
            safe_summary="exact trigger marker present once",
        )

    # -- post thread reply ----------------------------------------------

    def _post_thread_reply(
        self,
        effect: PostThreadReplyEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        del now
        self._validate_thread_binding(str(effect.thread_id), effect.binding)
        reply_text, marker = self._reply_marker(
            run_id=run_id, ref=effect.reply_ref, idempotency_key=effect.idempotency_key
        )
        needle = html_comment_marker(marker.marker_text)
        existing = self._find_marked_thread_comments(str(effect.thread_id), needle)
        if len(existing) > 1:
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "duplicate thread replies")
        if len(existing) == 1:
            body = existing[0]
            try:
                if unmarked_body_hash(body, marker.marker_text) != marker.content_sha256:
                    raise _block(
                        GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                        "existing thread reply marker content mismatch",
                    )
            except ValueError as exc:
                raise _block(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    "existing thread reply marker is ambiguous",
                ) from exc
            return WriteGatewaySuccess(
                outcome=ThreadReplyConfirmedOutcome(
                    thread_id=effect.thread_id, reply_ref=effect.reply_ref
                ),
                already_applied=True,
            )
        if self._thread_resolved(str(effect.thread_id)):
            raise _block(
                GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                "thread already resolved without a matching owned reply",
            )
        authorize()
        reply_with_marker = append_owned_marker(reply_text, marker.marker_text)

        def reply() -> None:
            created = self._writes.add_review_thread_reply(
                thread_id=str(effect.thread_id),
                body=reply_with_marker,
                timeout_seconds=self._call_timeout(),
            )
            response_body = self._reply_body_from_response(created.body_json)
            if (
                not self._reply_id_from_response(created.body_json)
                or needle not in response_body
                or unmarked_body_hash(response_body, marker.marker_text) != marker.content_sha256
            ):
                raise AmbiguousWriteError("thread reply could not be confirmed")
            self._validate_thread_binding(str(effect.thread_id), effect.binding)

        self._after_authorize(reply)
        return WriteGatewaySuccess(
            outcome=ThreadReplyConfirmedOutcome(
                thread_id=effect.thread_id, reply_ref=effect.reply_ref
            ),
            already_applied=False,
        )

    def _reconcile_post_thread_reply(
        self, effect: PostThreadReplyEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_THREAD_REPLY
        try:
            self._validate_thread_binding(str(effect.thread_id), effect.binding)
        except GhTransportError:
            return _unresolved(strategy, "thread ownership/identity incomplete")
        _reply_text, marker = self._reply_marker(
            run_id=run_id, ref=effect.reply_ref, idempotency_key=effect.idempotency_key
        )
        needle = html_comment_marker(marker.marker_text)
        existing = self._find_marked_thread_comments(str(effect.thread_id), needle)
        if len(existing) > 1:
            return _unresolved(strategy, "duplicate thread replies")
        if not existing:
            if self._thread_resolved(str(effect.thread_id)):
                return _unresolved(strategy, "thread resolved without a matching owned reply")
            return proven_not_applied_proof(
                strategy=strategy,
                occurred_at=now,
                failed_attempt=effect.attempt,
                safe_summary="thread reply absent",
            )
        body = existing[0]
        try:
            if unmarked_body_hash(body, marker.marker_text) != marker.content_sha256:
                return _unresolved(strategy, "thread reply content drift")
        except ValueError:
            return _unresolved(strategy, "thread reply marker ambiguous")
        return ReconciliationProof(
            proof=WriteProofKind.APPLIED,
            strategy=strategy,
            confirmed_outcome=ThreadReplyConfirmedOutcome(
                thread_id=effect.thread_id, reply_ref=effect.reply_ref
            ),
            safe_summary="exact reply marker and content present",
        )

    # -- resolve thread -------------------------------------------------

    def _resolve_thread(
        self,
        effect: ResolveThreadEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        del run_id, now
        self._validate_thread_binding(str(effect.thread_id), effect.binding)
        if self._thread_resolved(str(effect.thread_id)):
            return WriteGatewaySuccess(
                outcome=ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id),
                already_applied=True,
            )
        authorize()

        def resolve() -> None:
            result = self._writes.resolve_review_thread(
                thread_id=str(effect.thread_id), timeout_seconds=self._call_timeout()
            )
            if not self._resolved_from_response(result.body_json, str(effect.thread_id)):
                raise AmbiguousWriteError("thread resolution could not be confirmed")
            self._validate_thread_binding(str(effect.thread_id), effect.binding)
            if not self._thread_resolved(str(effect.thread_id)):
                raise AmbiguousWriteError("thread resolution did not persist")

        self._after_authorize(resolve)
        return WriteGatewaySuccess(
            outcome=ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id),
            already_applied=False,
        )

    def _reconcile_resolve_thread(
        self, effect: ResolveThreadEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        del run_id
        strategy = ReconciliationStrategyKind.FIND_THREAD_RESOLVED
        try:
            self._validate_thread_binding(str(effect.thread_id), effect.binding)
        except GhTransportError:
            return _unresolved(strategy, "thread ownership/identity incomplete")
        resolved = self._thread_resolved(str(effect.thread_id))
        if resolved:
            return ReconciliationProof(
                proof=WriteProofKind.APPLIED,
                strategy=strategy,
                confirmed_outcome=ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id),
                safe_summary="thread is resolved",
            )
        return proven_not_applied_proof(
            strategy=strategy,
            occurred_at=now,
            failed_attempt=effect.attempt,
            safe_summary="thread is still unresolved",
        )

    # -- shared marker/artifact helpers ---------------------------------

    def _pr_marker(
        self, *, run_id: str, ref: Any, operation: str, idempotency_key: str
    ) -> tuple[str, str, ContentBoundMarker]:
        try:
            publication = self._reader.read_publication_text(
                run_id=run_id, ref=ref, max_bytes=self._policy.max_text_bytes
            )
        except InputArtifactError as exc:
            raise _block(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION, "publication artifact invalid"
            ) from exc
        marker = derive_content_bound_marker(
            operation=operation,
            target_kind="pr_body",
            idempotency_key=idempotency_key,
            canonical_content=canonicalize_publication_text(
                title=publication.title, body=publication.body
            ),
        )
        return publication.title, publication.body, marker

    def _reply_marker(
        self, *, run_id: str, ref: Any, idempotency_key: str
    ) -> tuple[str, ContentBoundMarker]:
        try:
            reply_text = self._reader.read_reply_text(
                run_id=run_id, ref=ref, max_bytes=self._policy.max_text_bytes
            )
        except InputArtifactError as exc:
            raise _block(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION, "reply artifact invalid"
            ) from exc
        marker = derive_content_bound_marker(
            operation="post_thread_reply",
            target_kind="thread_reply",
            idempotency_key=idempotency_key,
            canonical_content=reply_text,
        )
        return reply_text, marker

    def _trigger_outcome(
        self,
        effect: RequestBotReviewEffect,
        comment: dict[str, Any],
        *,
        run_id: str,
        now: datetime,
    ) -> ReviewTriggerConfirmedOutcome:
        comment_id = str(comment.get("id") or comment.get("databaseId") or "").strip()
        body = str(comment.get("body") or "")
        created_raw = comment.get("createdAt") or comment.get("created_at")
        if not comment_id or not created_raw:
            raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "trigger comment identity missing")
        from ai_dev_loop.pr_review_v2.domain.common import coerce_utc_instant

        try:
            created_at = coerce_utc_instant(str(created_raw))
        except (TypeError, ValueError) as exc:
            raise _block(
                GatewayBlockKind.MALFORMED_EVIDENCE, "trigger comment timestamp invalid"
            ) from exc
        artifact = TriggerEvidenceArtifact(
            marker=effect.marker,
            comment_id=comment_id,
            created_at=created_at,
            body_sha256=sha256_hex(body),
            head_sha=effect.binding.head_sha,
            pr_number=effect.binding.pr_number,
            repository=effect.binding.repository.name_with_owner,
        )
        try:
            comment_ref = self._evidence.persist_trigger_evidence(run_id=run_id, artifact=artifact)
        except WriteEvidenceStoreError as exc:
            raise _block(
                GatewayBlockKind.ARTIFACT_FAILURE, "trigger evidence persistence failed"
            ) from exc
        del now
        return ReviewTriggerConfirmedOutcome(
            evidence=TriggerEvidence(
                marker=effect.marker,
                comment_ref=comment_ref,
                head_sha=effect.binding.head_sha,
            )
        )

    # -- PR parsing / proofs --------------------------------------------

    def _validate_binding(self, binding: PullRequestBinding) -> None:
        _safe_branch_or_block(binding.head_branch, "binding head_branch is unsafe")
        _safe_branch_or_block(binding.base_branch, "binding base_branch is unsafe")
        owner, name = _split_nwo(binding.repository.name_with_owner)
        result = self._reads.fetch_pull_request_identity(
            owner=owner, name=name, number=binding.pr_number, timeout_seconds=self._call_timeout()
        )
        data = _as_dict(result.body_json, "PR identity response malformed")
        repository = _as_dict(_dig(data, ("data", "repository")), "repository identity malformed")
        pr = _as_dict(repository.get("pullRequest"), "pull request identity missing")
        pr_number = _require_int_field(pr, "number", "PR identity number malformed")
        head_ref = _require_str(pr.get("headRefName"), "PR identity headRefName malformed")
        base_ref = _require_str(pr.get("baseRefName"), "PR identity baseRefName malformed")
        head_oid = _require_str(pr.get("headRefOid"), "PR identity headRefOid malformed")
        state = _require_str(pr.get("state"), "PR identity state malformed")
        nwo = _require_str(repository.get("nameWithOwner"), "repository nameWithOwner malformed")
        if (
            nwo != binding.repository.name_with_owner
            or pr_number != binding.pr_number
            or state.upper() != "OPEN"
            or pr.get("isCrossRepository") is not False
            or head_ref != binding.head_branch
            or base_ref != binding.base_branch
            or head_oid.lower() != binding.head_sha
        ):
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "PR binding drift")

    def _validate_thread_binding(self, thread_id: str, binding: PullRequestBinding) -> None:
        if not thread_id:
            raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "thread id is empty")
        self._validate_binding(binding)
        result = self._writes.fetch_thread_resolved(
            thread_id=thread_id, timeout_seconds=self._call_timeout()
        )
        data = _as_dict(result.body_json, "thread identity response malformed")
        node = _as_dict(_dig(data, ("data", "node")), "thread identity missing")
        node_id = _require_str(node.get("id"), "thread identity id malformed")
        if node_id != thread_id:
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "thread identity drift")
        pull_request = node.get("pullRequest")
        if pull_request is None:
            raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "thread PR ownership is missing")
        pr_number = _require_int_field(
            _as_dict(pull_request, "thread PR malformed"),
            "number",
            "thread PR number malformed",
        )
        if pr_number != binding.pr_number:
            raise _block(GatewayBlockKind.CONTRADICTORY_EVIDENCE, "thread belongs to another PR")

    def _require_head_branch_sha(
        self, *, owner: str, name: str, branch: str, expected_sha: str
    ) -> None:
        """Prove the bound head branch tip equals ``expected_sha`` before create."""

        result = self._writes.fetch_branch_head_sha(
            owner=owner, name=name, branch=branch, timeout_seconds=self._call_timeout()
        )
        data = _as_dict(result.body_json, "branch ref response malformed")
        obj = _as_dict(data.get("object"), "branch ref object missing")
        sha = str(obj.get("sha") or "").lower()
        if sha != expected_sha.lower():
            raise _block(
                GatewayBlockKind.HEAD_DRIFT,
                "head branch tip does not match bound_head_sha",
            )

    def _list_pr_candidates(
        self, *, owner: str, name: str, head: str, base: str
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        expected_full = f"{owner}/{name}"
        for page in range(1, self._policy.max_pages + 1):
            result = self._writes.list_prs_by_head_base(
                owner=owner,
                name=name,
                head=head,
                base=base,
                page=page,
                per_page=100,
                timeout_seconds=self._call_timeout(),
            )
            items = _as_list(result.body_json, "pr list response malformed")
            if len(items) > 100:
                raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "PR page item limit exceeded")
            for item in items:
                pr = _as_dict(item, "pr list item malformed")
                head_obj = _as_dict(pr.get("head"), "pr head malformed")
                base_obj = _as_dict(pr.get("base"), "pr base malformed")
                head_ref = _require_str(head_obj.get("ref"), "pr head ref malformed")
                base_ref = _require_str(base_obj.get("ref"), "pr base ref malformed")
                if head_ref != head or base_ref != base:
                    continue
                _require_int_field(pr, "number", "pr list number malformed")
                repo_obj = head_obj.get("repo")
                if not isinstance(repo_obj, dict):
                    raise _block(
                        GatewayBlockKind.MALFORMED_EVIDENCE, "pr head repo ownership missing"
                    )
                full = _require_str(repo_obj.get("full_name"), "pr head repo full_name malformed")
                if full != expected_full:
                    raise _block(
                        GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                        "cross-repository or fork PR candidate",
                    )
                candidates.append(pr)
                if len(candidates) > self._policy.max_items:
                    raise _block(
                        GatewayBlockKind.MALFORMED_EVIDENCE, "PR candidates exceeded item limit"
                    )
            if len(items) < 100:
                return candidates
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "PR candidate pages exceeded")

    def _fetch_pr(self, *, owner: str, name: str, number: int) -> dict[str, Any]:
        result = self._writes.fetch_pr_text(
            owner=owner,
            name=name,
            number=number,
            timeout_seconds=self._call_timeout(),
        )
        return _as_dict(result.body_json, "pr text response malformed")

    def _pr_marker_state(self, pr: dict[str, Any], marker: ContentBoundMarker, title: str) -> str:
        """Return ``applied`` / ``absent`` / ``ambiguous`` for a PR candidate."""

        body = str(pr.get("body") or "")
        needle = html_comment_marker(marker.marker_text)
        count = body.count(needle)
        if count == 0:
            return "absent"
        if count > 1:
            return "ambiguous"
        if str(pr.get("title") or "") != title:
            return "ambiguous"
        if not verify_owned_preimage_content(body=body, title=title, evidence=marker):
            return "ambiguous"
        return "applied"

    def _eligible_pr_preimage(self, pr: dict[str, Any]) -> bool:
        """Only an intact v2 PR-body preimage authorizes overwrite/proven-absence."""

        body = str(pr.get("body") or "")
        title = str(pr.get("title") or "")
        try:
            evidence = parse_single_owned_preimage(body)
        except ValueError:
            return False
        return (
            evidence is not None
            and evidence.target_kind == "pr_body"
            and evidence.operation in {"create_or_update_pr", "update_pr_text"}
            and verify_owned_preimage_content(body=body, title=title, evidence=evidence)
        )

    def _pr_bound(self, effect: CreateOrUpdatePrEffect, pr: dict[str, Any]) -> PrBoundOutcome:
        number = _require_int_field(pr, "number", "PR number malformed")
        return PrBoundOutcome(
            binding=PullRequestBinding(
                repository=effect.repository,
                pr_number=number,
                head_branch=effect.head_branch,
                base_branch=effect.base_branch,
                head_sha=effect.bound_head_sha,
            )
        )

    def _confirm_pr(
        self,
        body_json: object,
        marker: ContentBoundMarker,
        title: str,
        effect: CreateOrUpdatePrEffect,
    ) -> PrBoundOutcome:
        pr = _as_dict(body_json, "pr mutation response malformed")
        head_obj = _as_dict(pr.get("head"), "pr head malformed")
        base_obj = _as_dict(pr.get("base"), "pr base malformed")
        if str(pr.get("state") or "").upper() != "OPEN":
            raise AmbiguousWriteError("confirmed PR is not open")
        if str(head_obj.get("ref")) != effect.head_branch:
            raise AmbiguousWriteError("confirmed PR head branch mismatch")
        if str(base_obj.get("ref")) != effect.base_branch:
            raise AmbiguousWriteError("confirmed PR base branch mismatch")
        if str(head_obj.get("sha") or "").lower() != effect.bound_head_sha:
            raise AmbiguousWriteError("confirmed PR head SHA mismatch")
        if self._pr_marker_state(pr, marker, title) != "applied":
            raise AmbiguousWriteError("confirmed PR marker/content mismatch")
        return self._pr_bound(effect, pr)

    def _pr_proof(
        self,
        strategy: ReconciliationStrategyKind,
        pr: dict[str, Any],
        marker: ContentBoundMarker,
        title: str,
        effect: CreateOrUpdatePrEffect,
        now: datetime,
    ) -> ReconciliationProof:
        state = self._pr_marker_state(pr, marker, title)
        if state == "applied":
            return ReconciliationProof(
                proof=WriteProofKind.APPLIED,
                strategy=strategy,
                confirmed_outcome=self._pr_bound(effect, pr),
                safe_summary="unique PR carries the exact owned marker and content",
            )
        if state != "ambiguous" and self._eligible_pr_preimage(pr):
            return proven_not_applied_proof(
                strategy=strategy,
                occurred_at=now,
                failed_attempt=effect.attempt,
                safe_summary="unique PR has an owned preimage without the intended content",
            )
        # Human-edited or foreign text without an owned preimage is not safe absence.
        return _unresolved(strategy, "PR marker/content absent or ambiguous")

    # -- comment / thread reads -----------------------------------------

    def _find_marked_issue_comments(
        self, *, owner: str, name: str, number: int, needle: str
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        items = 0
        previous: str | None = None
        while True:
            pages += 1
            if pages > self._policy.max_pages:
                raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "issue comment pages exceeded")
            result = self._reads.fetch_issue_comments_page(
                owner=owner,
                name=name,
                number=number,
                cursor=cursor,
                timeout_seconds=self._call_timeout(),
            )
            data = _as_dict(result.body_json, "issue comment response malformed")
            conn = _dig(data, ("data", "repository", "pullRequest", "comments"))
            nodes, has_next, end = _connection(conn)
            items += len(nodes)
            for node in nodes:
                nd = _as_dict(node, "issue comment node malformed")
                if needle in str(nd.get("body") or ""):
                    matches.append(nd)
            if items > self._policy.max_items or len(matches) > self._policy.max_items:
                raise _block(
                    GatewayBlockKind.MALFORMED_EVIDENCE, "issue comment item limit exceeded"
                )
            if not has_next:
                break
            if end is None or end == previous:
                raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "issue comment cursor stalled")
            previous = end
            cursor = end
        return matches

    def _find_marked_thread_comments(self, thread_id: str, needle: str) -> list[str]:
        matches: list[str] = []
        cursor: str | None = None
        pages = 0
        items = 0
        previous: str | None = None
        while True:
            pages += 1
            if pages > self._policy.max_pages:
                raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "thread comment pages exceeded")
            result = self._writes.fetch_thread_comments_page(
                thread_id=thread_id,
                cursor=cursor,
                timeout_seconds=self._call_timeout(),
            )
            data = _as_dict(result.body_json, "thread comment response malformed")
            node = _dig(data, ("data", "node"))
            node_dict = _as_dict(node, "thread node malformed")
            node_id = _require_str(node_dict.get("id"), "thread comment page node id malformed")
            if node_id != thread_id:
                raise _block(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    "thread comment page node id mismatch",
                )
            conn = _as_dict(node_dict.get("comments"), "thread comments malformed")
            nodes, has_next, end = _connection(conn)
            items += len(nodes)
            for cnode in nodes:
                cd = _as_dict(cnode, "thread comment node malformed")
                body = str(cd.get("body") or "")
                if needle in body:
                    matches.append(body)
            if items > self._policy.max_items or len(matches) > self._policy.max_items:
                raise _block(
                    GatewayBlockKind.MALFORMED_EVIDENCE, "thread comment item limit exceeded"
                )
            if not has_next:
                break
            if end is None or end == previous:
                raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "thread comment cursor stalled")
            previous = end
            cursor = end
        return matches

    def _thread_resolved(self, thread_id: str) -> bool:
        result = self._writes.fetch_thread_resolved(
            thread_id=thread_id, timeout_seconds=self._call_timeout()
        )
        data = _as_dict(result.body_json, "thread resolved response malformed")
        node = _as_dict(_dig(data, ("data", "node")), "thread node malformed")
        node_id = _require_str(node.get("id"), "thread resolved node id malformed")
        if node_id != thread_id:
            raise _block(
                GatewayBlockKind.CONTRADICTORY_EVIDENCE, "thread resolved node id mismatch"
            )
        value = node.get("isResolved")
        if not isinstance(value, bool):
            raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "isResolved missing")
        return value

    def _resolved_from_response(self, body_json: object, thread_id: str) -> bool:
        data = _as_dict(body_json, "resolve response malformed")
        thread = _dig(data, ("data", "resolveReviewThread", "thread"))
        thread_dict = _as_dict(thread, "resolve thread payload malformed")
        node_id = _require_str(thread_dict.get("id"), "resolve thread id malformed")
        if node_id != thread_id:
            return False
        return thread_dict.get("isResolved") is True

    def _reply_body_from_response(self, body_json: object) -> str:
        data = _as_dict(body_json, "reply response malformed")
        comment = _dig(data, ("data", "addPullRequestReviewThreadReply", "comment"))
        comment_dict = _as_dict(comment, "reply comment payload malformed")
        return str(comment_dict.get("body") or "")

    def _reply_id_from_response(self, body_json: object) -> str:
        data = _as_dict(body_json, "reply response malformed")
        comment = _dig(data, ("data", "addPullRequestReviewThreadReply", "comment"))
        comment_dict = _as_dict(comment, "reply comment payload malformed")
        return str(comment_dict.get("id") or "")


def _unresolved(strategy: ReconciliationStrategyKind, summary: str) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.UNRESOLVED, strategy=strategy, safe_summary=summary
    )


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> object:
    """Walk nested mapping keys; missing keys are typed malformed evidence."""

    cursor: object = data
    for key in path:
        mapping = _as_dict(cursor, f"missing '{key}' object")
        if key not in mapping:
            raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, f"missing nested key '{key}'")
        cursor = mapping[key]
    return cursor


def _connection(conn: object) -> tuple[list[Any], bool, str | None]:
    connection = _as_dict(conn, "connection malformed")
    nodes = _as_list(connection.get("nodes"), "connection nodes malformed")
    page_info = _as_dict(connection.get("pageInfo"), "connection pageInfo malformed")
    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        raise _block(GatewayBlockKind.MALFORMED_EVIDENCE, "hasNextPage malformed")
    end = page_info.get("endCursor")
    end_cursor = end if isinstance(end, str) and end else None
    return nodes, has_next, end_cursor


__all__ = ["GitHubWriteGateway"]
