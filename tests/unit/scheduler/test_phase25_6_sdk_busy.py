"""Phase 25.6 creation-readiness, rejection, and restart regressions."""

from __future__ import annotations

import hashlib
import json
import signal
import time
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase25_2_sdk_worker import _evidence, _repo

import ai_dev_loop.scheduler.cursor_attempt_runner as cursor_runner
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkAgentIdentity,
    CursorSdkBridgeHealth,
    CursorSdkErrorCode,
    CursorSdkFailure,
    CursorSdkOperation,
    CursorSdkOutcome,
    CursorSdkPreflight,
    CursorSdkRunIdentity,
    CursorSdkRunPage,
    CursorSdkTerminalRun,
)
from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_sdk_journal import (
    _SEALABLE_SEND_PREFIXES_V2,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_SCHEMA_VERSION_V2,
    append_sdk_journal,
    authenticate_sdk_journal,
    authenticate_sdk_recovery_journal,
    seal_interrupted_sdk_send_journal,
)
from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
    authenticated_published_reconciliation_result,
)
from ai_dev_loop.scheduler.cursor_attempt_runner import (
    CursorSdkWorkerInterrupt,
    _bound_readiness_call,
    _load_creation_inventory,
    _ReadinessBudgetExceeded,
    _run_sdk_create,
    _run_sdk_reconciliation_operation,
    _run_sdk_turn,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_reconciliation_record_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_readiness import (
    CreationInventoryFact,
    PublicRunFact,
    classify_creation_readiness,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkReconciliationRecordV1,
    CursorSdkReconciliationRecordV2,
)
from ai_dev_loop.state import sha256_file

AGENT = "019abc00-1111-4222-8333-444444444444"
CREATION = "run-019abc00-1111-4222-8333-777777777777"
PROMPT = "run-019abc00-1111-4222-8333-555555555555"
ACCEPTED = "run-019abc00-1111-4222-8333-888888888888"
FOREIGN = "run-019abc00-1111-4222-8333-999999999999"


def _fact(run_id: str, status: str) -> PublicRunFact:
    return PublicRunFact(run_id=run_id, status=status)


def _inventory(*runs: PublicRunFact, messages: int = 0, sends: int = 0) -> CreationInventoryFact:
    return CreationInventoryFact(runs=runs, message_count=messages, authorized_send_count=sends)


@pytest.mark.parametrize(
    ("inventory", "observed", "messages", "action"),
    [
        (_inventory(_fact(CREATION, "queued")), (_fact(CREATION, "queued"),), 0, "cancel"),
        (_inventory(_fact(CREATION, "running")), (_fact(CREATION, "running"),), 0, "cancel"),
        (_inventory(_fact(CREATION, "queued")), (_fact(CREATION, "finished"),), 0, "send"),
        (_inventory(_fact(CREATION, "queued")), (_fact(CREATION, "error"),), 0, "send"),
        (_inventory(_fact(CREATION, "queued")), (_fact(CREATION, "mystery"),), 0, "hold"),
        (
            _inventory(_fact(CREATION, "queued"), messages=1),
            (_fact(CREATION, "queued"),),
            0,
            "hold",
        ),
        (_inventory(_fact(CREATION, "queued")), (_fact(CREATION, "queued"),), 1, "hold"),
        (_inventory(_fact(CREATION, "queued"), sends=1), (_fact(CREATION, "queued"),), 0, "hold"),
        (
            _inventory(_fact(CREATION, "queued"), _fact(FOREIGN, "running")),
            (_fact(CREATION, "queued"),),
            0,
            "hold",
        ),
        (
            _inventory(_fact(CREATION, "queued")),
            (_fact(CREATION, "queued"), _fact(FOREIGN, "running")),
            0,
            "hold",
        ),
        (_inventory(), (), 0, "send"),
        (_inventory(), (_fact(FOREIGN, "running"),), 0, "hold"),
    ],
)
def test_creation_readiness_requires_authenticated_exclusive_no_work_proof(
    inventory: CreationInventoryFact,
    observed: tuple[PublicRunFact, ...],
    messages: int,
    action: str,
) -> None:
    decision = classify_creation_readiness(inventory, observed, message_count=messages)
    assert decision.action == action
    if action == "cancel":
        assert decision.occupant_id == CREATION
    else:
        assert decision.occupant_id is None


def test_creation_readiness_rejects_non_integer_counts() -> None:
    inventory = CreationInventoryFact(
        runs=(_fact(CREATION, "queued"),),
        message_count=True,  # type: ignore[arg-type]
        authorized_send_count=0,
    )
    assert (
        classify_creation_readiness(inventory, (_fact(CREATION, "queued"),), message_count=0).action
        == "hold"
    )


class ScriptedSdk:
    def __init__(
        self,
        runs: list[dict[str, str]] | None = None,
        *,
        messages: int = 0,
        send_modes: tuple[str, ...] = ("ok",),
        duplicate: bool = False,
        stuck: bool = False,
        hide_run: bool = False,
    ) -> None:
        self.runs = [dict(item) for item in (runs or [])]
        self.messages = messages
        self.send_modes = list(send_modes)
        self.duplicate = duplicate
        self.stuck = stuck
        self.hide_run = hide_run
        self.cancel_calls: list[str] = []
        self.send_calls = 0
        self.list_calls = 0
        self.conversation_calls = 0

    def open(self) -> CursorSdkOutcome[CursorSdkBridgeHealth]:
        return CursorSdkOutcome.success(CursorSdkBridgeHealth("1.0.37", "fake", "v1"))

    def preflight(self) -> CursorSdkOutcome[CursorSdkPreflight]:
        return CursorSdkOutcome.success(
            CursorSdkPreflight(CursorSdkBridgeHealth("1.0.37", "fake", "v1"), True, "grok-4.7", ())
        )

    def create_agent(self) -> CursorSdkOutcome[CursorSdkAgentIdentity]:
        return CursorSdkOutcome.success(CursorSdkAgentIdentity(AGENT))

    def resume_agent(self, agent_id: str) -> CursorSdkOutcome[CursorSdkAgentIdentity]:
        return CursorSdkOutcome.success(CursorSdkAgentIdentity(agent_id))

    def list_agent_messages(self, agent_id: str) -> CursorSdkOutcome[int]:
        assert agent_id == AGENT
        return CursorSdkOutcome.success(self.messages)

    def conversation(self) -> None:
        self.conversation_calls += 1
        raise TimeoutError("GetRunConversation timed out")

    def list_runs(
        self, agent_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CursorSdkOutcome[CursorSdkRunPage]:
        del limit
        assert agent_id == AGENT
        self.list_calls += 1
        if self.stuck and cursor == "stuck":
            return CursorSdkOutcome.success(CursorSdkRunPage(runs=(), next_cursor="stuck"))
        runs = [
            CursorSdkTerminalRun(AGENT, item["id"], item["status"], item.get("text", ""), None)
            for item in self.runs
        ]
        if self.duplicate and runs:
            runs = [runs[0], runs[0]]
        next_cursor = "stuck" if self.stuck and cursor is None else None
        return CursorSdkOutcome.success(CursorSdkRunPage(runs=tuple(runs), next_cursor=next_cursor))

    def get_run(self, agent_id: str, run_id: str) -> CursorSdkOutcome[CursorSdkTerminalRun]:
        assert agent_id == AGENT
        if self.hide_run:
            return CursorSdkOutcome.failed(
                CursorSdkFailure(
                    CursorSdkErrorCode.NETWORK, CursorSdkOperation.GET_RUN, agent_id, run_id
                )
            )
        item = next(run for run in self.runs if run["id"] == run_id)
        return CursorSdkOutcome.success(
            CursorSdkTerminalRun(AGENT, run_id, item["status"], item.get("text", ""), None)
        )

    def cancel(self, agent_id: str, run_id: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        assert agent_id == AGENT
        self.cancel_calls.append(run_id)
        for item in self.runs:
            if item["id"] == run_id:
                item["status"] = "cancelled"
        return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id, run_id))

    def send(self, agent_id: str, prompt: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        assert agent_id == AGENT and prompt.strip()
        self.send_calls += 1
        mode = self.send_modes.pop(0) if self.send_modes else "ok"
        if mode == "typed":
            return self._busy("typed")
        if mode == "structured":
            return self._busy("structured")
        if mode == "message":
            return self._busy("message")
        if mode == "network":
            return CursorSdkOutcome.failed(
                CursorSdkFailure(
                    CursorSdkErrorCode.NETWORK, CursorSdkOperation.SEND, agent_id, None
                )
            )
        if mode == "accept":
            self.runs.append({"id": ACCEPTED, "status": "finished", "text": "accepted once"})
            return self._busy("typed")
        if mode == "multiple":
            self.runs.append({"id": ACCEPTED, "status": "finished", "text": "one"})
            self.runs.append({"id": FOREIGN, "status": "finished", "text": "two"})
            return CursorSdkOutcome.failed(
                CursorSdkFailure(
                    CursorSdkErrorCode.NETWORK, CursorSdkOperation.SEND, agent_id, None
                )
            )
        run_id = (
            "run-019abc00-1111-4222-8333-555555555556"
            if any(item["id"] == PROMPT for item in self.runs)
            else PROMPT
        )
        self.runs.append({"id": run_id, "status": "finished", "text": "exact terminal"})
        return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id, run_id))

    def stream(
        self,
        agent_id: str,
        run_id: str,
        *,
        on_envelope: object = None,
        **kwargs: object,
    ) -> CursorSdkOutcome[CursorSdkTerminalRun]:
        del kwargs
        assert agent_id == AGENT
        if callable(on_envelope):
            from ai_dev_loop.integrations.cursor_sdk.port import CursorSdkEnvelope

            on_envelope(CursorSdkEnvelope("1", {"offset": "1", "kind": "result"}))
        if self_repo is not None:
            (self_repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        text = next((item.get("text", "") for item in self.runs if item["id"] == run_id), "")
        return CursorSdkOutcome.success(
            CursorSdkTerminalRun(agent_id, run_id, "finished", text or "exact terminal", None)
        )

    def close(self) -> None:
        return None

    def _busy(self, evidence_class: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        return CursorSdkOutcome.failed(
            CursorSdkFailure(
                CursorSdkErrorCode.BUSY,
                CursorSdkOperation.SEND,
                AGENT,
                None,
                evidence_class=evidence_class,  # type: ignore[arg-type]
            )
        )


self_repo: Path | None = None


def _bind(monkeypatch: pytest.MonkeyPatch, adapter: ScriptedSdk, repo: Path) -> None:
    global self_repo
    self_repo = repo
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda evidence: adapter)


def _clock(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, float], list[float]]:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def now() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(cursor_runner, "_readiness_now", now)
    monkeypatch.setattr(cursor_runner, "_readiness_sleep", sleep)
    return clock, sleeps


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter: ScriptedSdk,
) -> tuple[dict[str, object], Path, str]:
    repo = _repo(tmp_path)
    _bind(monkeypatch, adapter, repo)
    _clock(monkeypatch)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    create_attempt = "att-" + "1" * 32
    created = _run_sdk_create(
        _evidence(repo, effect_kind="cursor.create_chat", attempt_id=create_attempt),
        run_root,
        attempt_id=create_attempt,
    )
    assert created.get("failure_kind") in {None, ""}
    identity = {
        "schema_version": 1,
        "runtime": "sdk-local",
        "conversation_owner_run_id": "project-sdk-run",
        "conversation_key": cursor_conversation_key("project-sdk-run"),
        "agent_id": AGENT,
        "create_attempt_id": create_attempt,
        "create_journal_path": created["journal_path"],
        "create_journal_sha256": created["journal_sha256"],
    }
    relative = "cursor/sdk-identity.json"
    path = run_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
    prompt_rel = "prompts/cursor-initial.txt"
    prompt = run_root / prompt_rel
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    turn_attempt = "att-" + "2" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=turn_attempt)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT,
            "prompt_path": prompt_rel,
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
            "identity_artifact_path": relative,
            "identity_artifact_sha256": sha256_file(path),
        }
    )
    return evidence, run_root, turn_attempt


