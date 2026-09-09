"""Authenticated Cursor attempt evidence helpers for scheduler Phase 17.4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.iterations import iteration_label
from ai_dev_loop.runners.cursor_output import (
    fingerprints_match,
    load_fingerprint_artifact,
    recompute_cursor_output_fingerprint,
    recompute_usage_limit_failure_fingerprint,
)
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    validate_completion_evidence,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    RepositoryBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.state import RunState, sha256_bytes


class CursorEvidenceError(ValueError):
    """Raised when Cursor attempt evidence fails authentication or validation."""


def usage_limit_continuation_path_for_attempt(iteration_number: int, attempt_id: str) -> str:
    return (
        f"prompts/cursor-recovery/{iteration_label(iteration_number)}."
        f"{attempt_id}.usage-limit-continuation.txt"
    )


@dataclass(frozen=True)
class FrozenRepositoryIdentity:
    root: str
    git_common_dir: str
    git_dir: str
    branch: str
    initial_head: str


def parse_admission_artifact(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    required = ("branch", "head")
    missing = [item for item in required if item not in values]
    if missing:
        raise CursorEvidenceError(
            f"admission artifact missing required fields: {', '.join(missing)}"
        )
    return values


def frozen_repository_identity(
    context: SubmittedRunContext,
    *,
    run_id: str,
    artifacts: ProtectedArtifactStore,
    checkpoint: AdmittedRunCheckpoint | None,
) -> FrozenRepositoryIdentity:
    repository = context.repository
    if isinstance(repository, RepositoryBinding):
        return FrozenRepositoryIdentity(
            root=repository.root,
            git_common_dir=repository.git_common_dir,
            git_dir=repository.git_dir,
            branch=repository.branch,
            initial_head=repository.initial_head,
        )
    if checkpoint is None:
        raise CursorEvidenceError("admission checkpoint required for repository target binding")
    admission_bytes = artifacts.read_verified_bytes(
        run_id,
        checkpoint.admission_status_artifact_path,
        expected_sha256=checkpoint.admission_status_sha256,
    )
    admission = parse_admission_artifact(admission_bytes.decode("utf-8"))
    git_common_dir = admission.get("git_common_dir", "")
    git_dir = admission.get("git_dir", "")
    if not git_common_dir or not git_dir:
        from ai_dev_loop.runners.git import discover_repository

        discovered = discover_repository(Path(repository.root))
        git_common_dir = str(discovered.git_common_dir)
        git_dir = str(discovered.git_dir)
    return FrozenRepositoryIdentity(
        root=repository.root,
        git_common_dir=git_common_dir,
        git_dir=git_dir,
        branch=admission["branch"],
        initial_head=admission["head"],
    )


def validate_frozen_repository_identity(
    repo_root: Path,
    identity: FrozenRepositoryIdentity,
    *,
    context: str,
) -> None:
    from ai_dev_loop.runners.git import validate_repository_identity

    validate_repository_identity(
        repo_root,
        expected_root=identity.root,
        expected_git_common_dir=identity.git_common_dir,
        expected_git_dir=identity.git_dir,
        expected_branch=identity.branch,
        expected_head=identity.initial_head,
        context=context,
    )


def verify_prompt_binding(
    run_root: Path,
    *,
    prompt_path: str,
    prompt_sha256: str,
) -> None:
    path = run_root / prompt_path
    if not path.is_file():
        raise CursorEvidenceError("prompt artifact missing for invocation binding")
    digest = sha256_bytes(path.read_bytes())
    if digest != prompt_sha256:
        raise CursorEvidenceError("prompt artifact hash does not match invocation binding")


def canonical_invocation_evidence_text(evidence: dict[str, object]) -> str:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def invocation_evidence_sha256(evidence: dict[str, object]) -> str:
    return payload_sha256(canonical_invocation_evidence_text(evidence))


def frozen_repository_identity_from_evidence(
    evidence: dict[str, object],
) -> FrozenRepositoryIdentity:
    return FrozenRepositoryIdentity(
        root=str(evidence["repository_root"]),
        git_common_dir=str(evidence["repository_git_common_dir"]),
        git_dir=str(evidence["repository_git_dir"]),
        branch=str(evidence["repository_branch"]),
        initial_head=str(evidence["repository_initial_head"]),
    )


def verify_pre_execution_cursor_guards(
    run_root: Path,
    evidence: dict[str, object],
    *,
    run_id: str,
) -> None:
    effect_kind = str(evidence.get("effect_kind", ""))
    repo_root = Path(str(evidence["repository_root"]))
    identity = frozen_repository_identity_from_evidence(evidence)
    validate_frozen_repository_identity(
        repo_root,
        identity,
        context="before cursor execution",
    )
    fingerprint_path = evidence.get("usage_limit_fingerprint_path")
    fingerprint_sha = evidence.get("usage_limit_fingerprint_sha256")
    if fingerprint_path and fingerprint_sha:
        iteration_raw = evidence["iteration"]
        if isinstance(iteration_raw, bool) or not isinstance(iteration_raw, (int, str)):
            raise CursorEvidenceError("invocation evidence iteration must be numeric")
        iteration_number = int(iteration_raw)
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
        )

        now = datetime.now(UTC)
        run_state = RunState(
            schema_version=RUN_STATE_SCHEMA_VERSION_FRESH,
            run_id=run_id,
            status=RunStatus.STAGING,
            created_at=now,
            updated_at=now,
            project=ProjectRef(name="cursor-attempt"),
            repository=RepositoryState(
                root=identity.root,
                git_common_dir=identity.git_common_dir,
                git_dir=identity.git_dir,
                branch=identity.branch,
                initial_head=identity.initial_head,
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
                command=str(evidence["cursor_command"]),
                model=str(evidence["cursor_model"]),
                output_format=str(evidence["cursor_output_format"]),
                force=bool(evidence.get("cursor_force", True)),
                trust_workspace=bool(evidence.get("cursor_trust_workspace", True)),
                sandbox=str(evidence["cursor_sandbox"]),
                chat_id=str(evidence.get("chat_id", "")) or None,
            ),
            workflow=WorkflowState(
                max_review_iterations=1,
                stage_mode="all",
                cursor_timeout_minutes=1,
                codex_timeout_minutes=1,
            ),
        )
        verify_recorded_usage_limit_fingerprint(
            run_state,
            run_root,
            iteration_number=iteration_number,
            recorded_path=str(fingerprint_path),
            recorded_sha256=str(fingerprint_sha),
        )
    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        verify_prompt_binding(
            run_root,
            prompt_path=str(evidence["prompt_path"]),
            prompt_sha256=str(evidence["prompt_sha256"]),
        )


def authenticate_pinned_invocation_evidence(
    run_root: Path,
    *,
    attempt_id: str,
    pinned_invocation_evidence_sha256: str,
    run_id: str,
    dispatch_id: str,
    unit_identity: str,
    launch_nonce: str,
    launch_intent_sha256: str,
    effect_kind: str,
) -> dict[str, object]:
    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    if not evidence_path.is_file():
        raise CursorEvidenceError("invocation evidence artifact missing")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CursorEvidenceError("invocation evidence is not valid JSON") from exc
    if not isinstance(evidence, dict):
        raise CursorEvidenceError("invocation evidence must be a JSON object")
    if invocation_evidence_sha256(evidence) != pinned_invocation_evidence_sha256:
        raise CursorEvidenceError("pinned invocation evidence digest mismatch")
    verified = verify_cursor_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=launch_nonce,
        launch_intent_sha256=launch_intent_sha256,
        effect_kind=effect_kind,
    )
    verify_pre_execution_cursor_guards(run_root, verified, run_id=run_id)
    return verified


def verify_cursor_invocation_evidence(
    run_root: Path,
    *,
    attempt_id: str,
    run_id: str,
    dispatch_id: str,
    unit_identity: str,
    launch_nonce: str,
    launch_intent_sha256: str,
    effect_kind: str,
) -> dict[str, object]:
    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    if not evidence_path.is_file():
        raise CursorEvidenceError("invocation evidence artifact missing")
    evidence_bytes = evidence_path.read_bytes()
    try:
        evidence = json.loads(evidence_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CursorEvidenceError("invocation evidence is not valid JSON") from exc
    if not isinstance(evidence, dict):
        raise CursorEvidenceError("invocation evidence must be a JSON object")
    evidence_hash = invocation_evidence_sha256(evidence)
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": unit_identity,
            "launch_nonce": launch_nonce,
            "effect_kind": effect_kind,
            "invocation_evidence_sha256": evidence_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if payload_sha256(launch_intent) != launch_intent_sha256:
        raise CursorEvidenceError("launch intent does not match invocation evidence")
    if str(evidence.get("attempt_id", "")) != attempt_id:
        raise CursorEvidenceError("invocation evidence attempt_id mismatch")
    if str(evidence.get("run_id", "")) != run_id:
        raise CursorEvidenceError("invocation evidence run_id mismatch")
    if str(evidence.get("dispatch_id", "")) != dispatch_id:
        raise CursorEvidenceError("invocation evidence dispatch_id mismatch")
    if str(evidence.get("effect_kind", "")) != effect_kind:
        raise CursorEvidenceError("invocation evidence effect_kind mismatch")
    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        verify_prompt_binding(
            run_root,
            prompt_path=str(evidence["prompt_path"]),
            prompt_sha256=str(evidence["prompt_sha256"]),
        )
    return evidence


def verify_invocation_binding(
    run_root: Path,
    attempt_id: str,
    expected: dict[str, object],
) -> None:
    del expected
    raise CursorEvidenceError("mutable invocation binding verification is not authoritative")


def load_authenticated_cursor_outcome(
    run_root: Path,
    *,
    attempt_id: str,
    unit_identity: str,
    result_rel: str,
    stdout_rel: str,
    stderr_rel: str,
    observed_exit_code: int | None = None,
    observed_termination: TerminationClass | None = None,
    expected_envelope_sha256: str | None = None,
    expected_dispatch_id: str | None = None,
    expected_effect_kind: str | None = None,
) -> dict[str, object]:
    validated = validate_completion_evidence(
        run_root=run_root,
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        result_rel=result_rel,
        stdout_rel=stdout_rel,
        stderr_rel=stderr_rel,
        observed_exit_code=observed_exit_code,
        observed_termination=observed_termination,
    )
    if expected_envelope_sha256 and validated.envelope_sha256 != expected_envelope_sha256:
        raise CursorEvidenceError("completion envelope hash does not match attempt record")
    stdout_path = run_root / stdout_rel
    try:
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CursorEvidenceError("authenticated stdout outcome is not valid JSON") from exc
    if not isinstance(outcome, dict):
        raise CursorEvidenceError("authenticated stdout outcome must be a JSON object")
    effect_kind = str(outcome.get("effect_kind", ""))
    if effect_kind not in {CREATE_CHAT_EFFECT_KIND, RUN_CURSOR_TURN_EFFECT_KIND}:
        raise CursorEvidenceError("authenticated outcome effect_kind is invalid")
    if expected_effect_kind and effect_kind != expected_effect_kind:
        raise CursorEvidenceError("authenticated outcome effect_kind does not match dispatch")
    if expected_dispatch_id and str(outcome.get("dispatch_id", "")) != expected_dispatch_id:
        raise CursorEvidenceError("authenticated outcome dispatch_id does not match attempt")
    if str(outcome.get("attempt_id", "")) != attempt_id:
        raise CursorEvidenceError("authenticated outcome attempt_id does not match attempt")
    if (
        validated.exit_code != 0
        and effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        and not outcome.get("timed_out")
        and int(outcome.get("returncode", validated.exit_code)) == 0
    ):
        raise CursorEvidenceError("outcome disagrees with envelope termination")
    return outcome


def validate_cursor_turn_outcome_semantics(outcome: dict[str, object]) -> None:
    if bool(outcome.get("timed_out")):
        raise CursorEvidenceError("cursor turn timed out")
    returncode = outcome.get("returncode", 0)
    if isinstance(returncode, bool) or not isinstance(returncode, (int, str)):
        raise CursorEvidenceError("cursor turn returncode is invalid")
    if int(returncode) != 0:
        raise CursorEvidenceError("cursor turn exited nonzero")
    parse_ok = outcome.get("parse_ok")
    if parse_ok is not True:
        raise CursorEvidenceError("cursor stream output was not parseable")
    if outcome.get("has_completion_signal") is not True:
        raise CursorEvidenceError("cursor stream output did not include a completion signal")
    failure_code = outcome.get("failure_code")
    if failure_code:
        raise CursorEvidenceError("cursor turn reported a failure code on success path")


def verify_recorded_cursor_fingerprint(
    run_state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
    recorded_path: str,
    recorded_sha256: str,
) -> None:
    recorded = load_fingerprint_artifact(run_directory, recorded_path)
    if recorded is None:
        raise CursorEvidenceError("recorded cursor output fingerprint artifact is missing")
    if str(recorded.get("aggregate_sha256", "")) != recorded_sha256:
        raise CursorEvidenceError("recorded cursor output fingerprint hash mismatch")
    current = recompute_cursor_output_fingerprint(
        run_state,
        iteration_number=iteration_number,
    )
    if not fingerprints_match(recorded, current):
        raise CursorEvidenceError("cursor output fingerprint does not match current worktree")


def verify_recorded_usage_limit_fingerprint(
    run_state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
    recorded_path: str,
    recorded_sha256: str,
) -> None:
    recorded = load_fingerprint_artifact(run_directory, recorded_path)
    if recorded is None:
        raise CursorEvidenceError("recorded usage-limit fingerprint artifact is missing")
    if str(recorded.get("aggregate_sha256", "")) != recorded_sha256:
        raise CursorEvidenceError("recorded usage-limit fingerprint hash mismatch")
    current = recompute_usage_limit_failure_fingerprint(
        run_state,
        iteration_number=iteration_number,
    )
    if not fingerprints_match(recorded, current):
        raise CursorEvidenceError("usage-limit fingerprint does not match current worktree")


def run_state_with_frozen_identity(
    *,
    run_id: str,
    context: SubmittedRunContext,
    repo_root: Path,
    checkpoint: AdmittedRunCheckpoint | None,
    artifacts: ProtectedArtifactStore,
    chat_id: str | None = None,
) -> RunState:
    identity = frozen_repository_identity(
        context,
        run_id=run_id,
        artifacts=artifacts,
        checkpoint=checkpoint,
    )
    from ai_dev_loop.scheduler.application.run_state_bridge import run_state_from_scheduler_context

    run_state = run_state_from_scheduler_context(
        run_id=run_id,
        context=context,
        repo_root=repo_root,
        chat_id=chat_id,
        checkpoint=checkpoint,
        artifacts=artifacts,
    )
    return run_state.model_copy(
        update={
            "repository": run_state.repository.model_copy(
                update={
                    "root": identity.root,
                    "git_common_dir": identity.git_common_dir,
                    "git_dir": identity.git_dir,
                    "branch": identity.branch,
                    "initial_head": identity.initial_head,
                }
            )
        }
    )
