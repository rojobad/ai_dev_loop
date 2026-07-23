"""Unit tests for read-only existing-PR discovery and prepare wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev_loop.commands.pr_review_v2 import prepare_existing_pr
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.pr_review_v2.infrastructure.existing_pr_discovery import ExistingPrDiscoverer

SHA_A = "a" * 40
SESSION = "11111111-1111-1111-1111-111111111111"


class _FakeGh:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.last_argv: list[str] | None = None

    def run(self, argv: list[str], *, cwd: str | None, timeout: float) -> tuple[int, str, str]:
        del cwd, timeout
        self.last_argv = list(argv)
        return 0, json.dumps(self.payload), ""


def _write_enabled_config(root: Path) -> Path:
    cfg = {
        "version": 1,
        "project": {"name": "demo"},
        "cursor": {},
        "codex": {"review_model": "gpt-5"},
        "workflow": {},
        "prompt": {},
        "pr_review_v2": {"enabled": True},
    }
    path = root / "ai_dev_loop.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_discoverer_builds_binding_from_open_pr() -> None:
    fake = _FakeGh(
        {
            "state": "open",
            "number": 7,
            "head_ref": "feature",
            "base_ref": "main",
            "head_sha": SHA_A,
            "head_repo": "acme/demo",
        }
    )
    disc = ExistingPrDiscoverer(runner=fake)
    found = disc.discover(owner_repo="acme/demo", pr_number=7)
    assert found.binding.pr_number == 7
    assert found.binding.head_sha == SHA_A
    assert fake.last_argv is not None
    assert fake.last_argv[0:2] == ["gh", "api"]
    assert "shell=True" not in " ".join(fake.last_argv)


def test_discoverer_rejects_closed_pr() -> None:
    fake = _FakeGh(
        {
            "state": "closed",
            "number": 7,
            "head_ref": "feature",
            "base_ref": "main",
            "head_sha": SHA_A,
            "head_repo": "acme/demo",
        }
    )
    with pytest.raises(ValidationError, match="open"):
        ExistingPrDiscoverer(runner=fake).discover(owner_repo="acme/demo", pr_number=7)


def test_prepare_existing_pr_with_fake_discoverer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "plans").mkdir()
    plan = root / "plans" / "x.md"
    prompt = root / "plans" / "prompt.txt"
    plan.write_text("# plan\n", encoding="utf-8")
    prompt.write_text("do it\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-b", "feature"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=root)
    subprocess.check_call(["git", "add", "plans"], cwd=root)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=root)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.check_call(
        ["git", "remote", "add", "origin", "git@github.com:acme/demo.git"], cwd=root
    )
    cfg = _write_enabled_config(root)
    fake = _FakeGh(
        {
            "state": "open",
            "number": 9,
            "head_ref": "feature",
            "base_ref": "main",
            "head_sha": head,
            "head_repo": "acme/demo",
        }
    )
    disc = ExistingPrDiscoverer(runner=fake)
    text = prepare_existing_pr(
        repo="acme/demo",
        pr_number=9,
        codex_session_id=SESSION,
        plan_path=Path("plans/x.md"),
        prompt_path=Path("plans/prompt.txt"),
        review_model="gpt-5",
        config_path=cfg,
        repo_path=root,
        discoverer=disc,
    )
    assert "origin: existing_pr" in text
    assert "prepared run" in text
    assert "start" in text


def test_prepare_rejects_wrong_checkout_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "plans").mkdir()
    (root / "plans" / "x.md").write_text("# plan\n", encoding="utf-8")
    (root / "plans" / "prompt.txt").write_text("do it\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-b", "other"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=root)
    subprocess.check_call(["git", "add", "plans"], cwd=root)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=root)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.check_call(
        ["git", "remote", "add", "origin", "git@github.com:acme/demo.git"], cwd=root
    )
    cfg = _write_enabled_config(root)
    fake = _FakeGh(
        {
            "state": "open",
            "number": 9,
            "head_ref": "feature",
            "base_ref": "main",
            "head_sha": head,
            "head_repo": "acme/demo",
        }
    )
    with pytest.raises(ValidationError, match="branch"):
        prepare_existing_pr(
            repo="acme/demo",
            pr_number=9,
            codex_session_id=SESSION,
            plan_path=Path("plans/x.md"),
            prompt_path=Path("plans/prompt.txt"),
            review_model="gpt-5",
            config_path=cfg,
            repo_path=root,
            discoverer=ExistingPrDiscoverer(runner=fake),
        )
