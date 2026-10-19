"""Production worker evidence tests using an in-memory fake SDK port."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_dev_loop.scheduler.cursor_attempt_runner as cursor_runner
from ai_dev_loop.integrations.cursor_sdk.adapter import CursorSdkAdapter
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkAgentIdentity,
    CursorSdkBridgeHealth,
    CursorSdkClientConfig,
    CursorSdkEnvelope,
    CursorSdkErrorCode,
    CursorSdkFailure,
    CursorSdkOperation,
    CursorSdkOutcome,
    CursorSdkPreflight,
    CursorSdkRunIdentity,
    CursorSdkRunPage,
    CursorSdkTerminalRun,
)
from ai_dev_loop.iterations import (
    build_correction_execution_envelope,
    correction_execution_envelope_path,
    fix_prompt_path,
)
from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.scheduler.application.attempt_backend import LaunchRequest
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stdout_rel,
    parse_result_envelope,
)
from ai_dev_loop.scheduler.application.attempt_identity import unit_identity_from_attempt_id
from ai_dev_loop.scheduler.application.cursor_evidence import invocation_evidence_sha256
from ai_dev_loop.scheduler.application.cursor_sdk_journal import authenticate_sdk_journal
from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend
from ai_dev_loop.scheduler.cursor_attempt_runner import _run_sdk_create, _run_sdk_turn
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_sdk_attempt_journal_rel,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.state import sha256_bytes

AGENT_ID = "019abc00-1111-4222-8333-444444444444"
NATIVE_RUN_ID = "run-019abc00-1111-4222-8333-555555555555"


class FakeSdkAdapter:
    def __init__(
        self,
        repo: Path,
        *,
        create_uncertain: bool = False,
        send_uncertain: bool = False,
        stream_uncertain: bool = False,
        terminal_status: str = "finished",
        terminal_text: str = "exact terminal",
        envelopes: tuple[CursorSdkEnvelope, ...] | None = None,
    ) -> None:
        self.repo = repo
        self.create_uncertain = create_uncertain
        self.send_uncertain = send_uncertain
        self.stream_uncertain = stream_uncertain
        self.terminal_status = terminal_status
        self.terminal_text = terminal_text
        self.envelopes = envelopes or (
            CursorSdkEnvelope(
                offset="1",
                payload={"offset": "1", "kind": "sdk_message", "text": "partial"},
            ),
        )
        self.closed = False
        self.create_calls = 0
        self.send_calls = 0
        self.stream_calls = 0
        self.before_create: Callable[[], None] | None = None
        self.before_send: Callable[[], None] | None = None
        self.before_stream: Callable[[], None] | None = None

    def open(self):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(CursorSdkBridgeHealth("1.0.37", "fake", "v1"))

    def preflight(self):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(
            CursorSdkPreflight(
                CursorSdkBridgeHealth("1.0.37", "fake", "v1"),
                True,
                "grok-4.7",
                (("context", "256k"), ("fast", "false"), ("reasoning_effort", "high")),
            )
        )

    def create_agent(self):  # type: ignore[no-untyped-def]
        self.create_calls += 1
        if self.before_create is not None:
            self.before_create()
        if self.create_uncertain:
            return CursorSdkOutcome.failed(
                CursorSdkFailure(CursorSdkErrorCode.NETWORK, CursorSdkOperation.CREATE, None, None)
            )
        return CursorSdkOutcome.success(CursorSdkAgentIdentity(AGENT_ID))

    def resume_agent(self, agent_id: str):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(CursorSdkAgentIdentity(agent_id))

    def send(self, agent_id: str, prompt: str):  # type: ignore[no-untyped-def]
        assert prompt.strip()
        self.send_calls += 1
        if self.before_send is not None:
            self.before_send()
        if self.send_uncertain:
            return CursorSdkOutcome.failed(
                CursorSdkFailure(
                    CursorSdkErrorCode.NETWORK,
                    CursorSdkOperation.SEND,
                    agent_id,
                    None,
                )
            )
        return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id, NATIVE_RUN_ID))

    def list_runs(self, agent_id: str, *, cursor=None, limit=None):  # type: ignore[no-untyped-def]
        del cursor, limit
        assert agent_id == AGENT_ID
        return CursorSdkOutcome.success(CursorSdkRunPage(runs=(), next_cursor=None))

    def stream(self, agent_id: str, run_id: str, *, on_envelope=None, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        assert agent_id == AGENT_ID and run_id == NATIVE_RUN_ID
        self.stream_calls += 1
        if self.before_stream is not None:
            self.before_stream()
        if on_envelope is not None:
            for envelope in self.envelopes:
                on_envelope(envelope)
        if self.stream_uncertain:
            return CursorSdkOutcome.failed(
                CursorSdkFailure(
                    CursorSdkErrorCode.NETWORK,
                    CursorSdkOperation.STREAM,
                    agent_id,
                    run_id,
                )
            )
        (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        return CursorSdkOutcome.success(
            CursorSdkTerminalRun(
                agent_id,
                run_id,
                self.terminal_status,
                self.terminal_text,
                None,
            )
        )

    def close(self) -> None:
        self.closed = True


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def _evidence(repo: Path, *, effect_kind: str, attempt_id: str) -> dict[str, object]:
    run_id = "project-sdk-run"
    return {
        "schema_version": 2,
        "runtime": "sdk-local",
        "effect_kind": effect_kind,
        "repository_root": str(repo),
        "repository_git_common_dir": str(repo / ".git"),
        "repository_git_dir": str(repo / ".git"),
        "repository_branch": "master",
        "repository_initial_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "cursor_model": "grok-4.7",
        "cursor_sandbox": "enabled",
        "sdk_version": "1.0.37",
        "sdk_parameters": {
            "context": "256k",
            "fast": False,
            "reasoning_effort": "high",
        },
        "sdk_credential_ref": "cursor-test",
        "sdk_setting_sources": ["project"],
        "sdk_tool_policy": "scheduler-owned-no-shell",
        "conversation_owner_run_id": run_id,
        "conversation_key": cursor_conversation_key(run_id),
        "timeout_seconds": 60,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": f"dsp-{attempt_id}",
    }


def test_create_intent_precedes_agent_binding_and_terminal(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    adapter = FakeSdkAdapter(repo)
    attempt_id = "att-" + "1" * 32
    journal = run_root / cursor_sdk_attempt_journal_rel(attempt_id)

    def assert_create_intent_is_durable() -> None:
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [item["kind"] for item in records] == ["create_intent"]

    adapter.before_create = assert_create_intent_is_durable
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda evidence: adapter
    )
    outcome = _run_sdk_create(
        _evidence(repo, effect_kind="cursor.create_chat", attempt_id=attempt_id),
        run_root,
        attempt_id=attempt_id,
    )
    records = [
        json.loads(line) for line in (run_root / outcome["journal_path"]).read_text().splitlines()
    ]
    assert [item["kind"] for item in records] == ["create_intent", "agent_bound", "terminal"]
    assert records[1]["agent_id"] == AGENT_ID == outcome["agent_id"]
    expected_binding = {
        "attempt_id": "att-" + "1" * 32,
        "dispatch_id": "dsp-att-" + "1" * 32,
        "conversation_owner_run_id": "project-sdk-run",
        "conversation_key": cursor_conversation_key("project-sdk-run"),
    }
    authenticate_sdk_journal(
        run_root / outcome["journal_path"],
        expected_sha256=outcome["journal_sha256"],
        operation="create",
        expected_binding=expected_binding,
    )
    with pytest.raises(ValueError, match="attempt binding"):
        authenticate_sdk_journal(
            run_root / outcome["journal_path"],
            expected_sha256=outcome["journal_sha256"],
            operation="create",
            expected_binding={**expected_binding, "dispatch_id": "wrong-dispatch"},
        )
    assert adapter.closed


def test_send_intent_and_native_id_precede_bounded_terminal_evidence(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    run_root.mkdir(parents=True)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    adapter = FakeSdkAdapter(repo)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda evidence: adapter
    )
    attempt_id = "att-" + "2" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )
    journal = run_root / cursor_sdk_attempt_journal_rel(attempt_id)

    def assert_send_intent_is_durable() -> None:
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [item["kind"] for item in records] == [
            "run_inventory_baseline",
            "send_intent",
        ]

    def assert_native_run_binding_is_durable() -> None:
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [item["kind"] for item in records] == [
            "run_inventory_baseline",
            "send_intent",
            "native_run_bound",
        ]
        assert records[-1]["native_run_id"] == NATIVE_RUN_ID

    adapter.before_send = assert_send_intent_is_durable
    adapter.before_stream = assert_native_run_binding_is_durable
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    records = [
        json.loads(line) for line in (run_root / outcome["journal_path"]).read_text().splitlines()
    ]
    assert [item["kind"] for item in records] == [
        "run_inventory_baseline",
        "send_intent",
        "native_run_bound",
        "terminal",
    ]
    assert records[2]["native_run_id"] == NATIVE_RUN_ID == outcome["native_run_id"]
    assert (run_root / outcome["events_path"]).read_text().count("partial") == 1
    assert (run_root / outcome["final_response_path"]).read_text() == "exact terminal"
    metadata = json.loads((run_root / outcome["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["events_format"] == "cursor-sdk-envelope-ndjson-v1"
    assert metadata["stderr_format"] == "utf-8-text-v1"
    assert outcome["has_completion_signal"] is True
    assert outcome["events_sha256"]
    assert outcome["metadata_sha256"]
    assert outcome["stderr_sha256"]
    assert adapter.closed


def test_lost_create_and_send_results_are_terminal_uncertainty(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    create = FakeSdkAdapter(repo, create_uncertain=True)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda evidence: create
    )
    create_attempt = "att-" + "3" * 32
    create_outcome = _run_sdk_create(
        _evidence(repo, effect_kind="cursor.create_chat", attempt_id=create_attempt),
        run_root,
        attempt_id=create_attempt,
    )
    create_records = [
        json.loads(line)
        for line in (run_root / create_outcome["journal_path"]).read_text().splitlines()
    ]
    assert [record["kind"] for record in create_records] == ["create_intent", "terminal"]
    assert create_outcome["failure_kind"] == "cursor_sdk_create_uncertain"

    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    send = FakeSdkAdapter(repo, send_uncertain=True)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda evidence: send
    )
    send_attempt = "att-" + "4" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=send_attempt)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )
    send_outcome = _run_sdk_turn(evidence, run_root, attempt_id=send_attempt)
    send_records = [
        json.loads(line)
        for line in (run_root / send_outcome["journal_path"]).read_text().splitlines()
    ]
    assert [record["kind"] for record in send_records] == [
        "run_inventory_baseline",
        "send_intent",
        "terminal",
    ]
    assert send_outcome["failure_kind"] == "cursor_sdk_send_uncertain"
    assert send_outcome["events_stored_bytes"] == 0
    assert send_outcome["stderr_stored_bytes"] == 0
    assert send_outcome["events_sha256"] and send_outcome["stderr_sha256"]
    metadata = json.loads((run_root / send_outcome["metadata_path"]).read_text())
    assert metadata["capture_complete"] is False
    assert metadata["native_run_id"] is None


def test_sdk_rejects_non_finished_terminal_status(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    adapter = FakeSdkAdapter(repo, terminal_status="completed")
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda evidence: adapter)
    attempt_id = "att-" + "7" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )

    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)

    assert outcome["returncode"] == 1
    assert outcome["has_completion_signal"] is False
    assert outcome["final_response_path"] is None


def test_failed_sdk_stream_finalizes_authenticated_partial_capture(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    adapter = FakeSdkAdapter(repo, stream_uncertain=True)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda evidence: adapter)
    attempt_id = "att-" + "8" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )

    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)

    metadata = json.loads((run_root / outcome["metadata_path"]).read_text())
    assert outcome["failure_kind"] == "cursor_sdk_native_run_uncertain"
    assert outcome["events_sha256"] and outcome["metadata_sha256"]
    assert metadata["capture_complete"] is False
    assert "partial" in (run_root / outcome["events_path"]).read_text()


@pytest.mark.parametrize(
    ("barrier", "create_calls", "send_calls", "stream_calls"),
    [
        ("create_intent", 0, 0, 0),
        ("agent_bound", 1, 0, 0),
        ("send_intent", 0, 0, 0),
        ("native_run_bound", 0, 1, 0),
    ],
)
def test_sdk_persistence_failure_stops_the_next_external_effect(
    monkeypatch,
    tmp_path: Path,
    barrier: str,
    create_calls: int,
    send_calls: int,
    stream_calls: int,
) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    attempt_id = "att-" + "9" * 32
    adapter = FakeSdkAdapter(repo)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda evidence: adapter)
    real_append = cursor_runner.append_sdk_journal

    def fail_selected_barrier(path, *, kind, payload):  # type: ignore[no-untyped-def]
        if kind == barrier:
            raise OSError(f"synthetic {kind} persistence failure")
        return real_append(path, kind=kind, payload=payload)

    monkeypatch.setattr(cursor_runner, "append_sdk_journal", fail_selected_barrier)
    with pytest.raises(OSError, match="persistence failure"):
        if barrier in {"create_intent", "agent_bound"}:
            _run_sdk_create(
                _evidence(repo, effect_kind="cursor.create_chat", attempt_id=attempt_id),
                run_root,
                attempt_id=attempt_id,
            )
        else:
            prompt = run_root / "prompts/cursor-initial.txt"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("implement exactly", encoding="utf-8")
            evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
            evidence.update(
                {
                    "iteration": 1,
                    "agent_id": AGENT_ID,
                    "prompt_path": "prompts/cursor-initial.txt",
                    "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
                }
            )
            _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert (adapter.create_calls, adapter.send_calls, adapter.stream_calls) == (
        create_calls,
        send_calls,
        stream_calls,
    )


def _runner_argv(
    *,
    artifact_root: Path,
    run_id: str,
    attempt_id: str,
    evidence: dict[str, object],
) -> list[str]:
    run_root = run_artifact_root(artifact_root, run_id)
    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    pinned = invocation_evidence_sha256(evidence)
    unit_identity = unit_identity_from_attempt_id(attempt_id)
    launch_nonce = "sdk-correction-nonce"
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": evidence["dispatch_id"],
            "run_id": run_id,
            "unit_identity": unit_identity,
            "launch_nonce": launch_nonce,
            "effect_kind": evidence["effect_kind"],
            "invocation_evidence_sha256": pinned,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        "--run-id",
        run_id,
        "--attempt-id",
        attempt_id,
        "--unit-identity",
        unit_identity,
        "--artifact-root",
        str(artifact_root),
        "--invocation-evidence-sha256",
        pinned,
        "--launch-intent-sha256",
        payload_sha256(launch_intent),
        "--launch-nonce",
        launch_nonce,
        "--dispatch-id",
        str(evidence["dispatch_id"]),
        "--effect-kind",
        str(evidence["effect_kind"]),
    ]


@pytest.mark.parametrize("mutation", ["unchanged", "missing_fix", "tampered_fix", "staged_drift"])
def test_sdk_correction_artifacts_are_authenticated_at_worker_entry_before_send(
    monkeypatch,
    tmp_path: Path,
    mutation: str,
) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    staged = repo / "correction.txt"
    staged.write_text("approved staged correction\n", encoding="utf-8")
    subprocess.run(["git", "add", "correction.txt"], cwd=repo, check=True)
    artifact_root = tmp_path / "protected"
    run_id = "project-sdk-correction"
    run_root = run_artifact_root(artifact_root, run_id)
    fix_rel = fix_prompt_path(1)
    fix_text = "apply the exact correction"
    fix = run_root / fix_rel
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(fix_text, encoding="utf-8")
    envelope_rel = correction_execution_envelope_path(1)
    envelope_text = build_correction_execution_envelope(fix_text)
    envelope = run_root / envelope_rel
    envelope.write_text(envelope_text, encoding="utf-8")
    patch_rel = "git/diffs/01.patch"
    patch_path = run_root / patch_rel
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(
        subprocess.check_output(["git", "diff", "--cached"], cwd=repo, text=True),
        encoding="utf-8",
    )
    attempt_id = "att-" + "a" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "run_id": run_id,
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": envelope_rel,
            "prompt_sha256": sha256_bytes(envelope_text.encode("utf-8")),
            "fix_prompt_path": fix_rel,
            "fix_prompt_sha256": sha256_bytes(fix_text.encode("utf-8")),
            "staged_patch_path": patch_rel,
            "staged_patch_sha256": sha256_bytes(patch_path.read_bytes()),
        }
    )
    adapter = FakeSdkAdapter(repo)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda binding: adapter)
    if mutation == "missing_fix":
        fix.unlink()
    elif mutation == "tampered_fix":
        fix.write_text("different raw fix", encoding="utf-8")
    elif mutation == "staged_drift":
        staged.write_text("unapproved staged drift\n", encoding="utf-8")
        subprocess.run(["git", "add", "correction.txt"], cwd=repo, check=True)

    exit_code = cursor_runner.main(
        _runner_argv(
            artifact_root=artifact_root,
            run_id=run_id,
            attempt_id=attempt_id,
            evidence=evidence,
        )
    )

    if mutation == "unchanged":
        assert exit_code == 0
        assert adapter.send_calls == 1
    else:
        assert exit_code == 1
        assert adapter.send_calls == 0
        outcome = json.loads(
            (run_root / attempt_stdout_rel(attempt_id)).read_text(encoding="utf-8")
        )
        assert outcome["failure_kind"] == "pre_execution_guard_failed"


def test_sdk_worker_enforces_frozen_turn_deadline_and_binds_partial_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    class BlockingAdapter(FakeSdkAdapter):
        def stream(self, agent_id, run_id, *, on_envelope=None, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            self.stream_calls += 1
            if on_envelope is not None:
                on_envelope(self.envelopes[0])
            while True:
                time.sleep(0.1)

    repo = _repo(tmp_path)
    artifact_root = tmp_path / "protected"
    run_id = "project-sdk-timeout"
    run_root = run_artifact_root(artifact_root, run_id)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    attempt_id = "att-" + "b" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "run_id": run_id,
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "timeout_seconds": 1,
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": sha256_bytes(b"implement exactly"),
        }
    )
    adapter = BlockingAdapter(repo)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda binding: adapter)
    started = time.monotonic()

    exit_code = cursor_runner.main(
        _runner_argv(
            artifact_root=artifact_root,
            run_id=run_id,
            attempt_id=attempt_id,
            evidence=evidence,
        )
    )

    assert exit_code == 124
    assert time.monotonic() - started < 3
    outcome = json.loads((run_root / attempt_stdout_rel(attempt_id)).read_text())
    assert outcome["timed_out"] is True
    assert outcome["events_sha256"] and outcome["metadata_sha256"]
    assert outcome["failure_kind"] == "cursor_sdk_native_run_uncertain"
    envelope = parse_result_envelope((run_root / attempt_result_rel(attempt_id)).read_bytes())
    assert envelope.exit_code == 124
    assert envelope.termination_class == "timeout"


def test_sdk_worker_enforces_frozen_create_deadline_and_preserves_uncertainty(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    class BlockingCreateAdapter(FakeSdkAdapter):
        def create_agent(self):  # type: ignore[no-untyped-def]
            self.create_calls += 1
            while True:
                time.sleep(0.1)

    repo = _repo(tmp_path)
    artifact_root = tmp_path / "protected"
    run_id = "project-sdk-create-timeout"
    attempt_id = "att-" + "c" * 32
    evidence = _evidence(repo, effect_kind="cursor.create_chat", attempt_id=attempt_id)
    evidence.update(
        {
            "run_id": run_id,
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "timeout_seconds": 1,
        }
    )
    adapter = BlockingCreateAdapter(repo)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda binding: adapter)

    exit_code = cursor_runner.main(
        _runner_argv(
            artifact_root=artifact_root,
            run_id=run_id,
            attempt_id=attempt_id,
            evidence=evidence,
        )
    )

    run_root = run_artifact_root(artifact_root, run_id)
    outcome = json.loads((run_root / attempt_stdout_rel(attempt_id)).read_text())
    assert exit_code == 124 and adapter.create_calls == 1
    assert outcome["timed_out"] is True
    assert outcome["failure_kind"] == "cursor_sdk_create_uncertain"
    assert outcome["journal_sha256"]


def test_sdk_capture_is_bounded_and_adapter_redacts_secret_sentinels(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    run_root.mkdir(parents=True)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    adapter = FakeSdkAdapter(
        repo,
        envelopes=(
            CursorSdkEnvelope(offset="1", payload={"offset": "1", "text": "small"}),
            CursorSdkEnvelope(offset="2", payload={"offset": "2", "text": "x" * 512}),
        ),
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner.MAX_CURSOR_SDK_EVENT_BYTES", 150
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda evidence: adapter
    )
    attempt_id = "att-" + "5" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    captured = (run_root / outcome["events_path"]).read_text(encoding="utf-8")
    assert outcome["events_truncated"] is True
    assert "small" in captured and "x" * 128 not in captured

    secret = "synthetic-secret-sentinel"
    sdk_adapter = CursorSdkAdapter(
        CursorSdkClientConfig(
            sdk_version="1.0.37",
            workspace=repo,
            state_root=tmp_path / "state",
            store_root=tmp_path / "store",
            model="grok-4.7",
            parameters=(),
            credential_ref="cursor-test",
            setting_sources=("project",),
            sandbox="enabled",
        ),
        repository_root=repo,
    )
    sdk_adapter._api_key = secret
    safe = sdk_adapter._envelope(
        SimpleNamespace(
            offset="3",
            token="drop-me",
            nested=SimpleNamespace(
                message=f"prefix {secret} suffix",
                ordinary="retained",
                auth_token="nested-drop-me",
            ),
        )
    )
    rendered = json.dumps(safe.payload)
    assert secret not in rendered and "drop-me" not in rendered
    assert "retained" in rendered and "[redacted]" in rendered


def test_systemd_backend_owns_and_stops_the_complete_attempt_control_group(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    alive: set[str] = set()
    control_group_owned = False
    attempt_id = "att-" + "6" * 32
    unit = unit_identity_from_attempt_id(attempt_id)

    def runner(args: list[str], **kwargs: object) -> ProcessResult:
        nonlocal control_group_owned
        del kwargs
        calls.append(list(args))
        if args[0] == "systemd-run":
            control_group_owned = "--property=KillMode=control-group" in args
            alive.update({"launcher", "bridge-child", "tool-grandchild"})
        if args[:3] == ["systemctl", "--user", "stop"]:
            alive.discard("launcher")
            if control_group_owned:
                alive.clear()
        if "show" in args:
            return ProcessResult(
                args=args,
                returncode=0,
                stdout=(f"Id={unit}\nLoadState=loaded\nActiveState=active\nSubState=running\n"),
                stderr="",
            )
        return ProcessResult(args=args, returncode=0, stdout="", stderr="")

    backend = SystemdUserBackend(runner=runner)
    backend.launch(
        LaunchRequest(
            attempt_id=attempt_id,
            unit_identity=unit,
            working_directory=tmp_path,
            agent_argv=["fake-worker", "--spawns-bridge-and-tool-descendant"],
            lock_path=tmp_path / "attempt.lock",
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            result_envelope_path=tmp_path / "result.json",
            execution_timeout_seconds=60,
        )
    )
    backend.terminate(unit_identity=unit, attempt_id=attempt_id)
    assert "--property=KillMode=control-group" in calls[0]
    assert "--property=RuntimeMaxSec=360" in calls[0]
    assert calls[-1] == ["systemctl", "--user", "stop", unit]
    assert alive == set()
