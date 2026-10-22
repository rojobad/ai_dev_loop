"""Phase 25.7 C lifecycle: conversation disposal and seven-day retention."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ai_dev_loop.scheduler.application.cursor_sdk_retention as sdk_retention
from ai_dev_loop.cli import app
from ai_dev_loop.paths import CursorSdkPathError, cursor_conversation_key
from ai_dev_loop.scheduler.application.cursor_sdk_retention import (
    apply_cursor_sdk_cleanup,
    load_cursor_sdk_retention_policy,
    maintain_cursor_sdk_conversations,
    open_dispatch_store,
    preview_cursor_sdk_cleanup,
    set_cursor_sdk_retention_enabled,
    try_dispatch_open,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import RUN_CURSOR_TURN_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkSuccessorIntentV1,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_retention import (
    CURSOR_SDK_RETENTION_DECISION_AT,
    CURSOR_SDK_RETENTION_DECISION_SOURCE,
    CURSOR_SDK_RETENTION_HOURS,
    CURSOR_STORE_CLEANUP_CONFIRMATION,
    CursorSdkDisposalIntentV1,
    CursorSdkDisposalReceiptV1,
    CursorSdkRetentionPolicyV1,
)
from ai_dev_loop.scheduler.domain.events import CursorSdkReconciledEvent
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FINISHED = datetime(2026, 1, 1, tzinfo=UTC)
EXACT = FINISHED + timedelta(hours=168)
BEFORE = EXACT - timedelta(microseconds=1)
FAR = EXACT + timedelta(days=400)
STAMP = encode_utc_instant(FINISHED)


def test_phase25_7_persisted_models_match_published_json_schemas() -> None:
    import jsonschema

    from ai_dev_loop.paths import schema_path

    models = (
        (CursorSdkRetentionPolicyV1, "scheduler-cursor-sdk-retention-policy-v1.json"),
        (CursorSdkDisposalIntentV1, "scheduler-cursor-sdk-disposal-intent-v1.json"),
        (CursorSdkDisposalReceiptV1, "scheduler-cursor-sdk-disposal-receipt-v1.json"),
    )
    for model, name in models:
        published = json.loads(schema_path(name).read_text(encoding="utf-8"))
        published.pop("$schema", None)
        assert published == model.model_json_schema()
        jsonschema.Draft202012Validator.check_schema(published)


def test_preview_is_read_only_and_does_not_import_the_sdk_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)
    before_tree = _tree(state_root)
    before_rows = _disposal_count(store)
    policy_before = load_cursor_sdk_retention_policy(store)
    real_import = __import__

    def guarded(name: str, *args: object, **kwargs: object) -> object:
        if name == "cursor_sdk" or name.startswith("cursor_sdk."):
            raise AssertionError("preview imported the SDK bridge")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded)
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert preview.policy is not None
    assert preview.policy.retention_hours == 168
    assert preview.policy.decision_source == "operator_2026-10-20"
    assert preview.conversations[0].conversation_key == key
    assert preview.conversations[0].manual_eligible is True
    assert preview.conversations[0].automatic_eligible is True
    assert _tree(state_root) == before_tree
    assert _disposal_count(store) == before_rows
    assert load_cursor_sdk_retention_policy(store) == policy_before
    assert root.is_dir()
    assert owner


def test_manual_apply_deletes_one_root_and_preserves_protected_data(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)
    sibling = _seed_root(state_root, "b" * 64)
    protected = _protected_paths(state_root, tmp_path)
    result = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=FAR,
        mode="manual",
        conversation_key=key,
    )
    assert result.action == "deleted"
    assert result.deleted is True
    assert result.conversation_key == key
    assert not root.exists()
    assert sibling.is_dir()
    assert (state_root / "engine.sqlite3").is_file()
    for path, payload in protected.items():
        assert path.read_bytes() == payload
    receipt = _receipt_row(store, key)
    assert receipt["status"] == "deleted"
    assert receipt["receipt_id"] == result.receipt_id
    tombstone = state_root / "cursor-sdk-disposals" / f"{key}.json"
    assert tombstone.is_file()
    assert stat_mode(tombstone) == 0o600
    assert stat_mode(tombstone.parent) == 0o700
    body = json.loads(tombstone.read_text(encoding="utf-8"))
    assert body["receipt_sha256"] == receipt["receipt_sha256"]
    repeated = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=FAR,
        mode="manual",
        conversation_key=key,
    )
    assert repeated.action == "already_deleted"
    assert repeated.receipt_id == result.receipt_id
    assert sibling.is_dir()


def test_shared_successor_capacity_budget_and_holds_are_preserved(tmp_path: Path) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path, run_id="run-source")
    root = _seed_root(state_root, key)
    _insert_run(store, run_id="run-successor", owner=owner, key=key, kind="blocked", stamp=STAMP)
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, _owner, key = _finished_conversation(
        tmp_path / "capacity-kind", kind="waiting_codex_capacity"
    )
    root = _seed_root(state_root, key)
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, owner, key = _finished_conversation(tmp_path / "capacity-holder")
    root = _seed_root(state_root, key)
    with store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_capacity SET holder_run_id = ? WHERE capacity_name = 'global_active_agent'",
            (owner,),
        )
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, _owner, key = _finished_conversation(
        tmp_path / "budget", kind="max_iterations_reached"
    )
    root = _seed_root(state_root, key)
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, owner, key = _finished_conversation(tmp_path / "abort", kind="aborted")
    root = _seed_root(state_root, key)
    _insert_attempt(store, owner, status="uncertain")
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, owner, key = _finished_conversation(tmp_path / "handoff")
    root = _seed_root(state_root, key)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_checkpoint_holds(
                run_id, intent_sha256, hold_reason, ref_may_have_advanced, created_at, updated_at
            ) VALUES (?, ?, 'checkpoint', 0, ?, ?)
            """,
            (owner, "c" * 64, STAMP, STAMP),
        )
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()

    store, state_root, owner, key = _finished_conversation(tmp_path / "recovery")
    root = _seed_root(state_root, key)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_cursor_initial_recoveries(
                recovery_key, source_run_id, failed_attempt_id, successor_run_id, dispatch_id,
                status, intent_payload, intent_payload_sha256, created_at, updated_at
            ) VALUES ('recovery-1', ?, 'attempt-1', 'successor-not-adopted', 'dispatch-1',
                      'pending', '{}', ?, ?, ?)
            """,
            (owner, "d" * 64, STAMP, STAMP),
        )
    result = _manual(store, state_root, key)
    assert result.action == "ineligible"
    assert "recovery_publication" in result.reasons
    assert root.is_dir()


def test_symlink_escape_and_unowned_roots_are_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path / "child")
    root = _seed_root(state_root, key)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("keep", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert result.observed == "symlink_rejected"
    assert secret.read_text(encoding="utf-8") == "keep"
    assert root.is_dir()
    assert _receipt_row(store, key) is None

    store, state_root, _owner, key = _finished_conversation(tmp_path / "root-link")
    real = tmp_path / "real-store"
    real.mkdir()
    (real / "index.db").write_bytes(b"foreign")
    sdk = state_root / "cursor-sdk"
    sdk.mkdir()
    (sdk / key).symlink_to(real)
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert (real / "index.db").read_bytes() == b"foreign"
    assert _receipt_row(store, key) is None

    store, state_root, _owner, key = _finished_conversation(tmp_path / "unowned")
    root = _seed_root(state_root, key)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.cursor_sdk_retention.os.getuid",
        lambda: 1,
    )
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert result.observed == "unowned_root"
    assert (root / "index.db").is_file()
    assert _receipt_row(store, key) is None


def test_missing_root_records_an_absent_receipt_without_touching_siblings(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    sibling = _seed_root(state_root, "e" * 64)
    result = _manual(store, state_root, key)
    assert result.action == "already_absent"
    assert result.deleted is False
    assert _receipt_row(store, key)["status"] == "deleted"
    assert sibling.is_dir()
    assert (state_root / "engine.sqlite3").is_file()


def test_cleanup_and_dispatch_serialize_on_the_conversation_fence(tmp_path: Path) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path / "cleanup-wins")
    root = _seed_root(state_root, key)
    intent_ready = threading.Event()
    dispatch_done = threading.Event()
    observed: dict[str, object] = {}

    def hook() -> None:
        intent_ready.set()
        assert dispatch_done.wait(5)

    def dispatch() -> None:
        assert intent_ready.wait(5)

        def opener() -> str:
            observed["opened"] = True
            return "opened"

        observed["result"] = try_dispatch_open(
            store,
            conversation_key=key,
            conversation_owner_run_id=owner,
            now=FAR,
            opener=opener,
        )
        dispatch_done.set()

    thread = threading.Thread(target=dispatch)
    thread.start()
    result = _manual(store, state_root, key, after_intent_hook=hook)
    thread.join(5)
    assert not thread.is_alive()
    dispatch_result = observed["result"]
    assert result.action == "deleted"
    assert dispatch_result.observed == "disposal_intent"
    assert dispatch_result.value is None
    assert "opened" not in observed
    assert not root.exists()

    store, state_root, owner, key = _finished_conversation(tmp_path / "dispatch-wins")
    root = state_root / "cursor-sdk" / key
    fence_held = threading.Event()
    cleanup_done = threading.Event()
    cleanup: dict[str, object] = {}

    def hold() -> None:
        fence_held.set()
        assert cleanup_done.wait(5)

    def cleaner() -> None:
        assert fence_held.wait(5)
        cleanup["result"] = _manual(store, state_root, key)
        cleanup_done.set()

    thread = threading.Thread(target=cleaner)
    thread.start()

    def opener() -> str:
        root.mkdir(parents=True)
        (root / "index.db").write_bytes(b"dispatch")
        return "opened"

    opened = try_dispatch_open(
        store,
        conversation_key=key,
        conversation_owner_run_id=owner,
        now=FAR,
        opener=opener,
        hold_hook=hold,
    )
    thread.join(5)
    assert not thread.is_alive()
    assert opened.observed == "opened"
    assert cleanup["result"].action == "fence_busy"
    assert (root / "index.db").read_bytes() == b"dispatch"
    assert _receipt_row(store, key) is None


def test_crash_after_intent_converges_on_the_same_receipt(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)

    def crash() -> None:
        (root / "index.db").unlink()
        raise RuntimeError("interrupted after intent")

    with pytest.raises(RuntimeError, match="interrupted after intent"):
        _manual(store, state_root, key, after_intent_hook=crash)
    intent = _receipt_row(store, key)
    assert intent is not None
    assert intent["status"] == "intent"
    assert (root / "blobs" / "a.bin").is_file()
    resumed = _manual(store, state_root, key)
    assert resumed.action == "deleted"
    assert resumed.receipt_id == intent["receipt_id"]
    assert not root.exists()
    assert _receipt_row(store, key)["status"] == "deleted"


def test_unrelated_codex_work_does_not_block_cursor_cleanup(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)
    codex_id = "run-codex"
    text, digest = _dump(
        {"run_id": codex_id, "kind": "waiting_codex_capacity", "updated_at": STAMP}
    )
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, 'waiting_codex_capacity', ?, ?, 1, ?, ?, ?, ?)
            """,
            (codex_id, text, digest, "f" * 64, "a" * 64, STAMP, STAMP),
        )
        conn.execute(
            """
            INSERT INTO scheduler_repository_reservations(
                worktree_key, run_id, repository_root, status, created_at, updated_at
            ) VALUES (?, ?, '/tmp/codex-repo', 'active', ?, ?)
            """,
            ("a" * 64, codex_id, STAMP, STAMP),
        )
    _insert_attempt(store, codex_id, status="active", component="codex")
    result = _manual(store, state_root, key)
    assert result.action == "deleted"
    assert not root.exists()


