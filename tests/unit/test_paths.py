"""Unit tests for path resolution and permissions."""

from __future__ import annotations

import stat
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE, ensure_dir, state_dir
from ai_dev_loop.state import atomic_write_text


def test_state_dir_uses_xdg_fallback(isolated_xdg) -> None:
    assert state_dir() == isolated_xdg / "state" / "ai_dev_loop"


def test_secure_directory_permissions(permission_test_root: Path) -> None:
    path = permission_test_root / "runs" / "demo"
    ensure_dir(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_sensitive_file_permissions(permission_test_root: Path) -> None:
    path = permission_test_root / "secret.txt"
    atomic_write_text(path, "prompt", sensitive=True)
    assert stat.S_IMODE(path.stat().st_mode) == SENSITIVE_FILE_MODE
