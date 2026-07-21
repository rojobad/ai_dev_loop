"""Unit tests for protected observation artifact store."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_read_helpers import MARKER, binding

from ai_dev_loop.pr_review_v2.application.github_read import (
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedTriggerComment,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    resolve_run_relative_path,
    safe_run_directory_key,
)
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ArtifactStoreError,
    ReviewArtifactStore,
    sha256_text,
)


def _snapshot() -> ObservationSnapshot:
    return ObservationSnapshot(
        binding=binding(),
        cycle_number=1,
        poll_sequence=1,
        trigger_marker=MARKER,
        observed_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        evidence_kind=ObservationEvidenceKind.BOT_STILL_WAITING,
        trigger=ObservedTriggerComment(
            comment_id="101",
            author_login="orchestrator",
            created_at=datetime(2026, 7, 21, 11, 0, tzinfo=UTC),
            body_sha256=sha256_text("trigger"),
        ),
    )


def test_artifact_atomic_write_hash_and_permissions(tmp_path: Path) -> None:
    store = ReviewArtifactStore(tmp_path / "artifacts")
    ref = store.persist_observation_for_run(run_id="run-1", snapshot=_snapshot())
    assert isinstance(ref, ArtifactRef)
    assert not ref.relative_path.startswith("/")
    run_key = safe_run_directory_key("run-1")
    assert run_key != "run-1"
    path = tmp_path / "artifacts" / "runs" / run_key / ref.relative_path
    assert path.is_file()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600 or mode == 0o644  # some FS ignore chmod; still written
    manifest = store.read_and_verify(run_id="run-1", ref=ref, expected_binding=binding())
    assert manifest.evidence_kind is ObservationEvidenceKind.BOT_STILL_WAITING
    assert "token=" not in path.read_text(encoding="utf-8")


def test_artifact_rejects_traversal_and_hash_mismatch(tmp_path: Path) -> None:
    store = ReviewArtifactStore(tmp_path / "artifacts")
    ref = store.persist_observation_for_run(run_id="run-1", snapshot=_snapshot())
    with pytest.raises(ValueError):
        resolve_run_relative_path(
            tmp_path / "artifacts" / "runs" / safe_run_directory_key("run-1"), "../escape.json"
        )
    bad = ArtifactRef(relative_path=ref.relative_path, sha256="0" * 64)
    with pytest.raises(ArtifactStoreError):
        store.read_and_verify(run_id="run-1", ref=bad)


def test_competing_claimants_cannot_overwrite_accepted_observation(tmp_path: Path) -> None:
    """Expired vs replacement claimants write different observations for same cycle/poll.

    Content-addressed paths keep the accepted ArtifactRef immutable: the replacement
    writes a distinct object and must not replace the accepted bytes/hash.
    """

    store = ReviewArtifactStore(tmp_path / "artifacts")
    expired_snapshot = _snapshot()
    accepted = store.persist_observation_for_run(run_id="run-1", snapshot=expired_snapshot)
    replacement = ObservationSnapshot(
        binding=binding(),
        cycle_number=1,
        poll_sequence=1,
        trigger_marker=MARKER,
        observed_at=datetime(2026, 7, 21, 12, 5, tzinfo=UTC),
        evidence_kind=ObservationEvidenceKind.BOT_STILL_WAITING,
        trigger=ObservedTriggerComment(
            comment_id="101",
            author_login="orchestrator",
            created_at=datetime(2026, 7, 21, 11, 0, tzinfo=UTC),
            body_sha256=sha256_text("trigger-replacement"),
        ),
    )
    competing = store.persist_observation_for_run(run_id="run-1", snapshot=replacement)
    assert competing.relative_path != accepted.relative_path
    assert competing.sha256 != accepted.sha256
    verified = store.read_and_verify(run_id="run-1", ref=accepted, expected_binding=binding())
    assert verified.trigger.body_sha256 == sha256_text("trigger")
    run_root = tmp_path / "artifacts" / "runs" / safe_run_directory_key("run-1")
    accepted_bytes = (run_root / accepted.relative_path).read_bytes()
    assert hashlib.sha256(accepted_bytes).hexdigest() == accepted.sha256
    # Same content re-publish is idempotent and still does not mutate bytes.
    again = store.persist_observation_for_run(run_id="run-1", snapshot=expired_snapshot)
    assert again == accepted
    assert (run_root / accepted.relative_path).read_bytes() == accepted_bytes
