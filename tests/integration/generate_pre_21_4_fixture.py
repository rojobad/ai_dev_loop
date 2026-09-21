"""One-off generator for tests/fixtures/phase21_4_pre_21_4_historical (run with GENERATE_PRE21_FIXTURE=1)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from tests.integration.phase21_4_helpers import (
    BOOTSTRAP_ID,
    make_tick_service,
    run_until,
    submit_sample_run,
)

from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

FIXTURE_DEST = Path(__file__).resolve().parents[1] / "fixtures" / "phase21_4_pre_21_4_historical"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


@pytest.mark.skipif(
    os.environ.get("GENERATE_PRE21_FIXTURE") != "1",
    reason="set GENERATE_PRE21_FIXTURE=1 to regenerate frozen pre-21.4 review fixture",
)
def test_generate_pre_21_4_historical_review_fixture(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=80)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    with tick.store.begin_read() as conn:
        rows, _ = tick.store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=5)
    row = next(r for r in rows if str(r["effect_kind"]) == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND)
    attempt_id = str(row["attempt_id"])
    iteration = int(row["iteration"])
    (run_root / codex_review_prompt_rel(iteration, attempt_id)).unlink(missing_ok=True)
    (run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)).unlink(missing_ok=True)
    if FIXTURE_DEST.exists():
        shutil.rmtree(FIXTURE_DEST)
    FIXTURE_DEST.mkdir(parents=True)
    shutil.copytree(run_root, FIXTURE_DEST / "run_root")
    shutil.copy2(scheduler_paths["db_path"], FIXTURE_DEST / "engine.sqlite3")
    shutil.copytree(
        scheduler_paths["artifact_root"],
        FIXTURE_DEST / "artifacts",
        dirs_exist_ok=True,
    )
    manifest = {
        "provenance": {
            "baseline_commit": "e4c6b08824b76d3ef7fdf38a6d30f6533b5aed02",
            "writer": (
                "codex_attempt_runner at baseline e4c6b088 had no "
                "publish_review_prompt_before_launch; fixture omits .prompt.txt and "
                ".prompt-evidence.json by construction."
            ),
        },
        "run_id": run_id,
        "attempt_id": attempt_id,
        "review_iteration": iteration,
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    }
    (FIXTURE_DEST / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
