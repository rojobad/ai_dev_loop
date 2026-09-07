"""Filesystem-only repository binding discovery for scheduler submit."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import GitRepositoryInfo


def discover_repository_binding(repo_path: Path) -> GitRepositoryInfo:
    """Discover repository metadata by reading .git files only (no subprocess)."""

    worktree_root, dot_git = _find_git_metadata(repo_path)
    git_dir = _resolve_git_dir(worktree_root, dot_git)
    git_common_dir = _resolve_git_common_dir(git_dir)
    branch, head = _read_head(git_dir, git_common_dir)
    _validate_repository_binding_support(worktree_root, git_dir, git_common_dir)
    object_store = _GitObjectStore(git_common_dir)
    status_porcelain, staged_paths = _snapshot_worktree_status(
        worktree_root,
        git_dir,
        git_common_dir,
        head,
        object_store,
    )
    return GitRepositoryInfo(
        root=worktree_root,
        git_common_dir=git_common_dir,
        git_dir=git_dir,
        branch=branch,
        head=head,
        status_porcelain=status_porcelain,
        staged_paths=staged_paths,
    )


@dataclass
class _IndexEntry:
    path: str
    sha: str
    mode: int
    stage: int
    intent_to_add: bool = False
    skip_worktree: bool = False


@dataclass
class _WorktreeChangeReport:
    merge_entries: list[str] = field(default_factory=list)
    staged_additions: list[str] = field(default_factory=list)
    staged_modifications: list[str] = field(default_factory=list)
    staged_deletions: list[str] = field(default_factory=list)
    unstaged_modifications: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    unsupported_modes: list[str] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(
            self.merge_entries
            or self.staged_additions
            or self.staged_modifications
            or self.staged_deletions
            or self.unstaged_modifications
            or self.untracked_files
            or self.unsupported_modes
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.merge_entries:
            parts.append(f"unmerged index entries ({len(self.merge_entries)})")
        if self.staged_additions:
            parts.append(f"staged additions ({len(self.staged_additions)})")
        if self.staged_modifications:
            parts.append(f"staged modifications ({len(self.staged_modifications)})")
        if self.staged_deletions:
            parts.append(f"staged deletions ({len(self.staged_deletions)})")
        if self.unstaged_modifications:
            parts.append(f"unstaged modifications ({len(self.unstaged_modifications)})")
        if self.untracked_files:
            parts.append(f"untracked files ({len(self.untracked_files)})")
        if self.unsupported_modes:
            parts.append(f"unsupported index modes ({len(self.unsupported_modes)})")
        return ", ".join(parts)


class _GitObjectStore:
    """Read git objects from loose storage and packfiles under git_common_dir."""

    def __init__(self, git_common_dir: Path) -> None:
        self._objects_dir = git_common_dir / "objects"
        self._pack_indexes = _load_pack_indexes(self._objects_dir / "pack")
        self._cache: dict[str, tuple[str, bytes]] = {}

    def read_object(self, sha: str) -> tuple[str, bytes]:
        cached = self._cache.get(sha)
        if cached is not None:
            return cached
        loose_path = self._objects_dir / sha[:2] / sha[2:]
        if loose_path.is_file():
            result = _decode_git_object(loose_path.read_bytes())
            self._cache[sha] = result
            return result
        pack_offset = self._pack_indexes.lookup(sha)
        if pack_offset is not None:
            result = self._read_pack_object(pack_offset, sha)
            self._cache[sha] = result
            return result
        raise ValidationError(f"missing git object: {sha[:8]}")

    def _read_pack_object(
        self, location: tuple[Path, int], sha: str | None = None
    ) -> tuple[str, bytes]:
        pack_path, offset = location
        with pack_path.open("rb") as handle:
            handle.seek(offset)
            header = handle.read(1)
            if not header:
                raise ValidationError("truncated pack object header")
            type_bits = (header[0] >> 4) & 0x7
            size = header[0] & 0x0F
            shift = 4
            while header[0] & 0x80:
                header = handle.read(1)
                if not header:
                    raise ValidationError("truncated pack object size")
                size |= (header[0] & 0x7F) << shift
                shift += 7
            if type_bits == 6:
                # OFS_DELTA: additive base-128 offset (not size-style left shift).
                base_distance = _decode_pack_offset(handle)
                base_offset = offset - base_distance
                base_kind, base_body = self._read_pack_object((pack_path, base_offset))
                delta_data = zlib.decompress(handle.read())
                return _apply_delta(base_kind, base_body, delta_data)
            if type_bits == 7:
                base_name = handle.read(20)
                if len(base_name) != 20:
                    raise ValidationError("truncated ref_delta base name")
                base_sha = base_name.hex()
                base_kind, base_body = self.read_object(base_sha)
                delta_data = zlib.decompress(handle.read())
                return _apply_delta(base_kind, base_body, delta_data)
            payload = zlib.decompress(handle.read())
            if len(payload) != size:
                raise ValidationError("pack object size mismatch")
            type_name = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}.get(type_bits)
            if type_name is None:
                raise ValidationError("unsupported pack object type")
            result = (type_name, payload)
            if sha is not None:
                self._cache[sha] = result
            return result


class _PackIndexBundle:
    def __init__(self, idx_path: Path, pack_path: Path, names: bytes, offsets: list[int]) -> None:
        self.idx_path = idx_path
        self.pack_path = pack_path
        self.names = names
        self.offsets = offsets

    def lookup(self, sha: str) -> int | None:
        target = bytes.fromhex(sha)
        left = 0
        right = len(self.offsets) - 1
        while left <= right:
            mid = (left + right) // 2
            name = self.names[mid * 20 : (mid + 1) * 20]
            if name == target:
                return self.offsets[mid]
            if name < target:
                left = mid + 1
            else:
                right = mid - 1
        return None


class _PackIndexCatalog:
    def __init__(self, bundles: list[_PackIndexBundle]) -> None:
        self._bundles = bundles

    def lookup(self, sha: str) -> tuple[Path, int] | None:
        for bundle in self._bundles:
            offset = bundle.lookup(sha)
            if offset is not None:
                return bundle.pack_path, offset
        return None


def _load_pack_indexes(pack_dir: Path) -> _PackIndexCatalog:
    bundles: list[_PackIndexBundle] = []
    if not pack_dir.is_dir():
        return _PackIndexCatalog(bundles)
    for idx_path in sorted(pack_dir.glob("*.idx")):
        pack_path = idx_path.with_suffix(".pack")
        if not pack_path.is_file():
            continue
        bundles.append(_load_pack_index(idx_path, pack_path))
    return _PackIndexCatalog(bundles)


def _load_pack_index(idx_path: Path, pack_path: Path) -> _PackIndexBundle:
    data = idx_path.read_bytes()
    if len(data) < 8 or data[:4] != b"\xfftOc":
        raise ValidationError("invalid pack index")
    version = struct.unpack(">I", data[4:8])[0]
    if version != 2:
        raise ValidationError("unsupported pack index version")
    fanout = struct.unpack(">256I", data[8 : 8 + 256 * 4])
    object_count = fanout[255]
    names_offset = 8 + 256 * 4
    names_end = names_offset + object_count * 20
    crc_end = names_end + object_count * 4
    offsets_end = crc_end + object_count * 4
    if offsets_end > len(data):
        raise ValidationError("truncated pack index")
    names = data[names_offset:names_end]
    raw_offsets = struct.unpack(f">{object_count}I", data[crc_end:offsets_end])
    resolved_offsets: list[int] = []
    large_offset_base = offsets_end
    large_index = 0
    for raw in raw_offsets:
        if raw & 0x80000000:
            large_offset_offset = large_offset_base + large_index * 8
            if large_offset_offset + 8 > len(data):
                raise ValidationError("truncated pack index large offsets")
            resolved_offsets.append(
                struct.unpack(">Q", data[large_offset_offset : large_offset_offset + 8])[0]
            )
            large_index += 1
        else:
            resolved_offsets.append(raw)
    return _PackIndexBundle(idx_path, pack_path, names, resolved_offsets)


def _decode_git_object(data: bytes) -> tuple[str, bytes]:
    payload = zlib.decompress(data)
    nul = payload.index(b"\x00")
    kind = payload[:nul].decode("ascii").split(" ", 1)[0]
    return kind, payload[nul + 1 :]


def _read_delta_size(data: bytes, offset: int) -> tuple[int, int]:
    size = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValidationError("truncated delta size")
        byte = data[offset]
        offset += 1
        size |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return size, offset


def _decode_pack_offset(handle: io.BufferedIOBase) -> int:
    """Decode a pack OFS_DELTA distance using Git's additive base-128 algorithm."""

    header = handle.read(1)
    if not header:
        raise ValidationError("truncated ofs_delta offset")
    byte = header[0]
    value = byte & 0x7F
    while byte & 0x80:
        header = handle.read(1)
        if not header:
            raise ValidationError("truncated ofs_delta offset")
        byte = header[0]
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value


