"""Production-service integration coverage for Phase 25.3 SDK recovery."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase25_2_sdk_workflow import (
    OwnedProcessGroupBackend,
    _configure_v2,
    _credential,
    _install_fake_sdk,
    _wait_for_path,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_agent_led_submitted_context
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.attempt_backend import ObserveResult, UnitLifecycleState
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    analyze_cursor_recovery_evidence,
)
from ai_dev_loop.scheduler.application.cursor_sdk_journal import append_sdk_journal
from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
    CursorSdkReconciliationService,
)
from ai_dev_loop.scheduler.application.cursor_sdk_retention import (
    apply_cursor_sdk_cleanup,
    maintain_cursor_sdk_conversations,
    preview_cursor_sdk_cleanup,
)
from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
    authenticate_sdk_successor_authority,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.application.presentation_metadata import set_run_short_name
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.token_usage_query import TokenUsageReadService
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_reconciliation_record_rel,
)
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECOVERY_NOTE
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    SDK_SUCCESSOR_BASE_PROMPT_REL,
    SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
    CursorSdkReconciliationRecordV1,
)
from ai_dev_loop.scheduler.domain.events import CursorSdkAgentCreatedEvent
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    CursorReadyState,
    CursorSdkBinding,
    CursorSdkRuntimeBinding,
    CursorWorkflowCheckpoint,
    ImplementerBinding,
    SubmittedRunContext,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_bytes

NOW = datetime(2026, 10, 17, 16, tzinfo=UTC)
AGENT = "019abc00-1111-4222-8333-444444444444"
NATIVE = "run-019abc00-1111-4222-8333-555555555555"
_ATTEMPTS = itertools.count(10_000)


def _tick_until_kind(
    tick: TickService,
    store: SqliteSchedulerStore,
    run_id: str,
    *kinds: str,
    limit: int = 100,
) -> object:
    state: object | None = None
    for _ in range(limit):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind in kinds:  # type: ignore[attr-defined]
            return state
    raise AssertionError(
        f"run {run_id} did not reach {kinds}; last kind={getattr(state, 'kind', None)}"
    )


def _bind_fake_sdk_run(
    monkeypatch: pytest.MonkeyPatch,
    artifacts_root: Path,
    run_id: str,
) -> Path:
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_IDENTITY_PATH",
        str(run_root / "cursor/sdk-identity.json"),
    )
    return run_root


def _submit_sdk_run(
    git_repo: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    short_name: str,
    now_factory: Callable[[], datetime] | None = None,
    backend: object | None = None,
) -> tuple[str, Path, Path, SqliteSchedulerStore, TickService]:
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sdk recovery baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / f"fake-sdk-{short_name.replace(' ', '-').lower()}"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name=short_name,
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    _bind_fake_sdk_run(monkeypatch, artifacts_root, run_id)
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=now_factory or (lambda: datetime(2027, 1, 1, tzinfo=UTC)),
        tick_owner_factory=lambda: f"phase253-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend
        or FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    return run_id, db, artifacts_root, store, tick


def _insert_state(
    store: SqliteSchedulerStore,
    state: CursorReadyState,
    *,
    short_name: str,
) -> None:
    kind, payload, digest = store.dump_state(state)
    now_text = NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                kind,
                payload,
                digest,
                state.version,
                state.idempotency_key,
                state.context.repository.worktree_key,
                now_text,
                now_text,
            ),
        )
        store.insert_run_short_name(conn, run_id=state.run_id, short_name=short_name, now=NOW)
        assert store.reacquire_released_reservation_for_run(
            conn,
            run_id=state.run_id,
            worktree_key=state.context.repository.worktree_key,
            repository_root=state.context.repository.root,
            now=NOW,
        )


def _insert_terminal_cursor_attempt(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    attempt_id: str,
    dispatch_id: str,
    effect_kind: str,
    status: str,
) -> sqlite3.Row:
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        effect = store.get_effect_by_dispatch_id(conn, dispatch_id)
        if effect is None:
            assert isinstance(state, CursorReadyState) and state.cursor.sdk is not None
            sdk = state.cursor.sdk
            event = CursorSdkAgentCreatedEvent(
                run_id=run_id,
                agent_id=AGENT,
                conversation_key=sdk.conversation_key,
                identity_artifact_path=sdk.identity_artifact_path,
                identity_artifact_sha256=sdk.identity_artifact_sha256,
                journal_path=sdk.latest_journal_path,
                journal_sha256=sdk.latest_journal_sha256,
            )
            event_id = f"evt-ledger-{attempt_id}"
            store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=store.next_event_sequence(conn, run_id),
                event=event,
                now=NOW,
            )
            store.insert_effect(
                conn,
                dispatch_id=dispatch_id,
                source_event_id=event_id,
                run_id=run_id,
                effect_id=effect_kind,
                effect_kind=effect_kind,
                effect_payload={"iteration": 1},
                available_at=NOW,
                claimed_run_version=version,
                now=NOW,
            )
        assert store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=NOW)
        now_text = NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at, ingested
            ) VALUES (?, ?, ?, 'cursor', 1, ?, ?, ?, 0)
            """,
            (attempt_id, run_id, dispatch_id, status, now_text, now_text),
        )
        row = store.get_attempt_by_id(conn, attempt_id)
        assert row is not None
        return row


