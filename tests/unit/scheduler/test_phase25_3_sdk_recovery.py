"""R-01/R-02 production worker tests for exact Cursor SDK reconciliation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkAgentIdentity,
    CursorSdkBridgeHealth,
    CursorSdkEnvelope,
    CursorSdkOutcome,
    CursorSdkPreflight,
    CursorSdkRunIdentity,
    CursorSdkRunPage,
    CursorSdkTerminalRun,
)
from ai_dev_loop.paths import cursor_conversation_key, schema_path
from ai_dev_loop.scheduler.application.cursor_sdk_journal import append_sdk_journal
from ai_dev_loop.scheduler.cursor_attempt_runner import _run_sdk_reconciliation_operation
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    cursor_sdk_attempt_journal_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkReconciliationRecordV1,
    CursorSdkSuccessorIntentV1,
    CursorSdkSuccessorRecordV1,
)
from ai_dev_loop.scheduler.domain.state import CursorWorkflowCheckpoint
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore
from ai_dev_loop.state import sha256_file

AGENT = "019abc00-1111-4222-8333-444444444444"
NATIVE = "run-019abc00-1111-4222-8333-555555555555"


class RecoveryAdapter:
    def __init__(self, repo: Path, *, initial: str = "running", final: str = "cancelled"):
        self.repo = repo
        self.initial = initial
        self.final = final
        self.cancelled = False
        self.cancel_calls: list[str] = []
        self.send_calls = 0

    def open(self):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(CursorSdkBridgeHealth("1.0.37", "fake", "v1"))

    def preflight(self):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(
            CursorSdkPreflight(
                CursorSdkBridgeHealth("1.0.37", "fake", "v1"),
                True,
                "grok-4.7",
                (),
            )
        )

    def resume_agent(self, agent_id: str):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(CursorSdkAgentIdentity(agent_id))

    def list_runs(self, agent_id: str, *, cursor=None, limit=None):  # type: ignore[no-untyped-def]
        del cursor, limit
        return CursorSdkOutcome.success(
            CursorSdkRunPage((self._terminal(NATIVE, self.initial),), None)
        )

    def get_run(self, agent_id: str, run_id: str):  # type: ignore[no-untyped-def]
        assert agent_id == AGENT and run_id == NATIVE
        status = self.final if self.cancelled else self.initial
        return CursorSdkOutcome.success(self._terminal(run_id, status))

    def cancel(self, agent_id: str, run_id: str):  # type: ignore[no-untyped-def]
        assert agent_id == AGENT
        self.cancelled = True
        self.cancel_calls.append(run_id)
        return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id, run_id))

    def stream(self, agent_id: str, run_id: str, *, on_envelope=None, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("mode") == "replay"
        if on_envelope:
            on_envelope(CursorSdkEnvelope("1", {"kind": "result"}))
        return CursorSdkOutcome.success(self._terminal(run_id, "finished"))

    def send(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.send_calls += 1
        raise AssertionError("reconciliation must never send")

    def close(self) -> None:
        pass

    @staticmethod
    def _terminal(run_id: str, status: str) -> CursorSdkTerminalRun:
        return CursorSdkTerminalRun(
            AGENT,
            run_id,
            status,
            "recovered terminal" if status == "finished" else "",
            None,
        )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def _evidence(tmp_path: Path, *, native: str | None = NATIVE) -> tuple[dict[str, object], Path]:
    repo = _repo(tmp_path)
    source_attempt = "att-" + "1" * 32
    source_dispatch = "fx-source"
    run_id = "sdk-recovery-run"
    root = ProtectedArtifactStore(tmp_path / "artifacts").run_root(run_id)
    prompt = root / "prompts/turn.txt"
    prompt.parent.mkdir()
    prompt.write_text("continue exactly")
    prompt_sha = hashlib.sha256(prompt.read_bytes()).hexdigest()
    journal_rel = cursor_sdk_attempt_journal_rel(source_attempt)
    journal = root / journal_rel
    base = {
        "attempt_id": source_attempt,
        "dispatch_id": source_dispatch,
        "conversation_owner_run_id": run_id,
        "conversation_key": cursor_conversation_key(run_id),
        "agent_id": AGENT,
        "prompt_sha256": prompt_sha,
    }
    append_sdk_journal(journal, kind="run_inventory_baseline", payload={**base, "run_ids": []})
    append_sdk_journal(journal, kind="send_intent", payload=base)
    if native:
        append_sdk_journal(
            journal, kind="native_run_bound", payload={**base, "native_run_id": native}
        )
    append_sdk_journal(
        journal,
        kind="terminal",
        payload={
            **base,
            **({"native_run_id": native} if native else {}),
            "outcome": "uncertain",
            "failure_kind": "cursor_sdk_worker_lost",
        },
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return (
        {
            "schema_version": 2,
            "runtime": "sdk-local",
            "effect_kind": RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
            "run_id": run_id,
            "attempt_id": "att-" + "2" * 32,
            "dispatch_id": "fx-reconcile",
            "repository_root": str(repo),
            "repository_git_common_dir": str(repo / ".git"),
            "repository_git_dir": str(repo / ".git"),
            "repository_branch": "master",
            "repository_initial_head": head,
            "cursor_model": "grok-4.7",
            "cursor_sandbox": "enabled",
            "sdk_version": "1.0.37",
            "sdk_parameters": {},
            "sdk_credential_ref": "cursor-test",
            "sdk_setting_sources": ["project"],
            "sdk_tool_policy": "scheduler-owned-no-shell",
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "agent_id": AGENT,
            "iteration": 1,
            "prompt_path": "prompts/turn.txt",
            "prompt_sha256": prompt_sha,
            "source_attempt_id": source_attempt,
            "source_dispatch_id": source_dispatch,
            "source_journal_path": journal_rel,
            "source_journal_sha256": sha256_file(journal),
            "native_run_id": native,
            "recovery_key": "a" * 64,
            "timeout_seconds": 60,
        },
        root,
    )


def test_exact_running_native_run_is_cancelled_and_confirmed_before_retry(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    evidence, root = _evidence(tmp_path)
    adapter = RecoveryAdapter(Path(str(evidence["repository_root"])))
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda value: adapter
    )

    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )

    assert outcome["reconciliation_outcome"] == "continuation_ready"
    assert adapter.cancel_calls == [NATIVE]
    assert adapter.send_calls == 0
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        (root / str(outcome["reconciliation_record_path"])).read_bytes()
    )
    assert record.native_run_id == NATIVE
    assert record.cancellation_requested is True
    assert record.final_native_status == "cancelled"


def test_finished_native_run_is_replayed_without_a_second_send(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    evidence, root = _evidence(tmp_path)
    adapter = RecoveryAdapter(
        Path(str(evidence["repository_root"])), initial="finished", final="finished"
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda value: adapter
    )

    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )

    assert outcome["reconciliation_outcome"] == "terminal_recovered"
    assert adapter.cancel_calls == [] and adapter.send_calls == 0
    assert outcome["final_response_sha256"]
    assert outcome["cursor_output_fingerprint_sha256"]


def test_phase25_3_persisted_models_match_published_json_schemas() -> None:
    import jsonschema

    models = (
        (CursorSdkReconciliationIntentV1, "scheduler-cursor-sdk-reconciliation-intent-v1.json"),
        (CursorSdkReconciliationRecordV1, "scheduler-cursor-sdk-reconciliation-record-v1.json"),
        (CursorSdkSuccessorIntentV1, "scheduler-cursor-sdk-successor-intent-v1.json"),
        (CursorSdkSuccessorRecordV1, "scheduler-cursor-sdk-successor-record-v1.json"),
        (CursorWorkflowCheckpoint, "scheduler-cursor-workflow-checkpoint-v1.json"),
    )
    for model, name in models:
        published = json.loads(schema_path(name).read_text(encoding="utf-8"))
        published.pop("$schema", None)
        assert published == model.model_json_schema()
        jsonschema.Draft202012Validator.check_schema(published)


def _downgrade_fixture_to_schema17(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE scheduler_cursor_sdk_reconciliations")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 18")
        conn.execute("PRAGMA user_version = 17")
        conn.commit()


def test_schema18_migration_adds_sdk_reconciliation_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(db_path)
    _downgrade_fixture_to_schema17(db_path)

    store = SqliteSchedulerStore(db_path)

    assert SCHEMA_VERSION == 18
    with store.begin_read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='scheduler_cursor_sdk_reconciliations'"
            ).fetchone()[0]
            == 1
        )
        migration = conn.execute(
            "SELECT name FROM scheduler_schema_migrations WHERE version=18"
        ).fetchone()
        assert migration is not None and migration["name"] == "0018_cursor_sdk_reconciliation"


def test_schema18_migration_fault_rolls_back_table_and_audit_row(tmp_path: Path) -> None:
    db_path = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(db_path)
    _downgrade_fixture_to_schema17(db_path)

    def fail_after_table(statement: str) -> None:
        if statement.lstrip().startswith(
            "CREATE INDEX idx_scheduler_cursor_sdk_reconciliations_run"
        ):
            raise RuntimeError("injected SDK reconciliation migration fault")

    with pytest.raises(RuntimeError, match="injected SDK reconciliation migration fault"):
        SqliteSchedulerStore(db_path, migration_fault_hook=fail_after_table)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_schema_migrations WHERE version=18"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='scheduler_cursor_sdk_reconciliations'"
            ).fetchone()[0]
            == 0
        )
