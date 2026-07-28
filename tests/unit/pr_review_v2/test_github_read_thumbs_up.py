"""Gateway and executor tests for fail-closed bot thumbs-up no-findings evidence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import (
    MARKER,
    FakeTransport,
    observe_effect,
    policy,
    queue_waiting,
    result_from_fixture,
    token_for,
)

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GhTransportResult,
)
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import (
    GhTransportError,
    GitHubReadGateway,
)
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ObservationArtifactManifest,
    ReviewArtifactStore,
)
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor

T_TRIGGER = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
T_PLUS1 = datetime(2026, 7, 21, 12, 2, 0, tzinfo=UTC)
T_EYES = datetime(2026, 7, 21, 12, 1, 0, tzinfo=UTC)


def _empty_threads() -> GhTransportResult:
    return GhTransportResult(
        http_status=200,
        headers=AllowlistedHeaders(),
        body_json={
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        },
        returncode=0,
    )


def _reaction_page(*items: dict) -> GhTransportResult:
    return GhTransportResult(
        http_status=200,
        headers=AllowlistedHeaders(),
        body_json=list(items),
        returncode=0,
    )


def _gateway(tmp_path: Path, transport: FakeTransport, **policy_kwargs) -> GitHubReadGateway:
    return GitHubReadGateway(
        policy=policy(**policy_kwargs),
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
    )


def _executor(tmp_path: Path, transport: FakeTransport, **policy_kwargs) -> GitHubReadExecutor:
    pol = policy(**policy_kwargs)
    return GitHubReadExecutor(
        gateway=_gateway(tmp_path, transport, **policy_kwargs),
        policy=pol,
        clock=FakeClock(datetime(2026, 7, 21, 12, 10, tzinfo=UTC)),
    )


def _queue_waiting_with_reactions(
    transport: FakeTransport,
    reactions: list[dict],
) -> None:
    queue_waiting(transport)
    transport.thread_pages.clear()
    transport.thread_pages.append(_empty_threads())
    transport.reaction_pages.append(_reaction_page(*reactions))


def test_thumbs_up_on_exact_trigger_completes_no_findings(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 99,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": T_PLUS1.isoformat().replace("+00:00", "Z"),
            }
        ],
    )
    executor = _executor(tmp_path, transport, accept_bot_thumbs_up=True)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.outcome.kind == "verified_no_findings"
    manifest = ReviewArtifactStore(tmp_path / "artifacts").read_and_verify(
        run_id=effect.run_id,
        ref=event.outcome.evidence.observation_ref,
    )
    assert manifest.no_findings_reaction is not None
    assert manifest.no_findings_reaction.rule_id == "accept_bot_thumbs_up"
    assert manifest.no_findings_reaction.trigger_comment_id == "101"
    assert manifest.no_findings is None


def test_eyes_reaction_never_completes_no_findings(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 1,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "eyes",
                "created_at": T_EYES.isoformat().replace("+00:00", "Z"),
            }
        ],
    )
    executor = _executor(tmp_path, transport, accept_bot_thumbs_up=True)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.outcome.kind == "bot_still_waiting"


def test_wrong_actor_thumbs_up_is_not_success(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 2,
                "user": {"login": "other-reviewer"},
                "content": "+1",
                "created_at": T_PLUS1.isoformat().replace("+00:00", "Z"),
            }
        ],
    )
    executor = _executor(tmp_path, transport, accept_bot_thumbs_up=True)
    event = executor.execute(
        observe_effect(), token_for(observe_effect()), now=datetime.now(tz=UTC)
    )
    assert event.outcome.kind == "bot_still_waiting"


def test_thumbs_up_before_trigger_timestamp_is_ignored(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 3,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T11:59:00Z",
            }
        ],
    )
    executor = _executor(tmp_path, transport, accept_bot_thumbs_up=True)
    event = executor.execute(
        observe_effect(), token_for(observe_effect()), now=datetime.now(tz=UTC)
    )
    assert event.outcome.kind == "bot_still_waiting"


def test_multiple_matching_thumbs_up_fail_closed(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 4,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:03:00Z",
            },
            {
                "id": 5,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:04:00Z",
            },
        ],
    )
    gateway = _gateway(tmp_path, transport, accept_bot_thumbs_up=True)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert exc.value.block is not None
    assert exc.value.block.kind.value == "contradictory_evidence"


def test_comment_and_reaction_no_findings_contradict(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(result_from_fixture("issue_comments_waiting_and_nofindings.txt"))
    transport.thread_pages.append(_empty_threads())
    transport.reaction_pages.append(
        _reaction_page(
            {
                "id": 6,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:06:00Z",
            }
        )
    )
    gateway = _gateway(
        tmp_path,
        transport,
        accepted_no_findings_prefixes=("No findings found.",),
        accept_bot_thumbs_up=True,
    )
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert exc.value.block is not None
    assert exc.value.block.kind.value == "contradictory_evidence"


def test_thumbs_up_without_timestamp_is_typed_fail_closed(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 7,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
            }
        ],
    )
    gateway = _gateway(tmp_path, transport, accept_bot_thumbs_up=True)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert exc.value.block is not None
    assert exc.value.block.kind.value == "malformed_evidence"


def test_thumbs_up_wrong_trigger_never_completes(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(transport, [])
    gateway = _gateway(tmp_path, transport, accept_bot_thumbs_up=True)
    snapshot, _ref = gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert snapshot.evidence_kind.value == "bot_still_waiting"
    assert snapshot.no_findings_reaction is None


def test_duplicate_reaction_id_is_malformed(tmp_path: Path) -> None:
    transport = FakeTransport()
    _queue_waiting_with_reactions(
        transport,
        [
            {
                "id": 8,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:06:00Z",
            },
            {
                "id": 8,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:07:00Z",
            },
        ],
    )
    gateway = _gateway(tmp_path, transport, accept_bot_thumbs_up=True)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert exc.value.block is not None
    assert exc.value.block.kind.value == "malformed_evidence"


def test_eligible_threads_with_thumbs_up_contradict(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    transport.thread_pages.clear()
    transport.thread_pages.append(result_from_fixture("review_threads_eligible.txt"))
    transport.reaction_pages.append(
        _reaction_page(
            {
                "id": 9,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "+1",
                "created_at": "2026-07-21T12:06:00Z",
            }
        )
    )
    gateway = _gateway(tmp_path, transport, accept_bot_thumbs_up=True)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(observe_effect(), observation_time=T_PLUS1)
    assert exc.value.block is not None
    assert exc.value.block.kind.value == "contradictory_evidence"


def test_old_observation_artifact_without_reaction_field_still_reads(tmp_path: Path) -> None:
    legacy = {
        "schema_version": 1,
        "sanitization_version": 1,
        "repository": "acme/demo",
        "pr_number": 7,
        "head_branch": "feature",
        "base_branch": "main",
        "head_sha": "b" * 40,
        "cycle_number": 1,
        "poll_sequence": 1,
        "trigger_marker": MARKER,
        "observed_at": T_TRIGGER.isoformat(),
        "evidence_kind": "bot_still_waiting",
        "trigger": {
            "comment_id": "101",
            "author_login": "orchestrator",
            "created_at": T_TRIGGER.isoformat(),
            "body_sha256": "a" * 64,
        },
        "eligible_threads": [],
        "reaction_ids": [],
        "source_hashes": {"trigger_body": "a" * 64},
    }
    raw = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    store = ReviewArtifactStore(tmp_path / "artifacts")
    run_root = store.root
    from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root

    ensure_run_artifact_root(run_root, "run-1")
    target = ensure_run_artifact_root(run_root, "run-1") / "observations" / "legacy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef

    manifest = store.read_and_verify(
        run_id="run-1",
        ref=ArtifactRef(relative_path="observations/legacy.json", sha256=digest),
    )
    assert manifest.no_findings_reaction is None
    assert (
        ObservationArtifactManifest.model_validate(json.loads(raw.decode())).no_findings_reaction
        is None
    )
