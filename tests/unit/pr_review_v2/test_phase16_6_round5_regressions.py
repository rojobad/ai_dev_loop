"""Phase 16.6 round-5: upstream branch argv-safety aligned with CommitPatchEffect.

Unsafe ``SourceRunOrigin`` / ``PullRequestBinding`` branches must fail at DTO
construction so reducer publication success cannot raise ``ValidationError`` when
emitting ``CommitPatchEffect``. Fake data only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.durable_helpers import (
    FakeClock,
    publication_success,
    start_run,
)
from tests.unit.pr_review_v2.helpers import (
    HASH_2,
    SHA_A,
    SHA_B,
    T0,
    artifact,
    publication_text_outcome,
    start,
    succeed,
)

from ai_dev_loop.pr_review_v2.application.contracts import EffectCompletionRequest
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    CommitPatchEffect,
    ExistingPrOrigin,
    PreparedState,
    PullRequestBinding,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
    parse_pr_review_state,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

REPO = RepositoryIdentity(name_with_owner="acme/demo")
LIMITS = WorkflowLimits(max_external_cycles=2, max_local_iterations=3)


def _safe_origin(*, head_branch: str = "feature", base_branch: str = "main") -> SourceRunOrigin:
    return SourceRunOrigin(
        source_run_id="local-run-001",
        repository=REPO,
        head_branch=head_branch,
        base_branch=base_branch,
        expected_head_sha=SHA_A,
        accepted_patch=artifact("artifacts/accepted.patch"),
        execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
    )


def _safe_binding(*, head_branch: str = "feature", base_branch: str = "main") -> PullRequestBinding:
    return PullRequestBinding(
        repository=REPO,
        pr_number=7,
        head_branch=head_branch,
        base_branch=base_branch,
        head_sha=SHA_B,
    )


# -- DTO / loading rejection ----------------------------------------------


@pytest.mark.parametrize("field", ["head_branch", "base_branch"])
def test_source_run_origin_rejects_unsafe_branch(field: str) -> None:
    kwargs = {
        "source_run_id": "local-run-001",
        "repository": REPO,
        "head_branch": "feature",
        "base_branch": "main",
        "expected_head_sha": SHA_A,
        "accepted_patch": artifact("artifacts/accepted.patch"),
        "execution_context_ref": artifact("artifacts/execution-context.json", HASH_2),
        field: "../evil",
    }
    with pytest.raises(ValidationError):
        SourceRunOrigin(**kwargs)


@pytest.mark.parametrize("field", ["head_branch", "base_branch"])
def test_pull_request_binding_rejects_unsafe_branch(field: str) -> None:
    kwargs = {
        "repository": REPO,
        "pr_number": 7,
        "head_branch": "feature",
        "base_branch": "main",
        "head_sha": SHA_B,
        field: "../evil",
    }
    with pytest.raises(ValidationError):
        PullRequestBinding(**kwargs)


def test_existing_pr_origin_rejects_unsafe_binding_branch() -> None:
    with pytest.raises(ValidationError):
        ExistingPrOrigin(
            binding=PullRequestBinding(
                repository=REPO,
                pr_number=7,
                head_branch="../evil",
                base_branch="main",
                head_sha=SHA_B,
            ),
            execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
        )


def test_prepared_state_rejects_unsafe_source_origin_branch() -> None:
    with pytest.raises(ValidationError):
        PreparedState(
            run_id="run-1",
            origin=SourceRunOrigin(
                source_run_id="local-run-001",
                repository=REPO,
                head_branch="feature;rm",
                base_branch="main",
                expected_head_sha=SHA_A,
                accepted_patch=artifact("artifacts/accepted.patch"),
                execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
            ),
            limits=LIMITS,
            entered_at=T0,
        )


def test_parse_state_rejects_loaded_unsafe_origin_branch() -> None:
    prepared = PreparedState(
        run_id="run-1",
        origin=_safe_origin(),
        limits=LIMITS,
        entered_at=T0,
    )
    payload = json.loads(prepared.model_dump_json())
    payload["origin"]["head_branch"] = "../evil"
    with pytest.raises(ValidationError):
        parse_pr_review_state(payload)


# -- Reducer paths: safe branches transition; no ValidationError ----------


def test_initial_publication_from_source_origin_emits_safe_commit_effect() -> None:
    prepared = PreparedState(
        run_id="run-source",
        origin=_safe_origin(head_branch="feature/ok", base_branch="main"),
        limits=LIMITS,
        entered_at=T0,
    )
    state, effects = start(prepared)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0]
    assert isinstance(commit, CommitPatchEffect)
    assert commit.expected_branch == "feature/ok"
    # Constructing the same shape must remain valid (complete_claim path).
    CommitPatchEffect.model_validate(commit.model_dump(mode="json"))


def test_fix_publication_from_binding_emits_safe_commit_effect() -> None:
    """Fix publication copies ``PullRequestBinding.head_branch`` into CommitPatchEffect."""

    from tests.unit.pr_review_v2.helpers import (
        actionable_adjudication,
        confirm_trigger,
        drive_initial_publication_to_waiting_for_bot,
        freeze_threads,
    )

    from ai_dev_loop.pr_review_v2.domain import (
        AdjudicationRecordedOutcome,
        LocalFixFinishedOutcome,
        LocalFixOutcomeKind,
    )

    prepared = PreparedState(
        run_id="run-fix",
        origin=_safe_origin(head_branch="fix/branch", base_branch="main"),
        limits=LIMITS,
        entered_at=T0,
    )
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared)
    assert binding.head_branch == "fix/branch"
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("fix-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(state.frozen)),
    )
    state, effects = succeed(
        state,
        effects[0],
        LocalFixFinishedOutcome(
            outcome=LocalFixOutcomeKind.ACCEPTED,
            accepted_patch_ref=artifact("artifacts/fix.patch"),
            new_head_sha="f" * 40,
            result_ref=artifact("artifacts/local-result.json"),
        ),
    )
    assert state.kind == "publishing_fix"
    assert state.binding.head_branch == "fix/branch"
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0]
    assert isinstance(commit, CommitPatchEffect)
    assert commit.expected_branch == "fix/branch"
    CommitPatchEffect.model_validate(commit.model_dump(mode="json"))


def test_safe_origin_and_binding_serialize_round_trip() -> None:
    origin = _safe_origin(head_branch="release/1.0", base_branch="develop")
    binding = _safe_binding(head_branch="release/1.0", base_branch="develop")
    assert SourceRunOrigin.model_validate(origin.model_dump(mode="json")) == origin
    assert PullRequestBinding.model_validate(binding.model_dump(mode="json")) == binding
    existing = ExistingPrOrigin(
        binding=binding,
        execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
    )
    assert ExistingPrOrigin.model_validate(existing.model_dump(mode="json")) == existing


def test_complete_claim_after_publication_does_not_raise_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-valid source origin → publication success → commit claim without ValidationError."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="br"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = PreparedState(
        run_id="run-1",
        origin=_safe_origin(head_branch="feature", base_branch="main"),
        limits=LIMITS,
        entered_at=clock.now(),
    )
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    local = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert local is not None
    # Must not raise ValidationError when reducer emits CommitPatchEffect.
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="pub",
            dispatch_id=local.dispatch_id,
            claim_id=local.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(local.effect, local.completion_token, clock.now()),
        )
    )
    nxt = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert nxt is not None
    assert nxt.effect.kind == "commit_patch"
    assert isinstance(nxt.effect, CommitPatchEffect)
    assert nxt.effect.expected_branch == "feature"
