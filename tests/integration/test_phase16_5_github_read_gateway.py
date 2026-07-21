"""Phase 16.5 GitHub read gateway integration via fake transport."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.unit.pr_review_v2.github_read_helpers import (
    FakeTransport,
    observe_effect,
    policy,
    queue_waiting,
)

from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore


def test_gateway_waiting_observation_persists_protected_artifact(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    gateway = GitHubReadGateway(
        policy=policy(),
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
    )
    snapshot, ref = gateway.observe_with_artifact(
        observe_effect(),
        observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
    )
    assert snapshot.evidence_kind.value == "bot_still_waiting"
    assert ref.sha256
    assert "/poll-" in ref.relative_path
    assert ref.relative_path.endswith(f"{ref.sha256}.json")
    raw = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "artifacts").rglob("*.json"))
    assert "No findings found" not in raw  # raw body not persisted for waiting path
    assert "ghp_" not in raw
    assert all(op[0] != "rest_get" for op in transport.calls)
