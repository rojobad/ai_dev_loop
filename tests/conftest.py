"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"


def chmod_supported(directory: Path) -> bool:
    if os.name == "nt":
        return False
    probe = directory / ".chmod-probe"
    probe.mkdir(exist_ok=True)
    try:
        os.chmod(probe, 0o700)
        return stat.S_IMODE(probe.stat().st_mode) == 0o700
    except OSError:
        return False
    finally:
        if probe.exists():
            probe.rmdir()


@pytest.fixture
def permission_test_root() -> Iterator[Path]:
    if os.name == "nt":
        pytest.skip("permission bits are not enforced on Windows")
    for base_dir in (Path("/tmp"), Path(tempfile.gettempdir())):
        if not base_dir.is_dir():
            continue
        root = Path(tempfile.mkdtemp(prefix="ai_dev_loop_perm_", dir=base_dir))
        if chmod_supported(root):
            yield root
            shutil.rmtree(root, ignore_errors=True)
            return
        shutil.rmtree(root, ignore_errors=True)
    pytest.skip("no filesystem available that enforces Unix permission bits")


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "xdg"
    config = base / "config"
    state = base / "state"
    cache = base / "cache"
    for path in (config, state, cache):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return base


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    for relative in (
        "ai_dev_loop.yaml",
        "docs/plans/sample-plan.md",
        "docs/plans/prompt_sample-plan.txt",
    ):
        source = FIXTURE_REPO / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial fixture")
    return repo


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
