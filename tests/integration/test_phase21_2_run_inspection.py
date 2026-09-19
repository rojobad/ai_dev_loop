"""Integration tests for Phase 21.2 run inspection commands."""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ai_dev_loop.integration_api.validation import (
    ARTIFACT_HARD_MAX_LIMIT,
    COLLECTION_HARD_MAX_LIMIT,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import (
    CONTROLLER_SESSION,
    sample_agent_led_submitted_context,
    sample_submitted_state,
)
from tests.unit.scheduler.test_v5_migration import _pause_v4_database
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.run_service import default_run_read_service
from ai_dev_loop.integration_api.schemas import (
    ATTEMPT_LIST_DATA_SCHEMA,
    FROZEN_ARTIFACT_CHUNK_SCHEMA,
    HISTORY_LIST_DATA_SCHEMA,
    RUN_INSPECT_DATA_SCHEMA,
    RUN_LIST_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    SequenceRunBinding,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()
SECRET_SENTINEL = "INTEGRATION_PHASE21_2_SECRET_SENTINEL"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit_run(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def _invoke(args: list[str]) -> tuple[int, dict[str, object], str]:
    result = runner.invoke(app, args)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result.stderr


def _artifact_tree_snapshot(artifact_root: Path) -> dict[str, tuple[int, int]]:
    if not artifact_root.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in sorted(artifact_root.rglob("*")):
        stat = path.stat()
        snapshot[str(path.relative_to(artifact_root))] = (stat.st_mode, stat.st_size)
    return snapshot


def _logical_ledger_snapshot(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        runs = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT run_id, state_kind, version, idempotency_key, created_at
                FROM scheduler_runs
                ORDER BY run_id
                """
            )
        )
        events = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT event_id, run_id, sequence, event_kind
                FROM scheduler_events
                ORDER BY run_id, sequence
                """
            )
        )
        reservations = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT worktree_key, run_id, status
                FROM scheduler_repository_reservations
                ORDER BY worktree_key
                """
            )
        )
    finally:
        conn.close()
    return {
        "user_version": version,
        "runs": runs,
        "events": events,
        "reservations": reservations,
    }


def _insert_history_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    sequence: int,
    event_kind: str,
    payload: dict[str, object],
    created_at: str,
) -> None:
    payload_text = json.dumps(payload)
    conn.execute(
        """
        INSERT INTO scheduler_events(
            event_id, run_id, sequence, event_kind,
            event_payload, event_payload_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"evt-history-{sequence:03d}",
            run_id,
            sequence,
            event_kind,
            payload_text,
            payload_sha256(payload_text),
            created_at,
        ),
    )


def test_c01_list_and_inspect_standalone_and_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    standalone_id = _submit_run(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    other_repo = git_repo.parent / "sequence-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init"], cwd=other_repo, check=True, capture_output=True)
    sequence_context = sample_agent_led_submitted_context(
        repo_root=str(other_repo.resolve()),
    ).model_copy(
        update={
            "schema_version": SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
            "sequence": SequenceRunBinding(
                sequence_id="seq-11111111-1111-1111-1111-111111111111",
                ordinal=2,
                total_phases=4,
                entry_hash="c" * 64,
            ),
        }
    )
    sequence_state = sample_submitted_state(run_id="sequence-bound-run").model_copy(
        update={"context": sequence_context},
    )
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=sequence_state.run_id,
            state=sequence_state,
            event_id="evt-seq",
            event=RunSubmittedEvent(
                run_id=sequence_state.run_id,
                idempotency_key=sequence_state.idempotency_key,
                worktree_key=sequence_state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=now,
        )

    code, payload, stderr = _invoke(["integration", "runs", "list", "--output", "json"])
    assert code == 0
    assert payload["ok"] is True
    items = payload["data"]["items"]
    assert len(items) == 2
    listed_ids = {item["runId"] for item in items}
    assert listed_ids == {standalone_id, sequence_state.run_id}
    validate_integration_instance(payload["data"], RUN_LIST_DATA_SCHEMA)
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr

    code, payload, _stderr = _invoke(
        ["integration", "runs", "list", "--kind", "sequence", "--output", "json"]
    )
    assert code == 0
    assert len(payload["data"]["items"]) == 1
    binding = payload["data"]["items"][0]["sequenceBinding"]
    assert binding["sequenceId"] == "seq-11111111-1111-1111-1111-111111111111"
    assert binding["ordinal"] == 2
    assert binding["phaseCount"] == 4

    code, payload, _stderr = _invoke(
        ["integration", "run", "inspect", standalone_id, "--output", "json"]
    )
    assert code == 0
    data = payload["data"]
    validate_integration_instance(data, RUN_INSPECT_DATA_SCHEMA)
    assert data["runId"] == standalone_id
    assert data["safeNextAction"]["kind"] == "scheduler_start"
    assert "command" not in data["safeNextAction"]
    assert data["residualRisk"] is None
    assert data["reviewBudget"]["completed"] == 0
    assert data["reviewBudget"]["max"] == 3
    assert data["reviewBudget"]["submittedMax"] is None

    code, payload, _stderr = _invoke(
        ["integration", "run", "inspect", "missing-run-id", "--output", "json"]
    )
    assert code == 3
    assert payload["error"]["code"] == "NOT_FOUND"


