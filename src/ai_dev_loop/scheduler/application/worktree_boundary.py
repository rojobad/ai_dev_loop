"""Content fingerprint for one explicit recovery boundary."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path, PurePath

from ai_dev_loop.errors import AiDevLoopError

_GIT_TIMEOUT_SECONDS = 60.0


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AiDevLoopError("worktree fingerprint command failed") from exc
    if completed.returncode != 0:
        raise AiDevLoopError("worktree fingerprint command failed")
    return completed.stdout


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def worktree_content_fingerprint(repo_root: str, *, status_text: str) -> str:
    """Hash staged, unstaged and untracked content without persisting file bytes."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise AiDevLoopError("worktree fingerprint repository is missing")
    cached = _git_bytes(
        root,
        "--no-pager",
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
    )
    unstaged = _git_bytes(
        root,
        "--no-pager",
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
    )
    untracked_raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    digest = hashlib.sha256()
    for label, payload in (
        (b"status", status_text.encode("utf-8")),
        (b"cached", cached),
        (b"unstaged", unstaged),
    ):
        digest.update(label + b"\0" + len(payload).to_bytes(8, "big") + payload)
    for raw_relative in sorted(item for item in untracked_raw.split(b"\0") if item):
        relative = os.fsdecode(raw_relative)
        pure = PurePath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise AiDevLoopError("worktree fingerprint found an unsafe untracked path")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise AiDevLoopError("worktree fingerprint requires regular untracked files")
        digest.update(b"untracked\0" + raw_relative + b"\0" + _hash_file(path).encode("ascii"))
    return digest.hexdigest()
