"""Phase 16.6 correction regressions for Codex findings 1–10.

Covers required-but-nullable remote SHA baselines, post-mutation ambiguity,
gh env allowlisting, descriptor artifact reads, competing repository locks, and
worker-level ``lease_authority_lost`` fencing. Fake transports only.
"""

from __future__ import annotations

import os
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
    SHA_B,
    FakeGhWriteTransport,
    FakeReadTransport,
    create_pr_effect,
    graphql_result,
    post_reply_effect,
    pr_dict,
    push_effect,
    request_review_effect,
    resolve_thread_effect,
    rest_result,
    thread_comment_connection,
    thread_resolved_payload,
    transient_error,
    update_pr_text_effect,
    write_artifact,
    write_publication_text,
    write_reply_text,
)

from ai_dev_loop.locking import FileLock, LockError, LockMetadata
from ai_dev_loop.paths import repository_lock_path
from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.github_read import GatewayTransientKind
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    AuthorityLostError,
    CommitMessageArtifact,
    GitHubWritePolicy,
    PublicationTextArtifact,
    WriteOutcomeUncertain,
    WriteProofKind,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    parse_single_owned_preimage,
    reject_prohibited_controls,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import make_write_uncertain
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.common import build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.domain.effects import PR_REVIEW_EFFECT_ADAPTER, PushCommitEffect
from ai_dev_loop.pr_review_v2.domain.events import (
    PR_REVIEW_EVENT_ADAPTER,
    CommitRecordedOutcome,
    EffectBlocked,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import build_minimal_gh_env
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor
from ai_dev_loop.process import run_process, run_process_bytes

NOW = WH.NOW


# -- Finding 1 ------------------------------------------------------------


def test_commit_recorded_outcome_omitted_remote_sha_fails_closed() -> None:
    with pytest.raises(ValidationError):
        CommitRecordedOutcome.model_validate(
            {"kind": "commit_recorded", "commit_sha": "b" * 40, "new_head_sha": "b" * 40}
        )


def test_commit_recorded_outcome_explicit_null_is_absent_ref_baseline() -> None:
    outcome = CommitRecordedOutcome(
        commit_sha="b" * 40, new_head_sha="b" * 40, expected_remote_sha_before_push=None
    )
    dumped = outcome.model_dump(mode="json")
    assert dumped["expected_remote_sha_before_push"] is None
    assert CommitRecordedOutcome.model_validate(dumped).expected_remote_sha_before_push is None


def test_push_commit_effect_omitted_remote_sha_fails_closed() -> None:
    payload = {
        "kind": "push_commit",
        "effect_id": "e",
        "idempotency_key": "e",
        "run_id": RUN_ID,
        "cycle_number": 1,
        "attempt": 1,
        "max_attempts": 6,
        "repository": {"name_with_owner": "acme/demo"},
        "bound_head_sha": "b" * 40,
        "commit_sha": "b" * 40,
        "remote_ref": "feature",
        "force": False,
    }
    with pytest.raises(ValidationError):
        PushCommitEffect.model_validate(payload)
    payload["expected_remote_sha_before_push"] = None
    assert PushCommitEffect.model_validate(payload).expected_remote_sha_before_push is None


def test_effect_adapter_rejects_omitted_remote_sha_baseline() -> None:
    effect = push_effect(commit_sha="b" * 40, expected_remote_sha_before_push=None)
    adapted = PR_REVIEW_EFFECT_ADAPTER.validate_python(effect.model_dump(mode="json"))
    assert adapted.expected_remote_sha_before_push is None
    omitted = {
        k: v
        for k, v in effect.model_dump(mode="json").items()
        if k != "expected_remote_sha_before_push"
    }
    with pytest.raises(ValidationError):
        PR_REVIEW_EFFECT_ADAPTER.validate_python(omitted)


def test_uncertain_event_preserves_null_remote_sha_baseline() -> None:
    effect = push_effect(commit_sha="b" * 40, expected_remote_sha_before_push=None)
    event = make_write_uncertain(token=WH.token_for(effect), original_write=effect, occurred_at=NOW)
    adapted = PR_REVIEW_EVENT_ADAPTER.validate_python(event.model_dump(mode="json"))
    assert adapted.original_write.expected_remote_sha_before_push is None


# -- Finding 2 ------------------------------------------------------------


class _Auth:
    def __call__(self) -> None:
        return None


def _gh_gateway(
    tmp_path: Path,
    writes: FakeGhWriteTransport,
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


def test_create_pr_post_authorize_timeout_is_ambiguous(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "list_prs_by_head_base": rest_result([]),
                "create_pull_request": transient_error(GatewayTransientKind.TIMEOUT),
            }
        ),
    )
    with pytest.raises(AmbiguousWriteError):
        gw.create_or_update_pr(
            create_pr_effect(publication_text_ref=pub), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_update_pr_text_post_authorize_http_500_is_ambiguous(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    old_marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="previous-key",
        canonical_content=canonicalize_publication_text(title="old", body="old body"),
    )
    old_body = append_owned_marker("old body", old_marker.marker_text)
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "fetch_pr_text": rest_result(pr_dict(title="old", body=old_body, head_sha=SHA_B)),
                "update_pr_text": transient_error(GatewayTransientKind.HTTP_500),
            }
        ),
    )
    with pytest.raises(AmbiguousWriteError):
        gw.update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_request_review_post_authorize_connection_reset_is_ambiguous(tmp_path: Path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import comment_connection

    marker = build_opaque_trigger_marker(run_id=RUN_ID, cycle_number=1)
    reads = FakeReadTransport(issue_comment_pages=[graphql_result(comment_connection([]))])
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "create_issue_comment": transient_error(GatewayTransientKind.CONNECTION_RESET),
            }
        ),
        reads=reads,
    )
    with pytest.raises(AmbiguousWriteError):
        gw.request_review(
            request_review_effect(marker=marker), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_post_reply_post_authorize_timeout_is_ambiguous(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
                "fetch_thread_comments_page": graphql_result(thread_comment_connection([])),
                "add_review_thread_reply": transient_error(GatewayTransientKind.TIMEOUT),
            }
        ),
    )
    with pytest.raises(AmbiguousWriteError):
        gw.post_thread_reply(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_resolve_thread_post_authorize_http_500_is_ambiguous(tmp_path: Path) -> None:
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "fetch_thread_resolved": [
                    graphql_result(thread_resolved_payload(is_resolved=False)),
                    graphql_result(thread_resolved_payload(is_resolved=False)),
                ],
                "resolve_review_thread": transient_error(GatewayTransientKind.HTTP_500),
            }
        ),
    )
    with pytest.raises(AmbiguousWriteError):
        gw.resolve_thread(resolve_thread_effect(), run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_write_executor_maps_ambiguous_to_uncertain_not_retry(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    gw = _gh_gateway(
        tmp_path,
        FakeGhWriteTransport(
            responses={
                "list_prs_by_head_base": rest_result([]),
                "create_pull_request": transient_error(GatewayTransientKind.TIMEOUT),
            }
        ),
    )

    class _DummyGit:
        def __getattr__(self, name: str):
            raise AssertionError(f"git gateway unused: {name}")

    executor = WriteExecutor(
        git_gateway=_DummyGit(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    )
    result = executor.execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, WriteOutcomeUncertain)


def test_run_process_terminates_process_group_on_timeout(tmp_path: Path) -> None:
    script = tmp_path / "hang.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o700)
    result = run_process(["/bin/sh", str(script)], timeout=0.2)
    assert result.timed_out is True


def test_run_process_bytes_preserves_binary_stdout(tmp_path: Path) -> None:
    script = tmp_path / "bin.sh"
    script.write_bytes(b"#!/bin/sh\nprintf '\\000\\377 binary'\n")
    script.chmod(0o700)
    result = run_process_bytes(["/bin/sh", str(script)], timeout=5.0)
    assert result.returncode == 0
    assert result.stdout == b"\x00\xff binary"


# -- Finding 4 ------------------------------------------------------------


def test_foreign_v1_marker_never_authorizes_overwrite() -> None:
    body = "<!-- adl-v1:" + ("a" * 64) + " -->\nbody"
    assert parse_single_owned_preimage(body) is None


def test_duplicate_v2_marker_never_authorizes_overwrite() -> None:
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="k",
        canonical_content=canonicalize_publication_text(title="T", body="body"),
    )
    body = f"<!-- {marker.marker_text} -->\n<!-- {marker.marker_text} -->\nbody"
    with pytest.raises(ValueError, match="duplicate"):
        parse_single_owned_preimage(body)


def test_content_bound_marker_is_v2_parseable() -> None:
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="key",
        canonical_content=canonicalize_publication_text(title="T", body="body"),
    )
    assert marker.marker_text.startswith("adl-v2:pr_body:create_or_update_pr:")
    parsed = parse_single_owned_preimage(f"<!-- {marker.marker_text} -->\nbody")
    assert parsed is not None
    assert parsed.content_sha256 == marker.content_sha256


