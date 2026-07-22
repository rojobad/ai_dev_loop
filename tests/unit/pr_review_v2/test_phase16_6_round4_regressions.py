"""Phase 16.6 round-4 Codex correction regressions.

Covers typed ``_dig`` missing-key failures, argv-safe commit/push DTO + gateway
fencing, and operation-local GitPublicationGateway deadline state under shared
gateway lock contention. Fake transports only.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2 import write_helpers as WH
from tests.unit.pr_review_v2.durable_helpers import (
    FakeClock,
    publication_success,
    start_run,
)
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    FakeGhWriteTransport,
    FakeReadTransport,
    commit_effect,
    graphql_result,
    post_reply_effect,
    push_effect,
    request_review_effect,
    resolve_thread_effect,
    thread_resolved_payload,
    write_artifact,
    write_commit_message,
    write_reply_text,
)

from ai_dev_loop.pr_review_v2.application.contracts import EffectCompletionRequest, EventDisposition
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.github_read import GatewayBlockKind, block_for_kind
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    GitHubWritePolicy,
    GitRemoteScheme,
    GitWritePolicy,
    WriteProofKind,
)
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    CommitPatchEffect,
    PreparedState,
    PushCommitEffect,
    ReconciliationResolutionKind,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectBlocked,
    EffectSucceeded,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

NOW = WH.NOW


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _gh_gateway(
    tmp_path: Path,
    writes: FakeGhWriteTransport,
    *,
    reads: FakeReadTransport | None = None,
) -> GitHubWriteGateway:
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return GitHubWriteGateway(
        policy=GitHubWritePolicy(repository_cwd=str(repo)),
        write_transport=writes,
        read_transport=reads or FakeReadTransport(),
        input_reader=InputArtifactReader(art),
        write_evidence=WriteEvidenceStore(art),
    )


# -- Finding 1: _dig missing nested keys → MALFORMED_EVIDENCE -------------


def test_missing_nested_pr_identity_key_blocks_before_authorize(tmp_path: Path) -> None:
    effect = request_review_effect(marker="mk-review-1")
    reads = FakeReadTransport(identity_responses=graphql_result({"data": {}}))
    writes = FakeGhWriteTransport(responses={})
    auth = _Auth()
    with pytest.raises(GhTransportError) as exc_info:
        _gh_gateway(tmp_path, writes, reads=reads).request_review(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert exc_info.value.block is not None
    assert exc_info.value.block.kind is GatewayBlockKind.MALFORMED_EVIDENCE
    assert "missing nested key" in exc_info.value.block.safe_summary
    assert auth.calls == 0
    assert "create_issue_comment" not in writes.method_names()


def test_missing_nested_pr_identity_key_executor_blocks(tmp_path: Path) -> None:
    effect = request_review_effect(marker="mk-review-1")
    reads = FakeReadTransport(identity_responses=graphql_result({"data": {}}))
    writes = FakeGhWriteTransport(responses={})
    gw = _gh_gateway(tmp_path, writes, reads=reads)
    result = WriteExecutor(
        git_gateway=object(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(tmp_path / "repo")),
    ).execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, EffectBlocked)
    assert writes.method_names() == []


def test_missing_nested_pr_identity_key_reconciles_unresolved(tmp_path: Path) -> None:
    effect = request_review_effect(marker="mk-review-1")
    reads = FakeReadTransport(identity_responses=graphql_result({"data": {}}))
    writes = FakeGhWriteTransport(responses={})
    gw = _gh_gateway(tmp_path, writes, reads=reads)
    proof = gw.reconcile_request_review(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED
    recon = WH.reconcile_effect(effect)
    result = ReconcileWriteExecutor(
        git_gateway=object(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(tmp_path / "repo")),
    ).execute(
        recon,
        WH.token_for(recon),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(recon),
    )
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.resolution is ReconciliationResolutionKind.UNRESOLVED  # type: ignore[union-attr]


def test_missing_nested_thread_comment_key_blocks_reply(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result({"data": {}}),  # missing node
        }
    )
    auth = _Auth()
    with pytest.raises(GhTransportError) as exc_info:
        _gh_gateway(tmp_path, writes).post_thread_reply(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert exc_info.value.block is not None
    assert exc_info.value.block.kind is GatewayBlockKind.MALFORMED_EVIDENCE
    assert "missing nested key" in exc_info.value.block.safe_summary
    assert auth.calls == 0
    assert "add_review_thread_reply" not in writes.method_names()


def test_missing_nested_thread_key_reconciles_unresolved(tmp_path: Path) -> None:
    writes = FakeGhWriteTransport(responses={"fetch_thread_resolved": graphql_result({"data": {}})})
    gw = _gh_gateway(tmp_path, writes)
    proof = gw.reconcile_resolve_thread(resolve_thread_effect(), run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED

    original = resolve_thread_effect()
    recon = WH.reconcile_effect(original)
    result = ReconcileWriteExecutor(
        git_gateway=object(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(tmp_path / "repo")),
    ).execute(
        recon,
        WH.token_for(recon),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(recon),
    )
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.resolution is ReconciliationResolutionKind.UNRESOLVED  # type: ignore[union-attr]


# -- Finding 2: argv-safe DTO + typed gateway fencing ---------------------


def test_commit_expected_branch_rejected_at_dto_construction() -> None:
    with pytest.raises(ValidationError):
        CommitPatchEffect(
            effect_id="e1",
            idempotency_key="k1",
            run_id=RUN_ID,
            cycle_number=1,
            attempt=1,
            max_attempts=3,
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            bound_head_sha=WH.SHA_PARENT,
            patch_ref=ArtifactRef(relative_path="artifacts/p.patch", sha256="a" * 64),
            expected_head_sha=WH.SHA_PARENT,
            expected_branch="../evil",
            commit_message_ref=ArtifactRef(relative_path="artifacts/m.json", sha256="b" * 64),
        )


def test_push_remote_ref_rejected_at_dto_construction() -> None:
    with pytest.raises(ValidationError):
        PushCommitEffect(
            effect_id="e1",
            idempotency_key="k1",
            run_id=RUN_ID,
            cycle_number=1,
            attempt=1,
            max_attempts=3,
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            bound_head_sha=WH.SHA_COMMIT,
            commit_sha=WH.SHA_COMMIT,
            remote_ref="feature;rm",
            expected_remote_sha_before_push=None,
        )


def test_corrupted_commit_branch_blocks_with_zero_transport_and_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
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
    ).model_copy(update={"expected_branch": "../evil"})
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
    )
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
    with pytest.raises(GitTransportError) as exc_info:
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert exc_info.value.block is not None
    assert exc_info.value.block.kind is GatewayBlockKind.HTTP_VALIDATION_REJECTION
    assert auth.calls == 0
    assert transport.commits == []
    assert transport.pushes == []


def test_corrupted_push_ref_blocks_with_zero_transport_and_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    effect = push_effect(
        commit_sha=WH.SHA_COMMIT,
        remote_ref="feature",
        expected_remote_sha_before_push=None,
    ).model_copy(update={"remote_ref": "refs/heads/../evil"})
    transport = WH.FakeGitTransport(root=str(repo.resolve()), head=WH.SHA_COMMIT)
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
    with pytest.raises(GitTransportError) as exc_info:
        gateway.push(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert exc_info.value.block is not None
    assert auth.calls == 0
    assert transport.pushes == []
    assert transport.commits == []


def test_write_executor_maps_corrupted_push_ref_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    effect = push_effect(
        commit_sha=WH.SHA_COMMIT,
        remote_ref="feature",
        expected_remote_sha_before_push=None,
    ).model_copy(update={"remote_ref": "feature;rm"})
    transport = WH.FakeGitTransport(root=str(repo.resolve()), head=WH.SHA_COMMIT)
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    result = WriteExecutor(
        git_gateway=gateway,
        github_gateway=object(),  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    ).execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, EffectBlocked)
    assert transport.pushes == []


def test_write_executor_maps_corrupted_commit_branch_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
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
    ).model_copy(update={"expected_branch": "feature;rm"})
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()), head=WH.SHA_PARENT, staged=patch, status="M  f\n"
    )
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    result = WriteExecutor(
        git_gateway=gateway,
        github_gateway=object(),  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    ).execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, EffectBlocked)
    assert transport.commits == []


def test_effect_worker_fences_blocked_unsafe_ref_via_complete_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="unsafe"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = PreparedState(
        run_id="run-1",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json", sha256="2" * 64
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    local = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert local is not None
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
    captured: list[EffectCompletionRequest] = []
    real_complete = engine.complete_claim

    def spy(request: EffectCompletionRequest):
        captured.append(request)
        return real_complete(request)

    engine.complete_claim = spy  # type: ignore[method-assign]
    block = block_for_kind(GatewayBlockKind.HTTP_VALIDATION_REJECTION, detail="unsafe ref")

    class _BlockedExecutor:
        def execute(self, effect, token, *, now, authority, claim):
            del effect, authority, claim
            return EffectBlocked(
                occurred_at=now,
                token=token,
                reason=block.pause_reason,
                safe_action=block.safe_action,
                safe_summary=block.safe_summary,
            )

    worker = EffectWorker(
        engine,
        _BlockedExecutor(),  # type: ignore[arg-type]
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
    )
    step = worker.run_once(prepared.run_id)
    assert captured
    assert isinstance(captured[0].event, EffectBlocked)
    assert step.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }


def test_effect_worker_fences_blocked_push_from_real_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker fences a typed blocked push (corrupted remote_ref) through complete_claim."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir()
    unsafe = push_effect(
        commit_sha=WH.SHA_COMMIT,
        remote_ref="feature",
        expected_remote_sha_before_push=None,
    ).model_copy(update={"remote_ref": "feature;rm"})
    transport = WH.FakeGitTransport(root=str(repo.resolve()), head=WH.SHA_COMMIT)
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="blk"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = PreparedState(
        run_id="run-1",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json", sha256="2" * 64
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    local = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert local is not None
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
    captured: list[EffectCompletionRequest] = []
    real_complete = engine.complete_claim

    def spy(request: EffectCompletionRequest):
        captured.append(request)
        return real_complete(request)

    engine.complete_claim = spy  # type: ignore[method-assign]
    inner = WriteExecutor(
        git_gateway=gateway,
        github_gateway=object(),  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    )
    token = WH.token_for(unsafe)
    unsafe_claim = WH.claim_for(unsafe)

    class _ForceUnsafePush:
        def execute(self, effect, token_ignored, *, now, authority, claim):
            del effect, token_ignored, claim
            return inner.execute(unsafe, token, now=now, authority=authority, claim=unsafe_claim)

    worker = EffectWorker(
        engine,
        _ForceUnsafePush(),  # type: ignore[arg-type]
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
    )
    step = worker.run_once(prepared.run_id)
    assert captured
    assert isinstance(captured[0].event, EffectBlocked)
    assert transport.pushes == []
    assert step.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }


# -- Finding 3: operation-local deadline under shared gateway -------------


def test_lock_contending_call_preserves_active_deadline_and_post_dispatch_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
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
    # Same run_id so protected input artifacts resolve; lock contention is per-repo.
    rival_effect = commit_effect(
        patch_ref=patch_ref,
        commit_message_ref=msg_ref,
        expected_head_sha=WH.SHA_PARENT,
        expected_branch="feature",
        run_id=RUN_ID,
    ).model_copy(
        update={
            "effect_id": "pr-review:run-16-6:cycle:01:commit_patch:rival",
            "idempotency_key": "pr-review:run-16-6:cycle:01:commit_patch:rival",
        }
    )
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
    )
    mono = {"t": 100.0}

    def clock() -> float:
        return mono["t"]

    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
            per_call_timeout_seconds=1.0,
            overall_timeout_seconds=30.0,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
        monotonic=clock,
    )
    held = threading.Event()
    contender_done = threading.Event()
    contender_errors: list[BaseException] = []
    original_branch = transport.read_current_branch

    def hold_under_lock():
        assert gateway._call_timeout() > 0  # noqa: SLF001
        held.set()
        assert contender_done.wait(timeout=2.0)
        # Contender must not clear this operation's overall budget.
        assert gateway._call_timeout() > 0  # noqa: SLF001
        return original_branch()

    transport.read_current_branch = hold_under_lock  # type: ignore[method-assign]

    def contender() -> None:
        assert held.wait(timeout=2.0)
        try:
            gateway.commit(rival_effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
        except BaseException as exc:  # noqa: BLE001
            contender_errors.append(exc)
        finally:
            contender_done.set()

    thread = threading.Thread(target=contender)
    thread.start()

    original_commit = transport.commit_with_message_stdin

    def expire_after_dispatch(message: str):
        mono["t"] = 200.0
        gateway._call_timeout()  # noqa: SLF001
        return original_commit(message)

    transport.commit_with_message_stdin = expire_after_dispatch  # type: ignore[method-assign]

    with pytest.raises(AmbiguousWriteError):
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert contender_errors
    assert isinstance(contender_errors[0], GitTransportError)
    assert contender_errors[0].transient is not None
    assert "lock" in contender_errors[0].transient.safe_summary.lower()

    # Executor maps the same post-dispatch expiry to WriteOutcomeUncertain.
    transport2 = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
    )
    mono["t"] = 100.0
    gateway2 = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(repo),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
            per_call_timeout_seconds=1.0,
            overall_timeout_seconds=30.0,
        ),
        transport=transport2,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
        monotonic=clock,
    )
    real_commit = transport2.commit_with_message_stdin

    def expire_then_commit(message: str):
        mono["t"] = 200.0
        gateway2._call_timeout()  # noqa: SLF001
        return real_commit(message)

    transport2.commit_with_message_stdin = expire_then_commit  # type: ignore[method-assign]
    result = WriteExecutor(
        git_gateway=gateway2,
        github_gateway=object(),  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    ).execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, WriteOutcomeUncertain)