def test_c01_empty_xdg_list(isolated_xdg: Path) -> None:
    code, payload, _stderr = _invoke(["integration", "runs", "list", "--output", "json"])
    assert code == 0
    assert payload["data"]["items"] == []


def test_c02_attempts_history_pagination_and_redaction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    base = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    created_text = base.isoformat().replace("+00:00", "Z")
    secret_payload = json.dumps({"cursor_fix_prompt": SECRET_SENTINEL})
    with store.begin_immediate() as conn:
        source_event_id = str(
            conn.execute(
                "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence ASC LIMIT 1",
                (run_id,),
            ).fetchone()[0]
        )
        for index in range(3):
            dispatch_id = f"dispatch-{index:02d}"
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'cursor.turn', ?, ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    source_event_id,
                    index + 1,
                    run_id,
                    dispatch_id,
                    f"{run_id}:{dispatch_id}",
                    secret_payload,
                    "d" * 64,
                    created_text,
                    created_text,
                    created_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, 'cursor', 1, 'completed', ?, ?, ?, ?)
                """,
                (
                    f"attempt-{index:02d}",
                    run_id,
                    dispatch_id,
                    created_text,
                    created_text,
                    created_text,
                    created_text,
                ),
            )
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 2, 'attempt_completed', ?, ?, ?)
            """,
            (
                "evt-history",
                run_id,
                secret_payload,
                "d" * 64,
                created_text,
            ),
        )

    code, attempts_payload, stderr = _invoke(
        ["integration", "run", "attempts", run_id, "--limit", "2", "--output", "json"]
    )
    assert code == 0
    validate_integration_instance(attempts_payload["data"], ATTEMPT_LIST_DATA_SCHEMA)
    assert attempts_payload["data"]["page"]["hasMore"] is True
    assert len(attempts_payload["data"]["items"]) == 2
    assert attempts_payload["data"]["items"][0]["phaseAttempt"] == 1
    assert attempts_payload["data"]["items"][0]["attemptId"] == "attempt-00"
    assert attempts_payload["data"]["items"][0]["effectKind"] == "cursor.turn"
    assert SECRET_SENTINEL not in json.dumps(attempts_payload)
    assert SECRET_SENTINEL not in stderr

    code, payload, _stderr = _invoke(
        [
            "integration",
            "run",
            "attempts",
            run_id,
            "--offset",
            "2",
            "--limit",
            "2",
            "--output",
            "json",
        ]
    )
    assert payload["data"]["items"][0]["phaseAttempt"] == 3

    code, payload, stderr = _invoke(["integration", "run", "history", run_id, "--output", "json"])
    assert code == 0
    validate_integration_instance(payload["data"], HISTORY_LIST_DATA_SCHEMA)
    assert SECRET_SENTINEL not in payload["data"]["items"][0]["safeDetail"]
    assert SECRET_SENTINEL not in stderr
    assert (
        SECRET_SENTINEL not in runner.invoke(app, ["integration", "run", "history", run_id]).stdout
    )

    code, timeline_payload, _stderr = _invoke(
        ["integration", "run", "timeline", run_id, "--output", "json"]
    )
    assert code == 0
    validate_integration_instance(timeline_payload["data"], ATTEMPT_LIST_DATA_SCHEMA)
    assert (
        timeline_payload["data"]["items"][0]["attemptId"]
        == attempts_payload["data"]["items"][0]["attemptId"]
    )


