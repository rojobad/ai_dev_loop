"""Phase 16.6 unit tests: protected hash-verifying input artifact readers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_write_helpers import RUN_ID, sha256_hex, write_artifact

from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactErrorKind,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    run_artifact_root,
)


def _reader(tmp_path: Path) -> InputArtifactReader:
    return InputArtifactReader(tmp_path / "art")


def test_read_patch_bytes_exact_hash(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/p.patch", b"diff --git a b\n")
    reader = _reader(tmp_path)
    assert reader.read_patch_bytes(run_id=RUN_ID, ref=ref) == b"diff --git a b\n"


def test_read_patch_rejects_empty(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/empty.patch", b"")
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    write_artifact(tmp_path / "art", RUN_ID, "artifacts/p.patch", b"real bytes")
    bad_ref = ArtifactRef(relative_path="artifacts/p.patch", sha256="0" * 64)
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=bad_ref)


def test_missing_artifact_is_rejected(tmp_path: Path) -> None:
    ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    ref = ArtifactRef(relative_path="artifacts/missing.patch", sha256="0" * 64)
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_size_cap_is_rejected(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/big.patch", b"x" * 100)
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref, max_bytes=10)


def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    run_root = ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    (run_root / "artifacts").mkdir(parents=True, exist_ok=True)
    external = tmp_path / "outside.patch"
    external.write_bytes(b"data")
    link = run_root / "artifacts" / "link.patch"
    link.symlink_to(external)
    ref = ArtifactRef(relative_path="artifacts/link.patch", sha256=sha256_hex(b"data"))
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_group_other_writable_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("mode bits not enforced on Windows")
    run_root = ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    (run_root / "artifacts").mkdir(parents=True, exist_ok=True)
    target = run_root / "artifacts" / "loose.patch"
    target.write_bytes(b"data")
    os.chmod(target, 0o666)
    ref = ArtifactRef(relative_path="artifacts/loose.patch", sha256=sha256_hex(b"data"))
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_commit_message_schema_valid(tmp_path: Path) -> None:
    payload = {
        "schema_name": "ai_dev_loop.pr_review_v2.commit_message",
        "schema_version": 1,
        "subject": "Fix bug",
        "body": "details",
    }
    data = json.dumps(payload).encode("utf-8")
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/m.json", data)
    msg = _reader(tmp_path).read_commit_message(run_id=RUN_ID, ref=ref)
    assert msg.subject == "Fix bug"
    assert msg.body == "details"


def test_commit_message_wrong_schema_rejected(tmp_path: Path) -> None:
    data = json.dumps({"schema_version": 999, "subject": "x"}).encode("utf-8")
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/m.json", data)
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_commit_message(run_id=RUN_ID, ref=ref)


def test_commit_message_invalid_json_rejected(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/m.json", b"{not json")
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_commit_message(run_id=RUN_ID, ref=ref)


def test_publication_text_schema_valid(tmp_path: Path) -> None:
    payload = {
        "schema_name": "ai_dev_loop.pr_review_v2.publication_text",
        "schema_version": 1,
        "title": "My PR",
        "body": "body text",
    }
    data = json.dumps(payload).encode("utf-8")
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/pub.json", data)
    pub = _reader(tmp_path).read_publication_text(run_id=RUN_ID, ref=ref)
    assert pub.title == "My PR"


def test_reply_text_rejects_non_utf8(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/reply.txt", b"\xff\xfe bad")
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_reply_text(run_id=RUN_ID, ref=ref)


def test_reply_text_rejects_empty(tmp_path: Path) -> None:
    ref = write_artifact(tmp_path / "art", RUN_ID, "artifacts/reply.txt", b"   \n")
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_reply_text(run_id=RUN_ID, ref=ref)


def test_traversal_path_rejected(tmp_path: Path) -> None:
    ensure_run_artifact_root(tmp_path / "art", RUN_ID)
    # ArtifactRef itself rejects traversal, so build a raw ref via model_construct.
    ref = ArtifactRef.model_construct(relative_path="../escape.patch", sha256="0" * 64)
    with pytest.raises(InputArtifactError):
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=ref)


def test_error_message_never_contains_content(tmp_path: Path) -> None:
    secret = b"-----BEGIN PRIVATE KEY----- supersecret"
    write_artifact(tmp_path / "art", RUN_ID, "artifacts/p.patch", secret)
    bad_ref = ArtifactRef(relative_path="artifacts/p.patch", sha256="0" * 64)
    try:
        _reader(tmp_path).read_patch_bytes(run_id=RUN_ID, ref=bad_ref)
    except InputArtifactError as exc:
        assert "supersecret" not in str(exc)
        assert "BEGIN PRIVATE KEY" not in str(exc)
        assert exc.kind is InputArtifactErrorKind.HASH_MISMATCH
    else:
        raise AssertionError("expected InputArtifactError")


def test_input_artifact_error_kind_is_structural() -> None:
    err = InputArtifactError(InputArtifactErrorKind.UNSAFE_MODE)
    assert err.kind is InputArtifactErrorKind.UNSAFE_MODE
    assert "group/other" not in str(err)


def test_run_root_is_hashed(tmp_path: Path) -> None:
    root = run_artifact_root(tmp_path / "art", RUN_ID)
    assert RUN_ID not in str(root)
