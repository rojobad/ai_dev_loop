"""Unit tests for safe Codex session-runtime extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import (
    CROSS_FAMILY_COMPACTION_WARNING,
    cross_family_compaction_warning,
    model_family,
    read_codex_session_runtime,
    require_codex_session_id,
)
from conftest import (
    DEFAULT_FIXTURE_SESSION_ID,
    SENSITIVE_SENTINEL,
    write_session_rollout,
)


def _settings_event(
    *,
    event_type: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timestamp: str = "2026-07-10T12:01:00.000Z",
) -> str:
    payload: dict[str, object] = {"type": event_type}
    if model is not None:
        payload["model"] = model
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    return json.dumps({"timestamp": timestamp, "type": "event_msg", "payload": payload})


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_require_codex_session_id_accepts_exact_uuid_and_strips_whitespace() -> None:
    assert require_codex_session_id(f" {DEFAULT_FIXTURE_SESSION_ID} ") == DEFAULT_FIXTURE_SESSION_ID


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", "../session", "abc/def"])
def test_require_codex_session_id_rejects_missing_or_unsafe_values(value: str | None) -> None:
    with pytest.raises(ValidationError, match="session id"):
        require_codex_session_id(value)


def test_reads_exact_native_wsl_session(codex_home: Path) -> None:
    write_session_rollout(codex_home / "sessions")

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.session_id == DEFAULT_FIXTURE_SESSION_ID
    assert runtime.model == "gpt-5.6-sol"
    assert runtime.reasoning_effort == "high"
    assert runtime.origin == "native_wsl"
    assert runtime.source_event_type == "turn_context"
    assert runtime.source_timestamp == "2026-07-10T12:00:02.000Z"


def test_reads_desktop_session_through_bridge_symlink(codex_home: Path, tmp_path: Path) -> None:
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    desktop_sessions = tmp_path / "windows-codex" / "sessions"
    write_session_rollout(desktop_sessions)
    (sessions / "from-desktop").symlink_to(desktop_sessions, target_is_directory=True)

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.origin == "desktop_bridge"
    assert runtime.model == "gpt-5.6-sol"


def test_latest_turn_context_values_win_after_settings_and_mid_session_change(
    codex_home: Path,
) -> None:
    write_session_rollout(
        codex_home / "sessions",
        model="gpt-5.5",
        reasoning_effort="medium",
        extra_lines=[
            _settings_event(
                event_type="thread_settings_applied",
                model="gpt-5.6-sol",
                timestamp="2026-07-10T12:01:00.000Z",
            ),
            _settings_event(
                event_type="turn_context",
                reasoning_effort="xhigh",
                timestamp="2026-07-10T12:01:01.000Z",
            ),
        ],
    )

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.model == "gpt-5.6-sol"
    assert runtime.reasoning_effort == "xhigh"
    assert runtime.source_event_type == "turn_context"
    assert runtime.source_timestamp == "2026-07-10T12:01:01.000Z"


@pytest.mark.parametrize(
    ("model", "reasoning_effort", "message"),
    [
        ("", "high", "missing a model"),
        ("gpt-5.6-sol", "", "missing a reasoning effort"),
    ],
)
def test_missing_required_runtime_scalar_fails(
    codex_home: Path,
    model: str,
    reasoning_effort: str,
    message: str,
) -> None:
    write_session_rollout(
        codex_home / "sessions",
        model=model,
        reasoning_effort=reasoning_effort,
    )

    with pytest.raises(ValidationError, match=message):
        read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)


def test_malformed_json_error_does_not_include_raw_line_or_transcript(
    codex_home: Path,
) -> None:
    write_session_rollout(
        codex_home / "sessions",
        extra_lines=[f'{{"private":"{SENSITIVE_SENTINEL}"'],
    )

    with pytest.raises(ValidationError) as exc_info:
        read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    message = str(exc_info.value)
    assert "malformed JSON" in message
    assert SENSITIVE_SENTINEL not in message
    assert '"private"' not in message


def test_duplicate_distinct_rollouts_are_rejected_as_ambiguous(codex_home: Path) -> None:
    write_session_rollout(codex_home / "sessions" / "2026" / "07" / "10")
    write_session_rollout(
        codex_home / "sessions" / "2026" / "07" / "11",
        timestamp="2026-07-11T12-00-00",
    )

    with pytest.raises(ValidationError, match="ambiguous"):
        read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)


def test_filename_and_session_meta_id_mismatch_is_rejected(codex_home: Path) -> None:
    rollout = write_session_rollout(codex_home / "sessions")
    other_id = "019abc00-0000-0000-0000-000000000001"
    content = rollout.read_text(encoding="utf-8").replace(
        DEFAULT_FIXTURE_SESSION_ID,
        other_id,
        1,
    )
    rollout.write_text(content, encoding="utf-8")

    with pytest.raises(ValidationError, match="does not match"):
        read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)


def test_missing_exact_session_has_actionable_error_without_session_id(codex_home: Path) -> None:
    write_session_rollout(
        codex_home / "sessions",
        session_id="019abc00-0000-0000-0000-000000000001",
    )

    with pytest.raises(ValidationError) as exc_info:
        read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    message = str(exc_info.value)
    assert "exact session id" in message
    assert "sessions install" in message
    assert DEFAULT_FIXTURE_SESSION_ID not in message


def test_transcript_sentinel_never_enters_runtime_metadata(codex_home: Path) -> None:
    write_session_rollout(codex_home / "sessions", include_transcript_sentinel=True)

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert SENSITIVE_SENTINEL not in repr(runtime)
    assert SENSITIVE_SENTINEL not in runtime.model
    assert SENSITIVE_SENTINEL not in runtime.reasoning_effort


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("gpt-5.5", "gpt-5.5"),
        ("gpt-5.5-codex", "gpt-5.5"),
        ("GPT-5-6-SOL", "gpt-5.6"),
        ("gpt-5.6-sol", "gpt-5.6"),
        ("gpt-5", None),
        ("o4-mini", None),
        ("prefix-gpt-5.6", None),
    ],
)
def test_model_family_is_conservative(model: str, family: str | None) -> None:
    assert model_family(model) == family


def test_cross_family_warning_is_non_universal_and_only_for_55_56_switches() -> None:
    assert (
        cross_family_compaction_warning("gpt-5.5", "gpt-5.6-sol") == CROSS_FAMILY_COMPACTION_WARNING
    )
    assert cross_family_compaction_warning("gpt-5.6-sol", "gpt-5.5") is not None
    assert cross_family_compaction_warning("gpt-5.6-sol", "gpt-5.6-codex") is None
    assert cross_family_compaction_warning("o4-mini", "gpt-5.6-sol") is None
    assert "puede" in CROSS_FAMILY_COMPACTION_WARNING
    assert "siempre" not in CROSS_FAMILY_COMPACTION_WARNING.lower()


def test_reads_desktop_production_effort_and_thread_settings_shapes(codex_home: Path) -> None:
    write_session_rollout(
        codex_home / "sessions",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        desktop_production_shape=True,
    )

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.model == "gpt-5.6-sol"
    assert runtime.reasoning_effort == "high"
    assert runtime.source_event_type == "turn_context"


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_captures_gpt56_max_and_ultra_desktop_efforts(codex_home: Path, effort: str) -> None:
    write_session_rollout(
        codex_home / "sessions",
        model="gpt-5.6-sol",
        reasoning_effort=effort,
        desktop_production_shape=True,
    )

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.model == "gpt-5.6-sol"
    assert runtime.reasoning_effort == effort
    assert runtime.source_event_type == "turn_context"


def test_propagated_mnt_codex_home_falls_back_to_native_bridge(
    hermetic_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_home = hermetic_home / ".codex"
    sessions = native_home / "sessions"
    sessions.mkdir(parents=True)
    desktop_sessions = tmp_path / "windows-codex" / "sessions"
    write_session_rollout(desktop_sessions, desktop_production_shape=True)
    (sessions / "from-desktop").symlink_to(desktop_sessions, target_is_directory=True)

    # Simulate Codex Desktop propagating a Windows/DrvFS CODEX_HOME into WSL.
    monkeypatch.setenv("CODEX_HOME", "/mnt/c/Users/WinUser/.codex")

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.origin == "desktop_bridge"
    assert runtime.model == "gpt-5.6-sol"
    assert runtime.reasoning_effort == "high"


def test_forked_session_meta_entries_still_accept_matching_id(codex_home: Path) -> None:
    other_id = "019abc00-1111-2222-3333-444444444444"
    write_session_rollout(
        codex_home / "sessions",
        desktop_production_shape=True,
        extra_lines=[
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:04.000Z",
                    "type": "session_meta",
                    "payload": {"id": other_id},
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:05.000Z",
                    "type": "session_meta",
                    "payload": {"id": DEFAULT_FIXTURE_SESSION_ID},
                }
            ),
        ],
    )

    runtime = read_codex_session_runtime(DEFAULT_FIXTURE_SESSION_ID)

    assert runtime.session_id == DEFAULT_FIXTURE_SESSION_ID
    assert runtime.reasoning_effort == "high"