def test_seven_day_boundary_uses_completion_not_mtime_or_sql_column(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path / "boundary")
    root = _seed_root(state_root, key)
    old = (datetime(2020, 1, 1, tzinfo=UTC).timestamp(),) * 2
    os.utime(root, old)
    os.utime(root / "index.db", old)
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=BEFORE).deleted_keys
        == ()
    )
    assert root.is_dir()
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT)
    assert deleted.deleted_keys == (key,)
    assert not root.exists()

    store, state_root, _owner, key = _finished_conversation(tmp_path / "young-payload")
    root = _seed_root(state_root, key)
    os.utime(root, old)
    with store.begin_immediate() as conn:
        conn.execute("UPDATE scheduler_runs SET updated_at = ?", ("2020-01-01T00:00:00.000000Z",))
    recent = encode_utc_instant(EXACT)
    _rewrite_stamp(store, updated_at=recent)
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT).deleted_keys
        == ()
    )
    assert root.is_dir()

    store, state_root, owner, key = _finished_conversation(
        tmp_path / "aborted-stamp", kind="aborted"
    )
    root = _seed_root(state_root, key)
    _rewrite_stamp(
        store, updated_at="2020-01-01T00:00:00.000000Z", aborted_at=encode_utc_instant(EXACT)
    )
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT).deleted_keys
        == ()
    )
    _rewrite_stamp(store, updated_at=encode_utc_instant(FAR), aborted_at=STAMP)
    assert maintain_cursor_sdk_conversations(
        store, state_root=state_root, now=EXACT
    ).deleted_keys == (key,)
    assert owner