def _ready_fixture(
    tmp_path: Path,
    *,
    scheduler_state_root: Path | None = None,
) -> tuple[SqliteSchedulerStore, ProtectedArtifactStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = scheduler_state_root or (tmp_path / "state")
    db = state_root / "engine.sqlite3"
    artifacts = ProtectedArtifactStore(state_root / "artifacts")
    store = SqliteSchedulerStore(db)
    run_id = "fixture-sdk-recovery"
    prompt = b"implement the approved phase"
    admission = b"clean baseline\n"
    identity = json.dumps(
        {
            "schema_version": 1,
            "runtime": "sdk-local",
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "agent_id": AGENT,
        },
        sort_keys=True,
    ).encode()
    prompt_artifact = artifacts.write_bytes(
        run_id, "prompts/cursor-initial.txt", prompt, max_bytes=10_000
    )
    admitted_artifact = artifacts.write_bytes(
        run_id, "preflight/admission-status.txt", admission, max_bytes=10_000
    )
    identity_artifact = artifacts.write_bytes(
        run_id, "cursor/sdk-identity.json", identity, max_bytes=10_000
    )
    source_attempt = "att-" + "1" * 32
    source_dispatch = "fx-source"
    journal_rel = cursor_sdk_attempt_journal_rel(source_attempt)
    journal = artifacts.run_root(run_id) / journal_rel
    journal_base = {
        "attempt_id": source_attempt,
        "dispatch_id": source_dispatch,
        "conversation_owner_run_id": run_id,
        "conversation_key": cursor_conversation_key(run_id),
        "agent_id": AGENT,
        "prompt_sha256": prompt_artifact.sha256,
    }
    append_sdk_journal(
        journal, kind="run_inventory_baseline", payload={**journal_base, "run_ids": []}
    )
    append_sdk_journal(journal, kind="send_intent", payload=journal_base)
    append_sdk_journal(
        journal,
        kind="native_run_bound",
        payload={**journal_base, "native_run_id": NATIVE},
    )
    append_sdk_journal(
        journal,
        kind="terminal",
        payload={
            **journal_base,
            "native_run_id": NATIVE,
            "outcome": "uncertain",
            "failure_kind": "cursor_sdk_native_run_uncertain",
        },
    )
    base = sample_agent_led_submitted_context(repo_root=str(repo)).model_dump(mode="json")
    base.update(
        {
            "schema_version": 7,
            "cursor": CursorSdkBinding(
                sdk_version="1.0.37",
                model="grok-4.7",
                parameters={"context": "256k", "reasoning_effort": "high"},
                credential_ref="cursor-test",
                setting_sources=("project",),
                sandbox="enabled",
            ).model_dump(mode="json"),
            "implementer": ImplementerBinding(
                schema_version=2,
                provider="cursor",
                runtime="sdk-local",
                model="grok-4.7",
                timeout_minutes=30,
                sandbox="enabled",
            ).model_dump(mode="json"),
        }
    )
    plan_artifact = artifacts.write_bytes(
        run_id, "plan/plan.md", b"approved plan\n", max_bytes=10_000
    )
    effective_config = artifacts.write_bytes(
        run_id,
        "effective-config.yaml",
        b"project: fixture\n",
        max_bytes=10_000,
    )
    source_config = artifacts.write_bytes(
        run_id,
        "source-config.yaml",
        b"project: fixture\n",
        max_bytes=10_000,
    )
    reviewer_input = artifacts.write_bytes(
        run_id,
        "codex/fresh-reviewer-input.json",
        b'{"reviewer":"fresh"}\n',
        max_bytes=10_000,
    )
    base["plan_prompt"]["plan_sha256"] = plan_artifact.sha256  # type: ignore[index]
    base["plan_prompt"]["prompt_sha256"] = prompt_artifact.sha256  # type: ignore[index]
    base["effective_config"]["effective_config_sha256"] = (  # type: ignore[index]
        effective_config.sha256
    )
    base["effective_config"]["source_config_sha256"] = source_config.sha256  # type: ignore[index]
    base["codex"]["binding_sha256"] = reviewer_input.sha256  # type: ignore[index]
    context = SubmittedRunContext.model_validate(base)
    state = CursorReadyState(
        run_id=run_id,
        version=1,
        submitted_at=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        updated_at=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        idempotency_key="b" * 64,
        context=context,
        checkpoint=AdmittedRunCheckpoint(
            authorized_at=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            admitted_at=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            admission_status_artifact_path=admitted_artifact.relative_path,
            admission_status_sha256=admitted_artifact.sha256,
        ),
        cursor=CursorWorkflowCheckpoint(
            iteration=1,
            original_prompt_path=prompt_artifact.relative_path,
            original_prompt_sha256=prompt_artifact.sha256,
            sdk=CursorSdkRuntimeBinding(
                conversation_owner_run_id=run_id,
                conversation_key=cursor_conversation_key(run_id),
                agent_id=AGENT,
                identity_artifact_path=identity_artifact.relative_path,
                identity_artifact_sha256=identity_artifact.sha256,
                latest_journal_path=journal_rel,
                latest_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
            ),
        ),
    )
    _insert_state(store, state, short_name="SDK recovery phase")
    _insert_terminal_cursor_attempt(
        store,
        run_id=run_id,
        attempt_id=source_attempt,
        dispatch_id=source_dispatch,
        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        status="failed",
    )
    return store, artifacts, run_id


def test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-01/F-02/F-03/F-07: exercise the production owner guard and full loop."""
    del fake_clis
    reviewer_id = "019abc00-1111-4222-8333-666666666666"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", reviewer_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    send_counter = tmp_path / "recovery-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    clock = [datetime(2027, 1, 1, tzinfo=UTC)]
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK repeated recovery",
        now_factory=lambda: clock[0],
    )

    source = _tick_until_kind(tick, store, run_id, "blocked")
    assert source.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    runner = CliRunner()
    checked = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)["reason_code"] == ("authenticated_cursor_sdk_reconciliation")
    forced = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    first_successor = str(json.loads(forced.stdout)["successor_run_id"])

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, first_successor)
    waiting_fix = _tick_until_kind(tick, store, first_successor, "waiting_for_cursor_fix")
    assert waiting_fix.cursor.iteration == 2  # type: ignore[attr-defined]
    assert waiting_fix.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]
    correction_path = waiting_fix.codex.latest_correction_envelope_path  # type: ignore[attr-defined]
    correction_sha = waiting_fix.codex.latest_correction_envelope_sha256  # type: ignore[attr-defined]
    assert correction_path and correction_sha

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    failed_correction = _tick_until_kind(tick, store, first_successor, "blocked")
    assert failed_correction.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    forced_again = runner.invoke(
        app,
        [
            "scheduler",
            "implementer-retry",
            first_successor,
            "--force",
            "--output",
            "json",
        ],
    )
    assert forced_again.exit_code == 0, forced_again.output
    second_successor = str(json.loads(forced_again.stdout)["run_id"])
    assert second_successor not in {run_id, first_successor}

    monkeypatch.setenv("FAKE_CURSOR_SDK_RATE_LIMIT", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    second_root = _bind_fake_sdk_run(monkeypatch, artifacts_root, second_successor)
    recovered_capacity = _tick_until_kind(
        tick,
        store,
        second_successor,
        "waiting_usage_limit",
    )
    with store.begin_read() as conn:
        recovered_authority = authenticate_sdk_successor_authority(
            store,
            ProtectedArtifactStore(artifacts_root),
            conn,
            second_successor,
        )
    assert recovered_authority is not None
    assert recovered_authority.turn_kind == "correction"
    assert recovered_capacity.cursor.iteration == 2  # type: ignore[attr-defined]
    assert recovered_capacity.cursor.original_prompt_path == (  # type: ignore[attr-defined]
        recovered_authority.effective_prompt_path
    )
    assert recovered_capacity.cursor.original_prompt_sha256 == (  # type: ignore[attr-defined]
        recovered_authority.effective_prompt_sha256
    )
    assert recovered_capacity.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]

    monkeypatch.delenv("FAKE_CURSOR_SDK_RATE_LIMIT")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    clock[0] = datetime(2027, 1, 1, 1, tzinfo=UTC)
    completed = _tick_until_kind(tick, store, second_successor, "completed")
    assert completed.cursor.iteration == 2  # type: ignore[attr-defined]
    assert completed.cursor.sdk.conversation_owner_run_id == run_id  # type: ignore[attr-defined]
    assert completed.cursor.sdk.agent_id == AGENT  # type: ignore[attr-defined]
    assert completed.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]
    assert send_counter.read_text(encoding="utf-8") == "5"

    artifacts = ProtectedArtifactStore(artifacts_root)
    inherited_base = artifacts.read_verified_bytes(
        second_successor,
        SDK_SUCCESSOR_BASE_PROMPT_REL,
        expected_sha256=correction_sha,
    )
    assert inherited_base == artifacts.read_verified_bytes(
        first_successor,
        correction_path,
        expected_sha256=correction_sha,
    )
    with store.begin_read() as conn:
        second_authority = authenticate_sdk_successor_authority(
            store, artifacts, conn, second_successor
        )
    assert second_authority is not None
    effective = artifacts.read_verified_bytes(
        second_successor,
        SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
        expected_sha256=second_authority.effective_prompt_sha256,
    )
    assert effective.count(RECOVERY_NOTE.encode()) == 1
    assert inherited_base in effective
    assert (second_root / "cursor/sdk-identity.json").is_file()

    with store.begin_read() as conn:
        immutable_source, _, _ = store.load_validated_snapshot(conn, run_id)
        immutable_first, _, _ = store.load_validated_snapshot(conn, first_successor)
        names = [
            store.run_short_name(conn, candidate)
            for candidate in (run_id, first_successor, second_successor)
        ]
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE e.effect_kind=? AND a.run_id IN (?, ?)
            """,
            (RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND, run_id, first_successor),
        ).fetchone()[0]
        recovered_turn_attempts = conn.execute(
            """
            SELECT a.attempt_id
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind=? AND a.iteration=2
            ORDER BY a.created_at, a.attempt_id
            """,
            (second_successor, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchall()
    assert immutable_source.kind == immutable_first.kind == "blocked"
    assert names == ["SDK repeated recovery"] * 3
    assert reconciliation_attempts == 2
    assert len(recovered_turn_attempts) == 2
    for attempt in recovered_turn_attempts:
        evidence = json.loads(
            (
                artifacts.run_root(second_successor)
                / f"attempts/{attempt['attempt_id']}/invocation-evidence.json"
            ).read_text(encoding="utf-8")
        )
        assert evidence["iteration"] == 2
        assert evidence["prompt_path"] == recovered_authority.effective_prompt_path
        assert evidence["prompt_sha256"] == recovered_authority.effective_prompt_sha256
        assert evidence["sdk_successor_recovery_key"] == recovered_authority.recovery_key


def test_phase25_7_completed_recovery_lineage_follows_latest_completion(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-06: superseded blocked ancestors do not keep a finished SDK lineage."""

    test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift(
        git_repo,
        isolated_xdg,
        fake_clis,
        monkeypatch,
        tmp_path,
    )
    store = SqliteSchedulerStore(isolated_xdg / "state/ai_dev_loop/engine.sqlite3")
    state_root = store.db_path.parent
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert len(preview.conversations) == 1
    item = preview.conversations[0]
    assert item.manual_eligible is True
    assert item.automatic_eligible is True
    assert item.manual_reasons == ()
    assert item.definitively_finished_at is not None
    finished = datetime.fromisoformat(item.definitively_finished_at.replace("Z", "+00:00"))
    root = state_root / "cursor-sdk" / item.conversation_key
    assert root.is_dir()
    held = maintain_cursor_sdk_conversations(
        store,
        state_root=state_root,
        now=finished + timedelta(hours=168) - timedelta(microseconds=1),
    )
    assert held.deleted_keys == ()
    assert root.is_dir()
    deleted = maintain_cursor_sdk_conversations(
        store,
        state_root=state_root,
        now=finished + timedelta(hours=168),
    )
    assert deleted.deleted_keys == (item.conversation_key,)
    assert not root.exists()
    with store.begin_read() as conn:
        receipt = conn.execute(
            """
            SELECT receipt_id, receipt_payload
              FROM scheduler_cursor_sdk_disposals
             WHERE conversation_key = ?
            """,
            (item.conversation_key,),
        ).fetchone()
        kinds = [
            str(row["state_kind"])
            for row in conn.execute("SELECT state_kind FROM scheduler_runs ORDER BY run_id")
        ]
    assert sorted(kinds) == ["blocked", "blocked", "completed"]
    tombstone = state_root / "cursor-sdk-disposals" / f"{item.conversation_key}.json"
    assert receipt is not None
    assert tombstone.read_text(encoding="utf-8") == receipt["receipt_payload"]
    repeated = maintain_cursor_sdk_conversations(
        store,
        state_root=state_root,
        now=finished + timedelta(hours=168),
    )
    assert repeated.deleted_keys == ()
    with store.begin_read() as conn:
        again = conn.execute(
            "SELECT receipt_id FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
            (item.conversation_key,),
        ).fetchone()
    assert again["receipt_id"] == receipt["receipt_id"]


def test_correction_capacity_timer_preserves_exact_turn_then_uses_decisive_failure(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-05/F-08: a capacity reconciliation may precede the decisive failure."""
    del fake_clis
    reviewer_id = "019abc00-1111-4222-8333-777777777777"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", reviewer_id)
    clock = [datetime(2027, 1, 2, tzinfo=UTC)]
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK correction capacity",
        now_factory=lambda: clock[0],
    )

    waiting_fix = _tick_until_kind(tick, store, run_id, "waiting_for_cursor_fix")
    correction_path = waiting_fix.codex.latest_correction_envelope_path  # type: ignore[attr-defined]
    correction_sha = waiting_fix.codex.latest_correction_envelope_sha256  # type: ignore[attr-defined]
    assert correction_path and correction_sha
    monkeypatch.setenv("FAKE_CURSOR_SDK_RATE_LIMIT", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    waiting_capacity = _tick_until_kind(tick, store, run_id, "waiting_usage_limit")
    assert waiting_capacity.cursor.iteration == 2  # type: ignore[attr-defined]
    assert waiting_capacity.cursor.original_prompt_path == correction_path  # type: ignore[attr-defined]
    assert waiting_capacity.cursor.original_prompt_sha256 == correction_sha  # type: ignore[attr-defined]
    assert waiting_capacity.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]

    monkeypatch.delenv("FAKE_CURSOR_SDK_RATE_LIMIT")
    clock[0] = datetime(2027, 1, 2, 1, tzinfo=UTC)
    failed = _tick_until_kind(tick, store, run_id, "blocked")
    assert failed.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    runner = CliRunner()
    checked = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert checked.exit_code == 0, checked.output
    checked_payload = json.loads(checked.stdout)
    assert checked_payload["reason_code"] == "authenticated_cursor_sdk_reconciliation"
    forced = runner.invoke(
        app,
        ["scheduler", "implementer-retry", run_id, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    successor = str(json.loads(forced.stdout)["run_id"])

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor)
    completed = _tick_until_kind(tick, store, successor, "completed")
    assert completed.cursor.iteration == 2  # type: ignore[attr-defined]
    assert completed.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        reconciliation_rows = conn.execute(
            """
            SELECT * FROM scheduler_cursor_sdk_reconciliations
            WHERE run_id=? ORDER BY created_at, recovery_key
            """,
            (run_id,),
        ).fetchall()
        turn_attempts = conn.execute(
            """
            SELECT a.attempt_id
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind=? AND a.iteration=2
            ORDER BY a.created_at, a.attempt_id
            """,
            (run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchall()
    assert [
        json.loads(str(row["intent_payload"]))["failure_class"] for row in reconciliation_rows
    ] == [
        "capacity",
        "failure",
    ]
    assert len(turn_attempts) == 2
    for attempt in turn_attempts:
        evidence = json.loads(
            (
                ProtectedArtifactStore(artifacts_root).run_root(run_id)
                / f"attempts/{attempt['attempt_id']}/invocation-evidence.json"
            ).read_text(encoding="utf-8")
        )
        assert evidence["prompt_path"] == correction_path
        assert evidence["prompt_sha256"] == correction_sha


def test_abort_reconciliation_completes_under_fence_without_redispatch(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-04: only the authenticated reconciliation effect may cross the abort fence."""
    del fake_clis
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    send_counter = tmp_path / "abort-reconciliation-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK abort reconciliation",
        backend=backend,
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_sdk_reconciliation_failed"  # type: ignore[attr-defined]
    assert send_counter.read_text(encoding="utf-8") == "1"
    monkeypatch.delenv("FAKE_CURSOR_SDK_STREAM_FAILURE")

    aborted = SchedulerAbortService(
        store,
        backend,
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: datetime(2027, 1, 1, 1, tzinfo=UTC),
    ).abort_run(run_id)
    assert aborted.abort_persisted is True and aborted.termination_pending is True
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            reservation = store.get_reservation_for_run(conn, run_id)
        if reservation is None:
            break
    assert state.kind == "aborted" and reservation is None
    assert send_counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        relation = conn.execute(
            """
            SELECT * FROM scheduler_cursor_sdk_reconciliations
            WHERE run_id=? ORDER BY updated_at DESC, recovery_key DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        downstream = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id=? AND component IN ('staging', 'codex')
            """,
            (run_id,),
        ).fetchone()[0]
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind=?
            """,
            (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND),
        ).fetchone()[0]
    assert relation is not None and relation["status"] == "ready"
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        ProtectedArtifactStore(artifacts_root).read_verified_bytes(
            run_id,
            str(relation["result_artifact_path"]),
            expected_sha256=str(relation["result_sha256"]),
        )
    )
    assert record.outcome == "abort_reconciled"
    assert reconciliation_attempts == 2 and downstream == 0
    for _ in range(3):
        tick.run_once()
    assert send_counter.read_text(encoding="utf-8") == "1"


def test_abort_active_sdk_send_persists_native_obligation_until_idempotent_release(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-10: abort cannot release an active SDK send before native reconciliation."""
    del fake_clis

    class SwitchableObservationBackend(OwnedProcessGroupBackend):
        def __init__(self) -> None:
            super().__init__()
            self.unavailable: set[str] = set()

        def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
            if attempt_id in self.unavailable:
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                    owned=False,
                )
            return super().observe(unit_identity=unit_identity, attempt_id=attempt_id)

    backend = SwitchableObservationBackend()
    markers = tmp_path / "active-abort-tree"
    send_counter = tmp_path / "active-abort-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK active abort obligation",
        backend=backend,
    )
    for _ in range(180):
        tick.run_once()
        if markers.joinpath("tree.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "tool.pid")
    with store.begin_read() as conn:
        active = store.get_nonterminal_attempt_for_run(conn, run_id)
        before_rows = conn.execute(
            "SELECT COUNT(*) FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        assert store.get_reservation_for_run(conn, run_id) is not None
    assert active is not None
    active_attempt = str(active["attempt_id"])
    assert before_rows == 0 and send_counter.read_text(encoding="utf-8") == "1"

    abort_service = SchedulerAbortService(
        store,
        backend,
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: datetime(2027, 1, 1, 1, tzinfo=UTC),
    )
    backend.unavailable.add(active_attempt)
    unavailable = abort_service.abort_run(run_id)
    assert unavailable.abort_persisted is True
    assert unavailable.termination_pending is True
    with store.begin_read() as conn:
        unavailable_rows = conn.execute(
            "SELECT COUNT(*) FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        unavailable_reservation = store.get_reservation_for_run(conn, run_id)
    assert unavailable_rows == 0 and unavailable_reservation is not None

    backend.unavailable.remove(active_attempt)
    restart_receipt = tick.run_once()
    assert any(
        receipt.action == "attempt_result_stale"
        for receipt in restart_receipt.run_receipts
        if receipt.run_id == run_id
    )
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    with store.begin_read() as conn:
        relation = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        reservation = store.get_reservation_for_run(conn, run_id)
    assert relation is not None and relation["status"] == "pending"
    assert relation["abort_requested_at"] is not None
    assert reservation is not None

    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_TREE")
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            relation = conn.execute(
                "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            reservation = store.get_reservation_for_run(conn, run_id)
        if relation is not None and relation["status"] == "ready" and reservation is None:
            break
        time.sleep(0.01)
    assert relation is not None and relation["status"] == "ready"
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        ProtectedArtifactStore(artifacts_root).read_verified_bytes(
            run_id,
            str(relation["result_artifact_path"]),
            expected_sha256=str(relation["result_sha256"]),
        )
    )
    assert record.outcome == "abort_reconciled"
    assert reservation is None and send_counter.read_text(encoding="utf-8") == "1"
    replay = abort_service.abort_run(run_id)
    assert replay.idempotent_replay is True and replay.termination_pending is False
    with store.begin_read() as conn:
        downstream = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id=? AND component IN ('staging', 'codex')
            """,
            (run_id,),
        ).fetchone()[0]
        relation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    assert downstream == 0 and relation_count == 1


def test_abort_later_successor_correction_uses_current_turn_authority(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-10: immutable successor ancestry cannot replace a later correction prompt."""
    del fake_clis
    backend = OwnedProcessGroupBackend()
    markers = tmp_path / "advanced-successor-abort-tree"
    send_counter = tmp_path / "advanced-successor-abort-send-count"
    reviewer_id = "019abc00-1111-4222-8333-121212121212"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", reviewer_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    source_run, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK advanced successor abort",
        backend=backend,
    )
    source = _tick_until_kind(tick, store, source_run, "blocked")
    assert source.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    runner = CliRunner()
    forced = runner.invoke(
        app,
        ["scheduler", "cursor-retry", source_run, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    successor_run = str(json.loads(forced.stdout)["successor_run_id"])

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    successor_root = _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_run)
    waiting_fix = _tick_until_kind(tick, store, successor_run, "waiting_for_cursor_fix")
    correction_path = waiting_fix.codex.latest_correction_envelope_path  # type: ignore[attr-defined]
    correction_sha = waiting_fix.codex.latest_correction_envelope_sha256  # type: ignore[attr-defined]
    assert correction_path and correction_sha
    assert waiting_fix.cursor.iteration == 2  # type: ignore[attr-defined]
    assert waiting_fix.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]

    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    correction_attempt = ""
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, successor_run)
            if active is not None and int(active["iteration"] or 0) == 2:
                correction_attempt = str(active["attempt_id"])
        if markers.joinpath("tree.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "tool.pid")
    assert correction_attempt and send_counter.read_text(encoding="utf-8") == "3"
    invocation = json.loads(
        (successor_root / f"attempts/{correction_attempt}/invocation-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    with store.begin_read() as conn:
        ancestry = authenticate_sdk_successor_authority(
            store,
            ProtectedArtifactStore(artifacts_root),
            conn,
            successor_run,
        )
    assert ancestry is not None and ancestry.turn_kind == "initial"
    assert invocation["sdk_successor_recovery_key"] == ancestry.recovery_key
    assert invocation["prompt_path"] == correction_path
    assert invocation["prompt_sha256"] == correction_sha

    abort_service = SchedulerAbortService(
        store,
        backend,
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: datetime(2027, 1, 1, 2, tzinfo=UTC),
    )
    aborted = abort_service.abort_run(successor_run)
    assert aborted.abort_persisted is True and aborted.termination_pending is True
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    with store.begin_read() as conn:
        relation = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (successor_run,),
        ).fetchone()
        reservation = store.get_reservation_for_run(conn, successor_run)
    assert relation is not None and relation["status"] == "pending"
    intent = json.loads(str(relation["intent_payload"]))
    assert intent["turn_kind"] == "correction" and intent["iteration"] == 2
    assert intent["prompt_path"] == correction_path
    assert intent["prompt_sha256"] == correction_sha
    assert reservation is not None

    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_TREE")
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            relation = conn.execute(
                "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
                (successor_run,),
            ).fetchone()
            reservation = store.get_reservation_for_run(conn, successor_run)
        if relation is not None and relation["status"] == "ready" and reservation is None:
            break
        time.sleep(0.01)
    assert relation is not None and relation["status"] == "ready"
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        ProtectedArtifactStore(artifacts_root).read_verified_bytes(
            successor_run,
            str(relation["result_artifact_path"]),
            expected_sha256=str(relation["result_sha256"]),
        )
    )
    assert record.outcome == "abort_reconciled"
    assert reservation is None and send_counter.read_text(encoding="utf-8") == "3"
    replay = abort_service.abort_run(successor_run)
    assert replay.idempotent_replay is True and replay.termination_pending is False
    with store.begin_read() as conn:
        final_state, _, _ = store.load_validated_snapshot(conn, successor_run)
        source_name = store.run_short_name(conn, source_run)
        successor_name = store.run_short_name(conn, successor_run)
        downstream = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts "
            "WHERE run_id=? AND component IN ('staging', 'codex') AND iteration >= 2",
            (successor_run,),
        ).fetchone()[0]
    assert downstream == 0
    assert final_state.kind == "aborted"
    assert final_state.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]
    assert final_state.cursor.sdk.conversation_owner_run_id == source_run  # type: ignore[attr-defined]
    assert final_state.context.workflow.max_review_iterations == (  # type: ignore[attr-defined]
        waiting_fix.context.workflow.max_review_iterations  # type: ignore[attr-defined]
    )
    assert source_name == successor_name == "SDK advanced successor abort"


def test_failed_worker_without_result_stops_owned_descendants_before_native_replay(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-06: a retained failed unit cannot release recovery before tree termination."""
    del fake_clis
    backend = OwnedProcessGroupBackend()
    markers = tmp_path / "failed-worker-tree"
    send_counter = tmp_path / "failed-worker-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    usage = {
        "input_tokens": 4820,
        "output_tokens": 139,
        "cache_read_tokens": 2560,
        "cache_write_tokens": 0,
        "total_tokens": 7519,
        "reasoning_tokens": 118,
    }
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK failed worker replay",
        backend=backend,
    )
    turn_attempt = ""
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, run_id)
            if active is not None and int(active["iteration"] or 0) == 1:
                turn_attempt = str(active["attempt_id"])
        if markers.joinpath("tree.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "tool.pid")
    assert turn_attempt and send_counter.read_text(encoding="utf-8") == "1"
    worker = backend.processes[turn_attempt]
    bridge_pid = int((markers / "bridge.pid").read_text(encoding="utf-8"))
    tool_pid = int((markers / "tool.pid").read_text(encoding="utf-8"))
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=3)
    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_TREE")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")

    completed = _tick_until_kind(tick, store, run_id, "completed", limit=180)
    assert completed.cursor.sdk.agent_id == AGENT  # type: ignore[attr-defined]
    assert turn_attempt in backend.terminate_calls
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    assert send_counter.read_text(encoding="utf-8") == "1"
    for pid in (bridge_pid, tool_pid):
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            assert proc_stat.read_text(encoding="utf-8").split()[2] == "Z"
    with store.begin_read() as conn:
        recovered = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id=? AND event_kind='cursor_sdk_reconciled'
              AND json_extract(event_payload, '$.outcome')='terminal_recovered'
            """,
            (run_id,),
        ).fetchone()[0]
        reservation = store.get_reservation_for_run(conn, run_id)
    assert recovered == 1 and reservation is None
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.known_total_tokens == 7519
    assert cursor.inference_attempts == 1
    assert cursor.known_attempts == 1
    assert (
        len(
            list(
                ProtectedArtifactStore(artifacts_root)
                .run_root(run_id)
                .glob("usage/observations/*/*.json")
            )
        )
        == 1
    )
    assert (
        ProtectedArtifactStore(artifacts_root)
        .run_root(run_id)
        .joinpath(f"cursor/sdk-attempts/{turn_attempt}/dispatch-journal.jsonl")
        .is_file()
    )


def test_killed_reconciliation_worker_resumes_obligation_without_prompt_redispatch(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-11: a lost reconciliation envelope is fenced, stopped and rescheduled."""
    del fake_clis

    class SwitchableObservationBackend(OwnedProcessGroupBackend):
        def __init__(self) -> None:
            super().__init__()
            self.unavailable: set[str] = set()

        def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
            if attempt_id in self.unavailable:
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                    owned=False,
                )
            return super().observe(unit_identity=unit_identity, attempt_id=attempt_id)

    backend = SwitchableObservationBackend()
    markers = tmp_path / "lost-reconciliation-tree"
    send_counter = tmp_path / "lost-reconciliation-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_RECONCILIATION", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    run_id, _db, _artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK reconciliation worker restart",
        backend=backend,
    )
    reconciliation_attempt = ""
    for _ in range(220):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, run_id)
            if active is not None:
                effect = store.get_effect_by_dispatch_id(conn, str(active["dispatch_id"]))
                if effect is not None and str(effect["effect_kind"]) == (
                    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                ):
                    reconciliation_attempt = str(active["attempt_id"])
        if markers.joinpath("reconciliation.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "reconciliation.ready")
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "tool.pid")
    assert reconciliation_attempt
    assert send_counter.read_text(encoding="utf-8") == "1"

    worker = backend.processes[reconciliation_attempt]
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=3)
    backend.unavailable.add(reconciliation_attempt)
    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_RECONCILIATION")
    tick.run_once()
    with store.begin_read() as conn:
        relation = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        reservation = store.get_reservation_for_run(conn, run_id)
        interrupted = store.get_attempt_by_id(conn, reconciliation_attempt)
    assert relation is not None and relation["status"] == "pending"
    assert reservation is not None
    assert interrupted is not None and interrupted["ingested"] == 0
    assert send_counter.read_text(encoding="utf-8") == "1"

    backend.unavailable.remove(reconciliation_attempt)
    blocked = _tick_until_kind(tick, store, run_id, "blocked", limit=220)
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    with store.begin_read() as conn:
        relation = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        reservation = store.get_reservation_for_run(conn, run_id)
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind=?
            """,
            (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND),
        ).fetchone()[0]
    assert relation is not None and relation["status"] == "ready"
    assert reservation is not None
    assert reconciliation_attempts == 2
    assert send_counter.read_text(encoding="utf-8") == "1"


def test_killed_abort_reconciliation_preserves_historical_record_and_resumes(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-11: a prior published record is history, never the killed worker's result."""
    del fake_clis

    class SwitchableObservationBackend(OwnedProcessGroupBackend):
        def __init__(self) -> None:
            super().__init__()
            self.unavailable: set[str] = set()

        def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
            if attempt_id in self.unavailable:
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                    owned=False,
                )
            return super().observe(unit_identity=unit_identity, attempt_id=attempt_id)

    backend = SwitchableObservationBackend()
    markers = tmp_path / "historical-reconciliation-tree"
    send_counter = tmp_path / "historical-reconciliation-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_NATIVE_UNAVAILABLE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK historical recon restart",
        backend=backend,
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked", limit=220)
    assert blocked.block_reason_kind == "cursor_sdk_native_unavailable"  # type: ignore[attr-defined]
    artifacts = ProtectedArtifactStore(artifacts_root)
    with store.begin_read() as conn:
        historical = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert historical is not None and historical["status"] == "blocked"
    historical_path = str(historical["result_artifact_path"])
    historical_sha = str(historical["result_sha256"])
    historical_record = CursorSdkReconciliationRecordV1.model_validate_json(
        artifacts.read_verified_bytes(
            run_id,
            historical_path,
            expected_sha256=historical_sha,
        )
    )
    assert send_counter.read_text(encoding="utf-8") == "1"

    monkeypatch.delenv("FAKE_CURSOR_SDK_STREAM_FAILURE")
    monkeypatch.delenv("FAKE_CURSOR_SDK_NATIVE_UNAVAILABLE")
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_RECONCILIATION", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    abort_service = SchedulerAbortService(
        store,
        backend,
        artifacts=artifacts,
        now_factory=lambda: datetime(2027, 1, 1, 3, tzinfo=UTC),
    )
    aborted = abort_service.abort_run(run_id)
    assert aborted.abort_persisted is True and aborted.termination_pending is True

    interrupted_attempt = ""
    for _ in range(220):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, run_id)
            if active is not None:
                effect = store.get_effect_by_dispatch_id(conn, str(active["dispatch_id"]))
                if effect is not None and str(effect["effect_kind"]) == (
                    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                ):
                    interrupted_attempt = str(active["attempt_id"])
        if markers.joinpath("reconciliation.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "reconciliation.ready")
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "tool.pid")
    assert interrupted_attempt
    assert interrupted_attempt != historical_record.reconciliation_attempt_id

    worker = backend.processes[interrupted_attempt]
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=3)
    backend.unavailable.add(interrupted_attempt)
    tick.run_once()
    with store.begin_read() as conn:
        relation = conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        held = store.get_reservation_for_run(conn, run_id)
        interrupted = store.get_attempt_by_id(conn, interrupted_attempt)
    assert relation is not None and relation["status"] == "pending"
    assert held is not None and interrupted is not None and interrupted["ingested"] == 0

    backend.unavailable.remove(interrupted_attempt)
    monkeypatch.delenv("FAKE_CURSOR_SDK_BLOCK_RECONCILIATION")
    for _ in range(240):
        tick.run_once()
        with store.begin_read() as conn:
            relation = conn.execute(
                "SELECT * FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            reservation = store.get_reservation_for_run(conn, run_id)
        if relation is not None and relation["status"] == "ready" and reservation is None:
            break
        time.sleep(0.01)
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    assert relation is not None and relation["status"] == "ready"
    assert reservation is None and send_counter.read_text(encoding="utf-8") == "1"
    final_path = str(relation["result_artifact_path"])
    final_record = CursorSdkReconciliationRecordV1.model_validate_json(
        artifacts.read_verified_bytes(
            run_id,
            final_path,
            expected_sha256=str(relation["result_sha256"]),
        )
    )
    assert final_record.outcome == "abort_reconciled"
    assert final_record.reconciliation_attempt_id != interrupted_attempt
    assert final_path != historical_path
    preserved = CursorSdkReconciliationRecordV1.model_validate_json(
        artifacts.read_verified_bytes(
            run_id,
            historical_path,
            expected_sha256=historical_sha,
        )
    )
    assert preserved == historical_record
    with store.begin_read() as conn:
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind=?
            """,
            (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND),
        ).fetchone()[0]
    assert reconciliation_attempts == 3
    replay = abort_service.abort_run(run_id)
    assert replay.idempotent_replay is True and replay.termination_pending is False
    _assert_clean_abort_is_discoverable(store, run_id)


def test_phase25_7_abort_retention_starts_after_native_reconciliation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-05: retention waits for authenticated native completion, not aborted_at."""

    stores: list[SqliteSchedulerStore] = []
    original = _submit_sdk_run

    def clock() -> datetime:
        if stores:
            with stores[0].begin_read() as conn:
                aborted = conn.execute(
                    "SELECT 1 FROM scheduler_runs WHERE state_kind = 'aborted'"
                ).fetchone()
            if aborted is not None:
                return datetime(2027, 1, 1, 2, tzinfo=UTC)
        return datetime(2027, 1, 1, tzinfo=UTC)

    def submit(
        *args: object, **kwargs: object
    ) -> tuple[str, Path, Path, SqliteSchedulerStore, TickService]:
        kwargs["now_factory"] = clock
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        stores.append(result[3])
        return result

    namespace = (
        test_abort_active_sdk_send_persists_native_obligation_until_idempotent_release.__globals__
    )
    namespace["_submit_sdk_run"] = submit
    try:
        test_abort_active_sdk_send_persists_native_obligation_until_idempotent_release(
            git_repo,
            isolated_xdg,
            fake_clis,
            monkeypatch,
            tmp_path,
        )
    finally:
        namespace["_submit_sdk_run"] = original
    store = stores[0]
    state_root = store.db_path.parent
    with store.begin_read() as conn:
        run = conn.execute("SELECT run_id, state_payload FROM scheduler_runs").fetchone()
        event = conn.execute(
            """
            SELECT created_at FROM scheduler_events
             WHERE run_id = ? AND event_kind = 'cursor_sdk_reconciled'
             ORDER BY sequence DESC
            """,
            (run["run_id"],),
        ).fetchone()
    payload = json.loads(str(run["state_payload"]))
    assert payload["aborted_at"] == "2027-01-01T01:00:00.000000Z"
    assert event is not None and event["created_at"] == "2027-01-01T02:00:00.000000Z"
    key = cursor_conversation_key(str(run["run_id"]))
    root = state_root / "cursor-sdk" / key
    assert root.is_dir()
    abort_boundary = datetime(2027, 1, 8, 1, tzinfo=UTC)
    completion = datetime(2027, 1, 8, 2, tzinfo=UTC)
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=abort_boundary)
    assert len(preview.conversations) == 1
    assert preview.conversations[0].definitively_finished_at == "2027-01-01T02:00:00.000000Z"
    assert preview.conversations[0].automatic_eligible is False
    assert (
        maintain_cursor_sdk_conversations(
            store, state_root=state_root, now=abort_boundary
        ).deleted_keys
        == ()
    )
    assert (
        maintain_cursor_sdk_conversations(
            store,
            state_root=state_root,
            now=completion - timedelta(microseconds=1),
        ).deleted_keys
        == ()
    )
    assert root.is_dir()
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=completion)
    assert deleted.deleted_keys == (key,)
    assert not root.exists()
    with store.begin_read() as conn:
        receipt = conn.execute(
            "SELECT receipt_id, receipt_payload FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
            (key,),
        ).fetchone()
    tombstone = (state_root / "cursor-sdk-disposals" / f"{key}.json").read_text(encoding="utf-8")
    assert receipt is not None and tombstone == receipt["receipt_payload"]
    repeated = maintain_cursor_sdk_conversations(store, state_root=state_root, now=completion)
    assert repeated.deleted_keys == ()
    with store.begin_read() as conn:
        again = conn.execute(
            "SELECT receipt_id, receipt_payload FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
            (key,),
        ).fetchone()
    assert again["receipt_id"] == receipt["receipt_id"]
    assert (state_root / "cursor-sdk-disposals" / f"{key}.json").read_text(
        encoding="utf-8"
    ) == tombstone


