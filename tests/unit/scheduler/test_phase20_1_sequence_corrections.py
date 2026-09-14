"""Regression tests for Phase 20.1 Codex correction findings."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_NOW,
    _prepare_service,
    _two_phase_manifest,
    _write_manifest,
)
from tests.unit.scheduler.test_v5_migration import _pause_v4_database

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    SequencePrepareService,
    _format_manifest_validation_error,
    _parse_manifest,
)
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.sequence import SequenceManifest
from ai_dev_loop.scheduler.infrastructure.paths import (
    SEQUENCES_DIRNAME,
    reject_repository_overlap,
    sequence_artifact_root,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_sequence_artifact_root_rejects_symlinked_sequences_dir(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = artifact_root / SEQUENCES_DIRNAME
    link.symlink_to(outside)
    store = ProtectedArtifactStore(artifact_root)
    with pytest.raises(ValueError, match="symlink"):
        store.write_sequence_text(
            "seq-1",
            "manifest/original.yaml",
            "schema_version: 1\n",
            max_bytes=1024,
        )


def test_sequence_artifact_rejects_symlinked_sequence_root(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    sequences_dir = artifact_root / SEQUENCES_DIRNAME
    sequences_dir.mkdir(parents=True)
    outside = tmp_path / "outside-seq"
    outside.mkdir()
    key = hashlib.sha256(b"seq-1").hexdigest()
    link = sequences_dir / key
    link.symlink_to(outside)
    store = ProtectedArtifactStore(artifact_root)
    with pytest.raises(ValueError, match="symlink"):
        store.write_sequence_text(
            "seq-1",
            "manifest/original.yaml",
            "schema_version: 1\n",
            max_bytes=1024,
        )


def test_sequence_artifact_rejects_nested_symlink_inside_root(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    seq_root = sequence_artifact_root(artifact_root, "seq-1")
    seq_root.mkdir(parents=True)
    internal = seq_root / "internal"
    internal.mkdir()
    (internal / "target.txt").write_text("target", encoding="utf-8")
    link = seq_root / "entries"
    link.symlink_to(internal)
    with pytest.raises(ValueError, match="symlink"):
        store.write_sequence_text(
            "seq-1",
            "entries/01/plan/plan.md",
            "plan body\n",
            max_bytes=1024,
        )


def test_sequence_artifact_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    link = tmp_path / "artifacts-link"
    link.symlink_to(outside)
    store = ProtectedArtifactStore(link)
    with pytest.raises(ValueError, match="symlink"):
        store.write_sequence_text(
            "seq-1",
            "manifest/original.yaml",
            "schema_version: 1\n",
            max_bytes=1024,
        )


def test_reject_repository_overlap_blocks_nested_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    nested = repo / "artifacts"
    nested.mkdir()
    with pytest.raises(ValueError, match="inside the target repository"):
        reject_repository_overlap(nested, repo)


def test_sequence_artifact_rejects_nested_symlink_parent(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    seq_root = sequence_artifact_root(artifact_root, "seq-1")
    seq_root.mkdir(parents=True)
    outside = tmp_path / "outside-nested"
    outside.mkdir()
    link = seq_root / "entries"
    link.symlink_to(outside)
    with pytest.raises((ValueError, ProtectedArtifactError), match="symlink|escapes"):
        store.write_sequence_text(
            "seq-1",
            "entries/01/plan/plan.md",
            "plan body\n",
            max_bytes=1024,
        )


def test_reuse_verifies_artifact_integrity(
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
    seq_root = sequence_artifact_root(scheduler_paths["artifact_root"], first.sequence_id)
    plan_path = seq_root / "entries/01/plan/plan.md"
    plan_path.write_text("corrupted plan bytes\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="integrity verification"):
        service.prepare(options)


def test_prepare_retry_is_deterministic_after_partial_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    attempts = {"count": 0}

    def fault_hook(step: str) -> None:
        if step == "manifest_original" and attempts["count"] == 0:
            attempts["count"] += 1
            raise RuntimeError("injected fault after manifest original")

    service = SequencePrepareService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
        now_factory=lambda: FIXED_NOW,
        prepare_step_hook=fault_hook,
    )
    options = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with pytest.raises(RuntimeError, match="injected fault"):
        service.prepare(options)
    result = service.prepare(options)
    replay = service.prepare(options)
    assert result.reused_existing is False
    assert replay.reused_existing is True
    assert replay.sequence_id == result.sequence_id


def test_conflicting_manifest_bytes_block_reuse(
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
    seq_root = sequence_artifact_root(scheduler_paths["artifact_root"], first.sequence_id)
    original = seq_root / "manifest/original.yaml"
    original.write_bytes(b"schema_version: 1\nname: conflicting\nphases: []\n")
    with pytest.raises(ProtectedArtifactError, match="hash mismatch"):
        service.artifacts.write_sequence_bytes_or_verify(
            first.sequence_id,
            "manifest/original.yaml",
            manifest.read_bytes(),
            max_bytes=1024 * 1024,
        )


def test_compare_and_swap_rejects_stale_and_immutable_changes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
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
    store = SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False)
    with store.begin_read() as conn:
        current = store.load_validated_sequence_state(conn, result.sequence_id)
    bumped = current.model_copy(
        update={
            "version": 2,
            "updated_at": current.updated_at,
        }
    )
    now = datetime(2026, 9, 12, 12, 5, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        assert not store.compare_and_swap_sequence_state(
            conn,
            sequence_id=result.sequence_id,
            expected_version=99,
            new_state=bumped,
            now=now,
        )
        assert store.compare_and_swap_sequence_state(
            conn,
            sequence_id=result.sequence_id,
            expected_version=1,
            new_state=bumped,
            now=now,
        )
    with store.begin_immediate() as conn:
        reloaded = store.load_validated_sequence_state(conn, result.sequence_id)
        mutated_identity = reloaded.model_copy(
            update={
                "version": 3,
                "definition": reloaded.definition.model_copy(update={"name": "changed-name"}),
            }
        )
        with pytest.raises(SchedulerEngineError, match="definition is immutable"):
            store.compare_and_swap_sequence_state(
                conn,
                sequence_id=result.sequence_id,
                expected_version=2,
                new_state=mutated_identity,
                now=now,
            )
        with pytest.raises(SchedulerEngineError, match="expected_version \\+ 1"):
            store.compare_and_swap_sequence_state(
                conn,
                sequence_id=result.sequence_id,
                expected_version=2,
                new_state=reloaded.model_copy(update={"version": 2}),
                now=now,
            )


@pytest.mark.parametrize(
    ("mutation"),
    [
        lambda state: state.model_copy(
            update={
                "version": 2,
                "definition": state.definition.model_copy(
                    update={"sequence_id": "changed-sequence-id"}
                ),
            }
        ),
        lambda state: state.model_copy(
            update={
                "version": 2,
                "definition": state.definition.model_copy(
                    update={
                        "entries": tuple(
                            entry.model_copy(update={"planned_run_id": "changed-run-id"})
                            for entry in state.definition.entries
                        )
                    }
                ),
            }
        ),
        lambda state: state.model_copy(
            update={
                "version": 2,
                "definition": state.definition.model_copy(
                    update={
                        "controller": state.definition.controller.model_copy(
                            update={"controller_session_id": CONTROLLER_SESSION}
                        )
                    }
                ),
            }
        ),
    ],
)
def test_compare_and_swap_rejects_definition_mutations(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    mutation,
) -> None:
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
    store = SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False)
    now = datetime(2026, 9, 12, 12, 5, 0, tzinfo=UTC)
    with store.begin_read() as conn:
        original = store.load_validated_sequence_state(conn, result.sequence_id)
    mutated = mutation(original)
    with (
        store.begin_immediate() as conn,
        pytest.raises(SchedulerEngineError, match="definition is immutable"),
    ):
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=result.sequence_id,
            expected_version=1,
            new_state=mutated,
            now=now,
        )
    with store.begin_read() as conn:
        unchanged = store.load_validated_sequence_state(conn, result.sequence_id)
    assert unchanged == original


def test_prepare_rejects_insecure_partial_artifacts_before_insert(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")

    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    attempts = {"count": 0}

    def fault_hook(step: str) -> None:
        if step == "manifest_original" and attempts["count"] == 0:
            attempts["count"] += 1
            raise RuntimeError("injected fault after manifest original")

    service = SequencePrepareService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
        now_factory=lambda: FIXED_NOW,
        prepare_step_hook=fault_hook,
    )
    options = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with pytest.raises(RuntimeError, match="injected fault"):
        service.prepare(options)
    partial_root = next((scheduler_paths["artifact_root"] / SEQUENCES_DIRNAME).iterdir())
    manifest_path = partial_root / "manifest/original.yaml"
    os.chmod(manifest_path, 0o644)
    with pytest.raises(ProtectedArtifactError, match="unsafe permissions"):
        service.prepare(options)
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_sequences").fetchone()[0] == 0


def test_load_validated_sequence_state_rejects_empty_entry_payload(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
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
    empty_payload = "{}"
    empty_digest = hashlib.sha256(empty_payload.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(scheduler_paths["db_path"])
    conn.execute(
        """
        UPDATE scheduler_sequence_entries
        SET payload = ?, payload_sha256 = ?
        WHERE sequence_id = ? AND ordinal = 1
        """,
        (empty_payload, empty_digest, result.sequence_id),
    )
    conn.commit()
    conn.close()
    store = SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="entry payload failed validation"),
    ):
        store.load_validated_sequence_state(conn, result.sequence_id)


def test_load_validated_sequence_state_rejects_aggregate_entry_mismatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
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
    store = SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False)
    with store.begin_read() as conn:
        state = store.load_validated_sequence_state(conn, result.sequence_id)
    entry = state.definition.entries[0]
    tampered = entry.model_copy(update={"phase_name": "tampered-phase"})
    payload, digest = store.dump_sequence_entry(tampered)
    conn = sqlite3.connect(scheduler_paths["db_path"])
    conn.execute(
        """
        UPDATE scheduler_sequence_entries
        SET payload = ?, payload_sha256 = ?, phase_name = ?
        WHERE sequence_id = ? AND ordinal = 1
        """,
        (payload, digest, entry.phase_name, result.sequence_id),
    )
    conn.commit()
    conn.close()
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="disagrees with aggregate state"),
    ):
        store.load_validated_sequence_state(conn, result.sequence_id)


def test_manifest_validation_errors_do_not_leak_sentinel_values() -> None:
    secret = "SENTINEL_SECRET_DO_NOT_LEAK"
    raw = textwrap.dedent(
        f"""
        schema_version: 1
        name: {secret}
        extra_field: {secret}
        phases:
          - name: one
            plan_path: a
            prompt_source_path: b
            commit_message: one
            codex:
              review_model: m
              review_reasoning_effort: high
          - name: two
            plan_path: a
            prompt_source_path: b
            codex:
              review_model: m
              review_reasoning_effort: high
        """
    ).encode("utf-8")
    with pytest.raises(ValidationError) as exc:
        _parse_manifest(raw)
    rendered = str(exc.value)
    assert secret not in rendered


def test_manifest_validation_reports_field_locations_without_values() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError) as exc:
        SequenceManifest.model_validate(
            {
                "schema_version": 1,
                "name": "",
                "phases": [],
            }
        )
    message = _format_manifest_validation_error(exc.value)
    assert "name" in message
    assert "phases" in message


def test_open_readonly_accepts_populated_v4_database(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    store = SqliteSchedulerStore(db, bootstrap=False)
    state = sample_submitted_state(run_id="fixture-run-v4")
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-v4-readonly",
            event=event,
            now=now,
        )
    readonly = SqliteSchedulerStore.open_readonly(db)
    assert readonly._read_only is True
    listings = scheduler_list(db_path=db)
    assert len(listings) == 1
    status = scheduler_status(state.run_id, db_path=db)
    assert status.summary.run_id == state.run_id


def test_sequence_status_requires_schema_version_five(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    service = SequenceStatusService(SqliteSchedulerStore.open_readonly(db))
    with pytest.raises(SchedulerEngineError) as exc:
        service.get_status("missing-sequence")
    assert exc.value.kind is SchedulerEngineErrorKind.SCHEMA
    assert "schema version 5" in str(exc.value)
