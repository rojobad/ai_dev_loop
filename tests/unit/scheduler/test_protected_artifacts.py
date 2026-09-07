"""Unit tests for scheduler protected artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
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
