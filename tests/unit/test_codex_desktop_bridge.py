"""Unit tests for desktop session bridge logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import desktop_bridge


@pytest.fixture
def fixture_homes(hermetic_tmp_path: Path) -> dict[str, Path]:
    wsl_home = hermetic_tmp_path / "wsl-home"
    wsl_codex = wsl_home / ".codex"
    wsl_sessions = wsl_codex / "sessions"
    wsl_sessions.mkdir(parents=True)

    windows_codex = hermetic_tmp_path / "mnt" / "c" / "Users" / "Test User" / ".codex"
    desktop_sessions = windows_codex / "sessions"
    desktop_sessions.mkdir(parents=True)
    (
        desktop_sessions / "rollout-2026-07-07T12-00-00-019abc00-1111-2222-3333-444444444444.jsonl"
    ).write_text('{"events": []}', encoding="utf-8")

    return {
        "wsl_home": wsl_home,
        "wsl_codex": wsl_codex,
        "wsl_sessions": wsl_sessions,
        "windows_codex": windows_codex,
        "desktop_sessions": desktop_sessions,
    }


def test_fixture_homes_use_native_linux_temp(hermetic_tmp_path: Path) -> None:
    assert not str(hermetic_tmp_path).startswith("/mnt/")


def test_install_creates_nested_symlink(fixture_homes: dict[str, Path]) -> None:
    status = desktop_bridge.install_desktop_bridge(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    bridge = fixture_homes["wsl_sessions"] / "from-desktop"
    assert bridge.is_symlink()
    assert bridge.resolve() == fixture_homes["desktop_sessions"].resolve()
    assert status.bridge_present is True
    assert status.rollout_count == 1


def test_install_rejects_mnt_codex_home() -> None:
    mnt_codex = Path("/mnt/c/Users/TestUser/.codex")
    with pytest.raises(ValidationError, match="CODEX_HOME points at"):
        desktop_bridge.install_desktop_bridge(
            wsl_codex_home=mnt_codex,
            windows_codex_home=Path("/tmp/windows/.codex"),
        )


def test_install_rejects_symlinked_sessions_dir(
    fixture_homes: dict[str, Path], hermetic_tmp_path: Path
) -> None:
    wsl_codex = fixture_homes["wsl_codex"]
    sessions = wsl_codex / "sessions"
    sessions.rmdir()
    sessions.symlink_to(hermetic_tmp_path / "shared-sessions")
    with pytest.raises(ValidationError, match="whole-sessions share"):
        desktop_bridge.install_desktop_bridge(
            wsl_codex_home=wsl_codex,
            windows_codex_home=fixture_homes["windows_codex"],
        )


def test_remove_only_symlink(fixture_homes: dict[str, Path]) -> None:
    desktop_bridge.install_desktop_bridge(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    bridge = fixture_homes["wsl_sessions"] / "from-desktop"
    status = desktop_bridge.remove_desktop_bridge(wsl_codex_home=fixture_homes["wsl_codex"])
    assert not bridge.exists()
    assert status.bridge_present is False
    assert fixture_homes["desktop_sessions"].is_dir()


def test_remove_refuses_non_symlink(fixture_homes: dict[str, Path]) -> None:
    bridge = fixture_homes["wsl_sessions"] / "from-desktop"
    bridge.write_text("not a symlink", encoding="utf-8")
    with pytest.raises(ValidationError, match="Refusing to remove non-symlink"):
        desktop_bridge.remove_desktop_bridge(wsl_codex_home=fixture_homes["wsl_codex"])


def test_list_sessions_from_rollout_filenames(fixture_homes: dict[str, Path]) -> None:
    desktop_bridge.install_desktop_bridge(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    entries = desktop_bridge.list_desktop_sessions(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    assert len(entries) == 1
    assert entries[0].session_id == "019abc00-1111-2222-3333-444444444444"


def test_status_json_payload(fixture_homes: dict[str, Path]) -> None:
    status = desktop_bridge.collect_bridge_status(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    rendered = desktop_bridge.render_bridge_status(output="json", status=status)
    assert '"bridge_present": false' in rendered
    assert '"bridge_target_matches": null' in rendered
    assert str(fixture_homes["wsl_codex"]) in rendered


def test_mispointed_symlink_reports_warning(
    fixture_homes: dict[str, Path], hermetic_tmp_path: Path
) -> None:
    wrong_target = hermetic_tmp_path / "wrong-sessions"
    wrong_target.mkdir()
    bridge = fixture_homes["wsl_sessions"] / "from-desktop"
    bridge.symlink_to(wrong_target)

    status = desktop_bridge.collect_bridge_status(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )

    assert status.bridge_present is True
    assert status.bridge_target_matches is False
    assert status.rollout_count == 0
    assert any(
        "does not point at the resolved Windows desktop" in warning for warning in status.warnings
    )


def test_remove_works_when_windows_home_detection_fails(
    fixture_homes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop_bridge.install_desktop_bridge(
        wsl_codex_home=fixture_homes["wsl_codex"],
        windows_codex_home=fixture_homes["windows_codex"],
    )
    bridge = fixture_homes["wsl_sessions"] / "from-desktop"

    def fail_detection(*, explicit: Path | None = None) -> Path:
        raise ValidationError("Windows Codex home detection failed.")

    monkeypatch.setattr(
        "ai_dev_loop.integrations.codex.desktop_bridge.resolve_windows_codex_home",
        fail_detection,
    )

    status = desktop_bridge.remove_desktop_bridge(wsl_codex_home=fixture_homes["wsl_codex"])

    assert not bridge.exists()
    assert status.bridge_present is False