def _kinds(run_root: Path, outcome: dict[str, object]) -> list[str]:
    return [
        json.loads(line)["kind"]
        for line in (run_root / str(outcome["journal_path"])).read_text().splitlines()
    ]


def test_owned_queued_occupant_is_cancelled_once_before_the_first_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    _, sleeps = _clock(monkeypatch)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["native_run_id"] == PROMPT
    assert adapter.cancel_calls == [CREATION]
    assert adapter.send_calls == 1
    assert adapter.conversation_calls == 0
    assert sleeps == []
    assert "implement exactly" not in json.dumps(outcome)
    assert _kinds(run_root, outcome) == [
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "native_run_bound",
        "terminal",
    ]


def test_public_running_creation_occupant_uses_the_same_no_work_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "running"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["native_run_id"] == PROMPT
    assert adapter.cancel_calls == [CREATION]


def test_terminal_creation_occupant_is_not_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "error", "text": ""}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["native_run_id"] == PROMPT
    assert adapter.cancel_calls == []


@pytest.mark.parametrize(
    "runs",
    [
        [{"id": CREATION, "status": "mystery"}],
        [{"id": CREATION, "status": "queued"}, {"id": FOREIGN, "status": "running"}],
    ],
)
def test_unknown_or_nonexclusive_occupants_hold_without_cancel_or_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: list[dict[str, str]]
) -> None:
    adapter = ScriptedSdk(runs)
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    _, sleeps = _clock(monkeypatch)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.cancel_calls == []
    assert adapter.send_calls == 0
    assert adapter.conversation_calls == 0
    if runs[0]["status"] == "mystery":
        assert len(sleeps) == 2
        assert sum(sleeps) <= 10


