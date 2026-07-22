"""Focused Phase 16.6 unit tests: artifacts, transports, gateways, executors."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from tests.unit.pr_review_v2 import write_helpers as H

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    WriteAuthorityStatus,
    assert_no_force_argv,
    derive_content_bound_marker,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import (
    DefaultGhWriteProcessRunner,
    GhWriteTransport,
)
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitProcessOutcome,
    GitWriteTransport,
    extract_remote_nwo,
)
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import (
    WriteEvidenceStore,
)
from ai_dev_loop.pr_review_v2.workers.effect_executor_router import (
    EffectExecutorRouter,
    UnsupportedRoutedEffectError,
)
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

# ------------------------------------------------------------------ artifacts


def test_input_reader_verifies_hash_and_rejects_mismatch(tmp_path):
    reader = InputArtifactReader(tmp_path)
    ref = H.write_artifact(tmp_path, H.RUN_ID, "artifacts/patch.bin", b"diff --git\n")
    assert reader.read_patch_bytes(run_id=H.RUN_ID, ref=ref) == b"diff --git\n"

    bad = ArtifactRef(relative_path="artifacts/patch.bin", sha256="0" * 64)
    with pytest.raises(InputArtifactError):
        reader.read_patch_bytes(run_id=H.RUN_ID, ref=bad)


def test_input_reader_rejects_symlink_escape(tmp_path):
    reader = InputArtifactReader(tmp_path)
    from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root

    run_root = ensure_run_artifact_root(tmp_path, H.RUN_ID)
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"secret")
    (run_root / "artifacts").mkdir(parents=True, exist_ok=True)
    link = run_root / "artifacts" / "escape.txt"
    link.symlink_to(outside)
    linked = ArtifactRef(relative_path="artifacts/escape.txt", sha256=H.sha256_hex(b"secret"))
    with pytest.raises(InputArtifactError):
        reader.read_reply_text(run_id=H.RUN_ID, ref=linked)


def test_input_reader_commit_and_publication_schema(tmp_path):
    reader = InputArtifactReader(tmp_path)
    cm = H.write_commit_message(tmp_path, H.RUN_ID, "subject", "body")
    parsed = reader.read_commit_message(run_id=H.RUN_ID, ref=cm)
    assert parsed.subject == "subject"
    pub = H.write_publication(tmp_path, H.RUN_ID, "title", "body")
    assert reader.read_publication_text(run_id=H.RUN_ID, ref=pub).title == "title"


def test_input_reader_rejects_group_writable(tmp_path):
    reader = InputArtifactReader(tmp_path)
    ref = H.write_artifact(tmp_path, H.RUN_ID, "artifacts/patch.bin", b"data")
    from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root

    target = run_artifact_root(tmp_path, H.RUN_ID) / "artifacts" / "patch.bin"
    os.chmod(target, 0o666)
    with pytest.raises(InputArtifactError):
        reader.read_patch_bytes(run_id=H.RUN_ID, ref=ref)


def test_write_evidence_collision_safe(tmp_path):
    from ai_dev_loop.pr_review_v2.application.write_contracts import TriggerEvidenceArtifact

    store = WriteEvidenceStore(tmp_path)
    artifact = TriggerEvidenceArtifact(
        marker="adl-v1:" + "a" * 64,
        comment_id="123",
        created_at="2026-07-21T12:00:00Z",
        body_sha256="a" * 64,
        head_sha=H.SHA_COMMIT,
        pr_number=7,
        repository="acme/demo",
    )
    ref1 = store.persist_trigger_evidence(run_id=H.RUN_ID, artifact=artifact)
    ref2 = store.persist_trigger_evidence(run_id=H.RUN_ID, artifact=artifact)
    assert ref1.sha256 == ref2.sha256


# ------------------------------------------------------------------ markers


def test_trigger_marker_is_opaque_no_raw_identity():
    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    assert "run-1" not in marker
    assert marker == build_opaque_trigger_marker(run_id="run-1", cycle_number=1)


def test_content_bound_marker_distinct_by_operation_and_content():
    a = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="k",
        canonical_content="body",
    )
    b = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="k",
        canonical_content="body",
    )
    c = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="k",
        canonical_content="different",
    )
    assert a.marker_text != b.marker_text
    assert a.marker_text != c.marker_text
    assert "k" not in a.marker_text


def test_assert_no_force_argv_rejects_force():
    assert_no_force_argv(["git", "push", "origin", "b:refs/heads/x"])
    with pytest.raises(ValueError):
        assert_no_force_argv(["git", "push", "--force", "origin", "b:refs/heads/x"])
    with pytest.raises(ValueError):
        assert_no_force_argv(["git", "push", "--force-with-lease", "origin", "b:x"])


# ------------------------------------------------------------------ git transport


@dataclass
class ScriptedGitRunner:
    outcomes: list[GitProcessOutcome] = field(default_factory=list)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def run(self, args, *, cwd, timeout, env, stdin_text=None):
        self.calls.append((list(args), stdin_text))
        assert "SSH_AUTH_SOCK" not in " ".join(args)
        return self.outcomes.pop(0)

    def run_bytes(self, args, *, cwd, timeout, env):
        outcome = self.run(args, cwd=cwd, timeout=timeout, env=env, stdin_text=None)
        return (
            outcome.returncode,
            outcome.stdout.encode("utf-8"),
            outcome.stderr.encode("utf-8"),
            outcome.timed_out,
        )


def _ok(stdout: str = "") -> GitProcessOutcome:
    return GitProcessOutcome(returncode=0, stdout=stdout, stderr="", timed_out=False, argv=("git",))


def test_git_transport_commit_uses_stdin_and_no_force():
    runner = ScriptedGitRunner(outcomes=[_ok()])
    transport = GitWriteTransport(
        repository_cwd="/tmp/repo", per_call_timeout_seconds=5.0, runner=runner
    )
    transport.commit_with_message_stdin("subject\n\nbody\n")
    argv, stdin = runner.calls[0]
    assert argv[:2] == ["git", "commit"]
    assert "--file" in argv and argv[-1] == "-"
    assert stdin == "subject\n\nbody\n"
    # message never appears in argv
    assert not any("subject" in tok for tok in argv)


def test_git_transport_push_builds_refspec_and_rejects_force():
    runner = ScriptedGitRunner(outcomes=[_ok()])
    transport = GitWriteTransport(
        repository_cwd="/tmp/repo", per_call_timeout_seconds=5.0, runner=runner
    )
    transport.push_non_force(remote_name="origin", commit_sha=H.SHA_COMMIT, remote_ref="feature")
    argv, _ = runner.calls[0]
    assert argv == ["git", "push", "origin", f"{H.SHA_COMMIT}:refs/heads/feature"]


def test_git_transport_read_remote_ref_parses_ls_remote():
    stdout = f"{H.SHA_COMMIT}\trefs/heads/feature\n"
    runner = ScriptedGitRunner(outcomes=[_ok(stdout)])
    transport = GitWriteTransport(
        repository_cwd="/tmp/repo", per_call_timeout_seconds=5.0, runner=runner
    )
    obs = transport.read_remote_ref("origin", "feature")
    assert obs.sha == H.SHA_COMMIT
    assert obs.complete


def test_extract_remote_nwo():
    assert extract_remote_nwo("git@github.com:acme/demo.git") == "acme/demo"
    assert extract_remote_nwo("https://x") is None


# ------------------------------------------------------------------ gh transport


@dataclass
class ScriptedGhRunner:
    outputs: list[tuple[int, str]] = field(default_factory=list)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def run(self, args, *, cwd, timeout, env=None, stdin_text=None):
        self.calls.append((list(args), stdin_text))

        @dataclass
        class _R:
            returncode: int
            stdout: str
            stderr: str = ""
            timed_out: bool = False

        rc, stdout = self.outputs.pop(0)
        return _R(returncode=rc, stdout=stdout)


def _include(status: int, body: str) -> str:
    return f"HTTP/2 {status}\nContent-Type: application/json\n\n{body}"


def test_gh_write_transport_create_pr_uses_stdin_json_and_post():
    runner = ScriptedGhRunner(outputs=[(0, _include(201, '{"number":7}'))])
    transport = GhWriteTransport(
        command="gh", cwd="/tmp/repo", per_call_timeout_seconds=5.0, runner=runner
    )
    result = transport.create_pull_request(
        owner="acme", name="demo", head="feature", base="main", title="t", body="b"
    )
    assert result.body_json == {"number": 7}
    argv, stdin = runner.calls[0]
    assert argv[:3] == ["gh", "api", "repos/acme/demo/pulls"]
    assert "POST" in argv and "--input" in argv and argv[-1] == "-"
    assert '"title": "t"' in stdin and '"body": "b"' in stdin


def test_gh_write_transport_graphql_reply_via_stdin():
    runner = ScriptedGhRunner(
        outputs=[
            (
                0,
                _include(
                    200, '{"data":{"addPullRequestReviewThreadReply":{"comment":{"id":"c1"}}}}'
                ),
            )
        ]
    )
    transport = GhWriteTransport(
        command="gh", cwd="/tmp/repo", per_call_timeout_seconds=5.0, runner=runner
    )
    transport.add_review_thread_reply(thread_id="T1", body="reply body")
    argv, stdin = runner.calls[0]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert "reply body" in stdin
    assert not any("reply body" in tok for tok in argv)


# ------------------------------------------------------------------ github write gateway


@dataclass
class FakeWriteTransport:
    pr_list: list = field(default_factory=list)
    created_pr: dict | None = None
    thread_comments: list[dict] = field(default_factory=list)
    thread_resolved: bool = False
    resolve_response: dict | None = None
    issue_comment_created: dict | None = None
    calls: list[str] = field(default_factory=list)

    def list_prs_by_head_base(self, **kw):
        self.calls.append("list")
        return H.gh_result(self.pr_list)

    def create_pull_request(self, **kw):
        self.calls.append("create_pr")
        return H.gh_result(self.created_pr)

    def update_pull_request(self, **kw):
        self.calls.append("update_pr")
        return H.gh_result(self.created_pr)

    def update_pr_text(self, **kw):
        return self.update_pull_request(**kw)

    def create_issue_comment(self, **kw):
        self.calls.append("issue_comment")
        return H.gh_result(self.issue_comment_created)

    def add_review_thread_reply(self, **kw):
        self.calls.append("reply")
        body = kw["body"]
        return H.gh_result(
            {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "c1", "body": body}}}}
        )

    def resolve_review_thread(self, **kw):
        self.calls.append("resolve")
        self.thread_resolved = True
        thread_id = str(kw.get("thread_id") or "THREAD_1")
        return H.gh_result(
            self.resolve_response
            or {"data": {"resolveReviewThread": {"thread": {"id": thread_id, "isResolved": True}}}}
        )

    def fetch_pr_text(self, **kw):
        self.calls.append("fetch_pr")
        return H.gh_result(self.created_pr)

    def fetch_branch_head_sha(self, **kw):
        self.calls.append("branch_sha")
        return H.gh_result({"ref": "refs/heads/feature", "object": {"sha": H.SHA_COMMIT}})

    def fetch_thread_comments_page(self, **kw):
        self.calls.append("thread_comments")
        thread_id = str(kw.get("thread_id") or "THREAD_1")
        return H.gh_result(
            {
                "data": {
                    "node": {
                        "id": thread_id,
                        "isResolved": self.thread_resolved,
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": self.thread_comments,
                        },
                    }
                }
            }
        )

    def fetch_thread_resolved(self, **kw):
        self.calls.append("thread_resolved")
        thread_id = str(kw.get("thread_id") or "THREAD_1")
        return H.gh_result(
            {
                "data": {
                    "node": {
                        "id": thread_id,
                        "isResolved": self.thread_resolved,
                        "pullRequest": {"number": 7},
                    }
                }
            }
        )


@dataclass
class FakeReadTransport:
    comment_pages: list[dict] = field(default_factory=list)

    def fetch_pull_request_identity(self, **kw):
        number = int(kw.get("number") or 7)
        return H.gh_result(
            {
                "data": {
                    "repository": {
                        "nameWithOwner": "acme/demo",
                        "pullRequest": {
                            "number": number,
                            "state": "OPEN",
                            "isCrossRepository": False,
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRefOid": H.SHA_COMMIT,
                        },
                    }
                }
            }
        )

    def fetch_issue_comments_page(self, **kw):
        page = self.comment_pages.pop(0)
        return H.gh_result(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": page,
                            }
                        }
                    }
                }
            }
        )

    def fetch_review_threads_page(self, **kw):
        raise AssertionError("not used")

    def fetch_issue_comment_reactions_page(self, **kw):
        raise AssertionError("not used")


def _gateway(tmp_path, write_transport, read_transport):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return GitHubWriteGateway(
        policy=H.github_policy(repository_cwd=str(repo)),
        write_transport=write_transport,
        read_transport=read_transport,
        input_reader=InputArtifactReader(tmp_path),
        write_evidence=WriteEvidenceStore(tmp_path),
    )


def test_trigger_zero_match_writes_once(tmp_path):
    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    write = FakeWriteTransport(
        issue_comment_created={
            "id": "999",
            "body": f"{needle}\n@codex review\n",
            "createdAt": "2026-07-21T12:00:00Z",
        }
    )
    read = FakeReadTransport(comment_pages=[[]])
    gateway = _gateway(tmp_path, write, read)
    calls = []
    result = gateway.request_review(
        effect, run_id="run-1", now=H.NOW, authorize=lambda: calls.append("auth")
    )
    assert result.already_applied is False
    assert "issue_comment" in write.calls
    assert calls == ["auth"]


def test_trigger_one_match_is_idempotent(tmp_path):
    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    read = FakeReadTransport(
        comment_pages=[
            [
                {
                    "id": "5",
                    "body": f"{needle}\n@codex review",
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "x"},
                }
            ]
        ]
    )
    write = FakeWriteTransport()
    gateway = _gateway(tmp_path, write, read)
    result = gateway.request_review(
        effect, run_id="run-1", now=H.NOW, authorize=lambda: pytest.fail("no write")
    )
    assert result.already_applied is True
    assert "issue_comment" not in write.calls


def test_trigger_duplicate_blocks(tmp_path):
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError

    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    dup = {
        "id": "5",
        "body": f"{needle} x",
        "createdAt": "2026-07-21T12:00:00Z",
        "author": {"login": "x"},
    }
    read = FakeReadTransport(
        comment_pages=[
            [
                dup,
                {
                    "id": "6",
                    "body": f"{needle} y",
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "y"},
                },
            ]
        ]
    )
    gateway = _gateway(tmp_path, FakeWriteTransport(), read)
    with pytest.raises(GhTransportError):
        gateway.request_review(effect, run_id="run-1", now=H.NOW, authorize=lambda: None)


def test_resolve_thread_idempotent_when_already_resolved(tmp_path):
    effect = H.resolve_thread_effect()
    write = FakeWriteTransport(thread_resolved=True)
    gateway = _gateway(tmp_path, write, FakeReadTransport())
    result = gateway.resolve_thread(
        effect, run_id="run-1", now=H.NOW, authorize=lambda: pytest.fail("no write")
    )
    assert result.already_applied is True


def test_resolve_thread_writes_when_unresolved(tmp_path):
    effect = H.resolve_thread_effect()
    write = FakeWriteTransport(thread_resolved=False)
    gateway = _gateway(tmp_path, write, FakeReadTransport())
    calls = []
    result = gateway.resolve_thread(
        effect, run_id="run-1", now=H.NOW, authorize=lambda: calls.append("a")
    )
    assert result.already_applied is False
    assert calls == ["a"]
    assert "resolve" in write.calls


# ------------------------------------------------------------------ write executor / router


def _write_executor(tmp_path, github_gateway=None, git_gateway=None):
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )

    if git_gateway is None:
        git_gateway = GitPublicationGateway(
            policy=H.git_policy(str(tmp_path)),
            transport=H.FakeGitTransport(root=str(tmp_path)),
            input_reader=InputArtifactReader(tmp_path),
        )
    return WriteExecutor(
        git_gateway=git_gateway,
        github_gateway=github_gateway,
        github_policy=H.github_policy(),
    )


def test_write_executor_authority_rejection_zero_writes(tmp_path):
    from ai_dev_loop.pr_review_v2.application.write_contracts import AuthorityLostError

    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    write = FakeWriteTransport(
        issue_comment_created={"id": "1", "body": "x", "createdAt": "2026-07-21T12:00:00Z"}
    )
    read = FakeReadTransport(comment_pages=[[]])
    gateway = _gateway(tmp_path, write, read)
    executor = _write_executor(tmp_path, github_gateway=gateway)
    authority = H.FixedAuthority(status=WriteAuthorityStatus.REJECTED)
    # AuthorityLostError propagates so EffectWorker can set lease_authority_lost=True.
    with pytest.raises(AuthorityLostError):
        executor.execute(
            effect, H.token_for(effect), now=H.NOW, authority=authority, claim=H.claim_for(effect)
        )
    assert "issue_comment" not in write.calls
    assert authority.calls == 1


def test_write_executor_success_maps_to_effect_succeeded(tmp_path):
    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    write = FakeWriteTransport(
        issue_comment_created={
            "id": "9",
            "body": f"{needle}\n@codex review",
            "createdAt": "2026-07-21T12:00:00Z",
        }
    )
    read = FakeReadTransport(comment_pages=[[]])
    gateway = _gateway(tmp_path, write, read)
    executor = _write_executor(tmp_path, github_gateway=gateway)
    event = executor.execute(
        effect,
        H.token_for(effect),
        now=H.NOW,
        authority=H.FixedAuthority(),
        claim=H.claim_for(effect),
    )
    assert isinstance(event, EffectSucceeded)
    assert event.outcome.kind == "review_trigger_confirmed"


def test_router_rejects_local_effect_with_zero_calls(tmp_path):
    from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect

    local = GeneratePublicationTextEffect(
        effect_id="pr-review:run-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-1:cycle:01:generate_publication_text",
        run_id="run-1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=H.REPO,
        bound_head_sha=H.SHA_PARENT,
        evidence_ref=ArtifactRef(relative_path="artifacts/ev.json", sha256="1" * 64),
        patch_ref=ArtifactRef(relative_path="artifacts/patch.bin", sha256="2" * 64),
    )

    class _ReadExec:
        def execute(self, *a, **k):
            raise AssertionError("no read")

    router = EffectExecutorRouter(
        read_executor=_ReadExec(),
        write_executor=_write_executor(tmp_path, github_gateway=None),
        reconcile_executor=object(),
    )
    with pytest.raises(UnsupportedRoutedEffectError):
        router.execute(local, H.token_for(local), now=H.NOW, authority=None, claim=None)


def _reconcile_executor(tmp_path, github_gateway):
    from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import (
        GitPublicationGateway,
    )
    from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor

    git_gateway = GitPublicationGateway(
        policy=H.git_policy(str(tmp_path)),
        transport=H.FakeGitTransport(root=str(tmp_path)),
        input_reader=InputArtifactReader(tmp_path),
    )
    return ReconcileWriteExecutor(
        git_gateway=git_gateway,
        github_gateway=github_gateway,
        github_policy=H.github_policy(),
    )


def test_reconcile_trigger_applied_maps_to_reconciliation_resolved(tmp_path):
    from ai_dev_loop.pr_review_v2.domain.common import ReconciliationResolutionKind

    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    original = H.trigger_effect(marker)
    reconcile = H.reconcile_effect(original)
    needle = html_comment_marker(marker)
    read = FakeReadTransport(
        comment_pages=[
            [
                {
                    "id": "5",
                    "body": f"{needle}\n@codex review",
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "x"},
                }
            ]
        ]
    )
    gateway = _gateway(tmp_path, FakeWriteTransport(), read)
    executor = _reconcile_executor(tmp_path, gateway)
    event = executor.execute(
        reconcile,
        H.token_for(reconcile),
        now=H.NOW,
        authority=H.FixedAuthority(),
        claim=H.claim_for(reconcile),
    )
    assert isinstance(event, EffectSucceeded)
    assert event.outcome.kind == "reconciliation_resolved"
    assert event.outcome.resolution is ReconciliationResolutionKind.APPLIED
    assert event.outcome.original_effect_id == original.effect_id


def test_reconcile_trigger_absent_is_proven_not_applied(tmp_path):
    from ai_dev_loop.pr_review_v2.domain.common import ReconciliationResolutionKind

    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    original = H.trigger_effect(marker)
    reconcile = H.reconcile_effect(original)
    read = FakeReadTransport(comment_pages=[[]])
    gateway = _gateway(tmp_path, FakeWriteTransport(), read)
    executor = _reconcile_executor(tmp_path, gateway)
    event = executor.execute(
        reconcile,
        H.token_for(reconcile),
        now=H.NOW,
        authority=H.FixedAuthority(),
        claim=H.claim_for(reconcile),
    )
    assert isinstance(event, EffectSucceeded)
    assert event.outcome.resolution is ReconciliationResolutionKind.PROVEN_NOT_APPLIED
    assert event.outcome.next_attempt_at is not None
    assert event.outcome.next_attempt_at > H.NOW


def test_default_gh_write_runner_is_constructible():
    assert isinstance(DefaultGhWriteProcessRunner(), DefaultGhWriteProcessRunner)


# reference imports to keep flake happy for optional event types
_ = (EffectBlocked, EffectRetryableFailure, WriteOutcomeUncertain)
