"""Phase 25.6 cold first-send regression through the real scheduler."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase25_2_sdk_workflow import (
    _configure_v2,
    _credential,
    _install_fake_sdk,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_sdk_attempt_journal_rel,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationRecordV1,
    CursorSdkReconciliationRecordV2,
)
from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_file

_ATTEMPTS = itertools.count(1)


def _run_cold_first_send(
    git_repo: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    occupant: str,
    extra_env: dict[str, str] | None = None,
    max_ticks: int = 60,
    after_tick: object | None = None,
) -> tuple[object, dict[str, object], int]:
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = __import__("os").environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    store_path = tmp_path / "durable-sdk-store.json"
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(store_path))
    monkeypatch.setenv("FAKE_CURSOR_SDK_CREATION_OCCUPANT", occupant)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
        "019abc00-1111-4222-8333-666666666666",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK first send",
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
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 19, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase256-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    state = None
    for _ in range(max_ticks):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            reviews = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id=? AND event_kind='run_completed'",
                (run_id,),
            ).fetchone()[0]
            reservation = store.get_reservation_for_run(conn, run_id)
        if state.kind in {"completed", "blocked"}:
            break
        if state.kind == "aborted" and reservation is None:
            break
        if after_tick is not None:
            after_tick(tick, store, run_id, store_path)  # type: ignore[operator]
    assert state is not None
    durable = json.loads(store_path.read_text(encoding="utf-8"))
    return state, durable, int(reviews)


@pytest.mark.parametrize("occupant", ["queued", "creating"])
def test_cold_first_send_resolves_owned_creation_occupant_once(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    occupant: str,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant=occupant,
    )
    assert state.kind == "completed", getattr(state, "block_reason_kind", state.kind)
    assert durable["sends"] == 1
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert durable["conversation_calls"] == 0
    creation = next(item for item in durable["runs"] if item["id"].endswith("777777777777"))
    assert creation["status"] == "cancelled"
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.native_run_id == "run-019abc00-1111-4222-8333-555555555555"
    assert state.cursor.sdk.native_run_id != creation["id"]
    assert reviews == 1
    assert state.codex.reviews_completed == 1


@pytest.mark.parametrize(
    ("extra_env", "reason"),
    [
        ({"FAKE_CURSOR_SDK_MESSAGE_COUNT": "1"}, "cursor_sdk_creation_readiness_hold"),
        ({"FAKE_CURSOR_SDK_INJECT_FOREIGN_ON_RESUME": "1"}, "cursor_sdk_creation_readiness_hold"),
        (
            {"FAKE_CURSOR_SDK_EXTRA_CREATION_RUN": "run-019abc00-1111-4222-8333-999999999999"},
            "cursor_sdk_creation_readiness_hold",
        ),
        ({"FAKE_CURSOR_SDK_FIRST_SEND_MODE": "message-busy"}, "cursor_sdk_ambiguous_missing"),
        ({"FAKE_CURSOR_SDK_FIRST_SEND_MODE": "typed-busy-twice"}, "cursor_sdk_ambiguous_missing"),
    ],
)
def test_missing_creation_proof_or_unproven_busy_keeps_the_reservation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_env: dict[str, str],
    reason: str,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env=extra_env,
    )
    assert state.kind == "blocked"
    assert state.block_reason_kind == reason
    assert int(durable.get("sends", 0)) == 0
    assert reviews == 0
    codex = getattr(state, "codex", None)
    assert codex is None or codex.reviews_completed == 0


def test_unknown_creation_status_holds_without_cancellation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    state, durable, _reviews = _run_cold_first_send(
        git_repo, isolated_xdg, monkeypatch, tmp_path, occupant="mystery"
    )
    assert state.kind == "blocked"
    assert state.block_reason_kind == "cursor_sdk_creation_readiness_hold"
    assert durable.get("cancels", []) == []
    assert int(durable.get("sends", 0)) == 0


@pytest.mark.parametrize("mode", ["typed-busy", "structured-busy"])
def test_one_definitive_rejection_resends_the_same_prompt_once(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={"FAKE_CURSOR_SDK_FIRST_SEND_MODE": mode},
    )
    assert state.kind == "completed", getattr(state, "block_reason_kind", state.kind)
    assert durable["send_attempts"] == 2
    assert durable["sends"] == 1
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert reviews == 1
    assert state.codex.reviews_completed == 1


def test_network_loss_after_an_accepted_run_replays_without_another_send(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={
            "FAKE_CURSOR_SDK_FIRST_SEND_MODE": "accept-network",
            "FAKE_CURSOR_SDK_REPLAY_SUCCESS": "1",
            "FAKE_CURSOR_SDK_DISCOVER_UNIQUE_RUN": "1",
        },
    )
    assert state.kind == "completed", getattr(state, "block_reason_kind", state.kind)
    assert durable["send_attempts"] == 1
    assert int(durable.get("sends", 0)) == 0
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.native_run_id == "run-019abc00-1111-4222-8333-888888888888"
    assert reviews == 1
    assert state.codex.reviews_completed == 1


@pytest.mark.parametrize("selected_umask", [0o022, 0o077])
def test_sequence_leaf_passes_creation_readiness_before_the_next_phase(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    selected_umask: int,
) -> None:
    del fake_clis
    previous_umask = os.umask(selected_umask)
    request.addfinalizer(lambda: os.umask(previous_umask))
    repo_yaml = Path(__file__).resolve().parents[2] / "ai_dev_loop.yaml"
    (git_repo / "ai_dev_loop.yaml").write_bytes(repo_yaml.read_bytes())
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt", "ai_dev_loop.yaml"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sequence baseline"], cwd=git_repo, check=True)
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()
    fake_sdk = tmp_path / "fake-sdk-sequence"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    credential = isolated_xdg / "config/ai_dev_loop/credentials"
    credential.mkdir(parents=True, mode=0o700)
    credential.parent.chmod(0o700)
    credential.chmod(0o700)
    key = credential / "cursor-default"
    key.write_text("fake-test-credential", encoding="utf-8")
    key.chmod(0o600)
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(tmp_path / "sequence-store.json"))
    monkeypatch.setenv("FAKE_CURSOR_SDK_CREATION_OCCUPANT", "queued")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    phases = []
    for ordinal in (1, 2):
        phases.append(
            {
                "name": f"phase-{ordinal}",
                "short_name": f"SDK leaf {ordinal}",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "commit_message": "accepted phase one" if ordinal == 1 else None,
                "cursor": cursor,
                "codex": {"review_model": "gpt-6.1-sol", "review_reasoning_effort": "high"},
                "implementer": {"provider": "cursor"},
            }
        )
    manifest = tmp_path / "sdk-v4.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "sdk-first-send",
                "short_name": "SDK first send sequence",
                "phases": phases,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    paths = {"db_path": state_root / "engine.sqlite3", "artifact_root": state_root / "artifacts"}
    prepared = prepare_sequence(
        SequencePrepareOptions(manifest_path=manifest, repo_path=git_repo, **paths)
    )
    start_sequence(prepared.sequence_id, db_path=paths["db_path"])
    store = SqliteSchedulerStore(paths["db_path"])
    tick = TickService(
        store,
        ProtectedArtifactStore(paths["artifact_root"]),
        BoundedGitAdmissionPort(),
        now_factory=lambda: datetime(2026, 10, 19, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase256-seq-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(),
        preflight_port=OkPreflightPort(),
    )
    sequence = None
    for _ in range(120):
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if isinstance(sequence, AwaitingFinalizationSequenceState):
            break
        ordinal = sequence.current_ordinal
        monkeypatch.setenv(
            "FAKE_CODEX_REVIEWER_SESSION_ID",
            f"019def00-0000-0000-0003-{ordinal:012x}",
        )
        tick.run_once()
    else:
        raise AssertionError("sequence did not reach the next phase handoff")
    assert sequence is not None
    assert len(sequence.materialized_entries) == 2
    assert (
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{baseline}..HEAD"], cwd=git_repo, text=True
        ).strip()
        == "1"
    )
    durable = json.loads((tmp_path / "sequence-store.json").read_text(encoding="utf-8"))
    assert int(durable["sends"]) == 1
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert durable["conversation_calls"] == 0


def test_creation_readiness_does_not_block_the_same_agent_correction(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={"FAKE_CODEX_REVIEW_SEQUENCE": "findings,no_findings"},
        max_ticks=80,
    )
    assert state.kind == "completed", getattr(state, "block_reason_kind", state.kind)
    prompts = durable.get("prompts", [])
    assert isinstance(prompts, list)
    assert len(prompts) == 2
    assert len(set(prompts)) == 2
    assert int(durable["sends"]) == 2
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.native_run_id == "run-019abc00-1111-4222-8333-555555555556"
    assert state.cursor.sdk.native_run_id != "run-019abc00-1111-4222-8333-777777777777"
    assert state.codex.reviewer_session_id == "019abc00-1111-4222-8333-666666666666"
    assert state.codex.reviews_completed == 2
    assert reviews == 1
    db = isolated_xdg / "state/ai_dev_loop/engine.sqlite3"
    store = SqliteSchedulerStore(db)
    with store.begin_read() as conn:
        run_id = str(conn.execute("SELECT run_id FROM scheduler_runs").fetchone()[0])
        attempts = conn.execute(
            """
            SELECT attempt_id FROM scheduler_attempts
            WHERE run_id=? AND component='cursor'
            ORDER BY created_at, attempt_id
            """,
            (run_id,),
        ).fetchall()
    root = run_artifact_root(isolated_xdg / "state/ai_dev_loop/artifacts", run_id)
    correction = json.loads(
        (root / invocation_evidence_rel(str(attempts[-1]["attempt_id"]))).read_text(
            encoding="utf-8"
        )
    )
    prior_path = str(correction["prior_authorized_journal_path"])
    prior_sha = str(correction["prior_authorized_journal_sha256"])
    prior_attempt = str(correction["prior_authorized_attempt_id"])
    assert sha256_file(root / prior_path) == prior_sha
    assert prior_path == cursor_sdk_attempt_journal_rel(prior_attempt)
    assert prior_attempt != str(attempts[-1]["attempt_id"])
    assert correction["prior_authorized_dispatch_id"]
    assert correction["prior_authorized_prompt_sha256"]
    first_turn = json.loads(
        (root / invocation_evidence_rel(prior_attempt)).read_text(encoding="utf-8")
    )
    assert "prior_authorized_journal_sha256" not in first_turn


def test_resolved_pre_send_interruption_sends_one_initial_prompt(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={"AI_DEV_LOOP_SDK_INTERRUPT_AFTER": "creation_blocker_cancel_confirmed"},
    )
    assert state.kind == "completed", getattr(state, "block_reason_kind", state.kind)
    assert int(durable["sends"]) == 1
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.native_run_id == "run-019abc00-1111-4222-8333-555555555555"
    assert reviews == 1
    db = isolated_xdg / "state/ai_dev_loop/engine.sqlite3"
    persisted = SqliteSchedulerStore(db)
    with persisted.begin_read() as conn:
        run_id = str(conn.execute("SELECT run_id FROM scheduler_runs").fetchone()[0])
        event = conn.execute(
            """
            SELECT event_kind, event_payload FROM scheduler_events
            WHERE run_id=? AND event_kind LIKE 'cursor_sdk_reconciled%'
            """,
            (run_id,),
        ).fetchone()
    assert event is not None
    assert str(event["event_kind"]) == "cursor_sdk_reconciled_v2"
    payload = json.loads(str(event["event_payload"]))
    assert payload["outcome"] == "prompt_unsent"
    assert payload["schema_version"] == 2
    record_path = run_artifact_root(isolated_xdg / "state/ai_dev_loop/artifacts", run_id) / str(
        payload["record_path"]
    )
    record = CursorSdkReconciliationRecordV2.model_validate_json(record_path.read_bytes())
    assert record.outcome == "prompt_unsent"
    assert str(payload["record_path"]).endswith("record-v2.json")


def test_abort_after_pre_send_interruption_sends_nothing(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    aborted = {"done": False}

    def abort_once(
        tick: TickService, store: SqliteSchedulerStore, run_id: str, store_path: Path
    ) -> None:
        if aborted["done"] or not store_path.is_file():
            return
        durable = json.loads(store_path.read_text(encoding="utf-8"))
        if not durable.get("cancels") or int(durable.get("sends", 0)) != 0:
            return
        receipt = tick._run_abort.abort_run(run_id)
        assert receipt.abort_persisted is True
        aborted["done"] = True

    state, durable, reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={"AI_DEV_LOOP_SDK_INTERRUPT_AFTER": "creation_blocker_cancel_confirmed"},
        after_tick=abort_once,
    )
    assert aborted["done"] is True
    assert state.kind == "aborted"
    assert int(durable.get("sends", 0)) == 0
    assert durable["cancels"] == ["run-019abc00-1111-4222-8333-777777777777"]
    assert reviews == 0
    db = isolated_xdg / "state/ai_dev_loop/engine.sqlite3"
    persisted = SqliteSchedulerStore(db)
    with persisted.begin_read() as conn:
        run_id = str(conn.execute("SELECT run_id FROM scheduler_runs").fetchone()[0])
        assert persisted.get_reservation_for_run(conn, run_id) is None
        downstream = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id=? AND component IN ('staging', 'codex')
            """,
            (run_id,),
        ).fetchone()[0]
        relation = conn.execute(
            """
            SELECT status FROM scheduler_cursor_sdk_reconciliations
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        event = conn.execute(
            """
            SELECT event_kind, event_payload FROM scheduler_events
            WHERE run_id=? AND event_kind LIKE 'cursor_sdk_reconciled%'
            """,
            (run_id,),
        ).fetchone()
    assert downstream == 0
    assert relation is not None
    assert str(relation["status"]) == "ready"
    assert event is not None
    assert str(event["event_kind"]) == "cursor_sdk_reconciled"
    payload = json.loads(str(event["event_payload"]))
    assert payload["outcome"] == "abort_reconciled"
    assert "schema_version" not in payload
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        (
            run_artifact_root(isolated_xdg / "state/ai_dev_loop/artifacts", run_id)
            / str(payload["record_path"])
        ).read_bytes()
    )
    assert record.schema_version == 1
    assert record.outcome == "abort_reconciled"
    assert str(payload["record_path"]).endswith("record-v1.json")


def test_lost_published_v2_envelope_does_not_start_another_reconciliation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A published v2 result survives envelope loss without another SDK observation."""

    del fake_clis
    original = FakeAgentProcessBackend._execute_cursor_attempt
    seen = {"dropped": False, "reconciliations": 0}

    def execute(self: FakeAgentProcessBackend, request: object) -> int:
        result = original(self, request)  # type: ignore[arg-type]
        stdout_path = getattr(request, "stdout_path", None)
        envelope_path = getattr(request, "result_envelope_path", None)
        try:
            outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return result
        if outcome.get("effect_kind") == "cursor.reconcile_sdk_run":
            seen["reconciliations"] += 1
        published = str(outcome.get("reconciliation_record_path", ""))
        if published.endswith("record-v2.json") and not seen["dropped"]:
            assert envelope_path.is_file()
            envelope_path.unlink()
            seen["dropped"] = True
            monkeypatch.setenv("FAKE_CURSOR_SDK_TURN_OPEN_FAILURE", "1")
        elif seen["dropped"] and outcome.get("effect_kind") == "cursor.reconcile_sdk_run":
            monkeypatch.delenv("FAKE_CURSOR_SDK_TURN_OPEN_FAILURE", raising=False)
        return result

    monkeypatch.setattr(FakeAgentProcessBackend, "_execute_cursor_attempt", execute)

    def clear_outage_after_published_record(
        tick: TickService,
        store: SqliteSchedulerStore,
        run_id: str,
        store_path: Path,
    ) -> None:
        del store_path
        root = tick._attempt_service.artifacts.run_root(run_id)
        monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(root / "cursor/sdk-identity.json"))
        monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(root))
        with store.begin_read() as conn:
            row = conn.execute(
                "SELECT status FROM scheduler_cursor_sdk_reconciliations WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is not None and str(row["status"]) == "ready":
            monkeypatch.delenv("FAKE_CURSOR_SDK_TURN_OPEN_FAILURE", raising=False)

    state, durable, _reviews = _run_cold_first_send(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        occupant="queued",
        extra_env={"AI_DEV_LOOP_SDK_INTERRUPT_AFTER": "creation_blocker_cancel_confirmed"},
        after_tick=clear_outage_after_published_record,
        max_ticks=100,
    )
    assert seen["dropped"] is True
    assert seen["reconciliations"] == 1
    assert state.kind == "completed", (state.kind, getattr(state, "block_reason_kind", None))
    assert int(durable["sends"]) == 1