# -- Finding 5 ------------------------------------------------------------


def test_competing_local_runs_serialize_on_repository_filelock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    holder = FileLock(repository_lock_path(repo))
    holder.acquire(
        LockMetadata(
            pid=os.getpid(),
            run_id="run:opaque-holder",
            repository_path="repo:opaque-path",
            started_at=NOW,
        )
    )
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    gw = _gh_gateway(
        tmp_path, FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result([])})
    )
    errors: list[BaseException] = []

    def rival() -> None:
        try:
            gw.create_or_update_pr(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=rival)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    holder.release()
    assert errors
    assert isinstance(errors[0], GhTransportError)
    assert errors[0].transient is not None
    assert "lock" in errors[0].transient.safe_summary.lower()
    assert RUN_ID not in str(errors[0])
    assert str(repo) not in str(errors[0])


# -- Finding 7 ------------------------------------------------------------


def test_incomplete_pr_pages_never_prove_absence(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    full_page = [
        {"number": i, "head": {"ref": "other"}, "base": {"ref": "main"}} for i in range(100)
    ]
    writes = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result(full_page)})
    art = tmp_path / "art"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    gw = GitHubWriteGateway(
        policy=GitHubWritePolicy(repository_cwd=str(repo), max_pages=1),
        write_transport=writes,
        read_transport=FakeReadTransport(),
        input_reader=InputArtifactReader(art),
        write_evidence=WriteEvidenceStore(art),
    )
    try:
        proof = gw.reconcile_create_or_update_pr(effect, run_id=RUN_ID, now=NOW)
    except Exception:
        return
    assert proof.proof is not WriteProofKind.PROVEN_NOT_APPLIED


