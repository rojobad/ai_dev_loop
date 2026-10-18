"""Durable Codex implementer attempt runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_dev_loop.fresh_codex_reviewer import classify_bootstrap_session_id_from_text
from ai_dev_loop.paths import ensure_dir
from ai_dev_loop.process import run_process_streaming
from ai_dev_loop.runners.codex_failure import classify_codex_review_events_path
from ai_dev_loop.runners.probes import capture_git_status
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_paths import prepare_attempt_output_paths
from ai_dev_loop.scheduler.application.codex_argv import (
    build_scheduler_codex_implementer_bootstrap_args,
    build_scheduler_codex_implementer_resume_args,
)
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    authenticate_pinned_codex_implementer_evidence,
)
from ai_dev_loop.scheduler.application.codex_subprocess_env import sanitize_codex_subprocess_env
from ai_dev_loop.scheduler.application.cursor_evidence import verify_prompt_binding
from ai_dev_loop.scheduler.application.token_usage_capture import capture_codex_usage_safely
from ai_dev_loop.scheduler.application.worktree_boundary import worktree_content_fingerprint
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    IMPLEMENTER_ATTEMPT_EFFECT_KINDS,
    MAX_CODEX_CAPTURE_STDERR_BYTES,
    MAX_CODEX_CAPTURE_STDOUT_BYTES,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    codex_implementer_events_rel,
    codex_implementer_final_rel,
    codex_implementer_latest_final_rel,
    codex_implementer_metadata_rel,
    codex_implementer_session_capture_rel,
    codex_implementer_status_after_rel,
    codex_implementer_status_before_rel,
    codex_implementer_stderr_rel,
    codex_implementer_wait_intent_rel,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.state import atomic_write_json, atomic_write_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scheduler Codex implementer attempt runner")
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
    if effect_kind not in IMPLEMENTER_ATTEMPT_EFFECT_KINDS:
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
    try:
        evidence = authenticate_pinned_codex_implementer_evidence(
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
        outcome = _execute(evidence, run_root, args.run_id)
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
            stderr_text=f"{type(exc).__name__}: {exc}"[:240],
            stdout_payload={"failure_kind": "pre_execution_guard_failed"},
        )

    exit_code = 0
    if outcome.get("timed_out"):
        exit_code = 124
    else:
        returncode = outcome.get("returncode", 0)
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            returncode = 0
        if returncode != 0:
            exit_code = returncode
        elif (
            outcome.get("usage_limit")
            or outcome.get("bootstrap_uncertainty_reason")
            or outcome.get("output_incomplete")
        ):
            exit_code = 2
    return _finalize_success_artifacts(
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_path=result_path,
        stdout_rel=stdout_rel,
        stderr_rel=stderr_rel,
        attempt_id=args.attempt_id,
        unit_identity=args.unit_identity,
        exit_code=exit_code,
        outcome=outcome,
    )


def _execute(evidence: dict[str, object], run_root: Path, run_id: str) -> dict[str, object]:
    effect_kind = str(evidence["effect_kind"])
    raw_iteration = evidence.get("iteration")
    iteration = raw_iteration if isinstance(raw_iteration, int) else 1
    attempt_id = str(evidence["attempt_id"])
    prompt_path = str(evidence["prompt_path"])
    prompt_sha = str(evidence["prompt_sha256"])
    verify_prompt_binding(run_root, prompt_path=prompt_path, prompt_sha256=prompt_sha)
    # Decode the authenticated bytes directly. Path.read_text() enables universal
    # newline translation, which would rewrite a frozen CRLF/mixed-newline prompt
    # after its digest had already been verified.
    prompt_text = (run_root / prompt_path).read_bytes().decode("utf-8")
    repo_root = str(evidence["repository_root"])
    command = str(evidence["codex_command"])
    model = str(evidence["model"])
    effort = str(evidence["reasoning_effort"])
    session_id = str(evidence.get("session_id") or "").strip()
    raw_timeout = evidence.get("timeout_seconds")
    timeout_seconds = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else 0.0
    turn = str(evidence.get("turn") or "initial")
    if effect_kind == BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND:
        args = build_scheduler_codex_implementer_bootstrap_args(
            command=command,
            repo_root=repo_root,
            model=model,
            reasoning_effort=effort,
        )
    elif effect_kind == RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
        args = build_scheduler_codex_implementer_resume_args(
            command=command,
            repo_root=repo_root,
            model=model,
            reasoning_effort=effort,
            session_id=session_id,
        )
    else:
        raise CodexEvidenceError(f"unsupported effect kind: {effect_kind}")
    if "--last" in args:
        raise CodexEvidenceError("codex implementer argv contains --last")

    events_rel = codex_implementer_events_rel(iteration, attempt_id)
    stderr_rel = codex_implementer_stderr_rel(iteration, attempt_id)
    final_rel = codex_implementer_final_rel(iteration, attempt_id)
    metadata_rel = codex_implementer_metadata_rel(iteration, attempt_id)
    before_rel = codex_implementer_status_before_rel(iteration, attempt_id)
    after_rel = codex_implementer_status_after_rel(iteration, attempt_id)
    events_path = run_root / events_rel
    stderr_path = run_root / stderr_rel
    for relative in (events_rel, stderr_rel, final_rel, metadata_rel, before_rel, after_rel):
        ensure_dir((run_root / relative).parent)

    before_text = _status_text(repo_root)
    atomic_write_text(run_root / before_rel, before_text, sensitive=True)
    process = run_process_streaming(
        args,
        cwd=repo_root,
        timeout=timeout_seconds if timeout_seconds > 0 else None,
        stdin_text=prompt_text,
        stdout_path=events_path,
        stderr_path=stderr_path,
        sensitive=True,
        env=sanitize_codex_subprocess_env(),
        max_stdout_bytes=MAX_CODEX_CAPTURE_STDOUT_BYTES,
        max_stderr_bytes=MAX_CODEX_CAPTURE_STDERR_BYTES,
        drain_after_limit=True,
    )
    after_text = _status_text(repo_root)
    atomic_write_text(run_root / after_rel, after_text, sensitive=True)
    partial_work_sha256 = worktree_content_fingerprint(repo_root, status_text=after_text)
    events_text = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    final_text = _final_message(events_text)
    atomic_write_text(run_root / final_rel, final_text, sensitive=True)
    stable_final = run_root / codex_implementer_latest_final_rel(iteration)
    ensure_dir(stable_final.parent)
    atomic_write_text(stable_final, final_text, sensitive=True)

    truncated = bool(process.stdout_truncated or process.stderr_truncated)
    limit_classification = classify_codex_review_events_path(events_path)
    usage_limit = limit_classification.is_usage_limit
    if effect_kind == BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND:
        classification = classify_bootstrap_session_id_from_text(events_text)
        captured_session = classification.session_id
        uncertainty = classification.uncertainty_reason
    else:
        captured_session = session_id or None
        observed_sessions = _thread_started_ids(events_text)
        uncertainty = (
            "resume_identity_conflict"
            if any(item != session_id for item in observed_sessions)
            else None
        )
    if captured_session:
        session_rel = codex_implementer_session_capture_rel(attempt_id)
        atomic_write_json(
            run_root / session_rel,
            {
                "schema_version": 1,
                "role": "implementer",
                "session_id": captured_session,
                "attempt_id": attempt_id,
                "effect_kind": effect_kind,
            },
            sensitive=True,
        )
        session_sha = sha256_file(run_root / session_rel)
    else:
        session_rel = None
        session_sha = None
    work_observed = _work_observed(events_text)
    final_sha = payload_sha256(final_text)
    metadata = {
        "schema_version": 1,
        "role": "implementer",
        "turn": turn,
        "effect_kind": effect_kind,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "prompt_sha256": prompt_sha,
        "prompt_artifact_path": prompt_path,
        "events_path": events_rel,
        "stderr_path": stderr_rel,
        "final_path": final_rel,
        "status_before_path": before_rel,
        "status_before_sha256": sha256_file(run_root / before_rel),
        "status_after_path": after_rel,
        "status_after_sha256": sha256_file(run_root / after_rel),
        "partial_work_sha256": partial_work_sha256,
        "session_id": captured_session,
        "session_capture_path": session_rel,
        "session_capture_sha256": session_sha,
        "bootstrap_uncertainty_reason": uncertainty,
        "usage_limit": usage_limit,
        "failure_code": limit_classification.code.value,
        "limit_evidence_kind": limit_classification.evidence.value,
        "truncated": truncated,
        "stdout_truncated": process.stdout_truncated,
        "stderr_truncated": process.stderr_truncated,
        "stdout_captured_bytes": events_path.stat().st_size,
        "stderr_captured_bytes": stderr_path.stat().st_size,
        "timed_out": process.timed_out,
        "returncode": process.returncode,
        "worktree_unchanged": before_text == after_text,
        "work_observed": work_observed,
        "final_sha256": final_sha,
    }
    atomic_write_json(run_root / metadata_rel, metadata, sensitive=True)
    usage_capture = capture_codex_usage_safely(
        run_root=run_root,
        run_id=run_id,
        role="implementer",
        stage="initial" if turn == "initial" else "correction",
        scheduler_attempt_id=attempt_id,
        publisher_attempt_id=attempt_id,
        source_dispatch_id=str(evidence["dispatch_id"]),
        source_artifact_path=events_rel,
        source_artifact_sha256=sha256_file(events_path),
        capture_complete=not truncated,
    )
    # A nonzero result with a resolved process and authenticated identity may be
    # reclassified by the scheduler's bounded post-failure capacity probe. Keep
    # the exact role/turn/prompt continuation available for that decision too;
    # the artifact alone never authorizes a retry.
    post_failure_probe_eligible = bool(
        process.returncode > 0
        and not process.timed_out
        and not truncated
        and captured_session
        and not uncertainty
    )
    if usage_limit or post_failure_probe_eligible:
        wait_intent_rel, wait_intent_sha = _write_wait_intent(run_root, metadata)
    else:
        wait_intent_rel = None
        wait_intent_sha = None
    return {
        "role": "implementer",
        "turn": turn,
        "effect_kind": effect_kind,
        "attempt_id": attempt_id,
        "dispatch_id": str(evidence["dispatch_id"]),
        "run_id": run_id,
        "returncode": process.returncode,
        "timed_out": process.timed_out,
        "usage_limit": usage_limit,
        "failure_code": limit_classification.code.value,
        "limit_evidence_kind": limit_classification.evidence.value,
        "output_incomplete": truncated,
        "session_id": captured_session or "",
        "bootstrap_uncertainty_reason": uncertainty or "",
        "session_capture_path": session_rel or "",
        "session_capture_sha256": session_sha or "",
        "wait_intent_path": wait_intent_rel or "",
        "wait_intent_sha256": wait_intent_sha or "",
        "events_path": events_rel,
        "stderr_path": stderr_rel,
        "final_path": final_rel,
        "metadata_path": metadata_rel,
        "metadata_sha256": sha256_file(run_root / metadata_rel),
        "events_sha256": sha256_file(events_path),
        "stderr_sha256": sha256_file(stderr_path),
        "status_before_path": before_rel,
        "status_before_sha256": sha256_file(run_root / before_rel),
        "status_after_path": after_rel,
        "status_after_sha256": sha256_file(run_root / after_rel),
        "partial_work_sha256": partial_work_sha256,
        "prompt_sha256": prompt_sha,
        "prompt_artifact_path": prompt_path,
        "worktree_unchanged": before_text == after_text,
        "work_observed": work_observed,
        "final_sha256": final_sha,
        **usage_capture.outcome_fields(),
    }


def _write_wait_intent(run_root: Path, metadata: dict[str, object]) -> tuple[str, str]:
    attempt_id = str(metadata["attempt_id"])
    relative = codex_implementer_wait_intent_rel(attempt_id)
    ensure_dir((run_root / relative).parent)
    atomic_write_json(
        run_root / relative,
        {
            "schema_version": 1,
            "role": "implementer",
            "turn": metadata["turn"],
            "prompt_sha256": metadata["prompt_sha256"],
            "prompt_artifact_path": metadata["prompt_artifact_path"],
            "session_id": metadata["session_id"],
            "session_capture_path": metadata["session_capture_path"],
            "session_capture_sha256": metadata["session_capture_sha256"],
            "partial_output_path": metadata["final_path"],
            "partial_output_sha256": metadata["final_sha256"],
            "terminated_attempt_id": attempt_id,
            "events_path": metadata["events_path"],
            "worktree_unchanged": metadata["worktree_unchanged"],
            "work_observed": metadata["work_observed"],
        },
        sensitive=True,
    )
    return relative, sha256_file(run_root / relative)


def _final_message(stdout_text: str) -> str:
    messages: list[str] = []
    for line in stdout_text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            messages.append(text)
    return messages[-1] if messages else ""


def _thread_started_ids(stdout_text: str) -> tuple[str, ...]:
    identities: list[str] = []
    for line in stdout_text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "thread.started":
            continue
        identity = payload.get("thread_id")
        if isinstance(identity, str) and identity.strip() and identity not in identities:
            identities.append(identity)
    return tuple(identities)


def _work_observed(stdout_text: str) -> bool:
    """Conservatively recognize provider events that prove a turn began work."""

    for line in stdout_text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type", ""))
        if event_type.startswith("item.") or event_type in {
            "turn.started",
            "turn.completed",
            "response.completed",
        }:
            return True
    return False


def _status_text(repo_root: str) -> str:
    raw = capture_git_status(repo_root)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


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
    }
    if stdout_payload:
        payload.update(stdout_payload)
    atomic_write_text(stderr_path, stderr_text[:240] + "\n", sensitive=True)
    atomic_write_text(
        stdout_path,
        json.dumps(payload, sort_keys=True) + "\n",
        sensitive=True,
    )
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
    atomic_write_text(result_path, envelope.decode("utf-8"), sensitive=True)
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
    outcome: dict[str, object],
) -> int:
    atomic_write_text(
        stdout_path,
        json.dumps(outcome, sort_keys=True) + "\n",
        sensitive=True,
    )
    atomic_write_text(stderr_path, "", sensitive=True)
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
    atomic_write_text(result_path, envelope.decode("utf-8"), sensitive=True)
    _ = envelope_sha256(envelope)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
