"""Phase 25.5 final Cursor runtime cutover contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import DIGEST, sample_submitted_state
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_recovery_check import (
    scheduler_cursor_recovery_check,
)
from ai_dev_loop.scheduler.application.cursor_runtime_policy import (
    context_uses_cursor_cli,
    require_supported_context,
    unsupported_sequence_cursor_cli_ordinals,
)
from ai_dev_loop.scheduler.application.cursor_timeout_retry import CursorTimeoutRetryService
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_materializer import SequenceRunMaterializer
from ai_dev_loop.scheduler.application.sequence_start import SequenceStartService
from ai_dev_loop.scheduler.application.start import StartService
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.sequence import (
    FrozenSequenceEntry,
    PreparedSequenceDefinition,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import CursorSdkBinding, ImplementerBinding
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _codex_implementer() -> ImplementerBinding:
    return ImplementerBinding(
        provider="codex",
        command="codex",
        model="gpt-6.1-sol",
        reasoning_effort="high",
        timeout_minutes=90,
        sandbox="workspace-write",
        approval_policy="never",
    )


def _sdk_cursor() -> CursorSdkBinding:
    return CursorSdkBinding(
        sdk_version="1.0.37",
        model="grok-4.7",
        parameters={"context": "256k", "fast": False, "reasoning_effort": "high"},
        credential_ref="cursor-default",
        setting_sources=("project",),
        sandbox="disabled",
    )


def _sdk_implementer() -> ImplementerBinding:
    return ImplementerBinding(
        schema_version=2,
        provider="cursor",
        runtime="sdk-local",
        model="grok-4.7",
        timeout_minutes=90,
        sandbox="disabled",
    )


def _insert_state(store: SqliteSchedulerStore, state: object) -> None:
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    event = RunSubmittedEvent(
        run_id=state.run_id,  # type: ignore[attr-defined]
        idempotency_key=state.idempotency_key,  # type: ignore[attr-defined]
        worktree_key=state.context.repository.worktree_key,  # type: ignore[attr-defined]
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,  # type: ignore[attr-defined]
            state=state,  # type: ignore[arg-type]
            event_id="evt-phase25-5-submit",
            event=event,
            now=now,
        )


def test_cli_help_removes_cursor_cli_only_admission_flags() -> None:
    for command in (
        ("scheduler", "submit", "--help"),
        ("scheduler", "sequence", "prepare", "--help"),
    ):
        result = CliRunner().invoke(app, list(command))
        assert result.exit_code == 0, result.output
        assert "--cursor-command" not in result.output
        assert "--cursor-model" not in result.output
        assert "--cursor-output-format" not in result.output
        if command[1] == "submit":
            assert "--implementer-provider" in result.output


def test_effective_provider_runtime_keeps_codex_v1_and_sdk_cursor_supported() -> None:
    legacy = sample_submitted_state().context
    assert context_uses_cursor_cli(legacy) is True
    with pytest.raises(ValidationError, match="Cursor CLI execution is retired"):
        require_supported_context(legacy, action="test")

    codex = legacy.model_copy(update={"schema_version": 5, "implementer": _codex_implementer()})
    assert context_uses_cursor_cli(codex) is False
    require_supported_context(codex, action="test")

    sdk = legacy.model_copy(
        update={
            "schema_version": 7,
            "cursor": _sdk_cursor(),
            "implementer": _sdk_implementer(),
        }
    )
    assert context_uses_cursor_cli(sdk) is False
    require_supported_context(sdk, action="test")


def test_historical_cursor_start_rejects_without_mutating_state_and_check_is_read_only(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    artifacts = tmp_path / "artifacts-that-must-not-be-created"
    store = SqliteSchedulerStore(db)
    queued = sample_submitted_state(run_id="historical-cursor-cli")
    _insert_state(store, queued)

    with pytest.raises(ValidationError, match="Cursor CLI execution is retired"):
        StartService(store).start(queued.run_id)

    with store.begin_read() as conn:
        current, version, _ = store.load_validated_snapshot(conn, queued.run_id)
        reservation = store.get_reservation_for_run(conn, queued.run_id)
        effect_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=?", (queued.run_id,)
        ).fetchone()[0]
    assert current.kind == "queued"
    assert version == queued.version
    assert reservation is not None
    assert effect_count == 0

    receipt = scheduler_cursor_recovery_check(
        queued.run_id,
        db_path=db,
        artifact_root=artifacts,
    )
    assert receipt.reason_code == "ineligible_cursor_cli_runtime_retired"
    assert receipt.recovery_supported is False
    assert not artifacts.exists()


def test_codex_v1_context_still_authorizes_start_without_cursor_runtime(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    queued = sample_submitted_state(run_id="codex-v1-config")
    context = queued.context.model_copy(
        update={"schema_version": 5, "implementer": _codex_implementer()}
    )
    queued = queued.model_copy(update={"context": context})
    _insert_state(store, queued)

    result = StartService(store).start(queued.run_id)
    assert result.state_kind == "authorized"
    assert result.changed is True


def _definition(
    *,
    schema_version: int,
    cursor: object,
    implementers: tuple[ImplementerBinding | None, ImplementerBinding | None],
) -> PreparedSequenceDefinition:
    context = sample_submitted_state().context
    entries = tuple(
        FrozenSequenceEntry(
            schema_version=schema_version,
            ordinal=ordinal,
            phase_name=f"phase-{ordinal}",
            planned_run_id=f"planned-{ordinal}",
            commit_message="accepted checkpoint" if ordinal == 1 else None,
            plan_prompt=context.plan_prompt,
            effective_config=context.effective_config,
            codex=context.codex,  # type: ignore[arg-type]
            cursor=cursor,  # type: ignore[arg-type]
            workflow=context.workflow,
            implementer=implementers[ordinal - 1],
        )
        for ordinal in (1, 2)
    )
    return PreparedSequenceDefinition(
        schema_version=schema_version,
        sequence_id=f"sequence-v{schema_version}",
        name=f"sequence-v{schema_version}",
        project_name=context.project_name,
        repository=context.repository,
        manifest_original_artifact_path="sequence/manifest.original.yaml",
        manifest_original_sha256=DIGEST,
        manifest_resolved_artifact_path="sequence/manifest.resolved.yaml",
        manifest_resolved_sha256=DIGEST,
        controller=context.controller,
        entries=entries,
    )


def test_whole_sequence_classification_rejects_only_effective_cli_cursor_leaves() -> None:
    context = sample_submitted_state().context
    legacy = _definition(
        schema_version=1,
        cursor=context.cursor,
        implementers=(None, None),
    )
    assert unsupported_sequence_cursor_cli_ordinals(legacy) == (1, 2)

    codex_only = _definition(
        schema_version=2,
        cursor=context.cursor,
        implementers=(_codex_implementer(), _codex_implementer()),
    )
    assert unsupported_sequence_cursor_cli_ordinals(codex_only) == ()

    mixed_sdk = _definition(
        schema_version=3,
        cursor=_sdk_cursor(),
        implementers=(_sdk_implementer(), _codex_implementer()),
    )
    assert unsupported_sequence_cursor_cli_ordinals(mixed_sdk) == ()


@pytest.mark.parametrize("action", ["cursor-retry", "implementer-retry", "review-retry", "extend"])
def test_retired_mutation_callers_preserve_ledger_and_artifacts(
    tmp_path: Path, action: str
) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="retired-cli")
    _insert_state(store, state)
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    with store.begin_read() as conn:
        before = conn.execute("SELECT * FROM scheduler_runs").fetchall()
        before_events = len(
            store.list_events_for_run(conn, state.run_id, limit=500, newest_first=False)
        )
    with pytest.raises(
        (ValidationError, SchedulerEngineError), match="Cursor CLI execution is retired"
    ):
        if action == "cursor-retry":
            CursorTimeoutRetryService(store, artifacts).retry(state.run_id)
        elif action == "implementer-retry":
            ImplementerRetryService(store, artifacts).retry(state.run_id, force=True)
        elif action == "review-retry":
            ReviewRetryService(store, artifacts).retry(state.run_id)
        else:
            ReviewBudgetExtendService(store, artifacts).extend(state.run_id, target_total=5)
    with store.begin_read() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM scheduler_runs")] == [
            tuple(row) for row in before
        ]
        assert (
            len(store.list_events_for_run(conn, state.run_id, limit=500, newest_first=False))
            == before_events
        )
    assert not artifacts.artifact_root.exists()


def test_mixed_historical_sequence_rejects_before_first_codex_materialization(
    tmp_path: Path,
) -> None:
    definition = _definition(
        schema_version=2,
        cursor=sample_submitted_state().context.cursor,
        implementers=(
            _codex_implementer(),
            ImplementerBinding(
                provider="cursor",
                command="agent",
                model="legacy",
                timeout_minutes=90,
                sandbox="disabled",
            ),
        ),
    )
    state = PreparedSequenceState(
        sequence_id=definition.sequence_id,
        version=1,
        prepared_at="2026-10-18T12:00:00Z",
        updated_at="2026-10-18T12:00:00Z",
        idempotency_key=DIGEST,
        definition=definition,
    )
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    with store.begin_immediate() as conn:
        store.insert_prepared_sequence(
            conn, state=state, now=datetime(2026, 10, 18, 12, tzinfo=UTC)
        )
    with pytest.raises(ValidationError, match="Unsupported sequence ordinals: 2"):
        SequenceStartService(store, artifacts).start(state.sequence_id)
    with pytest.raises(ValidationError, match="Unsupported sequence ordinals: 2"):
        SequenceRunMaterializer(artifacts).materialize_first_entry(
            sequence_id=state.sequence_id, definition=definition, entry=definition.entries[0]
        )
    with store.begin_read() as conn:
        assert store.load_validated_sequence_state(conn, state.sequence_id) == state
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0
    assert not artifacts.artifact_root.exists()