def test_commit_post_authorize_timeout_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    from ai_dev_loop.pr_review_v2.application.write_contracts import GitRemoteScheme, GitWritePolicy
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitProcessOutcome

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    art = tmp_path / "art"
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
        root=str(tmp_path / "repo"),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
        commit_result=GitProcessOutcome(
            returncode=-1, stdout="", stderr="", timed_out=True, argv=("git", "commit")
        ),
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(tmp_path / "repo"),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
    )
    with pytest.raises(AmbiguousWriteError, match="timed out"):
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_push_post_authorize_timeout_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.pr_review_v2.application.write_contracts import GitRemoteScheme, GitWritePolicy
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitProcessOutcome

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    (tmp_path / "repo").mkdir(exist_ok=True)
    effect = push_effect(
        commit_sha=WH.SHA_COMMIT,
        remote_ref="feature",
        expected_remote_sha_before_push=None,
    )
    transport = WH.FakeGitTransport(
        root=str(tmp_path / "repo"),
        head=WH.SHA_COMMIT,
        remote_shas={},
        push_result=GitProcessOutcome(
            returncode=-1, stdout="", stderr="", timed_out=True, argv=("git", "push")
        ),
    )
    gateway = GitPublicationGateway(
        policy=GitWritePolicy(
            repository_cwd=str(tmp_path / "repo"),
            remote_scheme=GitRemoteScheme.LOCAL,
            require_ssh_agent_identity=False,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(tmp_path / "art"),
    )
    with pytest.raises(AmbiguousWriteError, match="timed out"):
        gateway.push(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


# -- Finding 8 ------------------------------------------------------------


def test_build_minimal_gh_env_excludes_unrelated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/x")
    monkeypatch.setenv("GH_TOKEN", "keep-me")
    monkeypatch.setenv("CURSOR_API_KEY", "drop")
    monkeypatch.setenv("OPENAI_API_KEY", "drop")
    monkeypatch.setenv("CODEX_API_KEY", "drop")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "drop")
    env = build_minimal_gh_env()
    assert env["GH_TOKEN"] == "keep-me"
    for banned in ("CURSOR_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        assert banned not in env


def test_gh_write_transport_preserves_explicit_empty_env() -> None:
    recorded: list[dict[str, str] | None] = []

    class _Runner:
        def run(self, args, *, cwd, timeout, env, stdin_text=None):
            from ai_dev_loop.process import StreamingProcessResult

            recorded.append(dict(env) if env is not None else None)
            return StreamingProcessResult(
                args=list(args),
                returncode=0,
                stdout="HTTP/1.1 200 OK\r\n\r\n[]",
                stderr="",
                timed_out=False,
                elapsed_seconds=0.01,
            )

    transport = GhWriteTransport(
        command="gh",
        cwd="/tmp",
        per_call_timeout_seconds=5.0,
        runner=_Runner(),  # type: ignore[arg-type]
        env={},
    )
    transport.list_prs_by_head_base(
        owner="acme", name="demo", head="feature", base="main", page=1, per_page=10
    )
    assert recorded == [{}]


# -- Finding 9 ------------------------------------------------------------


def test_publication_rejects_prohibited_controls() -> None:
    with pytest.raises(ValidationError):
        PublicationTextArtifact(title="ok", body="bad\x00null")
    with pytest.raises(ValueError):
        reject_prohibited_controls("x\x01y", field_name="body")


def test_commit_message_rejects_prohibited_controls() -> None:
    with pytest.raises(ValidationError):
        CommitMessageArtifact(subject="ok", body="line\x07bell")


def test_symlink_swap_race_rejected_via_nofollow(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW unavailable")
    from ai_dev_loop.pr_review_v2.infrastructure.paths import (
        ensure_run_artifact_root,
        resolve_run_relative_path,
    )

    reader = InputArtifactReader(tmp_path / "art")
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/p.patch", b"exact-bytes")
    run_root = ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    target = resolve_run_relative_path(run_root, ref.relative_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"other")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(InputArtifactError):
        reader.read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_parent_directory_symlink_swap_rejected_between_validate_and_open(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        pytest.skip("descriptor no-follow directory open unavailable")
    import shutil

    from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root

    reader = InputArtifactReader(tmp_path / "art")
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/nested/p.patch", b"exact-bytes")
    run_root = ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    nested = run_root / "artifacts" / "nested"
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "p.patch").write_bytes(b"tampered")
    # Replace the parent directory with a symlink after the artifact was written.
    shutil.rmtree(nested)
    nested.symlink_to(outside_dir)
    with pytest.raises(InputArtifactError):
        reader.read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_binary_patch_bytes_roundtrip_exact(tmp_path: Path) -> None:
    blob = bytes(range(256)) + b"\n\x00\xff"
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/bin.patch", blob)
    assert InputArtifactReader(tmp_path / "art").read_patch_bytes(run_id=RUN_ID, ref=ref) == blob


# -- Finding 10 -----------------------------------------------------------


def test_effect_worker_sets_lease_authority_lost_on_authority_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="authz"),
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

    class _RejectingMutating:
        def execute(self, effect, token, *, now, authority, claim):
            del effect, token, now, authority, claim
            raise AuthorityLostError("lease owner, generation, or expiry fence")

    worker = EffectWorker(
        engine,
        _RejectingMutating(),  # type: ignore[arg-type]
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
    )
    step = worker.run_once(prepared.run_id)
    assert captured
    assert captured[0].lease_authority_lost is True
    assert isinstance(captured[0].event, EffectBlocked)
    assert step.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }


# -- Round-2 Codex correction findings ------------------------------------


class _CountingAuth:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_create_pr_requires_head_branch_tip_before_authorize(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "fetch_branch_head_sha": rest_result(
                {"ref": "refs/heads/feature", "object": {"sha": "c" * 40}}
            ),
        }
    )
    auth = _CountingAuth()
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert auth.calls == 0
    assert "create_pull_request" not in writes.method_names()