def test_public_history_blocks_creation_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], messages=1)
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.cancel_calls == []
    assert adapter.send_calls == 0


def test_duplicate_or_stuck_inventory_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], duplicate=True)
    _bind(monkeypatch, adapter, repo)
    with pytest.raises(ValueError, match="identities conflict"):
        _run_sdk_create(
            _evidence(repo, effect_kind="cursor.create_chat", attempt_id="att-" + "5" * 32),
            tmp_path / "artifacts" / "runs" / ("b" * 64),
            attempt_id="att-" + "5" * 32,
        )
    stuck = ScriptedSdk([{"id": CREATION, "status": "queued"}], stuck=True)
    _bind(monkeypatch, stuck, repo)
    with pytest.raises(ValueError, match="no progress"):
        _run_sdk_create(
            _evidence(repo, effect_kind="cursor.create_chat", attempt_id="att-" + "6" * 32),
            tmp_path / "artifacts" / "runs" / ("c" * 64),
            attempt_id="att-" + "6" * 32,
        )


@pytest.mark.parametrize(
    ("mode", "expected_sends"), [("typed", 2), ("structured", 2), ("message", 1), ("network", 1)]
)
def test_only_definitive_rejection_can_resend_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, expected_sends: int
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=(mode, "ok"))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert adapter.send_calls == expected_sends
    assert adapter.cancel_calls == [CREATION]
    if expected_sends == 2:
        assert outcome["native_run_id"] == PROMPT
        assert _kinds(run_root, outcome).count("send_intent") == 2
    else:
        assert outcome["failure_kind"] == "cursor_sdk_send_uncertain"
        assert _kinds(run_root, outcome).count("send_intent") == 1


def test_an_accepted_prompt_is_not_sent_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=("accept",))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert adapter.send_calls == 1
    assert outcome["failure_kind"] == "cursor_sdk_send_uncertain"
    reconciled = _reconcile(monkeypatch, evidence, run_root, outcome, adapter)
    assert reconciled["reconciliation_outcome"] == "terminal_recovered"
    assert adapter.send_calls == 1
    record = _record(run_root, reconciled)
    assert record.native_run_id == ACCEPTED
    assert CREATION not in record.candidate_run_ids