def test_c03_frozen_plan_survives_repository_mutation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    plan_path = git_repo / "docs/plans/sample-plan.md"
    original = plan_path.read_bytes()
    plan_path.write_text("mutated plan content", encoding="utf-8")
    plan_path.unlink()

    code, payload, _stderr = _invoke(["integration", "run", "plan", run_id, "--output", "json"])
    assert code == 0
    chunk = payload["data"]
    validate_integration_instance(chunk, FROZEN_ARTIFACT_CHUNK_SCHEMA)
    assert chunk["sha256"] is not None
    decoded = base64.b64decode(chunk["contentBase64"])
    assert decoded == original
    assert chunk["sourceRepositoryPath"] == "docs/plans/sample-plan.md"


def test_c04_read_only_with_lock_held(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    created_text = datetime(2026, 9, 18, 14, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    with store.begin_immediate() as conn:
        _insert_history_event(
            conn,
            run_id=run_id,
            sequence=2,
            event_kind="attempt_completed",
            payload={"cursor_fix_prompt": SECRET_SENTINEL, "safe_summary": SECRET_SENTINEL},
            created_at=created_text,
        )
    before_ledger = _logical_ledger_snapshot(scheduler_paths["db_path"])
    before_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])
    with store.begin_immediate():
        code, inspect_payload, inspect_stderr = _invoke(
            ["integration", "run", "inspect", run_id, "--output", "json"]
        )
        assert code == 0
        code, plan_payload, plan_stderr = _invoke(
            ["integration", "run", "plan", run_id, "--output", "json"]
        )
        assert code == 0
        code, history_payload, history_stderr = _invoke(
            ["integration", "run", "history", run_id, "--output", "json"]
        )
        assert code == 0
        for blob in (
            json.dumps(inspect_payload),
            json.dumps(plan_payload),
            json.dumps(history_payload),
            inspect_stderr,
            plan_stderr,
            history_stderr,
        ):
            assert SECRET_SENTINEL not in blob
    assert _logical_ledger_snapshot(scheduler_paths["db_path"]) == before_ledger
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree


def test_c05_v4_database_list_runs_without_migrating(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    SqliteSchedulerStore.open_readonly(db)
    service = default_run_read_service(db_path=db)
    listed = service.list_runs()
    assert listed.items == ()
    version = int(sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0])
    assert version == 4


