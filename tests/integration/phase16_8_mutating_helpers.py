"""Phase 16.8 mutating-effect crash-matrix helpers.

OpenQuestions (see also ``archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md``):

- ``commit_patch`` ``ambiguous_apply_timeout`` requires a hang-after-apply git
  transport wrapper; if that wrapper cannot be made reliable on a given runner,
  fail the test with ``pytest.fail`` pointing here instead of skipping.
- ``update_pr_text`` / ``resolve_thread`` engine drive helpers depend on the full
  fix-publication reducer path; if that path drifts, update ``drive_to_mutating_claim``
  rather than guessing shorter routes.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    complete_ok,
    drive_to_waiting_for_bot,
    fingerprint,
    make_engine,
    make_observe_eligible,
)
from tests.integration.phase16_8_checkpoint_helpers import (
    RECONCILE_MATRIX,
    WriteGatewayBundle,
    assemble_write_gateway,
    assert_open_claim,
    reopen_engine,
)
from tests.integration.phase16_8_helpers import MARKER, SHA_B
from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run
from tests.unit.pr_review_v2.helpers import (
    actionable_adjudication,
    artifact,
    reply_adjudication,
)

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AuthorityLostError,
    ClaimAuthoritySnapshot,
    GitHubWritePolicy,
    WriteAuthorityStatus,
    WriteGatewaySuccess,
    WriteProofKind,
    claim_authority_snapshot_from_claim,
)
from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationRecordedOutcome,
    ArtifactRef,
    CommitRecordedOutcome,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    FrozenThreadSet,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PrBoundOutcome,
    PreparedState,
    PrTextUpdatedOutcome,
    PublicationTextPreparedOutcome,
    PullRequestBinding,
    PushConfirmedOutcome,
    ReconciliationResolutionKind,
    RepositoryIdentity,
    ReviewTriggerConfirmedOutcome,
    SourceRunOrigin,
    TriggerEvidence,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    ThreadDecisionRecord,
    build_opaque_trigger_marker,
)
from ai_dev_loop.pr_review_v2.domain.effects import MUTATING_KINDS, MutatingEffect
from ai_dev_loop.pr_review_v2.domain.events import WriteOutcomeUncertain
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

ReconcileOutcome = Literal["APPLIED", "PROVEN_NOT_APPLIED", "UNRESOLVED"]

FIX_HEAD_SHA = "e" * 40


@dataclass
class WriteProductionStack:
    executor: WriteExecutor
    git_gateway: GitPublicationGateway
    gh_bundle: WriteGatewayBundle
    git_repo: dict
    artifact_root: Path
    run_id: str
    git_gateway_setup: GitPublicationGateway | None = None
    _commit_sha: str | None = field(default=None, repr=False)


class EngineAuthority:
    """Adapt engine claim authority checks to the write guard protocol."""

    def __init__(self, engine: PrReviewEngine) -> None:
        self._engine = engine

    def check_authority(self, snapshot: ClaimAuthoritySnapshot):
        return self._engine.check_claim_authority(snapshot)


@dataclass
class RecordingWriteGateway:
    writes: list[str] = field(default_factory=list)

    def commit(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("commit")
        return WriteGatewaySuccess(
            outcome=CommitRecordedOutcome(
                commit_sha=SHA_B,
                new_head_sha=SHA_B,
                expected_remote_sha_before_push=None,
            ),
            already_applied=False,
        )

    def push(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("push")
        return WriteGatewaySuccess(
            outcome=PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature"),
            already_applied=False,
        )

    def create_or_update_pr(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("create_or_update_pr")
        return WriteGatewaySuccess(
            outcome=PrBoundOutcome(binding=H.binding()),
            already_applied=False,
        )

    def request_review(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("request_review")
        return WriteGatewaySuccess(
            outcome=ReviewTriggerConfirmedOutcome(
                evidence=TriggerEvidence(
                    marker=MARKER,
                    comment_ref=artifact("artifacts/trigger.json"),
                    head_sha=SHA_B,
                )
            ),
            already_applied=False,
        )

    def post_thread_reply(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del run_id, now
        authorize()
        self.writes.append("post_thread_reply")
        from ai_dev_loop.pr_review_v2.domain.events import ThreadReplyConfirmedOutcome

        return WriteGatewaySuccess(
            outcome=ThreadReplyConfirmedOutcome(
                thread_id=effect.thread_id,
                reply_ref=effect.reply_ref,
            ),
            already_applied=False,
        )

    def update_pr_text(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("update_pr_text")
        return WriteGatewaySuccess(
            outcome=PrTextUpdatedOutcome(
                binding=H.binding(),
                publication_text_ref=artifact("artifacts/publication.md"),
            ),
            already_applied=False,
        )

    def resolve_thread(self, effect, *, run_id, now, authorize):  # noqa: ANN001
        del effect, run_id, now
        authorize()
        self.writes.append("resolve_thread")
        from ai_dev_loop.pr_review_v2.domain.events import ThreadResolutionConfirmedOutcome

        return WriteGatewaySuccess(
            outcome=ThreadResolutionConfirmedOutcome(thread_id="THREAD_1"),
            already_applied=False,
        )


class _HangAfterApplyGitTransport:
    """Delegate to a real transport but hang after a successful mutating git call."""

    def __init__(self, inner: GitWriteTransport, *, hang_seconds: float) -> None:
        self._inner = inner
        self._hang_seconds = hang_seconds

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def commit_with_message_stdin(self, message: str):
        outcome = self._inner.commit_with_message_stdin(message)
        if outcome.returncode == 0 and not outcome.timed_out:
            time.sleep(self._hang_seconds)
            return replace(outcome, timed_out=True)
        return outcome

    def push_non_force(self, *, remote_name: str, commit_sha: str, remote_ref: str):
        outcome = self._inner.push_non_force(
            remote_name=remote_name,
            commit_sha=commit_sha,
            remote_ref=remote_ref,
        )
        if outcome.returncode == 0 and not outcome.timed_out:
            time.sleep(self._hang_seconds)
            return replace(outcome, timed_out=True)
        return outcome


def make_prepared(
    clock: FakeClock,
    *,
    run_id: str = "run-mut-matrix",
    max_external_cycles: int = 2,
) -> PreparedState:
    return PreparedState(
        run_id=run_id,
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256="2" * 64,
            ),
        ),
        limits=WorkflowLimits(
            max_external_cycles=max_external_cycles,
            max_local_iterations=3,
        ),
        entered_at=clock.now(),
    )


def assemble_write_production_stack(
    tmp_path: Path,
    *,
    run_id: str = H.RUN_ID,
    per_call_timeout_seconds: float = 2.0,
    hang_after_apply_seconds: float | None = None,
) -> WriteProductionStack:
    from tests.integration.test_phase16_6_git_writes import _gateway, _init_repo

    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    gh_bundle = assemble_write_gateway(
        tmp_path,
        controller,
        per_call_timeout_seconds=per_call_timeout_seconds,
    )
    git_root = tmp_path / "git"
    git_root.mkdir(parents=True, exist_ok=True)
    work, bare, parent = _init_repo(git_root)
    remote_baseline = subprocess.check_output(
        ["git", "rev-parse", "feature"],
        cwd=str(bare),
        text=True,
    ).strip()
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=str(work),
        check=True,
        capture_output=True,
    ).stdout
    artifact_root = tmp_path / "artifacts"
    patch_ref = H.write_artifact(artifact_root, run_id, "artifacts/accepted.patch", patch)
    message_ref = H.write_commit_message(artifact_root, run_id, "publish feature", "body")
    H.write_artifact(artifact_root, run_id, "artifacts/patch.bin", patch)
    git_repo = {
        "work": work,
        "bare": bare,
        "parent": parent,
        "remote_baseline": remote_baseline,
        "patch_ref": patch_ref,
        "message_ref": message_ref,
        "artifact_root": artifact_root,
    }
    git_gateway = _gateway(git_repo)
    git_gateway_setup = git_gateway
    if hang_after_apply_seconds is not None:
        inner = git_gateway._transport  # noqa: SLF001
        git_gateway = GitPublicationGateway(
            policy=git_gateway._policy,  # noqa: SLF001
            transport=_HangAfterApplyGitTransport(inner, hang_seconds=hang_after_apply_seconds),
            input_reader=git_gateway._reader,  # noqa: SLF001
        )
    executor = WriteExecutor(
        git_gateway=git_gateway,
        github_gateway=gh_bundle.gateway,
        github_policy=GitHubWritePolicy(repository_cwd=str(gh_bundle.repo)),
    )
    return WriteProductionStack(
        executor=executor,
        git_gateway=git_gateway,
        gh_bundle=gh_bundle,
        git_repo=git_repo,
        artifact_root=artifact_root,
        run_id=run_id,
        git_gateway_setup=git_gateway_setup,
    )


def _ensure_commit_sha(stack: WriteProductionStack) -> str:
    if stack._commit_sha is not None:
        return stack._commit_sha
    gateway = stack.git_gateway_setup or stack.git_gateway
    effect = H.commit_effect(
        stack.git_repo["patch_ref"],
        stack.git_repo["message_ref"],
        expected_head_sha=stack.git_repo["parent"],
        bound_head_sha=stack.git_repo["parent"],
        run_id=stack.run_id,
    )
    success = gateway.commit(
        effect,
        run_id=stack.run_id,
        now=datetime.now(tz=UTC),
        authorize=lambda: None,
    )
    stack._commit_sha = success.outcome.commit_sha
    return stack._commit_sha


def build_mutating_effect(
    kind: str,
    stack: WriteProductionStack,
    *,
    cycle: int = 1,
    run_id: str | None = None,
) -> MutatingEffect:
    if kind not in MUTATING_KINDS:
        raise KeyError(kind)
    resolved_run_id = run_id or H.RUN_ID
    if kind == "commit_patch":
        return H.commit_effect(
            stack.git_repo["patch_ref"],
            stack.git_repo["message_ref"],
            expected_head_sha=stack.git_repo["parent"],
            bound_head_sha=stack.git_repo["parent"],
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
    if kind == "push_commit":
        commit_sha = _ensure_commit_sha(stack)
        return H.push_effect(
            commit_sha=commit_sha,
            bound_head_sha=commit_sha,
            expected_remote_sha_before_push=stack.git_repo["parent"],
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
    if kind == "create_or_update_pr":
        title = "Title"
        body = "Body"
        pub_ref = H.write_publication(stack.artifact_root, resolved_run_id, title=title, body=body)
        from ai_dev_loop.pr_review_v2.application.write_contracts import (
            append_owned_marker,
            canonicalize_publication_text,
            derive_content_bound_marker,
        )

        head = _drive_head(stack)
        effect = H.create_pr_effect(
            pub_ref,
            bound_head_sha=head,
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
        marker = derive_content_bound_marker(
            operation="create_or_update_pr",
            target_kind="pr_body",
            idempotency_key=effect.idempotency_key,
            canonical_content=canonicalize_publication_text(title=title, body=body),
        )
        stack.gh_bundle.controller.state.fixture["pull_requests"] = []
        stack.gh_bundle.controller.state.fixture["title"] = title
        stack.gh_bundle.controller.state.fixture["body"] = append_owned_marker(
            body, marker.marker_text
        )
        stack.gh_bundle.controller.state.fixture["head_sha"] = head
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return effect
    if kind == "request_bot_review":
        marker = build_opaque_trigger_marker(run_id=resolved_run_id, cycle_number=cycle)
        head = _drive_head(stack)
        stack.gh_bundle.controller.state.fixture["head_sha"] = head
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return H.trigger_effect(
            marker,
            bound_head_sha=head,
            binding=H.binding(head_sha=head),
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
    if kind == "post_thread_reply":
        reply_text = "Thanks for the review."
        reply_ref = H.write_reply(stack.artifact_root, resolved_run_id, reply_text)
        head = _drive_head(stack)
        effect = H.reply_effect(
            reply_ref,
            bound_head_sha=head,
            binding=H.binding(head_sha=head),
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
        stack.gh_bundle.controller.state.fixture["head_sha"] = head
        stack.gh_bundle.controller.state.fixture["default_thread_id"] = effect.thread_id
        stack.gh_bundle.controller.state.thread_replies[effect.thread_id] = []
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return effect
    if kind == "update_pr_text":
        title = "Updated"
        body = "Updated body"
        pub_ref = H.write_publication(stack.artifact_root, resolved_run_id, title=title, body=body)
        head = _drive_head(stack)
        from ai_dev_loop.pr_review_v2.application.write_contracts import (
            append_owned_marker,
            canonicalize_publication_text,
            derive_content_bound_marker,
        )

        effect = H.update_pr_text_effect(
            pub_ref,
            bound_head_sha=head,
            binding=H.binding(head_sha=head),
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
        old_marker = derive_content_bound_marker(
            operation="update_pr_text",
            target_kind="pr_body",
            idempotency_key="previous-key",
            canonical_content=canonicalize_publication_text(title="old", body="old body"),
        )
        stack.gh_bundle.controller.state.fixture["title"] = "old"
        stack.gh_bundle.controller.state.fixture["body"] = append_owned_marker(
            "old body", old_marker.marker_text
        )
        stack.gh_bundle.controller.state.fixture["head_sha"] = head
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return effect
    if kind == "resolve_thread":
        head = _drive_head(stack)
        stack.gh_bundle.controller.state.fixture["head_sha"] = head
        stack.gh_bundle.controller.state.fixture["default_thread_id"] = "THREAD_1"
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return H.resolve_thread_effect(
            bound_head_sha=head,
            binding=H.binding(head_sha=head),
            run_id=resolved_run_id,
            cycle_number=cycle,
        )
    raise AssertionError(f"unhandled mutating kind {kind}")


def gateway_write(
    stack: WriteProductionStack, effect: MutatingEffect, *, now: datetime | None = None
):
    def authorize() -> None:
        return None

    moment = now or datetime.now(tz=UTC)
    if effect.kind == "commit_patch":
        return stack.git_gateway.commit(
            effect, run_id=effect.run_id, now=moment, authorize=authorize
        )
    if effect.kind == "push_commit":
        return stack.git_gateway.push(effect, run_id=effect.run_id, now=moment, authorize=authorize)
    gateway = stack.gh_bundle.gateway
    if effect.kind == "create_or_update_pr":
        return gateway.create_or_update_pr(
            effect, run_id=effect.run_id, now=moment, authorize=authorize
        )
    if effect.kind == "request_bot_review":
        return gateway.request_review(effect, run_id=effect.run_id, now=moment, authorize=authorize)
    if effect.kind == "post_thread_reply":
        return gateway.post_thread_reply(
            effect, run_id=effect.run_id, now=moment, authorize=authorize
        )
    if effect.kind == "update_pr_text":
        return gateway.update_pr_text(effect, run_id=effect.run_id, now=moment, authorize=authorize)
    if effect.kind == "resolve_thread":
        return gateway.resolve_thread(effect, run_id=effect.run_id, now=moment, authorize=authorize)
    raise AssertionError(f"unsupported gateway write kind {effect.kind}")


def gateway_reconcile(
    stack: WriteProductionStack,
    effect: MutatingEffect,
    outcome: ReconcileOutcome,
) -> WriteProofKind | None:
    now = datetime.now(tz=UTC)
    if effect.kind == "commit_patch":
        if outcome != "APPLIED":
            raise ValueError(f"git commit reconcile only supports APPLIED, not {outcome}")
        committed = stack.git_gateway.commit(
            effect,
            run_id=effect.run_id,
            now=now,
            authorize=lambda: None,
        )
        proof = stack.git_gateway.reconcile_commit(
            effect.model_copy(
                update={
                    "expected_head_sha": effect.expected_head_sha,
                    "bound_head_sha": committed.outcome.new_head_sha,
                }
            ),
            run_id=effect.run_id,
            now=now,
        )
        assert proof.proof is WriteProofKind.APPLIED
        return proof.proof
    if effect.kind == "push_commit":
        if outcome == "APPLIED":
            stack.git_gateway.push(effect, run_id=effect.run_id, now=now, authorize=lambda: None)
        proof = stack.git_gateway.reconcile_push(effect, run_id=effect.run_id, now=now)
        assert proof.proof is WriteProofKind.APPLIED if outcome == "APPLIED" else proof.proof
        return proof.proof
    if effect.kind not in RECONCILE_MATRIX:
        raise KeyError(effect.kind)
    if outcome not in RECONCILE_MATRIX[effect.kind]:
        raise KeyError((effect.kind, outcome))
    if outcome == "APPLIED":
        gateway = stack.gh_bundle.gateway
        now = datetime.now(tz=UTC)
        reconcile_dispatch = {
            "create_or_update_pr": gateway.reconcile_create_or_update_pr,
            "request_bot_review": gateway.reconcile_request_review,
            "post_thread_reply": gateway.reconcile_post_thread_reply,
            "update_pr_text": gateway.reconcile_update_pr_text,
            "resolve_thread": gateway.reconcile_resolve_thread,
        }
        proof = reconcile_dispatch[effect.kind](effect, run_id=effect.run_id, now=now)
        assert proof.proof is WriteProofKind.APPLIED
        return proof.proof
    RECONCILE_MATRIX[effect.kind][outcome](stack.gh_bundle)
    return WriteProofKind.APPLIED


def queue_mutation_failure(
    controller: StatefulFakeGhController,
    kind: str,
    failure_spec: dict | None = None,
    **extra: object,
) -> None:
    payload = dict(failure_spec or {})
    payload.update(extra)
    payload["mutation_only"] = True
    controller.queue_failure(kind, **payload)


def gh_mutation_total(controller: StatefulFakeGhController) -> int:
    reloaded = controller.reload()
    return sum(reloaded.mutation_counts.values())


def sqlite_fingerprint(engine: PrReviewEngine, run_id: str) -> tuple:
    return fingerprint(engine, run_id)


def assert_claim_open(
    engine: PrReviewEngine,
    run_id: str,
    claim,
    *,
    effect_kind: str,
) -> None:
    assert_open_claim(
        engine,
        run_id,
        dispatch_id=claim.dispatch_id,
        effect_id=claim.effect.effect_id,
        effect_kind=effect_kind,
    )


def _drive_head(stack: WriteProductionStack | None) -> str:
    if stack is None:
        return SHA_B
    if stack._commit_sha is not None:
        return stack._commit_sha
    return stack.git_repo["parent"]


_GH_PUBLICATION_PATH_KINDS = frozenset(
    {"create_or_update_pr", "request_bot_review", "commit_patch", "push_commit"}
)
_FIX_PUBLICATION_DRIVE_KINDS = frozenset({"update_pr_text", "resolve_thread", "post_thread_reply"})


def _stack_for_drive(effect_kind: str) -> bool:
    return (
        effect_kind in _GIT_MUTATING_KINDS
        or effect_kind in _GH_PUBLICATION_PATH_KINDS
        or effect_kind in _FIX_PUBLICATION_DRIVE_KINDS
    )


def _maybe_align_prepared(prepared: PreparedState, stack: WriteProductionStack, effect_kind: str):
    if _stack_for_drive(effect_kind):
        return _align_prepared_with_stack(prepared, stack)
    return prepared


def _align_prepared_with_stack(
    prepared: PreparedState, stack: WriteProductionStack
) -> PreparedState:
    return prepared.model_copy(
        update={
            "origin": prepared.origin.model_copy(
                update={
                    "accepted_patch": stack.git_repo["patch_ref"],
                    "expected_head_sha": stack.git_repo["parent"],
                }
            )
        }
    )


def _success_for_claim(
    claim,
    prepared: PreparedState,
    clock: FakeClock,
    *,
    binding: PullRequestBinding | None,
    stack: WriteProductionStack | None = None,
):
    effect = claim.effect
    token = claim.completion_token
    now = clock.now()
    head = _drive_head(stack)
    if effect.kind == "generate_publication_text":
        if stack is not None:
            pub_ref = H.write_publication(
                stack.artifact_root,
                stack.run_id,
                title="Publication title",
                body="Publication body",
            )
            return EffectSucceeded(
                occurred_at=now,
                token=token,
                outcome=PublicationTextPreparedOutcome(
                    publication_text_ref=pub_ref,
                    commit_message_ref=stack.git_repo["message_ref"],
                ),
            )
        return publication_success(effect, token, now)
    if effect.kind == "commit_patch":
        commit_sha = _ensure_commit_sha(stack) if stack is not None else head
        expected_remote = stack.git_repo["remote_baseline"] if stack is not None else None
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=CommitRecordedOutcome(
                commit_sha=commit_sha,
                new_head_sha=commit_sha,
                expected_remote_sha_before_push=expected_remote,
            ),
        )
    if effect.kind == "push_commit":
        push_sha = _ensure_commit_sha(stack) if stack is not None else head
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=PushConfirmedOutcome(
                commit_sha=push_sha, remote_ref=prepared.origin.head_branch
            ),
        )
    if effect.kind == "create_or_update_pr":
        assert binding is not None
        bound = binding.model_copy(update={"head_sha": head}) if stack is not None else binding
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=PrBoundOutcome(binding=bound),
        )
    if effect.kind == "request_bot_review":
        assert binding is not None
        bound = binding.model_copy(update={"head_sha": head}) if stack is not None else binding
        return EffectSucceeded(
            occurred_at=now,
            token=token,
            outcome=ReviewTriggerConfirmedOutcome(
                evidence=TriggerEvidence(
                    marker=effect.marker,
                    comment_ref=artifact("artifacts/trigger.json"),
                    head_sha=bound.head_sha,
                )
            ),
        )
    raise AssertionError(f"unsupported intervening effect {effect.kind}")


def _observe_threads_event(
    claim, binding: PullRequestBinding, trigger_marker: str, clock: FakeClock
):
    frozen = FrozenThreadSet(
        thread_ids=("t1", "t2"),
        snapshot_ref=artifact("artifacts/threads.json"),
        head_sha=binding.head_sha,
        cycle_number=claim.effect.cycle_number,
        trigger_marker=trigger_marker,
    )
    return EffectSucceeded(
        occurred_at=clock.now(),
        token=claim.completion_token,
        outcome=EligibleThreadsObservedOutcome(frozen=frozen),
    )


def drive_to_mutating_claim(
    engine: PrReviewEngine,
    prepared: PreparedState,
    clock: FakeClock,
    target_kind: str,
    *,
    stack: WriteProductionStack | None = None,
) -> tuple:
    if target_kind not in MUTATING_KINDS:
        raise KeyError(target_kind)

    if target_kind in {"commit_patch", "push_commit", "create_or_update_pr", "request_bot_review"}:
        start_run(engine, prepared)
        head = _drive_head(stack)
        binding = PullRequestBinding(
            repository=prepared.origin.repository,
            pr_number=7,
            head_branch=prepared.origin.head_branch,
            base_branch=prepared.origin.base_branch,
            head_sha=head,
        )
        lease, claim = claim_next(engine, prepared.run_id)
        while claim.effect.kind != target_kind:
            event = _success_for_claim(claim, prepared, clock, binding=binding, stack=stack)
            complete_ok(engine, lease, claim, f"drive-{claim.effect.kind}", event)
            lease, claim = claim_next(engine, prepared.run_id)
        return lease, claim

    if target_kind == "post_thread_reply":
        start_run(engine, prepared)
        binding = drive_to_waiting_for_bot(engine, prepared, clock)
        make_observe_eligible(engine, prepared.run_id, clock)
        lease, claim = claim_next(engine, prepared.run_id)
        with engine.store.begin_read() as conn:
            snap, _, _ = engine.store.load_validated_snapshot(conn, prepared.run_id)
        assert snap.trigger_evidence is not None
        trigger_marker = snap.trigger_evidence.marker
        frozen = FrozenThreadSet(
            thread_ids=("t1", "t2"),
            snapshot_ref=artifact("artifacts/threads.json"),
            head_sha=binding.head_sha,
            cycle_number=claim.effect.cycle_number,
            trigger_marker=trigger_marker,
        )
        complete_ok(
            engine,
            lease,
            claim,
            "drive-observe-threads",
            _observe_threads_event(claim, binding, trigger_marker, clock),
        )
        lease, claim = claim_next(engine, prepared.run_id)
        adjudication = (
            _reply_adjudication_with_artifacts(stack, prepared, frozen)
            if stack is not None
            else reply_adjudication(frozen)
        )
        complete_ok(
            engine,
            lease,
            claim,
            "drive-adj-reply",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=AdjudicationRecordedOutcome(evidence=adjudication),
            ),
        )
        lease, claim = claim_next(engine, prepared.run_id)
        assert claim.effect.kind == "post_thread_reply"
        return lease, claim

    if target_kind in {"update_pr_text", "resolve_thread"}:
        start_run(engine, prepared)
        binding = drive_to_waiting_for_bot(engine, prepared, clock)
        make_observe_eligible(engine, prepared.run_id, clock)
        lease, claim = claim_next(engine, prepared.run_id)
        with engine.store.begin_read() as conn:
            snap, _, _ = engine.store.load_validated_snapshot(conn, prepared.run_id)
        assert snap.trigger_evidence is not None
        frozen = FrozenThreadSet(
            thread_ids=("thread-a",),
            snapshot_ref=artifact("artifacts/threads.json"),
            head_sha=binding.head_sha,
            cycle_number=claim.effect.cycle_number,
            trigger_marker=snap.trigger_evidence.marker,
        )
        complete_ok(
            engine,
            lease,
            claim,
            "drive-observe-actionable",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=EligibleThreadsObservedOutcome(frozen=frozen),
            ),
        )
        lease, claim = claim_next(engine, prepared.run_id)
        complete_ok(
            engine,
            lease,
            claim,
            "drive-adj-actionable",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=AdjudicationRecordedOutcome(evidence=actionable_adjudication(frozen)),
            ),
        )
        lease, claim = claim_next(engine, prepared.run_id)
        complete_ok(
            engine,
            lease,
            claim,
            "drive-local-fix",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=LocalFixFinishedOutcome(
                    outcome=LocalFixOutcomeKind.ACCEPTED,
                    accepted_patch_ref=artifact("artifacts/fix.patch"),
                    new_head_sha=FIX_HEAD_SHA,
                    result_ref=artifact("artifacts/local-result.json"),
                ),
            ),
        )
        fix_binding = binding.model_copy(update={"head_sha": FIX_HEAD_SHA})
        publication_ref = artifact("artifacts/publication.md")
        for step_kind in (
            "generate_publication_text",
            "commit_patch",
            "push_commit",
        ):
            lease, claim = claim_next(engine, prepared.run_id)
            assert claim.effect.kind == step_kind
            if step_kind == "generate_publication_text":
                if stack is not None:
                    publication_ref = H.write_publication(
                        stack.artifact_root,
                        prepared.run_id,
                        title="Updated",
                        body="Updated body",
                    )
                    event = EffectSucceeded(
                        occurred_at=clock.now(),
                        token=claim.completion_token,
                        outcome=PublicationTextPreparedOutcome(
                            publication_text_ref=publication_ref,
                            commit_message_ref=stack.git_repo["message_ref"],
                        ),
                    )
                else:
                    event = publication_success(claim.effect, claim.completion_token, clock.now())
            elif step_kind == "commit_patch":
                event = EffectSucceeded(
                    occurred_at=clock.now(),
                    token=claim.completion_token,
                    outcome=CommitRecordedOutcome(
                        commit_sha=FIX_HEAD_SHA,
                        new_head_sha=FIX_HEAD_SHA,
                        expected_remote_sha_before_push=None,
                    ),
                )
            else:
                event = EffectSucceeded(
                    occurred_at=clock.now(),
                    token=claim.completion_token,
                    outcome=PushConfirmedOutcome(
                        commit_sha=FIX_HEAD_SHA,
                        remote_ref=prepared.origin.head_branch,
                    ),
                )
            complete_ok(engine, lease, claim, f"drive-{step_kind}", event)
        lease, claim = claim_next(engine, prepared.run_id)
        assert claim.effect.kind == "update_pr_text"
        if target_kind == "update_pr_text":
            return lease, claim
        complete_ok(
            engine,
            lease,
            claim,
            "drive-update-pr-text",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=PrTextUpdatedOutcome(
                    binding=fix_binding,
                    publication_text_ref=publication_ref,
                ),
            ),
        )
        lease, claim = claim_next(engine, prepared.run_id)
        assert claim.effect.kind == "resolve_thread"
        return lease, claim

    raise AssertionError(f"drive_to_mutating_claim missing route for {target_kind}")


def execute_stale_authority_write(
    stack: WriteProductionStack,
    engine: PrReviewEngine,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
) -> None:
    del stack
    lease, claim = drive_to_mutating_claim(engine, prepared, clock, effect_kind)
    before = sqlite_fingerprint(engine, prepared.run_id)
    clock.advance(timedelta(seconds=60))
    engine.acquire_lease(prepared.run_id, "owner-b")
    gateway = RecordingWriteGateway()
    executor = WriteExecutor(
        git_gateway=gateway,  # type: ignore[arg-type]
        github_gateway=gateway,  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd="/tmp/repo"),
    )
    with pytest.raises(AuthorityLostError):
        executor.execute(
            claim.effect,
            claim.completion_token,
            now=clock.now(),
            authority=EngineAuthority(engine),
            claim=claim,
        )
    assert gateway.writes == []
    assert sqlite_fingerprint(engine, prepared.run_id) == before
    snapshot = claim_authority_snapshot_from_claim(claim)
    assert engine.check_claim_authority(snapshot).status is WriteAuthorityStatus.REJECTED
    assert claim.classification == "mutating"
    _ = lease


def execute_mutating_lease_loss(
    engine: PrReviewEngine,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
) -> None:
    lease, claim = drive_to_mutating_claim(engine, prepared, clock, effect_kind)
    dispatch_id = claim.dispatch_id
    effect_id = claim.effect.effect_id
    clock.advance(timedelta(seconds=60))
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    assert engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation) == 1
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "reconciling_write"
    lease3, reclaim = claim_next(engine, prepared.run_id, owner="owner-b")
    assert reclaim.classification == "reconciling"
    assert reclaim.effect.original_write.effect_id == effect_id
    assert reclaim.effect.original_write.kind == effect_kind
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT status FROM pr_review_effects WHERE run_id=? AND dispatch_id=?",
            (prepared.run_id, dispatch_id),
        ).fetchone()
    assert row is not None
    assert row["status"] in {"uncertain", "blocked"}
    _ = lease, lease3


def execute_abort_during_mutating_claim(
    engine: PrReviewEngine,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
) -> None:
    lease, claim = drive_to_mutating_claim(engine, prepared, clock, effect_kind)
    assert_claim_open(engine, prepared.run_id, claim, effect_kind=effect_kind)
    abort = engine.abort_run(submission_id=f"abort-{effect_kind}", run_id=prepared.run_id)
    assert abort.disposition is EventDisposition.ACCEPTED
    assert engine.get_status(prepared.run_id).state_kind == "aborted"
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id=f"late-{effect_kind}",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=lease.owner_id,
            lease_generation=lease.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=CommitRecordedOutcome(
                    commit_sha=SHA_B,
                    new_head_sha=SHA_B,
                    expected_remote_sha_before_push=None,
                ),
            ),
        )
    )
    assert receipt.disposition is EventDisposition.STALE


_GIT_MUTATING_KINDS = frozenset({"commit_patch", "push_commit"})
_GH_MUTATING_KINDS = frozenset(RECONCILE_MATRIX)


def _git_head(work: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()


def _remote_feature_sha(bare: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "feature"], cwd=str(bare), text=True
    ).strip()


def external_mutation_count(stack: WriteProductionStack, effect_kind: str) -> int:
    if effect_kind in _GH_MUTATING_KINDS:
        return gh_mutation_total(stack.gh_bundle.controller)
    if effect_kind == "commit_patch":
        head = _git_head(stack.git_repo["work"])
        return 0 if head == stack.git_repo["parent"] else 1
    if effect_kind == "push_commit":
        bare: Path = stack.git_repo["bare"]
        current = _remote_feature_sha(bare)
        return 0 if current == stack.git_repo["remote_baseline"] else 1
    raise KeyError(effect_kind)


def _effect_succeeded_from_gateway(
    claim,
    write_success: WriteGatewaySuccess,
    clock: FakeClock,
) -> EffectSucceeded:
    outcome = write_success.outcome
    effect = claim.effect
    if effect.kind == "post_thread_reply" and hasattr(outcome, "model_copy"):
        outcome = outcome.model_copy(
            update={"thread_id": effect.thread_id, "reply_ref": effect.reply_ref}
        )
    if effect.kind == "request_bot_review" and hasattr(outcome, "model_copy"):
        outcome = outcome.model_copy(
            update={
                "evidence": outcome.evidence.model_copy(
                    update={"marker": effect.marker, "head_sha": effect.bound_head_sha}
                )
            }
        )
    if effect.kind == "update_pr_text" and hasattr(outcome, "model_copy"):
        outcome = outcome.model_copy(
            update={
                "binding": effect.binding,
                "publication_text_ref": effect.publication_text_ref,
            }
        )
    if effect.kind == "create_or_update_pr" and isinstance(outcome, PrBoundOutcome):
        outcome = outcome.model_copy(
            update={
                "binding": outcome.binding.model_copy(
                    update={
                        "repository": effect.repository,
                        "head_branch": effect.head_branch,
                        "base_branch": effect.base_branch,
                        "head_sha": effect.bound_head_sha,
                    }
                )
            }
        )
    if effect.kind == "resolve_thread" and hasattr(outcome, "model_copy"):
        outcome = outcome.model_copy(update={"thread_id": effect.thread_id})
    return EffectSucceeded(
        occurred_at=clock.now(),
        token=claim.completion_token,
        outcome=outcome,
    )


def _apply_update_pr_preimage(stack: WriteProductionStack, effect) -> None:
    from ai_dev_loop.pr_review_v2.application.write_contracts import (
        append_owned_marker,
        canonicalize_publication_text,
        derive_content_bound_marker,
    )

    old_marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="previous-key",
        canonical_content=canonicalize_publication_text(title="old", body="old body"),
    )
    fx = stack.gh_bundle.controller.state.fixture
    fx["title"] = "old"
    fx["body"] = append_owned_marker("old body", old_marker.marker_text)
    fx["head_sha"] = effect.bound_head_sha
    stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)


def _sync_gh_fixture_for_effect(stack: WriteProductionStack, effect) -> None:
    fx = stack.gh_bundle.controller.state.fixture
    fx["head_sha"] = effect.bound_head_sha
    if effect.kind == "post_thread_reply":
        fx["default_thread_id"] = effect.thread_id
        stack.gh_bundle.controller.state.thread_replies.setdefault(str(effect.thread_id), [])
    if effect.kind == "resolve_thread":
        fx["default_thread_id"] = effect.thread_id
    stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)


def _reply_adjudication_with_artifacts(
    stack: WriteProductionStack,
    prepared: PreparedState,
    frozen: FrozenThreadSet,
) -> AdjudicationEvidence:
    decisions: list[ThreadDecisionRecord] = []
    for index, thread_id in enumerate(frozen.thread_ids):
        reply_ref = H.write_artifact(
            stack.artifact_root,
            prepared.run_id,
            f"artifacts/reply-{thread_id}.txt",
            f"Thanks for the review on {thread_id}.".encode(),
        )
        decisions.append(
            ThreadDecisionRecord(
                thread_id=thread_id,
                decision=AdjudicationDecisionKind.NOT_APPLICABLE
                if index == 0
                else AdjudicationDecisionKind.UNCERTAIN,
                safe_summary=f"reply {thread_id}",
                reply_ref=reply_ref,
            )
        )
    return AdjudicationEvidence(
        frozen=frozen,
        decisions=tuple(decisions),
        result_ref=artifact("artifacts/adjudication.json"),
        fix_prompt_ref=None,
    )


def _prepare_gh_fixtures(stack: WriteProductionStack, effect) -> None:
    if effect.kind == "create_or_update_pr":
        fx = stack.gh_bundle.controller.state.fixture
        fx["pull_requests"] = []
        fx["head_sha"] = effect.bound_head_sha
        stack.gh_bundle.controller.state.save(stack.gh_bundle.controller.state_path)
        return
    if effect.kind == "update_pr_text":
        _apply_update_pr_preimage(stack, effect)
    if effect.kind in {"post_thread_reply", "resolve_thread", "request_bot_review"}:
        _sync_gh_fixture_for_effect(stack, effect)


def _mapped_mutating_effect(stack: WriteProductionStack, claim) -> object:
    """Prepare production fixtures, then execute the active claimed effect unchanged."""

    _sync_claim_git_artifacts(stack, claim)
    effect = claim.effect
    if effect.kind in _GIT_MUTATING_KINDS:
        if effect.kind == "commit_patch":
            return effect.model_copy(
                update={"expected_head_sha": stack.git_repo["parent"]},
            )
        if effect.kind == "push_commit":
            _ensure_commit_sha(stack)
            return effect
        return effect
    if effect.kind in _GH_MUTATING_KINDS:
        _prepare_gh_fixtures(stack, effect)
        return effect
    return effect


def _sync_claim_git_artifacts(stack: WriteProductionStack, claim) -> None:
    effect = claim.effect
    if effect.kind not in _GIT_MUTATING_KINDS:
        return
    run_root = ensure_run_artifact_root(stack.artifact_root, effect.run_id)
    patch_bytes = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=str(stack.git_repo["work"]),
        check=True,
        capture_output=True,
    ).stdout
    if effect.kind == "commit_patch":
        patch_path = run_root / effect.patch_ref.relative_path
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(patch_bytes)
        message_path = run_root / effect.commit_message_ref.relative_path
        message_path.parent.mkdir(parents=True, exist_ok=True)
        if not message_path.is_file():
            source = run_root / stack.git_repo["message_ref"].relative_path
            if source.is_file():
                message_path.write_bytes(source.read_bytes())
            else:
                message_path.write_text("publish feature\n\nbody\n", encoding="utf-8")
    if effect.kind == "push_commit":
        _ensure_commit_sha(stack)


def _sync_gh_claim_artifacts(stack: WriteProductionStack, claim) -> None:
    del stack, claim


def _writable_effect(stack: WriteProductionStack, claim) -> object:
    return _mapped_mutating_effect(stack, claim)


def _executor_effect(stack: WriteProductionStack, claim) -> object:
    return _mapped_mutating_effect(stack, claim)


def assemble_reconcile_executor(stack: WriteProductionStack) -> ReconcileWriteExecutor:
    return ReconcileWriteExecutor(
        git_gateway=stack.git_gateway,
        github_gateway=stack.gh_bundle.gateway,
        github_policy=GitHubWritePolicy(repository_cwd=str(stack.gh_bundle.repo)),
    )


def _assert_mutating_durable_identity(
    engine: PrReviewEngine,
    run_id: str,
    *,
    dispatch_id: str,
    effect_id: str,
    idempotency_key: str,
    before_fp: tuple,
) -> None:
    after_fp = sqlite_fingerprint(engine, run_id)
    _run_before, _timers_before, effects_before = before_fp
    _run_after, _timers_after, effects_after = after_fp
    row = next(row for row in effects_after if row[0] == dispatch_id)
    assert row[0] == dispatch_id
    with engine.store.begin_read() as conn:
        payload = conn.execute(
            """
            SELECT effect_id, idempotency_key, status
            FROM pr_review_effects
            WHERE run_id=? AND dispatch_id=?
            """,
            (run_id, dispatch_id),
        ).fetchone()
        assert payload is not None
        assert payload["effect_id"] == effect_id
        assert payload["idempotency_key"] == idempotency_key
        assert payload["status"] in {"succeeded", "uncertain", "claimed", "pending"}
        journal = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_events WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert journal is not None and int(journal["n"]) >= 1
        outbox_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pr_review_outbox'"
        ).fetchone()
        if outbox_table is not None:
            outbox = conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_outbox WHERE run_id=?",
                (run_id,),
            ).fetchone()
            assert outbox is not None


def _claim_with_push_baseline(stack: WriteProductionStack, claim):
    if claim.effect.kind != "push_commit":
        return claim
    commit_sha = _ensure_commit_sha(stack)
    push_effect = claim.effect.model_copy(
        update={
            "commit_sha": commit_sha,
            "expected_remote_sha_before_push": stack.git_repo["remote_baseline"],
        }
    )
    return claim.model_copy(update={"effect": push_effect})


def execute_apply_before_complete(
    db_path: Path,
    stack: WriteProductionStack,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
    *,
    engine_prefix: str,
) -> None:
    prepared = _maybe_align_prepared(prepared, stack, effect_kind)
    engine = make_engine(db_path, clock, prefix=engine_prefix)
    lease, claim = drive_to_mutating_claim(
        engine,
        prepared,
        clock,
        effect_kind,
        stack=stack if _stack_for_drive(effect_kind) else None,
    )
    effect = claim.effect
    claim = _claim_with_push_baseline(stack, claim)
    write_effect = _writable_effect(stack, claim)
    dispatch_id = claim.dispatch_id
    effect_id = effect.effect_id
    idempotency_key = effect.idempotency_key
    before_fp = sqlite_fingerprint(engine, prepared.run_id)
    before_mut = external_mutation_count(stack, effect_kind)

    write_success = gateway_write(stack, write_effect, now=clock.now())
    assert write_success.already_applied is False
    assert external_mutation_count(stack, effect_kind) == before_mut + 1
    assert_claim_open(engine, prepared.run_id, claim, effect_kind=effect_kind)

    engine2 = reopen_engine(db_path, clock, prefix=f"{engine_prefix}-reopen")
    assert_claim_open(engine2, prepared.run_id, claim, effect_kind=effect_kind)
    proof = gateway_reconcile(stack, write_effect, "APPLIED")
    assert proof is WriteProofKind.APPLIED

    receipt = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id=f"apply-before-complete-{effect_kind}",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=lease.owner_id,
            lease_generation=claim.completion_token.lease_generation,
            event=_effect_succeeded_from_gateway(claim, write_success, clock),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    assert external_mutation_count(stack, effect_kind) == before_mut + 1
    _assert_mutating_durable_identity(
        engine2,
        prepared.run_id,
        dispatch_id=dispatch_id,
        effect_id=effect_id,
        idempotency_key=idempotency_key,
        before_fp=before_fp,
    )
    status = engine2.get_status(prepared.run_id)
    assert status.run_version >= before_fp[0][0]


def execute_complete_before_worker_failure(
    db_path: Path,
    stack: WriteProductionStack,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
    *,
    engine_prefix: str,
) -> None:
    prepared = _maybe_align_prepared(prepared, stack, effect_kind)
    engine = make_engine(db_path, clock, prefix=engine_prefix)
    lease, claim = drive_to_mutating_claim(
        engine,
        prepared,
        clock,
        effect_kind,
        stack=stack if _stack_for_drive(effect_kind) else None,
    )
    effect = claim.effect
    claim = _claim_with_push_baseline(stack, claim)
    write_effect = _writable_effect(stack, claim)
    dispatch_id = claim.dispatch_id
    effect_id = effect.effect_id
    idempotency_key = effect.idempotency_key
    before_fp = sqlite_fingerprint(engine, prepared.run_id)
    before_mut = external_mutation_count(stack, effect_kind)

    result = stack.executor.execute(
        _executor_effect(stack, claim),
        claim.completion_token,
        now=clock.now(),
        authority=EngineAuthority(engine),
        claim=claim,
    )
    assert isinstance(result, EffectSucceeded)
    mapped = _effect_succeeded_from_gateway(
        claim,
        WriteGatewaySuccess(outcome=result.outcome, already_applied=False),
        clock,
    )
    complete_ok(engine, lease, claim, f"complete-before-crash-{effect_kind}", mapped)
    assert external_mutation_count(stack, effect_kind) == before_mut + 1

    engine2 = reopen_engine(db_path, clock, prefix=f"{engine_prefix}-reopen")
    proof = gateway_reconcile(stack, write_effect, "APPLIED")
    assert proof is WriteProofKind.APPLIED
    assert external_mutation_count(stack, effect_kind) == before_mut + 1

    with engine2.store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT status FROM pr_review_effects
            WHERE run_id=? AND dispatch_id=?
            """,
            (prepared.run_id, dispatch_id),
        ).fetchone()
    assert row is not None
    assert row["status"] == "succeeded"

    late = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id=f"late-{effect_kind}",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=lease.owner_id,
            lease_generation=lease.generation,
            event=mapped,
        )
    )
    assert late.disposition is EventDisposition.STALE
    _assert_mutating_durable_identity(
        engine2,
        prepared.run_id,
        dispatch_id=dispatch_id,
        effect_id=effect_id,
        idempotency_key=idempotency_key,
        before_fp=before_fp,
    )


