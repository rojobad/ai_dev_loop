"""Integration tests for Phase 20.1 scheduler sequence prepare boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_SEQUENCE_ID,
    _prepare_service,
    _two_phase_manifest,
    _write_manifest,
)

from ai_dev_loop.errors import UsageError
from ai_dev_loop.paths import runs_dir
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepare_has_no_git_or_subprocess_side_effects(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("sequence prepare must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    result = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )

    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after
    assert not runs_dir().exists()
    assert result.sequence_id == FIXED_SEQUENCE_ID


def test_preassigned_run_id_is_not_scheduler_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        planned = conn.execute(
            "SELECT planned_run_id FROM scheduler_sequence_entries WHERE sequence_id = ?",
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
    assert planned
    for row in planned:
        with pytest.raises(SchedulerEngineError, match="not found"):
            scheduler_status(str(row[0]))


def test_sequence_status_not_found(scheduler_paths: dict[str, Path]) -> None:
    from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService

    SqliteSchedulerStore(scheduler_paths["db_path"])
    service = SequenceStatusService(SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]))
    with pytest.raises(SchedulerEngineError, match="not found"):
        service.get_status("missing-sequence-id")


def test_sequence_start_placeholder_is_non_mutating() -> None:
    with pytest.raises(UsageError, match="Phase 20.2"):
        raise UsageError(
            "scheduler sequence start is not implemented until Phase 20.2; "
            "use ai_dev_loop scheduler sequence status <sequence-id> to inspect a prepared definition"
        )
