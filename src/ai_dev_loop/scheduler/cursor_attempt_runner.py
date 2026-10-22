"""Detached Cursor attempt runner for scheduler systemd/fake backends."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal

from ai_dev_loop.config import CursorSdkSection
from ai_dev_loop.integrations.cursor_sdk.adapter import (
    CursorSdkAdapter,
    CursorSdkOperationBudgetExhausted,
    bind_cursor_sdk_operation_deadline,
    build_cursor_sdk_client_config,
    reset_cursor_sdk_operation_deadline,
)
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkEnvelope,
    CursorSdkErrorCode,
    CursorSdkFailure,
    CursorSdkOperation,
    CursorSdkOutcome,
    CursorSdkRunIdentity,
    CursorSdkTerminalRun,
)
from ai_dev_loop.paths import CursorSdkPathError, cursor_conversation_key
from ai_dev_loop.runners.cursor_output import (
    capture_cursor_output_fingerprint,
)
from ai_dev_loop.runners.probes import capture_git_status
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_paths import prepare_attempt_output_paths
from ai_dev_loop.scheduler.application.cursor_evidence import (
    authenticate_pinned_invocation_evidence,
    verify_pre_execution_cursor_guards,
)
from ai_dev_loop.scheduler.application.cursor_sdk_journal import (
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_SCHEMA_VERSION_V2,
    append_sdk_journal,
    authenticate_sdk_journal,
    authenticate_sdk_recovery_journal,
)
from ai_dev_loop.scheduler.application.cursor_sdk_progress import (
    NATIVE_HANDLE_BOUND,
    SEND_AUTHORIZED,
    TERMINAL_OBSERVED,
    append_sdk_wait_progress,
    format_sdk_wait_log,
    inspect_sdk_wait,
    seal_interrupted_sdk_wait_progress,
)
from ai_dev_loop.scheduler.application.token_usage_capture import (
    CursorUsageEvent,
    capture_cursor_usage_evidence_safely,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    CURSOR_SDK_CAPTURE_SCHEMA_VERSION,
    CURSOR_SDK_EVENT_STREAM_FORMAT,
    CURSOR_SDK_TEXT_STREAM_FORMAT,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_final_rel,
    cursor_attempt_stderr_rel,
    cursor_sdk_attempt_events_rel,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_attempt_metadata_rel,
    cursor_sdk_reconciliation_record_rel,
    cursor_sdk_wait_progress_rel,
    git_status_after_cursor_rel,
    git_status_before_cursor_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_readiness import (
    MAX_CREATION_READINESS_OBSERVATIONS,
    MAX_CREATION_READINESS_SECONDS,
    TERMINAL_PUBLIC_RUN_STATUSES,
    CreationInventoryFact,
    PublicRunFact,
    classify_creation_readiness,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationRecordV1,
    CursorSdkReconciliationRecordV2,
)
from ai_dev_loop.scheduler.domain.effects import CURSOR_ATTEMPT_EFFECT_KINDS
from ai_dev_loop.scheduler.infrastructure.paths import (
    resolve_run_relative_path,
    run_artifact_root,
)
from ai_dev_loop.state import (
    RUN_STATE_SCHEMA_VERSION_FRESH,
    CodexState,
    CursorState,
    FreshCodexReviewerBinding,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_json,
    atomic_write_text,
)

MAX_CURSOR_SDK_EVENT_BYTES = 8 * 1024 * 1024
MAX_CURSOR_SDK_RECONCILIATION_RUNS = 1_000


class CursorSdkWorkerInterrupt(BaseException):
    """Deterministic test barrier after a durable journal record."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class CursorSdkOperationTimeout(BaseException):
    """Dedicated worker deadline that SDK exception translation cannot consume."""

    def __init__(
        self,
        message: str,
        *,
        capture_payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.capture_payload = {} if capture_payload is None else capture_payload


_OPERATION_DESCENDANTS: list[int] = []
_DEADLINE_EXCEPTIONS = (CursorSdkOperationTimeout, CursorSdkOperationBudgetExhausted)


def register_sdk_operation_descendant(pid: int) -> None:
    """Track one process group started for the current attempt.

    The worker terminates these groups when the frozen deadline fires. This
    does not cancel native runs or widen the retry policy.
    """

    if pid <= 1 or pid == os.getpid():
        raise ValueError("Cursor SDK descendant pid is invalid")
    _OPERATION_DESCENDANTS.append(pid)


def terminate_sdk_operation_descendants() -> None:
    """SIGKILL every registered descendant process group."""

    pids = list(_OPERATION_DESCENDANTS)
    _OPERATION_DESCENDANTS.clear()
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                continue


@contextmanager
def _sdk_operation_deadline(evidence: dict[str, object]) -> Iterator[None]:
    timeout_seconds = _binding_float(evidence, "timeout_seconds")
    if timeout_seconds <= 0:
        raise ValueError("Cursor SDK timeout_seconds must be positive")
    previous_handler = signal.getsignal(signal.SIGALRM)
    capture_payload: dict[str, object] = {}
    deadline_token = bind_cursor_sdk_operation_deadline(time.monotonic() + timeout_seconds)
    _OPERATION_DESCENDANTS.clear()

    def deadline_expired(_signum: int, _frame: FrameType | None) -> None:
        raise CursorSdkOperationTimeout(
            "Cursor SDK operation exceeded its frozen deadline",
            capture_payload=capture_payload,
        )

    signal.signal(signal.SIGALRM, deadline_expired)
    # Repeating delivery also bounds a bridge close that starts while unwinding
    # the first deadline exception. Resume, send, and stream do not reinstall it.
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds, min(timeout_seconds, 0.25))
    expired = False
    try:
        yield
    except _DEADLINE_EXCEPTIONS:
        expired = True
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        reset_cursor_sdk_operation_deadline(deadline_token)
        if expired:
            terminate_sdk_operation_descendants()


def _structured_error_records(stdout: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or event.get("event") or "")
        if event_type in {"error", "failure"}:
            records.append(event)
    return records


def _binding_int(binding: dict[str, object], key: str) -> int:
    value = binding[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"binding[{key!r}] must be numeric")
    return int(value)


def _binding_float(binding: dict[str, object], key: str) -> float:
    value = binding[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"binding[{key!r}] must be numeric")
    return float(value)


def _run_state_from_binding(binding: dict[str, object]) -> RunState:
    now = datetime.now(UTC)
    return RunState(
        schema_version=RUN_STATE_SCHEMA_VERSION_FRESH,
        run_id="cursor-attempt",
        status=RunStatus.STAGING,
        created_at=now,
        updated_at=now,
        project=ProjectRef(name="cursor-attempt"),
        repository=RepositoryState(
            root=str(binding["repository_root"]),
            git_common_dir=str(binding["repository_git_common_dir"]),
            git_dir=str(binding["repository_git_dir"]),
            branch=str(binding["repository_branch"]),
            initial_head=str(binding["repository_initial_head"]),
            baseline_status_path="",
        ),
        plan=PlanState(repository_path="", snapshot_path="", sha256="0" * 64),
        prompt=PromptState(source_repository_path="", snapshot_path="", sha256="0" * 64),
        codex=CodexState(
            command="codex",
            review_model="scheduler-attempt",
            review_skill="scheduler-attempt",
            sandbox="read-only",
            fresh_reviewer=FreshCodexReviewerBinding(
                review_model="scheduler-attempt",
                review_reasoning_effort="high",
            ),
        ),
        cursor=CursorState(
            command=str(binding["cursor_command"]),
            model=str(binding["cursor_model"]),
            output_format=str(binding["cursor_output_format"]),
            force=bool(binding.get("cursor_force", True)),
            trust_workspace=bool(binding.get("cursor_trust_workspace", True)),
            sandbox=str(binding["cursor_sandbox"]),
        ),
        workflow=WorkflowState(
            max_review_iterations=1,
            stage_mode="all",
            cursor_timeout_minutes=1,
            codex_timeout_minutes=1,
        ),
    )


def _outcome_identity(evidence: dict[str, object]) -> dict[str, object]:
    return {
        "attempt_id": str(evidence["attempt_id"]),
        "run_id": str(evidence["run_id"]),
        "dispatch_id": str(evidence["dispatch_id"]),
    }


def _bounded_diagnostic(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:240]


def _write_attempt_artifacts(
    *,
    stdout_path: Path,
    stderr_path: Path,
    result_path: Path,
    stdout_rel: str,
    stderr_rel: str,
    attempt_id: str,
    unit_identity: str,
    effect_kind: str,
    dispatch_id: str,
    run_id: str,
    exit_code: int,
    termination_class: TerminationClass,
    stderr_text: str,
    stdout_payload: dict[str, object] | None = None,
) -> int:
    payload: dict[str, object] = {
        "effect_kind": effect_kind,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "parse_ok": False,
        "has_completion_signal": False,
    }
    if stdout_payload:
        payload.update(stdout_payload)
    stderr_path.write_text(stderr_text[:240] + "\n", encoding="utf-8")
    stdout_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=exit_code,
        termination_class=termination_class,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    _ = envelope_sha256(envelope)
    return exit_code


def _finalize_success_artifacts(
    *,
    stdout_path: Path,
    stderr_path: Path,
    result_path: Path,
    stdout_rel: str,
    stderr_rel: str,
    attempt_id: str,
    unit_identity: str,
    exit_code: int,
    cursor_outcome: dict[str, object],
) -> int:
    stdout_path.write_text(json.dumps(cursor_outcome, sort_keys=True) + "\n", encoding="utf-8")
    wait_log = cursor_outcome.get("sdk_wait_log")
    stderr_path.write_text(
        f"{wait_log}\n" if isinstance(wait_log, str) and wait_log else "",
        encoding="utf-8",
    )
    termination = (
        TerminationClass.SUCCESS
        if exit_code == 0
        else (TerminationClass.TIMEOUT if exit_code == 124 else TerminationClass.NONZERO_EXIT)
    )
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=exit_code,
        termination_class=termination,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    _ = envelope_sha256(envelope)
    return exit_code


def _sdk_selection(evidence: dict[str, object]) -> CursorSdkSection:
    return CursorSdkSection.model_validate(
        {
            "runtime": "sdk-local",
            "sdk_version": evidence["sdk_version"],
            "model": evidence["cursor_model"],
            "parameters": evidence["sdk_parameters"],
            "credential_ref": evidence["sdk_credential_ref"],
            "setting_sources": evidence["sdk_setting_sources"],
            "sandbox": evidence["cursor_sandbox"],
        }
    )


