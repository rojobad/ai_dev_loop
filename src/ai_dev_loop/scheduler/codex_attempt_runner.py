"""Detached Codex review attempt runner for scheduler fake/systemd backends."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.fresh_codex_reviewer import classify_bootstrap_session_id_from_text
from ai_dev_loop.iterations import (
    build_correction_execution_envelope,
    correction_execution_envelope_path,
    fix_prompt_path,
)
from ai_dev_loop.paths import schema_path
from ai_dev_loop.process import run_process_streaming
from ai_dev_loop.response_schema import validate_codex_response_schema
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.runners.codex import (
    build_review_wrapper_prompt,
    ensure_codex_review_artifact_dirs,
    redact_codex_args,
)
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    build_result_envelope,
    envelope_sha256,
    read_bounded_bytes,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_paths import prepare_attempt_output_paths
from ai_dev_loop.scheduler.application.codex_argv import (
    build_scheduler_codex_bootstrap_args,
    build_scheduler_codex_resume_args,
)
from ai_dev_loop.scheduler.application.codex_evidence import (
    authenticate_pinned_codex_invocation_evidence,
)
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    CODEX_ATTEMPT_EFFECT_KINDS,
    MAX_CODEX_CAPTURE_STDERR_BYTES,
    MAX_CODEX_CAPTURE_STDOUT_BYTES,
    MAX_CODEX_EVENTS_ARTIFACT_BYTES,
    MAX_CODEX_REVIEW_RESULT_BYTES,
    codex_attempt_events_rel,
    codex_attempt_stderr_rel,
    codex_review_metadata_rel,
    codex_review_report_rel,
    codex_review_result_rel,
)
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
    sha256_bytes,
)


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


def _run_state_from_binding(binding: dict[str, object], run_id: str) -> RunState:
    now = datetime.now(UTC)
    review_model = str(binding["review_model"])
    review_reasoning = str(binding["review_reasoning_effort"])
    reviewer_session_id = str(binding.get("reviewer_session_id", "") or "").strip() or None
    if reviewer_session_id:
        fresh = FreshCodexReviewerBinding(
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
            bootstrap_session_id=reviewer_session_id,
            bootstrap_events_sha256=(
                str(binding.get("bootstrap_events_sha256", "") or "").strip() or None
            ),
            bootstrap_bound_at=str(binding.get("bootstrap_bound_at", "") or "").strip() or None,
        )
        session_id = reviewer_session_id
    else:
        fresh = FreshCodexReviewerBinding(
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
        )
        session_id = None
    return RunState(
        schema_version=RUN_STATE_SCHEMA_VERSION_FRESH,
        run_id=run_id,
        status=RunStatus.REVIEWING,
        created_at=now,
        updated_at=now,
        project=ProjectRef(name="codex-attempt"),
        repository=RepositoryState(
            root=str(binding["repository_root"]),
            git_common_dir=str(binding["repository_git_common_dir"]),
            git_dir=str(binding["repository_git_dir"]),
            branch=str(binding["repository_branch"]),
            initial_head=str(binding["repository_initial_head"]),
            baseline_status_path="",
        ),
        plan=PlanState(
            repository_path=str(binding["plan_repository_path"]),
            snapshot_path=str(binding["plan_artifact_path"]),
            sha256=str(binding["plan_sha256"]),
        ),
        prompt=PromptState(
            source_repository_path=str(binding["prompt_source_repository_path"]),
            snapshot_path=str(binding["prompt_artifact_path"]),
            sha256=str(binding["prompt_sha256"]),
        ),
        codex=CodexState(
            command=str(binding["codex_command"]),
            review_model=str(binding["review_model"]),
            review_reasoning_effort=str(binding["review_reasoning_effort"]),
            review_model_source="explicit",
            review_reasoning_source="explicit",
            review_skill=str(binding["review_skill"]),
            sandbox=str(binding["codex_sandbox"]),
            session_id=session_id,
            fresh_reviewer=fresh,
        ),
        cursor=CursorState(
            command="agent",
            model="scheduler-codex-attempt",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
            chat_id=str(binding.get("cursor_chat_id", "") or "") or None,
        ),
        workflow=WorkflowState(
            max_review_iterations=_binding_int(binding, "max_review_iterations"),
            stage_mode="all",
            cursor_timeout_minutes=1,
            codex_timeout_minutes=_binding_int(binding, "codex_timeout_minutes"),
        ),
    )


def _read_cursor_final(run_root: Path, iteration_label: str) -> str | None:
    final_path = run_root / f"cursor/iterations/{iteration_label}/final.txt"
    if not final_path.is_file():
        attempt_dirs = sorted(
            (run_root / "cursor/iterations" / iteration_label).glob("*/final.txt")
        )
        if attempt_dirs:
            final_path = attempt_dirs[-1]
        else:
            return None
    text = final_path.read_text(encoding="utf-8").strip()
    return text or None


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
    codex_outcome: dict[str, object],
) -> int:
    stdout_path.write_text(json.dumps(codex_outcome, sort_keys=True) + "\n", encoding="utf-8")
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


def _run_codex_review(
    evidence: dict[str, object], run_root: Path, run_id: str
) -> dict[str, object]:
    effect_kind = str(evidence["effect_kind"])
    review_iteration = _binding_int(evidence, "review_iteration")
    iteration_label = f"{review_iteration:02d}"
    attempt_id = str(evidence["attempt_id"])
    repository_root = str(evidence["repository_root"])
    timeout_seconds = _binding_float(evidence, "codex_timeout_minutes") * 60

    events_rel = codex_attempt_events_rel(review_iteration, attempt_id)
    stderr_rel = codex_attempt_stderr_rel(review_iteration, attempt_id)
    result_rel = codex_review_result_rel(review_iteration)
    metadata_rel = codex_review_metadata_rel(review_iteration)
    report_rel = codex_review_report_rel(review_iteration)

    events_path = run_root / events_rel
    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    metadata_path = run_root / metadata_rel
    report_path = run_root / report_rel

    schema_file = schema_path("codex-review-result-v1.json")
    validate_codex_response_schema(schema_file, schema_name="codex-review-result-v1.json")
    ensure_codex_review_artifact_dirs(
        events_path,
        stderr_path,
        result_path,
        metadata_path,
        report_path,
    )

    run_state = _run_state_from_binding(evidence, run_id)
    cursor_final = _read_cursor_final(run_root, iteration_label)
    prompt = build_review_wrapper_prompt(
        run_state,
        run_root,
        iteration=iteration_label,
        cursor_final_response=cursor_final,
    )

    if effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
        args = build_scheduler_codex_bootstrap_args(
            command=str(evidence["codex_command"]),
            repo_root=repository_root,
            sandbox=str(evidence["codex_sandbox"]),
            review_model=str(evidence["review_model"]),
            review_reasoning_effort=str(evidence["review_reasoning_effort"]),
            schema_file=schema_file,
            result_file=result_path,
        )
        review_mode = "bootstrap"
        resume_session_id = ""
    else:
        resume_session_id = str(evidence["reviewer_session_id"])
        args = build_scheduler_codex_resume_args(
            command=str(evidence["codex_command"]),
            repo_root=repository_root,
            sandbox=str(evidence["codex_sandbox"]),
            review_model=str(evidence["review_model"]),
            review_reasoning_effort=str(evidence["review_reasoning_effort"]),
            session_id=resume_session_id,
            schema_file=schema_file,
            result_file=result_path,
        )
        review_mode = "resume"

    process = run_process_streaming(
        args,
        cwd=repository_root,
        timeout=timeout_seconds,
        stdin_text=prompt,
        stdout_path=events_path,
        stderr_path=stderr_path,
        sensitive=True,
        max_stdout_bytes=MAX_CODEX_CAPTURE_STDOUT_BYTES,
        max_stderr_bytes=MAX_CODEX_CAPTURE_STDERR_BYTES,
    )

    bootstrap_session_id: str | None = None
    bootstrap_uncertainty: str | None = None
    if review_mode == "bootstrap":
        events_text = ""
        if events_path.is_file() and not events_path.is_symlink():
            try:
                if events_path.stat().st_size <= MAX_CODEX_EVENTS_ARTIFACT_BYTES:
                    events_text = read_bounded_bytes(
                        events_path, MAX_CODEX_EVENTS_ARTIFACT_BYTES
                    ).decode("utf-8")
            except (OSError, UnicodeError):
                events_text = ""
        capture = classify_bootstrap_session_id_from_text(events_text)
        bootstrap_session_id = capture.session_id
        bootstrap_uncertainty = capture.uncertainty_reason

    review_result: CodexReviewResult | None = None
    review_error: str | None = None
    if result_path.is_file() and not result_path.is_symlink():
        try:
            if result_path.stat().st_size > MAX_CODEX_REVIEW_RESULT_BYTES:
                review_error = "review result artifact exceeds size bound"
            else:
                raw = read_bounded_bytes(result_path, MAX_CODEX_REVIEW_RESULT_BYTES)
                payload = json.loads(raw.decode("utf-8"))
                review_result = CodexReviewResult.model_validate(payload)
        except Exception as exc:
            review_error = type(exc).__name__
    else:
        review_error = "review result artifact missing"

    if review_result is not None:
        atomic_write_text(report_path, review_result.review_markdown + "\n", sensitive=True)
        if review_result.cursor_fix_prompt:
            fix_prompt_rel = fix_prompt_path(review_iteration)
            fix_prompt_text = review_result.cursor_fix_prompt
            atomic_write_text(run_root / fix_prompt_rel, fix_prompt_text, sensitive=True)
            envelope_rel = correction_execution_envelope_path(review_iteration)
            envelope_text = build_correction_execution_envelope(fix_prompt_text)
            atomic_write_text(run_root / envelope_rel, envelope_text, sensitive=True)
            envelope_sha = sha256_bytes(envelope_text.encode("utf-8"))

    metadata = {
        "args_redacted": redact_codex_args(args),
        "review_mode": review_mode,
        "exit_code": process.returncode,
        "timed_out": process.timed_out,
        "elapsed_seconds": process.elapsed_seconds,
        "bootstrap_session_id_prefix": (bootstrap_session_id or "")[:8] or None,
        "bootstrap_uncertainty_reason": bootstrap_uncertainty,
        "review_error": review_error,
    }
    atomic_write_json(metadata_path, metadata, sensitive=True)

    outcome: dict[str, object] = {
        "effect_kind": effect_kind,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": str(evidence["dispatch_id"]),
        "review_iteration": review_iteration,
        "review_mode": review_mode,
        "returncode": process.returncode,
        "timed_out": process.timed_out,
        "events_path": events_rel,
        "stderr_path": stderr_rel,
        "review_result_path": result_rel,
        "review_result_sha256": sha256_file(result_path) if result_path.is_file() else "",
        "metadata_path": metadata_rel,
        "bootstrap_session_id": bootstrap_session_id or "",
        "bootstrap_uncertainty_reason": bootstrap_uncertainty or "",
        "resume_session_id": resume_session_id,
        "has_actionable_findings": (
            review_result.has_actionable_findings if review_result is not None else False
        ),
    }
    if review_result is not None and review_result.cursor_fix_prompt:
        outcome["fix_prompt_path"] = fix_prompt_path(review_iteration)
        outcome["fix_prompt_sha256"] = sha256_bytes(review_result.cursor_fix_prompt.encode("utf-8"))
        outcome["execution_envelope_path"] = correction_execution_envelope_path(review_iteration)
        outcome["execution_envelope_sha256"] = envelope_sha
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scheduler Codex review attempt runner")
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
    if effect_kind not in CODEX_ATTEMPT_EFFECT_KINDS:
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
    try:
        evidence = authenticate_pinned_codex_invocation_evidence(
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
        codex_outcome = _run_codex_review(evidence, run_root, args.run_id)
        if codex_outcome.get("timed_out"):
            exit_code = 124
        else:
            returncode = codex_outcome.get("returncode", 0)
            if isinstance(returncode, bool) or not isinstance(returncode, int):
                returncode = 0
            if returncode != 0:
                exit_code = returncode
            elif codex_outcome.get("bootstrap_uncertainty_reason") or not codex_outcome.get(
                "review_result_sha256"
            ):
                exit_code = 2
            else:
                try:
                    result_artifact = run_root / str(codex_outcome["review_result_path"])
                    if (
                        not result_artifact.is_file()
                        or result_artifact.stat().st_size > MAX_CODEX_REVIEW_RESULT_BYTES
                    ):
                        exit_code = 2
                    else:
                        raw = read_bounded_bytes(result_artifact, MAX_CODEX_REVIEW_RESULT_BYTES)
                        payload = json.loads(raw.decode("utf-8"))
                        CodexReviewResult.model_validate(payload)
                except Exception:
                    exit_code = 2
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

    return _finalize_success_artifacts(
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_path=result_path,
        stdout_rel=stdout_rel,
        stderr_rel=stderr_rel,
        attempt_id=args.attempt_id,
        unit_identity=args.unit_identity,
        exit_code=exit_code,
        codex_outcome=codex_outcome,
    )


if __name__ == "__main__":
    raise SystemExit(main())
