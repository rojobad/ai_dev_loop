"""Phase 16.6 integration: GitHub writes through the executor with fake gh transport.

Drives the full effect -> ``WriteExecutor`` -> ``GitHubWriteGateway`` path against a
fake ``gh`` transport (no network). Asserts opaque/content-bound markers carry no raw
run/session/claim/owner identity, that a 16.5-style observation would find the opaque
trigger marker, that repeated writes are idempotent with no second mutation, and that
the router refuses the LOCAL publication-text effect with zero side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.unit.pr_review_v2.github_write_helpers import (
    REPO,
    RUN_ID,
    SHA_B,
    FakeAuthority,
    FakeGhWriteTransport,
    FakeReadTransport,
    claim_for,
    comment_connection,
    create_pr_effect,
    graphql_result,
    pr_dict,
    request_review_effect,
    resolve_thread_effect,
    rest_result,
    token_for,
    write_publication_text,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect
from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.workers.effect_executor_router import (
    EffectExecutorRouter,
    UnsupportedRoutedEffectError,
)
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

_SENSITIVE = ("run-16-6", "owner-a", "claim-1", "dispatch-1", "session")
_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _gateway(tmp_path: Path, writes: FakeGhWriteTransport, reads: FakeReadTransport | None = None):
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


def _executor(gateway: GitHubWriteGateway, *, repo_cwd: str) -> WriteExecutor:
    return WriteExecutor(
        git_gateway=_DummyGit(),  # type: ignore[arg-type]
        github_gateway=gateway,
        github_policy=GitHubWritePolicy(repository_cwd=repo_cwd),
    )


class _DummyGit:
    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - not used here
        raise AssertionError("git gateway must not be used for GitHub write tests")


def _run(effect, gateway, *, authority: FakeAuthority | None = None):
    authority = authority or FakeAuthority()
    return _executor(gateway, repo_cwd=gateway._policy.repository_cwd).execute(  # noqa: SLF001
        effect,
        token_for(effect),
        now=_NOW,
        authority=authority,
        claim=claim_for(effect),
    )


# -- opaque markers --------------------------------------------------------


def test_pr_marker_is_opaque_and_content_bound(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="Title", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title="Title", body="pr body"),
    )
    for banned in _SENSITIVE:
        assert banned not in marker.marker_text
    # Different content yields a different marker (content-bound).
    other = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title="Title", body="different body"),
    )
    assert other.marker_text != marker.marker_text
    assert marker.marker_text.startswith("adl-v2:")


def test_trigger_marker_is_opaque(tmp_path: Path) -> None:
    marker = build_opaque_trigger_marker(run_id="run-16-6", cycle_number=1)
    for banned in _SENSITIVE:
        assert banned not in marker


# -- create PR through the executor ----------------------------------------


def test_create_pr_through_executor_and_idempotent(tmp_path: Path) -> None:
    pub = write_publication_text(tmp_path / "art", RUN_ID, title="Title", body="pr body")
    effect = create_pr_effect(publication_text_ref=pub)
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title="Title", body="pr body"),
    )
    body_with = append_owned_marker("pr body", marker.marker_text)

    first = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result([]),
            "create_pull_request": rest_result(
                pr_dict(title="Title", body=body_with, head_sha=SHA_B)
            ),
        }
    )
    auth = FakeAuthority()
    result = _run(effect, _gateway(tmp_path, first), authority=auth)
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.kind == "pr_bound"
    assert auth.call_count == 1
    assert "create_pull_request" in first.method_names()

    # Idempotent replay: the PR already carries the owned marker; no second write.
    second = FakeGhWriteTransport(
        responses={
            "list_prs_by_head_base": rest_result(
                [pr_dict(title="Title", body=body_with, head_sha=SHA_B)]
            )
        }
    )
    auth2 = FakeAuthority()
    replay = _run(effect, _gateway(tmp_path, second), authority=auth2)
    assert isinstance(replay, EffectSucceeded)
    assert auth2.call_count == 0  # idempotent: authority not consulted, no write
    assert "create_pull_request" not in second.method_names()


# -- trigger marker found by 16.5-style observation ------------------------


def test_trigger_written_and_observable(tmp_path: Path) -> None:
    marker = build_opaque_trigger_marker(run_id="run-16-6", cycle_number=1)
    needle = html_comment_marker(marker)
    created_body = f"{needle}\nplease review\n"
    reads = FakeReadTransport(issue_comment_pages=[graphql_result(comment_connection([]))])
    writes = FakeGhWriteTransport(
        responses={
            "create_issue_comment": rest_result(
                {"id": "IC_1", "body": created_body, "createdAt": "2026-07-21T12:00:00Z"},
                status=201,
            )
        }
    )
    effect = request_review_effect(marker=marker)
    result = _run(effect, _gateway(tmp_path, writes, reads))
    assert isinstance(result, EffectSucceeded)
    assert result.outcome.kind == "review_trigger_confirmed"

    # A Phase 16.5 observation searches for exactly this opaque marker needle.
    observe = FakeReadTransport(
        issue_comment_pages=[
            graphql_result(
                comment_connection(
                    [{"id": "IC_1", "body": created_body, "createdAt": "2026-07-21T12:00:00Z"}]
                )
            )
        ]
    )
    gw = _gateway(tmp_path, FakeGhWriteTransport(), observe)
    found = gw._find_marked_issue_comments(  # noqa: SLF001 - exercising 16.5 search path
        owner="acme", name="demo", number=7, needle=needle
    )
    assert len(found) == 1


# -- resolve thread idempotency through executor ---------------------------


def test_resolve_thread_idempotent_through_executor(tmp_path: Path) -> None:
    from tests.unit.pr_review_v2.github_write_helpers import thread_resolved_payload

    writes = FakeGhWriteTransport(
        responses={
            "fetch_thread_resolved": graphql_result(thread_resolved_payload(is_resolved=True))
        }
    )
    effect = resolve_thread_effect()
    auth = FakeAuthority()
    result = _run(effect, _gateway(tmp_path, writes), authority=auth)
    assert isinstance(result, EffectSucceeded)
    assert auth.call_count == 0  # already resolved -> no write, no authority call


# -- router refuses LOCAL publication-text effect --------------------------


def test_router_refuses_local_publication_text_effect() -> None:
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-16-6:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-16-6:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        evidence_ref=ArtifactRef(relative_path="artifacts/evidence.json", sha256="a" * 64),
        patch_ref=ArtifactRef(relative_path="artifacts/patch.bin", sha256="b" * 64),
    )
    router = EffectExecutorRouter(
        read_executor=_Boom(),  # type: ignore[arg-type]
        write_executor=_Boom(),  # type: ignore[arg-type]
        reconcile_executor=_Boom(),  # type: ignore[arg-type]
    )
    with pytest.raises(UnsupportedRoutedEffectError):
        router.execute(
            effect,
            token_for(effect),
            now=None,
            authority=FakeAuthority(),
            claim=claim_for(effect),
        )


class _Boom:
    def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("no executor should run for a LOCAL effect")