def test_thread_missing_pr_ownership_fails_closed(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(
                {"data": {"node": {"id": "THREAD_1", "isResolved": False}}}
            )
        }
    )
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).resolve_thread(
            resolve_thread_effect(), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_resolved_thread_without_matching_reply_blocks_new_reply(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": [
                graphql_result(thread_resolved_payload(is_resolved=True)),
                graphql_result(thread_resolved_payload(is_resolved=True)),
            ],
            "fetch_thread_comments_page": graphql_result(thread_comment_connection([])),
        }
    )
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).post_thread_reply(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )
    assert "add_review_thread_reply" not in writes.method_names()


def test_reply_fast_path_rejects_tampered_body_with_correct_marker(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    marker = derive_content_bound_marker(
        operation="post_thread_reply",
        target_kind="thread_reply",
        idempotency_key=effect.idempotency_key,
        canonical_content="reply body",
    )
    tampered = append_owned_marker("tampered content", marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result(
                thread_comment_connection([{"id": "C1", "body": tampered}])
            ),
        }
    )
    auth = _CountingAuth()
    with pytest.raises(GhTransportError):
        _gh_gateway(tmp_path, writes).post_thread_reply(
            effect, run_id=RUN_ID, now=NOW, authorize=auth
        )
    assert auth.calls == 0
    assert "add_review_thread_reply" not in writes.method_names()


