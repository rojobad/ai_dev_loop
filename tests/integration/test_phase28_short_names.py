"""Phase 28.1 persistence, CLI, lineage, and read-only compatibility tests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase17_1_submit import (
    _submission_service,
    _submit_options,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    _prepare_service,
    _two_phase_manifest,
    _write_manifest,
)
from tests.unit.scheduler.test_phase20_2_sequence_start import _start_service
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.sqlite_read import SqliteDashboardReadRepository
from ai_dev_loop.dashboard.application.ports import DashboardReadOperation, DashboardReadScope
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService
from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.presentation_metadata import (
    set_run_short_name,
    set_sequence_short_name,
)
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.domain.presentation import require_short_name
from ai_dev_loop.scheduler.domain.sequence import PreparedSequenceState
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _codex_only_v3_manifest() -> str:
    payload = yaml.safe_load(_two_phase_manifest())
    for phase in payload["phases"]:
        phase["implementer"] = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "timeout_minutes": 90,
        }
    return yaml.safe_dump(payload, sort_keys=False)


def _artifact_digests(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _immutable_snapshot(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(db_path) as conn:
        return {
            "runs": conn.execute(
                "SELECT run_id, state_kind, state_payload, state_payload_sha256, "
                "idempotency_key, worktree_key FROM scheduler_runs ORDER BY run_id"
            ).fetchall(),
            "sequences": conn.execute(
                "SELECT sequence_id, state_kind, payload, payload_sha256, idempotency_key, "
                "worktree_key FROM scheduler_sequences ORDER BY sequence_id"
            ).fetchall(),
            "entries": conn.execute(
                "SELECT sequence_id, ordinal, phase_name, planned_run_id, payload, payload_sha256 "
                "FROM scheduler_sequence_entries ORDER BY sequence_id, ordinal"
            ).fetchall(),
            "reservations": conn.execute(
                "SELECT worktree_key, run_id, repository_root, status FROM "
                "scheduler_repository_reservations ORDER BY worktree_key"
            ).fetchall(),
        }


def test_current_submit_documentation_examples_match_required_cli_options() -> None:
    repository_root = Path(__file__).parents[2]
    expected_examples = {
        "docs/referencia/cli.md": 2,
        "docs/guia/guia-rapida.md": 1,
        "docs/guia/flujo-completo.md": 1,
        "docs/guia/flujo-handoff.md": 1,
        "docs/operacion/prepare-start-resume-abort.md": 2,
        "docs/operacion/codex-implementer-recovery.md": 1,
    }
    for relative_path, expected_count in expected_examples.items():
        text = (repository_root / relative_path).read_text(encoding="utf-8")
        bash_blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
        submit_examples = [
            block
            for block in bash_blocks
            if "ai_dev_loop scheduler submit" in block and "\\\n" in block
        ]
        assert len(submit_examples) == expected_count, relative_path
        for example in submit_examples:
            assert "--codex-review-model" in example, relative_path
            assert "--codex-review-reasoning-effort" in example, relative_path
            short_names = re.findall(r'--short-name "([^"]+)"', example)
            assert len(short_names) == 1, relative_path
            assert require_short_name(short_names[0]) == short_names[0]


def test_real_cli_requires_and_persists_short_name_before_metadata_only_edits(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    del fake_clis
    runner = CliRunner()
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text()
    base_args = [
        "scheduler",
        "submit",
        "--repo-path",
        str(git_repo),
        "--plan-path",
        "docs/plans/sample-plan.md",
        "--prompt-source-path",
        "docs/plans/prompt_sample-plan.txt",
        "--codex-review-model",
        "gpt-6.1-sol",
        "--codex-review-reasoning-effort",
        "high",
        "--implementer-provider",
        "codex",
        "--implementer-command",
        "codex",
        "--implementer-model",
        "gpt-5.6-sol",
        "--implementer-reasoning-effort",
        "high",
        "--output",
        "json",
    ]

    missing = runner.invoke(app, base_args, input=prompt)
    assert missing.exit_code == 2
    assert not default_engine_db_path().exists()
    submit_help = runner.invoke(app, ["scheduler", "submit", "--help"])
    assert submit_help.exit_code == 0 and "--short-name" in submit_help.stdout
    assert runner.invoke(app, ["scheduler", "run", "set-short-name", "--help"]).exit_code == 0
    assert runner.invoke(app, ["scheduler", "sequence", "set-short-name", "--help"]).exit_code == 0

    submitted = runner.invoke(
        app,
        [*base_args[:2], "--short-name", "  Duplicate name  ", *base_args[2:]],
        input=prompt,
    )
    assert submitted.exit_code == 0, submitted.output
    run_id = str(json.loads(submitted.stdout)["run_id"])
    store = SqliteSchedulerStore.open_readonly(default_engine_db_path())
    with store.begin_read() as conn:
        assert store.run_short_name(conn, run_id) == "Duplicate name"
        before = store.get_run_row(conn, run_id)
        immutable = (before["state_payload_sha256"], before["idempotency_key"])
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations"
        ).fetchone()[0]
        effect_count = conn.execute("SELECT COUNT(*) FROM scheduler_effects").fetchone()[0]

    renamed = runner.invoke(
        app,
        [
            "scheduler",
            "run",
            "set-short-name",
            run_id,
            "--short-name",
            "Duplicate name",
            "--output",
            "json",
        ],
    )
    assert renamed.exit_code == 0, renamed.output
    rename_payload = json.loads(renamed.stdout)
    assert rename_payload == {
        "changed": False,
        "owner_id": run_id,
        "previous_short_name": "Duplicate name",
        "schema_version": 1,
        "short_name": "Duplicate name",
    }
    invalid = runner.invoke(
        app,
        ["scheduler", "run", "set-short-name", run_id, "--short-name", "bad\nname"],
    )
    assert invalid.exit_code == 4
    missing_target = runner.invoke(
        app,
        ["scheduler", "run", "set-short-name", "missing-run", "--short-name", "Valid"],
    )
    assert missing_target.exit_code == 1
    missing_sequence = runner.invoke(
        app,
        [
            "scheduler",
            "sequence",
            "set-short-name",
            "missing-sequence",
            "--short-name",
            "Valid",
        ],
    )
    assert missing_sequence.exit_code == 1
    with pytest.raises(SchedulerEngineError) as missing_error:
        set_sequence_short_name(
            "missing-sequence",
            "Valid",
            db_path=default_engine_db_path(),
        )
    assert missing_error.value.kind is SchedulerEngineErrorKind.NOT_FOUND

    store = SqliteSchedulerStore.open_readonly(default_engine_db_path())
    with store.begin_read() as conn:
        after = store.get_run_row(conn, run_id)
        assert (after["state_payload_sha256"], after["idempotency_key"]) == immutable
        assert store.run_short_name(conn, run_id) == "Duplicate name"
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_repository_reservations").fetchone()[0]
            == reservation_count
        )
        assert conn.execute("SELECT COUNT(*) FROM scheduler_effects").fetchone()[0] == effect_count


def test_application_submit_requires_explicit_short_name_before_resource_creation(
    isolated_xdg: Path,
) -> None:
    paths = _paths(isolated_xdg)
    with pytest.raises(ValidationError, match="--short-name must be a string"):
        submit_run(SubmitOptions(**paths))
    assert not paths["db_path"].exists()
    assert not paths["artifact_root"].exists()


def test_v3_prepare_materialize_and_replay_preserve_exact_leaf_alias(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    del fake_clis
    paths = _paths(isolated_xdg)
    manifest_path = _write_manifest(git_repo / "sequence.yaml", _codex_only_v3_manifest())
    service = _prepare_service(
        db_path=paths["db_path"], artifact_root=paths["artifact_root"], repo=git_repo
    )
    options = SequencePrepareOptions(manifest_path=manifest_path, repo_path=git_repo, **paths)
    prepared = service.prepare(options)
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        state = store.load_validated_sequence_state(conn, prepared.sequence_id)
        first = state.definition.entries[0]
        assert state.definition.schema_version == 2
        assert store.sequence_short_name(conn, prepared.sequence_id) == "Fixture sequence"
        assert (
            store.planned_run_short_name(
                conn,
                sequence_id=prepared.sequence_id,
                ordinal=1,
                planned_run_id=first.planned_run_id,
            )
            == "Phase one"
        )
        frozen_definition = state.definition

    started = _start_service(paths).start(prepared.sequence_id)
    assert started.run_id == first.planned_run_id
    with store.begin_read() as conn:
        assert store.run_short_name(conn, started.run_id) == "Phase one"
        started_definition = store.load_validated_sequence_state(
            conn, prepared.sequence_id
        ).definition
        assert started_definition == frozen_definition

    set_run_short_name(started.run_id, "Duplicate name", db_path=paths["db_path"])
    renamed_sequence = CliRunner().invoke(
        app,
        [
            "scheduler",
            "sequence",
            "set-short-name",
            prepared.sequence_id,
            "--short-name",
            "Duplicate name",
            "--output",
            "json",
        ],
    )
    assert renamed_sequence.exit_code == 0, renamed_sequence.output
    replay = _start_service(paths).start(prepared.sequence_id)
    assert replay.idempotent_replay is True
    with store.begin_read() as conn:
        assert store.run_short_name(conn, started.run_id) == "Duplicate name"
        assert store.sequence_short_name(conn, prepared.sequence_id) == "Duplicate name"
        assert store.load_validated_sequence_state(conn, prepared.sequence_id).definition == (
            frozen_definition
        )


def test_fresh_legacy_and_invalid_v3_fail_before_resource_creation(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    paths = _paths(isolated_xdg)
    legacy = yaml.safe_load(_two_phase_manifest())
    legacy["schema_version"] = 2
    legacy.pop("short_name")
    for phase in legacy["phases"]:
        phase.pop("short_name")
    legacy_path = git_repo / "legacy-sequence.yaml"
    legacy_path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    with pytest.raises(ValidationError, match="requires manifest v3"):
        prepare_sequence(
            SequencePrepareOptions(manifest_path=legacy_path, repo_path=git_repo, **paths)
        )
    assert not paths["db_path"].exists()
    assert not paths["artifact_root"].exists()

    invalid = yaml.safe_load(_two_phase_manifest())
    invalid["phases"][0]["short_name"] = None
    invalid_path = git_repo / "invalid-v3.yaml"
    invalid_path.write_text(yaml.safe_dump(invalid), encoding="utf-8")
    with pytest.raises(ValidationError, match="sequence manifest failed validation"):
        prepare_sequence(
            SequencePrepareOptions(manifest_path=invalid_path, repo_path=git_repo, **paths)
        )
    assert not paths["db_path"].exists()
    assert not paths["artifact_root"].exists()


@pytest.mark.parametrize("schema_version", [16, 17, 18, 19])
def test_fresh_legacy_rejection_does_not_change_existing_ledger(
    git_repo: Path,
    isolated_xdg: Path,
    schema_version: int,
) -> None:
    paths = _paths(isolated_xdg)
    legacy = yaml.safe_load(_two_phase_manifest())
    legacy["schema_version"] = 2
    legacy.pop("short_name")
    for phase in legacy["phases"]:
        phase.pop("short_name")
    legacy_path = git_repo / "legacy-existing-ledger.yaml"
    legacy_path.write_text(yaml.safe_dump(legacy), encoding="utf-8")

    SqliteSchedulerStore(paths["db_path"])
    if schema_version < 20:
        with sqlite3.connect(paths["db_path"]) as conn:
            conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_no_progress")
            conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 20")
            conn.execute("PRAGMA user_version = 19")
            conn.commit()
    if schema_version < 19:
        with sqlite3.connect(paths["db_path"]) as conn:
            conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_retention_policy")
            conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_conversation_fences")
            conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_disposals")
            conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 19")
            conn.execute("PRAGMA user_version = 18")
            conn.commit()
    if schema_version < 18:
        with sqlite3.connect(paths["db_path"]) as conn:
            conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
            conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 18")
            conn.execute("PRAGMA user_version = 17")
            conn.commit()
    if schema_version == 16:
        with sqlite3.connect(paths["db_path"]) as conn:
            conn.execute("DROP TABLE scheduler_planned_run_presentation")
            conn.execute("DROP TABLE scheduler_run_presentation")
            conn.execute("DROP TABLE scheduler_sequence_presentation")
            conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 17")
            conn.execute("PRAGMA user_version = 16")
            conn.commit()
    ledger_bytes_before = paths["db_path"].read_bytes()
    with sqlite3.connect(paths["db_path"]) as conn:
        authority_before = {
            "migrations": conn.execute(
                "SELECT version, name, checksum FROM scheduler_schema_migrations ORDER BY version"
            ).fetchall(),
            "sequences": conn.execute(
                "SELECT sequence_id, payload, payload_sha256 FROM scheduler_sequences "
                "ORDER BY sequence_id"
            ).fetchall(),
        }

    with pytest.raises(ValidationError, match="requires manifest v3"):
        prepare_sequence(
            SequencePrepareOptions(manifest_path=legacy_path, repo_path=git_repo, **paths)
        )

    assert paths["db_path"].read_bytes() == ledger_bytes_before
    assert not paths["artifact_root"].exists()
    with sqlite3.connect(paths["db_path"]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema_version
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'scheduler_%_presentation'"
        ).fetchone()[0] == (3 if schema_version >= 17 else 0)
        assert {
            "migrations": conn.execute(
                "SELECT version, name, checksum FROM scheduler_schema_migrations ORDER BY version"
            ).fetchall(),
            "sequences": conn.execute(
                "SELECT sequence_id, payload, payload_sha256 FROM scheduler_sequences "
                "ORDER BY sequence_id"
            ).fetchall(),
        } == authority_before


def test_accepted_v1_fixture_replays_but_retired_runtime_cannot_start(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/phase27_3_historical"
    accepted = PreparedSequenceState.model_validate_json((fixture / "prepared.json").read_text())
    repository_root = Path(accepted.definition.repository.root)
    if repository_root.parent.exists():
        pytest.skip("historical fixture repository path is unexpectedly occupied")
    paths = _paths(isolated_xdg)
    artifact_hashes = _artifact_digests(fixture / "artifacts")
    artifact_source = next((fixture / "artifacts/sequences").iterdir())
    try:
        (repository_root / "docs/plans").mkdir(parents=True)
        (repository_root / ".git").mkdir()
        (repository_root.parent / "bin").mkdir()
        (repository_root / "docs/plans/sample-plan.md").write_bytes(
            (artifact_source / "entries/01/plan/plan.md").read_bytes()
        )
        (repository_root / "docs/plans/prompt_sample-plan.txt").write_bytes(
            (artifact_source / "entries/01/prompts/cursor-initial.txt").read_bytes()
        )
        (repository_root / "ai_dev_loop.yaml").write_bytes(
            (artifact_source / "entries/01/source-config.yaml").read_bytes()
        )
        for executable in ("agent", "codex"):
            path = repository_root.parent / "bin" / executable
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        monkeypatch.setenv("PATH", f"{repository_root.parent / 'bin'}")

        shutil.copytree(fixture / "artifacts", paths["artifact_root"])
        for path in paths["artifact_root"].rglob("*"):
            path.chmod(DIR_MODE if path.is_dir() else SENSITIVE_FILE_MODE)
        store = SqliteSchedulerStore(paths["db_path"])
        with store.begin_immediate() as conn:
            store.insert_prepared_sequence(
                conn,
                state=accepted,
                now=datetime(2026, 10, 14, 17, 42, tzinfo=UTC),
            )
        set_sequence_short_name(
            accepted.sequence_id,
            "Edited historical",
            db_path=paths["db_path"],
        )
        replay = prepare_sequence(
            SequencePrepareOptions(
                manifest_path=artifact_source / "manifest/original.yaml",
                repo_path=repository_root,
                **paths,
            )
        )
        assert replay.reused_existing is True
        assert replay.sequence_id == accepted.sequence_id
        assert _artifact_digests(paths["artifact_root"]) == artifact_hashes
        with pytest.raises(ValidationError, match="Cursor CLI execution is retired"):
            _start_service(paths).start(accepted.sequence_id)
        with store.begin_read() as conn:
            assert store.sequence_short_name(conn, accepted.sequence_id) == "Edited historical"
            assert isinstance(
                store.load_validated_sequence_state(conn, accepted.sequence_id),
                PreparedSequenceState,
            )
        materialized_hashes = _artifact_digests(paths["artifact_root"])
        assert materialized_hashes == artifact_hashes
    finally:
        shutil.rmtree(repository_root.parent, ignore_errors=True)


def test_predecessor_v2_fixture_replays_but_mixed_cli_runtime_cannot_start(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/phase28_1_historical_v2"
    provenance = (fixture / "provenance.txt").read_text(encoding="utf-8")
    assert "source_commit: 67b8edc" in provenance
    assert "no model process was launched" in provenance
    accepted = PreparedSequenceState.model_validate_json((fixture / "prepared.json").read_text())
    assert accepted.definition.schema_version == 2
    repository_root = Path(accepted.definition.repository.root)
    if repository_root.parent.exists():
        pytest.skip("historical v2 fixture repository path is unexpectedly occupied")
    paths = _paths(isolated_xdg)
    artifact_hashes = _artifact_digests(fixture / "artifacts")
    artifact_source = next((fixture / "artifacts/sequences").iterdir())
    try:
        (repository_root / "docs/plans").mkdir(parents=True)
        (repository_root / ".git").mkdir()
        (repository_root.parent / "bin").mkdir()
        (repository_root / "docs/plans/sample-plan.md").write_bytes(
            (artifact_source / "entries/01/plan/plan.md").read_bytes()
        )
        (repository_root / "docs/plans/prompt_sample-plan.txt").write_bytes(
            (artifact_source / "entries/01/prompts/cursor-initial.txt").read_bytes()
        )
        (repository_root / "ai_dev_loop.yaml").write_bytes(
            (artifact_source / "entries/01/source-config.yaml").read_bytes()
        )
        for executable in ("agent", "codex"):
            path = repository_root.parent / "bin" / executable
            path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            path.chmod(0o700)
        monkeypatch.setenv("PATH", f"{repository_root.parent / 'bin'}")

        shutil.copytree(fixture / "artifacts", paths["artifact_root"])
        for path in paths["artifact_root"].rglob("*"):
            path.chmod(DIR_MODE if path.is_dir() else SENSITIVE_FILE_MODE)
        store = SqliteSchedulerStore(paths["db_path"])
        with store.begin_immediate() as conn:
            store.insert_prepared_sequence(
                conn,
                state=accepted,
                now=datetime(2026, 10, 16, 20, 30, 24, tzinfo=UTC),
            )
        set_sequence_short_name(
            accepted.sequence_id,
            "Edited accepted v2",
            db_path=paths["db_path"],
        )
        replay = prepare_sequence(
            SequencePrepareOptions(
                manifest_path=artifact_source / "manifest/original.yaml",
                repo_path=repository_root,
                **paths,
            )
        )
        assert replay.reused_existing is True
        assert replay.sequence_id == accepted.sequence_id
        assert _artifact_digests(paths["artifact_root"]) == artifact_hashes

        with pytest.raises(ValidationError, match="Unsupported sequence ordinals: 2"):
            _start_service(paths).start(accepted.sequence_id)
        with store.begin_read() as conn:
            state = store.load_validated_sequence_state(conn, accepted.sequence_id)
            assert state.definition == accepted.definition
            assert state.definition.entries[0].implementer.provider == "codex"
            assert state.definition.entries[1].implementer.provider == "cursor"
            assert store.sequence_short_name(conn, accepted.sequence_id) == "Edited accepted v2"
        materialized_hashes = _artifact_digests(paths["artifact_root"])
        assert materialized_hashes == artifact_hashes
    finally:
        shutil.rmtree(repository_root.parent, ignore_errors=True)


def test_schema16_read_fallback_and_v17_migration_preserve_authority(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    del fake_clis
    paths = _paths(isolated_xdg)
    manifest_path = _write_manifest(git_repo / "sequence.yaml", _codex_only_v3_manifest())
    prepared = _prepare_service(
        db_path=paths["db_path"], artifact_root=paths["artifact_root"], repo=git_repo
    ).prepare(SequencePrepareOptions(manifest_path=manifest_path, repo_path=git_repo, **paths))

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text()
    submit_options = replace(
        _submit_options(git_repo, **paths),
        short_name="Chosen standalone",
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = _submission_service(
            db_path=paths["db_path"], artifact_root=paths["artifact_root"], repo=git_repo
        ).submit(submit_options)

    immutable_before = _immutable_snapshot(paths["db_path"])
    artifacts_before = _artifact_digests(paths["artifact_root"])
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_no_progress")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_retention_policy")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_conversation_fences")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_disposals")
        conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
        conn.execute("DROP TABLE scheduler_planned_run_presentation")
        conn.execute("DROP TABLE scheduler_run_presentation")
        conn.execute("DROP TABLE scheduler_sequence_presentation")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_observations")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_attempt_facts")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 17")
        conn.execute("PRAGMA user_version = 16")
        conn.commit()

    size_before = paths["db_path"].stat().st_size
    readonly = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with readonly.begin_read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        assert readonly.sequence_short_name(conn, prepared.sequence_id) == "fixture-sequence"
        fallback = readonly.run_short_name(conn, submitted.run_id)
        assert fallback == submitted.run_id[:35]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'scheduler_%_presentation'"
            ).fetchone()[0]
            == 0
        )
    observations = SqliteDashboardReadRepository(db_path=paths["db_path"]).read_observations(
        DashboardReadScope(
            operation=DashboardReadOperation.RUNS,
            project_name=None,
            from_utc=datetime(2020, 1, 1, tzinfo=UTC),
            to_utc=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    observed_names = {item.run_id: item.short_name for item in observations.runs}
    assert observed_names[submitted.run_id] == submitted.run_id[:35]
    assert paths["db_path"].stat().st_size == size_before

    migrated = SqliteSchedulerStore(paths["db_path"])
    with migrated.begin_read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
        assert migrated.sequence_short_name(conn, prepared.sequence_id) == "fixture-sequence"
        assert migrated.run_short_name(conn, submitted.run_id) == submitted.run_id[:35]
        entries = conn.execute(
            "SELECT ordinal, planned_run_id FROM scheduler_sequence_entries "
            "WHERE sequence_id = ? ORDER BY ordinal",
            (prepared.sequence_id,),
        ).fetchall()
        assert [
            migrated.planned_run_short_name(
                conn,
                sequence_id=prepared.sequence_id,
                ordinal=int(entry["ordinal"]),
                planned_run_id=str(entry["planned_run_id"]),
            )
            for entry in entries
        ] == ["phase-one", "phase-two"]
    assert _immutable_snapshot(paths["db_path"]) == immutable_before
    assert _artifact_digests(paths["artifact_root"]) == artifacts_before

    set_run_short_name(submitted.run_id, "Edited after migration", db_path=paths["db_path"])
    set_sequence_short_name(prepared.sequence_id, "Edited sequence", db_path=paths["db_path"])
    with migrated.begin_immediate() as conn:
        migrated.backfill_presentation_short_names(
            conn,
            now_text="2026-10-16T20:00:00.000000Z",
        )
    with migrated.begin_read() as conn:
        assert migrated.run_short_name(conn, submitted.run_id) == "Edited after migration"
        assert migrated.sequence_short_name(conn, prepared.sequence_id) == "Edited sequence"
        run_item = IntegrationRunReadService(migrated).inspect_run(submitted.run_id)
        assert run_item.short_name == "Edited after migration"


def test_schema17_migration_fault_rolls_back_all_presentation_tables(
    isolated_xdg: Path,
) -> None:
    paths = _paths(isolated_xdg)
    SqliteSchedulerStore(paths["db_path"])
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_no_progress")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_retention_policy")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_conversation_fences")
        conn.execute("DROP TABLE IF EXISTS scheduler_cursor_sdk_disposals")
        conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
        conn.execute("DROP TABLE scheduler_planned_run_presentation")
        conn.execute("DROP TABLE scheduler_run_presentation")
        conn.execute("DROP TABLE scheduler_sequence_presentation")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_observations")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_attempt_facts")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 17")
        conn.execute("PRAGMA user_version = 16")
        conn.commit()

    def fail_mid_migration(statement: str) -> None:
        if "scheduler_sequence_presentation" in statement:
            raise RuntimeError("injected presentation migration fault")

    with pytest.raises(RuntimeError, match="injected presentation migration fault"):
        SqliteSchedulerStore(
            paths["db_path"],
            migration_fault_hook=fail_mid_migration,
        )

    with sqlite3.connect(paths["db_path"]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_schema_migrations WHERE version = 17"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'scheduler_%_presentation'"
            ).fetchone()[0]
            == 0
        )