def test_later_related_work_moves_the_retention_clock(tmp_path: Path) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path, run_id="run-early")
    root = _seed_root(state_root, key)
    later = FINISHED + timedelta(days=3)
    later_stamp = encode_utc_instant(later)
    _insert_run(
        store, run_id="run-later", owner=owner, key=key, kind="completed", stamp=later_stamp
    )
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT).deleted_keys
        == ()
    )
    assert root.is_dir()
    eligible_at = later + timedelta(hours=168)
    assert (
        maintain_cursor_sdk_conversations(
            store, state_root=state_root, now=eligible_at - timedelta(microseconds=1)
        ).deleted_keys
        == ()
    )
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=eligible_at)
    assert deleted.deleted_keys == (key,)


def test_missing_or_damaged_evidence_blocks_regardless_of_age(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path / "missing")
    root = _seed_root(state_root, key)
    _rewrite_stamp(store, updated_at=None)
    result = maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR)
    assert result.deleted_keys == ()
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert "missing_completion_evidence" in preview.conversations[0].manual_reasons
    assert root.is_dir()

    store, state_root, _owner, key = _finished_conversation(tmp_path / "damaged")
    root = _seed_root(state_root, key)
    corrupt, _digest = _dump({"run_id": "run-corrupt", "kind": "completed", "updated_at": STAMP})
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES ('run-corrupt', 'completed', ?, ?, 1, ?, ?, ?, ?)
            """,
            (corrupt, "0" * 64, "1" * 64, "2" * 64, STAMP, STAMP),
        )
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert "ledger_evidence_unavailable" in preview.conversations[0].manual_reasons
    assert root.is_dir()


def test_active_reference_survives_ancient_mtime(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path, kind="waiting_for_cursor_fix")
    root = _seed_root(state_root, key)
    old = (datetime(2020, 1, 1, tzinfo=UTC).timestamp(),) * 2
    os.utime(root, old)
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    assert _manual(store, state_root, key).action == "ineligible"
    assert root.is_dir()


def test_disabled_or_missing_policy_does_not_infer_cleanup(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path / "disabled")
    root = _seed_root(state_root, key)
    set_cursor_sdk_retention_enabled(store, enabled=False, now=FINISHED)
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    assert root.is_dir()
    manual = _manual(store, state_root, key)
    assert manual.action == "deleted"
    assert _receipt_row(store, key)["mode"] == "manual"

    store, state_root, _owner, key = _finished_conversation(tmp_path / "missing-policy")
    root = _seed_root(state_root, key)
    with store.begin_immediate() as conn:
        conn.execute("DELETE FROM scheduler_cursor_sdk_retention_policy")
    assert load_cursor_sdk_retention_policy(store) is None
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    assert root.is_dir()
    assert load_cursor_sdk_retention_policy(store) is None
    restored = set_cursor_sdk_retention_enabled(store, enabled=True, now=FAR)
    assert restored.automatic_cleanup_enabled is True
    assert restored.retention_hours == CURSOR_SDK_RETENTION_HOURS
    assert restored.decision_source == CURSOR_SDK_RETENTION_DECISION_SOURCE
    assert restored.decision_at == CURSOR_SDK_RETENTION_DECISION_AT


def test_repeated_ticks_delete_only_at_the_boundary(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)
    clock = {"now": BEFORE}
    service = TickService(
        store,
        ProtectedArtifactStore(state_root / "artifacts"),
        BoundedGitAdmissionPort(),
        now_factory=lambda: clock["now"],
    )
    early = service.run_once()
    assert early.lease_acquired is True
    assert root.is_dir()
    assert _receipt_row(store, key) is None
    clock["now"] = EXACT
    service.run_once()
    assert not root.exists()
    receipt_id = _receipt_row(store, key)["receipt_id"]
    assert _receipt_row(store, key)["mode"] == "automatic"
    service.run_once()
    assert _receipt_row(store, key)["receipt_id"] == receipt_id
    assert not root.exists()


def test_maintenance_batch_is_bounded_and_historical_jsonl_is_eligible(tmp_path: Path) -> None:
    store = _store(tmp_path / "batch")
    state_root = store.db_path.parent
    keys: list[str] = []
    for index in range(9):
        owner = f"run-batch-{index}"
        key = cursor_conversation_key(owner)
        _insert_run(store, run_id=owner, owner=owner, key=key, kind="completed", stamp=STAMP)
        _seed_root(state_root, key)
        keys.append(key)
    first = maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT)
    assert len(first.deleted_keys) == 8
    remaining = [key for key in keys if (state_root / "cursor-sdk" / key).exists()]
    assert len(remaining) == 1
    second = maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT)
    assert second.deleted_keys == tuple(remaining)

    store = _store(tmp_path / "jsonl")
    state_root = store.db_path.parent
    owner = "run-jsonl"
    key = cursor_conversation_key(owner)
    _insert_run(
        store,
        run_id=owner,
        owner=owner,
        key=key,
        kind="completed",
        stamp=STAMP,
        historical_jsonl=True,
    )
    root = _seed_root(state_root, key)
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT)
    assert deleted.deleted_keys == (key,)
    assert not root.exists()
    assert _receipt_row(store, key)["store_backend"] == "jsonl"


def test_dispatch_observes_tombstone_before_creating_native_state(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = default_engine_db_path()
    assert str(database).startswith(str(isolated_xdg))
    store = SqliteSchedulerStore(database)
    state_root = database.parent
    owner = "run-disposed"
    key = cursor_conversation_key(owner)
    _insert_run(store, run_id=owner, owner=owner, key=key, kind="completed", stamp=STAMP)
    _seed_root(state_root, key)
    apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=FAR,
        mode="manual",
        conversation_key=key,
    )
    opened: list[str] = []

    def opener() -> str:
        opened.append("opened")
        return "opened"

    with pytest.raises(CursorSdkPathError, match="conversation_disposed"):
        open_dispatch_store(
            conversation_key=key,
            conversation_owner_run_id=owner,
            opener=opener,
            now=FAR,
            db_path=database,
        )
    assert opened == []
    assert not (state_root / "cursor-sdk" / key).exists()

    from ai_dev_loop.scheduler import cursor_attempt_runner as runner

    monkeypatch.setattr(
        runner,
        "authenticate_pinned_invocation_evidence",
        lambda *_args, **_kwargs: {"runtime": "sdk-local"},
    )
    monkeypatch.setattr(
        runner, "verify_pre_execution_cursor_guards", lambda *_args, **_kwargs: None
    )

    def disposed(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise CursorSdkPathError("conversation_disposed")

    monkeypatch.setattr(runner, "_run_sdk_turn", disposed)
    artifacts = state_root / "worker-artifacts"
    code = runner.main(
        [
            "--run-id",
            owner,
            "--attempt-id",
            "att-phase257c",
            "--unit-identity",
            "unit-phase257c",
            "--artifact-root",
            str(artifacts),
            "--invocation-evidence-sha256",
            "ab" * 32,
            "--launch-intent-sha256",
            "cd" * 32,
            "--launch-nonce",
            "nonce-phase257c",
            "--dispatch-id",
            "dispatch-phase257c",
            "--effect-kind",
            RUN_CURSOR_TURN_EFFECT_KIND,
        ]
    )
    assert code == 1
    stdout = next(artifacts.rglob("stdout.txt")).read_text(encoding="utf-8")
    payload = json.loads(stdout)
    assert payload["failure_kind"] == "cursor_sdk_preflight_failed"
    assert payload["timed_out"] is False
    assert "native_run_id" not in payload
    stderr = next(artifacts.rglob("stderr.txt")).read_text(encoding="utf-8")
    assert "conversation_disposed" in stderr
    assert "prompt" not in stderr


def test_cli_preview_apply_and_policy_stay_explicit(isolated_xdg: Path) -> None:
    database = default_engine_db_path()
    assert str(database).startswith(str(isolated_xdg))
    store = SqliteSchedulerStore(database)
    state_root = database.parent
    owner = "run-cli"
    key = cursor_conversation_key(owner)
    _insert_run(store, run_id=owner, owner=owner, key=key, kind="completed", stamp=STAMP)
    root = _seed_root(state_root, key)
    runner = CliRunner()
    preview = runner.invoke(
        app, ["scheduler", "cursor-store", "cleanup", "--conversation-key", key]
    )
    assert preview.exit_code == 0
    assert "168" in preview.stdout
    assert "operator_2026-10-20" in preview.stdout
    assert root.is_dir()
    dry = runner.invoke(
        app,
        [
            "scheduler",
            "cursor-store",
            "cleanup",
            "--conversation-key",
            key,
            "--confirm",
            CURSOR_STORE_CLEANUP_CONFIRMATION,
            "--dry-run",
        ],
    )
    assert dry.exit_code == 0
    assert root.is_dir()
    wrong = runner.invoke(
        app,
        [
            "scheduler",
            "cursor-store",
            "cleanup",
            "--conversation-key",
            key,
            "--confirm",
            "delete-legacy-state",
        ],
    )
    assert wrong.exit_code != 0
    assert root.is_dir()
    applied = runner.invoke(
        app,
        [
            "scheduler",
            "cursor-store",
            "cleanup",
            "--owner-run-id",
            owner,
            "--confirm",
            CURSOR_STORE_CLEANUP_CONFIRMATION,
        ],
    )
    assert applied.exit_code == 0
    assert not root.exists()
    policy = runner.invoke(app, ["scheduler", "cursor-store", "policy"])
    assert policy.exit_code == 0
    assert "168" in policy.stdout
    assert "operator_2026-10-20" in policy.stdout
    disabled = runner.invoke(
        app, ["scheduler", "cursor-store", "policy", "--automatic", "disabled"]
    )
    assert disabled.exit_code == 0
    assert "disabled" in disabled.stdout
    stored = load_cursor_sdk_retention_policy(store)
    assert stored is not None
    assert stored.automatic_cleanup_enabled is False
    assert stored.retention_hours == 168
    cutover = runner.invoke(app, ["scheduler", "cutover", "cleanup", "--help"])
    assert cutover.exit_code == 0
    assert "runs/" in cutover.stdout
    assert "pr-review-v2/" in cutover.stdout
    assert "cursor-sdk" not in cutover.stdout


def test_cli_inspection_does_not_create_or_migrate_a_ledger(isolated_xdg: Path) -> None:
    database = default_engine_db_path()
    assert str(database).startswith(str(isolated_xdg))
    parent = database.parent
    assert not parent.exists()
    runner = CliRunner()
    missing_preview = runner.invoke(app, ["scheduler", "cursor-store", "cleanup", "--dry-run"])
    missing_policy = runner.invoke(app, ["scheduler", "cursor-store", "policy"])
    assert missing_preview.exit_code == 0
    assert missing_policy.exit_code == 0
    assert not database.exists()
    assert not parent.exists()

    SqliteSchedulerStore(database)
    with sqlite3.connect(database) as conn:
        for table in (
            "scheduler_cursor_sdk_no_progress",
            "scheduler_cursor_sdk_retention_policy",
            "scheduler_cursor_sdk_conversation_fences",
            "scheduler_cursor_sdk_disposals",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 19")
        conn.execute("PRAGMA user_version = 18")
        conn.commit()
    before = _ledger_snapshot(database, parent)
    assert before["user_version"] == 18
    assert "scheduler_cursor_sdk_retention_policy" not in before["tables"]
    historical_preview = runner.invoke(app, ["scheduler", "cursor-store", "cleanup", "--dry-run"])
    historical_policy = runner.invoke(app, ["scheduler", "cursor-store", "policy"])
    assert historical_preview.exit_code == 0, historical_preview.output
    assert historical_policy.exit_code == 0, historical_policy.output
    assert _ledger_snapshot(database, parent) == before

    SqliteSchedulerStore(database)
    owner = "run-current-preview"
    key = cursor_conversation_key(owner)
    store = SqliteSchedulerStore(database)
    _insert_run(store, run_id=owner, owner=owner, key=key, kind="completed", stamp=STAMP)
    root = _seed_root(parent, key)
    current = _ledger_snapshot(database, parent)
    current_preview = runner.invoke(
        app, ["scheduler", "cursor-store", "cleanup", "--conversation-key", key, "--dry-run"]
    )
    current_policy = runner.invoke(app, ["scheduler", "cursor-store", "policy"])
    assert current_preview.exit_code == 0
    assert "manual=True" in current_preview.stdout
    assert current_policy.exit_code == 0
    assert root.is_dir()
    assert _ledger_snapshot(database, parent) == current
    assert _disposal_count(store) == 0


def test_tombstone_publication_rejects_symlink_escapes(tmp_path: Path) -> None:
    secret = tmp_path / "operator-credential"
    secret.write_text("must preserve", encoding="utf-8")
    os.chmod(secret, 0o600)
    secret_mode = secret.stat().st_mode

    store, state_root, _owner, key = _finished_conversation(tmp_path / "directory")
    _seed_root(state_root, key)
    outside = tmp_path / "outside-receipts"
    outside.mkdir(mode=0o700)
    outside_file = outside / "keep.txt"
    outside_file.write_text("outside", encoding="utf-8")
    outside_mode = outside.stat().st_mode
    (state_root / "cursor-sdk-disposals").symlink_to(outside)
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert result.observed == "symlink_rejected"
    assert outside_file.read_text(encoding="utf-8") == "outside"
    assert outside.stat().st_mode == outside_mode
    assert secret.read_text(encoding="utf-8") == "must preserve"

    store, state_root, _owner, key = _finished_conversation(tmp_path / "temporary")
    _seed_root(state_root, key)
    receipts = state_root / "cursor-sdk-disposals"
    receipts.mkdir(mode=0o700)
    (receipts / f".{key}.json.tmp").symlink_to(secret)
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert secret.read_text(encoding="utf-8") == "must preserve"
    assert secret.stat().st_mode == secret_mode

    store, state_root, _owner, key = _finished_conversation(tmp_path / "destination")
    _seed_root(state_root, key)
    receipts = state_root / "cursor-sdk-disposals"
    receipts.mkdir(mode=0o700)
    (receipts / f"{key}.json").symlink_to(secret)
    result = _manual(store, state_root, key)
    assert result.action == "refused"
    assert secret.read_text(encoding="utf-8") == "must preserve"
    assert secret.stat().st_mode == secret_mode
    assert (receipts / f"{key}.json").is_symlink()


def test_stale_cleanup_generation_cannot_replace_the_receipt(tmp_path: Path) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    _seed_root(state_root, key)
    now = FAR

    def overtake() -> None:
        apply_cursor_sdk_cleanup(
            store,
            state_root=state_root,
            now=now + timedelta(seconds=31),
            mode="manual",
            conversation_key=key,
        )

    first = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=now,
        mode="manual",
        conversation_key=key,
        after_intent_hook=overtake,
    )
    ledger = _receipt_row(store, key)
    assert ledger is not None
    tombstone_path = state_root / "cursor-sdk-disposals" / f"{key}.json"
    tombstone_text = tombstone_path.read_text(encoding="utf-8")
    tombstone = json.loads(tombstone_text)
    assert ledger["receipt_sha256"] == tombstone["receipt_sha256"]
    assert tombstone_text == ledger["receipt_payload"]
    assert first.receipt_id == ledger["receipt_id"]
    assert first.action == "already_deleted"
    resumed = _manual(store, state_root, key)
    assert resumed.receipt_id == ledger["receipt_id"]
    assert tombstone_path.read_text(encoding="utf-8") == ledger["receipt_payload"]


def test_reconciled_abort_uses_authenticated_completion_not_abort_time(tmp_path: Path) -> None:
    store, state_root, owner, key = _aborted_without_sdk(tmp_path)
    root = _seed_root(state_root, key)
    old = (datetime(2020, 1, 1, tzinfo=UTC).timestamp(),) * 2
    os.utime(root, old)
    abort_at = FINISHED
    completed_at = FINISHED + timedelta(hours=1)
    _insert_ready_reconciliation(
        store,
        run_id=owner,
        owner=owner,
        key=key,
        completed_at=encode_utc_instant(completed_at),
        sql_updated_at="2020-01-01T00:00:00.000000Z",
    )
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=abort_at)
    assert len(preview.conversations) == 1
    assert preview.conversations[0].manual_eligible is True
    assert preview.conversations[0].conversation_key == key
    assert (
        maintain_cursor_sdk_conversations(
            store,
            state_root=state_root,
            now=abort_at + timedelta(hours=168),
        ).deleted_keys
        == ()
    )
    assert root.is_dir()
    just_before = completed_at + timedelta(hours=168) - timedelta(microseconds=1)
    exact = completed_at + timedelta(hours=168)
    assert (
        maintain_cursor_sdk_conversations(
            store, state_root=state_root, now=just_before
        ).deleted_keys
        == ()
    )
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=exact)
    assert deleted.deleted_keys == (key,)
    assert not root.exists()
    receipt = _receipt_row(store, key)["receipt_id"]
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=exact).deleted_keys
        == ()
    )
    assert _receipt_row(store, key)["receipt_id"] == receipt

    store, state_root, owner, key = _aborted_without_sdk(tmp_path / "missing-event")
    root = _seed_root(state_root, key)
    _insert_ready_reconciliation(
        store,
        run_id=owner,
        owner=owner,
        key=key,
        completed_at=None,
        sql_updated_at=STAMP,
    )
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    withheld = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert "missing_completion_evidence" in withheld.conversations[0].manual_reasons
    assert root.is_dir()
    assert _manual(store, state_root, key).action == "ineligible"

    store, state_root, owner, key = _aborted_without_sdk(tmp_path / "pending")
    root = _seed_root(state_root, key)
    _insert_ready_reconciliation(
        store,
        run_id=owner,
        owner=owner,
        key=key,
        completed_at=None,
        sql_updated_at=STAMP,
        status="pending",
    )
    _insert_attempt(store, owner, status="uncertain")
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )
    assert root.is_dir()


def test_blocked_ancestor_without_successor_stays_ineligible(tmp_path: Path) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path)
    _seed_root(state_root, key)
    _insert_run(
        store,
        run_id="run-still-blocked",
        owner=owner,
        key=key,
        kind="blocked",
        stamp=STAMP,
    )
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert preview.conversations[0].manual_eligible is False
    assert "continuable_state" in preview.conversations[0].manual_reasons
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=FAR).deleted_keys == ()
    )


def test_authenticated_successor_chain_uses_the_completed_stamp(tmp_path: Path) -> None:
    store, state_root, owner, key = _finished_conversation(tmp_path)
    root = _seed_root(state_root, key)
    blocked = "run-superseded-blocked"
    _insert_run(store, run_id=blocked, owner=owner, key=key, kind="blocked", stamp=STAMP)
    waiting = "run-waiting-descendant"
    _insert_ready_successor_edge(
        store,
        source=blocked,
        successor=owner,
        owner=owner,
        key=key,
        recovery_key="11" * 32,
    )
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=BEFORE)
    assert len(preview.conversations) == 1
    assert preview.conversations[0].manual_eligible is True
    assert preview.conversations[0].automatic_eligible is False
    assert preview.conversations[0].definitively_finished_at == STAMP
    assert (
        maintain_cursor_sdk_conversations(store, state_root=state_root, now=BEFORE).deleted_keys
        == ()
    )
    deleted = maintain_cursor_sdk_conversations(store, state_root=state_root, now=EXACT)
    assert deleted.deleted_keys == (key,)
    assert not root.exists()

    store, state_root, owner, key = _finished_conversation(tmp_path / "corrupt-edge")
    _seed_root(state_root, key)
    _insert_run(store, run_id=blocked, owner=owner, key=key, kind="blocked", stamp=STAMP)
    _insert_ready_successor_edge(
        store,
        source=blocked,
        successor=owner,
        owner=owner,
        key=key,
        recovery_key="22" * 32,
        corrupt_hash=True,
    )
    withheld = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert withheld.conversations[0].manual_eligible is False
    assert "continuable_state" in withheld.conversations[0].manual_reasons

    store, state_root, owner, key = _finished_conversation(tmp_path / "waiting-descendant")
    _seed_root(state_root, key)
    _insert_run(
        store, run_id=waiting, owner=owner, key=key, kind="waiting_for_cursor_fix", stamp=STAMP
    )
    _insert_run(store, run_id=blocked, owner=owner, key=key, kind="blocked", stamp=STAMP)
    _insert_ready_successor_edge(
        store,
        source=blocked,
        successor=waiting,
        owner=owner,
        key=key,
        recovery_key="33" * 32,
    )
    _insert_attempt(store, owner, status="uncertain")
    waiting_preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=FAR)
    assert waiting_preview.conversations[0].manual_eligible is False
    assert "continuable_state" in waiting_preview.conversations[0].manual_reasons
    assert "live_or_uncertain_attempt" in waiting_preview.conversations[0].manual_reasons


def test_concurrent_tombstone_publication_keeps_one_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    _seed_root(state_root, key)
    original_write = sdk_retention.os.write
    publishing = threading.Event()
    resume = threading.Event()
    outcomes: list[object] = []

    def overlap(descriptor: int, data: bytes | memoryview) -> int:
        if threading.current_thread().name == "first-cleanup":
            publishing.set()
            assert resume.wait(10)
        return original_write(descriptor, data)

    monkeypatch.setattr(sdk_retention.os, "write", overlap)

    def first_apply() -> None:
        try:
            outcomes.append(
                apply_cursor_sdk_cleanup(
                    store,
                    state_root=state_root,
                    now=FAR,
                    mode="manual",
                    conversation_key=key,
                )
            )
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=first_apply, name="first-cleanup")
    worker.start()
    try:
        assert publishing.wait(10)
        second = apply_cursor_sdk_cleanup(
            store,
            state_root=state_root,
            now=FAR,
            mode="manual",
            conversation_key=key,
        )
        assert second.action == "already_deleted"
    finally:
        resume.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert not isinstance(outcomes[0], Exception), repr(outcomes[0])
    assert outcomes[0].action == "deleted"
    ledger = _receipt_row(store, key)
    tombstone = (state_root / "cursor-sdk-disposals" / f"{key}.json").read_text(encoding="utf-8")
    assert ledger is not None
    assert tombstone == ledger["receipt_payload"]
    assert outcomes[0].receipt_id == second.receipt_id == ledger["receipt_id"]


def test_overlapping_first_publication_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    _seed_root(state_root, key)
    assert not (state_root / "cursor-sdk-disposals").exists()
    original_mkdir = sdk_retention.os.mkdir
    publishing = threading.Event()
    resume = threading.Event()
    outcomes: list[object] = []

    def overlap(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if (
            Path(str(path)) == state_root / "cursor-sdk-disposals"
            and threading.current_thread().name == "first-cleanup"
        ):
            publishing.set()
            assert resume.wait(10)
        if dir_fd is None:
            original_mkdir(path, mode)
            return
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sdk_retention.os, "mkdir", overlap)

    def first_apply() -> None:
        try:
            outcomes.append(
                apply_cursor_sdk_cleanup(
                    store,
                    state_root=state_root,
                    now=FAR,
                    mode="manual",
                    conversation_key=key,
                )
            )
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=first_apply, name="first-cleanup")
    worker.start()
    try:
        assert publishing.wait(10)
        second = apply_cursor_sdk_cleanup(
            store,
            state_root=state_root,
            now=FAR,
            mode="manual",
            conversation_key=key,
        )
        assert second.action == "already_deleted"
    finally:
        resume.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert not isinstance(outcomes[0], Exception), repr(outcomes[0])
    assert outcomes[0].action == "deleted"
    ledger = _receipt_row(store, key)
    tombstone = (state_root / "cursor-sdk-disposals" / f"{key}.json").read_text(encoding="utf-8")
    assert ledger is not None
    assert tombstone == ledger["receipt_payload"]
    assert outcomes[0].receipt_id == second.receipt_id == ledger["receipt_id"]
    assert (state_root / "cursor-sdk-disposals").is_dir()
    assert not (state_root / "cursor-sdk-disposals").is_symlink()


def test_tick_repairs_tombstone_without_starving_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state_root, _owner, key = _finished_conversation(tmp_path)
    _seed_root(state_root, key)
    original = sdk_retention._ensure_tombstone

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulate process failure after receipt commit")

    monkeypatch.setattr(sdk_retention, "_ensure_tombstone", interrupted)
    service = TickService(
        store,
        ProtectedArtifactStore(state_root / "artifacts"),
        BoundedGitAdmissionPort(),
        now_factory=lambda: FAR,
    )
    first = service.run_once()
    assert first.lease_acquired
    assert not (state_root / "cursor-sdk" / key).exists()
    assert _receipt_row(store, key)["status"] == "deleted"
    assert not (state_root / "cursor-sdk-disposals" / f"{key}.json").exists()
    monkeypatch.setattr(sdk_retention, "_ensure_tombstone", original)
    service.run_once()
    service.run_once()
    tombstone = state_root / "cursor-sdk-disposals" / f"{key}.json"
    assert tombstone.is_file()
    payload = _receipt_row(store, key)["receipt_payload"]
    assert tombstone.read_text(encoding="utf-8") == payload
    repeated = service.run_once()
    assert repeated.lease_acquired
    assert tombstone.read_text(encoding="utf-8") == payload

    other_store, other_root, _other_owner, other_done = _finished_conversation(tmp_path / "batch")
    pending = "run-still-eligible"
    pending_key = cursor_conversation_key(pending)
    _insert_run(
        other_store,
        run_id=pending,
        owner=pending,
        key=pending_key,
        kind="completed",
        stamp=STAMP,
    )
    _seed_root(other_root, other_done)
    _seed_root(other_root, pending_key)
    monkeypatch.setattr(sdk_retention, "_ensure_tombstone", interrupted)
    with pytest.raises(RuntimeError, match="receipt commit"):
        apply_cursor_sdk_cleanup(
            other_store,
            state_root=other_root,
            now=FAR,
            mode="automatic",
            conversation_key=other_done,
        )
    assert _receipt_row(other_store, other_done)["status"] == "deleted"
    assert not (other_root / "cursor-sdk" / other_done).exists()
    monkeypatch.setattr(sdk_retention, "_ensure_tombstone", original)
    maintained = maintain_cursor_sdk_conversations(
        other_store,
        state_root=other_root,
        now=FAR,
        batch_limit=1,
    )
    assert maintained.deleted_keys == (pending_key,)
    repaired = other_root / "cursor-sdk-disposals" / f"{other_done}.json"
    assert repaired.is_file()
    assert (
        repaired.read_text(encoding="utf-8")
        == _receipt_row(other_store, other_done)["receipt_payload"]
    )


def test_cleanup_does_not_install_a_timer_or_change_the_dashboard() -> None:
    retention = Path("src/ai_dev_loop/scheduler/application/cursor_sdk_retention.py").read_text(
        encoding="utf-8"
    )
    assert "systemd" not in retention
    assert "linger" not in retention
    timer_assets = Path("src/ai_dev_loop/scheduler/infrastructure/systemd_assets.py").read_text(
        encoding="utf-8"
    )
    assert "cursor_sdk_retention" not in timer_assets
    assert "cursor-store" not in timer_assets
    for path in Path("src/ai_dev_loop").rglob("*.py"):
        if "dashboard" in path.parts:
            assert "cursor_sdk_retention" not in path.read_text(encoding="utf-8")


def _finished_conversation(
    tmp_path: Path,
    *,
    run_id: str = "run-owner",
    kind: str = "completed",
) -> tuple[SqliteSchedulerStore, Path, str, str]:
    store = _store(tmp_path)
    key = cursor_conversation_key(run_id)
    _insert_run(store, run_id=run_id, owner=run_id, key=key, kind=kind, stamp=STAMP)
    return store, store.db_path.parent, run_id, key


def _store(tmp_path: Path) -> SqliteSchedulerStore:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    return SqliteSchedulerStore(root / "engine.sqlite3")


def _insert_run(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    owner: str,
    key: str,
    kind: str,
    stamp: str,
    historical_jsonl: bool = False,
) -> None:
    sdk: dict[str, object] = {
        "conversation_owner_run_id": owner,
        "conversation_key": key,
    }
    if historical_jsonl:
        sdk["schema_version"] = 1
    else:
        sdk["schema_version"] = 2
        sdk["store_backend"] = "sqlite"
        sdk["native_store_layout_version"] = 1
    payload: dict[str, object] = {
        "run_id": run_id,
        "kind": kind,
        "updated_at": stamp,
        "cursor": {"sdk": sdk},
    }
    if kind == "aborted":
        payload["aborted_at"] = stamp
    text, digest = _dump(payload)
    idempotency = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    worktree = hashlib.sha256(f"worktree-{run_id}".encode()).hexdigest()
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (run_id, kind, text, digest, idempotency, worktree, stamp, stamp),
        )


def _rewrite_stamp(
    store: SqliteSchedulerStore,
    *,
    updated_at: str | None,
    aborted_at: str | None = None,
) -> None:
    with store.begin_immediate() as conn:
        row = conn.execute("SELECT run_id, state_payload FROM scheduler_runs").fetchone()
        payload = json.loads(str(row["state_payload"]))
        if updated_at is None:
            payload.pop("updated_at", None)
        else:
            payload["updated_at"] = updated_at
        if aborted_at is None:
            payload.pop("aborted_at", None)
        else:
            payload["aborted_at"] = aborted_at
        text, digest = _dump(payload)
        conn.execute(
            """
            UPDATE scheduler_runs
               SET state_payload = ?, state_payload_sha256 = ?
             WHERE run_id = ?
            """,
            (text, digest, row["run_id"]),
        )


def _insert_attempt(
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    status: str,
    component: str = "cursor",
) -> None:
    digest = hashlib.sha256(b"{}").hexdigest()
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, created_at
            ) VALUES (?, ?, 1, 'test', '{}', ?, ?)
            """,
            (f"evt-{run_id}", run_id, digest, STAMP),
        )
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, 'cursor_turn', '{}', ?, 'succeeded', ?, ?, ?)
            """,
            (
                f"dsp-{run_id}",
                f"evt-{run_id}",
                run_id,
                f"effect-{run_id}",
                hashlib.sha256(run_id.encode()).hexdigest(),
                digest,
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (f"att-{run_id}", run_id, f"dsp-{run_id}", component, status, STAMP, STAMP),
        )


def _seed_root(state_root: Path, key: str) -> Path:
    root = state_root / "cursor-sdk" / key
    (root / "blobs").mkdir(parents=True)
    (root / "index.db").write_bytes(b"native")
    (root / "blobs" / "a.bin").write_bytes(b"blob")
    return root


def _protected_paths(state_root: Path, tmp_path: Path) -> dict[Path, bytes]:
    paths = {
        state_root / "artifacts" / "keep.txt": b"artifact",
        state_root / "operator-backup" / "engine.sqlite3": b"backup",
        state_root / "codex-home" / "session.json": b"codex",
        tmp_path / "credentials" / "cursor-default": b"secret-line",
        tmp_path / "repo" / "README": b"repository",
    }
    for path, payload in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return paths


def _manual(
    store: SqliteSchedulerStore,
    state_root: Path,
    key: str,
    *,
    after_intent_hook: Callable[[], None] | None = None,
):
    return apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=FAR,
        mode="manual",
        conversation_key=key,
        after_intent_hook=after_intent_hook,
    )


def _receipt_row(store: SqliteSchedulerStore, key: str) -> sqlite3.Row | None:
    with store.begin_read() as conn:
        return conn.execute(
            "SELECT * FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
            (key,),
        ).fetchone()


def _disposal_count(store: SqliteSchedulerStore) -> int:
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM scheduler_cursor_sdk_disposals"
        ).fetchone()
    return int(row["count"])


def _dump(payload: dict[str, object]) -> tuple[str, str]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith("engine.sqlite3"):
            continue
        files[str(path.relative_to(root))] = path.read_bytes()
    return files


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _ledger_snapshot(database: Path, parent: Path) -> dict[str, object]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = tuple(
            sorted(
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        )
    return {
        "user_version": version,
        "tables": tables,
        "database_mode": database.stat().st_mode,
        "parent_mode": parent.stat().st_mode,
    }


def _aborted_without_sdk(
    tmp_path: Path,
) -> tuple[SqliteSchedulerStore, Path, str, str]:
    store = _store(tmp_path)
    owner = "run-aborted-blocked"
    key = cursor_conversation_key(owner)
    payload = {
        "run_id": owner,
        "kind": "aborted",
        "updated_at": "2020-01-01T00:00:00.000000Z",
        "aborted_at": STAMP,
        "abort_reason": "operator",
        "prior_state_kind": "blocked",
    }
    text, digest = _dump(payload)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, 'aborted', ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                owner,
                text,
                digest,
                hashlib.sha256(owner.encode()).hexdigest(),
                hashlib.sha256(b"aborted-worktree").hexdigest(),
                "2020-01-01T00:00:00.000000Z",
                "2020-01-01T00:00:00.000000Z",
            ),
        )
    return store, store.db_path.parent, owner, key


def _insert_ready_successor_edge(
    store: SqliteSchedulerStore,
    *,
    source: str,
    successor: str,
    owner: str,
    key: str,
    recovery_key: str,
    corrupt_hash: bool = False,
) -> None:
    digest = "ab" * 32
    intent = CursorSdkSuccessorIntentV1(
        recovery_key=recovery_key,
        status="ready",
        source_run_id=source,
        successor_run_id=successor,
        failed_attempt_id=f"failed-{source}",
        dispatch_id=f"dispatch-{source}",
        turn_kind="initial",
        iteration=1,
        conversation_owner_run_id=owner,
        conversation_key=key,
        agent_id="agent-11111111-1111-4111-8111-111111111111",
        identity_owner_run_id=owner,
        identity_artifact_path="cursor/sdk-identity.json",
        identity_artifact_sha256=digest,
        reconciliation_owner_run_id=owner,
        reconciliation_artifact_path="cursor/reconciliation.json",
        reconciliation_artifact_sha256=digest,
        base_prompt_owner_run_id=owner,
        base_prompt_path="prompts/base.txt",
        base_prompt_sha256=digest,
        admitted_owner_run_id=owner,
        admitted_artifact_path="admitted.json",
        admitted_artifact_sha256=digest,
        reviews_completed=0,
        submitted_max_review_iterations=1,
        effective_review_ceiling=1,
        effective_prompt_path="prompts/effective.txt",
        effective_prompt_sha256=digest,
        record_sha256="12" * 32,
    )
    payload = intent.canonical_bytes()
    recorded = hashlib.sha256(payload).hexdigest()
    if corrupt_hash:
        recorded = "ff" * 32
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_cursor_initial_recoveries(
                recovery_key, source_run_id, failed_attempt_id, successor_run_id,
                dispatch_id, parent_recovery_key, status, intent_payload,
                intent_payload_sha256, record_artifact_path, record_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 'ready', ?, ?, ?, ?, ?, ?)
            """,
            (
                recovery_key,
                source,
                f"failed-{source}",
                successor,
                f"dispatch-{source}",
                payload.decode("utf-8"),
                recorded,
                "cursor/sdk-recovery/record-v1.json",
                intent.record_sha256,
                STAMP,
                STAMP,
            ),
        )