def test_malformed_create_pr_envelope_after_authorize_is_uncertain(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result({"number": 7}),  # missing head/base/state
        }
    )
    with pytest.raises(AmbiguousWriteError):
        _gh_gateway(tmp_path, writes).create_or_update_pr(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_malformed_update_pr_text_envelope_after_authorize_is_uncertain(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    old_marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="previous-key",
        canonical_content=canonicalize_publication_text(title="old", body="old body"),
    )
    old_body = append_owned_marker("old body", old_marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(pr_dict(title="old", body=old_body, head_sha=SHA_B)),
            "update_pr_text": rest_result({"number": 7}),  # missing marker/content fields
        }
    )
    with pytest.raises(AmbiguousWriteError):
        _gh_gateway(tmp_path, writes).update_pr_text(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_malformed_request_review_envelope_after_authorize_is_uncertain(tmp_path: Path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import comment_connection

    marker = build_opaque_trigger_marker(run_id=RUN_ID, cycle_number=1)
    reads = FakeReadTransport(issue_comment_pages=[graphql_result(comment_connection([]))])
    writes = FakeGhWriteTransport(
        responses={
            "create_issue_comment": rest_result({"body": "<!--x-->"}),  # missing id/createdAt
        }
    )
    with pytest.raises(AmbiguousWriteError):
        _gh_gateway(tmp_path, writes, reads=reads).request_review(
            request_review_effect(marker=marker), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_malformed_thread_reply_envelope_after_authorize_is_uncertain(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="reply body")
    effect = post_reply_effect(reply_ref=reply)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result(thread_comment_connection([])),
            "add_review_thread_reply": graphql_result({"data": {}}),
        }
    )
    with pytest.raises(AmbiguousWriteError):
        _gh_gateway(tmp_path, writes).post_thread_reply(
            effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_malformed_resolve_thread_envelope_after_authorize_is_uncertain(tmp_path: Path) -> None:
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "resolve_review_thread": graphql_result({"data": {}}),
        }
    )
    with pytest.raises(AmbiguousWriteError):
        _gh_gateway(tmp_path, writes).resolve_thread(
            resolve_thread_effect(), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_post_authorize_keyerror_becomes_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result(pr_dict(title="T", body="b", head_sha=SHA_B)),
        }
    )
    gw = _gh_gateway(tmp_path, writes)

    def boom(*_a, **_k):
        raise KeyError("partial envelope")

    monkeypatch.setattr(gw, "_confirm_pr", boom)
    with pytest.raises(AmbiguousWriteError):
        gw.create_or_update_pr(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_write_executor_maps_malformed_success_to_uncertain(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result({"number": 7}),
        }
    )
    gw = _gh_gateway(tmp_path, writes)

    class _DummyGit:
        def __getattr__(self, name: str):
            raise AssertionError(name)

    executor = WriteExecutor(
        git_gateway=_DummyGit(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(tmp_path / "repo")),
    )
    result = executor.execute(
        effect,
        WH.token_for(effect),
        now=NOW,
        authority=WH.FixedAuthority(),
        claim=WH.claim_for(effect),
    )
    assert isinstance(result, WriteOutcomeUncertain)