def _sdk_adapter(evidence: dict[str, object]) -> CursorSdkAdapter:
    owner = str(evidence["conversation_owner_run_id"])
    expected_key = str(evidence["conversation_key"])
    if cursor_conversation_key(owner) != expected_key:
        raise ValueError("Cursor SDK conversation ownership binding is invalid")
    repository_root = Path(str(evidence["repository_root"]))
    from ai_dev_loop.scheduler.application.cursor_sdk_retention import open_dispatch_store

    evidence_schema = evidence.get("schema_version")
    store_backend: Literal["jsonl", "sqlite"] = "sqlite" if evidence_schema == 3 else "jsonl"
    native_store_layout_version: Literal[1] = 1
    if evidence_schema == 3 and evidence.get("store_backend") != "sqlite":
        raise ValueError("Cursor SDK SQLite storage binding is invalid")
    config = open_dispatch_store(
        conversation_key=expected_key,
        conversation_owner_run_id=owner,
        opener=lambda: build_cursor_sdk_client_config(
            _sdk_selection(evidence),
            workspace=repository_root,
            repository_root=repository_root,
            conversation_owner_identity=owner,
            store_backend=store_backend,
            native_store_layout_version=native_store_layout_version,
            require_existing_native_state=bool(evidence.get("agent_id")),
        ),
    )
    return CursorSdkAdapter(config, repository_root=repository_root)


def _no_progress_send_guard(
    adapter: CursorSdkAdapter,
    evidence: dict[str, object],
    agent_id: str,
    run_root: Path,
) -> str | None:
    """Block a continuation send unless the frozen capture and native error still match.

    The expected events digest is the authorization witness, not a fresh copy.
    A missing, changed, or conflicting capture does not send.
    """

    refused = evidence.get("no_progress_evidence_refused") is True
    native_run_id = str(evidence.get("no_progress_guard_native_run_id") or "")
    events_sha = str(evidence.get("no_progress_events_sha256") or "")
    if not refused and not native_run_id and not events_sha:
        return None
    if refused or not native_run_id or not events_sha:
        return "cursor_sdk_native_run_uncertain"
    source_run_id = str(evidence.get("no_progress_source_run_id") or "")
    events_path = str(evidence.get("no_progress_events_path") or "")
    if not source_run_id or not events_path:
        return "cursor_sdk_native_run_uncertain"
    try:
        source_root = run_artifact_root(run_root.parent.parent, source_run_id)
        digest = sha256_file(resolve_run_relative_path(source_root, events_path))
    except (OSError, ValueError):
        return "cursor_sdk_native_run_uncertain"
    if digest != events_sha:
        return "cursor_sdk_native_run_uncertain"
    try:
        observed = adapter.get_run(agent_id, native_run_id)
    except Exception:
        return "cursor_sdk_native_run_uncertain"
    if observed.failure is not None or observed.value is None:
        return "cursor_sdk_native_run_uncertain"
    if observed.value.run_id != native_run_id or observed.value.agent_id != agent_id:
        return "cursor_sdk_native_run_uncertain"
    if observed.value.status.strip().lower() != "error":
        return "cursor_sdk_native_run_uncertain"
    return None


def _sdk_failure_payload(failure: CursorSdkFailure | None) -> dict[str, object]:
    if failure is None:
        return {}
    value = asdict(failure)
    return {
        key: str(item.value if hasattr(item, "value") else item)
        for key, item in value.items()
        if item is not None
    }


def _sdk_uncertainty_payload(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
    effect_kind: str,
    timed_out: bool = False,
) -> dict[str, object]:
    journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
    journal = run_root / journal_rel
    payload: dict[str, object] = {
        "runtime": "sdk-local",
        "returncode": 124 if timed_out else 1,
        "timed_out": timed_out,
        "parse_ok": False,
        "has_completion_signal": False,
    }
    if not journal.is_file():
        payload["failure_kind"] = (
            "cursor_sdk_recovery_deferred" if timed_out else "cursor_sdk_preflight_failed"
        )
        payload.update(_existing_sdk_capture_payload(evidence, run_root, attempt_id=attempt_id))
        return payload
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    kinds = [str(item.get("kind", "")) for item in records if isinstance(item, dict)]
    if effect_kind == CREATE_CHAT_EFFECT_KIND:
        failure_kind = "cursor_sdk_create_uncertain"
    else:
        failure_kind = (
            "cursor_sdk_native_run_uncertain"
            if "native_run_bound" in kinds
            else "cursor_sdk_send_uncertain"
        )
    if "terminal" not in kinds:
        last = records[-1] if records and isinstance(records[-1], dict) else {}
        terminal = {
            key: last[key]
            for key in (
                "attempt_id",
                "dispatch_id",
                "conversation_owner_run_id",
                "conversation_key",
                "agent_id",
                "prompt_sha256",
                "native_run_id",
            )
            if key in last
        }
        versions = {item.get("schema_version") for item in records if isinstance(item, dict)}
        schema_version = (
            JOURNAL_SCHEMA_VERSION_V2
            if versions == {JOURNAL_SCHEMA_VERSION_V2}
            else JOURNAL_SCHEMA_VERSION
        )
        append_sdk_journal(
            journal,
            kind="terminal",
            payload={**terminal, "outcome": "uncertain", "failure_kind": failure_kind},
            schema_version=schema_version,
        )
    payload.update(
        {
            "failure_kind": failure_kind,
            "journal_path": journal_rel,
            "journal_sha256": sha256_file(journal),
        }
    )
    payload.update(_existing_sdk_capture_payload(evidence, run_root, attempt_id=attempt_id))
    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        seal_interrupted_sdk_wait_progress(run_root / cursor_sdk_wait_progress_rel(attempt_id))
        payload.update(_sdk_wait_fields(run_root, attempt_id))
    return payload


def _note_sdk_wait(run_root: Path, attempt_id: str, iteration: int, kind: str) -> None:
    """Persist one safe stage without mutating the authenticated child stderr."""

    del iteration
    append_sdk_wait_progress(
        run_root / cursor_sdk_wait_progress_rel(attempt_id),
        kind=kind,
        attempt_id=attempt_id,
    )


def _deadline_stderr(run_root: Path, attempt_id: str, exc: BaseException) -> str:
    inspection = inspect_sdk_wait(run_root / cursor_sdk_wait_progress_rel(attempt_id))
    detail = f"{type(exc).__name__}: Cursor SDK worker failed"
    return f"{format_sdk_wait_log(inspection)}; {detail}"[:240]


def _sdk_wait_fields(run_root: Path, attempt_id: str) -> dict[str, str]:
    inspection = inspect_sdk_wait(run_root / cursor_sdk_wait_progress_rel(attempt_id))
    return {
        "sdk_wait_stage": inspection.stage,
        "sdk_wait_elapsed_seconds": inspection.elapsed_seconds,
        "sdk_wait_waiting_for_initial_response": inspection.waiting_for_initial_response,
        "sdk_wait_log": format_sdk_wait_log(inspection),
    }


def _closed_sdk_wait_fields(run_root: Path, attempt_id: str) -> dict[str, str]:
    """Seal an open wait and project it. A missing file contributes nothing."""

    path = run_root / cursor_sdk_wait_progress_rel(attempt_id)
    if path.is_symlink() or not path.is_file():
        return {}
    seal_interrupted_sdk_wait_progress(path)
    return _sdk_wait_fields(run_root, attempt_id)


def _readiness_now() -> float:
    return time.monotonic()


def _readiness_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


class _ReadinessBudgetExceeded(Exception):
    """The ten-second creation-readiness budget elapsed around an SDK call."""


@contextmanager
def _bound_readiness_call(deadline: float) -> Iterator[None]:
    """Stop a readiness SDK call when its own budget is gone.

    The caller's frozen operation itimer is restored afterward. A shorter
    readiness remainder replaces it only for the duration of this call.
    """

    remaining = deadline - _readiness_now()
    if remaining <= 0:
        raise _ReadinessBudgetExceeded()
    current = signal.getitimer(signal.ITIMER_REAL)
    overall_remaining = current[0]
    if overall_remaining > 0 and overall_remaining <= remaining:
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    entered = time.monotonic()

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise _ReadinessBudgetExceeded()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining, 0)
    try:
        yield
    finally:
        elapsed = time.monotonic() - entered
        restored = max(0.0, current[0] - elapsed) if current[0] > 0 else 0.0
        signal.setitimer(signal.ITIMER_REAL, restored, current[1])
        signal.signal(signal.SIGALRM, previous_handler)


def _readiness_expired(deadline: float) -> bool:
    return _readiness_now() > deadline


def _maybe_interrupt(kind: str) -> None:
    if os.environ.get("AI_DEV_LOOP_SDK_INTERRUPT_AFTER") == kind:
        raise CursorSdkWorkerInterrupt(kind)


def _journal_append(
    path: Path,
    *,
    kind: str,
    payload: dict[str, object],
    schema_version: int,
) -> None:
    append_sdk_journal(path, kind=kind, payload=payload, schema_version=schema_version)
    _maybe_interrupt(kind)


def _load_creation_inventory(
    evidence: dict[str, object],
    run_root: Path,
) -> CreationInventoryFact | None:
    path = evidence.get("identity_artifact_path")
    digest = evidence.get("identity_artifact_sha256")
    if not isinstance(path, str) or not path or not isinstance(digest, str) or not digest:
        return None
    identity_path = run_root / path
    if not identity_path.is_file() or sha256_file(identity_path) != digest:
        raise ValueError("Cursor SDK identity artifact digest mismatch")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if not isinstance(identity, dict):
        raise ValueError("Cursor SDK identity artifact is invalid")
    evidence_schema = evidence.get("schema_version")
    expected_identity_schema = 2 if evidence_schema == 3 else 1
    identity_schema = identity.get("schema_version")
    if (expected_identity_schema == 2 and identity_schema != 2) or (
        expected_identity_schema == 1 and identity_schema not in {None, 1}
    ):
        raise ValueError("Cursor SDK identity storage schema mismatch")
    if expected_identity_schema == 2 and (
        identity.get("store_backend") != evidence.get("store_backend")
        or identity.get("native_store_layout_version")
        != evidence.get("native_store_layout_version")
    ):
        raise ValueError("Cursor SDK identity storage binding mismatch")
    journal_rel = identity.get("create_journal_path")
    journal_sha = identity.get("create_journal_sha256")
    if not isinstance(journal_rel, str) or not isinstance(journal_sha, str):
        return None
    journal_path = run_root / journal_rel
    # A successor can carry the identity artifact without the source create
    # journal. Absence is not eligibility; a present journal must still authenticate.
    if journal_path.is_symlink() or not journal_path.is_file():
        return None
    records = authenticate_sdk_journal(
        journal_path,
        expected_sha256=journal_sha,
        operation="create",
        expected_binding={
            "conversation_owner_run_id": str(evidence["conversation_owner_run_id"]),
            "conversation_key": str(evidence["conversation_key"]),
        },
    )
    inventory = next(
        (record for record in records if record.get("kind") == "creation_inventory"), None
    )
    bound = next(record for record in records if record.get("kind") == "agent_bound")
    if str(bound.get("agent_id", "")) != str(evidence.get("agent_id", "")):
        raise ValueError("Cursor SDK creation inventory agent does not match the turn")
    if inventory is None:
        return None
    raw_runs = inventory.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("Cursor SDK creation inventory is invalid")
    message_count = inventory.get("message_count")
    authorized = inventory.get("authorized_send_count")
    if isinstance(message_count, bool) or not isinstance(message_count, int):
        raise ValueError("Cursor SDK creation message count is invalid")
    if isinstance(authorized, bool) or not isinstance(authorized, int):
        raise ValueError("Cursor SDK authorized send count is invalid")
    runs: list[PublicRunFact] = []
    for item in raw_runs:
        if not isinstance(item, dict):
            raise ValueError("Cursor SDK creation inventory entry is invalid")
        runs.append(PublicRunFact(run_id=str(item["run_id"]), status=str(item["status"])))
    return CreationInventoryFact(
        runs=tuple(runs),
        message_count=message_count,
        authorized_send_count=authorized,
    )