def _decode_index_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a Git index varint using the same encoding as pack OFS_DELTA offsets."""

    if offset >= len(data):
        raise ValidationError("truncated git index varint")
    byte = data[offset]
    offset += 1
    value = byte & 0x7F
    while byte & 0x80:
        if offset >= len(data):
            raise ValidationError("truncated git index varint")
        byte = data[offset]
        offset += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, offset


def _decode_index_path_v4(data: bytes, offset: int, previous_path: str) -> tuple[str, int]:
    strip_len, offset = _decode_index_varint(data, offset)
    if strip_len > len(previous_path):
        raise ValidationError("invalid git index v4 path prefix")
    nul = data.find(b"\x00", offset)
    if nul == -1:
        raise ValidationError("truncated git index path")
    suffix = data[offset:nul]
    previous_path_bytes = previous_path.encode("utf-8")
    if strip_len == 0:
        path_bytes = previous_path_bytes + suffix
    else:
        path_bytes = previous_path_bytes[:-strip_len] + suffix
    path = path_bytes.decode("utf-8")
    return path, nul + 1


def _apply_delta(kind: str, base: bytes, delta: bytes) -> tuple[str, bytes]:
    offset = 0
    src_size, offset = _read_delta_size(delta, offset)
    dst_size, offset = _read_delta_size(delta, offset)
    if src_size != len(base):
        raise ValidationError("delta base size mismatch")
    result = bytearray()
    while offset < len(delta):
        cmd = delta[offset]
        offset += 1
        if cmd == 0:
            raise ValidationError("invalid delta command")
        if cmd & 0x80:
            copy_offset = 0
            copy_size = 0
            if cmd & 0x01:
                copy_offset = delta[offset]
                offset += 1
            if cmd & 0x02:
                copy_offset |= delta[offset] << 8
                offset += 1
            if cmd & 0x04:
                copy_offset |= delta[offset] << 16
                offset += 1
            if cmd & 0x08:
                copy_offset |= delta[offset] << 24
                offset += 1
            if cmd & 0x10:
                copy_size = delta[offset]
                offset += 1
            if cmd & 0x20:
                copy_size |= delta[offset] << 8
                offset += 1
            if cmd & 0x40:
                copy_size |= delta[offset] << 16
                offset += 1
            if copy_size == 0:
                copy_size = 0x10000
            result.extend(base[copy_offset : copy_offset + copy_size])
        else:
            result.extend(delta[offset : offset + cmd])
            offset += cmd
    if len(result) != dst_size:
        raise ValidationError("delta result size mismatch")
    return kind, bytes(result)


def _find_git_metadata(start: Path) -> tuple[Path, Path]:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        dot_git = current / ".git"
        if dot_git.exists():
            git_dir = _resolve_git_dir(current, dot_git)
            if (git_dir / "HEAD").is_file():
                return current, dot_git
        if current.parent == current:
            break
        current = current.parent
    raise ValidationError(f"repository path is not a git worktree: {start}")


def _resolve_git_dir(worktree_root: Path, dot_git: Path) -> Path:
    if dot_git.is_dir():
        return dot_git.resolve()
    text = dot_git.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        raise ValidationError("invalid .git file")
    raw = text.split(":", 1)[1].strip()
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = (worktree_root / git_dir).resolve()
    return git_dir


def _resolve_git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    if common.is_file():
        rel = common.read_text(encoding="utf-8").strip()
        return (git_dir / rel).resolve()
    return git_dir


def _read_packed_ref(refs_root: Path, ref: str) -> str | None:
    packed = refs_root / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        sha, name = line.split(" ", 1)
        if name.strip() == ref:
            return sha.strip()
    return None


def _resolve_ref(refs_root: Path, ref: str) -> str:
    loose = refs_root / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()
    packed = _read_packed_ref(refs_root, ref)
    if packed is not None:
        return packed
    raise ValidationError(f"unresolved git ref: {ref}")


def _read_head(git_dir: Path, git_common_dir: Path) -> tuple[str, str]:
    content = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if content.startswith("ref: "):
        ref = content[5:].strip()
        branch = ref.removeprefix("refs/heads/")
        try:
            head = _resolve_ref(git_dir, ref)
        except ValidationError:
            head = _resolve_ref(git_common_dir, ref)
        if len(head) != 40:
            raise ValidationError("invalid HEAD sha")
        return branch, head
    if len(content) == 40:
        return "HEAD", content
    raise ValidationError("invalid HEAD")


def _blob_hash(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324


@dataclass(frozen=True)
class _TreeBlob:
    sha: str
    mode: str


def _tree_entries(
    object_store: _GitObjectStore,
    tree_sha: str,
    prefix: str = "",
) -> dict[str, _TreeBlob]:
    kind, body = object_store.read_object(tree_sha)
    if kind != "tree":
        raise ValidationError("expected git tree object")
    entries: dict[str, _TreeBlob] = {}
    offset = 0
    while offset < len(body):
        space = body.index(b" ", offset)
        nul = body.index(b"\x00", space)
        mode = body[offset:space].decode("ascii")
        name = body[space + 1 : nul].decode("utf-8")
        obj_sha = body[nul + 1 : nul + 21].hex()
        offset = nul + 21
        full_name = f"{prefix}{name}"
        if mode == "40000":
            entries.update(_tree_entries(object_store, obj_sha, f"{full_name}/"))
        else:
            entries[full_name] = _TreeBlob(sha=obj_sha, mode=mode)
    return entries


def _commit_tree_sha(object_store: _GitObjectStore, commit_sha: str) -> str:
    kind, body = object_store.read_object(commit_sha)
    if kind != "commit":
        raise ValidationError("expected git commit object")
    for line in body.split(b"\n"):
        if line.startswith(b"tree "):
            return line.decode("ascii").split(" ", 1)[1].strip()
    raise ValidationError("commit object missing tree")


def _parse_index(git_dir: Path) -> list[_IndexEntry]:
    index_path = git_dir / "index"
    if not index_path.is_file():
        return []
    data = index_path.read_bytes()
    if len(data) < 12 or data[:4] != b"DIRC":
        raise ValidationError("invalid git index")
    version = struct.unpack(">I", data[4:8])[0]
    if version not in {2, 3, 4}:
        raise ValidationError(f"unsupported git index version: {version}")
    count = struct.unpack(">I", data[8:12])[0]
    offset = 12
    entries: list[_IndexEntry] = []
    previous_path = ""
    for _ in range(count):
        entry_start = offset
        if offset + 62 > len(data):
            raise ValidationError("truncated git index")
        entry = data[offset : offset + 62]
        sha = entry[40:60].hex()
        mode = struct.unpack(">I", entry[24:28])[0]
        flags = struct.unpack(">H", entry[60:62])[0]
        offset += 62
        extended = bool(flags & 0x4000)
        intent_to_add = False
        skip_worktree = False
        if extended:
            if offset + 2 > len(data):
                raise ValidationError("truncated git index entry")
            extended_flags = struct.unpack(">H", data[offset : offset + 2])[0]
            intent_to_add = bool(extended_flags & 0x2000)
            skip_worktree = bool(extended_flags & 0x4000)
            offset += 2
        if mode == 0o040000:
            raise ValidationError("sparse git index entries are not supported")
        if version == 4:
            path, offset = _decode_index_path_v4(data, offset, previous_path)
            previous_path = path
        else:
            name_length = flags & 0xFFF
            if name_length == 0xFFF:
                nul = data.find(b"\x00", offset)
                if nul == -1:
                    raise ValidationError("truncated git index path")
                path = data[offset:nul].decode("utf-8")
                offset = nul + 1
            else:
                if offset + name_length + 1 > len(data):
                    raise ValidationError("truncated git index path")
                path = data[offset : offset + name_length].decode("utf-8")
                if data[offset + name_length] != 0:
                    raise ValidationError("invalid git index path terminator")
                offset += name_length + 1
            entry_bytes = offset - entry_start
            padding = (8 - (entry_bytes % 8)) % 8
            offset += padding
        stage = (flags >> 12) & 0x3
        entries.append(
            _IndexEntry(
                path=path,
                sha=sha,
                mode=mode,
                stage=stage,
                intent_to_add=intent_to_add,
                skip_worktree=skip_worktree,
            )
        )
    _reject_unsupported_index_extensions(data, offset)
    return entries


def _reject_unsupported_index_extensions(data: bytes, offset: int) -> None:
    checksum_offset = len(data) - 20
    if checksum_offset < offset:
        raise ValidationError("truncated git index")
    while offset + 8 <= checksum_offset:
        signature = data[offset : offset + 4]
        if signature == b"\x00\x00\x00\x00":
            break
        ext_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        payload_end = offset + 8 + ext_size
        if payload_end > checksum_offset:
            raise ValidationError("truncated git index extension")
        if signature == b"link":
            raise ValidationError("split git index is not supported")
        if signature == b"sdir":
            raise ValidationError("sparse git index is not supported")
        offset = payload_end
        if offset >= checksum_offset:
            break
        while offset < checksum_offset and data[offset] == 0:
            offset += 1


@dataclass(frozen=True)
class _GitConfigContext:
    values: dict[str, str]
    file_mode_enabled: bool
    autocrlf: str


def _global_git_config_paths(home: Path) -> list[Path]:
    paths: list[Path] = []
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        paths.append(Path(xdg_config_home) / "git" / "config")
    else:
        paths.append(home / ".config" / "git" / "config")
    paths.append(home / ".gitconfig")
    return paths


def _git_config_file_order(git_dir: Path, git_common_dir: Path, home: Path) -> list[Path]:
    paths = [Path("/etc/gitconfig")]
    paths.extend(_global_git_config_paths(home))
    if git_common_dir != git_dir:
        paths.append(git_common_dir / "config")
    paths.append(git_dir / "config")
    return paths


def _load_effective_git_config(
    git_dir: Path,
    git_common_dir: Path,
    *,
    home: Path | None = None,
) -> _GitConfigContext:
    home = home or Path.home()
    merged: dict[str, str] = {}
    seen_paths: set[Path] = set()
    for path in _git_config_file_order(git_dir, git_common_dir, home):
        _load_git_config_file(path, merged, seen_paths, home)
    autocrlf = merged.get("core.autocrlf", "false").lower()
    file_mode_enabled = merged.get("core.filemode", "true").lower() not in {"false", "0", "no"}
    return _GitConfigContext(
        values=merged,
        file_mode_enabled=file_mode_enabled,
        autocrlf=autocrlf,
    )


def _load_git_config_file(
    path: Path,
    merged: dict[str, str],
    seen_paths: set[Path],
    home: Path,
) -> None:
    resolved = path.resolve()
    if resolved in seen_paths or not resolved.is_file():
        return
    seen_paths.add(resolved)
    content = resolved.read_text(encoding="utf-8")
    section = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section.startswith("includeif "):
                raise ValidationError("git config includeIf is not supported")
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip().strip('"')
        if section == "include" and key == "path":
            include_path = Path(value)
            if not include_path.is_absolute():
                include_path = (resolved.parent / include_path).resolve()
            else:
                include_path = include_path.resolve()
            _load_git_config_file(include_path, merged, seen_paths, home)
            continue
        full_key = f"{section}.{key}" if section else key
        merged[full_key] = value


def _read_git_config(git_dir: Path, git_common_dir: Path) -> dict[str, str]:
    return _load_effective_git_config(git_dir, git_common_dir).values


def _merge_git_config(target: dict[str, str], content: str) -> None:
    section = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip().strip('"')
        full_key = f"{section}.{key}" if section else key
        target[full_key] = value


def _expand_git_path(value: str, home: Path) -> Path:
    if value.startswith("~/"):
        return (home / value[2:]).resolve()
    if value == "~":
        return home.resolve()
    return Path(value).expanduser().resolve()


def _iter_gitattributes_files(
    worktree_root: Path, config: _GitConfigContext, home: Path
) -> list[Path]:
    paths: list[Path] = []
    attributes_file = config.values.get("core.attributesfile")
    if attributes_file:
        paths.append(_expand_git_path(attributes_file, home))
    paths.append(worktree_root / ".gitattributes")
    for candidate in worktree_root.rglob(".gitattributes"):
        if ".git" in candidate.parts:
            continue
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _validate_gitattributes_files(paths: list[Path]) -> None:
    for attributes_path in paths:
        if not attributes_path.is_file():
            continue
        for raw_line in attributes_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "filter=" in line:
                raise ValidationError("gitattributes filters are not supported")
            if "text=" in line or "eol=" in line:
                raise ValidationError("gitattributes text/eol conversions are not supported")


def _validate_repository_binding_support(
    worktree_root: Path,
    git_dir: Path,
    git_common_dir: Path,
) -> None:
    config = _load_effective_git_config(git_dir, git_common_dir)
    if config.autocrlf in {"true", "auto"}:
        raise ValidationError("unsupported git config: core.autocrlf must be false or input")
    if config.values.get("core.splitindex", "false").lower() in {"true", "1", "on"}:
        raise ValidationError("split git index is not supported")
    _validate_gitattributes_files(
        _iter_gitattributes_files(worktree_root, config, Path.home()),
    )
    index_entries = _parse_index(git_dir)
    for entry in index_entries:
        if entry.stage != 0:
            raise ValidationError("unmerged index entries are not supported")
        if entry.mode == 0o160000:
            raise ValidationError("git submodule entries are not supported")


def _worktree_mode(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "000000"
    file_mode = path.lstat().st_mode
    if stat.S_ISLNK(file_mode):
        return "120000"
    if stat.S_ISDIR(file_mode):
        return "040000"
    if file_mode & 0o111:
        return "100755"
    return "100644"


def _worktree_blob_hash(path: Path, index_mode: int) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if index_mode == 0o120000:
        if not path.is_symlink():
            return None
        return _blob_hash(os.readlink(path).encode("utf-8"))
    if path.is_symlink():
        return None
    if not path.is_file():
        return None
    return _blob_hash(path.read_bytes())


@dataclass(frozen=True)
class _GitIgnoreRule:
    pattern: str
    base_dir: str
    negated: bool
    directory_only: bool


class _GitIgnoreMatcher:
    """Filesystem-only gitignore matcher for porcelain parity."""

    def __init__(self, worktree_root: Path, git_common_dir: Path, git_dir: Path) -> None:
        self._worktree_root = worktree_root
        self._exclude_rules: list[_GitIgnoreRule] = []
        exclude_path = git_common_dir / "info" / "exclude"
        if exclude_path.is_file():
            self._exclude_rules.extend(
                _load_gitignore_rules(exclude_path.read_text(encoding="utf-8"), "")
            )
        config = _load_effective_git_config(git_dir, git_common_dir)
        excludes_file = config.values.get("core.excludesfile")
        if excludes_file:
            excludes_path = _expand_git_path(excludes_file, Path.home())
            if excludes_path.is_file():
                self._exclude_rules.extend(
                    _load_gitignore_rules(excludes_path.read_text(encoding="utf-8"), "")
                )
        self._gitignore_cache: dict[str, list[_GitIgnoreRule]] = {}

    def is_ignored(self, rel_path: str) -> bool:
        parts = rel_path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if self._matches_rules(prefix, is_dir=True):
                return True
        is_dir = (self._worktree_root / rel_path).is_dir()
        return self._matches_rules(rel_path, is_dir=is_dir)

    def _matches_rules(self, rel_path: str, *, is_dir: bool) -> bool:
        ignored = False
        basename = rel_path.rsplit("/", 1)[-1]
        for rule in self._rules_for_path(rel_path):
            if _gitignore_rule_matches(rule, rel_path, basename, is_dir=is_dir):
                ignored = not rule.negated
        return ignored

    def _rules_for_path(self, rel_path: str) -> list[_GitIgnoreRule]:
        rules = list(self._exclude_rules)
        parts = rel_path.split("/")
        current = ""
        rules.extend(self._load_gitignore_at(""))
        for part in parts[:-1]:
            current = f"{current}/{part}" if current else part
            rules.extend(self._load_gitignore_at(current))
        return rules

    def _load_gitignore_at(self, base_dir: str) -> list[_GitIgnoreRule]:
        cached = self._gitignore_cache.get(base_dir)
        if cached is not None:
            return cached
        ignore_path = (
            self._worktree_root / ".gitignore"
            if not base_dir
            else self._worktree_root / base_dir / ".gitignore"
        )
        if ignore_path.is_file():
            loaded = _load_gitignore_rules(ignore_path.read_text(encoding="utf-8"), base_dir)
        else:
            loaded = []
        self._gitignore_cache[base_dir] = loaded
        return loaded


def _load_gitignore_rules(content: str, base_dir: str) -> list[_GitIgnoreRule]:
    rules: list[_GitIgnoreRule] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
            if not line or line.startswith("#"):
                continue
        directory_only = line.endswith("/")
        if directory_only:
            line = line.rstrip("/")
        rules.append(
            _GitIgnoreRule(
                pattern=line,
                base_dir=base_dir,
                negated=negated,
                directory_only=directory_only,
            )
        )
    return rules


def _gitignore_rule_matches(
    rule: _GitIgnoreRule,
    rel_path: str,
    basename: str,
    *,
    is_dir: bool,
) -> bool:
    if rule.directory_only and not is_dir:
        return False
    pattern = rule.pattern
    if pattern.startswith("/"):
        scoped = pattern[1:]
        if rule.base_dir:
            return _gitwildmatch(scoped, rel_path[len(rule.base_dir) + 1 :])
        return _gitwildmatch(scoped, rel_path)
    if "/" in pattern:
        scoped = f"{rule.base_dir}/{pattern}" if rule.base_dir else pattern
        return _gitwildmatch(scoped, rel_path)
    return _gitwildmatch(pattern, basename) or _gitwildmatch(pattern, rel_path)


def _gitwildmatch(pattern: str, text: str) -> bool:
    if not pattern:
        return text == ""
    regex_parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                regex_parts.append(".*")
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
            else:
                regex_parts.append("[^/]*")
                index += 1
            continue
        if char == "?":
            regex_parts.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                regex_parts.append(re.escape("["))
            else:
                regex_parts.append(pattern[index : end + 1])
                index = end
        else:
            regex_parts.append(re.escape(char))
        index += 1
    return re.fullmatch("".join(regex_parts), text) is not None


def _walk_worktree_files(worktree_root: Path) -> set[str]:
    paths: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(worktree_root, followlinks=False):
        dir_path = Path(dirpath)
        rel_dir = dir_path.relative_to(worktree_root)
        if rel_dir.parts and rel_dir.parts[0] == ".git":
            continue
        for name in filenames:
            candidate = dir_path / name
            if candidate.is_symlink() or candidate.is_file():
                rel = candidate.relative_to(worktree_root).as_posix()
                if rel == ".git" or rel.startswith(".git/"):
                    continue
                paths.add(rel)
    return paths


def _analyze_worktree_changes(
    worktree_root: Path,
    git_dir: Path,
    git_common_dir: Path,
    head: str,
    object_store: _GitObjectStore,
) -> _WorktreeChangeReport:
    report = _WorktreeChangeReport()
    index_entries = _parse_index(git_dir)
    if not index_entries:
        return report

    ignore_matcher = _GitIgnoreMatcher(worktree_root, git_common_dir, git_dir)

    stage_zero = [entry for entry in index_entries if entry.stage == 0]
    for entry in index_entries:
        if entry.stage != 0:
            report.merge_entries.append(entry.path)
        elif entry.mode == 0o160000:
            report.unsupported_modes.append(entry.path)

    tree_sha = _commit_tree_sha(object_store, head)
    head_blobs = _tree_entries(object_store, tree_sha)
    index_map = {entry.path: entry for entry in stage_zero}

    for path in head_blobs:
        if path not in index_map:
            report.staged_deletions.append(path)

    for path, entry in index_map.items():
        if entry.skip_worktree:
            continue
        head_blob = head_blobs.get(path)
        if head_blob is None:
            report.staged_additions.append(path)
            continue
        if head_blob.sha != entry.sha:
            report.staged_modifications.append(path)

        file_path = worktree_root / path
        worktree_sha = _worktree_blob_hash(file_path, entry.mode)
        if worktree_sha != entry.sha:
            report.unstaged_modifications.append(path)

    indexed_paths = set(index_map)
    for rel_path in sorted(_walk_worktree_files(worktree_root) - indexed_paths):
        if not ignore_matcher.is_ignored(rel_path):
            report.untracked_files.append(rel_path)

    return report


def _modes_equivalent(left: str, right: str, *, file_mode_enabled: bool) -> bool:
    if not file_mode_enabled:
        return True
    return left == right


def _index_matches_head(
    head_blob: _TreeBlob,
    entry: _IndexEntry,
    *,
    file_mode_enabled: bool,
) -> bool:
    index_mode = _format_index_mode(entry.mode)
    return head_blob.sha == entry.sha and _modes_equivalent(
        head_blob.mode,
        index_mode,
        file_mode_enabled=file_mode_enabled,
    )


def _worktree_matches_index(
    worktree_sha: str | None,
    index_sha: str,
    worktree_mode: str,
    index_mode: str,
    *,
    file_mode_enabled: bool,
) -> bool:
    if worktree_sha is None:
        return False
    return worktree_sha == index_sha and _modes_equivalent(
        worktree_mode,
        index_mode,
        file_mode_enabled=file_mode_enabled,
    )


def _match_rename_pairs(
    deleted_paths: list[str],
    added_paths: list[str],
    head_blobs: dict[str, _TreeBlob],
    index_map: dict[str, _IndexEntry],
) -> dict[str, str]:
    rename_pairs: dict[str, str] = {}
    for new_path in added_paths:
        entry = index_map[new_path]
        matching_deletes = [
            old_path
            for old_path in deleted_paths
            if old_path not in rename_pairs.values() and head_blobs[old_path].sha == entry.sha
        ]
        if len(matching_deletes) > 1:
            raise ValidationError("ambiguous rename detection is not supported")
        if len(matching_deletes) == 1:
            rename_pairs[new_path] = matching_deletes[0]
    for old_path in deleted_paths:
        matching_adds = [
            new_path for new_path in added_paths if rename_pairs.get(new_path) == old_path
        ]
        if len(matching_adds) > 1:
            raise ValidationError("ambiguous rename detection is not supported")
    return rename_pairs


def _porcelain_v2_rename_line(
    *,
    xy: str,
    head_mode: str,
    index_mode: str,
    worktree_mode: str,
    head_oid: str,
    index_oid: str,
    score: int,
    new_path: str,
    old_path: str,
) -> str:
    return (
        f"2 {xy} N... {head_mode} {index_mode} {worktree_mode} "
        f"{head_oid} {index_oid} R{score} {new_path}\t{old_path}"
    )


_ZERO_OID = "0" * 40


def _format_index_mode(mode: int) -> str:
    return format(mode & 0o777777, "o")


def _porcelain_v2_ordinary_line(
    *,
    xy: str,
    head_mode: str,
    index_mode: str,
    worktree_mode: str,
    head_oid: str,
    index_oid: str,
    path: str,
) -> str:
    return f"1 {xy} N... {head_mode} {index_mode} {worktree_mode} {head_oid} {index_oid} {path}"


def _build_porcelain_v2_status(
    worktree_root: Path,
    git_dir: Path,
    git_common_dir: Path,
    head: str,
    object_store: _GitObjectStore,
) -> tuple[str, tuple[str, ...]]:
    report = _analyze_worktree_changes(
        worktree_root,
        git_dir,
        git_common_dir,
        head,
        object_store,
    )
    config = _load_effective_git_config(git_dir, git_common_dir)
    file_mode_enabled = config.file_mode_enabled
    ignore_matcher = _GitIgnoreMatcher(worktree_root, git_common_dir, git_dir)
    index_entries = _parse_index(git_dir)
    stage_zero = [entry for entry in index_entries if entry.stage == 0]
    index_map = {entry.path: entry for entry in stage_zero}
    tree_sha = _commit_tree_sha(object_store, head)
    head_blobs = _tree_entries(object_store, tree_sha)

    deleted_paths = sorted(path for path in head_blobs if path not in index_map)
    added_paths = sorted(
        path for path in index_map if path not in head_blobs and not index_map[path].intent_to_add
    )
    rename_pairs = _match_rename_pairs(deleted_paths, added_paths, head_blobs, index_map)
    renamed_old_paths = set(rename_pairs.values())
    renamed_new_paths = set(rename_pairs)

    lines: list[str] = []
    staged_paths: list[str] = []

    for new_path in sorted(rename_pairs):
        old_path = rename_pairs[new_path]
        entry = index_map[new_path]
        head_blob = head_blobs[old_path]
        index_sha = entry.sha
        index_mode = _format_index_mode(entry.mode)
        file_path = worktree_root / new_path
        worktree_sha = _worktree_blob_hash(file_path, entry.mode)
        worktree_mode = _worktree_mode(file_path) if worktree_sha is not None else "000000"
        worktree_matches = _worktree_matches_index(
            worktree_sha,
            index_sha,
            worktree_mode,
            index_mode,
            file_mode_enabled=file_mode_enabled,
        )
        xy = "R." if worktree_matches else "RM"
        lines.append(
            _porcelain_v2_rename_line(
                xy=xy,
                head_mode=head_blob.mode,
                index_mode=index_mode,
                worktree_mode=worktree_mode,
                head_oid=head_blob.sha,
                index_oid=index_sha,
                score=100,
                new_path=new_path,
                old_path=old_path,
            )
        )
        staged_paths.append(new_path)

    for path in sorted(index_map):
        entry = index_map[path]
        if entry.skip_worktree or path in renamed_new_paths:
            continue
        index_sha = entry.sha
        index_mode = _format_index_mode(entry.mode)
        head_entry = head_blobs.get(path)
        head_sha = head_entry.sha if head_entry is not None else None
        head_mode = head_entry.mode if head_entry is not None else "000000"
        file_path = worktree_root / path
        worktree_sha = _worktree_blob_hash(file_path, entry.mode)
        worktree_mode = _worktree_mode(file_path) if worktree_sha is not None else "000000"

        if entry.intent_to_add:
            lines.append(
                _porcelain_v2_ordinary_line(
                    xy=".A",
                    head_mode="000000",
                    index_mode="000000",
                    worktree_mode=worktree_mode,
                    head_oid=_ZERO_OID,
                    index_oid=_ZERO_OID,
                    path=path,
                )
            )
            continue

        if head_sha is None:
            worktree_matches = _worktree_matches_index(
                worktree_sha,
                index_sha,
                worktree_mode,
                index_mode,
                file_mode_enabled=file_mode_enabled,
            )
            if worktree_matches:
                xy = "A."
            elif worktree_sha is None:
                xy = "AD"
            else:
                xy = "AM"
            lines.append(
                _porcelain_v2_ordinary_line(
                    xy=xy,
                    head_mode="000000",
                    index_mode=index_mode,
                    worktree_mode=worktree_mode,
                    head_oid=_ZERO_OID,
                    index_oid=index_sha,
                    path=path,
                )
            )
            staged_paths.append(path)
            continue

        assert head_entry is not None

        index_matches_head = _index_matches_head(
            head_entry,
            entry,
            file_mode_enabled=file_mode_enabled,
        )
        worktree_matches = _worktree_matches_index(
            worktree_sha,
            index_sha,
            worktree_mode,
            index_mode,
            file_mode_enabled=file_mode_enabled,
        )
        if index_matches_head and worktree_matches:
            continue

        staged_change = not index_matches_head
        worktree_change = not worktree_matches
        if staged_change and worktree_change:
            xy = "MM"
            index_oid = index_sha
        elif staged_change:
            xy = "M."
            index_oid = index_sha if head_sha != index_sha else head_sha
        else:
            xy = ".M"
            index_oid = head_sha

        lines.append(
            _porcelain_v2_ordinary_line(
                xy=xy,
                head_mode=head_mode,
                index_mode=index_mode,
                worktree_mode=worktree_mode,
                head_oid=head_sha,
                index_oid=index_oid,
                path=path,
            )
        )
        if xy in {"M.", "MM"}:
            staged_paths.append(path)

    for path in sorted(head_blobs):
        if path not in index_map and path not in renamed_old_paths:
            head_blob = head_blobs[path]
            lines.append(
                _porcelain_v2_ordinary_line(
                    xy="D.",
                    head_mode=head_blob.mode,
                    index_mode="000000",
                    worktree_mode="000000",
                    head_oid=head_blob.sha,
                    index_oid=_ZERO_OID,
                    path=path,
                )
            )
            staged_paths.append(path)
            if (worktree_root / path).exists() or (worktree_root / path).is_symlink():
                lines.append(f"? {path}")

    indexed_paths = set(index_map)
    for rel_path in sorted(_walk_worktree_files(worktree_root) - indexed_paths):
        if rel_path not in head_blobs and not ignore_matcher.is_ignored(rel_path):
            lines.append(f"? {rel_path}")

    for path in sorted(report.merge_entries):
        lines.append(f"u UU N... 000000 000000 000000 000000 000000 000000 000000 {path}")

    return "\n".join(lines), tuple(sorted(staged_paths))


def _snapshot_worktree_status(
    worktree_root: Path,
    git_dir: Path,
    git_common_dir: Path,
    head: str,
    object_store: _GitObjectStore,
) -> tuple[str, tuple[str, ...]]:
    return _build_porcelain_v2_status(
        worktree_root,
        git_dir,
        git_common_dir,
        head,
        object_store,
    )
