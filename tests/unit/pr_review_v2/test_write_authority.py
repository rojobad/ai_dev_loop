"""Phase 16.6 unit tests: pre-mutation authority snapshot and guard fencing.

The authority snapshot must carry only safe claim/lease identity (no patch bytes,
prompts, or artifact contents), must be derived faithfully from the claim, and the
write executor must consult the guard with exactly that snapshot immediately before
a mutation. Guard rejection must fence to zero writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.github_write_helpers import FakeAuthority

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AuthorityLostError,
    ClaimAuthoritySnapshot,
    WriteAuthorityStatus,
    WriteGatewaySuccess,
    claim_authority_snapshot_from_claim,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectSucceeded,
    ThreadResolutionConfirmedOutcome,
)
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor


def test_snapshot_is_faithfully_derived_from_claim() -> None:
    effect = H.resolve_thread_effect()
    claim = H.claim_for(effect)
    snap = claim_authority_snapshot_from_claim(claim)
    assert snap.run_id == claim.run_id
    assert snap.dispatch_id == claim.dispatch_id
    assert snap.claim_id == claim.claim_id
    assert snap.owner_id == claim.owner_id
    assert snap.lease_generation == claim.lease_generation
    assert snap.claimed_run_version == claim.claimed_run_version
    assert snap.effect_id == effect.effect_id
    assert snap.attempt == claim.attempt
    assert snap.cycle_number == effect.cycle_number
    assert snap.bound_head_sha == effect.bound_head_sha


def test_snapshot_carries_only_safe_identity() -> None:
    effect = H.resolve_thread_effect()
    snap = claim_authority_snapshot_from_claim(H.claim_for(effect))
    payload = snap.model_dump_json()
    # No artifact/patch/prompt content should ever appear in an authority snapshot.
    for banned in ("diff --git", "artifacts/", "reply", "publication", "commit-message"):
        assert banned not in payload
    # It is exactly the documented safe-identity field set.
    assert set(ClaimAuthoritySnapshot.model_fields) == {
        "run_id",
        "dispatch_id",
        "claim_id",
        "owner_id",
        "lease_generation",
        "claimed_run_version",
        "effect_id",
        "attempt",
        "cycle_number",
        "bound_head_sha",
    }


@dataclass
class _RecordingGateway:
    performed: list[str] = field(default_factory=list)

    def resolve_thread(self, effect, *, run_id, now, authorize):
        authorize()  # consulted immediately before the write
        self.performed.append("resolve_thread")
        return WriteGatewaySuccess(
            outcome=ThreadResolutionConfirmedOutcome(thread_id="THREAD_1"),
            already_applied=False,
        )

    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - defensive
        raise AssertionError("unexpected gateway method call")


def _executor(gw) -> WriteExecutor:
    return WriteExecutor(
        git_gateway=gw,  # type: ignore[arg-type]
        github_gateway=gw,  # type: ignore[arg-type]
        github_policy=H.github_policy(),
    )


def test_executor_consults_guard_with_exact_snapshot_once() -> None:
    effect = H.resolve_thread_effect()
    claim = H.claim_for(effect)
    gw = _RecordingGateway()
    auth = FakeAuthority()
    result = _executor(gw).execute(
        effect, H.token_for(effect), now=H.NOW, authority=auth, claim=claim
    )
    assert isinstance(result, EffectSucceeded)
    assert gw.performed == ["resolve_thread"]
    assert auth.call_count == 1
    recorded = auth.calls[0]
    assert recorded == claim_authority_snapshot_from_claim(claim)


def test_guard_rejection_before_write_is_zero_writes() -> None:
    effect = H.resolve_thread_effect()
    gw = _RecordingGateway()
    auth = FakeAuthority(status=WriteAuthorityStatus.REJECTED)
    with pytest.raises(AuthorityLostError):
        _executor(gw).execute(
            effect, H.token_for(effect), now=H.NOW, authority=auth, claim=H.claim_for(effect)
        )
    assert gw.performed == []
    assert auth.call_count == 1
