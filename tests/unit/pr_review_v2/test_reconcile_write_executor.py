"""Phase 16.6 unit tests: RECONCILING executor maps proofs to typed events.

Covers all seven reconcile strategies dispatching to the correct gateway proof,
the APPLIED / PROVEN_NOT_APPLIED / UNRESOLVED mapping, transient->retryable,
malformed/contradictory evidence collapsing to UNRESOLVED, hard blocks staying
blocked, and token/kind guards. It never performs a write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.github_write_helpers import block_error, transient_error

from ai_dev_loop.pr_review_v2.application.github_read import GatewayBlockKind, block_for_kind
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ReconciliationProof,
    WriteProofKind,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    ReconciliationResolutionKind,
    ReconciliationStrategyKind,
    TriggerEvidence,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    CommitRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    PushConfirmedOutcome,
    ReconciliationResolvedOutcome,
    ReviewTriggerConfirmedOutcome,
    ThreadReplyConfirmedOutcome,
    ThreadResolutionConfirmedOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import (
    ReconcileWriteExecutor,
    UnsupportedReconcileEffectError,
)

_REF = ArtifactRef(relative_path="artifacts/x.txt", sha256="d" * 64)

_STRATEGY = {
    "commit": ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
    "push": ReconciliationStrategyKind.FIND_REMOTE_REF,
    "create_or_update_pr": ReconciliationStrategyKind.FIND_PR_BY_HEAD_BASE,
    "request_review": ReconciliationStrategyKind.FIND_REVIEW_MARKER,
    "post_thread_reply": ReconciliationStrategyKind.FIND_THREAD_REPLY,
    "update_pr_text": ReconciliationStrategyKind.FIND_PR_TEXT,
    "resolve_thread": ReconciliationStrategyKind.FIND_THREAD_RESOLVED,
}

_CONFIRMED = {
    "commit": CommitRecordedOutcome(
        commit_sha=H.SHA_COMMIT, new_head_sha=H.SHA_COMMIT, expected_remote_sha_before_push=None
    ),
    "push": PushConfirmedOutcome(commit_sha=H.SHA_COMMIT, remote_ref="feature"),
    "create_or_update_pr": PrBoundOutcome(binding=H.binding()),
    "request_review": ReviewTriggerConfirmedOutcome(
        evidence=TriggerEvidence(marker="mk-1", comment_ref=_REF, head_sha=H.SHA_COMMIT)
    ),
    "post_thread_reply": ThreadReplyConfirmedOutcome(thread_id="THREAD_1", reply_ref=_REF),
    "update_pr_text": PrTextUpdatedOutcome(binding=H.binding(), publication_text_ref=_REF),
    "resolve_thread": ThreadResolutionConfirmedOutcome(thread_id="THREAD_1"),
}


def _applied(kind: str) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.APPLIED,
        strategy=_STRATEGY[kind],
        confirmed_outcome=_CONFIRMED[kind],
        safe_summary="applied",
    )


def _proven_not(kind: str) -> ReconciliationProof:
    from datetime import timedelta

    return ReconciliationProof(
        proof=WriteProofKind.PROVEN_NOT_APPLIED,
        strategy=_STRATEGY[kind],
        # Placeholder; ReconcileWriteExecutor recomputes via approved backoff policy.
        next_attempt_at=H.NOW + timedelta(seconds=1),
        safe_summary="not applied",
    )


def _unresolved(kind: str) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.UNRESOLVED,
        strategy=_STRATEGY[kind],
        safe_summary="unresolved",
    )


@dataclass
class FakeReconcileGateways:
    behavior: dict[str, Any] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def _run(self, kind: str):
        self.reads.append(kind)
        beh = self.behavior[kind]
        if isinstance(beh, BaseException):
            raise beh
        return beh

    def reconcile_commit(self, original, *, run_id, now):
        return self._run("commit")

    def reconcile_push(self, original, *, run_id, now):
        return self._run("push")

    def reconcile_create_or_update_pr(self, original, *, run_id, now):
        return self._run("create_or_update_pr")

    def reconcile_request_review(self, original, *, run_id, now):
        return self._run("request_review")

    def reconcile_post_thread_reply(self, original, *, run_id, now):
        return self._run("post_thread_reply")

    def reconcile_update_pr_text(self, original, *, run_id, now):
        return self._run("update_pr_text")

    def reconcile_resolve_thread(self, original, *, run_id, now):
        return self._run("resolve_thread")


def _executor(gw: FakeReconcileGateways) -> ReconcileWriteExecutor:
    return ReconcileWriteExecutor(
        git_gateway=gw,  # type: ignore[arg-type]
        github_gateway=gw,  # type: ignore[arg-type]
        github_policy=H.github_policy(),
    )


def _originals(tmp_path):
    patch = H.write_artifact(tmp_path, H.RUN_ID, "artifacts/p.bin", b"diff\n")
    msg = H.write_commit_message(tmp_path, H.RUN_ID, "subj")
    pub = H.write_publication(tmp_path, H.RUN_ID, "title", "body")
    reply = H.write_reply(tmp_path, H.RUN_ID, "reply")
    return {
        "commit": H.commit_effect(patch, msg),
        "push": H.push_effect(),
        "create_or_update_pr": H.create_pr_effect(pub),
        "request_review": H.trigger_effect("mk-1"),
        "post_thread_reply": H.reply_effect(reply),
        "update_pr_text": H.update_pr_text_effect(pub),
        "resolve_thread": H.resolve_thread_effect(),
    }


def _run(reconcile, gw):
    return _executor(gw).execute(
        reconcile,
        H.token_for(reconcile),
        now=H.NOW,
        authority=H.FixedAuthority(),
        claim=H.claim_for(reconcile),
    )


_KINDS = list(_STRATEGY.keys())


@pytest.mark.parametrize("kind", _KINDS)
def test_applied_dispatches_and_maps(tmp_path, kind: str) -> None:
    original = _originals(tmp_path)[kind]
    reconcile = H.reconcile_effect(original)
    gw = FakeReconcileGateways(behavior={kind: _applied(kind)})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectSucceeded)
    outcome = result.outcome
    assert isinstance(outcome, ReconciliationResolvedOutcome)
    assert outcome.resolution is ReconciliationResolutionKind.APPLIED
    assert outcome.confirmed_outcome is not None
    assert outcome.original_effect_id == original.effect_id
    assert gw.reads == [kind]


@pytest.mark.parametrize("kind", _KINDS)
def test_proven_not_applied_maps(tmp_path, kind: str) -> None:
    original = _originals(tmp_path)[kind]
    reconcile = H.reconcile_effect(original)
    gw = FakeReconcileGateways(behavior={kind: _proven_not(kind)})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectSucceeded)
    outcome = result.outcome
    assert outcome.resolution is ReconciliationResolutionKind.PROVEN_NOT_APPLIED
    assert outcome.confirmed_outcome is None
    assert outcome.next_attempt_at is not None
    assert outcome.next_attempt_at > H.NOW


@pytest.mark.parametrize("kind", _KINDS)
def test_unresolved_maps(tmp_path, kind: str) -> None:
    original = _originals(tmp_path)[kind]
    reconcile = H.reconcile_effect(original)
    gw = FakeReconcileGateways(behavior={kind: _unresolved(kind)})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.resolution is ReconciliationResolutionKind.UNRESOLVED


def test_transient_read_maps_to_retryable(tmp_path) -> None:
    original = _originals(tmp_path)["create_or_update_pr"]
    reconcile = H.reconcile_effect(original)
    gw = FakeReconcileGateways(behavior={"create_or_update_pr": transient_error()})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectRetryableFailure)


def test_malformed_evidence_block_collapses_to_unresolved(tmp_path) -> None:
    original = _originals(tmp_path)["create_or_update_pr"]
    reconcile = H.reconcile_effect(original)
    exc = GhTransportError(block=block_for_kind(GatewayBlockKind.MALFORMED_EVIDENCE))
    gw = FakeReconcileGateways(behavior={"create_or_update_pr": exc})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.resolution is ReconciliationResolutionKind.UNRESOLVED


def test_contradictory_evidence_block_collapses_to_unresolved(tmp_path) -> None:
    original = _originals(tmp_path)["request_review"]
    reconcile = H.reconcile_effect(original)
    exc = GhTransportError(block=block_for_kind(GatewayBlockKind.CONTRADICTORY_EVIDENCE))
    gw = FakeReconcileGateways(behavior={"request_review": exc})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.resolution is ReconciliationResolutionKind.UNRESOLVED


def test_hard_block_stays_blocked(tmp_path) -> None:
    original = _originals(tmp_path)["create_or_update_pr"]
    reconcile = H.reconcile_effect(original)
    gw = FakeReconcileGateways(behavior={"create_or_update_pr": block_error()})
    result = _run(reconcile, gw)
    assert isinstance(result, EffectBlocked)


def test_token_mismatch_rejected(tmp_path) -> None:
    original = _originals(tmp_path)["resolve_thread"]
    reconcile = H.reconcile_effect(original)
    bad = H.token_for(reconcile).model_copy(update={"bound_head_sha": "e" * 40})
    with pytest.raises(UnsupportedReconcileEffectError):
        _executor(FakeReconcileGateways()).execute(
            reconcile,
            bad,
            now=H.NOW,
            authority=H.FixedAuthority(),
            claim=H.claim_for(reconcile),
        )


def test_non_reconcile_effect_rejected(tmp_path) -> None:
    original = _originals(tmp_path)["resolve_thread"]
    with pytest.raises(UnsupportedReconcileEffectError):
        _executor(FakeReconcileGateways()).execute(
            original,
            H.token_for(original),
            now=H.NOW,
            authority=H.FixedAuthority(),
            claim=H.claim_for(original),
        )
