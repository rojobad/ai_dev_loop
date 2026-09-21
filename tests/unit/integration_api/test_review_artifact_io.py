"""Tests for confined Codex review artifact IO."""

from __future__ import annotations

import os
from pathlib import Path

from ai_dev_loop.integration_api.review_artifact_io import (
    read_confined_chunk,
    verify_confined_artifact,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.state import sha256_bytes


def test_c04_multichunk_prompt_reconstruction(tmp_path: Path) -> None:
    run_id = "run-chunks"
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    run_root.mkdir(parents=True)
    attempt_id = "att-" + "a" * 32
    rel = f"codex/reviews/01.{attempt_id}.prompt.txt"
    body = b"x" * (200_000)
    path = run_root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    digest = sha256_bytes(body)
    total = verify_confined_artifact(run_root, rel, expected_sha256=digest, expected_size=len(body))
    assert total == len(body)
    chunk_a, _ = read_confined_chunk(
        run_root,
        rel,
        expected_sha256=digest,
        expected_size=len(body),
        byte_offset=0,
        limit=65536,
    )
    chunk_b, _ = read_confined_chunk(
        run_root,
        rel,
        expected_sha256=digest,
        expected_size=len(body),
        byte_offset=65536,
        limit=65536,
    )
    assert chunk_a + chunk_b == body[:131072]


def test_c02_response_0644_under_confined_root_is_readable(tmp_path: Path) -> None:
    run_id = "run-umask"
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    run_root.mkdir(parents=True)
    attempt_id = "att-" + "b" * 32
    rel = f"codex/reviews/01.{attempt_id}.json"
    payload = b'{"has_actionable_findings":false}'
    path = run_root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    if os.name != "nt":
        os.chmod(path, 0o644)
    digest = sha256_bytes(payload)
    total = verify_confined_artifact(
        run_root,
        rel,
        expected_sha256=digest,
        max_bytes=1_048_576,
    )
    assert total == len(payload)
