"""Public ``pr-review`` CLI namespace cutover tests (Phase 16.9)."""

from __future__ import annotations

from typer.testing import CliRunner

from ai_dev_loop.cli import app


def test_public_pr_review_exposes_v2_command_set() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["pr-review", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("create", "prepare", "start", "status", "history", "resume", "abort"):
        assert name in result.output
    for retired in ("continue", "recover", "set-cursor-model"):
        assert retired not in result.output


def test_pr_review_v2_namespace_is_absent() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["pr-review-v2", "status", "run-id"])
    assert result.exit_code != 0
    assert "No such command" in result.output or "pr-review-v2" in result.output


def test_prepare_does_not_call_start(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_prepare(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        calls.append("prepare")
        return "pr-review created prepared run x\nnext: ai_dev_loop pr-review start x\n"

    def fail_start(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        calls.append("start")
        raise AssertionError("start must not run during prepare")

    monkeypatch.setattr("ai_dev_loop.commands.pr_review_v2.prepare_existing_pr", fake_prepare)
    monkeypatch.setattr("ai_dev_loop.commands.pr_review_v2.start_run", fail_start)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "pr-review",
            "prepare",
            "--repo",
            "acme/demo",
            "--pr",
            "1",
            "--codex-session-id",
            "019abc00-0000-0000-0000-000000000099",
            "--plan",
            "plan.md",
            "--prompt",
            "prompt.txt",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["prepare"]


def test_start_is_explicit_effects_gate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_start(run_id: str) -> str:
        calls.append(f"start:{run_id}")
        return f"pr-review start {run_id}\nstate: active\n"

    monkeypatch.setattr("ai_dev_loop.commands.pr_review_v2.start_run", fake_start)

    runner = CliRunner()
    result = runner.invoke(app, ["pr-review", "start", "prv2-test-run"])
    assert result.exit_code == 0, result.output
    assert calls == ["start:prv2-test-run"]
    assert "pr-review start" in result.output