def _insert_ready_reconciliation(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    owner: str,
    key: str,
    completed_at: str | None,
    sql_updated_at: str,
    status: str = "ready",
) -> None:
    record_sha = "cd" * 32
    intent = CursorSdkReconciliationIntentV1(
        run_id=run_id,
        source_attempt_id=f"source-{run_id}",
        source_dispatch_id=f"dispatch-{run_id}",
        turn_kind="initial",
        iteration=1,
        conversation_owner_run_id=owner,
        conversation_key=key,
        agent_id="agent-11111111-1111-4111-8111-111111111111",
        prompt_path="prompts/initial.txt",
        prompt_sha256="ab" * 32,
        source_journal_path="cursor/journal.jsonl",
        source_journal_sha256="ef" * 32,
        identity_artifact_path="cursor/sdk-identity.json",
        identity_artifact_sha256="12" * 32,
        source_state={
            "cursor": {
                "sdk": {
                    "schema_version": 2,
                    "conversation_owner_run_id": owner,
                    "conversation_key": key,
                    "store_backend": "sqlite",
                    "native_store_layout_version": 1,
                }
            }
        },
        reviews_completed=0,
        submitted_max_review_iterations=1,
        effective_review_ceiling=1,
        failure_class="ambiguous",
    )
    payload = intent.canonical_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    recovery_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_cursor_sdk_reconciliations(
                recovery_key, run_id, source_attempt_id, source_dispatch_id, status,
                intent_payload, intent_payload_sha256, result_artifact_path, result_sha256,
                abort_requested_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recovery_key,
                run_id,
                f"source-{run_id}",
                f"dispatch-{run_id}",
                status,
                payload.decode("utf-8"),
                digest,
                "cursor/reconciliation.json",
                record_sha if status == "ready" else None,
                STAMP,
                sql_updated_at,
                sql_updated_at,
            ),
        )
        if completed_at is None:
            return
        event = CursorSdkReconciledEvent(
            run_id=run_id,
            recovery_key=recovery_key,
            source_attempt_id=f"source-{run_id}",
            reconciliation_attempt_id=f"recon-{run_id}",
            outcome="abort_reconciled",
            record_path="cursor/reconciliation.json",
            record_sha256=record_sha,
        )
        _kind, event_payload, event_digest = store.dump_event(event)
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (f"evt-recon-{run_id}", run_id, _kind, event_payload, event_digest, completed_at),
        )