def _public_run_facts(runs: list[CursorSdkTerminalRun]) -> tuple[PublicRunFact, ...]:
    return tuple(PublicRunFact(run_id=run.run_id, status=run.status) for run in runs)


def _observe_public_agent(
    adapter: CursorSdkAdapter,
    agent_id: str,
    *,
    deadline: float | None = None,
) -> tuple[tuple[PublicRunFact, ...], int] | None:
    try:
        if deadline is None:
            listed, failure = _list_sdk_runs_for_agent(adapter, agent_id)
        else:
            with _bound_readiness_call(deadline):
                listed, failure = _list_sdk_runs_for_agent(adapter, agent_id)
        if listed is None or failure is not None or _readiness_expired(deadline or float("inf")):
            return None
        if deadline is None:
            messages = adapter.list_agent_messages(agent_id)
        else:
            with _bound_readiness_call(deadline):
                messages = adapter.list_agent_messages(agent_id)
    except _ReadinessBudgetExceeded:
        return None
    if deadline is not None and _readiness_expired(deadline):
        return None
    if (
        messages.failure is not None
        or isinstance(messages.value, bool)
        or not isinstance(messages.value, int)
    ):
        return None
    return _public_run_facts(listed), messages.value


def _definitive_send_rejection(failure: CursorSdkFailure | None) -> bool:
    return (
        failure is not None
        and failure.code == CursorSdkErrorCode.BUSY
        and failure.evidence_class in {"typed", "structured"}
    )


