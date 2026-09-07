"""Unit tests for Codex integration package assets."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ai_dev_loop.integrations.codex import assets


def test_load_skill_and_hook_assets() -> None:
    skill = assets.load_skill_content()
    controller = assets.load_skill_content(assets.CONTROLLER_SKILL)
    hook = assets.load_hook_script_content()
    assert "name: ai-dev-loop-handoff" in skill
    assert "name: ai-dev-loop-controller" in controller
    assert "ai_dev_loop prepare" in skill
    assert "--codex-review-model" in skill
    assert "--codex-review-reasoning-effort" in skill
    assert "ai_dev_loop start" in skill
    assert "ai_dev_loop launch" in controller
    assert "controller status" in controller
    assert "--last" in skill
    assert "hookSpecificOutput" in hook
    assert "transcript_path" in hook
    assert "ai_dev_loop" in hook


def test_package_assets_exist_on_disk() -> None:
    assert assets.skill_package_path().is_file()
    assert assets.skill_package_path(assets.CONTROLLER_SKILL).is_file()
    assert assets.hook_script_package_path().is_file()


def test_built_wheel_includes_integration_assets(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(
        ["uv", "run", "python", "-m", "build", "--outdir", str(tmp_path / "dist")],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted((tmp_path / "dist").glob("ai_dev_loop-*.whl"))
    assert wheels
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = archive.namelist()
    assert any("integrations/codex/skill/SKILL.md" in name for name in names)
    assert any("integrations/codex/controller_skill/SKILL.md" in name for name in names)
    assert any("integrations/codex/session_start.py" in name for name in names)


def test_skill_guardrail_text_regression() -> None:
    handoff = assets.load_skill_content()
    controller = assets.load_skill_content(assets.CONTROLLER_SKILL)
    for text in (handoff, controller):
        assert "--last" in text
        assert "exact" in text.lower()
    assert "first review" in handoff.lower()
    assert "not" in handoff.lower() and "--codex-session-id" in handoff
    assert "controller status" in controller
    assert "staged patches" in controller or "review Markdown" in controller
    assert "non-force push the prepared" in controller
    assert "force-pushes, retargets" in controller
    assert "pr-review prepare" in controller
    assert "pr-review start" in handoff
    assert "pr-review prepare" in handoff
