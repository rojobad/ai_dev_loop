"""Unit tests for filesystem repository binding discovery."""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.scheduler.infrastructure.repository_binding import (
    _apply_delta,
    _decode_pack_offset,
    _GitObjectStore,
    _parse_index,
    discover_repository_binding,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _assert_binding_matches_git(repo: Path) -> None:
    git_info = discover_repository(repo)
    fs_info = discover_repository_binding(repo)
    assert fs_info.root == git_info.root.resolve()
    assert fs_info.git_common_dir == git_info.git_common_dir.resolve()
    assert fs_info.git_dir == git_info.git_dir.resolve()
    assert fs_info.branch == git_info.branch
    assert fs_info.head == git_info.head
    assert fs_info.status_porcelain == git_info.status_porcelain
    assert fs_info.staged_paths == git_info.staged_paths


def _repack_with_deltas(repo: Path, *, use_ofs_delta: bool) -> None:
    _git(
        repo,
        "config",
        "pack.useDeltaBaseOffset",
        "true" if use_ofs_delta else "false",
    )
    _git(repo, "repack", "-adf", "--depth=50", "--window=250")
    objects_dir = repo / ".git" / "objects"
    for path in objects_dir.iterdir():
        if path.name in {"pack", "info"}:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def test_discover_repository_binding_clean_repo(git_repo: Path) -> None:
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_rejects_non_repo(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not a git worktree"):
        discover_repository_binding(tmp_path)


def test_discover_repository_binding_captures_staged_addition(git_repo: Path) -> None:
    (git_repo / "staged-new.txt").write_text("new\n", encoding="utf-8")
    _git(git_repo, "add", "staged-new.txt")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_staged_deletion(git_repo: Path) -> None:
    tracked = next(
        path for path in git_repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    rel = tracked.relative_to(git_repo).as_posix()
    _git(git_repo, "rm", "--cached", rel)
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_untracked_file(git_repo: Path) -> None:
    (git_repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_dirty_tracked_file(git_repo: Path) -> None:
    tracked = next(
        path for path in git_repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    tracked.write_text(tracked.read_text(encoding="utf-8") + "dirty\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_intent_to_add(git_repo: Path) -> None:
    (git_repo / "intent-to-add.txt").write_text("intent\n", encoding="utf-8")
    _git(git_repo, "add", "--intent-to-add", "intent-to-add.txt")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_skip_worktree(git_repo: Path) -> None:
    tracked = next(
        path for path in git_repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    rel = tracked.relative_to(git_repo).as_posix()
    _git(git_repo, "update-index", "--skip-worktree", rel)
    _assert_binding_matches_git(git_repo)


def test_decode_pack_offset_multibyte_additive_rule() -> None:
    handle = io.BytesIO(bytes([0x81, 0x00]))
    assert _decode_pack_offset(handle) == 256


def test_decode_pack_offset_single_byte() -> None:
    handle = io.BytesIO(bytes([0x01]))
    assert _decode_pack_offset(handle) == 1


def test_apply_delta_copy_and_insert_instructions() -> None:
    base = b"aaaaBBBBcccc"
    payload = bytes(
        [
            0x0C,
            0x0B,
            3,
            ord("x"),
            ord("y"),
            ord("z"),
            0x91,
            0x00,
            0x04,
            0x91,
            0x08,
            0x04,
        ]
    )
    kind, result = _apply_delta("blob", base, payload)
    assert kind == "blob"
    assert result == b"xyzaaaacccc"


def _assert_pack_objects_readable(repo: Path) -> None:
    store = _GitObjectStore(repo / ".git")
    pack_dir = repo / ".git" / "objects" / "pack"
    idx_path = next(pack_dir.glob("*.idx"))
    data = idx_path.read_bytes()
    fanout = struct.unpack(">256I", data[8 : 8 + 256 * 4])
    object_count = fanout[255]
    names_offset = 8 + 256 * 4
    names = data[names_offset : names_offset + object_count * 20]
    for index in range(object_count):
        sha = names[index * 20 : (index + 1) * 20].hex()
        store.read_object(sha)


def test_discover_repository_binding_reads_ofs_delta_pack(git_repo: Path) -> None:
    for index in range(50):
        (git_repo / f"packed-{index}.txt").write_text(
            "A" * 8000 + f" variant {index}\n",
            encoding="utf-8",
        )
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "packed variants")
    _repack_with_deltas(git_repo, use_ofs_delta=True)
    _assert_pack_objects_readable(git_repo)
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_reads_ref_delta_pack(git_repo: Path) -> None:
    for index in range(50):
        (git_repo / f"ref-packed-{index}.txt").write_text(
            "B" * 8000 + f" variant {index}\n",
            encoding="utf-8",
        )
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "ref packed variants")
    _repack_with_deltas(git_repo, use_ofs_delta=False)
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_omits_ignored_untracked_files(git_repo: Path) -> None:
    gitignore = git_repo / ".gitignore"
    gitignore.write_text("ignored-dir/\n*.ignored\n", encoding="utf-8")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "-m", "track gitignore")
    (git_repo / "foo.ignored").write_text("ignored\n", encoding="utf-8")
    (git_repo / "visible.txt").write_text("visible\n", encoding="utf-8")
    ignored_dir = git_repo / "ignored-dir"
    ignored_dir.mkdir()
    (ignored_dir / "nested.txt").write_text("nested\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_supports_linked_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    linked = tmp_path / "linked-worktree"
    _git(git_repo, "worktree", "add", str(linked), "-b", "linked-branch")
    _assert_binding_matches_git(linked)


def test_discover_repository_binding_supports_index_version_4(git_repo: Path) -> None:
    _git(git_repo, "update-index", "--index-version=4")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_supports_index_version_4_zero_strip(
    git_repo: Path,
) -> None:
    # Lexicographically consecutive paths where each entry appends to the full
    # previous pathname (Git index v4 N=0) exercise the zero-strip decode path.
    (git_repo / "x").write_text("one\n", encoding="utf-8")
    (git_repo / "xy").write_text("two\n", encoding="utf-8")
    (git_repo / "xyz").write_text("three\n", encoding="utf-8")
    _git(git_repo, "add", "x", "xy", "xyz")
    _git(git_repo, "update-index", "--index-version=4")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_staged_add_then_modified(git_repo: Path) -> None:
    (git_repo / "staged-then-modified.txt").write_text("v1\n", encoding="utf-8")
    _git(git_repo, "add", "staged-then-modified.txt")
    (git_repo / "staged-then-modified.txt").write_text("v2\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_staged_add_then_deleted(git_repo: Path) -> None:
    (git_repo / "staged-then-deleted.txt").write_text("v1\n", encoding="utf-8")
    _git(git_repo, "add", "staged-then-deleted.txt")
    (git_repo / "staged-then-deleted.txt").unlink()
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_executable_staged_deletion(git_repo: Path) -> None:
    script = git_repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    _git(git_repo, "add", "run.sh")
    _git(git_repo, "commit", "-m", "track executable")
    _git(git_repo, "rm", "--cached", "run.sh")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_honors_core_excludes_file(git_repo: Path) -> None:
    excludes = git_repo / "global-exclude"
    excludes.write_text("*.global\n", encoding="utf-8")
    _git(git_repo, "config", "core.excludesFile", str(excludes))
    (git_repo / "foo.global").write_text("ignored\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_tracked_symlink_target_modified(git_repo: Path) -> None:
    (git_repo / "real.txt").write_text("content\n", encoding="utf-8")
    (git_repo / "sym").symlink_to("real.txt")
    _git(git_repo, "add", "sym")
    _git(git_repo, "commit", "-m", "track symlink")
    (git_repo / "real.txt").write_text("changed\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_rejects_split_index(git_repo: Path) -> None:
    _git(git_repo, "config", "core.splitIndex", "true")
    (git_repo / "split.txt").write_text("one\n", encoding="utf-8")
    _git(git_repo, "add", "split.txt")
    _git(git_repo, "commit", "-m", "split index base")
    (git_repo / "split.txt").write_text("two\n", encoding="utf-8")
    _git(git_repo, "add", "split.txt")
    with pytest.raises(ValidationError, match="split git index is not supported"):
        discover_repository_binding(git_repo)


def test_discover_repository_binding_rejects_sparse_index_entries(git_repo: Path) -> None:
    for index in range(2):
        directory = git_repo / f"dir{index}" / "nested"
        directory.mkdir(parents=True)
        (directory / "file.txt").write_text(f"content {index}\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "sparse base")
    _git(git_repo, "sparse-checkout", "init", "--sparse-index", "--cone")
    _git(git_repo, "sparse-checkout", "set", "dir0")
    with pytest.raises(ValidationError, match="sparse git index entries are not supported"):
        discover_repository_binding(git_repo)


def test_discover_repository_binding_captures_staged_rename(git_repo: Path) -> None:
    (git_repo / "old-name.txt").write_text("content\n", encoding="utf-8")
    _git(git_repo, "add", "old-name.txt")
    _git(git_repo, "commit", "-m", "track old name")
    _git(git_repo, "mv", "old-name.txt", "new-name.txt")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_rename_with_worktree_modification(
    git_repo: Path,
) -> None:
    (git_repo / "old-name.txt").write_text("content\n", encoding="utf-8")
    _git(git_repo, "add", "old-name.txt")
    _git(git_repo, "commit", "-m", "track old name")
    _git(git_repo, "mv", "old-name.txt", "new-name.txt")
    (git_repo / "new-name.txt").write_text("modified\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_staged_executable_bit_change(git_repo: Path) -> None:
    script = git_repo / "mode-staged.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)
    _git(git_repo, "add", "mode-staged.sh")
    _git(git_repo, "commit", "-m", "non-executable")
    script.chmod(0o755)
    _git(git_repo, "add", "mode-staged.sh")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_captures_unstaged_executable_bit_change(
    git_repo: Path,
) -> None:
    script = git_repo / "mode-unstaged.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    _git(git_repo, "add", "mode-unstaged.sh")
    _git(git_repo, "commit", "-m", "executable")
    script.chmod(0o644)
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_ignores_mode_when_filemode_disabled(git_repo: Path) -> None:
    script = git_repo / "mode-ignored.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)
    _git(git_repo, "add", "mode-ignored.sh")
    _git(git_repo, "commit", "-m", "non-executable")
    _git(git_repo, "config", "core.fileMode", "false")
    script.chmod(0o755)
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_honors_included_config(git_repo: Path, tmp_path: Path) -> None:
    excludes = tmp_path / "included-excludes"
    excludes.write_text("*.included\n", encoding="utf-8")
    included_config = tmp_path / "included.config"
    included_config.write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
    include_file = tmp_path / "includes.config"
    include_file.write_text(f"[include]\n\tpath = {included_config}\n", encoding="utf-8")
    _git(git_repo, "config", "include.path", str(include_file))
    (git_repo / "tracked.included").write_text("ignored\n", encoding="utf-8")
    _assert_binding_matches_git(git_repo)


def test_discover_repository_binding_rejects_includeif(git_repo: Path, tmp_path: Path) -> None:
    include_file = tmp_path / "conditional.config"
    include_file.write_text(
        '[includeIf "gitdir:foo/"]\n\tpath = /tmp/extra\n',
        encoding="utf-8",
    )
    _git(git_repo, "config", "include.path", str(include_file))
    with pytest.raises(ValidationError, match="includeIf is not supported"):
        discover_repository_binding(git_repo)


def test_discover_repository_binding_rejects_autocrlf_true(git_repo: Path) -> None:
    _git(git_repo, "config", "core.autocrlf", "true")
    with pytest.raises(ValidationError, match="core.autocrlf"):
        discover_repository_binding(git_repo)


def test_discover_repository_binding_rejects_nested_gitattributes_filter(
    git_repo: Path,
) -> None:
    nested = git_repo / "nested"
    nested.mkdir()
    (nested / ".gitattributes").write_text("*.bin filter=custom\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="gitattributes filters are not supported"):
        discover_repository_binding(git_repo)


def test_discover_repository_binding_rejects_core_attributes_file(
    git_repo: Path, tmp_path: Path
) -> None:
    attributes = tmp_path / "global-attributes"
    attributes.write_text("*.dat text=auto\n", encoding="utf-8")
    _git(git_repo, "config", "core.attributesFile", str(attributes))
    with pytest.raises(
        ValidationError, match="gitattributes text/eol conversions are not supported"
    ):
        discover_repository_binding(git_repo)


def test_parse_index_v4_reads_tree_extension_before_checksum(git_repo: Path) -> None:
    _git(git_repo, "update-index", "--index-version=4")
    entries = _parse_index(git_repo / ".git")
    assert len(entries) >= 1


def test_parse_index_v4_rejects_link_extension_after_tree(git_repo: Path) -> None:
    _git(git_repo, "update-index", "--index-version=4")
    index_path = git_repo / ".git" / "index"
    data = bytearray(index_path.read_bytes())
    checksum = bytes(data[-20:])
    body = bytes(data[:-20])
    link_payload = b"\x00" * 20
    link_ext = b"link" + struct.pack(">I", len(link_payload)) + link_payload
    index_path.write_bytes(body + link_ext + checksum)
    with pytest.raises(ValidationError, match="split git index is not supported"):
        _parse_index(git_repo / ".git")


def test_parse_index_v4_rejects_sdir_extension_after_tree(git_repo: Path) -> None:
    _git(git_repo, "update-index", "--index-version=4")
    index_path = git_repo / ".git" / "index"
    data = bytearray(index_path.read_bytes())
    checksum = bytes(data[-20:])
    body = bytes(data[:-20])
    sdir_payload = b"\x00" * 16
    sdir_ext = b"sdir" + struct.pack(">I", len(sdir_payload)) + sdir_payload
    index_path.write_bytes(body + sdir_ext + checksum)
    with pytest.raises(ValidationError, match="sparse git index is not supported"):
        _parse_index(git_repo / ".git")
