"""Commit and push execution plus FIND_COMMIT_AT_HEAD / FIND_REMOTE_REF proof.

The gateway performs local/remote preflight under a generic repository lock, calls
the worker-supplied authority guard immediately before the single mutating process,
confirms the exact result, and emits a durable confirmed outcome. Ambiguous
post-start results raise ``AmbiguousWriteError`` so the executor emits
``WriteOutcomeUncertain`` (never an inline second write). No SQLite transaction is
held across the repository lock or the process.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

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
    GitRemoteScheme,
    GitWritePolicy,
    ReconciliationProof,
    WriteGatewaySuccess,
    WriteProofKind,
    derive_commit_trailer,
    opaque_repository_lock_id,
    opaque_run_lock_id,
    repository_lock_contention_transient,
    validate_branch_name,
    validate_remote_name,
    validate_remote_ref,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import (
    proven_not_applied_proof,
)
from ai_dev_loop.pr_review_v2.domain.common import ReconciliationStrategyKind, TransientErrorKind
from ai_dev_loop.pr_review_v2.domain.effects import CommitPatchEffect, PushCommitEffect
from ai_dev_loop.pr_review_v2.domain.events import CommitRecordedOutcome, PushConfirmedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitTransportError,
    GitWriteTransport,
    extract_remote_nwo,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)

AuthorizeCallback = Callable[[], None]
_T = TypeVar("_T")


@dataclass
class _OperationBudget:
    """Per-call deadline/mutation flag; never shared across concurrent workers."""

    deadline: float
    mutation_started: bool = False


# Operation-local so a lock-contending call cannot clear another worker's budget.
_OPERATION_BUDGET: ContextVar[_OperationBudget | None] = ContextVar(
    "git_publication_operation_budget", default=None
)


def _block_error(kind: GatewayBlockKind, detail: str) -> GitTransportError:
    return GitTransportError(block=block_for_kind(kind, detail=detail))


def _require_safe_branch(value: str, *, detail: str) -> str:
    try:
        return validate_branch_name(value)
    except ValueError as exc:
        raise _block_error(GatewayBlockKind.HTTP_VALIDATION_REJECTION, detail) from exc


def _require_safe_remote_ref(value: str, *, detail: str) -> str:
    try:
        return validate_remote_ref(value)
    except ValueError as exc:
        raise _block_error(GatewayBlockKind.HTTP_VALIDATION_REJECTION, detail) from exc


class GitPublicationGateway:
    def __init__(
        self,
        *,
        policy: GitWritePolicy,
        transport: GitWriteTransport,
        input_reader: InputArtifactReader,
        remote_name: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._reader = input_reader
        # Policy remote_name is authoritative; an explicit override must match it.
        if remote_name is not None and remote_name != policy.remote_name:
            raise ValueError("gateway remote_name override must match GitWritePolicy.remote_name")
        self._remote = validate_remote_name(policy.remote_name)
        self._monotonic = monotonic
        if hasattr(self._transport, "set_timeout_provider"):
            self._transport.set_timeout_provider(self._call_timeout)

    # -- repository lock / deadline --------------------------------------

    def _repo_lock(self, *, run_id: str, repo_root: str) -> FileLock:
        del run_id
        return FileLock(repository_lock_path(Path(repo_root)))

    def _lock_metadata(self, *, run_id: str, repo_root: str, now: datetime) -> LockMetadata:
        return LockMetadata(
            pid=os.getpid(),
            run_id=opaque_run_lock_id(run_id),
            repository_path=opaque_repository_lock_id(repo_root),
            started_at=now,
        )

    def _begin_deadline(self) -> None:
        """Start the overall operation deadline before the first Git subprocess."""

        if _OPERATION_BUDGET.get() is None:
            _OPERATION_BUDGET.set(
                _OperationBudget(deadline=self._monotonic() + self._policy.overall_timeout_seconds)
            )

    def _clear_deadline(self) -> None:
        _OPERATION_BUDGET.set(None)

    def _acquire_lock(self, *, run_id: str, repo_root: str, now: datetime) -> FileLock:
        lock = self._repo_lock(run_id=run_id, repo_root=repo_root)
        try:
            lock.acquire(self._lock_metadata(run_id=run_id, repo_root=repo_root, now=now))
        except LockError as exc:
            raise GitTransportError(transient=repository_lock_contention_transient()) from exc
        self._begin_deadline()
        return lock

    def _release_lock(self, lock: FileLock) -> None:
        self._clear_deadline()
        lock.release()

    def _call_timeout(self) -> float:
        budget = _OPERATION_BUDGET.get()
        if budget is None:
            return float(self._policy.per_call_timeout_seconds)
        remaining = budget.deadline - self._monotonic()
        if remaining <= 0:
            if budget.mutation_started:
                raise AmbiguousWriteError("git overall deadline exceeded after mutation start")
            raise GitTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="git overall deadline exceeded before mutation",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )
        return min(float(self._policy.per_call_timeout_seconds), remaining)

    def _authorize(self, authorize: AuthorizeCallback) -> None:
        """Consult authority without marking mutation start (dispatch sets the flag)."""

        self._call_timeout()  # fail closed if deadline already elapsed
        authorize()

    def _mark_mutation_started(self) -> None:
        budget = _OPERATION_BUDGET.get()
        if budget is None:
            _OPERATION_BUDGET.set(
                _OperationBudget(deadline=self._monotonic(), mutation_started=True)
            )
            return
        budget.mutation_started = True

    def _after_authorize(self, action: Callable[[], _T]) -> _T:
        try:
            return action()
        except AmbiguousWriteError:
            raise
        except AuthorityLostError:
            raise
        except GitTransportError as exc:
            raise AmbiguousWriteError("git write outcome uncertain after mutation start") from exc
        except Exception as exc:
            raise AmbiguousWriteError("git write outcome uncertain after mutation start") from exc

    # -- commit ----------------------------------------------------------

    def commit(
        self,
        effect: CommitPatchEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        try:
            patch_bytes = self._reader.read_patch_bytes(
                run_id=run_id, ref=effect.patch_ref, max_bytes=self._policy.max_patch_bytes
            )
            message = self._reader.read_commit_message(
                run_id=run_id,
                ref=effect.commit_message_ref,
                max_bytes=self._policy.max_commit_message_bytes,
            )
        except InputArtifactError as exc:
            raise _block_error(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                "commit input artifact was invalid",
            ) from exc

        trailer = derive_commit_trailer(idempotency_key=effect.idempotency_key)
        commit_message = _compose_commit_message(message.subject, message.body, trailer)
        _require_safe_branch(effect.expected_branch, detail="commit expected_branch is unsafe")
        # Overall deadline covers root inspection through confirmation.
        self._begin_deadline()
        try:
            repo_root = self._resolve_root()
            lock = self._acquire_lock(run_id=run_id, repo_root=repo_root, now=now)
        except Exception:
            self._clear_deadline()
            raise
        try:
            self._require_branch(effect.expected_branch)
            head = self._transport.read_head_sha()

            # Idempotent success: HEAD is already the exact owned commit.
            if head != effect.expected_head_sha:
                if self._is_owned_commit(head, effect, trailer):
                    remote_baseline = self._read_commit_remote_baseline(effect, head)
                    return WriteGatewaySuccess(
                        outcome=CommitRecordedOutcome(
                            commit_sha=head,
                            new_head_sha=head,
                            expected_remote_sha_before_push=remote_baseline,
                        ),
                        already_applied=True,
                    )
                raise _block_error(GatewayBlockKind.HEAD_DRIFT, "HEAD is not the expected parent")

            self._require_clean_except_staged()
            staged = self._transport.read_staged_patch_bytes()
            if not staged:
                raise _block_error(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION, "no staged changes to commit"
                )
            if _sha256(staged) != effect.patch_ref.sha256 or staged != patch_bytes:
                raise _block_error(GatewayBlockKind.MALFORMED_EVIDENCE, "staged patch drift")

            # Normative order: authority, then durable remote baseline, then one commit.
            self._authorize(authorize)
            remote_baseline = self._read_commit_remote_baseline(effect, effect.expected_head_sha)

            def mutate() -> WriteGatewaySuccess:
                self._mark_mutation_started()
                outcome = self._transport.commit_with_message_stdin(commit_message)
                if outcome.timed_out:
                    raise AmbiguousWriteError("commit process timed out")
                if outcome.returncode != 0:
                    post = self._transport.read_head_sha()
                    if post == effect.expected_head_sha:
                        raise _block_error(
                            GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                            "commit failed and HEAD is unchanged",
                        )
                    raise AmbiguousWriteError("commit returned nonzero with a changed HEAD")

                new_head = self._transport.read_head_sha()
                if new_head == effect.expected_head_sha or not self._is_owned_commit(
                    new_head, effect, trailer
                ):
                    raise AmbiguousWriteError("commit could not be confirmed at HEAD")
                return WriteGatewaySuccess(
                    outcome=CommitRecordedOutcome(
                        commit_sha=new_head,
                        new_head_sha=new_head,
                        expected_remote_sha_before_push=remote_baseline,
                    ),
                    already_applied=False,
                )

            return self._after_authorize(mutate)
        finally:
            self._release_lock(lock)

    def reconcile_commit(
        self, effect: CommitPatchEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD
        trailer = derive_commit_trailer(idempotency_key=effect.idempotency_key)
        try:
            validate_branch_name(effect.expected_branch)
        except ValueError:
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="commit expected_branch is unsafe",
            )
        try:
            patch_bytes = self._reader.read_patch_bytes(
                run_id=run_id, ref=effect.patch_ref, max_bytes=self._policy.max_patch_bytes
            )
        except InputArtifactError:
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="commit patch artifact unavailable for reconciliation",
            )
        self._begin_deadline()
        try:
            repo_root = self._resolve_root()
        except GitTransportError:
            self._clear_deadline()
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="commit reconciliation could not inspect repository root",
            )
        try:
            lock = self._acquire_lock(run_id=run_id, repo_root=repo_root, now=now)
        except GitTransportError:
            self._clear_deadline()
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="commit reconciliation could not acquire repository lock",
            )
        try:
            try:
                self._require_branch(effect.expected_branch)
                head = self._transport.read_head_sha()
            except GitTransportError:
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="commit reconciliation could not read branch/HEAD",
                )
            if self._is_owned_commit(head, effect, trailer):
                try:
                    remote_baseline = self._read_commit_remote_baseline(effect, head)
                    self._validate_remote_identity_for_repo(effect.repository)
                except GitTransportError:
                    return ReconciliationProof(
                        proof=WriteProofKind.UNRESOLVED,
                        strategy=strategy,
                        safe_summary="commit applied but remote evidence incomplete",
                    )
                return ReconciliationProof(
                    proof=WriteProofKind.APPLIED,
                    strategy=strategy,
                    confirmed_outcome=CommitRecordedOutcome(
                        commit_sha=head,
                        new_head_sha=head,
                        expected_remote_sha_before_push=remote_baseline,
                    ),
                    safe_summary="commit proven applied at HEAD",
                )
            if head != effect.expected_head_sha:
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="HEAD drifted from expected parent without owned commit",
                )
            try:
                self._require_clean_except_staged()
                staged = self._transport.read_staged_patch_bytes()
                self._validate_remote_identity_for_repo(effect.repository)
                remote = self._transport.read_remote_ref(self._remote, effect.expected_branch)
            except GitTransportError:
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="commit absence evidence incomplete",
                )
            if not (
                staged
                and staged == patch_bytes
                and _sha256(staged) == effect.patch_ref.sha256
                and remote.complete
            ):
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="commit reconciliation unresolved",
                )
            # Remote must be absent or an eligible durable baseline under ancestry rules.
            if remote.sha is None:
                return proven_not_applied_proof(
                    strategy=strategy,
                    occurred_at=now,
                    failed_attempt=effect.attempt,
                    safe_summary="commit not applied; branch/HEAD/patch/remote evidence exact",
                )
            if remote.sha == effect.expected_head_sha or self._transport.is_ancestor(
                remote.sha, effect.expected_head_sha
            ):
                return proven_not_applied_proof(
                    strategy=strategy,
                    occurred_at=now,
                    failed_attempt=effect.attempt,
                    safe_summary="commit not applied; branch/HEAD/patch/remote evidence exact",
                )
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="remote ref is a third or non-ancestor SHA",
            )
        finally:
            self._release_lock(lock)

    # -- push ------------------------------------------------------------

    def push(
        self,
        effect: PushCommitEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: AuthorizeCallback,
    ) -> WriteGatewaySuccess:
        _require_safe_remote_ref(effect.remote_ref, detail="push remote_ref is unsafe")
        self._begin_deadline()
        try:
            repo_root = self._resolve_root()
            lock = self._acquire_lock(run_id=run_id, repo_root=repo_root, now=now)
        except Exception:
            self._clear_deadline()
            raise
        try:
            head = self._transport.read_head_sha()
            if head != effect.commit_sha:
                raise _block_error(GatewayBlockKind.HEAD_DRIFT, "local HEAD is not the commit")
            self._require_branch_for_ref(effect.remote_ref)
            self._validate_remote_identity(effect)
            self._require_ssh_agent_if_needed()

            remote = self._transport.read_remote_ref(self._remote, effect.remote_ref)
            if not remote.complete:
                raise _block_error(
                    GatewayBlockKind.MALFORMED_EVIDENCE, "remote ref read incomplete"
                )
            if remote.sha == effect.commit_sha:
                return WriteGatewaySuccess(
                    outcome=PushConfirmedOutcome(
                        commit_sha=effect.commit_sha, remote_ref=effect.remote_ref
                    ),
                    already_applied=True,
                )
            if remote.sha != effect.expected_remote_sha_before_push:
                raise _block_error(
                    GatewayBlockKind.BRANCH_DRIFT,
                    "remote ref drifted from the durable pre-push baseline",
                )

            self._authorize(authorize)

            def mutate() -> WriteGatewaySuccess:
                self._mark_mutation_started()
                outcome = self._transport.push_non_force(
                    remote_name=self._remote,
                    commit_sha=effect.commit_sha,
                    remote_ref=effect.remote_ref,
                )
                if outcome.timed_out:
                    raise AmbiguousWriteError("push process timed out")
                confirm = self._transport.read_remote_ref(self._remote, effect.remote_ref)
                if not confirm.complete:
                    raise AmbiguousWriteError("post-push remote read incomplete")
                if confirm.sha == effect.commit_sha:
                    return WriteGatewaySuccess(
                        outcome=PushConfirmedOutcome(
                            commit_sha=effect.commit_sha, remote_ref=effect.remote_ref
                        ),
                        already_applied=False,
                    )
                if (
                    outcome.returncode != 0
                    and confirm.sha == effect.expected_remote_sha_before_push
                ):
                    raise _block_error(
                        GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                        "push failed and remote ref is unchanged",
                    )
                raise AmbiguousWriteError("push could not be confirmed at the remote ref")

            return self._after_authorize(mutate)
        finally:
            self._release_lock(lock)

    def reconcile_push(
        self, effect: PushCommitEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        strategy = ReconciliationStrategyKind.FIND_REMOTE_REF
        try:
            validate_remote_ref(effect.remote_ref)
        except ValueError:
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="push remote_ref is unsafe",
            )
        self._begin_deadline()
        try:
            repo_root = self._resolve_root()
        except GitTransportError:
            self._clear_deadline()
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="push reconciliation could not inspect repository root",
            )
        try:
            lock = self._acquire_lock(run_id=run_id, repo_root=repo_root, now=now)
        except GitTransportError:
            self._clear_deadline()
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="push reconciliation could not acquire repository lock",
            )
        try:
            try:
                head = self._transport.read_head_sha()
                if head != effect.commit_sha:
                    return ReconciliationProof(
                        proof=WriteProofKind.UNRESOLVED,
                        strategy=strategy,
                        safe_summary="local HEAD is not the intended commit",
                    )
                self._require_branch_for_ref(effect.remote_ref)
                self._validate_remote_identity(effect)
                remote = self._transport.read_remote_ref(self._remote, effect.remote_ref)
            except GitTransportError:
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="push reconciliation evidence incomplete",
                )
            if not remote.complete:
                return ReconciliationProof(
                    proof=WriteProofKind.UNRESOLVED,
                    strategy=strategy,
                    safe_summary="remote ref evidence incomplete",
                )
            if remote.sha == effect.commit_sha:
                return ReconciliationProof(
                    proof=WriteProofKind.APPLIED,
                    strategy=strategy,
                    confirmed_outcome=PushConfirmedOutcome(
                        commit_sha=effect.commit_sha, remote_ref=effect.remote_ref
                    ),
                    safe_summary="remote ref equals the intended commit",
                )
            if remote.sha == effect.expected_remote_sha_before_push:
                return proven_not_applied_proof(
                    strategy=strategy,
                    occurred_at=now,
                    failed_attempt=effect.attempt,
                    safe_summary="remote ref still equals the durable baseline",
                )
            return ReconciliationProof(
                proof=WriteProofKind.UNRESOLVED,
                strategy=strategy,
                safe_summary="remote ref drifted to a third value",
            )
        finally:
            self._release_lock(lock)

    # -- shared helpers --------------------------------------------------

    def _resolve_root(self) -> str:
        root = self._transport.inspect_repository_root()
        expected = str(Path(self._policy.repository_cwd).resolve())
        if str(Path(root).resolve()) != expected:
            raise _block_error(
                GatewayBlockKind.REPOSITORY_DRIFT, "repository root drifted from policy cwd"
            )
        return root

    def _require_branch(self, expected_branch: str) -> None:
        branch = self._transport.read_current_branch()
        if branch != expected_branch:
            raise _block_error(GatewayBlockKind.BRANCH_DRIFT, "current branch is not expected")

    def _require_branch_for_ref(self, remote_ref: str) -> None:
        branch = self._transport.read_current_branch()
        expected = remote_ref.removeprefix("refs/heads/")
        if branch != expected:
            raise _block_error(
                GatewayBlockKind.BRANCH_DRIFT, "current branch does not match remote ref"
            )

    def _require_clean_except_staged(self) -> None:
        status = self._transport.read_status_porcelain()
        for line in status.splitlines():
            if not line:
                continue
            xy = line[:2]
            worktree = xy[1] if len(xy) > 1 else " "
            if xy == "??":
                raise _block_error(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    "untracked non-ignored files present before commit",
                )
            if worktree != " ":
                raise _block_error(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    "tracked unstaged changes present before commit",
                )

    def _validate_remote_identity(self, effect: PushCommitEffect) -> None:
        self._validate_remote_identity_for_repo(effect.repository)

    def _validate_remote_identity_for_repo(self, repository: object) -> None:
        if not self._policy.enforce_remote_identity:
            return
        name_with_owner = getattr(repository, "name_with_owner", None)
        if not isinstance(name_with_owner, str):
            raise _block_error(
                GatewayBlockKind.REPOSITORY_DRIFT,
                "repository identity missing for remote validation",
            )
        url = self._transport.read_remote_url(self._remote)
        nwo = extract_remote_nwo(url)
        if nwo is None or nwo != name_with_owner:
            raise _block_error(
                GatewayBlockKind.REPOSITORY_DRIFT,
                "remote URL does not match the effect repository",
            )

    def _require_ssh_agent_if_needed(self) -> None:
        if (
            self._policy.remote_scheme is GitRemoteScheme.SSH
            and self._policy.require_ssh_agent_identity
            and not self._transport.check_ssh_agent()
        ):
            raise _block_error(
                GatewayBlockKind.AUTHENTICATION, "no usable SSH agent identity for push"
            )

    def _is_owned_commit(self, head: str, effect: CommitPatchEffect, trailer: str) -> bool:
        try:
            parents = self._transport.read_commit_parents(head)
            if len(parents) != 1 or parents[0] != effect.expected_head_sha:
                return False
            branch = self._transport.read_current_branch()
            if branch != effect.expected_branch:
                return False
            message = self._transport.read_commit_message_raw(head)
            if trailer not in message:
                return False
            patch = self._transport.read_commit_patch_bytes(effect.expected_head_sha, head)
            return _sha256(patch) == effect.patch_ref.sha256
        except GitTransportError:
            return False

    def _read_commit_remote_baseline(
        self, effect: CommitPatchEffect, head_for_ancestry: str
    ) -> str | None:
        remote = self._transport.read_remote_ref(self._remote, effect.expected_branch)
        if not remote.complete:
            raise _block_error(
                GatewayBlockKind.MALFORMED_EVIDENCE, "remote baseline read incomplete"
            )
        if remote.sha is None:
            return None
        # A present remote SHA must be an ancestor of the expected head (parent) or
        # equal to the head itself (already pushed). Otherwise it is drift.
        if remote.sha == head_for_ancestry:
            return remote.sha
        if not self._transport.is_ancestor(remote.sha, effect.expected_head_sha):
            raise _block_error(
                GatewayBlockKind.BRANCH_DRIFT,
                "remote ref is not an ancestor of the expected head",
            )
        return remote.sha


def _compose_commit_message(subject: str, body: str, trailer: str) -> str:
    parts = [subject.rstrip("\n")]
    if body.strip():
        parts.append("")
        parts.append(body.rstrip("\n"))
    parts.append("")
    parts.append(trailer)
    return "\n".join(parts) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["GitPublicationGateway"]