def test_effect_worker_records_uncertain_via_complete_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="unc"),
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

    class _UncertainMutating:
        def execute(self, effect, token, *, now, authority, claim):
            del authority, claim
            return make_write_uncertain(
                token=token,
                original_write=effect,
                occurred_at=now,  # type: ignore[arg-type]
            )

    worker = EffectWorker(
        engine,
        _UncertainMutating(),  # type: ignore[arg-type]
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
    )
    step = worker.run_once(prepared.run_id)
    assert captured
    assert isinstance(captured[0].event, WriteOutcomeUncertain)
    assert step.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }


def test_commit_reconcile_third_remote_sha_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    from ai_dev_loop.pr_review_v2.application.write_contracts import GitRemoteScheme, GitWritePolicy
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )

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
    transport = WH.FakeGitTransport(
        root=str(repo.resolve()),
        head=WH.SHA_PARENT,
        staged=patch,
        status="M  f\n",
        remote_shas={"feature": "d" * 40, "refs/heads/feature": "d" * 40},
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
    proof = gateway.reconcile_commit(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED
    assert (
        "third" in (proof.safe_summary or "").lower()
        or "non-ancestor" in (proof.safe_summary or "").lower()
    )


def test_git_overall_deadline_before_mutation_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    from ai_dev_loop.pr_review_v2.application.write_contracts import GitRemoteScheme, GitWritePolicy
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )
    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError

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
            overall_timeout_seconds=1.0,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
        monotonic=clock,
    )
    original = transport.read_current_branch

    def expire_then_read():
        mono["t"] = 200.0
        return original()

    transport.read_current_branch = expire_then_read  # type: ignore[method-assign]
    with pytest.raises(GitTransportError) as exc_info:
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    assert exc_info.value.transient is not None
    assert "before mutation" in exc_info.value.transient.safe_summary


