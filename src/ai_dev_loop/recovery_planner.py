"""Pure recoverability analysis for terminal failed runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.abort_control import read_active_process, validate_active_process_metadata
from ai_dev_loop.commands.start_preflight import (
    validate_plan_contract,
    validate_prompt_contract,
    validate_timeouts,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import read_codex_session_runtime
from ai_dev_loop.iterations import (
    find_iteration,
    iteration_label,
    iteration_staged_patch_rel_path,
    max_iteration_number,
)
from ai_dev_loop.resume_planner import (
    WorkflowActionKind,
    cursor_turn_complete,
    review_result_available,
)
from ai_dev_loop.review_runtime import is_legacy_phase9_codex_state
from ai_dev_loop.runners.git import (
    discover_repository,
    git_status_porcelain,
    paths_with_unstaged_changes,
    paths_with_untracked,
    validate_staged_patch_matches_artifact,
)
from ai_dev_loop.runners.staging import staging_complete_for_iteration
from ai_dev_loop.state import (
    RECOVERY_CHECKPOINTS,
    CodexState,
    RunState,
    RunStatus,
    sha256_file,
)


class SessionRuntimeAction(StrEnum):
    PRESERVE_PHASE10 = "preserve_phase10"
    MIGRATE_PHASE9 = "phase9_session_capture"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedRecoveryRuntime:
    session_model: str
    session_reasoning_effort: str
    review_model: str
    review_reasoning_effort: str
    review_model_source: str
    review_reasoning_source: str
    model_family_warning: str | None
    session_origin: str
    source_event_type: str
    source_timestamp: str | None
    runtime_migration: str
    session_runtime_action: SessionRuntimeAction


@dataclass
class RecoveryAnalysis:
    eligible: bool
    source_run_id: str
    source_status: str
    checkpoint: str | None = None
    iteration: int | None = None
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    staged_patch_sha256: str | None = None
    session_runtime_action: SessionRuntimeAction = SessionRuntimeAction.UNRESOLVED
    reason_code: str | None = None
    resolved_runtime: ResolvedRecoveryRuntime | None = None
    existing_successor_run_id: str | None = None
    existing_successor_status: str | None = None
    reused_existing_successor: bool = False


def _add_blocker(blockers: list[str], code: str) -> None:
    if code not in blockers:
        blockers.append(code)


def _read_chat_id_from_artifact(run_directory: Path) -> str | None:
    chat_path = run_directory / "cursor" / "chat.json"
    if not chat_path.is_file():
        return None
    try:
        payload = json.loads(chat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    chat_id = payload.get("chat_id")
    return chat_id if isinstance(chat_id, str) and chat_id.strip() else None


def _has_invalid_review_json(run_directory: Path, iteration_number: int) -> bool:
    result_path = run_directory / f"codex/reviews/{iteration_label(iteration_number)}.json"
    if not result_path.is_file():
        return False
    return not review_result_available(run_directory, iteration_number)


def derive_recovery_reason_code(
    run_directory: Path,
    *,
    checkpoint: str,
    iteration_number: int,
) -> str:
    if checkpoint == "process_review":
        return "codex_review_processing_failed"
    if _has_invalid_review_json(run_directory, iteration_number):
        return "codex_review_result_invalid"
    return "codex_review_failed"


def is_phase10_frozen_codex_state(codex: CodexState) -> bool:
    """True when prepare froze complete Phase 10 session and effective runtime provenance."""

    return (
        codex.review_model is not None
        and codex.review_reasoning_effort is not None
        and codex.session_model is not None
        and codex.session_reasoning_effort is not None
        and codex.review_model_source in {"session", "explicit"}
        and codex.review_reasoning_source in {"session", "explicit"}
    )


def needs_phase9_session_capture(codex: CodexState) -> bool:
    """True for historical Phase 9 states, including model-only or reasoning-only overrides."""

    if is_legacy_phase9_codex_state(
        review_model=codex.review_model,
        review_reasoning_effort=codex.review_reasoning_effort,
        review_model_source=codex.review_model_source,
        review_reasoning_source=codex.review_reasoning_source,
    ):
        return True
    if is_phase10_frozen_codex_state(codex):
        return False
    # Phase 9 stored optional independent overrides without provenance or session fields.
    return (
        codex.review_model_source is None
        and codex.review_reasoning_source is None
        and codex.session_model is None
        and codex.session_reasoning_effort is None
    )


def _phase9_override_or_session(
    *,
    configured_value: str | None,
    configured_source: str | None,
    session_value: str,
) -> tuple[str, str]:
    """Preserve each Phase 9 non-null override independently; fill the rest from session."""

    if configured_source == "explicit" and configured_value:
        return configured_value, "explicit"
    if configured_value and configured_source in {None, "legacy_inherit"}:
        return configured_value, "explicit"
    return session_value, "session"


def resolve_recovery_runtime(
    state: RunState,
    run_directory: Path | None = None,
) -> ResolvedRecoveryRuntime:
    """Resolve successor review runtime from source state or exact session capture."""

    codex = state.codex
    if needs_phase9_session_capture(codex):
        session = read_codex_session_runtime(codex.session_id)
        review_model, review_model_source = _phase9_override_or_session(
            configured_value=codex.review_model,
            configured_source=codex.review_model_source,
            session_value=session.model,
        )
        review_reasoning_effort, review_reasoning_source = _phase9_override_or_session(
            configured_value=codex.review_reasoning_effort,
            configured_source=codex.review_reasoning_source,
            session_value=session.reasoning_effort,
        )
        return ResolvedRecoveryRuntime(
            session_model=session.model,
            session_reasoning_effort=session.reasoning_effort,
            review_model=review_model,
            review_reasoning_effort=review_reasoning_effort,
            review_model_source=review_model_source,
            review_reasoning_source=review_reasoning_source,
            model_family_warning=codex.model_family_warning,
            session_origin=session.origin,
            source_event_type=session.source_event_type,
            source_timestamp=session.source_timestamp,
            runtime_migration="phase9_session_capture",
            session_runtime_action=SessionRuntimeAction.MIGRATE_PHASE9,
        )

    if (
        not codex.review_model
        or not codex.review_reasoning_effort
        or not codex.session_model
        or not codex.session_reasoning_effort
        or codex.review_model_source not in {"session", "explicit"}
        or codex.review_reasoning_source not in {"session", "explicit"}
    ):
        raise ValidationError(
            "source Phase 10 review runtime is incomplete or inconsistent; "
            "cannot preserve frozen values"
        )
    origin = "preserved"
    source_event_type = "preserved"
    source_timestamp: str | None = None
    if run_directory is not None:
        artifact = run_directory / "codex" / "session-runtime.json"
        if artifact.is_file():
            try:
                payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                payload = None
            if isinstance(payload, dict):
                if payload.get("model") not in {None, codex.session_model}:
                    raise ValidationError(
                        "source session-runtime.json model conflicts with frozen CodexState"
                    )
                if payload.get("reasoning_effort") not in {
                    None,
                    codex.session_reasoning_effort,
                }:
                    raise ValidationError(
                        "source session-runtime.json reasoning conflicts with frozen CodexState"
                    )
                if payload.get("review_model") not in {None, codex.review_model}:
                    raise ValidationError(
                        "source session-runtime.json review_model conflicts with frozen CodexState"
                    )
                if payload.get("review_reasoning_effort") not in {
                    None,
                    codex.review_reasoning_effort,
                }:
                    raise ValidationError(
                        "source session-runtime.json review_reasoning conflicts with frozen CodexState"
                    )
                origin_value = payload.get("origin")
                event_value = payload.get("source_event_type")
                ts_value = payload.get("source_timestamp")
                if isinstance(origin_value, str) and origin_value:
                    origin = origin_value
                if isinstance(event_value, str) and event_value:
                    source_event_type = event_value
                if isinstance(ts_value, str) and ts_value:
                    source_timestamp = ts_value
    return ResolvedRecoveryRuntime(
        session_model=codex.session_model,
        session_reasoning_effort=codex.session_reasoning_effort,
        review_model=codex.review_model,
        review_reasoning_effort=codex.review_reasoning_effort,
        review_model_source=codex.review_model_source,
        review_reasoning_source=codex.review_reasoning_source,
        model_family_warning=codex.model_family_warning,
        session_origin=origin,
        source_event_type=source_event_type,
        source_timestamp=source_timestamp,
        runtime_migration="none",
        session_runtime_action=SessionRuntimeAction.PRESERVE_PHASE10,
    )


def resolved_runtime_from_successor(successor: RunState) -> ResolvedRecoveryRuntime:
    """Build display/runtime metadata from an already created successor, without recapture."""

    recovery = successor.recovery
    if recovery is None:
        raise ValidationError("successor is missing recovery lineage")
    codex = successor.codex
    if (
        not codex.session_model
        or not codex.session_reasoning_effort
        or not codex.review_model
        or not codex.review_reasoning_effort
        or not codex.review_model_source
        or not codex.review_reasoning_source
    ):
        raise ValidationError("successor review runtime is incomplete")
    migration = recovery.runtime_migration
    action = (
        SessionRuntimeAction.MIGRATE_PHASE9
        if migration == "phase9_session_capture"
        else SessionRuntimeAction.PRESERVE_PHASE10
    )
    return ResolvedRecoveryRuntime(
        session_model=codex.session_model,
        session_reasoning_effort=codex.session_reasoning_effort,
        review_model=codex.review_model,
        review_reasoning_effort=codex.review_reasoning_effort,
        review_model_source=codex.review_model_source,
        review_reasoning_source=codex.review_reasoning_source,
        model_family_warning=codex.model_family_warning,
        session_origin="successor",
        source_event_type="successor",
        source_timestamp=None,
        runtime_migration=migration,
        session_runtime_action=action,
    )


def apply_resolved_runtime_to_codex(
    codex: CodexState,
    resolved: ResolvedRecoveryRuntime,
) -> CodexState:
    return CodexState(
        command=codex.command,
        session_id=codex.session_id,
        session_model=resolved.session_model,
        session_reasoning_effort=resolved.session_reasoning_effort,
        review_model=resolved.review_model,
        review_reasoning_effort=resolved.review_reasoning_effort,
        review_model_source=resolved.review_model_source,
        review_reasoning_source=resolved.review_reasoning_source,
        model_family_warning=resolved.model_family_warning,
        review_skill=codex.review_skill,
        sandbox=codex.sandbox,
    )


def analyze_recovery(
    state: RunState,
    run_directory: Path,
    *,
    resolve_runtime: bool = True,
) -> RecoveryAnalysis:
    """Analyze whether a failed run can safely produce a review-retry successor."""

    blockers: list[str] = []
    warnings: list[str] = []
    analysis = RecoveryAnalysis(
        eligible=False,
        source_run_id=state.run_id,
        source_status=state.status.value,
        blockers=blockers,
        warnings=warnings,
    )

    if state.status != RunStatus.FAILED:
        _add_blocker(blockers, "source_status_not_failed")
        return analysis

    try:
        validate_timeouts(state)
    except ValidationError:
        _add_blocker(blockers, "invalid_timeouts")

    if not state.codex.session_id.strip():
        _add_blocker(blockers, "missing_codex_session_id")

    active = read_active_process(run_directory)
    if active is not None:
        validation = validate_active_process_metadata(
            run_directory,
            run_id=state.run_id,
            metadata=active,
        )
        if validation is None:
            _add_blocker(blockers, "ambiguous_active_process_metadata")
        elif validation.is_live and not validation.is_stale:
            _add_blocker(blockers, "active_child_process")
        elif (validation.is_live and validation.is_stale) or (
            validation.is_stale and not validation.pgid_checked_live
        ):
            _add_blocker(blockers, "ambiguous_active_process_metadata")
        else:
            warnings.append("stale_inactive_process_metadata_present")

    try:
        repo_info = discover_repository(Path(state.repository.root))
    except ValidationError:
        _add_blocker(blockers, "repository_unavailable")
        return analysis

    if repo_info.root.resolve() != Path(state.repository.root).resolve():
        _add_blocker(blockers, "repository_root_mismatch")
    if repo_info.git_common_dir.resolve() != Path(state.repository.git_common_dir).resolve():
        _add_blocker(blockers, "git_common_dir_mismatch")
    if repo_info.git_dir.resolve() != Path(state.repository.git_dir).resolve():
        _add_blocker(blockers, "git_dir_mismatch")
    if repo_info.branch != state.repository.branch:
        _add_blocker(blockers, "branch_mismatch")
    if repo_info.head != state.repository.initial_head:
        _add_blocker(blockers, "head_mismatch")

    try:
        validate_plan_contract(state, run_directory)
    except ValidationError:
        _add_blocker(blockers, "plan_contract_invalid")
    try:
        validate_prompt_contract(state, run_directory)
    except ValidationError:
        _add_blocker(blockers, "prompt_contract_invalid")

    state_chat_id = state.cursor.chat_id
    artifact_chat_id = _read_chat_id_from_artifact(run_directory)
    if not state_chat_id:
        _add_blocker(blockers, "missing_cursor_chat_id")
    elif artifact_chat_id is None:
        _add_blocker(blockers, "missing_cursor_chat_artifact")
    elif artifact_chat_id != state_chat_id:
        _add_blocker(blockers, "cursor_chat_id_mismatch")

    iteration_number = max_iteration_number(state)
    if iteration_number < 1:
        _add_blocker(blockers, "missing_iteration_metadata")
        return analysis
    if find_iteration(state, iteration_number) is None:
        _add_blocker(blockers, "ambiguous_iteration_metadata")
        return analysis

    analysis.iteration = iteration_number
    label = iteration_label(iteration_number)

    if not cursor_turn_complete(run_directory, iteration_number):
        _add_blocker(blockers, "cursor_turn_incomplete")
        _add_blocker(blockers, "phase11_supports_post_staging_only")
        return analysis

    if not staging_complete_for_iteration(state, run_directory, label):
        _add_blocker(blockers, "staging_incomplete")
        _add_blocker(blockers, "phase11_supports_post_staging_only")
        return analysis

    try:
        patch_rel = iteration_staged_patch_rel_path(state, iteration_number)
        patch_artifact = run_directory / patch_rel
        if not patch_artifact.is_file():
            _add_blocker(blockers, "staged_patch_artifact_missing")
        else:
            # Record the source artifact hash even when the live index drifted, so
            # matching successors remain discoverable for dry-run/diagnostics.
            analysis.staged_patch_sha256 = sha256_file(patch_artifact)
            try:
                validate_staged_patch_matches_artifact(
                    Path(state.repository.root), patch_artifact
                )
            except ValidationError:
                _add_blocker(blockers, "staged_patch_drift")
    except ValidationError:
        _add_blocker(blockers, "staged_patch_artifact_missing")

    try:
        status = git_status_porcelain(Path(state.repository.root))
        if paths_with_unstaged_changes(status):
            _add_blocker(blockers, "unstaged_tracked_changes")
        if paths_with_untracked(status):
            _add_blocker(blockers, "untracked_files")
    except ValidationError:
        _add_blocker(blockers, "git_status_unavailable")

    if review_result_available(run_directory, iteration_number):
        checkpoint = "process_review"
        next_kind = WorkflowActionKind.PROCESS_REVIEW
    else:
        checkpoint = "reviewing"
        next_kind = WorkflowActionKind.REVIEW

    if next_kind not in {WorkflowActionKind.REVIEW, WorkflowActionKind.PROCESS_REVIEW}:
        _add_blocker(blockers, "phase11_supports_post_staging_only")
        return analysis

    if checkpoint not in RECOVERY_CHECKPOINTS:
        _add_blocker(blockers, "unsupported_checkpoint")
        return analysis

    analysis.checkpoint = checkpoint
    analysis.reason_code = derive_recovery_reason_code(
        run_directory,
        checkpoint=checkpoint,
        iteration_number=iteration_number,
    )

    if resolve_runtime:
        try:
            resolved = resolve_recovery_runtime(state, run_directory)
            analysis.resolved_runtime = resolved
            analysis.session_runtime_action = resolved.session_runtime_action
            if resolved.runtime_migration == "phase9_session_capture":
                warnings.append(
                    "phase9_runtime_captured_at_recovery; "
                    "values reflect current session metadata, not a prepare-time freeze"
                )
        except ValidationError as exc:
            message = str(exc).lower()
            if "session" in message or "rollout" in message or "ambiguous" in message:
                _add_blocker(blockers, "session_runtime_unavailable")
            else:
                _add_blocker(blockers, "review_runtime_unresolved")
            analysis.session_runtime_action = SessionRuntimeAction.UNRESOLVED

    analysis.eligible = len(blockers) == 0
    return analysis