def test_multiple_post_send_runs_stay_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=("multiple",))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    reconciled = _reconcile(monkeypatch, evidence, run_root, outcome, adapter)
    assert adapter.send_calls == 1
    assert reconciled["reconciliation_outcome"] == "ambiguous_multiple"
    assert _record(run_root, reconciled).native_run_id is None


def test_a_second_rejection_does_not_send_a_third_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=("typed", "typed"))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert adapter.send_calls == 2
    assert outcome["failure_kind"] == "cursor_sdk_send_uncertain"


@pytest.mark.parametrize(
    "barrier",
    [
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "native_run_bound",
    ],
)
def test_restart_after_interrupt_confirms_without_another_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, barrier: str
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    monkeypatch.setenv("AI_DEV_LOOP_SDK_INTERRUPT_AFTER", barrier)
    with pytest.raises(CursorSdkWorkerInterrupt):
        _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    journal = run_root / cursor_sdk_attempt_journal_rel(attempt_id)
    assert seal_interrupted_sdk_send_journal(journal) is True
    sends_before = adapter.send_calls
    cancels_before = list(adapter.cancel_calls)
    sealed = {
        "journal_path": cursor_sdk_attempt_journal_rel(attempt_id),
        "failure_kind": "cursor_sdk_worker_lost",
    }
    first = _reconcile(monkeypatch, evidence, run_root, sealed, adapter)
    assert adapter.send_calls == sends_before
    if barrier == "creation_blocker_cancel_intent":
        assert cancels_before == []
        assert adapter.cancel_calls == [CREATION]
    else:
        assert adapter.cancel_calls == cancels_before
    assert CREATION not in _record(run_root, first).candidate_run_ids
    if barrier == "native_run_bound":
        assert _record(run_root, first).native_run_id == PROMPT
        assert first["reconciliation_outcome"] == "terminal_recovered"
    elif barrier == "send_intent":
        assert first["reconciliation_outcome"] == "ambiguous_missing"
        assert _record(run_root, first).native_run_id is None
    else:
        record = _record(run_root, first)
        assert first["reconciliation_outcome"] == "prompt_unsent"
        assert record.schema_version == 2
        assert record.native_run_id is None
        assert record.candidate_run_ids == ()
        assert str(first["reconciliation_record_path"]).endswith("record-v2.json")
        historical = json.loads((run_root / str(first["reconciliation_record_path"])).read_bytes())
        historical["schema_version"] = 1
        with pytest.raises(ValueError, match="prompt_unsent"):
            CursorSdkReconciliationRecordV1.model_validate(historical)
    if barrier == "send_intent":
        historical_v1 = _record(run_root, first)
        assert historical_v1.schema_version == 1
        assert str(first["reconciliation_record_path"]).endswith("record-v1.json")
    again = _reconcile(monkeypatch, evidence, run_root, sealed, adapter)
    assert adapter.send_calls == sends_before
    assert adapter.cancel_calls == (
        [CREATION] if barrier == "creation_blocker_cancel_intent" else cancels_before
    )
    assert again["reconciliation_outcome"] == first["reconciliation_outcome"]


def test_abort_cancels_only_the_owned_creation_blocker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    _bind(monkeypatch, adapter, repo)
    evidence, root = _v2_baseline(tmp_path, repo)
    evidence["abort_requested"] = True
    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )
    assert outcome["reconciliation_outcome"] == "abort_reconciled"
    assert adapter.cancel_calls == [CREATION]
    assert adapter.send_calls == 0
    record = _record(root, outcome)
    assert record.schema_version == 1
    assert str(outcome["reconciliation_record_path"]).endswith("record-v1.json")
    assert record.candidate_run_ids == ()
    assert record.cancellation_requested is True
    assert record.native_run_id is None


def test_unconfirmed_abort_keeps_the_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], hide_run=True)
    _bind(monkeypatch, adapter, repo)
    evidence, root = _v2_baseline(tmp_path, repo)
    evidence["abort_requested"] = True
    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )
    assert outcome["reconciliation_outcome"] == "native_unavailable"
    assert adapter.cancel_calls == []
    assert adapter.send_calls == 0