def _run_sdk_create(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    with _sdk_operation_deadline(evidence):
        return _run_sdk_create_operation(evidence, run_root, attempt_id=attempt_id)


def _run_sdk_create_operation(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
    journal = run_root / journal_rel
    adapter = _sdk_adapter(evidence)
    try:
        opened = adapter.open()
        if opened.failure is not None:
            return {
                "runtime": "sdk-local",
                "effect_kind": CREATE_CHAT_EFFECT_KIND,
                "failure_kind": "cursor_sdk_preflight_failed",
                "sdk_failure": _sdk_failure_payload(opened.failure),
                "returncode": 1,
                "parse_ok": False,
                "has_completion_signal": False,
                **_outcome_identity(evidence),
            }
        preflight = adapter.preflight()
        if preflight.failure is not None:
            return {
                "runtime": "sdk-local",
                "effect_kind": CREATE_CHAT_EFFECT_KIND,
                "failure_kind": "cursor_sdk_preflight_failed",
                "sdk_failure": _sdk_failure_payload(preflight.failure),
                "returncode": 1,
                "parse_ok": False,
                "has_completion_signal": False,
                **_outcome_identity(evidence),
            }
        base: dict[str, object] = {
            "attempt_id": attempt_id,
            "dispatch_id": str(evidence["dispatch_id"]),
            "conversation_owner_run_id": str(evidence["conversation_owner_run_id"]),
            "conversation_key": str(evidence["conversation_key"]),
        }
        _journal_append(
            journal,
            kind="create_intent",
            payload=base,
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        created = adapter.create_agent()
        if created.failure is not None or created.value is None:
            _journal_append(
                journal,
                kind="terminal",
                payload={
                    **base,
                    "outcome": "uncertain",
                    "failure_kind": "cursor_sdk_create_uncertain",
                },
                schema_version=JOURNAL_SCHEMA_VERSION_V2,
            )
            return {
                "runtime": "sdk-local",
                "effect_kind": CREATE_CHAT_EFFECT_KIND,
                "failure_kind": "cursor_sdk_create_uncertain",
                "sdk_failure": _sdk_failure_payload(created.failure),
                "journal_path": journal_rel,
                "journal_sha256": sha256_file(journal),
                "returncode": 1,
                "parse_ok": False,
                "has_completion_signal": False,
                **_outcome_identity(evidence),
            }
        agent_id = created.value.agent_id
        bound = {**base, "agent_id": agent_id}
        _journal_append(
            journal,
            kind="agent_bound",
            payload=bound,
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        listed, list_failure = _list_sdk_runs_for_agent(adapter, agent_id)
        messages = adapter.list_agent_messages(agent_id)
        if (
            listed is None
            or list_failure is not None
            or messages.failure is not None
            or isinstance(messages.value, bool)
            or not isinstance(messages.value, int)
        ):
            _journal_append(
                journal,
                kind="terminal",
                payload={
                    **bound,
                    "outcome": "uncertain",
                    "failure_kind": "cursor_sdk_create_uncertain",
                },
                schema_version=JOURNAL_SCHEMA_VERSION_V2,
            )
            return {
                "runtime": "sdk-local",
                "effect_kind": CREATE_CHAT_EFFECT_KIND,
                "failure_kind": "cursor_sdk_create_uncertain",
                "sdk_failure": _sdk_failure_payload(
                    list_failure if list_failure is not None else messages.failure
                ),
                "journal_path": journal_rel,
                "journal_sha256": sha256_file(journal),
                "returncode": 1,
                "parse_ok": False,
                "has_completion_signal": False,
                **_outcome_identity(evidence),
            }
        _journal_append(
            journal,
            kind="creation_inventory",
            payload={
                **bound,
                "runs": [{"run_id": run.run_id, "status": run.status} for run in listed],
                "message_count": messages.value,
                "authorized_send_count": 0,
            },
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        _journal_append(
            journal,
            kind="terminal",
            payload={**bound, "outcome": "success"},
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        return {
            "runtime": "sdk-local",
            "effect_kind": CREATE_CHAT_EFFECT_KIND,
            "agent_id": agent_id,
            "conversation_owner_run_id": base["conversation_owner_run_id"],
            "conversation_key": base["conversation_key"],
            "journal_path": journal_rel,
            "journal_sha256": sha256_file(journal),
            "returncode": 0,
            "parse_ok": True,
            "has_completion_signal": True,
            **_outcome_identity(evidence),
        }
    finally:
        adapter.close()


def _cursor_usage_from_envelope(envelope: CursorSdkEnvelope) -> object | None:
    """Return only an explicit public SDK usage-message payload."""

    payload: object = envelope.payload
    if not isinstance(payload, dict):
        return None
    message = payload.get("sdk_message", payload)
    if not isinstance(message, dict) or message.get("type") != "usage":
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


def _safe_cursor_usage_metadata(usage: object) -> dict[str, object] | None:
    if not isinstance(usage, dict):
        return None
    return {
        key: value if value is None or isinstance(value, (int, float, bool)) else None
        for key, value in usage.items()
        if key
        in {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "reasoning_tokens",
        }
    }


def _cursor_sdk_capture_complete(status: str | None) -> bool:
    """Return whether a native terminal status proves the usage capture complete."""

    return status is not None and status.lower() == "finished"


def _sdk_capture_payload(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
    agent_id: str,
    native_run_id: str | None,
    status: str | None,
    stored: int,
    truncated: bool,
    capture_complete: bool,
    terminal: CursorSdkTerminalRun | None = None,
    stream_usages: tuple[CursorUsageEvent, ...] = (),
    has_completion: bool = False,
) -> dict[str, object]:
    iteration = _binding_int(evidence, "iteration")
    events_rel = cursor_sdk_attempt_events_rel(iteration, attempt_id)
    metadata_rel = cursor_sdk_attempt_metadata_rel(iteration, attempt_id)
    final_rel = cursor_attempt_final_rel(iteration, attempt_id)
    stderr_rel = cursor_attempt_stderr_rel(iteration, attempt_id)
    events_path = run_root / events_rel
    stderr_path = run_root / stderr_rel
    final_sha: str | None = None
    if has_completion and terminal is not None:
        atomic_write_text(run_root / final_rel, terminal.text, sensitive=True)
        final_sha = sha256_file(run_root / final_rel)
    events_sha = sha256_file(events_path)
    stderr_sha = sha256_file(stderr_path)
    stderr_stored = stderr_path.stat().st_size
    terminal_usage = asdict(terminal.usage) if terminal is not None and terminal.usage else None
    metadata = {
        "schema_version": CURSOR_SDK_CAPTURE_SCHEMA_VERSION,
        "runtime": "sdk-local",
        "agent_id": agent_id,
        "native_run_id": native_run_id,
        "status": status,
        "capture_complete": capture_complete,
        "events_path": events_rel,
        "events_format": CURSOR_SDK_EVENT_STREAM_FORMAT,
        "events_sha256": events_sha,
        "events_stored_bytes": stored,
        "events_truncated": truncated,
        "stderr_path": stderr_rel,
        "stderr_format": CURSOR_SDK_TEXT_STREAM_FORMAT,
        "stderr_sha256": stderr_sha,
        "stderr_stored_bytes": stderr_stored,
        "stderr_truncated": False,
        "usage": _safe_cursor_usage_metadata(terminal_usage),
        "stream_usage": [
            {
                "offset": item.offset,
                "ordinal": item.ordinal,
                "usage": _safe_cursor_usage_metadata(item.usage),
            }
            for item in stream_usages
        ],
        "final_response_path": final_rel if final_sha else None,
        "final_response_format": CURSOR_SDK_TEXT_STREAM_FORMAT if final_sha else None,
        "final_response_sha256": final_sha,
    }
    atomic_write_json(run_root / metadata_rel, metadata, sensitive=True)
    payload: dict[str, object] = {
        "metadata_path": metadata_rel,
        "metadata_sha256": sha256_file(run_root / metadata_rel),
        "events_path": events_rel,
        "events_sha256": events_sha,
        "events_stored_bytes": stored,
        "events_truncated": truncated,
        "stderr_path": stderr_rel,
        "stderr_sha256": stderr_sha,
        "stderr_stored_bytes": stderr_stored,
        "stderr_truncated": False,
        "final_response_path": final_rel if final_sha else None,
        "final_response_sha256": final_sha,
    }
    if native_run_id is not None:
        logical_attempt_id = str(evidence.get("source_attempt_id") or attempt_id)
        logical_dispatch_id = str(evidence.get("source_dispatch_id") or evidence["dispatch_id"])
        terminal_usages = (terminal_usage,) if terminal_usage is not None else ()
        usage_result = capture_cursor_usage_evidence_safely(
            run_root=run_root,
            run_id=str(evidence["run_id"]),
            stage="initial" if iteration == 1 else "correction",
            scheduler_attempt_id=logical_attempt_id,
            publisher_attempt_id=attempt_id,
            source_dispatch_id=logical_dispatch_id,
            native_run_id=native_run_id,
            source_artifact_path=metadata_rel,
            source_artifact_sha256=str(payload["metadata_sha256"]),
            stream_usages=stream_usages,
            cumulative_usages=terminal_usages,
            capture_complete=capture_complete and not truncated,
        )
        payload.update(usage_result.outcome_fields())
    return payload


def _existing_sdk_capture_payload(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    if evidence.get("effect_kind") != RUN_CURSOR_TURN_EFFECT_KIND:
        return {}
    try:
        iteration = _binding_int(evidence, "iteration")
        metadata_rel = cursor_sdk_attempt_metadata_rel(iteration, attempt_id)
        metadata_path = run_root / metadata_rel
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return {}
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return {}
        events_rel = cursor_sdk_attempt_events_rel(iteration, attempt_id)
        stderr_rel = cursor_attempt_stderr_rel(iteration, attempt_id)
        events_path = run_root / events_rel
        stderr_path = run_root / stderr_rel
        if any(path.is_symlink() or not path.is_file() for path in (events_path, stderr_path)):
            return {}
        return {
            "metadata_path": metadata_rel,
            "metadata_sha256": sha256_file(metadata_path),
            "events_path": events_rel,
            "events_sha256": sha256_file(events_path),
            "events_stored_bytes": events_path.stat().st_size,
            "events_truncated": bool(metadata.get("events_truncated", False)),
            "stderr_path": stderr_rel,
            "stderr_sha256": sha256_file(stderr_path),
            "stderr_stored_bytes": stderr_path.stat().st_size,
            "stderr_truncated": bool(metadata.get("stderr_truncated", False)),
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _run_payload(
    runs: tuple[PublicRunFact, ...] | list[CursorSdkTerminalRun],
) -> list[dict[str, str]]:
    return [{"run_id": run.run_id, "status": run.status} for run in runs]


def _turn_stop(
    evidence: dict[str, object],
    *,
    iteration: int,
    failure_kind: str,
    failure: CursorSdkFailure | None,
    journal: Path,
    journal_rel: str,
    empty_capture_payload: dict[str, object],
    run_root: Path,
    attempt_id: str,
) -> dict[str, object]:
    return {
        "runtime": "sdk-local",
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "iteration": iteration,
        "returncode": 1,
        "timed_out": False,
        "parse_ok": False,
        "has_completion_signal": False,
        "failure_kind": failure_kind,
        "sdk_failure": _sdk_failure_payload(failure),
        **empty_capture_payload,
        **_closed_sdk_wait_fields(run_root, attempt_id),
        "journal_path": journal_rel,
        "journal_sha256": sha256_file(journal),
        **_outcome_identity(evidence),
    }


def _append_turn_terminal(
    journal: Path,
    base: dict[str, object],
    *,
    outcome: str,
    failure_kind: str | None = None,
) -> None:
    payload = {**base, "outcome": outcome}
    if failure_kind is not None:
        payload["failure_kind"] = failure_kind
    append_sdk_journal(
        journal,
        kind="terminal",
        payload=payload,
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )


def _readiness_record(
    base: dict[str, object],
    *,
    ordinal: int,
    runs: tuple[PublicRunFact, ...],
    message_count: int,
    reason: str,
) -> dict[str, object]:
    return {
        **base,
        "observation_ordinal": ordinal,
        "runs": _run_payload(runs),
        "message_count": message_count,
        "reason": reason[:160],
    }


def _journal_readiness_hold(
    journal: Path,
    base: dict[str, object],
    *,
    ordinal: int,
    runs: tuple[PublicRunFact, ...],
    message_count: int,
    reason: str,
) -> None:
    append_sdk_journal(
        journal,
        kind="readiness_hold",
        payload=_readiness_record(
            base,
            ordinal=ordinal,
            runs=runs,
            message_count=message_count,
            reason=reason,
        ),
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    _append_turn_terminal(
        journal,
        base,
        outcome="failed",
        failure_kind="cursor_sdk_creation_readiness_hold",
    )


def _await_creation_blocker_terminal(
    adapter: CursorSdkAdapter,
    agent_id: str,
    run_id: str,
    *,
    deadline: float,
) -> str | None:
    for poll in range(1, MAX_CREATION_READINESS_OBSERVATIONS + 1):
        if _readiness_expired(deadline):
            return None
        try:
            with _bound_readiness_call(deadline):
                observed = adapter.get_run(agent_id, run_id)
        except _ReadinessBudgetExceeded:
            return None
        if _readiness_expired(deadline):
            return None
        if observed.failure is not None or observed.value is None:
            return None
        status = observed.value.status.strip().lower()
        if status in TERMINAL_PUBLIC_RUN_STATUSES:
            return status
        if poll == MAX_CREATION_READINESS_OBSERVATIONS:
            return None
        remaining = deadline - _readiness_now()
        if remaining <= 0:
            return None
        _readiness_sleep(
            min(remaining, MAX_CREATION_READINESS_SECONDS / MAX_CREATION_READINESS_OBSERVATIONS)
        )
    return None


def _dispatch_after_creation_readiness(
    adapter: CursorSdkAdapter,
    evidence: dict[str, object],
    run_root: Path,
    *,
    journal: Path,
    journal_rel: str,
    base: dict[str, object],
    prompt: str,
    agent_id: str,
    iteration: int,
    inventory: CreationInventoryFact,
    baseline_runs: list[CursorSdkTerminalRun],
    empty_capture_payload: dict[str, object],
) -> CursorSdkRunIdentity | dict[str, object]:
    baseline_ids = {run.run_id for run in baseline_runs}
    creation_ids = [run.run_id for run in inventory.runs if run.run_id in baseline_ids]
    append_sdk_journal(
        journal,
        kind="run_inventory_baseline",
        payload={
            **base,
            "run_ids": sorted(baseline_ids),
            "runs": _run_payload(baseline_runs),
            "creation_run_ids": creation_ids,
            "message_count": inventory.message_count,
            "authorized_send_count": inventory.authorized_send_count,
        },
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    started = _readiness_now()
    deadline = started + MAX_CREATION_READINESS_SECONDS
    decision_ordinal = 0
    decision = None
    observed_runs: tuple[PublicRunFact, ...] = ()
    observed_messages = 0
    for ordinal in range(1, MAX_CREATION_READINESS_OBSERVATIONS + 1):
        if _readiness_expired(deadline):
            break
        observed = _observe_public_agent(adapter, agent_id, deadline=deadline)
        decision_ordinal = ordinal
        if observed is None:
            decision = None
            break
        observed_runs, observed_messages = observed
        decision = classify_creation_readiness(
            inventory,
            observed_runs,
            message_count=observed_messages,
        )
        if decision.action != "hold":
            break
        if ordinal == MAX_CREATION_READINESS_OBSERVATIONS:
            break
        remaining = deadline - _readiness_now()
        if remaining <= 0:
            break
        _readiness_sleep(
            min(remaining, MAX_CREATION_READINESS_SECONDS / MAX_CREATION_READINESS_OBSERVATIONS)
        )
    if decision is None or decision.action == "hold":
        reason = "public observation failed" if decision is None else decision.reason
        append_sdk_journal(
            journal,
            kind="readiness_hold",
            payload=_readiness_record(
                base,
                ordinal=max(decision_ordinal, 1),
                runs=observed_runs,
                message_count=observed_messages,
                reason=reason,
            ),
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        _append_turn_terminal(
            journal,
            base,
            outcome="failed",
            failure_kind="cursor_sdk_creation_readiness_hold",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_creation_readiness_hold",
            failure=None,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    if decision.action == "cancel" and _readiness_expired(deadline):
        _journal_readiness_hold(
            journal,
            base,
            ordinal=max(decision_ordinal, 1),
            runs=observed_runs,
            message_count=observed_messages,
            reason="creation readiness budget expired",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_creation_readiness_hold",
            failure=None,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    if decision.action == "cancel":
        occupant_id = decision.occupant_id or ""
        append_sdk_journal(
            journal,
            kind="readiness_observation",
            payload=_readiness_record(
                base,
                ordinal=decision_ordinal,
                runs=observed_runs,
                message_count=observed_messages,
                reason=decision.reason,
            ),
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        _journal_append(
            journal,
            kind="creation_blocker_cancel_intent",
            payload={**base, "run_id": occupant_id},
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        try:
            with _bound_readiness_call(deadline):
                cancelled = adapter.cancel(agent_id, occupant_id)
        except _ReadinessBudgetExceeded:
            _append_turn_terminal(
                journal,
                base,
                outcome="failed",
                failure_kind="cursor_sdk_creation_readiness_hold",
            )
            return _turn_stop(
                evidence,
                iteration=iteration,
                failure_kind="cursor_sdk_creation_readiness_hold",
                failure=None,
                journal=journal,
                journal_rel=journal_rel,
                empty_capture_payload=empty_capture_payload,
                run_root=run_root,
                attempt_id=str(base["attempt_id"]),
            )
        confirmed = (
            _await_creation_blocker_terminal(
                adapter,
                agent_id,
                occupant_id,
                deadline=deadline,
            )
            if cancelled.failure is None and not _readiness_expired(deadline)
            else None
        )
        if confirmed is None:
            _append_turn_terminal(
                journal,
                base,
                outcome="failed",
                failure_kind="cursor_sdk_creation_readiness_hold",
            )
            return _turn_stop(
                evidence,
                iteration=iteration,
                failure_kind="cursor_sdk_creation_readiness_hold",
                failure=cancelled.failure,
                journal=journal,
                journal_rel=journal_rel,
                empty_capture_payload=empty_capture_payload,
                run_root=run_root,
                attempt_id=str(base["attempt_id"]),
            )
        _journal_append(
            journal,
            kind="creation_blocker_cancel_confirmed",
            payload={**base, "run_id": occupant_id, "status": confirmed},
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
    else:
        append_sdk_journal(
            journal,
            kind="readiness_clear",
            payload=_readiness_record(
                base,
                ordinal=decision_ordinal,
                runs=observed_runs,
                message_count=observed_messages,
                reason=decision.reason,
            ),
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )

    def send_once(ordinal: int) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        if _readiness_expired(deadline):
            raise _ReadinessBudgetExceeded()
        _journal_append(
            journal,
            kind="send_intent",
            payload={**base, "dispatch_ordinal": ordinal},
            schema_version=JOURNAL_SCHEMA_VERSION_V2,
        )
        _note_sdk_wait(run_root, str(base["attempt_id"]), iteration, SEND_AUTHORIZED)
        # send_intent is the authorization barrier. The native handle and first
        # stream event belong to the frozen turn deadline, not the readiness budget.
        # The readiness itimer is not installed around this call, and the attempt
        # deadline installed at operation start is not replaced.
        return adapter.send(agent_id, prompt)

    if _readiness_expired(deadline):
        _journal_readiness_hold(
            journal,
            base,
            ordinal=max(decision_ordinal, 1),
            runs=observed_runs,
            message_count=observed_messages,
            reason="creation readiness budget expired",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_creation_readiness_hold",
            failure=None,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    try:
        sent = send_once(1)
    except _ReadinessBudgetExceeded:
        _append_turn_terminal(
            journal,
            base,
            outcome="uncertain",
            failure_kind="cursor_sdk_send_uncertain",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_send_uncertain",
            failure=None,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    if sent.failure is None and sent.value is not None:
        return sent.value
    fresh = _observe_public_agent(adapter, agent_id, deadline=deadline)
    post: tuple[PublicRunFact, ...] = ()
    fresh_runs: tuple[PublicRunFact, ...] = ()
    fresh_messages = 0
    if fresh is not None:
        fresh_runs, fresh_messages = fresh
        post = tuple(run for run in fresh_runs if run.run_id not in baseline_ids)
    blocker = inventory.runs[0] if len(inventory.runs) == 1 else None
    blocker_terminal = False
    other_active = False
    if fresh is not None and blocker is not None:
        current = next((run for run in fresh_runs if run.run_id == blocker.run_id), None)
        blocker_terminal = (
            current is not None and current.status.strip().lower() in TERMINAL_PUBLIC_RUN_STATUSES
        )
        other_active = any(
            run.run_id != blocker.run_id
            and run.status.strip().lower() not in TERMINAL_PUBLIC_RUN_STATUSES
            for run in fresh_runs
        )
    next_ordinal = decision_ordinal + 1
    can_resend = (
        _definitive_send_rejection(sent.failure)
        and fresh is not None
        and not post
        and blocker_terminal
        and not other_active
        and fresh_messages == 0
        and next_ordinal <= MAX_CREATION_READINESS_OBSERVATIONS
        and not _readiness_expired(deadline)
    )
    if len(post) == 1 or len(post) > 1 or fresh is None or not can_resend:
        if (
            can_resend is False
            and _definitive_send_rejection(sent.failure)
            and fresh is not None
            and not post
            and blocker is not None
            and not blocker_terminal
            and next_ordinal <= MAX_CREATION_READINESS_OBSERVATIONS
        ):
            evidence_class = sent.failure.evidence_class if sent.failure is not None else None
            append_sdk_journal(
                journal,
                kind="send_rejected",
                payload={
                    **base,
                    "dispatch_ordinal": 1,
                    "evidence_class": evidence_class or "typed",
                },
                schema_version=JOURNAL_SCHEMA_VERSION_V2,
            )
            append_sdk_journal(
                journal,
                kind="readiness_hold",
                payload=_readiness_record(
                    base,
                    ordinal=next_ordinal,
                    runs=fresh_runs,
                    message_count=fresh_messages,
                    reason="owned creation blocker is still nonterminal",
                ),
                schema_version=JOURNAL_SCHEMA_VERSION_V2,
            )
            _append_turn_terminal(
                journal,
                base,
                outcome="failed",
                failure_kind="cursor_sdk_creation_readiness_hold",
            )
            return _turn_stop(
                evidence,
                iteration=iteration,
                failure_kind="cursor_sdk_creation_readiness_hold",
                failure=sent.failure,
                journal=journal,
                journal_rel=journal_rel,
                empty_capture_payload=empty_capture_payload,
                run_root=run_root,
                attempt_id=str(base["attempt_id"]),
            )
        _append_turn_terminal(
            journal,
            base,
            outcome="uncertain",
            failure_kind="cursor_sdk_send_uncertain",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_send_uncertain",
            failure=sent.failure,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    evidence_class = sent.failure.evidence_class if sent.failure is not None else None
    append_sdk_journal(
        journal,
        kind="send_rejected",
        payload={**base, "dispatch_ordinal": 1, "evidence_class": evidence_class or "typed"},
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    append_sdk_journal(
        journal,
        kind="readiness_observation",
        payload=_readiness_record(
            base,
            ordinal=next_ordinal,
            runs=fresh_runs,
            message_count=fresh_messages,
            reason="owned creation blocker is terminal and no prompt run was accepted",
        ),
        schema_version=JOURNAL_SCHEMA_VERSION_V2,
    )
    try:
        resent = send_once(2)
    except _ReadinessBudgetExceeded:
        _append_turn_terminal(
            journal,
            base,
            outcome="uncertain",
            failure_kind="cursor_sdk_send_uncertain",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_send_uncertain",
            failure=sent.failure,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    if resent.failure is not None or resent.value is None:
        _append_turn_terminal(
            journal,
            base,
            outcome="uncertain",
            failure_kind="cursor_sdk_send_uncertain",
        )
        return _turn_stop(
            evidence,
            iteration=iteration,
            failure_kind="cursor_sdk_send_uncertain",
            failure=resent.failure,
            journal=journal,
            journal_rel=journal_rel,
            empty_capture_payload=empty_capture_payload,
            run_root=run_root,
            attempt_id=str(base["attempt_id"]),
        )
    return resent.value


def _run_sdk_turn(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    with _sdk_operation_deadline(evidence):
        return _run_sdk_turn_operation(evidence, run_root, attempt_id=attempt_id)


def _write_no_progress_witness(
    run_root: Path,
    *,
    attempt_id: str,
    dispatch_id: str,
    agent_id: str,
    native_run_id: str,
    events_rel: str,
    journal_rel: str,
    journal_path: Path,
    terminal_status: str,
    truncated: bool,
    status_hits: tuple[tuple[str, str], ...],
    terminal_status_names: tuple[str, ...],
    events_path: Path,
    capture_complete: bool,
) -> dict[str, str]:
    """Persist a witness only for one exact public status event and terminal error.

    A later or contradictory public terminal status, a truncated capture, or
    more than one exact no-progress line leaves the turn on the manual path.
    """

    if (
        terminal_status != "error"
        or truncated
        or len(status_hits) != 1
        or terminal_status_names != ("ERROR",)
    ):
        return {}
    from ai_dev_loop.scheduler.domain.cursor_sdk_no_progress import (
        NO_PROGRESS_MESSAGE_SHA256,
        CursorSdkNoProgressWitnessV1,
        no_progress_witness_rel,
    )

    offset, capture_sha = status_hits[0]
    journal_sha = sha256_file(journal_path)
    events_sha = sha256_file(events_path)
    witness = CursorSdkNoProgressWitnessV1(
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        agent_id=agent_id,
        native_run_id=native_run_id,
        offset=offset,
        capture_sha256=capture_sha,
        events_path=events_rel,
        journal_path=journal_rel,
        journal_sha256=journal_sha,
        events_sha256=events_sha,
        events_truncated=False,
        capture_complete=capture_complete,
        message_sha256=NO_PROGRESS_MESSAGE_SHA256,
    )
    relative = no_progress_witness_rel(attempt_id)
    atomic_write_text(
        run_root / relative,
        witness.canonical_bytes().decode("utf-8"),
        sensitive=True,
    )
    return {
        "no_progress_witness_path": relative,
        "no_progress_witness_sha256": sha256_file(run_root / relative),
    }


def _run_sdk_turn_operation(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    iteration = _binding_int(evidence, "iteration")
    repository_root = str(evidence["repository_root"])
    prompt_path = run_root / str(evidence["prompt_path"])
    prompt_bytes = prompt_path.read_bytes()
    prompt = prompt_bytes.decode("utf-8")
    prompt_sha = hashlib.sha256(prompt_bytes).hexdigest()
    if prompt_sha != str(evidence["prompt_sha256"]):
        raise ValueError("Cursor SDK prompt digest changed before send")
    agent_id = str(evidence["agent_id"])
    journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
    journal = run_root / journal_rel
    events_rel = cursor_sdk_attempt_events_rel(iteration, attempt_id)
    stderr_rel = cursor_attempt_stderr_rel(iteration, attempt_id)
    before_rel = git_status_before_cursor_rel(iteration, attempt_id)
    after_rel = git_status_after_cursor_rel(iteration, attempt_id)
    events_path = run_root / events_rel
    events_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(events_path, "", sensitive=True)
    atomic_write_text(run_root / stderr_rel, "", sensitive=True)
    atomic_write_text(run_root / before_rel, capture_git_status(repository_root) + "\n")
    empty_capture_payload = _sdk_capture_payload(
        evidence,
        run_root,
        attempt_id=attempt_id,
        agent_id=agent_id,
        native_run_id=None,
        status=None,
        stored=0,
        truncated=False,
        capture_complete=False,
    )
    adapter = _sdk_adapter(evidence)
    try:
        opened = adapter.open()
        if opened.failure is not None:
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": "cursor_sdk_preflight_failed",
                "sdk_failure": _sdk_failure_payload(opened.failure),
                **empty_capture_payload,
                **_outcome_identity(evidence),
            }
        preflight = adapter.preflight()
        resumed = adapter.resume_agent(agent_id) if preflight.failure is None else None
        if preflight.failure is not None or resumed is None or resumed.failure is not None:
            failure = preflight.failure if preflight.failure is not None else resumed.failure  # type: ignore[union-attr]
            bound_agent_missing = (
                resumed is not None
                and resumed.failure is not None
                and resumed.failure.operation is CursorSdkOperation.RESUME
                and resumed.failure.code is CursorSdkErrorCode.UNKNOWN_IDENTITY
            )
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": (
                    "cursor_sdk_native_unavailable"
                    if bound_agent_missing
                    else "cursor_sdk_preflight_failed"
                ),
                "sdk_failure": _sdk_failure_payload(failure),
                **empty_capture_payload,
                **_outcome_identity(evidence),
            }
        base: dict[str, object] = {
            "attempt_id": attempt_id,
            "dispatch_id": str(evidence["dispatch_id"]),
            "conversation_owner_run_id": str(evidence["conversation_owner_run_id"]),
            "conversation_key": str(evidence["conversation_key"]),
            "agent_id": agent_id,
            "prompt_sha256": prompt_sha,
        }
        baseline_runs, baseline_failure = _list_sdk_runs_for_agent(adapter, agent_id)
        if baseline_runs is None:
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": "cursor_sdk_preflight_failed",
                "sdk_failure": _sdk_failure_payload(baseline_failure),
                **empty_capture_payload,
                **_outcome_identity(evidence),
            }
        baseline_run_ids = sorted(run.run_id for run in baseline_runs)
        guarded = _no_progress_send_guard(adapter, evidence, agent_id, run_root)
        if guarded is not None:
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": guarded,
                **empty_capture_payload,
                **_outcome_identity(evidence),
            }
        inventory = _load_creation_inventory(evidence, run_root)
        journal_version = JOURNAL_SCHEMA_VERSION
        if (
            inventory is not None
            and inventory.runs
            and not _prior_prompt_authorized(run_root, evidence, attempt_id)
        ):
            journal_version = JOURNAL_SCHEMA_VERSION_V2
            dispatched = _dispatch_after_creation_readiness(
                adapter,
                evidence,
                run_root,
                journal=journal,
                journal_rel=journal_rel,
                base=base,
                prompt=prompt,
                agent_id=agent_id,
                iteration=iteration,
                inventory=inventory,
                baseline_runs=baseline_runs,
                empty_capture_payload=empty_capture_payload,
            )
            if isinstance(dispatched, dict):
                return dispatched
            native_run_id = dispatched.run_id
        else:
            append_sdk_journal(
                journal,
                kind="run_inventory_baseline",
                payload={**base, "run_ids": baseline_run_ids},
            )
            _journal_append(
                journal,
                kind="send_intent",
                payload=base,
                schema_version=JOURNAL_SCHEMA_VERSION,
            )
            _note_sdk_wait(run_root, attempt_id, iteration, SEND_AUTHORIZED)
            sent = adapter.send(agent_id, prompt)
            if sent.failure is not None or sent.value is None:
                append_sdk_journal(
                    journal,
                    kind="terminal",
                    payload={
                        **base,
                        "outcome": "uncertain",
                        "failure_kind": "cursor_sdk_send_uncertain",
                    },
                )
                return {
                    "runtime": "sdk-local",
                    "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                    "iteration": iteration,
                    "returncode": 1,
                    "timed_out": False,
                    "parse_ok": False,
                    "has_completion_signal": False,
                    "failure_kind": "cursor_sdk_send_uncertain",
                    "sdk_failure": _sdk_failure_payload(sent.failure),
                    **empty_capture_payload,
                    **_closed_sdk_wait_fields(run_root, attempt_id),
                    "journal_path": journal_rel,
                    "journal_sha256": sha256_file(journal),
                    **_outcome_identity(evidence),
                }
            native_run_id = sent.value.run_id
        append_sdk_journal(
            journal,
            kind="native_run_bound",
            payload={**base, "native_run_id": native_run_id},
            schema_version=journal_version,
        )
        _note_sdk_wait(run_root, attempt_id, iteration, NATIVE_HANDLE_BOUND)
        _maybe_interrupt("native_run_bound")
        stored = 0
        truncated = False
        stream_usages: list[CursorUsageEvent] = []
        status_hits: list[tuple[str, str]] = []
        terminal_status_names: list[str] = []

        def capture(envelope: CursorSdkEnvelope) -> None:
            nonlocal stored, truncated
            usage = _cursor_usage_from_envelope(envelope)
            if usage is not None:
                stream_usages.append(
                    CursorUsageEvent(
                        offset=envelope.offset,
                        ordinal=len(stream_usages) + 1,
                        usage=usage,
                    )
                )
            line = (
                json.dumps(
                    {
                        "schema_version": CURSOR_SDK_CAPTURE_SCHEMA_VERSION,
                        **asdict(envelope),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if stored + len(line) > MAX_CURSOR_SDK_EVENT_BYTES:
                truncated = True
                return
            descriptor = os.open(events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            stored += len(line)
            from ai_dev_loop.scheduler.domain.cursor_sdk_no_progress import (
                exact_status_no_progress,
                public_terminal_status_name,
            )

            terminal_name = public_terminal_status_name(envelope.payload)
            if terminal_name is not None:
                terminal_status_names.append(terminal_name)
            if exact_status_no_progress(
                envelope.payload,
                agent_id=agent_id,
                native_run_id=native_run_id,
            ):
                status_hits.append((envelope.offset, hashlib.sha256(line).hexdigest()))

        try:
            streamed = adapter.stream(agent_id, native_run_id, on_envelope=capture)
        except _DEADLINE_EXCEPTIONS as exc:
            captured = _sdk_capture_payload(
                evidence,
                run_root,
                attempt_id=attempt_id,
                agent_id=agent_id,
                native_run_id=native_run_id,
                status=None,
                stored=stored,
                truncated=truncated,
                capture_complete=False,
                stream_usages=tuple(stream_usages),
            )
            if isinstance(exc, CursorSdkOperationTimeout):
                exc.capture_payload.update(captured)
            raise
        if streamed.failure is not None or streamed.value is None:
            capture_payload = _sdk_capture_payload(
                evidence,
                run_root,
                attempt_id=attempt_id,
                agent_id=agent_id,
                native_run_id=native_run_id,
                status=None,
                stored=stored,
                truncated=truncated,
                capture_complete=False,
                stream_usages=tuple(stream_usages),
            )
            append_sdk_journal(
                journal,
                kind="terminal",
                payload={
                    **base,
                    "native_run_id": native_run_id,
                    "outcome": "uncertain",
                    "failure_kind": "cursor_sdk_native_run_uncertain",
                },
                schema_version=journal_version,
            )
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "native_run_id": native_run_id,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": "cursor_sdk_native_run_uncertain",
                "sdk_failure": _sdk_failure_payload(streamed.failure),
                **capture_payload,
                **_closed_sdk_wait_fields(run_root, attempt_id),
                "journal_path": journal_rel,
                "journal_sha256": sha256_file(journal),
                **_outcome_identity(evidence),
            }
        terminal = streamed.value
        atomic_write_text(run_root / after_rel, capture_git_status(repository_root) + "\n")
        has_completion = terminal.status.lower() == "finished" and bool(terminal.text)
        capture_payload = _sdk_capture_payload(
            evidence,
            run_root,
            attempt_id=attempt_id,
            agent_id=agent_id,
            native_run_id=native_run_id,
            status=terminal.status,
            stored=stored,
            truncated=truncated,
            capture_complete=_cursor_sdk_capture_complete(terminal.status),
            terminal=terminal,
            stream_usages=tuple(stream_usages),
            has_completion=has_completion,
        )
        run_state = _run_state_from_binding(
            {**evidence, "cursor_command": "cursor-sdk-local", "cursor_output_format": "sdk-ndjson"}
        )
        post_fp = capture_cursor_output_fingerprint(
            run_state,
            run_root,
            iteration_number=iteration,
            status_text=(run_root / after_rel).read_text(encoding="utf-8"),
            artifact_id=attempt_id,
        )
        append_sdk_journal(
            journal,
            kind="terminal",
            payload={
                **base,
                "native_run_id": native_run_id,
                "outcome": "success" if has_completion else "failed",
            },
            schema_version=journal_version,
        )
        _note_sdk_wait(run_root, attempt_id, iteration, TERMINAL_OBSERVED)
        witness_fields = _write_no_progress_witness(
            run_root,
            attempt_id=attempt_id,
            dispatch_id=str(evidence["dispatch_id"]),
            agent_id=agent_id,
            native_run_id=native_run_id,
            events_rel=events_rel,
            journal_rel=journal_rel,
            journal_path=journal,
            terminal_status=terminal.status,
            truncated=truncated,
            status_hits=tuple(status_hits),
            terminal_status_names=tuple(terminal_status_names),
            events_path=events_path,
            capture_complete=_cursor_sdk_capture_complete(terminal.status),
        )
        return {
            "runtime": "sdk-local",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "iteration": iteration,
            "agent_id": agent_id,
            "native_run_id": native_run_id,
            "returncode": 0 if has_completion else 1,
            "timed_out": False,
            "parse_ok": True,
            "has_completion_signal": has_completion,
            **_sdk_wait_fields(run_root, attempt_id),
            **capture_payload,
            "after_status_path": after_rel,
            "cursor_output_fingerprint_path": post_fp.relative_path,
            "cursor_output_fingerprint_sha256": post_fp.aggregate_sha256,
            "journal_path": journal_rel,
            "journal_sha256": sha256_file(journal),
            **witness_fields,
            **_outcome_identity(evidence),
        }
    finally:
        adapter.close()


def _list_sdk_runs_for_agent(
    adapter: CursorSdkAdapter,
    agent_id: str,
) -> tuple[list[CursorSdkTerminalRun] | None, CursorSdkFailure | None]:
    runs: list[CursorSdkTerminalRun] = []
    seen: set[str] = set()
    next_cursor: str | None = None
    while True:
        listed = adapter.list_runs(agent_id, cursor=next_cursor, limit=100)
        if listed.failure is not None or listed.value is None:
            return None, listed.failure
        for native in listed.value.runs:
            if native.agent_id != agent_id or not native.run_id:
                raise ValueError("Cursor SDK reconciliation inventory crossed the bound agent")
            if native.run_id in seen:
                raise ValueError("Cursor SDK run inventory identities conflict")
            seen.add(native.run_id)
            runs.append(native)
            if len(runs) > MAX_CURSOR_SDK_RECONCILIATION_RUNS:
                raise ValueError("Cursor SDK reconciliation inventory exceeded its bound")
        if listed.value.next_cursor is None:
            return runs, None
        if listed.value.next_cursor == next_cursor:
            raise ValueError("Cursor SDK reconciliation cursor made no progress")
        next_cursor = listed.value.next_cursor


def _prior_prompt_authorized(
    run_root: Path,
    evidence: dict[str, object],
    attempt_id: str,
) -> bool:
    """True only for the one journal the application pinned as authorized.

    The caller supplies the durable path, digest, attempt, dispatch and prompt
    binding. A self-hash of whatever file is on disk, or any other journal in
    the attempt directory, is not authority. A digest or binding mismatch is
    unsafe evidence and rejects the turn.
    """

    fields = (
        "prior_authorized_journal_path",
        "prior_authorized_journal_sha256",
        "prior_authorized_attempt_id",
        "prior_authorized_dispatch_id",
        "prior_authorized_prompt_sha256",
    )
    present = [evidence.get(name) for name in fields]
    if all(value in {None, ""} for value in present):
        return False
    if any(not isinstance(value, str) or not value for value in present):
        raise ValueError("prior prompt authority is incomplete")
    relative, digest, prior_attempt, prior_dispatch, prior_prompt = (
        str(value) for value in present
    )
    if prior_attempt == attempt_id:
        raise ValueError("prior prompt authority names the current attempt")
    if relative != cursor_sdk_attempt_journal_rel(prior_attempt):
        raise ValueError("prior prompt journal is not the source attempt journal")
    path = run_root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError("prior prompt journal is missing")
    if sha256_file(path) != digest:
        raise ValueError("prior prompt journal digest mismatch")
    binding = {
        "attempt_id": prior_attempt,
        "dispatch_id": prior_dispatch,
        "conversation_owner_run_id": str(evidence["conversation_owner_run_id"]),
        "conversation_key": str(evidence["conversation_key"]),
        "agent_id": str(evidence["agent_id"]),
        "prompt_sha256": prior_prompt,
    }
    try:
        records = authenticate_sdk_recovery_journal(
            path,
            expected_sha256=digest,
            expected_binding=binding,
        )
    except ValueError:
        records = authenticate_sdk_journal(
            path,
            expected_sha256=digest,
            operation="send",
            expected_binding=binding,
        )
    if not any(record.get("kind") == "send_intent" for record in records):
        return False
    kinds = {str(record.get("kind", "")) for record in records}
    version = records[0].get("schema_version")
    return version == JOURNAL_SCHEMA_VERSION or bool(
        kinds.intersection({"readiness_clear", "creation_blocker_cancel_confirmed"})
    )


def _prompt_journal_authorizes_send(records: tuple[dict[str, object], ...]) -> bool:
    return any(record.get("kind") == "send_intent" for record in records)


def _exclusive_creation_blocker(records: tuple[dict[str, object], ...]) -> str | None:
    baseline = records[0]
    raw = baseline.get("creation_run_ids")
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item for item in raw)
        or len(set(raw)) != len(raw)
    ):
        raise ValueError("Cursor SDK creation blocker inventory is invalid")
    if len(raw) != 1:
        return None
    run_ids = baseline.get("run_ids")
    if not isinstance(run_ids, list) or raw[0] not in run_ids:
        raise ValueError("Cursor SDK creation blocker is outside the baseline")
    return str(raw[0])


def _confirm_creation_blocker(
    adapter: CursorSdkAdapter,
    agent_id: str,
    blocker_id: str,
) -> tuple[bool, bool]:
    observed = adapter.get_run(agent_id, blocker_id)
    if observed.failure is not None or observed.value is None:
        return False, False
    if observed.value.status.strip().lower() in TERMINAL_PUBLIC_RUN_STATUSES:
        return True, False
    cancelled = adapter.cancel(agent_id, blocker_id)
    if cancelled.failure is not None:
        return False, True
    confirmed = adapter.get_run(agent_id, blocker_id)
    if (
        confirmed.failure is not None
        or confirmed.value is None
        or confirmed.value.status.strip().lower() not in TERMINAL_PUBLIC_RUN_STATUSES
    ):
        return False, True
    return True, True


def _run_sdk_reconciliation(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    with _sdk_operation_deadline(evidence):
        return _run_sdk_reconciliation_operation(evidence, run_root, attempt_id=attempt_id)


def _run_sdk_reconciliation_operation(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    source_attempt_id = str(evidence["source_attempt_id"])
    source_dispatch_id = str(evidence["source_dispatch_id"])
    source_journal_rel = str(evidence["source_journal_path"])
    source_journal_sha = str(evidence["source_journal_sha256"])
    agent_id = str(evidence["agent_id"])
    iteration = _binding_int(evidence, "iteration")
    expected_binding = {
        "attempt_id": source_attempt_id,
        "dispatch_id": source_dispatch_id,
        "conversation_owner_run_id": str(evidence["conversation_owner_run_id"]),
        "conversation_key": str(evidence["conversation_key"]),
        "agent_id": agent_id,
        "prompt_sha256": str(evidence["prompt_sha256"]),
    }
    records = authenticate_sdk_recovery_journal(
        run_root / source_journal_rel,
        expected_sha256=source_journal_sha,
        expected_binding=expected_binding,
    )
    raw_baseline = records[0].get("run_ids")
    if not isinstance(raw_baseline, list) or not all(
        isinstance(item, str) for item in raw_baseline
    ):
        raise ValueError("Cursor SDK run inventory baseline is invalid")
    baseline = tuple(sorted(raw_baseline))
    native_run_id = str(evidence.get("native_run_id") or "") or None
    journal_native = next(
        (
            str(record.get("native_run_id"))
            for record in records
            if record.get("kind") == "native_run_bound" and record.get("native_run_id")
        ),
        None,
    )
    if native_run_id is not None and journal_native != native_run_id:
        raise ValueError("Cursor SDK reconciliation native identity disagrees with journal")
    if native_run_id is None:
        native_run_id = journal_native

    events_rel = cursor_sdk_attempt_events_rel(iteration, attempt_id)
    stderr_rel = cursor_attempt_stderr_rel(iteration, attempt_id)
    before_rel = git_status_before_cursor_rel(iteration, attempt_id)
    after_rel = git_status_after_cursor_rel(iteration, attempt_id)
    events_path = run_root / events_rel
    events_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(events_path, "", sensitive=True)
    atomic_write_text(run_root / stderr_rel, "", sensitive=True)
    repository_root = str(evidence["repository_root"])
    atomic_write_text(run_root / before_rel, capture_git_status(repository_root) + "\n")

    adapter = _sdk_adapter(evidence)
    initial_status: str | None = None
    final_status: str | None = None
    cancellation_requested = False
    outcome = "native_unavailable"
    candidate_run_ids: list[str] = []
    candidate_final_statuses: dict[str, str] = {}
    abort_requested = evidence.get("abort_requested") is True
    capture_payload: dict[str, object] = {}
    blocker_ok = True
    try:
        opened = adapter.open()
        preflight = adapter.preflight() if opened.failure is None else None
        resumed = (
            adapter.resume_agent(agent_id)
            if preflight is not None and preflight.failure is None
            else None
        )
        if (
            opened.failure is not None
            or preflight is None
            or preflight.failure is not None
            or resumed is None
            or resumed.failure is not None
        ):
            outcome = "native_unavailable"
        else:
            blocker_id = _exclusive_creation_blocker(records)
            has_cancel_intent = any(
                record.get("kind") == "creation_blocker_cancel_intent" for record in records
            )
            if blocker_id is not None and (abort_requested or has_cancel_intent):
                blocker_terminal, blocker_cancel_requested = _confirm_creation_blocker(
                    adapter, agent_id, blocker_id
                )
                cancellation_requested = cancellation_requested or blocker_cancel_requested
                blocker_ok = blocker_terminal
            if native_run_id is None:
                listed, _failure = _list_sdk_runs_for_agent(adapter, agent_id)
                if listed is None:
                    outcome = "native_unavailable"
                else:
                    candidates = [run for run in listed if run.run_id not in set(baseline)]
                    candidate_run_ids = [run.run_id for run in candidates]
                    prompt_authorized = _prompt_journal_authorizes_send(records)
                    if not prompt_authorized and not abort_requested:
                        native_run_id = None
                        outcome = "prompt_unsent" if not candidates else "native_unavailable"
                    elif not candidates:
                        outcome = "abort_reconciled" if abort_requested else "ambiguous_missing"
                    elif len(candidates) != 1:
                        outcome = "ambiguous_multiple"
                    else:
                        native_run_id = candidates[0].run_id
            else:
                candidate_run_ids = [native_run_id]
            if abort_requested and candidate_run_ids:
                all_stopped = True
                for candidate_id in candidate_run_ids:
                    observed = adapter.get_run(agent_id, candidate_id)
                    if observed.failure is not None or observed.value is None:
                        all_stopped = False
                        break
                    status = observed.value.status.lower()
                    if status not in {"finished", "failed", "cancelled", "canceled", "error"}:
                        cancelled = adapter.cancel(agent_id, candidate_id)
                        cancellation_requested = True
                        if cancelled.failure is not None:
                            all_stopped = False
                            break
                        confirmed = adapter.get_run(agent_id, candidate_id)
                        if confirmed.failure is not None or confirmed.value is None:
                            all_stopped = False
                            break
                        if confirmed.value.status.lower() not in {
                            "finished",
                            "failed",
                            "cancelled",
                            "canceled",
                            "error",
                        }:
                            all_stopped = False
                            break
                        status = confirmed.value.status.lower()
                    candidate_final_statuses[candidate_id] = status
                outcome = "abort_reconciled" if all_stopped else "native_unavailable"
            terminal: CursorSdkTerminalRun | None = None
            if not abort_requested and native_run_id is not None:
                observed = adapter.get_run(agent_id, native_run_id)
                if observed.failure is not None or observed.value is None:
                    outcome = "native_unavailable"
                else:
                    terminal = observed.value
                    outcome = "continuation_ready"
                    initial_status = terminal.status
                    status = terminal.status.lower()
                    if status not in {"finished", "failed", "cancelled", "canceled", "error"}:
                        if evidence.get("no_progress_reobserve") is True:
                            terminal = None
                            outcome = "native_unavailable"
                        else:
                            cancelled = adapter.cancel(agent_id, native_run_id)
                            cancellation_requested = True
                            if cancelled.failure is not None:
                                terminal = None
                                outcome = "native_unavailable"
                            else:
                                confirmed = adapter.get_run(agent_id, native_run_id)
                                if confirmed.failure is not None or confirmed.value is None:
                                    terminal = None
                                    outcome = "native_unavailable"
                                else:
                                    terminal = confirmed.value
                                    status = terminal.status.lower()
                                    if status not in {
                                        "finished",
                                        "failed",
                                        "cancelled",
                                        "canceled",
                                        "error",
                                    }:
                                        terminal = None
                                        outcome = "native_unavailable"
                    if terminal is not None:
                        final_status = terminal.status
                        candidate_final_statuses[native_run_id] = terminal.status.lower()
                        if terminal.status.lower() == "finished" and bool(terminal.text):
                            stored = 0
                            truncated = False
                            stream_usages: list[CursorUsageEvent] = []

                            def capture(envelope: CursorSdkEnvelope) -> None:
                                nonlocal stored, truncated
                                usage = _cursor_usage_from_envelope(envelope)
                                if usage is not None:
                                    stream_usages.append(
                                        CursorUsageEvent(
                                            offset=envelope.offset,
                                            ordinal=len(stream_usages) + 1,
                                            usage=usage,
                                        )
                                    )
                                line = (
                                    json.dumps(
                                        {
                                            "schema_version": CURSOR_SDK_CAPTURE_SCHEMA_VERSION,
                                            **asdict(envelope),
                                        },
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                    + "\n"
                                ).encode("utf-8")
                                if stored + len(line) > MAX_CURSOR_SDK_EVENT_BYTES:
                                    truncated = True
                                    return
                                descriptor = os.open(
                                    events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
                                )
                                try:
                                    os.write(descriptor, line)
                                    os.fsync(descriptor)
                                finally:
                                    os.close(descriptor)
                                stored += len(line)

                            replayed = adapter.stream(
                                agent_id,
                                native_run_id,
                                mode="replay",
                                on_envelope=capture,
                            )
                            if replayed.failure is not None or replayed.value is None:
                                outcome = "native_unavailable"
                            else:
                                terminal = replayed.value
                                final_status = terminal.status
                                candidate_final_statuses[native_run_id] = terminal.status.lower()
                                atomic_write_text(
                                    run_root / after_rel,
                                    capture_git_status(repository_root) + "\n",
                                )
                                capture_payload = _sdk_capture_payload(
                                    evidence,
                                    run_root,
                                    attempt_id=attempt_id,
                                    agent_id=agent_id,
                                    native_run_id=native_run_id,
                                    status=terminal.status,
                                    stored=stored,
                                    truncated=truncated,
                                    capture_complete=_cursor_sdk_capture_complete(terminal.status),
                                    terminal=terminal,
                                    stream_usages=tuple(stream_usages),
                                    has_completion=True,
                                )
                                run_state = _run_state_from_binding(
                                    {
                                        **evidence,
                                        "cursor_command": "cursor-sdk-local",
                                        "cursor_output_format": "sdk-ndjson",
                                    }
                                )
                                fingerprint = capture_cursor_output_fingerprint(
                                    run_state,
                                    run_root,
                                    iteration_number=iteration,
                                    status_text=(run_root / after_rel).read_text(encoding="utf-8"),
                                    artifact_id=attempt_id,
                                )
                                capture_payload.update(
                                    {
                                        "after_status_path": after_rel,
                                        "cursor_output_fingerprint_path": fingerprint.relative_path,
                                        "cursor_output_fingerprint_sha256": fingerprint.aggregate_sha256,
                                    }
                                )
                                outcome = "terminal_recovered"
                        elif outcome != "native_unavailable":
                            if (
                                evidence.get("no_progress_reobserve") is True
                                and terminal.status.lower() != "error"
                            ):
                                outcome = "native_unavailable"
                            else:
                                outcome = "continuation_ready"
    finally:
        adapter.close()
    if not blocker_ok:
        outcome = "native_unavailable"

    record_model = (
        CursorSdkReconciliationRecordV2
        if outcome == "prompt_unsent"
        else CursorSdkReconciliationRecordV1
    )
    record = record_model(
        run_id=str(evidence["run_id"]),
        source_attempt_id=source_attempt_id,
        reconciliation_attempt_id=attempt_id,
        source_dispatch_id=source_dispatch_id,
        reconciliation_dispatch_id=str(evidence["dispatch_id"]),
        conversation_owner_run_id=str(evidence["conversation_owner_run_id"]),
        conversation_key=str(evidence["conversation_key"]),
        agent_id=agent_id,
        prompt_sha256=str(evidence["prompt_sha256"]),
        source_journal_path=source_journal_rel,
        source_journal_sha256=source_journal_sha,
        baseline_run_ids=baseline,
        candidate_run_ids=tuple(candidate_run_ids),
        candidate_final_statuses=candidate_final_statuses,
        native_run_id=native_run_id,
        initial_native_status=initial_status,
        final_native_status=final_status,
        outcome=outcome,  # type: ignore[arg-type]
        cancellation_requested=cancellation_requested,
        terminal_response_path=(
            str(capture_payload.get("final_response_path"))
            if outcome == "terminal_recovered"
            else None
        ),
        terminal_response_sha256=(
            str(capture_payload.get("final_response_sha256"))
            if outcome == "terminal_recovered"
            else None
        ),
        cursor_output_fingerprint_path=(
            str(capture_payload.get("cursor_output_fingerprint_path"))
            if outcome == "terminal_recovered"
            else None
        ),
        cursor_output_fingerprint_sha256=(
            str(capture_payload.get("cursor_output_fingerprint_sha256"))
            if outcome == "terminal_recovered"
            else None
        ),
    )
    record_rel = cursor_sdk_reconciliation_record_rel(
        source_attempt_id,
        attempt_id,
        schema_version=2 if outcome == "prompt_unsent" else 1,
    )
    atomic_write_json(run_root / record_rel, record.model_dump(mode="json"), sensitive=True)
    _maybe_interrupt("reconciliation_record")
    record_sha = sha256_file(run_root / record_rel)
    return {
        "runtime": "sdk-local",
        "effect_kind": RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        "iteration": iteration,
        "agent_id": agent_id,
        "native_run_id": native_run_id,
        "returncode": 0,
        "timed_out": False,
        "parse_ok": True,
        "has_completion_signal": True,
        "reconciliation_outcome": outcome,
        "reconciliation_record_path": record_rel,
        "reconciliation_record_sha256": record_sha,
        **capture_payload,
        **_outcome_identity(evidence),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scheduler Cursor attempt runner")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--unit-identity", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--invocation-evidence-sha256", required=True)
    parser.add_argument("--launch-intent-sha256", required=True)
    parser.add_argument("--launch-nonce", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--effect-kind", required=True)
    args = parser.parse_args(argv)

    run_root = run_artifact_root(Path(args.artifact_root), args.run_id)
    stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, _result_rel = (
        prepare_attempt_output_paths(run_root, args.attempt_id)
    )
    effect_kind = str(args.effect_kind)
    if effect_kind not in CURSOR_ATTEMPT_EFFECT_KINDS:
        return _write_attempt_artifacts(
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            attempt_id=args.attempt_id,
            unit_identity=args.unit_identity,
            effect_kind=effect_kind,
            dispatch_id=args.dispatch_id,
            run_id=args.run_id,
            exit_code=2,
            termination_class=TerminationClass.NONZERO_EXIT,
            stderr_text=f"unsupported effect kind: {effect_kind}",
        )

    exit_code = 0
    cursor_outcome: dict[str, object]
    try:
        evidence = authenticate_pinned_invocation_evidence(
            run_root,
            attempt_id=args.attempt_id,
            pinned_invocation_evidence_sha256=args.invocation_evidence_sha256,
            run_id=args.run_id,
            dispatch_id=args.dispatch_id,
            unit_identity=args.unit_identity,
            launch_nonce=args.launch_nonce,
            launch_intent_sha256=args.launch_intent_sha256,
            effect_kind=effect_kind,
        )
        verify_pre_execution_cursor_guards(run_root, evidence, run_id=args.run_id)
        if evidence.get("runtime") != "sdk-local":
            raise ValueError("Cursor CLI execution is retired; freshly submit sdk-local inputs")
    except Exception as exc:
        return _write_attempt_artifacts(
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            attempt_id=args.attempt_id,
            unit_identity=args.unit_identity,
            effect_kind=effect_kind,
            dispatch_id=args.dispatch_id,
            run_id=args.run_id,
            exit_code=1,
            termination_class=TerminationClass.NONZERO_EXIT,
            stderr_text=_bounded_diagnostic(exc),
            stdout_payload={"failure_kind": "pre_execution_guard_failed"},
        )

    try:
        if effect_kind == CREATE_CHAT_EFFECT_KIND:
            cursor_outcome = _run_sdk_create(evidence, run_root, attempt_id=args.attempt_id)
            raw_returncode = cursor_outcome.get("returncode", 0)
            if isinstance(raw_returncode, bool) or not isinstance(
                raw_returncode, (int, float, str)
            ):
                raise TypeError("Cursor SDK create returncode must be numeric")
            if int(raw_returncode) != 0:
                exit_code = int(raw_returncode)
        elif effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
            cursor_outcome = _run_sdk_turn(evidence, run_root, attempt_id=args.attempt_id)
            raw_returncode = cursor_outcome.get("returncode", 0)
            returncode = (
                int(raw_returncode)
                if isinstance(raw_returncode, (int, str)) and not isinstance(raw_returncode, bool)
                else 0
            )
            if cursor_outcome.get("timed_out"):
                exit_code = 124
            elif returncode != 0:
                exit_code = returncode
            elif (
                cursor_outcome.get("parse_ok") is not True
                or cursor_outcome.get("has_completion_signal") is not True
            ):
                exit_code = 1
        else:
            if effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
                raise ValueError("Cursor SDK reconciliation requires the sdk-local runtime")
            cursor_outcome = _run_sdk_reconciliation(
                evidence,
                run_root,
                attempt_id=args.attempt_id,
            )
    except (Exception, *_DEADLINE_EXCEPTIONS) as exc:
        sdk_timed_out = isinstance(exc, _DEADLINE_EXCEPTIONS)
        native_state_unavailable = (
            isinstance(exc, CursorSdkPathError) and exc.code == "native_state_unavailable"
        )
        conversation_disposed = (
            isinstance(exc, CursorSdkPathError) and exc.code == "conversation_disposed"
        )
        if native_state_unavailable:
            failure_kind = "cursor_sdk_native_unavailable"
        elif conversation_disposed:
            failure_kind = "cursor_sdk_preflight_failed"
        elif effect_kind == CREATE_CHAT_EFFECT_KIND:
            failure_kind = "cursor_chat_create_failed"
        elif effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
            failure_kind = "cursor_sdk_reconciliation_failed"
        else:
            failure_kind = "cursor_turn_execution_failed"
        diagnostic_payload: dict[str, object] = {"failure_kind": failure_kind}
        if native_state_unavailable or conversation_disposed:
            diagnostic_payload.update(
                {
                    "runtime": "sdk-local",
                    "returncode": 1,
                    "timed_out": False,
                }
            )
        if (
            evidence.get("runtime") == "sdk-local"
            and effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            and not native_state_unavailable
            and not conversation_disposed
        ):
            diagnostic_payload.update(
                _sdk_uncertainty_payload(
                    evidence,
                    run_root,
                    attempt_id=args.attempt_id,
                    effect_kind=effect_kind,
                    timed_out=sdk_timed_out,
                )
            )
            if isinstance(exc, CursorSdkOperationTimeout):
                diagnostic_payload.update(exc.capture_payload)
        failure_exit_code = 124 if sdk_timed_out else 1
        return _write_attempt_artifacts(
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            attempt_id=args.attempt_id,
            unit_identity=args.unit_identity,
            effect_kind=effect_kind,
            dispatch_id=args.dispatch_id,
            run_id=args.run_id,
            exit_code=failure_exit_code,
            termination_class=(
                TerminationClass.TIMEOUT if sdk_timed_out else TerminationClass.NONZERO_EXIT
            ),
            stderr_text=(
                _bounded_diagnostic(exc)
                if conversation_disposed or evidence.get("runtime") != "sdk-local"
                else _deadline_stderr(run_root, args.attempt_id, exc)
            ),
            stdout_payload=diagnostic_payload,
        )

    return _finalize_success_artifacts(
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_path=result_path,
        stdout_rel=stdout_rel,
        stderr_rel=stderr_rel,
        attempt_id=args.attempt_id,
        unit_identity=args.unit_identity,
        exit_code=exit_code,
        cursor_outcome=cursor_outcome,
    )


if __name__ == "__main__":
    raise SystemExit(main())
