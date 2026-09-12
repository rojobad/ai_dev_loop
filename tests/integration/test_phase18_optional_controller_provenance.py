"""Integration tests for Phase 18 optional controller provenance and timeline."""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION

from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.commands.scheduler import render_status_output, render_submit_output
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import assets
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.timeline import scheduler_timeline
from ai_dev_loop.scheduler.infrastructure.systemd_assets import (
    load_timer_template,
    validate_packaged_assets,
)

_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _submit_options(
    repo: Path,
    *,
    db_path: Path,
    artifact_root: Path,
    controller_session_id: str | None = None,
) -> SubmitOptions:
    return SubmitOptions(
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=controller_session_id,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=db_path,
        artifact_root=artifact_root,
    )


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_submit_without_controller_succeeds(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))
    assert result.state_kind == "queued"
    assert result.safe_next_action.command == f"ai_dev_loop scheduler start {result.run_id}"
    rendered = render_submit_output(result, output="json")
    assert "controller-session-id" not in rendered


def test_invalid_optional_controller_rejected(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths, controller_session_id="not-a-uuid")
    with patch("sys.stdin", StringIO(prompt)), pytest.raises(ValidationError):
        submit_run(options)


def test_optional_controller_is_redacted_in_public_output(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(
            _submit_options(git_repo, **scheduler_paths, controller_session_id=CONTROLLER_SESSION)
        )
    from ai_dev_loop.scheduler.application.status import scheduler_status

    status = scheduler_status(result.run_id, db_path=scheduler_paths["db_path"])
    assert status.summary.controller_session_id_prefix == "11111111…1111"
    rendered = render_status_output(status, output="json")
    assert CONTROLLER_SESSION not in rendered
    assert _UUID_PATTERN.search(rendered) is None


def test_afree_start_authorizes_and_is_idempotent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        submitted = submit_run(_submit_options(git_repo, **scheduler_paths))
    first = start_run(submitted.run_id, db_path=scheduler_paths["db_path"])
    second = start_run(submitted.run_id, db_path=scheduler_paths["db_path"])
    assert first.changed is True
    assert first.state_kind == "authorized"
    assert second.idempotent_replay is True


def test_idempotency_ignores_optional_controller_provenance(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        without_a = submit_run(_submit_options(git_repo, **scheduler_paths))
    with patch("sys.stdin", StringIO(prompt)):
        with_a = submit_run(
            _submit_options(git_repo, **scheduler_paths, controller_session_id=CONTROLLER_SESSION)
        )
    assert without_a.run_id == with_a.run_id
    assert with_a.reused_existing is True


def test_controller_status_by_run_id_without_controller(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        submitted = submit_run(_submit_options(git_repo, **scheduler_paths))
    status = controller_status(
        repo_path=git_repo,
        run_id=submitted.run_id,
    )
    assert status.match_count == 1
    assert status.run_id == submitted.run_id
    assert status.max_review_iterations == 3
    assert status.review_iterations_completed == 0


def test_controller_status_requires_selector(
    git_repo: Path,
) -> None:
    with pytest.raises(ValidationError, match="requires --run-id or --controller-session-id"):
        controller_status(repo_path=git_repo)


def test_controller_status_rejects_wrong_repository(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    import shutil

    other_repo = tmp_path / "other"
    shutil.copytree(git_repo, other_repo)
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        submitted = submit_run(_submit_options(git_repo, **scheduler_paths))
    status = controller_status(repo_path=other_repo, run_id=submitted.run_id)
    assert status.match_count == 0


def test_timeline_is_bounded_and_redacted(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        submitted = submit_run(_submit_options(git_repo, **scheduler_paths))
    timeline = scheduler_timeline(submitted.run_id, db_path=scheduler_paths["db_path"])
    assert timeline.entries == ()
    payload = json.loads(
        json.dumps(
            {
                "entries": [entry.model_dump(mode="json") for entry in timeline.entries],
            }
        )
    )
    text = json.dumps(payload)
    assert "attempt_id" not in text
    assert "dispatch_id" not in text


def test_packaged_timer_includes_accuracy_sec() -> None:
    timer = load_timer_template()
    assert "OnBootSec=30" in timer
    assert "OnUnitActiveSec=30" in timer
    assert "AccuracySec=1s" in timer
    assert validate_packaged_assets() == []


def test_skill_assets_do_not_require_controller_for_scheduler_commands() -> None:
    handoff = assets.load_skill_content()
    controller = assets.load_skill_content(assets.CONTROLLER_SKILL)
    start_example = (
        controller.split("### “inicia el run”", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    )
    assert "scheduler start <run-id>" in start_example
    assert "--controller-session-id" not in start_example
    assert "optional" in handoff.lower()