def _assert_clean_abort_is_discoverable(store: SqliteSchedulerStore, run_id: str) -> None:
    state_root = store.db_path.parent
    key = cursor_conversation_key(run_id)
    root = state_root / "cursor-sdk" / key
    assert root.is_dir()
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert len(preview.conversations) == 1
    item = preview.conversations[0]
    assert item.conversation_key == key
    assert item.manual_eligible is True
    assert item.definitively_finished_at is not None
    finished = datetime.fromisoformat(item.definitively_finished_at.replace("Z", "+00:00"))
    due = preview_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=finished + timedelta(hours=168),
    )
    assert due.conversations[0].automatic_eligible is True
    held = maintain_cursor_sdk_conversations(
        store,
        state_root=state_root,
        now=finished + timedelta(hours=168) - timedelta(microseconds=1),
    )
    assert held.deleted_keys == ()
    assert root.is_dir()


def test_phase25_7_blocked_abort_manual_disposal_removes_one_root(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F-04: a reconciled abort with no current SDK checkpoint can still be deleted."""

    test_killed_abort_reconciliation_preserves_historical_record_and_resumes(
        git_repo,
        isolated_xdg,
        fake_clis,
        monkeypatch,
        tmp_path,
    )
    store = SqliteSchedulerStore(isolated_xdg / "state/ai_dev_loop/engine.sqlite3")
    with store.begin_read() as conn:
        run_id = str(conn.execute("SELECT run_id FROM scheduler_runs").fetchone()["run_id"])
    state_root = store.db_path.parent
    key = cursor_conversation_key(run_id)
    root = state_root / "cursor-sdk" / key
    assert root.is_dir()
    applied = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=datetime(2030, 1, 1, tzinfo=UTC),
        mode="manual",
        conversation_key=key,
    )
    assert applied.action == "deleted"
    assert not root.exists()
    with store.begin_read() as conn:
        still = conn.execute(
            "SELECT state_kind FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert still is not None and still["state_kind"] == "aborted"


def test_recovered_sdk_sequence_leaf_checkpoints_then_advances_to_codex_provider(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R-04 acceptance: recovery preserves ordinal, then mixed-provider advancement."""
    del fake_clis
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sequence recovery baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk-sequence-recovery"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    reviewer_id = "019abc00-1111-4222-8333-999999999999"
    implementer_id = "019abc00-1111-4222-8333-888888888888"
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", reviewer_id)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", implementer_id)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "codex-final.txt")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "failed")
    manifest = git_repo / "sdk-recovery-mixed-v4.yaml"
    sdk_cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "sdk recovery mixed",
                "short_name": "SDK recovery mixed",
                "phases": [
                    {
                        "name": "sdk-recovery",
                        "short_name": "SDK recovery leaf",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "commit_message": "accepted SDK recovery checkpoint",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {"provider": "cursor"},
                    },
                    {
                        "name": "codex-final",
                        "short_name": "Codex final leaf",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {
                            "provider": "codex",
                            "command": "codex",
                            "model": "gpt-6.1-sol",
                            "reasoning_effort": "high",
                            "timeout_minutes": 90,
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    source_run = started.run_id
    _bind_fake_sdk_run(monkeypatch, artifacts_root, source_run)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2027, 1, 3, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase253-sequence-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    source = _tick_until_kind(tick, store, source_run, "blocked")
    assert source.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    immutable_source = source.model_dump_json()  # type: ignore[attr-defined]
    for _ in range(5):
        tick.run_once()
        with store.begin_read() as conn:
            blocked_sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if type(blocked_sequence).__name__ == "BlockedSequenceState":
            break
    assert type(blocked_sequence).__name__ == "BlockedSequenceState"
    assert blocked_sequence.current_run_id == source_run  # type: ignore[attr-defined]
    runner = CliRunner()
    forced = runner.invoke(
        app,
        ["scheduler", "cursor-retry", source_run, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    successor = str(json.loads(forced.stdout)["successor_run_id"])
    with store.begin_read() as conn:
        adopted = store.load_validated_sequence_state(conn, prepared.sequence_id)
    assert adopted.current_ordinal == 1 and adopted.current_run_id == successor  # type: ignore[attr-defined]

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor)
    final_run = ""
    sequence = None
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
            final_run = str(getattr(sequence, "final_run_id", "") or "")
        if type(sequence).__name__ == "AwaitingFinalizationSequenceState":
            break
    assert sequence is not None
    assert type(sequence).__name__ == "AwaitingFinalizationSequenceState"
    assert final_run
    with store.begin_read() as conn:
        source_after, _, _ = store.load_validated_snapshot(conn, source_run)
        recovered, _, _ = store.load_validated_snapshot(conn, successor)
        final, _, _ = store.load_validated_snapshot(conn, final_run)
        attempts = conn.execute(
            """
            SELECT run_id, attempt_kind FROM scheduler_sequence_run_attempts
            WHERE sequence_id=? AND ordinal=1 ORDER BY generation
            """,
            (prepared.sequence_id,),
        ).fetchall()
    assert source_after.model_dump_json() == immutable_source
    assert recovered.kind == final.kind == "completed"
    assert recovered.context.implementer.provider == "cursor"
    assert final.context.implementer.provider == "codex"
    assert final.codex.implementer_session_id == implementer_id
    assert final.codex.reviewer_session_id == reviewer_id
    assert final.codex.implementer_session_id != final.codex.reviewer_session_id
    assert [(row["run_id"], row["attempt_kind"]) for row in attempts] == [
        (source_run, "planned_run"),
        (successor, "cursor_retry"),
    ]
    assert (
        subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"], cwd=git_repo, text=True
        ).strip()
        == "accepted SDK recovery checkpoint"
    )
    assert (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True
        ).strip()
        == "codex-final.txt"
    )


def test_reconciled_failure_routes_through_cursor_implementer_retry_and_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    store, artifacts, run_id = _ready_fixture(
        tmp_path,
        scheduler_state_root=xdg_state / "ai_dev_loop",
    )
    event_ids = iter(("evt-reconciliation-request", "evt-reconciled", "evt-blocked"))
    service = CursorSdkReconciliationService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        event_id_factory=lambda: next(event_ids),
        dispatch_id_factory=lambda: "fx-reconciliation",
    )
    source_attempt = "att-" + "1" * 32
    source_dispatch = "fx-source"
    journal_rel = cursor_sdk_attempt_journal_rel(source_attempt)
    journal = artifacts.run_root(run_id) / journal_rel
    requested = service.request(
        run_id,
        source_attempt_id=source_attempt,
        source_dispatch_id=source_dispatch,
        outcome={
            "journal_path": journal_rel,
            "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        },
        failure_class="failure",
    )
    assert requested.action == "cursor_sdk_reconciliation_requested"
    reconciliation_attempt = _insert_terminal_cursor_attempt(
        store,
        run_id=run_id,
        attempt_id="att-" + "2" * 32,
        dispatch_id="fx-reconciliation",
        effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        status="completed",
    )
    record = CursorSdkReconciliationRecordV1(
        run_id=run_id,
        source_attempt_id=source_attempt,
        reconciliation_attempt_id="att-" + "2" * 32,
        source_dispatch_id=source_dispatch,
        reconciliation_dispatch_id="fx-reconciliation",
        conversation_owner_run_id=run_id,
        conversation_key=cursor_conversation_key(run_id),
        agent_id=AGENT,
        prompt_sha256=sha256_bytes(b"implement the approved phase"),
        source_journal_path=journal_rel,
        source_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
        baseline_run_ids=(),
        candidate_run_ids=(NATIVE,),
        candidate_final_statuses={NATIVE: "failed"},
        native_run_id=NATIVE,
        initial_native_status="running",
        final_native_status="failed",
        outcome="continuation_ready",
        cancellation_requested=True,
    )
    record_path = cursor_sdk_reconciliation_record_rel(source_attempt)
    stored = artifacts.write_bytes(run_id, record_path, record.canonical_bytes(), max_bytes=100_000)
    ingested = service.ingest(
        run_id,
        attempt=reconciliation_attempt,
        outcome={
            "reconciliation_record_path": stored.relative_path,
            "reconciliation_record_sha256": stored.sha256,
        },
    )
    assert ingested.action == "blocked" and ingested.detail == "cursor_failure"

    checked = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert checked.receipt.reason_code == "authenticated_cursor_sdk_reconciliation"
    assert checked.receipt.recovery_supported is True
    runner = CliRunner()
    cli_check = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert cli_check.exit_code == 0, cli_check.output
    assert json.loads(cli_check.stdout)["reason_code"] == (
        "authenticated_cursor_sdk_reconciliation"
    )
    cli_force = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"],
    )
    assert cli_force.exit_code == 0, cli_force.output
    force_payload = json.loads(cli_force.stdout)
    assert force_payload["changed"] is True
    first_successor_run_id = str(force_payload["successor_run_id"])
    with store.begin_read() as conn:
        successor, _, _ = store.load_validated_snapshot(conn, first_successor_run_id)
        source_name = store.run_short_name(conn, run_id)
        successor_name = store.run_short_name(conn, first_successor_run_id)
        relation = store.get_cursor_initial_recovery_by_successor(
            conn, successor_run_id=first_successor_run_id
        )
    assert isinstance(successor, CursorReadyState)
    assert successor.cursor.sdk is not None
    assert successor.cursor.sdk.agent_id == AGENT
    assert successor.cursor.sdk.conversation_owner_run_id == run_id
    assert source_name == successor_name == "SDK recovery phase"
    assert (
        relation is not None and json.loads(str(relation["intent_payload"]))["schema_version"] == 3
    )

    set_run_short_name(
        first_successor_run_id,
        "Operator successor rename",
        db_path=store.db_path,
        now=NOW,
    )
    cli_replay = runner.invoke(
        app,
        ["scheduler", "implementer-retry", run_id, "--force", "--output", "json"],
    )
    assert cli_replay.exit_code == 0, cli_replay.output
    replay_payload = json.loads(cli_replay.stdout)
    assert replay_payload["run_id"] == first_successor_run_id
    assert replay_payload["provider"] == "cursor" and replay_payload["changed"] is False
    with store.begin_read() as conn:
        assert store.run_short_name(conn, first_successor_run_id) == "Operator successor rename"

    second_source = first_successor_run_id
    second_attempt = "att-" + "4" * 32
    with store.begin_read() as conn:
        source_effect = conn.execute(
            "SELECT dispatch_id FROM scheduler_effects "
            "WHERE run_id=? AND effect_kind=? AND status='pending'",
            (second_source, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
    assert source_effect is not None
    second_dispatch = str(source_effect["dispatch_id"])
    _insert_terminal_cursor_attempt(
        store,
        run_id=second_source,
        attempt_id=second_attempt,
        dispatch_id=second_dispatch,
        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        status="failed",
    )
    second_native = "run-019abc00-1111-4222-8333-777777777777"
    effective = artifacts.read_verified_bytes(
        second_source,
        SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
        expected_sha256=sha256_bytes(
            b"implement the approved phase\n\n" + RECOVERY_NOTE.encode() + b"\n"
        ),
    )
    effective_sha = sha256_bytes(effective)
    second_journal_rel = cursor_sdk_attempt_journal_rel(second_attempt)
    second_journal = artifacts.run_root(second_source) / second_journal_rel
    second_base = {
        "attempt_id": second_attempt,
        "dispatch_id": second_dispatch,
        "conversation_owner_run_id": run_id,
        "conversation_key": cursor_conversation_key(run_id),
        "agent_id": AGENT,
        "prompt_sha256": effective_sha,
    }
    append_sdk_journal(
        second_journal,
        kind="run_inventory_baseline",
        payload={**second_base, "run_ids": [NATIVE]},
    )
    append_sdk_journal(second_journal, kind="send_intent", payload=second_base)
    append_sdk_journal(
        second_journal,
        kind="native_run_bound",
        payload={**second_base, "native_run_id": second_native},
    )
    append_sdk_journal(
        second_journal,
        kind="terminal",
        payload={
            **second_base,
            "native_run_id": second_native,
            "outcome": "uncertain",
            "failure_kind": "cursor_sdk_native_run_uncertain",
        },
    )
    second_service = CursorSdkReconciliationService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        event_id_factory=iter(
            ("evt-second-request", "evt-second-reconciled", "evt-second-blocked")
        ).__next__,
        dispatch_id_factory=lambda: "fx-second-reconciliation",
    )
    second_service.request(
        second_source,
        source_attempt_id=second_attempt,
        source_dispatch_id=second_dispatch,
        outcome={
            "journal_path": second_journal_rel,
            "journal_sha256": hashlib.sha256(second_journal.read_bytes()).hexdigest(),
        },
        failure_class="failure",
    )
    second_reconciliation_attempt = _insert_terminal_cursor_attempt(
        store,
        run_id=second_source,
        attempt_id="att-" + "5" * 32,
        dispatch_id="fx-second-reconciliation",
        effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        status="completed",
    )
    second_record = CursorSdkReconciliationRecordV1(
        run_id=second_source,
        source_attempt_id=second_attempt,
        reconciliation_attempt_id="att-" + "5" * 32,
        source_dispatch_id=second_dispatch,
        reconciliation_dispatch_id="fx-second-reconciliation",
        conversation_owner_run_id=run_id,
        conversation_key=cursor_conversation_key(run_id),
        agent_id=AGENT,
        prompt_sha256=effective_sha,
        source_journal_path=second_journal_rel,
        source_journal_sha256=hashlib.sha256(second_journal.read_bytes()).hexdigest(),
        baseline_run_ids=(NATIVE,),
        candidate_run_ids=(second_native,),
        candidate_final_statuses={second_native: "failed"},
        native_run_id=second_native,
        initial_native_status="running",
        final_native_status="failed",
        outcome="continuation_ready",
        cancellation_requested=True,
    )
    second_record_path = cursor_sdk_reconciliation_record_rel(second_attempt)
    second_stored = artifacts.write_bytes(
        second_source,
        second_record_path,
        second_record.canonical_bytes(),
        max_bytes=100_000,
    )
    second_service.ingest(
        second_source,
        attempt=second_reconciliation_attempt,
        outcome={
            "reconciliation_record_path": second_stored.relative_path,
            "reconciliation_record_sha256": second_stored.sha256,
        },
    )
    second_retry = ImplementerRetryService(store, artifacts, now_factory=lambda: NOW).retry(
        second_source,
        force=True,
    )
    with store.begin_read() as conn:
        second_successor, _, _ = store.load_validated_snapshot(conn, second_retry.run_id)
        assert store.run_short_name(conn, second_retry.run_id) == "Operator successor rename"
    assert isinstance(second_successor, CursorReadyState)
    assert second_successor.cursor.sdk is not None
    assert second_successor.cursor.sdk.agent_id == AGENT
    assert second_successor.cursor.sdk.conversation_owner_run_id == run_id
    assert (
        artifacts.read_verified_bytes(
            second_retry.run_id,
            SDK_SUCCESSOR_BASE_PROMPT_REL,
            expected_sha256=sha256_bytes(b"implement the approved phase"),
        )
        == b"implement the approved phase"
    )
    assert (
        artifacts.read_verified_bytes(
            second_retry.run_id,
            SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
            expected_sha256=effective_sha,
        )
        == effective
    )


def test_abort_holds_reservation_until_native_candidates_are_terminal(tmp_path: Path) -> None:
    store, artifacts, run_id = _ready_fixture(tmp_path)
    event_ids = iter(("evt-abort-request", "evt-abort-rescheduled", "evt-abort-reconciled"))
    dispatch_ids = iter(("fx-abort-reconciliation", "fx-abort-reconciliation-rescheduled"))
    service = CursorSdkReconciliationService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        event_id_factory=event_ids.__next__,
        dispatch_id_factory=dispatch_ids.__next__,
    )
    source_attempt = "att-" + "1" * 32
    source_dispatch = "fx-source"
    journal_rel = cursor_sdk_attempt_journal_rel(source_attempt)
    journal = artifacts.run_root(run_id) / journal_rel
    service.request(
        run_id,
        source_attempt_id=source_attempt,
        source_dispatch_id=source_dispatch,
        outcome={
            "journal_path": journal_rel,
            "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        },
        failure_class="failure",
    )

    abort_service = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=artifacts,
        now_factory=lambda: NOW,
        event_id_factory=iter(("evt-abort-durable", "evt-aborted")).__next__,
        fence_id_factory=lambda: "fnc-sdk-abort",
    )
    first_abort = abort_service.abort_run(run_id)
    assert first_abort.abort_persisted is True
    assert first_abort.termination_pending is True
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None

    receipts = service.ensure_pending_effects()
    assert [receipt.action for receipt in receipts] == ["cursor_sdk_reconciliation_rescheduled"]
    reconciliation_attempt = _insert_terminal_cursor_attempt(
        store,
        run_id=run_id,
        attempt_id="att-" + "6" * 32,
        dispatch_id="fx-abort-reconciliation-rescheduled",
        effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        status="completed",
    )
    record = CursorSdkReconciliationRecordV1(
        run_id=run_id,
        source_attempt_id=source_attempt,
        reconciliation_attempt_id="att-" + "6" * 32,
        source_dispatch_id=source_dispatch,
        reconciliation_dispatch_id="fx-abort-reconciliation-rescheduled",
        conversation_owner_run_id=run_id,
        conversation_key=cursor_conversation_key(run_id),
        agent_id=AGENT,
        prompt_sha256=sha256_bytes(b"implement the approved phase"),
        source_journal_path=journal_rel,
        source_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
        baseline_run_ids=(),
        candidate_run_ids=(NATIVE,),
        candidate_final_statuses={NATIVE: "cancelled"},
        native_run_id=NATIVE,
        initial_native_status="running",
        final_native_status="cancelled",
        outcome="abort_reconciled",
        cancellation_requested=True,
    )
    record_path = cursor_sdk_reconciliation_record_rel(source_attempt)
    stored = artifacts.write_bytes(
        run_id,
        record_path,
        record.canonical_bytes(),
        max_bytes=100_000,
    )
    reconciled = service.ingest(
        run_id,
        attempt=reconciliation_attempt,
        outcome={
            "reconciliation_record_path": stored.relative_path,
            "reconciliation_record_sha256": stored.sha256,
        },
    )
    assert reconciled.action == "cursor_sdk_abort_reconciled"
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None

    final_abort = abort_service.abort_run(run_id)
    assert final_abort.termination_pending is False
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is None


@pytest.mark.parametrize(
    ("failure_class", "expected_action"),
    (("timeout", "cursor_timeout_retry_scheduled"), ("capacity", "waiting_usage_limit")),
)
def test_reconciled_timeout_and_capacity_reenter_existing_policy_once(
    tmp_path: Path,
    failure_class: str,
    expected_action: str,
) -> None:
    store, artifacts, run_id = _ready_fixture(tmp_path)
    event_counter = iter(range(10))
    service = CursorSdkReconciliationService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        event_id_factory=lambda: f"evt-policy-{next(event_counter)}",
        dispatch_id_factory=lambda: f"fx-policy-{next(event_counter)}",
    )
    source_attempt = "att-" + "1" * 32
    source_dispatch = "fx-source"
    journal_rel = cursor_sdk_attempt_journal_rel(source_attempt)
    journal = artifacts.run_root(run_id) / journal_rel
    service.request(
        run_id,
        source_attempt_id=source_attempt,
        source_dispatch_id=source_dispatch,
        outcome={
            "journal_path": journal_rel,
            "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        },
        failure_class=failure_class,
        retry_after_seconds=120 if failure_class == "capacity" else None,
    )
    with store.begin_read() as conn:
        reconcile_effect = conn.execute(
            "SELECT dispatch_id FROM scheduler_effects "
            "WHERE run_id=? AND effect_kind='cursor.reconcile_sdk_run'",
            (run_id,),
        ).fetchone()
    assert reconcile_effect is not None
    reconciliation_dispatch = str(reconcile_effect["dispatch_id"])
    reconciliation_attempt = _insert_terminal_cursor_attempt(
        store,
        run_id=run_id,
        attempt_id="att-" + "3" * 32,
        dispatch_id=reconciliation_dispatch,
        effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        status="completed",
    )
    record = CursorSdkReconciliationRecordV1(
        run_id=run_id,
        source_attempt_id=source_attempt,
        reconciliation_attempt_id="att-" + "3" * 32,
        source_dispatch_id=source_dispatch,
        reconciliation_dispatch_id=reconciliation_dispatch,
        conversation_owner_run_id=run_id,
        conversation_key=cursor_conversation_key(run_id),
        agent_id=AGENT,
        prompt_sha256=sha256_bytes(b"implement the approved phase"),
        source_journal_path=journal_rel,
        source_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
        baseline_run_ids=(),
        candidate_run_ids=(NATIVE,),
        candidate_final_statuses={NATIVE: "cancelled"},
        native_run_id=NATIVE,
        initial_native_status="running",
        final_native_status="cancelled",
        outcome="continuation_ready",
        cancellation_requested=True,
    )
    record_path = cursor_sdk_reconciliation_record_rel(source_attempt)
    stored = artifacts.write_bytes(run_id, record_path, record.canonical_bytes(), max_bytes=100_000)
    receipt = service.ingest(
        run_id,
        attempt=reconciliation_attempt,
        outcome={
            "reconciliation_record_path": stored.relative_path,
            "reconciliation_record_sha256": stored.sha256,
        },
    )
    assert receipt.action == expected_action
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        pending_turns = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects "
            "WHERE run_id=? AND effect_kind='cursor.run_turn' AND status='pending'",
            (run_id,),
        ).fetchone()[0]
        pending_timers = conn.execute(
            "SELECT COUNT(*) FROM scheduler_timers WHERE run_id=? AND status='pending'",
            (run_id,),
        ).fetchone()[0]
    if failure_class == "timeout":
        assert isinstance(state, CursorReadyState)
        assert state.cursor.timeout_automatic_retries == 1
        assert pending_turns == 1 and pending_timers == 0
    else:
        assert isinstance(state, WaitingUsageLimitState)
        assert pending_turns == 0 and pending_timers == 1