def test_git_overall_deadline_after_mutation_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import commit_effect, write_commit_message

    from ai_dev_loop.pr_review_v2.application.write_contracts import GitRemoteScheme, GitWritePolicy
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )

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
            overall_timeout_seconds=5.0,
        ),
        transport=transport,  # type: ignore[arg-type]
        input_reader=InputArtifactReader(art),
        monotonic=clock,
    )
    original_commit = transport.commit_with_message_stdin

    def expire_during_commit(message: str):
        mono["t"] = 200.0
        # Expire before confirmation reads; mutation has started.
        gateway._call_timeout()  # noqa: SLF001
        return original_commit(message)

    transport.commit_with_message_stdin = expire_during_commit  # type: ignore[method-assign]
    with pytest.raises(AmbiguousWriteError):
        gateway.commit(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_lock_metadata_is_opaque(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_dev_loop.pr_review_v2.application.write_contracts import (
        opaque_repository_lock_id,
        opaque_run_lock_id,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result(
                pr_dict(
                    title="T",
                    body=append_owned_marker(
                        "b",
                        derive_content_bound_marker(
                            operation="create_or_update_pr",
                            target_kind="pr_body",
                            idempotency_key=effect.idempotency_key,
                            canonical_content=canonicalize_publication_text(title="T", body="b"),
                        ).marker_text,
                    ),
                    head_sha=SHA_B,
                )
            ),
        }
    )
    _gh_gateway(tmp_path, writes).create_or_update_pr(
        effect, run_id=RUN_ID, now=NOW, authorize=_Auth()
    )
    lock_path = repository_lock_path(repo)
    payload = lock_path.read_text(encoding="utf-8")
    assert RUN_ID not in payload
    assert str(repo.resolve()) not in payload
    assert opaque_run_lock_id(RUN_ID) in payload
    assert opaque_repository_lock_id(str(repo)) in payload


def test_write_executor_lock_contention_is_retryable_without_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.pr_review_v2.domain.events import EffectRetryableFailure

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    holder = FileLock(repository_lock_path(repo))
    holder.acquire(
        LockMetadata(
            pid=os.getpid(),
            run_id="run:holder",
            repository_path="repo:holder",
            started_at=NOW,
        )
    )
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="b")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result([])})
    gw = _gh_gateway(tmp_path, writes)
    auth = WH.FixedAuthority()

    class _DummyGit:
        def __getattr__(self, name: str):
            raise AssertionError(name)

    executor = WriteExecutor(
        git_gateway=_DummyGit(),  # type: ignore[arg-type]
        github_gateway=gw,
        github_policy=GitHubWritePolicy(repository_cwd=str(repo)),
    )
    result = executor.execute(
        effect, WH.token_for(effect), now=NOW, authority=auth, claim=WH.claim_for(effect)
    )
    holder.release()
    assert isinstance(result, EffectRetryableFailure)
    assert auth.calls == 0
    assert "create_pull_request" not in writes.method_names()


def test_effect_worker_lock_contention_is_retryable_and_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.pr_review_v2.domain.events import EffectRetryableFailure

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "e.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="lock"),
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
    auth = WH.FixedAuthority()
    writes: list[str] = []

    class _LockContendedGit:
        def commit(self, effect, *, run_id, now, authorize):
            del effect, run_id, now, authorize
            raise LockError("lock already held: /tmp/x (pid=1, run_id=run:x, repository=repo:y)")

        def __getattr__(self, name: str):
            raise AssertionError(name)

    class _NoGithub:
        def __getattr__(self, name: str):
            raise AssertionError(name)

    class _AuthorityForwardingExecutor:
        """WriteExecutor wrapper that records authority consultations."""

        def __init__(self) -> None:
            self._inner = WriteExecutor(
                git_gateway=_LockContendedGit(),  # type: ignore[arg-type]
                github_gateway=_NoGithub(),  # type: ignore[arg-type]
                github_policy=GitHubWritePolicy(repository_cwd=str(tmp_path / "repo")),
            )

        def execute(self, effect, token, *, now, authority, claim):
            def counting_authority(snapshot):
                auth.calls += 1
                return authority.check_authority(snapshot)

            class _Guard:
                def check_authority(self, snapshot):
                    return counting_authority(snapshot)

            result = self._inner.execute(effect, token, now=now, authority=_Guard(), claim=claim)
            writes.append(effect.kind)
            return result

    worker = EffectWorker(
        engine,
        _AuthorityForwardingExecutor(),  # type: ignore[arg-type]
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
    )
    step = worker.run_once(prepared.run_id)
    assert captured
    event = captured[0].event
    assert isinstance(event, EffectRetryableFailure)
    assert "lock" in event.error.safe_summary.lower()
    assert auth.calls == 0
    assert step.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }
