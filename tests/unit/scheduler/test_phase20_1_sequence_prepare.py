"""Unit tests for Phase 20.1 scheduler sequence preparation."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError
from tests.unit.scheduler.helpers import CONTROLLER_SESSION

from ai_dev_loop.commands.scheduler import (
    render_sequence_prepare_output,
    render_sequence_status_output,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    SequencePrepareService,
    _parse_manifest,
)
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import MAX_SEQUENCE_PHASE_COUNT, SequenceManifest
from ai_dev_loop.scheduler.infrastructure.paths import sequence_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore

RESUBMISSION_ID = "22222222-2222-2222-2222-222222222222"
FIXED_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
FIXED_SEQUENCE_ID = "fixture-project-seq-20260912T120000Z-abc123"
FIXED_RUN_IDS = (
    "fixture-project-20260912T120000Z-run001",
    "fixture-project-20260912T120000Z-run002",
)


def _manifest_yaml(*, phases: list[dict[str, object]], name: str = "fixture-sequence") -> str:
    payload = {"schema_version": 1, "name": name, "phases": phases}
    return yaml.safe_dump(payload, sort_keys=False)


def _two_phase_manifest() -> str:
    return _manifest_yaml(
        phases=[
            {
                "name": "phase-one",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "commit_message": "checkpoint after phase one",
                "codex": {
                    "review_model": "gpt-5.6-sol",
                    "review_reasoning_effort": "high",
                },
            },
            {
                "name": "phase-two",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "codex": {
                    "review_model": "gpt-5.6-sol",
                    "review_reasoning_effort": "high",
                },
            },
        ]
    )


def _write_manifest(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _fake_repo_target(repo: Path) -> RepositoryTarget:
    return RepositoryTarget(root=repo.resolve())


def _prepare_service(
    *,
    db_path: Path,
    artifact_root: Path,
    repo: Path,
) -> SequencePrepareService:
    run_counter = {"value": 0}

    def run_id_factory(_slug: str, _now: datetime) -> str:
        index = run_counter["value"]
        run_counter["value"] += 1
        return f"fixture-project-20260912T120000Z-run{index:03d}"

    return SequencePrepareService(
        SqliteSchedulerStore(db_path),
        ProtectedArtifactStore(artifact_root),
        repository_discoverer=lambda _path: _fake_repo_target(repo),
        now_factory=lambda: FIXED_NOW,
        sequence_id_factory=lambda _slug, _now: FIXED_SEQUENCE_ID,
        run_id_factory=run_id_factory,
    )


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_manifest_rejects_invalid_phase_counts() -> None:
    with pytest.raises(PydanticValidationError, match="between 2 and 32"):
        SequenceManifest.model_validate(
            {
                "schema_version": 1,
                "name": "x",
                "phases": [
                    {
                        "name": "only",
                        "plan_path": "a",
                        "prompt_source_path": "b",
                        "codex": {"review_model": "m", "review_reasoning_effort": "high"},
                    }
                ],
            }
        )
    too_many = [
        {
            "name": f"phase-{index:02d}",
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "commit_message": "checkpoint",
            "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
        }
        for index in range(1, MAX_SEQUENCE_PHASE_COUNT + 2)
    ]
    too_many[-1] = {
        "name": "final",
        "plan_path": "docs/plans/sample-plan.md",
        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
        "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
    }
    with pytest.raises(PydanticValidationError, match="between 2 and 32"):
        SequenceManifest.model_validate({"schema_version": 1, "name": "x", "phases": too_many})


def test_manifest_rejects_duplicate_names_and_bad_commit_messages() -> None:
    with pytest.raises(PydanticValidationError, match="unique"):
        SequenceManifest.model_validate(
            yaml.safe_load(
                _manifest_yaml(
                    phases=[
                        {
                            "name": "dup",
                            "plan_path": "a",
                            "prompt_source_path": "b",
                            "commit_message": "one",
                            "codex": {
                                "review_model": "gpt-5.6-sol",
                                "review_reasoning_effort": "high",
                            },
                        },
                        {
                            "name": "dup",
                            "plan_path": "a",
                            "prompt_source_path": "b",
                            "codex": {
                                "review_model": "gpt-5.6-sol",
                                "review_reasoning_effort": "high",
                            },
                        },
                    ]
                )
            )
        )
    with pytest.raises(PydanticValidationError, match="commit_message"):
        SequenceManifest.model_validate(
            yaml.safe_load(
                _manifest_yaml(
                    phases=[
                        {
                            "name": "one",
                            "plan_path": "a",
                            "prompt_source_path": "b",
                            "codex": {
                                "review_model": "gpt-5.6-sol",
                                "review_reasoning_effort": "high",
                            },
                        },
                        {
                            "name": "two",
                            "plan_path": "a",
                            "prompt_source_path": "b",
                            "commit_message": "not allowed on final",
                            "codex": {
                                "review_model": "gpt-5.6-sol",
                                "review_reasoning_effort": "high",
                            },
                        },
                    ]
                )
            )
        )


def test_parse_manifest_rejects_unknown_keys(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "bad.yaml",
        textwrap.dedent(
            """
            schema_version: 1
            name: bad
            extra: true
            phases: []
            """
        ),
    )
    with pytest.raises(ValidationError):
        _parse_manifest(manifest.read_bytes())


def test_prepare_freezes_sequence_without_scheduler_runs(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
) -> None:
    del fake_clis
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
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
    assert result.sequence_id == FIXED_SEQUENCE_ID
    assert result.reused_existing is False
    assert "Phase 20.2" in (result.safe_next_action.command or "")
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheduler_sequences").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM scheduler_sequence_entries").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_repository_reservations").fetchone()[0]
            == 0
        )
    seq_root = sequence_artifact_root(scheduler_paths["artifact_root"], FIXED_SEQUENCE_ID)
    assert seq_root.exists()
    assert (seq_root / "entries/01/plan/plan.md").is_file()
    assert (seq_root / "entries/02/prompts/cursor-initial.txt").is_file()
    assert not (git_repo / "entries").exists()


def test_identical_prepare_reuses_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    options = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    first = service.prepare(options)
    second = service.prepare(options)
    assert first.sequence_id == second.sequence_id
    assert second.reused_existing is True


def test_controller_provenance_does_not_change_sequence_identity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    base = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    first = service.prepare(base)
    second = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            controller_session_id=CONTROLLER_SESSION,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    assert first.sequence_id == second.sequence_id


def test_resubmission_id_creates_fresh_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    counter = {"value": 0}

    def sequence_id_factory(_slug: str, _now: datetime) -> str:
        counter["value"] += 1
        return f"fixture-project-seq-20260912T120000Z-seq{counter['value']:03d}"

    run_counter = {"value": 0}

    def unique_run_id(_slug: str, _now: datetime) -> str:
        index = run_counter["value"]
        run_counter["value"] += 1
        return f"fixture-project-20260912T120000Z-run{index:03d}"

    service = SequencePrepareService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: _fake_repo_target(git_repo),
        now_factory=lambda: FIXED_NOW,
        sequence_id_factory=sequence_id_factory,
        run_id_factory=unique_run_id,
    )
    first = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            resubmission_id=RESUBMISSION_ID,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    second = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    replay = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            resubmission_id=RESUBMISSION_ID,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    assert first.sequence_id != second.sequence_id
    assert first.sequence_id == replay.sequence_id
    assert replay.reused_existing is True


def test_prepare_output_does_not_leak_prompt_or_plan(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    prompt_text = (git_repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    plan_text = (git_repo / "docs/plans/sample-plan.md").read_text(encoding="utf-8")
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
    text = render_sequence_prepare_output(result, output="text")
    json_text = render_sequence_prepare_output(result, output="json")
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(result.sequence_id)
    status_text = render_sequence_status_output(status, output="text")
    status_json = render_sequence_status_output(status, output="json")
    for rendered in (text, json_text, status_text, status_json):
        assert prompt_text not in rendered
        assert plan_text not in rendered
        assert CONTROLLER_SESSION not in rendered


def test_schema_version_is_five_after_bootstrap(scheduler_paths: dict[str, Path]) -> None:
    SqliteSchedulerStore(scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 5
