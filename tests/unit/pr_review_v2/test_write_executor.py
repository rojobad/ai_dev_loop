"""Phase 16.6 unit tests: MUTATING WriteExecutor mapping and authority fencing.

Covers dispatch to all seven writes, authority-before-write with zero-write
rejection, WriteOutcomeUncertain after ambiguous post-start, transient->retryable,
block->blocked, and token/kind guards. A fake gateway pair stands in for the real
Git/GitHub gateways so the executor's mapping is isolated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.unit.pr_review_v2 import write_helpers as H

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    WriteAuthorityStatus,
    WriteGatewaySuccess,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, TriggerEvidence
from ai_dev_loop.pr_review_v2.domain.events import (
    CommitRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    PushConfirmedOutcome,
    ReviewTriggerConfirmedOutcome,
    ThreadReplyConfirmedOutcome,
    ThreadResolutionConfirmedOutcome,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.workers.write_executor import (
    UnsupportedMutatingEffectError,
    WriteExecutor,
)

_REF = ArtifactRef(relative_path="artifacts/x.txt", sha256="d" * 64)


def _outcome(kind: str):
    return {
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
    }[kind]


@dataclass
class FakeGateways:
    """Stands in for both git + github gateways; records authority ordering."""

    behavior: dict[str, Any] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    authorize_calls: int = 0

    def _run(self, kind: str, authorize):
        # Authority is consulted immediately before the mutating call.
        authorize()
        self.authorize_calls += 1
        beh = self.behavior.get(kind, "ok")
        if isinstance(beh, BaseException):
            raise beh
        self.writes.append(kind)
        return WriteGatewaySuccess(outcome=_outcome(kind), already_applied=False)

    # git gateway surface
    def commit(self, effect, *, run_id, now, authorize):
        return self._run("commit", authorize)

    def push(self, effect, *, run_id, now, authorize):
        return self._run("push", authorize)

    # github gateway surface
    def create_or_update_pr(self, effect, *, run_id, now, authorize):
        return self._run("create_or_update_pr", authorize)

    def request_review(self, effect, *, run_id, now, authorize):
        return self._run("request_review", authorize)

    def post_thread_reply(self, effect, *, run_id, now, authorize):
        return self._run("post_thread_reply", authorize)

    def update_pr_text(self, effect, *, run_id, now, authorize):
        return self._run("update_pr_text", authorize)

    def resolve_thread(self, effect, *, run_id, now, authorize):
        return self._run("resolve_thread", authorize)


def _executor(gw: FakeGateways) -> WriteExecutor:
    return WriteExecutor(
        git_gateway=gw,  # type: ignore[arg-type]
        github_gateway=gw,  # type: ignore[arg-type]
        github_policy=H.github_policy(),
    )


def _run(effect, gw: FakeGateways, *, authority=None):
    authority = authority or H.FixedAuthority()
    return _executor(gw).execute(
        effect,
        H.token_for(effect),
        now=H.NOW,
        authority=authority,
        claim=H.claim_for(effect),
    )


def _all_effects(tmp_path):
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


# -- dispatch + authority for each of the seven writes ---------------------


@pytest.mark.parametrize(
    "kind",
    [
        "commit",
        "push",
        "create_or_update_pr",
        "request_review",
        "post_thread_reply",
        "update_pr_text",
        "resolve_thread",
    ],
)
def test_each_write_consults_authority_then_succeeds(tmp_path, kind: str) -> None:
    effect = _all_effects(tmp_path)[kind]
    gw = FakeGateways()
    auth = H.FixedAuthority()
    result = _run(effect, gw, authority=auth)
    assert isinstance(result, EffectSucceeded)
    assert gw.writes == [kind]
    assert auth.calls == 1


@pytest.mark.parametrize(
    "kind",
    [
        "commit",
        "push",
        "create_or_update_pr",
        "request_review",
        "post_thread_reply",
        "update_pr_text",
        "resolve_thread",
    ],
)
def test_authority_rejection_is_zero_writes(tmp_path, kind: str) -> None:
    from ai_dev_loop.pr_review_v2.application.write_contracts import AuthorityLostError

    effect = _all_effects(tmp_path)[kind]
    gw = FakeGateways()
    auth = H.FixedAuthority(status=WriteAuthorityStatus.REJECTED)
    with pytest.raises(AuthorityLostError):
        _run(effect, gw, authority=auth)
    assert gw.writes == []  # zero mutations performed
    assert auth.calls == 1


# -- ambiguity, transient, block mapping ----------------------------------


def test_ambiguous_write_maps_to_uncertain(tmp_path) -> None:
    effect = _all_effects(tmp_path)["create_or_update_pr"]
    gw = FakeGateways(behavior={"create_or_update_pr": AmbiguousWriteError("uncertain")})
    result = _run(effect, gw)
    assert isinstance(result, WriteOutcomeUncertain)
    assert result.original_write.effect_id == effect.effect_id
    assert result.reconciliation_identity is not None
    assert gw.writes == []


def test_transient_maps_to_retryable(tmp_path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import transient_error

    effect = _all_effects(tmp_path)["create_or_update_pr"]
    gw = FakeGateways(behavior={"create_or_update_pr": transient_error()})
    result = _run(effect, gw)
    assert isinstance(result, EffectRetryableFailure)
    assert result.next_attempt_at is not None


def test_block_maps_to_blocked(tmp_path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import block_error

    effect = _all_effects(tmp_path)["create_or_update_pr"]
    gw = FakeGateways(behavior={"create_or_update_pr": block_error()})
    result = _run(effect, gw)
    assert isinstance(result, EffectBlocked)


def test_git_transport_error_maps(tmp_path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError

    effect = _all_effects(tmp_path)["commit"]
    gw = FakeGateways(behavior={"commit": GitTransportError(transient=_git_transient())})
    result = _run(effect, gw)
    assert isinstance(result, EffectRetryableFailure)


def _git_transient():
    from ai_dev_loop.pr_review_v2.application.github_read import (
        GatewayTransient,
        GatewayTransientKind,
    )
    from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind

    return GatewayTransient(
        kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
        safe_summary="git transient",
        transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
    )


# -- guards ----------------------------------------------------------------


def test_token_mismatch_is_rejected(tmp_path) -> None:
    effect = _all_effects(tmp_path)["resolve_thread"]
    bad_token = H.token_for(effect).model_copy(update={"bound_head_sha": "e" * 40})
    with pytest.raises(UnsupportedMutatingEffectError):
        _executor(FakeGateways()).execute(
            effect,
            bad_token,
            now=H.NOW,
            authority=H.FixedAuthority(),
            claim=H.claim_for(effect),
        )


def test_non_mutating_effect_is_rejected(tmp_path) -> None:
    # A reconcile effect is not a mutating effect.
    original = _all_effects(tmp_path)["resolve_thread"]
    reconcile = H.reconcile_effect(original)
    with pytest.raises(UnsupportedMutatingEffectError):
        _executor(FakeGateways()).execute(
            reconcile,
            H.token_for(reconcile),
            now=H.NOW,
            authority=H.FixedAuthority(),
            claim=H.claim_for(reconcile),
        )
