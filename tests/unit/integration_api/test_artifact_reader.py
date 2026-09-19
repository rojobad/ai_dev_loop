"""Unit tests for integration frozen artifact reader."""

from __future__ import annotations

import base64
import hashlib
import shutil
from pathlib import Path

import pytest

from ai_dev_loop.integration_api.artifact_reader import (
    FrozenArtifactReadError,
    read_frozen_run_artifact_chunk,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.validation import ARTIFACT_HARD_MAX_LIMIT
from ai_dev_loop.scheduler.infrastructure.paths import (
    readonly_confined_run_artifact_root,
    run_artifact_root,
)
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore


def test_read_chunk_verifies_hash_and_supports_eof(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifacts)
    content = "plan bytes with unicode: café\n".encode()
    digest = store.write_bytes(
        "run-1",
        "plan/plan.md",
        content,
        max_bytes=1024,
    ).sha256
    chunk = read_frozen_run_artifact_chunk(
        artifact_root=artifacts,
        run_id="run-1",
        artifact_kind="plan",
        relative_path="plan/plan.md",
        source_repository_path="docs/plans/sample-plan.md",
        expected_sha256=digest,
        byte_offset=0,
        limit=8,
    )
    assert chunk.has_more is True
    assert chunk.next_offset == 8
    decoded = base64.b64decode(chunk.content_base64)
    assert decoded == content[:8]

    eof = read_frozen_run_artifact_chunk(
        artifact_root=artifacts,
        run_id="run-1",
        artifact_kind="plan",
        relative_path="plan/plan.md",
        source_repository_path="docs/plans/sample-plan.md",
        expected_sha256=digest,
        byte_offset=len(content),
        limit=64,
    )
    assert eof.returned_bytes == 0
    assert eof.has_more is False
    assert eof.next_offset is None


def test_read_chunk_rejects_hash_mismatch(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifacts)
    store.write_bytes("run-1", "plan/plan.md", b"content", max_bytes=1024)
    with pytest.raises(FrozenArtifactReadError, match="hash mismatch"):
        read_frozen_run_artifact_chunk(
            artifact_root=artifacts,
            run_id="run-1",
            artifact_kind="plan",
            relative_path="plan/plan.md",
            source_repository_path="docs/plans/sample-plan.md",
            expected_sha256="b" * 64,
            byte_offset=0,
            limit=64,
        )


def test_read_chunk_rejects_offset_beyond_size(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifacts)
    digest = store.write_bytes("run-1", "plan/plan.md", b"abc", max_bytes=1024).sha256
    with pytest.raises(IntegrationApiError) as exc:
        read_frozen_run_artifact_chunk(
            artifact_root=artifacts,
            run_id="run-1",
            artifact_kind="plan",
            relative_path="plan/plan.md",
            source_repository_path="docs/plans/sample-plan.md",
            expected_sha256=digest,
            byte_offset=10,
            limit=64,
        )
    assert exc.value.code == "INVALID_ARGUMENT"


def test_read_chunk_rejects_symlink_escape(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    root = run_artifact_root(artifacts, "run-1")
    root.mkdir(parents=True)
    plan_dir = root / "plan"
    plan_dir.mkdir()
    (plan_dir / "plan.md").symlink_to(outside)
    digest = "a" * 64
    with pytest.raises(FrozenArtifactReadError):
        read_frozen_run_artifact_chunk(
            artifact_root=artifacts,
            run_id="run-1",
            artifact_kind="plan",
            relative_path="plan/plan.md",
            source_repository_path="docs/plans/sample-plan.md",
            expected_sha256=digest,
            byte_offset=0,
            limit=64,
        )


def test_readonly_confined_root_rejects_symlink_run_directory(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifacts)
    store.write_bytes("run-1", "plan/plan.md", b"plan", max_bytes=1024)
    run_root = run_artifact_root(artifacts, "run-1")
    outside = tmp_path / "outside"
    shutil.move(str(run_root), outside)
    run_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        readonly_confined_run_artifact_root(artifacts, "run-1")


def test_read_chunk_empty_artifact(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    plan_path = run_artifact_root(artifacts, "run-1") / "plan" / "plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(b"")
    set_sensitive_file_mode(plan_path)
    digest = hashlib.sha256(b"").hexdigest()
    chunk = read_frozen_run_artifact_chunk(
        artifact_root=artifacts,
        run_id="run-1",
        artifact_kind="plan",
        relative_path="plan/plan.md",
        source_repository_path="docs/plans/sample-plan.md",
        expected_sha256=digest,
        byte_offset=0,
        limit=64,
    )
    assert chunk.returned_bytes == 0
    assert chunk.total_bytes == 0
    assert chunk.has_more is False
    assert chunk.content_base64 == ""


def test_read_chunk_respects_hard_limit(tmp_path: Path) -> None:
    with pytest.raises(IntegrationApiError):
        read_frozen_run_artifact_chunk(
            artifact_root=tmp_path,
            run_id="run-1",
            artifact_kind="plan",
            relative_path="plan/plan.md",
            source_repository_path="docs/plans/sample-plan.md",
            expected_sha256="a" * 64,
            byte_offset=0,
            limit=ARTIFACT_HARD_MAX_LIMIT + 1,
        )
