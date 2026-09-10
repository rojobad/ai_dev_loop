"""Detached Cursor attempt runner for scheduler systemd/fake backends."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.cursor import (
    CREATE_CHAT_METADATA_REL,
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
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_events_rel,
    cursor_attempt_final_rel,
    cursor_attempt_metadata_rel,
    cursor_attempt_stderr_rel,
    git_status_after_cursor_rel,
    git_status_before_cursor_rel,
)
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
    return {
        "effect_kind": CREATE_CHAT_EFFECT_KIND,
        "chat_id": chat_id,
        "create_chat_metadata_path": CREATE_CHAT_METADATA_REL,
        "has_completion_signal": True,
        **_outcome_identity(evidence),
    }


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
    atomic_write_json(run_root / metadata_rel, metadata_payload, sensitive=True)
    if has_completion_signal and execution.parse.final_text:
        atomic_write_text(run_root / final_rel, execution.parse.final_text, sensitive=True)

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
        if effect_kind == CREATE_CHAT_EFFECT_KIND:
            cursor_outcome = _run_create_chat(evidence, run_root, args.run_id)
        else:
            cursor_outcome = _run_cursor_turn(
                evidence,
                run_root,
                args.run_id,
                attempt_id=args.attempt_id,
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
    except Exception as exc:
        failure_kind = (
            "cursor_chat_create_failed"
            if effect_kind == CREATE_CHAT_EFFECT_KIND
            else "cursor_turn_execution_failed"
        )
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
            stdout_payload={"failure_kind": failure_kind},
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
