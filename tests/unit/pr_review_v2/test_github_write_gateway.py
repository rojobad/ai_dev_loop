"""Phase 16.6 unit tests: GitHub mutation gateway + reconciliation proofs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    SHA_B,
    FakeGhWriteTransport,
    FakeReadTransport,
    binding,
    comment_connection,
    create_pr_effect,
    graphql_result,
    post_reply_effect,
    pr_dict,
    request_review_effect,
    resolve_thread_effect,
    rest_result,
    thread_comment_connection,
    thread_resolved_payload,
    transient_error,
    update_pr_text_effect,
    write_publication_text,
    write_reply_text,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    GitHubWritePolicy,
    WriteProofKind,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _gateway(tmp_path: Path, writes: FakeGhWriteTransport, reads: FakeReadTransport | None = None):
    art_root = tmp_path / "art"
    policy = GitHubWritePolicy(repository_cwd=str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir(exist_ok=True)
    return GitHubWriteGateway(
        policy=policy,
        write_transport=writes,
        read_transport=reads or FakeReadTransport(),
        input_reader=InputArtifactReader(art_root),
        write_evidence=WriteEvidenceStore(art_root),
    )


def _pr_marker(effect, *, title: str, body: str, operation: str = "create_or_update_pr"):
    return derive_content_bound_marker(
        operation=operation,
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title=title, body=body),
    )


# -- create / update PR -----------------------------------------------------


def test_create_pr_when_no_candidates(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    marker = _pr_marker(effect, title="My PR", body="pr body")
    body_with = append_owned_marker("pr body", marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result(
                pr_dict(title="My PR", body=body_with, head_sha=SHA_B)
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.create_or_update_pr(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is False
    assert result.outcome.kind == "pr_bound"
    assert auth.calls == 1
    assert "create_pull_request" in writes.method_names()


def test_update_pr_when_candidate_lacks_marker(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="Old", body="old body", head_sha=SHA_B)]
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    with pytest.raises(GhTransportError):
        gw.create_or_update_pr(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    assert "update_pull_request" not in writes.method_names()


def test_create_pr_idempotent_when_marker_present(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    marker = _pr_marker(effect, title="My PR", body="pr body")
    body_with = append_owned_marker("pr body", marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="My PR", body=body_with, head_sha=SHA_B)]
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.create_or_update_pr(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is True
    assert auth.calls == 0
    assert "create_pull_request" not in writes.method_names()
    assert "update_pull_request" not in writes.method_names()


def test_create_pr_multiple_candidates_blocks(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [
                    pr_dict(number=7, title="a", body="a", head_sha=SHA_B),
                    pr_dict(number=8, title="b", body="b", head_sha=SHA_B),
                ]
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    with pytest.raises(GhTransportError):
        gw.create_or_update_pr(
            create_pr_effect(publication_text_ref=pub),
            run_id=RUN_ID,
            now=NOW,
            authorize=_Auth(),
        )


def test_create_pr_transient_is_ambiguous(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    writes = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": transient_error(),
        }
    )
    gw = _gateway(tmp_path, writes)
    with pytest.raises(AmbiguousWriteError):
        gw.create_or_update_pr(
            create_pr_effect(publication_text_ref=pub),
            run_id=RUN_ID,
            now=NOW,
            authorize=_Auth(),
        )


def test_reconcile_pr_applied_absent_ambiguous(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="My PR", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    marker = _pr_marker(effect, title="My PR", body="pr body")
    body_with = append_owned_marker("pr body", marker.marker_text)

    applied = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="My PR", body=body_with, head_sha=SHA_B)]
            )
        }
    )
    proof = _gateway(tmp_path, applied).reconcile_create_or_update_pr(
        effect, run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.APPLIED

    absent = FakeGhWriteTransport(responses={"list_prs_by_head_base": rest_result([])})
    proof2 = _gateway(tmp_path, absent).reconcile_create_or_update_pr(
        effect, run_id=RUN_ID, now=NOW
    )
    assert proof2.proof is WriteProofKind.PROVEN_NOT_APPLIED

    human = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="Human", body="no owned marker", head_sha=SHA_B)]
            )
        }
    )
    proof3 = _gateway(tmp_path, human).reconcile_create_or_update_pr(effect, run_id=RUN_ID, now=NOW)
    assert proof3.proof is WriteProofKind.UNRESOLVED


# -- update PR text ---------------------------------------------------------


def test_update_pr_text_idempotent(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    marker = _pr_marker(effect, title="T", body="text body", operation="update_pr_text")
    body_with = append_owned_marker("text body", marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={"fetch_pr_text": rest_result(pr_dict(title="T", body=body_with, head_sha=SHA_B))}
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is True
    assert auth.calls == 0


def test_update_pr_text_writes_when_owned_preimage(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    marker = _pr_marker(effect, title="T", body="text body", operation="update_pr_text")
    body_with = append_owned_marker("text body", marker.marker_text)
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
            "update_pr_text": rest_result(pr_dict(title="T", body=body_with, head_sha=SHA_B)),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is False
    assert auth.calls == 1


def test_update_pr_text_human_edited_is_unresolved(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="T", body="human tampered body", head_sha=SHA_B)
            )
        }
    )
    proof = _gateway(tmp_path, writes).reconcile_update_pr_text(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED
    with pytest.raises(GhTransportError):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


# -- request review trigger -------------------------------------------------


def _trigger_marker() -> str:
    return build_opaque_trigger_marker(run_id="run-16-6", cycle_number=1)


def _issue_comment(marker: str, *, comment_id: str = "IC_1") -> dict:
    return {
        "id": comment_id,
        "databaseId": 101,
        "body": f"please review {html_comment_marker(marker)}",
        "createdAt": "2026-07-21T12:00:00Z",
        "author": {"login": "orchestrator"},
    }


def test_request_review_creates_trigger(tmp_path: Path) -> None:
    marker = _trigger_marker()
    reads = FakeReadTransport(issue_comment_pages=[graphql_result(comment_connection([]))])
    writes = FakeGhWriteTransport(
        responses={
            "create_issue_comment": rest_result(
                {
                    "id": "IC_9",
                    "body": f"x {html_comment_marker(marker)}",
                    "createdAt": "2026-07-21T12:01:00Z",
                },
                status=201,
            )
        }
    )
    gw = _gateway(tmp_path, writes, reads)
    auth = _Auth()
    result = gw.request_review(
        request_review_effect(marker=marker), run_id=RUN_ID, now=NOW, authorize=auth
    )
    assert result.already_applied is False
    assert result.outcome.kind == "review_trigger_confirmed"
    assert auth.calls == 1


def test_request_review_idempotent_when_marker_present(tmp_path: Path) -> None:
    marker = _trigger_marker()
    reads = FakeReadTransport(
        issue_comment_pages=[graphql_result(comment_connection([_issue_comment(marker)]))]
    )
    writes = FakeGhWriteTransport(responses={})
    gw = _gateway(tmp_path, writes, reads)
    auth = _Auth()
    result = gw.request_review(
        request_review_effect(marker=marker), run_id=RUN_ID, now=NOW, authorize=auth
    )
    assert result.already_applied is True
    assert auth.calls == 0
    assert writes.method_names() == []


def test_request_review_duplicate_blocks(tmp_path: Path) -> None:
    marker = _trigger_marker()
    reads = FakeReadTransport(
        issue_comment_pages=[
            graphql_result(
                comment_connection(
                    [_issue_comment(marker), _issue_comment(marker, comment_id="IC_2")]
                )
            )
        ]
    )
    gw = _gateway(tmp_path, FakeGhWriteTransport(), reads)
    with pytest.raises(GhTransportError):
        gw.request_review(
            request_review_effect(marker=marker), run_id=RUN_ID, now=NOW, authorize=_Auth()
        )


def test_reconcile_request_review(tmp_path: Path) -> None:
    marker = _trigger_marker()
    effect = request_review_effect(marker=marker)
    from datetime import UTC, datetime

    applied = FakeReadTransport(
        issue_comment_pages=[graphql_result(comment_connection([_issue_comment(marker)]))]
    )
    proof = _gateway(tmp_path, FakeGhWriteTransport(), applied).reconcile_request_review(
        effect, run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.APPLIED

    absent = FakeReadTransport(issue_comment_pages=[graphql_result(comment_connection([]))])
    proof2 = _gateway(tmp_path, FakeGhWriteTransport(), absent).reconcile_request_review(
        effect, run_id=RUN_ID, now=datetime(2026, 7, 21, tzinfo=UTC)
    )
    assert proof2.proof is WriteProofKind.PROVEN_NOT_APPLIED


# -- post thread reply ------------------------------------------------------


def _reply_marker(effect, text: str):
    return derive_content_bound_marker(
        operation="post_thread_reply",
        target_kind="thread_reply",
        idempotency_key=effect.idempotency_key,
        canonical_content=text,
    )


def test_post_thread_reply_creates_reply(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="my reply")
    effect = post_reply_effect(reply_ref=reply)
    marker = _reply_marker(effect, "my reply")
    body_with = append_owned_marker("my reply", marker.marker_text)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result(thread_comment_connection([])),
            "add_review_thread_reply": graphql_result(
                {
                    "data": {
                        "addPullRequestReviewThreadReply": {
                            "comment": {"id": "C1", "body": body_with}
                        }
                    }
                }
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.post_thread_reply(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is False
    assert result.outcome.kind == "thread_reply_confirmed"
    assert auth.calls == 1


def test_post_thread_reply_idempotent(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="my reply")
    effect = post_reply_effect(reply_ref=reply)
    marker = _reply_marker(effect, "my reply")
    body_with = append_owned_marker("my reply", marker.marker_text)
    node = {
        "id": "C1",
        "body": body_with,
        "createdAt": "2026-07-21T12:00:00Z",
        "author": {"login": "bot"},
    }
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False)),
            "fetch_thread_comments_page": graphql_result(thread_comment_connection([node])),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.post_thread_reply(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is True
    assert auth.calls == 0


def test_reconcile_thread_reply(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="my reply")
    effect = post_reply_effect(reply_ref=reply)
    marker = _reply_marker(effect, "my reply")
    body_with = append_owned_marker("my reply", marker.marker_text)
    node = {
        "id": "C1",
        "body": body_with,
        "createdAt": "2026-07-21T12:00:00Z",
        "author": {"login": "bot"},
    }

    applied = FakeGhWriteTransport(
        responses={"fetch_thread_comments_page": graphql_result(thread_comment_connection([node]))}
    )
    proof = _gateway(tmp_path, applied).reconcile_post_thread_reply(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.APPLIED

    absent = FakeGhWriteTransport(
        responses={"fetch_thread_comments_page": graphql_result(thread_comment_connection([]))}
    )
    proof2 = _gateway(tmp_path, absent).reconcile_post_thread_reply(effect, run_id=RUN_ID, now=NOW)
    assert proof2.proof is WriteProofKind.PROVEN_NOT_APPLIED


def test_reconcile_thread_reply_content_drift_unresolved(tmp_path: Path) -> None:
    reply = write_reply_text(tmp_path / "art", RUN_ID, text="my reply")
    effect = post_reply_effect(reply_ref=reply)
    marker = _reply_marker(effect, "my reply")
    tampered = append_owned_marker("different content", marker.marker_text)
    node = {
        "id": "C1",
        "body": tampered,
        "createdAt": "2026-07-21T12:00:00Z",
        "author": {"login": "bot"},
    }
    writes = FakeGhWriteTransport(
        responses={"fetch_thread_comments_page": graphql_result(thread_comment_connection([node]))}
    )
    proof = _gateway(tmp_path, writes).reconcile_post_thread_reply(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED


# -- resolve thread ---------------------------------------------------------


def test_resolve_thread_writes(tmp_path: Path) -> None:
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": [
                graphql_result(thread_resolved_payload(is_resolved=False)),
                graphql_result(thread_resolved_payload(is_resolved=False)),
                graphql_result(thread_resolved_payload(is_resolved=True)),
                graphql_result(thread_resolved_payload(is_resolved=True)),
            ],
            "resolve_review_thread": graphql_result(
                {
                    "data": {
                        "resolveReviewThread": {"thread": {"id": "THREAD_1", "isResolved": True}}
                    }
                }
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.resolve_thread(resolve_thread_effect(), run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is False
    assert auth.calls == 1


def test_resolve_thread_idempotent(tmp_path: Path) -> None:
    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=True))
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.resolve_thread(resolve_thread_effect(), run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is True
    assert auth.calls == 0


def test_reconcile_resolve_thread(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    resolved = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=True))
        }
    )
    proof = _gateway(tmp_path, resolved).reconcile_resolve_thread(
        resolve_thread_effect(), run_id=RUN_ID, now=NOW
    )
    assert proof.proof is WriteProofKind.APPLIED

    unresolved = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=False))
        }
    )
    proof2 = _gateway(tmp_path, unresolved).reconcile_resolve_thread(
        resolve_thread_effect(), run_id=RUN_ID, now=datetime(2026, 7, 21, tzinfo=UTC)
    )
    assert proof2.proof is WriteProofKind.PROVEN_NOT_APPLIED


def test_binding_helper_used() -> None:
    assert binding().pr_number == 7
