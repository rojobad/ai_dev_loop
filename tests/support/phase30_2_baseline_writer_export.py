"""Schema-20 ledger export. Collected only after copy into the baseline archive."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from tests.integration.test_phase25_3_sdk_recovery import _submit_sdk_run, _tick_until_kind
from tests.unit.scheduler.test_phase27_2_codex_implementer import _run_until, _submit, _tick

from ai_dev_loop.scheduler.application.start import start_run

_OUT = Path(os.environ["BASELINE_EXPORT_ROOT"])
_IMPLEMENTER_110 = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}
_IMPLEMENTER_50 = {"input_tokens": 40, "cached_input_tokens": 0, "output_tokens": 10}
_REVIEW = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}
_CURSOR = {
    "input_tokens": 4820,
    "output_tokens": 139,
    "cache_read_tokens": 2560,
    "cache_write_tokens": 0,
    "total_tokens": 7519,
    "reasoning_tokens": 118,
}


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    state = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": state / "engine.sqlite3", "artifact_root": state / "artifacts"}


def test_export_codex_roles(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "implemented-by-codex.txt")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([[_IMPLEMENTER_110, _IMPLEMENTER_50]]),
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW]))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")
    _publish(scheduler_paths["db_path"], scheduler_paths["artifact_root"], "codex", run_id)


def test_export_cursor(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON", json.dumps([_REVIEW]))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_CURSOR))
    run_id, database, artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="Baseline cursor usage",
    )
    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    _publish(database, artifacts, "cursor", run_id)


def _publish(database: Path, artifacts: Path, name: str, run_id: str) -> None:
    destination = _OUT / name
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    if version != 20 or "scheduler_usage_attempt_facts" in tables:
        raise AssertionError(f"baseline export {name} is not schema 20 without usage facts")
    shutil.copyfile(database, destination / "engine.sqlite3")
    shutil.copytree(artifacts, destination / "artifacts")
    (destination / "run-id.txt").write_text(run_id, encoding="utf-8")
    (destination / "provenance.json").write_text(
        json.dumps(
            {
                "writer_commit": "1a97becaff1af8c54f892f42740f040d4cd7478c",
                "schema_user_version": version,
                "scope": name,
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
