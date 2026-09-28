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
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.scheduler.infrastructure.paths import (
    readonly_confined_run_artifact_root,
    run_artifact_root,
)
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


def _sample_completion_report_bytes(sequence_id: str) -> bytes:
    import json

    from ai_dev_loop.scheduler.domain.sequence import SequenceCompletionReport

    report = SequenceCompletionReport(
        sequence_id=sequence_id,
        sequence_name="sample",
        base_head_sha256="a" * 40,
        base_head_sha256_prefix="a" * 12,
        finalized_at="2026-01-01T00:00:00.000000Z",
        final_run_id="run-1",
        final_run_id_prefix="run-1",
        final_outcome="completed",
        final_staged_patch_sha256="b" * 64,
        final_staged_patch_sha256_prefix="b" * 12,
        phases=(),
    )
    return json.dumps(report.model_dump(mode="json"), separators=(",", ":")).encode()


def test_sequence_report_validation_rejects_identity_and_bound_digest(tmp_path: Path) -> None:
    import hashlib

    from ai_dev_loop.integration_api.artifact_reader import (
        FrozenArtifactReadError,
        load_validated_published_sequence_report,
        validate_published_sequence_report_bytes,
    )
    from ai_dev_loop.scheduler.infrastructure.paths import sequence_artifact_root

    artifacts = tmp_path / "artifacts"
    sequence_id = "seq-" + "a" * 32
    other_id = "seq-" + "c" * 32
    root = sequence_artifact_root(artifacts, sequence_id)
    report_dir = root / "reports"
    report_dir.mkdir(parents=True)
    raw = _sample_completion_report_bytes(other_id)
    (report_dir / "completion-v1.json").write_bytes(raw)
    with pytest.raises(FrozenArtifactReadError, match="identity"):
        validate_published_sequence_report_bytes(raw, sequence_id=sequence_id)
    good_raw = _sample_completion_report_bytes(sequence_id)
    good_path = report_dir / "completion-v1.json"
    good_path.write_bytes(good_raw)
    set_sensitive_file_mode(good_path)
    digest = hashlib.sha256(good_raw).hexdigest()
    with pytest.raises(FrozenArtifactReadError, match="hash mismatch"):
        load_validated_published_sequence_report(
            artifact_root=artifacts,
            sequence_id=sequence_id,
            expected_sha256="d" * 64,
        )
    validated = load_validated_published_sequence_report(
        artifact_root=artifacts,
        sequence_id=sequence_id,
        expected_sha256=digest,
    )
    assert validated.integrity == "ledger_sha256_bound"
    unbound = validate_published_sequence_report_bytes(good_raw, sequence_id=sequence_id)
    assert unbound.integrity == "schema_validated"
    assert unbound.sha256 == digest


def test_sequence_report_chunk_reads_nonzero_offset(tmp_path: Path) -> None:
    import hashlib

    from ai_dev_loop.integration_api.artifact_reader import (
        chunk_validated_sequence_report,
        validate_published_sequence_report_bytes,
    )
    from ai_dev_loop.scheduler.infrastructure.paths import sequence_artifact_root

    artifacts = tmp_path / "artifacts"
    sequence_id = "seq-" + "b" * 32
    root = sequence_artifact_root(artifacts, sequence_id)
    report_dir = root / "reports"
    report_dir.mkdir(parents=True)
    raw = _sample_completion_report_bytes(sequence_id)
    (report_dir / "completion-v1.json").write_bytes(raw)
    validated = validate_published_sequence_report_bytes(raw, sequence_id=sequence_id)
    chunk = chunk_validated_sequence_report(
        validated,
        sequence_id=sequence_id,
        byte_offset=10,
        limit=20,
    )
    assert chunk.returned_bytes == 20
    assert chunk.byte_offset == 10
    assert chunk.sha256 == hashlib.sha256(raw).hexdigest()


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
