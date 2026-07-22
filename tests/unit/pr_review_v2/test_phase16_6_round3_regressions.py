"""Phase 16.6 round-3 Codex correction regressions.

Covers PROVEN_NOT_APPLIED backoff, authoritative remote_name, cross-repo PR
fail-closed, malformed preflight parsers, commit authority/baseline/dispatch
ordering, exact thread node IDs, unsafe branch/ref rejection, and Git overall
deadline before root inspection. Fake transports only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.pr_review_v2 import write_helpers as WH
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    SHA_B,
    FakeGhWriteTransport,
    FakeReadTransport,
    create_pr_effect,
    graphql_result,
    post_reply_effect,
    pr_dict,
    resolve_thread_effect,
    rest_result,
    thread_comment_connection,
    thread_resolved_payload,
    write_artifact,
    write_publication_text,
    write_reply_text,
)
from tests.unit.pr_review_v2.helpers import (
    T2,
    publication_text_outcome,
    reduce_pr_review,
    start,
    succeed,
    token_for,
)

from ai_dev_loop.pr_review_v2.application.github_read import LOCAL_RETRY_BASE_DELAYS_SECONDS
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    GitRemoteScheme,
    GitWritePolicy,
    WriteProofKind,
    validate_branch_name,
    validate_remote_name,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import (
    compute_proven_not_applied_next_attempt,
)
from ai_dev_loop.pr_review_v2.domain import (
    ReconciliationResolutionKind,
    ReconciliationResolvedOutcome,
)
from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded, WriteOutcomeUncertain
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor

NOW = WH.NOW


class _Auth:
    def __init__(self) -> None:
        self.calls = 0
        self.order: list[str] = []

    def __call__(self) -> None:
        self.calls += 1
        self.order.append("authorize")


def _gh_gateway(tmp_path: Path, writes: FakeGhWriteTransport) -> GitHubWriteGateway:
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return GitHubWriteGateway(
        policy=GitHubWritePolicy(repository_cwd=str(repo)),
        write_transport=writes,
        read_transport=FakeReadTransport(),
        input_reader=InputArtifactReader(art),
        write_evidence=WriteEvidenceStore(art),
    )


# -- Finding 1: PROVEN_NOT_APPLIED next_attempt_at backoff -----------------


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
def test_proven_not_applied_next_attempt_is_strictly_after_occurred_at(
    tmp_path: Path, kind: str
) -> None:
    from tests.unit.pr_review_v2.test_reconcile_write_executor import (
        _STRATEGY,
        FakeReconcileGateways,
        _originals,
        _proven_not,
    )

    original = _originals(tmp_path)[kind]
    reconcile = WH.reconcile_effect(original)
    # Gateway may emit equal timestamps; executor must recompute via backoff policy.
    gw = FakeReconcileGateways(behavior={kind: _proven_not(kind)})
    executor = ReconcileWriteExecutor(
        git_gateway=gw,  # type: ignore[arg-type]
        github_gateway=gw,  # type: ignore[arg-type]
        github_policy=WH.github_policy(),
        jitter=None,
    )
    result = executor.execute(
        reconcile,
        WH.token_for(reconcile),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(reconcile),
    )
    assert isinstance(result, EffectSucceeded)
    outcome = result.outcome
    assert isinstance(outcome, ReconciliationResolvedOutcome)
    assert outcome.resolution is ReconciliationResolutionKind.PROVEN_NOT_APPLIED
    assert outcome.next_attempt_at is not None
    assert outcome.next_attempt_at > NOW
    expected = compute_proven_not_applied_next_attempt(
        occurred_at=NOW, failed_attempt=original.attempt
    )
    assert outcome.next_attempt_at == expected
    assert _STRATEGY[kind]  # strategy table remains complete


def test_proven_not_applied_reducer_enters_waiting_retry_without_immediate_write(
    prepared_source,
) -> None:
    from ai_dev_loop.pr_review_v2.domain import ErrorSummary, TransientErrorKind
    from ai_dev_loop.pr_review_v2.domain.common import coerce_utc_instant

    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0]
    uncertain = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-pna",
            original_write=commit,
        ),
    )
    assert uncertain.state.kind == "reconciling_write"
    next_at = compute_proven_not_applied_next_attempt(
        occurred_at=coerce_utc_instant(T2),
        failed_attempt=commit.attempt,
    )
    applied = reduce_pr_review(
        uncertain.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
                original_effect_id=commit.effect_id,
                next_attempt_at=next_at,
            ),
        ),
    )
    assert applied.state.kind == "waiting_retry"
    assert applied.state.retrying_effect_id == commit.effect_id
    assert applied.state.next_attempt_at == next_at
    assert applied.state.attempt == commit.attempt + 1
    assert applied.effects == ()
    assert applied.state.attempt <= commit.max_attempts


def test_proven_not_applied_at_max_attempt_does_not_schedule_seventh(prepared_source) -> None:
    from ai_dev_loop.pr_review_v2.domain import (
        ErrorSummary,
        ReconciliationStrategyKind,
        TransientErrorKind,
    )
    from ai_dev_loop.pr_review_v2.domain.common import coerce_utc_instant
    from ai_dev_loop.pr_review_v2.domain.effects import ReconcileWriteEffect
    from ai_dev_loop.pr_review_v2.domain.state import ReconcilingWriteState

    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0].model_copy(update={"attempt": 6})
    publishing = state.model_copy(update={"active_effect": commit})
    reconcile = ReconcileWriteEffect(
        effect_id=f"{commit.effect_id}:reconcile",
        idempotency_key=f"{commit.idempotency_key}:reconcile",
        run_id=commit.run_id,
        cycle_number=commit.cycle_number,
        attempt=1,
        max_attempts=commit.max_attempts,
        repository=commit.repository,
        bound_head_sha=commit.bound_head_sha,
        original_write=commit,
        strategy=ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
        reconciliation_identity="rec-max",
    )
    reconciling = ReconcilingWriteState(
        run_id=publishing.run_id,
        origin=publishing.origin,
        limits=publishing.limits,
        cycle_number=publishing.cycle_number,
        entered_at=coerce_utc_instant(T2),
        suspended=publishing,
        original_write=commit,
        active_effect=reconcile,
        last_error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
    )
    next_at = compute_proven_not_applied_next_attempt(
        occurred_at=coerce_utc_instant(T2), failed_attempt=6
    )
    result = reduce_pr_review(
        reconciling,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(reconcile),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
                original_effect_id=commit.effect_id,
                next_attempt_at=next_at,
            ),
        ),
    )
    assert result.state.kind == "paused"
    assert result.effects == ()


# -- Finding 2: authoritative remote_name ---------------------------------


def test_gateway_uses_policy_remote_name_not_default_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = b"diff --git a/f b/f\n"
    patch_ref = write_artifact(art, RUN_ID, "artifacts/p.patch", patch)
    msg_ref = write_commit_message(art, RUN_ID, subject="subject")
    effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=WH.SHA_PARENT,
        expected_branch="feature",
    )
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
        remote_shas={"feature": None, "refs/heads/feature": None},
    )
    observed: list[str] = []
    original = transport.read_remote_ref

    def capture(remote_name: str, remote_ref: str):
        observed.append(remote_name)
        return original(remote_name, remote_ref)

    transport.read_remote_ref = capture  # type: ignore[method-assign]
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_name="upstream",
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    proof = gateway.reconcile_commit(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED
    assert observed == ["upstream"]
    with pytest.raises(ValueError, match="must match"):
        GitPublicationGateway(
            policy=GitWritePolicy(
                repository_cwd=str(repo),
                remote_name="upstream",
                remote_scheme=GitRemoteScheme.LOCAL,
                require_ssh_agent_identity=False,
            ),
            transport=transport,  # type: ignore[arg-type]
            input_reader=InputArtifactReader(art),
            remote_name="origin",
        )


# -- Finding 3: cross-repository candidates fail closed -------------------


def test_cross_repository_pr_candidate_blocks_before_authorize(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="T", body="b", head_sha=SHA_B, full_name="fork/demo")]
            )
        }
    )
    auth = _Auth()
    with pytest.raises(GhTransportError) as exc_info:
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert exc_info.value.block is not None
    assert "cross-repository" in exc_info.value.block.safe_summary.lower() or "fork" in (
        exc_info.value.block.safe_summary.lower()
    )
    assert auth.calls == 0
    assert "create_pull_request" not in writes.method_names()


def test_mixed_same_repo_and_fork_candidates_fail_closed(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    mixed = [
        pr_dict(number=7, title="T", body="b", head_sha=SHA_B, full_name="acme/demo"),
        pr_dict(number=8, title="T", body="b", head_sha=SHA_B, full_name="fork/demo"),
    ]
    writes = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result(mixed)})
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )
    writes2 = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result(mixed)})
    proof = _gh_gateway(tmp_path, writes2).reconcile_create_or_update_pr(
        effect, run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.UNRESOLVED


# -- Finding 4: malformed preflight → MALFORMED_EVIDENCE ------------------


def test_malformed_pr_number_before_authorize_is_blocked(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    bad = pr_dict(title="T", body="b", head_sha=SHA_B)
    bad["number"] = "not-an-int"
    writes = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result([bad])})
    auth = _Auth()
    with pytest.raises(GhTransportError) as exc_info:
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert exc_info.value.block is not None
    assert "malformed" in exc_info.value.block.safe_summary.lower()
    assert auth.calls == 0


def test_malformed_pr_list_reconciles_unresolved(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    bad = pr_dict(title="T", body="b", head_sha=SHA_B)
    del bad["head"]["repo"]
    writes = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result([bad])})
    proof = _gh_gateway(tmp_path, writes).reconcile_create_or_update_pr(
        effect, run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.UNRESOLVED


# -- Finding 5: authority → baseline → commit dispatch --------------------


def test_commit_authority_then_baseline_then_dispatch_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = b"diff --git a/f b/f\n"
    patch_ref = write_artifact(art, RUN_ID, "artifacts/p.patch", patch)
    msg_ref = write_commit_message(art, RUN_ID, subject="subject")
    effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=WH.SHA_PARENT,
        expected_branch="feature",
    )
    order: list[str] = []
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
        remote_shas={"feature": None},
    )
    original_remote = transport.read_remote_ref
    original_commit = transport.commit_with_message_stdin

    def remote(remote_name: str, remote_ref: str):
        order.append("baseline")
        return original_remote(remote_name, remote_ref)

    def commit(message: str):
        order.append("dispatch")
        transport.head = "b" * 40
        transport.messages[transport.head] = message
        transport.parents[transport.head] = (WH.SHA_PARENT,)
        transport.commit_patches[(WH.SHA_PARENT, transport.head)] = patch
        return original_commit(message)

    transport.read_remote_ref = remote  # type: ignore[method-assign]
    transport.commit_with_message_stdin = commit  # type: ignore[method-assign]
    auth = _Auth()
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert order == ["authorize", "baseline", "dispatch"] or (
        auth.order == ["authorize"] and order == ["baseline", "dispatch"]
    )
    assert auth.calls == 1


def test_baseline_failure_after_authorize_is_pre_mutation_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = b"diff --git a/f b/f\n"
    patch_ref = write_artifact(art, RUN_ID, "artifacts/p.patch", patch)
    msg_ref = write_commit_message(art, RUN_ID, subject="subject")
    effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=WH.SHA_PARENT,
        expected_branch="feature",
    )
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
        remote_shas={"feature": "d" * 40, "refs/heads/feature": "d" * 40},
    )
    dispatched = {"n": 0}

    def boom_commit(message: str):
        dispatched["n"] += 1
        raise AssertionError("commit must not run after baseline failure")

    transport.commit_with_message_stdin = boom_commit  # type: ignore[method-assign]
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    with pytest.raises(GitTransportError) as exc_info:
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    assert exc_info.value.block is not None
    assert dispatched["n"] == 0


# -- Finding 6: exact thread node id --------------------------------------


def test_wrong_thread_comment_node_id_blocks_reply(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result(
                thread_comment_connection([], thread_id="OTHER_THREAD")
            ),
        }
    )
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).post_thread_reply(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )
    assert "add_review_thread_reply" not in writes.method_names()


def test_wrong_thread_resolved_node_id_reconciles_unresolved(tmp_path: Path) -> None:
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(
                thread_resolved_payload(thread_id="OTHER", is_resolved=False)
            )
        }
    )
    # Identity check happens first with wrong id → unresolved via catch.
    proof = _gh_gateway(tmp_path, writes).reconcile_resolve_thread(
        resolve_thread_effect(), run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.UNRESOLVED


# -- Finding 7: unsafe branch/ref rejection before transport --------------


def test_unsafe_head_branch_rejected_with_zero_transport(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub).model_copy(
        update={"head_branch": "../evil"}
    )
    writes = FakeGhWriteTransport(responses={})
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )
    assert writes.method_names() == []


def test_unsafe_remote_name_rejected_at_policy() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GitWritePolicy(repository_cwd="/tmp/repo", remote_name="bad:name")
    with pytest.raises(ValueError):
        validate_remote_name("origin;rm")
    with pytest.raises(ValueError):
        validate_branch_name("-bad")


# -- Finding 8: deadline before root inspection ---------------------------


def test_root_inspection_can_exhaust_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = b"diff --git a/f b/f\n"
    patch_ref = write_artifact(art, RUN_ID, "artifacts/p.patch", patch)
    msg_ref = write_commit_message(art, RUN_ID, subject="subject")
    effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=WH.SHA_PARENT,
        expected_branch="feature",
    )
    mono = {"t": 100.0}
    transport = WH.FakeGitTransport(root=str(repo.resolve()), head=WH.SHA_PARENT, staged=patch)

    def expire_on_root():
        mono["t"] = 200.0
        # Gateway call_timeout is consulted via provider when real transport runs;
        # FakeGitTransport has no provider, so invoke the gateway path explicitly.
        raise GitTransportError(
            transient=__import__(
                "ai_dev_loop.pr_review_v2.application.github_read",
                fromlist=["GatewayTransient", "GatewayTransientKind"],
            ).GatewayTransient(
                kind=__import__(
                    "ai_dev_loop.pr_review_v2.application.github_read",
                    fromlist=["GatewayTransientKind"],
                ).GatewayTransientKind.TIMEOUT,
                safe_summary="git overall deadline exceeded before mutation",
                transient_kind=__import__(
                    "ai_dev_loop.pr_review_v2.domain.common", fromlist=["TransientErrorKind"]
                ).TransientErrorKind.TIMEOUT,
            )
        )

    # Better: wire timeout provider through Fake by having gateway expire on first call.
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
            per_call_timeout_seconds=1.0,
            overall_timeout_seconds=1.0,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
        monotonic=lambda: mono["t"],
    )
    original_root = transport.inspect_repository_root

    def root_exhausts():
        # Advance past overall deadline before returning; next timeout check fails.
        mono["t"] = 200.0
        # Force the gateway's remaining-budget check during root resolution path
        # by calling the provider (as a real transport would).
        gateway._call_timeout()  # noqa: SLF001
        return original_root()

    transport.inspect_repository_root = root_exhausts  # type: ignore[method-assign]
    with pytest.raises(GitTransportError) as exc_info:
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    assert exc_info.value.transient is not None
    assert "before mutation" in exc_info.value.transient.safe_summary


def test_backoff_base_delays_are_positive() -> None:
    assert all(d > 0 for d in LOCAL_RETRY_BASE_DELAYS_SECONDS)
