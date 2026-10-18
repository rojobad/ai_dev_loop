"""Detached Cursor attempt runner for scheduler systemd/fake backends."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from ai_dev_loop.config import CursorSdkSection
from ai_dev_loop.integrations.cursor_sdk.adapter import (
    CursorSdkAdapter,
    build_cursor_sdk_client_config,
)
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkEnvelope,
    CursorSdkFailure,
    CursorSdkTerminalRun,
)
from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.runners.cursor import (
    CREATE_CHAT_METADATA_REL,
    CREATE_CHAT_STDERR_REL,
    CREATE_CHAT_STDOUT_REL,
    create_chat,
    execute_prompt,
)
from ai_dev_loop.runners.cursor_output import (
    capture_cursor_output_fingerprint,
    capture_usage_limit_failure_fingerprint,
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
    append_sdk_journal,
    authenticate_sdk_recovery_journal,
)
from ai_dev_loop.scheduler.application.output_observation import write_cursor_output_observation
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    CURSOR_SDK_CAPTURE_SCHEMA_VERSION,
    CURSOR_SDK_EVENT_STREAM_FORMAT,
    CURSOR_SDK_TEXT_STREAM_FORMAT,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_events_rel,
    cursor_attempt_final_rel,
    cursor_attempt_metadata_rel,
    cursor_attempt_stderr_rel,
    cursor_sdk_attempt_events_rel,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_attempt_metadata_rel,
    cursor_sdk_reconciliation_record_rel,
    git_status_after_cursor_rel,
    git_status_before_cursor_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import CursorSdkReconciliationRecordV1
from ai_dev_loop.scheduler.domain.effects import CURSOR_ATTEMPT_EFFECT_KINDS
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
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


class CursorSdkOperationTimeout(BaseException):
    """Dedicated worker deadline that SDK exception translation cannot consume."""


@contextmanager
def _sdk_operation_deadline(evidence: dict[str, object]) -> Iterator[None]:
    timeout_seconds = _binding_float(evidence, "timeout_seconds")
    if timeout_seconds <= 0:
        raise ValueError("Cursor SDK timeout_seconds must be positive")
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_expired(_signum: int, _frame: FrameType | None) -> None:
        raise CursorSdkOperationTimeout("Cursor SDK operation exceeded its frozen deadline")

    signal.signal(signal.SIGALRM, deadline_expired)
    # Repeating delivery also bounds a bridge close that starts while unwinding
    # the first deadline exception.
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds, min(timeout_seconds, 0.25))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


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
    stderr_path.write_text("", encoding="utf-8")
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


def _run_create_chat(evidence: dict[str, object], run_root: Path, run_id: str) -> dict[str, object]:
    cursor_command = str(evidence["cursor_command"])
    repository_root = str(evidence["repository_root"])
    timeout_seconds = _binding_float(evidence, "timeout_seconds")
    chat_id = create_chat(
        cursor_command,
        repo_root=repository_root,
        timeout_seconds=timeout_seconds,
        run_directory=run_root,
        run_id=run_id,
    )
    stdout_path = run_root / CREATE_CHAT_STDOUT_REL
    stderr_path = run_root / CREATE_CHAT_STDERR_REL
    outcome: dict[str, object] = {
        "effect_kind": CREATE_CHAT_EFFECT_KIND,
        "chat_id": chat_id,
        "create_chat_metadata_path": CREATE_CHAT_METADATA_REL,
        "create_chat_stdout_artifact_path": CREATE_CHAT_STDOUT_REL,
        "create_chat_stderr_artifact_path": CREATE_CHAT_STDERR_REL,
        "has_completion_signal": True,
        **_outcome_identity(evidence),
    }
    if stdout_path.is_file() and not stdout_path.is_symlink():
        outcome["create_chat_stdout_sha256"] = sha256_file(stdout_path)
    if stderr_path.is_file() and not stderr_path.is_symlink():
        outcome["create_chat_stderr_sha256"] = sha256_file(stderr_path)
    return outcome


def _run_cursor_turn(
    evidence: dict[str, object],
    run_root: Path,
    run_id: str,
    *,
    attempt_id: str,
) -> dict[str, object]:
    iteration_number = _binding_int(evidence, "iteration")
    repository_root = str(evidence["repository_root"])
    chat_id = str(evidence["chat_id"])
    prompt_path = run_root / str(evidence["prompt_path"])
    prompt = prompt_path.read_text(encoding="utf-8")
    timeout_seconds = _binding_float(evidence, "timeout_seconds")
    cursor = CursorState(
        command=str(evidence["cursor_command"]),
        model=str(evidence["cursor_model"]),
        output_format=str(evidence["cursor_output_format"]),
        force=bool(evidence.get("cursor_force", True)),
        trust_workspace=bool(evidence.get("cursor_trust_workspace", True)),
        sandbox=str(evidence["cursor_sandbox"]),
        chat_id=chat_id,
    )
    before_status_rel = git_status_before_cursor_rel(iteration_number, attempt_id)
    after_status_rel = git_status_after_cursor_rel(iteration_number, attempt_id)
    events_rel = cursor_attempt_events_rel(iteration_number, attempt_id)
    stderr_rel = cursor_attempt_stderr_rel(iteration_number, attempt_id)
    metadata_rel = cursor_attempt_metadata_rel(iteration_number, attempt_id)
    final_rel = cursor_attempt_final_rel(iteration_number, attempt_id)
    before_status_path = run_root / before_status_rel
    after_status_path = run_root / after_status_rel
    events_path = run_root / events_rel
    stderr_path = run_root / stderr_rel
    events_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(before_status_path, capture_git_status(repository_root) + "\n")
    execution = execute_prompt(
        cursor,
        repo_root=repository_root,
        chat_id=chat_id,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        stdout_path=events_path,
        stderr_path=stderr_path,
        run_directory=run_root,
        run_id=run_id,
        iteration_number=iteration_number,
    )
    atomic_write_text(after_status_path, capture_git_status(repository_root) + "\n")
    after_status = after_status_path.read_text(encoding="utf-8")
    has_completion_signal = bool(execution.parse.final_text)
    metadata_payload: dict[str, object] = {
        "args": execution.metadata_args,
        "exit_code": execution.process.returncode,
        "elapsed_seconds": execution.process.elapsed_seconds,
        "timed_out": execution.process.timed_out,
        "parse_ok": execution.parse.parse_ok,
        "has_completion_signal": has_completion_signal,
        "errors": list(execution.parse.errors),
        "structured_errors": _structured_error_records(execution.process.stdout),
    }
    if execution.failure_code:
        metadata_payload["failure_code"] = execution.failure_code
    final_response_sha256: str | None = None
    if has_completion_signal and execution.parse.final_text:
        atomic_write_text(run_root / final_rel, execution.parse.final_text, sensitive=True)
        final_response_sha256 = hashlib.sha256(
            execution.parse.final_text.encode("utf-8")
        ).hexdigest()
        metadata_payload["final_response_path"] = final_rel
        metadata_payload["final_response_sha256"] = final_response_sha256
    atomic_write_json(run_root / metadata_rel, metadata_payload, sensitive=True)
    stdout_stored = events_path.stat().st_size if events_path.is_file() else 0
    stderr_stored = stderr_path.stat().st_size if stderr_path.is_file() else 0
    write_cursor_output_observation(
        run_root,
        run_id=run_id,
        attempt_id=attempt_id,
        iteration=iteration_number,
        stdout_stored_bytes=stdout_stored,
        stderr_stored_bytes=stderr_stored,
        stdout_truncated=execution.process.stdout_truncated,
        stderr_truncated=execution.process.stderr_truncated,
    )

    run_state = _run_state_from_binding(evidence)
    fingerprint_path = ""
    fingerprint_sha = ""
    usage_limit_fingerprint_path = ""
    usage_limit_fingerprint_sha = ""
    if execution.failure_code == "cursor_usage_limit":
        usage_fp = capture_usage_limit_failure_fingerprint(
            run_state,
            run_root,
            iteration_number=iteration_number,
            status_text=after_status,
            artifact_id=attempt_id,
        )
        usage_limit_fingerprint_path = usage_fp.relative_path
        usage_limit_fingerprint_sha = usage_fp.aggregate_sha256
    elif execution.process.returncode == 0 and not execution.process.timed_out:
        post_fp = capture_cursor_output_fingerprint(
            run_state,
            run_root,
            iteration_number=iteration_number,
            status_text=after_status,
            artifact_id=attempt_id,
        )
        fingerprint_path = post_fp.relative_path
        fingerprint_sha = post_fp.aggregate_sha256

    return {
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "iteration": iteration_number,
        "returncode": execution.process.returncode,
        "timed_out": execution.process.timed_out,
        "parse_ok": execution.parse.parse_ok,
        "has_completion_signal": has_completion_signal,
        "failure_code": execution.failure_code,
        "metadata_path": metadata_rel,
        "after_status_path": after_status_rel,
        "structured_errors": metadata_payload["structured_errors"],
        "cursor_output_fingerprint_path": fingerprint_path,
        "cursor_output_fingerprint_sha256": fingerprint_sha,
        "usage_limit_fingerprint_path": usage_limit_fingerprint_path,
        "usage_limit_fingerprint_sha256": usage_limit_fingerprint_sha,
        "final_response_path": final_rel if final_response_sha256 else None,
        "final_response_sha256": final_response_sha256,
        **_outcome_identity(evidence),
    }


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
    config = build_cursor_sdk_client_config(
        _sdk_selection(evidence),
        workspace=repository_root,
        repository_root=repository_root,
        conversation_owner_identity=owner,
    )
    return CursorSdkAdapter(config, repository_root=repository_root)


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
        append_sdk_journal(
            journal,
            kind="terminal",
            payload={**terminal, "outcome": "uncertain", "failure_kind": failure_kind},
        )
    payload.update(
        {
            "failure_kind": failure_kind,
            "journal_path": journal_rel,
            "journal_sha256": sha256_file(journal),
        }
    )
    payload.update(_existing_sdk_capture_payload(evidence, run_root, attempt_id=attempt_id))
    return payload


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
        append_sdk_journal(journal, kind="create_intent", payload=base)
        created = adapter.create_agent()
        if created.failure is not None or created.value is None:
            append_sdk_journal(
                journal,
                kind="terminal",
                payload={
                    **base,
                    "outcome": "uncertain",
                    "failure_kind": "cursor_sdk_create_uncertain",
                },
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
        append_sdk_journal(journal, kind="agent_bound", payload={**base, "agent_id": agent_id})
        append_sdk_journal(
            journal,
            kind="terminal",
            payload={**base, "agent_id": agent_id, "outcome": "success"},
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
        "usage": asdict(terminal.usage) if terminal is not None and terminal.usage else None,
        "final_response_path": final_rel if final_sha else None,
        "final_response_format": CURSOR_SDK_TEXT_STREAM_FORMAT if final_sha else None,
        "final_response_sha256": final_sha,
    }
    atomic_write_json(run_root / metadata_rel, metadata, sensitive=True)
    return {
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


def _run_sdk_turn(
    evidence: dict[str, object],
    run_root: Path,
    *,
    attempt_id: str,
) -> dict[str, object]:
    with _sdk_operation_deadline(evidence):
        return _run_sdk_turn_operation(evidence, run_root, attempt_id=attempt_id)


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
            return {
                "runtime": "sdk-local",
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "iteration": iteration,
                "returncode": 1,
                "timed_out": False,
                "parse_ok": False,
                "has_completion_signal": False,
                "failure_kind": "cursor_sdk_preflight_failed",
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
        baseline_run_ids: list[str] = []
        next_cursor: str | None = None
        while True:
            listed = adapter.list_runs(agent_id, cursor=next_cursor, limit=100)
            if listed.failure is not None or listed.value is None:
                return {
                    "runtime": "sdk-local",
                    "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                    "iteration": iteration,
                    "returncode": 1,
                    "timed_out": False,
                    "parse_ok": False,
                    "has_completion_signal": False,
                    "failure_kind": "cursor_sdk_preflight_failed",
                    "sdk_failure": _sdk_failure_payload(listed.failure),
                    **empty_capture_payload,
                    **_outcome_identity(evidence),
                }
            for native in listed.value.runs:
                if native.agent_id != agent_id or not native.run_id:
                    raise ValueError("Cursor SDK run inventory crossed the bound agent")
                if native.run_id not in baseline_run_ids:
                    baseline_run_ids.append(native.run_id)
                if len(baseline_run_ids) > MAX_CURSOR_SDK_RECONCILIATION_RUNS:
                    raise ValueError("Cursor SDK run inventory exceeded the reconciliation bound")
            if listed.value.next_cursor is None:
                break
            if listed.value.next_cursor == next_cursor:
                raise ValueError("Cursor SDK run inventory cursor made no progress")
            next_cursor = listed.value.next_cursor
        append_sdk_journal(
            journal,
            kind="run_inventory_baseline",
            payload={**base, "run_ids": sorted(baseline_run_ids)},
        )
        append_sdk_journal(journal, kind="send_intent", payload=base)
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
                "journal_path": journal_rel,
                "journal_sha256": sha256_file(journal),
                **_outcome_identity(evidence),
            }
        native_run_id = sent.value.run_id
        append_sdk_journal(
            journal, kind="native_run_bound", payload={**base, "native_run_id": native_run_id}
        )
        stored = 0
        truncated = False

        def capture(envelope: CursorSdkEnvelope) -> None:
            nonlocal stored, truncated
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

        try:
            streamed = adapter.stream(agent_id, native_run_id, on_envelope=capture)
        except CursorSdkOperationTimeout:
            _sdk_capture_payload(
                evidence,
                run_root,
                attempt_id=attempt_id,
                agent_id=agent_id,
                native_run_id=native_run_id,
                status=None,
                stored=stored,
                truncated=truncated,
                capture_complete=False,
            )
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
            capture_complete=True,
            terminal=terminal,
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
            **capture_payload,
            "after_status_path": after_rel,
            "cursor_output_fingerprint_path": post_fp.relative_path,
            "cursor_output_fingerprint_sha256": post_fp.aggregate_sha256,
            "journal_path": journal_rel,
            "journal_sha256": sha256_file(journal),
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
            if native.run_id not in seen:
                seen.add(native.run_id)
                runs.append(native)
            if len(runs) > MAX_CURSOR_SDK_RECONCILIATION_RUNS:
                raise ValueError("Cursor SDK reconciliation inventory exceeded its bound")
        if listed.value.next_cursor is None:
            return runs, None
        if listed.value.next_cursor == next_cursor:
            raise ValueError("Cursor SDK reconciliation cursor made no progress")
        next_cursor = listed.value.next_cursor


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
            if native_run_id is None:
                listed, _failure = _list_sdk_runs_for_agent(adapter, agent_id)
                if listed is None:
                    outcome = "native_unavailable"
                else:
                    candidates = [run for run in listed if run.run_id not in set(baseline)]
                    candidate_run_ids = [run.run_id for run in candidates]
                    if not candidates:
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

                            def capture(envelope: CursorSdkEnvelope) -> None:
                                nonlocal stored, truncated
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
                                    capture_complete=True,
                                    terminal=terminal,
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
                            outcome = "continuation_ready"
    finally:
        adapter.close()

    record = CursorSdkReconciliationRecordV1(
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
    record_rel = cursor_sdk_reconciliation_record_rel(source_attempt_id, attempt_id)
    atomic_write_json(run_root / record_rel, record.model_dump(mode="json"), sensitive=True)
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
        sdk_runtime = evidence.get("runtime") == "sdk-local"
        if effect_kind == CREATE_CHAT_EFFECT_KIND:
            cursor_outcome = (
                _run_sdk_create(evidence, run_root, attempt_id=args.attempt_id)
                if sdk_runtime
                else _run_create_chat(evidence, run_root, args.run_id)
            )
            raw_returncode = cursor_outcome.get("returncode", 0)
            if isinstance(raw_returncode, bool) or not isinstance(
                raw_returncode, (int, float, str)
            ):
                raise TypeError("Cursor SDK create returncode must be numeric")
            if sdk_runtime and int(raw_returncode) != 0:
                exit_code = int(raw_returncode)
        elif effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
            cursor_outcome = (
                _run_sdk_turn(evidence, run_root, attempt_id=args.attempt_id)
                if sdk_runtime
                else _run_cursor_turn(
                    evidence,
                    run_root,
                    args.run_id,
                    attempt_id=args.attempt_id,
                )
            )
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
            if not sdk_runtime or effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
                raise ValueError("Cursor SDK reconciliation requires the sdk-local runtime")
            cursor_outcome = _run_sdk_reconciliation(
                evidence,
                run_root,
                attempt_id=args.attempt_id,
            )
    except (Exception, CursorSdkOperationTimeout) as exc:
        sdk_timed_out = isinstance(exc, CursorSdkOperationTimeout)
        failure_kind = (
            "cursor_chat_create_failed"
            if effect_kind == CREATE_CHAT_EFFECT_KIND
            else (
                "cursor_sdk_reconciliation_failed"
                if effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                else "cursor_turn_execution_failed"
            )
        )
        diagnostic_payload: dict[str, object] = {"failure_kind": failure_kind}
        if (
            evidence.get("runtime") == "sdk-local"
            and effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
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
                f"{type(exc).__name__}: Cursor SDK worker failed"
                if evidence.get("runtime") == "sdk-local"
                else _bounded_diagnostic(exc)
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
