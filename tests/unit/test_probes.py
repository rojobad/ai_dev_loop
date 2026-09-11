"""Tests for local CLI probes."""

from __future__ import annotations

from ai_dev_loop.runners.probes import (
    ProbeResult,
    _parse_cursor_models,
    probe_cursor_auth,
    probe_cursor_model,
)


def test_parse_cursor_models_supports_formatted_output() -> None:
    stdout = "\n".join(
        [
            "Available models",
            "",
            "auto - Auto (default)",
            "composer-2.5-fast - Composer 2.5 Fast",
        ]
    )
    assert _parse_cursor_models(stdout) == {"auto", "composer-2.5-fast"}


def test_parse_cursor_models_supports_plain_lines() -> None:
    assert _parse_cursor_models("composer-2.5-fast\n") == {"composer-2.5-fast"}


def test_probe_cursor_auth_accepts_is_authenticated(monkeypatch) -> None:
    def fake_run_process(args, **kwargs):
        assert args[1:] == ["status", "--format", "json"]

        class Result:
            returncode = 0
            stdout = '{"status":"authenticated","isAuthenticated":true}'
            stderr = ""
            timed_out = False

        return Result()

    monkeypatch.setattr("ai_dev_loop.runners.probes.run_process", fake_run_process)
    result = probe_cursor_auth("agent")
    assert result == ProbeResult(command="agent", ok=True, detail="authenticated")


def test_probe_cursor_model_matches_formatted_models(monkeypatch) -> None:
    def fake_run_process(args, **kwargs):
        assert args == ["agent", "models"]

        class Result:
            returncode = 0
            stdout = "composer-2.5-fast - Composer 2.5 Fast\n"
            stderr = ""
            timed_out = False

        return Result()

    monkeypatch.setattr("ai_dev_loop.runners.probes.run_process", fake_run_process)
    result = probe_cursor_model("agent", "composer-2.5-fast")
    assert result.ok is True