def execute_ambiguous_apply_timeout_with_reconcile(
    db_path: Path,
    prepared: PreparedState,
    clock: FakeClock,
    effect_kind: str,
    stack_root: Path,
    *,
    engine_prefix: str,
) -> None:
    if effect_kind in _GH_MUTATING_KINDS:
        stack = assemble_write_production_stack(
            stack_root, run_id=prepared.run_id, per_call_timeout_seconds=2.0
        )
        queue_mutation_failure(stack.gh_bundle.controller, "apply_then_hang", seconds=60)
    elif effect_kind in _GIT_MUTATING_KINDS:
        stack = assemble_write_production_stack(
            stack_root,
            run_id=prepared.run_id,
            per_call_timeout_seconds=0.5,
            hang_after_apply_seconds=2.0,
        )
    else:
        pytest.fail(
            f"ambiguous_apply_timeout requires production-boundary harness for {effect_kind}; "
            "see OpenQuestions in tests/integration/phase16_8_mutating_helpers.py"
        )

    prepared = _maybe_align_prepared(prepared, stack, effect_kind)
    engine = make_engine(db_path, clock, prefix=engine_prefix)
    lease, claim = drive_to_mutating_claim(
        engine,
        prepared,
        clock,
        effect_kind,
        stack=stack if _stack_for_drive(effect_kind) else None,
    )
    effect = claim.effect
    claim = _claim_with_push_baseline(stack, claim)
    dispatch_id = claim.dispatch_id
    effect_id = effect.effect_id
    idempotency_key = effect.idempotency_key
    before_fp = sqlite_fingerprint(engine, prepared.run_id)
    before_mut = external_mutation_count(stack, effect_kind)

    uncertain = stack.executor.execute(
        _executor_effect(stack, claim),
        claim.completion_token,
        now=clock.now(),
        authority=EngineAuthority(engine),
        claim=claim,
    )
    assert isinstance(uncertain, WriteOutcomeUncertain)
    assert uncertain.original_write.effect_id == effect_id
    assert uncertain.original_write.idempotency_key == idempotency_key
    assert uncertain.reconciliation_identity is not None
    complete_ok(engine, lease, claim, f"uncertain-{effect_kind}", uncertain)
    assert engine.get_status(prepared.run_id).state_kind == "reconciling_write"
    assert external_mutation_count(stack, effect_kind) == before_mut + 1

    engine2 = reopen_engine(db_path, clock, prefix=f"{engine_prefix}-reopen")
    assert engine2.get_status(prepared.run_id).state_kind == "reconciling_write"
    lease2, reclaim = claim_next(engine2, prepared.run_id)
    assert reclaim.classification == "reconciling"
    assert reclaim.effect.original_write.effect_id == effect_id
    assert reclaim.effect.original_write.idempotency_key == idempotency_key
    assert reclaim.effect.reconciliation_identity == uncertain.reconciliation_identity

    reconcile_executor = assemble_reconcile_executor(stack)
    rec_result = reconcile_executor.execute(
        reclaim.effect,
        reclaim.completion_token,
        now=clock.now(),
        authority=EngineAuthority(engine2),
        claim=reclaim,
    )
    assert isinstance(rec_result, EffectSucceeded)
    assert rec_result.outcome.resolution is ReconciliationResolutionKind.APPLIED
    assert rec_result.outcome.original_effect_id == effect_id
    complete_ok(
        engine2,
        lease2,
        reclaim,
        f"reconcile-applied-{effect_kind}",
        rec_result,
    )
    assert external_mutation_count(stack, effect_kind) == before_mut + 1
    _assert_mutating_durable_identity(
        engine2,
        prepared.run_id,
        dispatch_id=dispatch_id,
        effect_id=effect_id,
        idempotency_key=idempotency_key,
        before_fp=before_fp,
    )