def test_c01_list_ordering_newest_first(scheduler_paths: dict[str, Path]) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    older_state = sample_submitted_state(run_id="older-submitted-run", repo_root="/tmp/repo-a")
    newer_state = sample_submitted_state(
        run_id="newer-submitted-run",
        repo_root="/tmp/repo-b",
    ).model_copy(
        update={
            "idempotency_key": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        },
    )
    older = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=older_state.run_id,
            state=older_state,
            event_id="evt-older",
            event=RunSubmittedEvent(
                run_id=older_state.run_id,
                idempotency_key=older_state.idempotency_key,
                worktree_key=older_state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=older,
        )
        store.insert_submitted_run(
            conn,
            run_id=newer_state.run_id,
            state=newer_state,
            event_id="evt-newer",
            event=RunSubmittedEvent(
                run_id=newer_state.run_id,
                idempotency_key=newer_state.idempotency_key,
                worktree_key=newer_state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=newer,
        )
    first_id = older_state.run_id
    second_id = newer_state.run_id
    code, payload, _stderr = _invoke(
        ["integration", "runs", "list", "--limit", "1", "--output", "json"]
    )
    assert code == 0
    assert payload["data"]["page"]["hasMore"] is True
    assert payload["data"]["items"][0]["runId"] == second_id
    assert payload["data"]["items"][0]["runId"] != first_id


def test_c01_kind_filter_before_pagination_and_bounded_projection(
    scheduler_paths: dict[str, Path],
) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    tie_time = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    standalone_ids = ("run-aaa-standalone", "run-bbb-standalone", "run-ccc-standalone")
    standalone_keys = ("a" * 64, "b" * 64, "c" * 64)
    for index, run_id in enumerate(standalone_ids):
        state = sample_submitted_state(
            run_id=run_id,
            repo_root=f"/tmp/repo-standalone-{index}",
        ).model_copy(update={"idempotency_key": standalone_keys[index]})
        with store.begin_immediate() as conn:
            store.insert_submitted_run(
                conn,
                run_id=state.run_id,
                state=state,
                event_id=f"evt-{run_id}",
                event=RunSubmittedEvent(
                    run_id=state.run_id,
                    idempotency_key=state.idempotency_key,
                    worktree_key=state.context.repository.worktree_key,
                    reused_existing=False,
                ),
                now=tie_time,
            )

    sequence_context = sample_agent_led_submitted_context(
        repo_root="/tmp/repo-sequence"
    ).model_copy(
        update={
            "schema_version": SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
            "sequence": SequenceRunBinding(
                sequence_id="seq-22222222-2222-2222-2222-222222222222",
                ordinal=1,
                total_phases=2,
                entry_hash="d" * 64,
            ),
        }
    )
    sequence_state = sample_submitted_state(run_id="run-seq-only").model_copy(
        update={
            "context": sequence_context,
            "idempotency_key": "d" * 64,
        },
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=sequence_state.run_id,
            state=sequence_state,
            event_id="evt-seq-only",
            event=RunSubmittedEvent(
                run_id=sequence_state.run_id,
                idempotency_key=sequence_state.idempotency_key,
                worktree_key=sequence_state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=tie_time + timedelta(seconds=1),
        )

    code, payload, stderr = _invoke(
        ["integration", "runs", "list", "--kind", "sequence", "--limit", "1", "--output", "json"]
    )
    assert code == 0
    assert len(payload["data"]["items"]) == 1
    assert payload["data"]["items"][0]["runId"] == sequence_state.run_id
    assert payload["data"]["page"]["hasMore"] is False
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in stderr

    code, standalone_page, _stderr = _invoke(
        [
            "integration",
            "runs",
            "list",
            "--kind",
            "standalone",
            "--offset",
            "1",
            "--limit",
            "1",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert standalone_page["data"]["page"]["hasMore"] is True
    assert standalone_page["data"]["items"][0]["runId"] == "run-bbb-standalone"

    code, tie_page, _stderr = _invoke(
        ["integration", "runs", "list", "--offset", "1", "--limit", "2", "--output", "json"]
    )
    assert code == 0
    tie_ids = [item["runId"] for item in tie_page["data"]["items"]]
    assert tie_ids == ["run-aaa-standalone", "run-bbb-standalone"]

    load_calls = 0
    original_load = SqliteSchedulerStore.load_validated_snapshot

    def _counting_load(
        self: SqliteSchedulerStore,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> object:
        nonlocal load_calls
        load_calls += 1
        return original_load(self, conn, run_id)

    with patch.object(SqliteSchedulerStore, "load_validated_snapshot", _counting_load):
        code, bounded, _stderr = _invoke(
            ["integration", "runs", "list", "--limit", "2", "--output", "json"]
        )
    assert code == 0
    assert len(bounded["data"]["items"]) == 2
    assert load_calls == 2


def test_c02_attempt_partitions_iterations_and_active_duration(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    base = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    launch = base + timedelta(seconds=10)
    complete = base + timedelta(seconds=15)
    launch_text = launch.isoformat().replace("+00:00", "Z")
    complete_text = complete.isoformat().replace("+00:00", "Z")
    created_text = base.isoformat().replace("+00:00", "Z")
    with store.begin_immediate() as conn:
        source_event_id = str(
            conn.execute(
                "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence ASC LIMIT 1",
                (run_id,),
            ).fetchone()[0]
        )
        for index, (component, iteration, status, launch_at, complete_at) in enumerate(
            (
                ("cursor", 1, "failed", None, None),
                ("cursor", 1, "completed", launch_text, complete_text),
                ("codex", 2, "active", launch_text, None),
            ),
            start=1,
        ):
            dispatch_id = f"dispatch-part-{index:02d}"
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    source_event_id,
                    index,
                    run_id,
                    dispatch_id,
                    f"{run_id}:{dispatch_id}",
                    f"{component}.turn",
                    "d" * 64,
                    created_text,
                    created_text,
                    created_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt-part-{index:02d}",
                    run_id,
                    dispatch_id,
                    component,
                    iteration,
                    status,
                    created_text,
                    created_text,
                    launch_at,
                    complete_at,
                ),
            )
    code, payload, _stderr = _invoke(["integration", "run", "attempts", run_id, "--output", "json"])
    assert code == 0
    items = payload["data"]["items"]
    assert [item["phaseAttempt"] for item in items[:2]] == [1, 2]
    assert items[0]["observedDurationSeconds"] is None
    assert items[1]["observedDurationSeconds"] == 5.0
    assert items[2]["iteration"] == 2
    assert items[2]["observedDurationSeconds"] is None


def test_c02_history_pagination_capacity_waits_vs_attempt_durations(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    base = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
    created_text = base.isoformat().replace("+00:00", "Z")
    wait_until = (base + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    secret_payload = {
        "wait_until": wait_until,
        "review_iteration": 1,
        "evidence_source": "capacity_probe",
        "operator_note": SECRET_SENTINEL,
    }
    with store.begin_immediate() as conn:
        source_event_id = str(
            conn.execute(
                "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence ASC LIMIT 1",
                (run_id,),
            ).fetchone()[0]
        )
        _insert_history_event(
            conn,
            run_id=run_id,
            sequence=2,
            event_kind="codex_usage_capacity_detected",
            payload=secret_payload,
            created_at=created_text,
        )
        _insert_history_event(
            conn,
            run_id=run_id,
            sequence=3,
            event_kind="cursor_usage_limit_detected",
            payload={"wait_until": wait_until, "operator_note": SECRET_SENTINEL},
            created_at=created_text,
        )
        dispatch_id = "dispatch-wait-01"
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, 'cursor.turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
            """,
            (
                dispatch_id,
                source_event_id,
                run_id,
                dispatch_id,
                f"{run_id}:{dispatch_id}",
                "e" * 64,
                created_text,
                created_text,
                created_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at, launch_requested_at, completed_at
            ) VALUES (?, ?, ?, 'cursor', 1, 'active', ?, ?, ?, NULL)
            """,
            (
                "attempt-wait-01",
                run_id,
                dispatch_id,
                created_text,
                created_text,
                created_text,
            ),
        )

    code, page0, stderr0 = _invoke(
        [
            "integration",
            "run",
            "history",
            run_id,
            "--offset",
            "0",
            "--limit",
            "1",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert page0["data"]["page"]["hasMore"] is True
    assert page0["data"]["items"][0]["sequence"] == 1
    assert page0["data"]["items"][0]["kind"] == "run_submitted"

    code, page1, stderr1 = _invoke(
        [
            "integration",
            "run",
            "history",
            run_id,
            "--offset",
            "1",
            "--limit",
            "1",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert page1["data"]["items"][0]["kind"] == "codex_usage_capacity_detected"
    assert page1["data"]["items"][0]["safeDetail"] == (
        "codex_usage_capacity_detected: review_iteration=1 evidence_source=capacity_probe"
    )

    code, page2, stderr2 = _invoke(
        [
            "integration",
            "run",
            "history",
            run_id,
            "--offset",
            "2",
            "--limit",
            "2",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert page2["data"]["items"][0]["safeDetail"] == (
        f"cursor_usage_limit_detected: wait_until={wait_until}"
    )
    full_stdout = runner.invoke(
        app,
        ["integration", "run", "history", run_id, "--output", "json"],
    ).stdout
    for blob in (json.dumps(page0), json.dumps(page1), json.dumps(page2), full_stdout):
        assert SECRET_SENTINEL not in blob
    for err in (stderr0, stderr1, stderr2):
        assert SECRET_SENTINEL not in err

    code, attempts_payload, _stderr = _invoke(
        ["integration", "run", "attempts", run_id, "--output", "json"]
    )
    assert code == 0
    wait_attempt = next(
        item for item in attempts_payload["data"]["items"] if item["attemptId"] == "attempt-wait-01"
    )
    assert wait_attempt["observedDurationSeconds"] is None
    assert wait_attempt["completedAt"] is None
    assert wait_attempt["launchRequestedAt"] is not None


def test_c03_multichunk_unicode_prompt_eof_and_integrity_errors(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    rel_prompt = Path("docs/plans/unicode-prompt.txt")
    prompt_path = git_repo / rel_prompt
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_bytes = "0123456789 café".encode("utf-8")
    accent_index = prompt_bytes.index(b"\xc3")
    assert prompt_bytes[accent_index : accent_index + 2] == b"\xc3\xa9"
    prompt_path.write_bytes(prompt_bytes)
    prompt = prompt_bytes.decode("utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=rel_prompt,
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(options).run_id
    prompt_path.unlink()

    code, first_chunk, _stderr = _invoke(
        [
            "integration",
            "run",
            "initial-prompt",
            run_id,
            "--offset",
            "0",
            "--limit",
            str(accent_index + 1),
            "--output",
            "json",
        ]
    )
    assert code == 0
    first = first_chunk["data"]
    validate_integration_instance(first, FROZEN_ARTIFACT_CHUNK_SCHEMA)
    first_bytes = base64.b64decode(first["contentBase64"])
    assert first_bytes == prompt_bytes[: accent_index + 1]
    assert first_bytes.endswith(b"\xc3")
    assert first["hasMore"] is True
    assert first["nextOffset"] == accent_index + 1

    code, second_chunk, _stderr = _invoke(
        [
            "integration",
            "run",
            "initial-prompt",
            run_id,
            "--offset",
            str(accent_index + 1),
            "--limit",
            "8",
            "--output",
            "json",
        ]
    )
    assert code == 0
    second = second_chunk["data"]
    second_bytes = base64.b64decode(second["contentBase64"])
    assert second_bytes == prompt_bytes[accent_index + 1 :]
    assert second_bytes.startswith(b"\xa9")
    assert first_bytes + second_bytes == prompt_bytes

    collected = b""
    offset = 0
    while True:
        code, payload, _stderr = _invoke(
            [
                "integration",
                "run",
                "initial-prompt",
                run_id,
                "--offset",
                str(offset),
                "--limit",
                "5",
                "--output",
                "json",
            ]
        )
        assert code == 0
        chunk = payload["data"]
        validate_integration_instance(chunk, FROZEN_ARTIFACT_CHUNK_SCHEMA)
        collected += base64.b64decode(chunk["contentBase64"])
        if not chunk["hasMore"]:
            assert chunk["nextOffset"] is None
            break
        assert chunk["nextOffset"] is not None
        offset = int(chunk["nextOffset"])
    assert collected == prompt_bytes

    code, eof_payload, _stderr = _invoke(
        [
            "integration",
            "run",
            "initial-prompt",
            run_id,
            "--offset",
            str(len(prompt_bytes)),
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert eof_payload["data"]["returnedBytes"] == 0
    assert eof_payload["data"]["hasMore"] is False

    code, bad_offset, _stderr = _invoke(
        [
            "integration",
            "run",
            "initial-prompt",
            run_id,
            "--offset",
            str(len(prompt_bytes) + 1),
            "--output",
            "json",
        ]
    )
    assert code == 2
    assert bad_offset["error"]["code"] == "INVALID_ARGUMENT"

    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    plan_file = run_root / "plan" / "plan.md"
    plan_file.write_bytes(plan_file.read_bytes() + b"tamper")

    code, bad_hash, _stderr = _invoke(["integration", "run", "plan", run_id, "--output", "json"])
    assert code == 5
    assert bad_hash["error"]["code"] == "DATA_INTEGRITY"

    plan_file.unlink()
    code, missing, _stderr = _invoke(["integration", "run", "plan", run_id, "--output", "json"])
    assert code == 5
    assert missing["error"]["code"] == "DATA_INTEGRITY"


def test_c03_cli_collection_and_artifact_read_bounds(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    code, bad_collection, _stderr = _invoke(
        [
            "integration",
            "runs",
            "list",
            "--limit",
            str(COLLECTION_HARD_MAX_LIMIT + 1),
            "--output",
            "json",
        ]
    )
    assert code == 2
    assert bad_collection["error"]["code"] == "INVALID_ARGUMENT"

    code, bad_offset, _stderr = _invoke(
        [
            "integration",
            "run",
            "history",
            run_id,
            "--offset",
            "-1",
            "--output",
            "json",
        ]
    )
    assert code == 2
    assert bad_offset["error"]["code"] == "INVALID_ARGUMENT"

    code, bad_byte_limit, _stderr = _invoke(
        [
            "integration",
            "run",
            "plan",
            run_id,
            "--limit",
            str(ARTIFACT_HARD_MAX_LIMIT + 1),
            "--output",
            "json",
        ]
    )
    assert code == 2
    assert bad_byte_limit["error"]["code"] == "INVALID_ARGUMENT"


def test_f01_run_root_symlink_rejected_for_plan_and_initial_prompt_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    outside = tmp_path / "outside-run"
    shutil.move(str(run_root), outside)
    run_root.symlink_to(outside, target_is_directory=True)
    before_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])

    for command in ("plan", "initial-prompt"):
        code, payload, _stderr = _invoke(
            ["integration", "run", command, run_id, "--output", "json"]
        )
        assert code == 5
        assert payload["error"]["code"] == "DATA_INTEGRITY"
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree


def test_f01_leaf_symlink_rejected_with_real_run_directory(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    outside = tmp_path / "outside-run"
    shutil.move(str(run_root), outside)
    run_root.mkdir()
    shutil.copytree(outside / "plan", run_root / "plan", dirs_exist_ok=True)
    shutil.copytree(outside / "prompts", run_root / "prompts", dirs_exist_ok=True)
    for name in ("effective-config.yaml", "source-config.yaml"):
        src = outside / name
        if src.exists():
            shutil.copy2(src, run_root / name)

    plan_path = run_root / "plan" / "plan.md"
    original_bytes = plan_path.read_bytes()
    external = tmp_path / "external-plan.md"
    external.write_bytes(original_bytes)
    plan_path.unlink()
    plan_path.symlink_to(external)
    before_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])
    code, payload, _stderr = _invoke(["integration", "run", "plan", run_id, "--output", "json"])
    assert code == 5
    assert payload["error"]["code"] == "DATA_INTEGRITY"
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree

    prompt_path = run_root / "prompts" / "cursor-initial.txt"
    prompt_bytes = prompt_path.read_bytes()
    external_prompt = tmp_path / "external-prompt.txt"
    external_prompt.write_bytes(prompt_bytes)
    prompt_path.unlink()
    prompt_path.symlink_to(external_prompt)
    before_prompt_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])
    code, payload, _stderr = _invoke(
        ["integration", "run", "initial-prompt", run_id, "--output", "json"]
    )
    assert code == 5
    assert payload["error"]["code"] == "DATA_INTEGRITY"
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_prompt_tree


def test_f01_runs_container_symlink_rejected_for_plan_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    runs_dir = scheduler_paths["artifact_root"] / "runs"
    outside_runs = tmp_path / "runs-outside"
    shutil.move(str(runs_dir), outside_runs)
    runs_dir.symlink_to(outside_runs, target_is_directory=True)
    before_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])
    code, payload, _stderr = _invoke(["integration", "run", "plan", run_id, "--output", "json"])
    assert code == 5
    assert payload["error"]["code"] == "DATA_INTEGRITY"
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree


def test_c04_error_inspect_preserves_ledger_and_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit_run(git_repo, scheduler_paths)
    before_ledger = _logical_ledger_snapshot(scheduler_paths["db_path"])
    before_tree = _artifact_tree_snapshot(scheduler_paths["artifact_root"])
    code, payload, stderr = _invoke(
        ["integration", "run", "inspect", "missing-run", "--output", "json"]
    )
    assert code == 3
    assert payload["error"]["code"] == "NOT_FOUND"
    assert SECRET_SENTINEL not in stderr
    assert _logical_ledger_snapshot(scheduler_paths["db_path"]) == before_ledger
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree

    code, bad_plan, bad_stderr = _invoke(
        ["integration", "run", "plan", run_id, "--offset", "999999", "--output", "json"]
    )
    assert code == 2
    assert bad_plan["error"]["code"] == "INVALID_ARGUMENT"
    assert SECRET_SENTINEL not in bad_stderr
    assert _logical_ledger_snapshot(scheduler_paths["db_path"]) == before_ledger
    assert _artifact_tree_snapshot(scheduler_paths["artifact_root"]) == before_tree


def test_c05_v4_populated_run_inspect_via_cli(
    tmp_path: Path,
    isolated_xdg: Path,
    git_repo: Path,
) -> None:
    from ai_dev_loop.scheduler.application.submission import SubmissionService
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget

    state_root = isolated_xdg / "state" / "ai_dev_loop"
    state_root.mkdir(parents=True)
    artifact_root = state_root / "artifacts"
    db = _pause_v4_database(tmp_path)
    shutil.copy(db, state_root / "engine.sqlite3")
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=state_root / "engine.sqlite3",
        artifact_root=artifact_root,
    )
    store = SqliteSchedulerStore(state_root / "engine.sqlite3", bootstrap=False)
    service = SubmissionService(
        store,
        ProtectedArtifactStore(artifact_root),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
    )
    with patch("sys.stdin", StringIO(prompt)):
        run_id = service.submit(options).run_id
    version = int(
        sqlite3.connect(state_root / "engine.sqlite3").execute("PRAGMA user_version").fetchone()[0]
    )
    assert version == 4
    before_ledger = _logical_ledger_snapshot(state_root / "engine.sqlite3")
    before_tree = _artifact_tree_snapshot(artifact_root)
    code, list_payload, list_stderr = _invoke(["integration", "runs", "list", "--output", "json"])
    assert code == 0
    assert len(list_payload["data"]["items"]) == 1
    assert list_payload["data"]["items"][0]["runId"] == run_id
    assert SECRET_SENTINEL not in json.dumps(list_payload)
    assert SECRET_SENTINEL not in list_stderr

    code, payload, inspect_stderr = _invoke(
        ["integration", "run", "inspect", run_id, "--output", "json"]
    )
    assert code == 0
    validate_integration_instance(payload["data"], RUN_INSPECT_DATA_SCHEMA)
    assert payload["data"]["runId"] == run_id
    assert SECRET_SENTINEL not in json.dumps(payload)
    assert SECRET_SENTINEL not in inspect_stderr

    code, history_payload, history_stderr = _invoke(
        ["integration", "run", "history", run_id, "--output", "json"]
    )
    assert code == 0
    validate_integration_instance(history_payload["data"], HISTORY_LIST_DATA_SCHEMA)
    assert SECRET_SENTINEL not in json.dumps(history_payload)
    assert SECRET_SENTINEL not in history_stderr

    assert _logical_ledger_snapshot(state_root / "engine.sqlite3") == before_ledger
    assert _artifact_tree_snapshot(artifact_root) == before_tree
    version_after = int(
        sqlite3.connect(state_root / "engine.sqlite3").execute("PRAGMA user_version").fetchone()[0]
    )
    assert version_after == 4