def test_v2_journal_rejects_null_bool_and_tampered_bindings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    del monkeypatch
    root = tmp_path / "artifacts" / "runs" / ("d" * 64)
    journal = root / cursor_sdk_attempt_journal_rel("att-" + "8" * 32)
    base = {
        "attempt_id": "att-" + "8" * 32,
        "dispatch_id": "dsp-att-" + "8" * 32,
        "conversation_owner_run_id": "project-sdk-run",
        "conversation_key": cursor_conversation_key("project-sdk-run"),
        "agent_id": AGENT,
        "prompt_sha256": "a" * 64,
    }
    with pytest.raises(ValueError, match="null"):
        append_sdk_journal(
            journal,
            kind="run_inventory_baseline",
            payload={**base, "run_ids": [CREATION], "runs": None},
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
    append_sdk_journal(
        journal,
        kind="run_inventory_baseline",
        payload={
            **base,
            "run_ids": [CREATION],
            "runs": [{"run_id": CREATION, "status": "queued"}],
            "creation_run_ids": [CREATION],
            "message_count": True,
            "authorized_send_count": 0,
        },
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    with pytest.raises(ValueError, match="integer"):
        authenticate_sdk_journal(
            journal,
            expected_sha256=sha256_file(journal),
            operation="send",
            expected_binding={"attempt_id": base["attempt_id"]},
        )
    legacy = root / cursor_sdk_attempt_journal_rel("att-" + "9" * 32)
    append_sdk_journal(legacy, kind="run_inventory_baseline", payload={**base, "run_ids": []})
    append_sdk_journal(legacy, kind="send_intent", payload=base)
    append_sdk_journal(legacy, kind="native_run_bound", payload={**base, "native_run_id": PROMPT})
    append_sdk_journal(
        legacy, kind="terminal", payload={**base, "native_run_id": PROMPT, "outcome": "success"}
    )
    records = authenticate_sdk_journal(
        legacy,
        expected_sha256=sha256_file(legacy),
        operation="send",
        expected_binding={
            "attempt_id": str(base["attempt_id"]),
            "dispatch_id": str(base["dispatch_id"]),
        },
    )
    assert [item["schema_version"] for item in records] == [JOURNAL_SCHEMA_VERSION] * 4
    legacy.write_text(legacy.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        authenticate_sdk_journal(
            legacy,
            expected_sha256="b" * 64,
            operation="send",
            expected_binding={"attempt_id": str(base["attempt_id"])},
        )


def _reconcile(
    monkeypatch: pytest.MonkeyPatch,
    turn_evidence: dict[str, object],
    run_root: Path,
    outcome: dict[str, object],
    adapter: ScriptedSdk,
) -> dict[str, object]:
    _bind(monkeypatch, adapter, Path(str(turn_evidence["repository_root"])))
    journal = run_root / str(outcome["journal_path"])
    evidence = {
        **turn_evidence,
        "effect_kind": "cursor.reconcile_sdk_run",
        "attempt_id": "att-" + "7" * 32,
        "dispatch_id": "fx-reconcile",
        "source_attempt_id": turn_evidence["attempt_id"],
        "source_dispatch_id": turn_evidence["dispatch_id"],
        "source_journal_path": outcome["journal_path"],
        "source_journal_sha256": sha256_file(journal),
        "native_run_id": None,
        "recovery_key": "c" * 64,
    }
    return _run_sdk_reconciliation_operation(
        evidence, run_root, attempt_id=str(evidence["attempt_id"])
    )


def _record(
    run_root: Path, outcome: dict[str, object]
) -> CursorSdkReconciliationRecordV1 | CursorSdkReconciliationRecordV2:
    payload = (run_root / str(outcome["reconciliation_record_path"])).read_bytes()
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise AssertionError("reconciliation record is not an object")
    model = (
        CursorSdkReconciliationRecordV2
        if raw.get("schema_version") == 2
        else CursorSdkReconciliationRecordV1
    )
    return model.model_validate(raw)


def _prior_anchor(evidence: dict[str, object], outcome: dict[str, object]) -> dict[str, str]:
    return {
        "prior_authorized_journal_path": str(outcome["journal_path"]),
        "prior_authorized_journal_sha256": str(outcome["journal_sha256"]),
        "prior_authorized_attempt_id": str(evidence["attempt_id"]),
        "prior_authorized_dispatch_id": str(evidence["dispatch_id"]),
        "prior_authorized_prompt_sha256": str(evidence["prompt_sha256"]),
    }


def _v2_baseline(tmp_path: Path, repo: Path) -> tuple[dict[str, object], Path]:
    run_root = tmp_path / "artifacts" / "runs" / ("e" * 64)
    attempt_id = "att-" + "4" * 32
    evidence = _evidence(repo, effect_kind="cursor.reconcile_sdk_run", attempt_id="att-" + "3" * 32)
    prompt_rel = "prompts/turn.txt"
    prompt = run_root / prompt_rel
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("continue exactly", encoding="utf-8")
    prompt_sha = __import__("hashlib").sha256(b"continue exactly").hexdigest()
    journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
    base = {
        "attempt_id": attempt_id,
        "dispatch_id": "dsp-source",
        "conversation_owner_run_id": evidence["conversation_owner_run_id"],
        "conversation_key": evidence["conversation_key"],
        "agent_id": AGENT,
        "prompt_sha256": prompt_sha,
    }
    append_sdk_journal(
        run_root / journal_rel,
        kind="run_inventory_baseline",
        payload={
            **base,
            "run_ids": [CREATION],
            "runs": [{"run_id": CREATION, "status": "queued"}],
            "creation_run_ids": [CREATION],
            "message_count": 0,
            "authorized_send_count": 0,
        },
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    append_sdk_journal(
        run_root / journal_rel,
        kind="terminal",
        payload={**base, "outcome": "uncertain", "failure_kind": "cursor_sdk_worker_lost"},
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT,
            "prompt_path": prompt_rel,
            "prompt_sha256": prompt_sha,
            "source_attempt_id": attempt_id,
            "source_dispatch_id": "dsp-source",
            "source_journal_path": journal_rel,
            "source_journal_sha256": sha256_file(run_root / journal_rel),
            "native_run_id": None,
            "recovery_key": "d" * 64,
        }
    )
    return evidence, run_root


def test_absent_create_journal_does_not_infer_creation_readiness(tmp_path: Path) -> None:
    run_root = tmp_path / "successor"
    identity_rel = "cursor/sdk-identity.json"
    identity_path = run_root / identity_rel
    identity_path.parent.mkdir(parents=True)
    identity_path.write_text(
        json.dumps(
            {
                "agent_id": AGENT,
                "create_journal_path": (
                    "cursor/sdk-attempts/att-" + "1" * 32 + "/dispatch-journal.jsonl"
                ),
                "create_journal_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    evidence = {
        "identity_artifact_path": identity_rel,
        "identity_artifact_sha256": sha256_file(identity_path),
        "conversation_owner_run_id": "successor-run",
        "conversation_key": "successor-key",
    }
    assert _load_creation_inventory(evidence, run_root) is None
    evidence["identity_artifact_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="identity artifact digest mismatch"):
        _load_creation_inventory(evidence, run_root)


def test_authorized_prompt_lets_the_same_agent_correct_without_another_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    first = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert first["native_run_id"] == PROMPT
    assert adapter.cancel_calls == [CREATION]
    adapter.messages = 1
    prompt_rel = "prompts/cursor-fix.txt"
    (run_root / prompt_rel).write_text("fix exactly", encoding="utf-8")
    second_attempt = "att-" + "3" * 32
    second = dict(evidence)
    second.update(
        {
            "attempt_id": second_attempt,
            "iteration": 2,
            "prompt_path": prompt_rel,
            "prompt_sha256": hashlib.sha256(b"fix exactly").hexdigest(),
            **_prior_anchor(evidence, first),
        }
    )
    outcome = _run_sdk_turn(second, run_root, attempt_id=second_attempt)
    assert outcome["native_run_id"] == "run-019abc00-1111-4222-8333-555555555556"
    assert adapter.cancel_calls == [CREATION]
    assert adapter.send_calls == 2
    assert "failure_kind" not in outcome


def test_changed_prior_journal_digest_rejects_the_correction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    first = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    journal = run_root / str(first["journal_path"])
    prompt_sha = str(evidence["prompt_sha256"])
    journal.write_text(
        journal.read_text(encoding="utf-8").replace(prompt_sha, "f" * 64), encoding="utf-8"
    )
    assert sha256_file(journal) != first["journal_sha256"]
    second_attempt = "att-" + "3" * 32
    second = dict(evidence)
    second.update(
        {
            "attempt_id": second_attempt,
            "iteration": 2,
            "prompt_path": "prompts/cursor-fix.txt",
            "prompt_sha256": hashlib.sha256(b"fix exactly").hexdigest(),
            **_prior_anchor(evidence, first),
        }
    )
    (run_root / "prompts/cursor-fix.txt").write_text("fix exactly", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        _run_sdk_turn(second, run_root, attempt_id=second_attempt)
    assert adapter.send_calls == 1


def test_orphan_journal_and_missing_anchor_do_not_authorize_another_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    first = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert first["native_run_id"] == PROMPT
    orphan_attempt = "att-" + "8" * 32
    orphan = run_root / cursor_sdk_attempt_journal_rel(orphan_attempt)
    binding = {
        "attempt_id": orphan_attempt,
        "dispatch_id": "dsp-orphan",
        "conversation_owner_run_id": evidence["conversation_owner_run_id"],
        "conversation_key": evidence["conversation_key"],
        "agent_id": AGENT,
        "prompt_sha256": evidence["prompt_sha256"],
    }
    append_sdk_journal(orphan, kind="run_inventory_baseline", payload={**binding, "run_ids": []})
    append_sdk_journal(orphan, kind="send_intent", payload=binding)
    append_sdk_journal(
        orphan,
        kind="terminal",
        payload={**binding, "outcome": "success"},
    )
    adapter.messages = 1
    second_attempt = "att-" + "3" * 32
    second = dict(evidence)
    second.update({"attempt_id": second_attempt, "iteration": 2})
    outcome = _run_sdk_turn(second, run_root, attempt_id=second_attempt)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.send_calls == 1
    assert adapter.cancel_calls == [CREATION]


def test_wrong_prior_binding_rejects_the_source_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    first = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    second_attempt = "att-" + "3" * 32
    anchor = _prior_anchor(evidence, first)
    anchor["prior_authorized_dispatch_id"] = "dsp-wrong"
    second = dict(evidence)
    second.update({"attempt_id": second_attempt, "iteration": 2, **anchor})
    with pytest.raises(ValueError, match="attempt binding"):
        _run_sdk_turn(second, run_root, attempt_id=second_attempt)
    assert adapter.send_calls == 1


def test_incomplete_prior_anchor_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    first = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    second_attempt = "att-" + "3" * 32
    second = dict(evidence)
    second.update(
        {
            "attempt_id": second_attempt,
            "iteration": 2,
            "prior_authorized_journal_path": first["journal_path"],
        }
    )
    with pytest.raises(ValueError, match="incomplete"):
        _run_sdk_turn(second, run_root, attempt_id=second_attempt)
    assert adapter.send_calls == 1


def test_authorized_send_keeps_the_frozen_turn_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    assert evidence["timeout_seconds"] == 60
    monkeypatch.setattr(cursor_runner, "MAX_CREATION_READINESS_SECONDS", 0.05)
    original = adapter.send

    def delayed(agent_id: str, prompt: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        outcome = original(agent_id, prompt)
        for item in adapter.runs:
            if item["id"] == PROMPT:
                item["status"] = "running"
        time.sleep(0.2)
        return outcome

    adapter.send = delayed  # type: ignore[method-assign]
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome.get("failure_kind") != "cursor_sdk_send_uncertain"
    assert outcome["has_completion_signal"] is True
    assert outcome["native_run_id"] == PROMPT
    assert adapter.send_calls == 1
    assert evidence["timeout_seconds"] == 60


def test_slow_observation_holds_before_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    clock, _sleeps = _clock(monkeypatch)
    original = cursor_runner._observe_public_agent

    def slow(*args: object, **kwargs: object) -> object:
        clock["now"] += 12
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cursor_runner, "_observe_public_agent", slow)
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.cancel_calls == []
    assert adapter.send_calls == 0


def test_slow_cancellation_holds_before_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    clock, _sleeps = _clock(monkeypatch)
    original = adapter.cancel

    def slow(agent_id: str, run_id: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        clock["now"] += 12
        return original(agent_id, run_id)

    adapter.cancel = slow  # type: ignore[method-assign]
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.cancel_calls == [CREATION]
    assert adapter.send_calls == 0


def test_slow_confirmation_holds_before_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    clock, _sleeps = _clock(monkeypatch)
    original = adapter.get_run

    def slow(agent_id: str, run_id: str) -> CursorSdkOutcome[CursorSdkTerminalRun]:
        clock["now"] += 12
        return original(agent_id, run_id)

    adapter.get_run = slow  # type: ignore[method-assign]
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert outcome["failure_kind"] == "cursor_sdk_creation_readiness_hold"
    assert adapter.cancel_calls == [CREATION]
    assert adapter.send_calls == 0


def test_slow_rejection_does_not_retry_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=("typed", "typed"))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    clock, _sleeps = _clock(monkeypatch)
    original = adapter.send

    def slow(agent_id: str, prompt: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        clock["now"] += 12
        return original(agent_id, prompt)

    adapter.send = slow  # type: ignore[method-assign]
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert adapter.send_calls == 1
    assert outcome["failure_kind"] in {
        "cursor_sdk_creation_readiness_hold",
        "cursor_sdk_send_uncertain",
    }


def test_readiness_budget_refuses_a_call_and_restores_the_outer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, _sleeps = _clock(monkeypatch)
    clock["now"] = 11
    with pytest.raises(_ReadinessBudgetExceeded), _bound_readiness_call(10):
        raise AssertionError("readiness call ran after its budget")
    signal.setitimer(signal.ITIMER_REAL, 30, 0)
    clock["now"] = 0
    try:
        with _bound_readiness_call(5):
            assert signal.getitimer(signal.ITIMER_REAL)[0] <= 5
    finally:
        remaining = signal.getitimer(signal.ITIMER_REAL)[0]
        signal.setitimer(signal.ITIMER_REAL, 0, 0)
    assert remaining > 25


@pytest.mark.parametrize("prefix", sorted(_SEALABLE_SEND_PREFIXES_V2))
def test_every_supported_v2_prefix_seals_for_recovery(
    tmp_path: Path, prefix: tuple[str, ...]
) -> None:
    root = tmp_path / "artifacts" / "runs" / ("f" * 64)
    attempt_id = "att-" + "8" * 32
    journal = root / cursor_sdk_attempt_journal_rel(attempt_id)
    base = {
        "attempt_id": attempt_id,
        "dispatch_id": "dsp-source",
        "conversation_owner_run_id": "project-sdk-run",
        "conversation_key": "a" * 64,
        "agent_id": AGENT,
        "prompt_sha256": "b" * 64,
    }
    observation = 0
    dispatch = 0
    for kind in prefix:
        payload = dict(base)
        if kind == "run_inventory_baseline":
            payload.update(
                {
                    "run_ids": [CREATION],
                    "runs": [{"run_id": CREATION, "status": "queued"}],
                    "creation_run_ids": [CREATION],
                    "message_count": 0,
                    "authorized_send_count": 0,
                }
            )
        elif kind in {"readiness_observation", "readiness_clear", "readiness_hold"}:
            observation += 1
            payload.update(
                {
                    "observation_ordinal": observation,
                    "runs": [{"run_id": CREATION, "status": "queued"}],
                    "message_count": 0,
                    "reason": "sealed prefix audit",
                }
            )
        elif kind == "creation_blocker_cancel_intent":
            payload["run_id"] = CREATION
        elif kind == "creation_blocker_cancel_confirmed":
            payload.update({"run_id": CREATION, "status": "cancelled"})
        elif kind == "send_intent":
            dispatch += 1
            payload["dispatch_ordinal"] = dispatch
        elif kind == "send_rejected":
            payload.update({"dispatch_ordinal": dispatch, "evidence_class": "typed"})
        elif kind == "native_run_bound":
            payload["native_run_id"] = PROMPT
        append_sdk_journal(
            journal, kind=kind, payload=payload, schema_version=JOURNAL_SCHEMA_VERSION_V2
        )
    assert seal_interrupted_sdk_send_journal(journal) is True
    records = authenticate_sdk_recovery_journal(
        journal,
        expected_sha256=sha256_file(journal),
        expected_binding=base,
    )
    assert tuple(str(item["kind"]) for item in records) == (*prefix, "terminal")


def _published_reader_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], Path, str, str]:
    repo = _repo(tmp_path)
    sdk = ScriptedSdk([{"id": CREATION, "status": "cancelled"}])
    _bind(monkeypatch, sdk, repo)
    evidence, root = _v2_baseline(tmp_path, repo)
    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )
    record_path = str(outcome["reconciliation_record_path"])
    record = json.loads((root / record_path).read_text(encoding="utf-8"))
    assert record["schema_version"] == 2 and record["outcome"] == "prompt_unsent"
    return evidence, root, record_path, sha256_file(root / record_path)


def _pending_reader(
    evidence: dict[str, object],
    root: Path,
    *,
    historical_path: str | None = None,
    historical_sha: str | None = None,
    attempts: dict[str, dict[str, object]] | None = None,
    effects: dict[str, dict[str, object]] | None = None,
) -> tuple[str, str] | None:
    intent = CursorSdkReconciliationIntentV1(
        run_id=str(evidence["run_id"]),
        source_attempt_id=str(evidence["source_attempt_id"]),
        source_dispatch_id=str(evidence["source_dispatch_id"]),
        turn_kind="initial",
        iteration=1,
        conversation_owner_run_id=str(evidence["conversation_owner_run_id"]),
        conversation_key=str(evidence["conversation_key"]),
        agent_id=str(evidence["agent_id"]),
        prompt_path="prompts/initial.txt",
        prompt_sha256=str(evidence["prompt_sha256"]),
        source_journal_path=str(evidence["source_journal_path"]),
        source_journal_sha256=str(evidence["source_journal_sha256"]),
        identity_artifact_path="cursor/sdk-identity.json",
        identity_artifact_sha256="a" * 64,
        source_state={},
        reviews_completed=0,
        submitted_max_review_iterations=2,
        effective_review_ceiling=2,
        failure_class="worker_lost",
    )
    text = intent.canonical_bytes().decode()
    key = "b" * 64
    row = {
        "run_id": evidence["run_id"],
        "status": "pending",
        "intent_payload": text,
        "intent_payload_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_attempt_id": evidence["source_attempt_id"],
        "source_dispatch_id": evidence["source_dispatch_id"],
        "result_artifact_path": historical_path,
        "result_sha256": historical_sha,
    }

    class Store:
        def get_cursor_sdk_reconciliation_by_key(self, conn, *, recovery_key):
            assert recovery_key == key
            return row

        def get_attempt_by_id(self, conn, attempt_id):
            assert attempts is not None
            return attempts.get(attempt_id)

        def get_effect_by_dispatch_id(self, conn, dispatch_id):
            assert effects is not None
            return effects.get(dispatch_id)

    class Artifacts:
        def run_root(self, run_id):
            assert run_id == evidence["run_id"]
            return root

        def read_verified_bytes(self, run_id, path, *, expected_sha256):
            assert run_id == evidence["run_id"]
            payload = (root / path).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == expected_sha256
            return payload

    return authenticated_published_reconciliation_result(
        Store(),  # type: ignore[arg-type]
        Artifacts(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        run_id=str(evidence["run_id"]),
        recovery_key=key,
        reconciliation_attempt_id=str(evidence["attempt_id"]),
        reconciliation_dispatch_id=str(evidence["dispatch_id"]),
    )


def test_published_v2_record_is_recovered_when_its_envelope_is_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence, root, record_path, digest = _published_reader_fixture(tmp_path, monkeypatch)
    assert _pending_reader(evidence, root) == (record_path, digest)


def test_published_v2_conflicts_with_a_same_attempt_v1_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence, root, record_path, _digest = _published_reader_fixture(tmp_path, monkeypatch)
    v1_path = root / cursor_sdk_reconciliation_record_rel(
        str(evidence["source_attempt_id"]),
        str(evidence["attempt_id"]),
        schema_version=1,
    )
    v1_path.write_bytes((root / record_path).read_bytes())
    with pytest.raises(SchedulerEngineError, match="conflicting versioned records"):
        _pending_reader(evidence, root)


def test_tampered_published_v2_record_is_not_recovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence, root, record_path, _digest = _published_reader_fixture(tmp_path, monkeypatch)
    path = root / record_path
    path.write_bytes(path.read_bytes().replace(b"prompt_unsent", b"prompt_unsenx"))
    with pytest.raises(SchedulerEngineError, match="failed authentication"):
        _pending_reader(evidence, root)


def test_historical_v1_record_is_not_the_current_attempt_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence, root, record_path, _digest = _published_reader_fixture(tmp_path, monkeypatch)
    historical_attempt = "att-" + "9" * 32
    raw = json.loads((root / record_path).read_text(encoding="utf-8"))
    raw.update(
        {
            "schema_version": 1,
            "outcome": "ambiguous_missing",
            "reconciliation_attempt_id": historical_attempt,
            "reconciliation_dispatch_id": "dsp-historical",
        }
    )
    historical = CursorSdkReconciliationRecordV1.model_validate(raw)
    historical_rel = cursor_sdk_reconciliation_record_rel(
        str(evidence["source_attempt_id"]),
        historical_attempt,
    )
    historical_path = root / historical_rel
    historical_path.parent.mkdir(parents=True, exist_ok=True)
    historical_path.write_bytes(historical.canonical_bytes())
    (root / record_path).unlink()
    current_v1 = root / cursor_sdk_reconciliation_record_rel(
        str(evidence["source_attempt_id"]),
        str(evidence["attempt_id"]),
    )
    assert not current_v1.is_file()
    assert (
        _pending_reader(
            evidence,
            root,
            historical_path=historical_rel,
            historical_sha=sha256_file(historical_path),
            attempts={
                historical_attempt: {
                    "run_id": evidence["run_id"],
                    "dispatch_id": "dsp-historical",
                    "component": "cursor",
                    "status": "failed",
                    "ingested": 1,
                }
            },
            effects={
                "dsp-historical": {
                    "run_id": evidence["run_id"],
                    "effect_kind": RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
                    "effect_payload": json.dumps({"recovery_key": "b" * 64}),
                }
            },
        )
        is None
    )
