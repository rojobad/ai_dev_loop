"""Unit tests for scheduler protected artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ai_dev_loop.paths import DIR_MODE
from ai_dev_loop.scheduler.infrastructure.paths import (
    run_artifact_root,
    sequence_artifact_root,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)


def test_write_and_verify_bytes(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    stored = store.write_text("run-1", "plan/plan.md", "plan bytes\n", max_bytes=1024)
    verified = store.read_verified_bytes(
        "run-1",
        "plan/plan.md",
        expected_sha256=stored.sha256,
    )
    assert verified == b"plan bytes\n"


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    root = run_artifact_root(tmp_path / "artifacts", "run-1")
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "plan"
    link.symlink_to(outside)
    with pytest.raises((ValueError, ProtectedArtifactError), match="symlink|escapes"):
        store.write_text("run-1", "plan/plan.md", "x", max_bytes=1024)


def test_sensitive_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    store.write_text("run-1", "prompts/cursor-initial.txt", "prompt", max_bytes=1024)
    path = run_artifact_root(tmp_path / "artifacts", "run-1") / "prompts/cursor-initial.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_hash_mismatch_rejected(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    stored = store.write_text("run-1", "plan/plan.md", "plan", max_bytes=1024)
    with pytest.raises(ProtectedArtifactError, match="hash mismatch"):
        store.read_verified_bytes(
            "run-1",
            "plan/plan.md",
            expected_sha256="0" * 64,
        )
    assert stored.sha256 != "0" * 64


def test_sequence_write_and_verify_bytes(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    stored = store.write_sequence_text(
        "seq-1",
        "manifest/original.yaml",
        "manifest bytes\n",
        max_bytes=1024,
    )
    verified = store.read_sequence_verified_bytes(
        "seq-1",
        "manifest/original.yaml",
        expected_sha256=stored.sha256,
    )
    assert verified == b"manifest bytes\n"
    assert sequence_artifact_root(tmp_path / "artifacts", "seq-1").exists()


def test_sequence_write_or_verify_accepts_identical_replay(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    first = store.write_sequence_text_or_verify(
        "seq-1",
        "entries/01/plan/plan.md",
        "plan",
        max_bytes=1024,
    )
    second = store.write_sequence_text_or_verify(
        "seq-1",
        "entries/01/plan/plan.md",
        "plan",
        max_bytes=1024,
    )
    assert first.sha256 == second.sha256


def test_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    link = tmp_path / "artifacts-link"
    link.symlink_to(outside)
    store = ProtectedArtifactStore(link)
    with pytest.raises(ValueError, match="symlink"):
        store.write_text("run-1", "plan/plan.md", "plan", max_bytes=1024)


def test_rejects_nested_symlink_inside_protected_root(tmp_path: Path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    root = run_artifact_root(tmp_path / "artifacts", "run-1")
    root.mkdir(parents=True)
    internal = root / "internal"
    internal.mkdir()
    (internal / "target.txt").write_text("target", encoding="utf-8")
    link = root / "entries"
    link.symlink_to(internal)
    with pytest.raises(ValueError, match="symlink"):
        store.write_text("run-1", "entries/01/plan/plan.md", "plan", max_bytes=1024)


def test_secures_existing_run_root_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    artifact_root = tmp_path / "artifacts"
    root = run_artifact_root(artifact_root, "run-1")
    root.mkdir(parents=True)
    os.chmod(root, 0o755)
    store = ProtectedArtifactStore(artifact_root)
    store.write_text("run-1", "plan/plan.md", "plan", max_bytes=1024)
    assert stat.S_IMODE(root.stat().st_mode) == DIR_MODE


def test_secures_existing_sequence_root_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    artifact_root = tmp_path / "artifacts"
    root = sequence_artifact_root(artifact_root, "seq-1")
    root.mkdir(parents=True)
    os.chmod(root, 0o755)
    store = ProtectedArtifactStore(artifact_root)
    store.write_sequence_text(
        "seq-1",
        "manifest/original.yaml",
        "manifest",
        max_bytes=1024,
    )
    assert stat.S_IMODE(root.stat().st_mode) == DIR_MODE


def test_write_or_verify_rejects_unsafe_existing_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    stored = store.write_sequence_text(
        "seq-1",
        "manifest/original.yaml",
        "manifest",
        max_bytes=1024,
    )
    path = sequence_artifact_root(tmp_path / "artifacts", "seq-1") / "manifest/original.yaml"
    os.chmod(path, 0o644)
    with pytest.raises(ProtectedArtifactError, match="unsafe permissions"):
        store.write_sequence_text_or_verify(
            "seq-1",
            "manifest/original.yaml",
            "manifest",
            max_bytes=1024,
        )
    assert stored.sha256
