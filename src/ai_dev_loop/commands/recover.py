"""Recover eligible failed runs by creating immutable-source successor runs."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import EventLevel, append_orchestrator_event
from ai_dev_loop.iterations import (
    iteration_label,
    source_prompt_for_usage_limit_recovery,
    write_usage_limit_continuation_envelope,
)
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import ensure_dir, run_dir, runs_dir, set_sensitive_file_mode
from ai_dev_loop.recovery_planner import (
    RecoveryAnalysis,
    ResolvedRecoveryRuntime,
    SessionRuntimeAction,
    analyze_recovery,
    apply_resolved_runtime_to_codex,
    resolve_recovery_runtime,
    resolved_runtime_from_successor,
)
from ai_dev_loop.resume_planner import (
    TERMINAL_STATUSES,
    cursor_turn_complete,
    review_result_available,
)
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.cursor_output import (
    build_usage_limit_adopted_payload,
    usage_limit_adopted_fingerprint_rel_path,
)
from ai_dev_loop.runners.staging import staging_complete_for_iteration
from ai_dev_loop.state import (
    ManifestArtifact,
    RecoveryState,
    RunManifest,
    RunState,
    RunStatus,
    atomic_write_json,
    atomic_write_text,
    generate_run_id,
    load_run_state,
    serialize_run_state,
    sha256_file,
    utc_now,
)


@dataclass(frozen=True)
class RecoveryResult:
    source_run_id: str
    recovery_run_id: str
    checkpoint: str
    iteration: int
    cursor_chat_id_short: str
    session_model: str
    session_reasoning_effort: str
    runtime_migration: str
    resume_command: str
    recommended_resume_command: str | None
    reused_existing_successor: bool
    reason_code: str
    staged_patch_sha256: str | None
    cursor_output_fingerprint_sha256: str | None = None
    legacy_cursor_output_adopted: bool = False
    legacy_cursor_usage_limit_adopted: bool = False
    requested_cursor_model: str | None = None
    source_cursor_model: str | None = None
    usage_limit_fingerprint_sha256: str | None = None
    continuation_envelope_path: str | None = None


def _shorten_id(value: str) -> str:
    if len(value) <= 12:
        return value
    return f"{value[:8]}…"


def _create_run_layout(base: Path) -> None:
    for relative in (
        "plan",
        "prompts/fixes",
        "prompts/cursor-recovery",
        "cursor/iterations",
        "codex/reviews",
        "codex/events",
        "preflight",
        "logs",
        "locks",
        "git/status",
        "git/diffs",
        "git/cursor-output",
    ):
        ensure_dir(base / relative)


def _copy_sensitive_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    temp = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, destination)
        set_sensitive_file_mode(destination)
    finally:
        temp.unlink(missing_ok=True)


def _should_copy_source_relative(
    rel: str,
    *,
    iteration: int,
    checkpoint: str,
) -> bool:
    if rel in {
        "plan/plan.md",
        "plan/metadata.json",
        "prompts/cursor-initial.txt",
        "source-config.yaml",
        "effective-config.yaml",
        "git/baseline-status.txt",
        "cursor/chat.json",
        "codex/session-runtime.json",
    }:
        return True
    if rel.startswith("prompts/fixes/"):
        # Keep fix prompts from earlier reviews only.
        name = Path(rel).name
        if name.endswith(".execution-envelope.txt"):
            try:
                prompt_iteration = int(name.removesuffix(".execution-envelope.txt"))
            except ValueError:
                return False
            return prompt_iteration < iteration
        if not name.endswith(".txt"):
            return False
        try:
            prompt_iteration = int(name.removesuffix(".txt"))
        except ValueError:
            return False
        return prompt_iteration < iteration
    if rel.startswith("prompts/cursor-recovery/"):
        # Continuation envelopes are written fresh for the successor.
        return False
    if rel.startswith("cursor/iterations/"):
        parts = Path(rel).parts
        if len(parts) < 3:
            return False
        try:
            iter_num = int(parts[2])
        except ValueError:
            return False
        return iter_num <= iteration
    if rel.startswith("git/cursor-output/"):
        name = Path(rel).name
        if name.endswith(".post-normalization.json"):
            try:
                iter_num = int(name.removesuffix(".post-normalization.json"))
            except ValueError:
                return False
            return iter_num <= iteration
        if name.endswith(".usage-limit-failure.json"):
            try:
                iter_num = int(name.removesuffix(".usage-limit-failure.json"))
            except ValueError:
                return False
            return iter_num <= iteration
        if name.endswith(".usage-limit-adopted.json"):
            return False
        if not name.endswith(".json"):
            return False
        try:
            iter_num = int(name.removesuffix(".json"))
        except ValueError:
            return False
        return iter_num <= iteration
    if rel.startswith("git/status/") or rel.startswith("git/diffs/"):
        name = Path(rel).name
        prefix = name.split(".", 1)[0].split("-", 1)[0]
        try:
            iter_num = int(prefix)
        except ValueError:
            return False
        if iter_num < iteration:
            return True
        if iter_num > iteration:
            return False
        if checkpoint == "staging":
            # Current iteration: copy Cursor status evidence; omit incomplete staging diffs.
            if rel.startswith("git/diffs/"):
                return False
            # Allow before/after-cursor and before-staging diagnostics; skip after-staging.
            return "-after-staging" not in name
        if checkpoint == "cursor":
            # Incomplete Cursor turn: keep status evidence, omit staged diffs.
            return not rel.startswith("git/diffs/")
        return True
    if rel.startswith("codex/reviews/") or rel.startswith("codex/events/"):
        name = Path(rel).name
        prefix = name.split(".", 1)[0]
        try:
            iter_num = int(prefix)
        except ValueError:
            return False
        if iter_num < iteration:
            return True
        return iter_num == iteration and checkpoint == "process_review"
    return False


def _copy_selected_artifacts(
    source_dir: Path,
    dest_dir: Path,
    *,
    iteration: int,
    checkpoint: str,
) -> list[str]:
    copied: list[str] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(source_dir))
        if not _should_copy_source_relative(rel, iteration=iteration, checkpoint=checkpoint):
            continue
        target = dest_dir / rel
        _copy_sensitive_file(path, target)
        copied.append(rel)
    return copied


def _sanitize_iterations_for_successor(
    iterations: list[dict[str, object]],
    *,
    iteration: int,
    checkpoint: str,
) -> list[dict[str, object]]:
    sanitized: list[dict[str, object]] = []
    for entry in iterations:
        number = entry.get("number")
        if not isinstance(number, int) or number > iteration:
            continue
        cloned: dict[str, object] = json.loads(json.dumps(entry))
        if number == iteration and checkpoint == "reviewing":
            cloned.pop("codex", None)
            cloned.pop("review", None)
            cloned.pop("completed_at", None)
        if number == iteration and checkpoint == "staging":
            cloned.pop("codex", None)
            cloned.pop("review", None)
            cloned.pop("completed_at", None)
            git_section = cloned.get("git")
            if isinstance(git_section, dict):
                cleaned_git = {
                    key: value
                    for key, value in git_section.items()
                    if key
                    in {
                        "status_before_cursor_path",
                        "status_after_cursor_path",
                        "cursor_output_fingerprint_path",
                        "staging_normalization_fingerprint_path",
                    }
                }
                cloned["git"] = cleaned_git
        if number == iteration and checkpoint == "cursor":
            cloned.pop("codex", None)
            cloned.pop("review", None)
            cloned.pop("completed_at", None)
        sanitized.append(cloned)
    return sanitized


def _build_session_runtime_artifact(
    *,
    session_id: str,
    resolved: ResolvedRecoveryRuntime,
) -> dict[str, object]:
    return {
        "session_id_prefix": session_id[:8],
        "model": resolved.session_model,
        "reasoning_effort": resolved.session_reasoning_effort,
        "origin": resolved.session_origin,
        "source_event_type": resolved.source_event_type,
        "source_timestamp": resolved.source_timestamp,
        "review_model": resolved.review_model,
        "review_reasoning_effort": resolved.review_reasoning_effort,
        "review_model_source": resolved.review_model_source,
        "review_reasoning_source": resolved.review_reasoning_source,
        "model_mismatch_warning": None,
        "model_family_warning": resolved.model_family_warning,
    }


def _find_matching_successors(
    *,
    project: str,
    source_run_id: str,
    iteration: int,
    checkpoint: str,
    staged_patch_sha256: str | None,
    cursor_output_fingerprint_sha256: str | None = None,
    legacy_cursor_output_adopted: bool = False,
    legacy_cursor_usage_limit_adopted: bool = False,
    usage_limit_fingerprint_sha256: str | None = None,
    source_prompt_sha256: str | None = None,
    cursor_model_fallback: str | None = None,
) -> list[tuple[Path, RunState]]:
    matches: list[tuple[Path, RunState]] = []
    for path, state in list_run_directories(project=project):
        recovery = state.recovery
        if recovery is None:
            continue
        if recovery.source_run_id != source_run_id:
            continue
        if recovery.source_iteration != iteration:
            continue
        if recovery.recovered_checkpoint != checkpoint:
            continue
        if checkpoint == "cursor":
            if recovery.usage_limit_fingerprint_sha256 != usage_limit_fingerprint_sha256:
                continue
            if recovery.source_prompt_sha256 != source_prompt_sha256:
                continue
            if recovery.cursor_model_fallback != cursor_model_fallback:
                continue
            adopted = bool(recovery.legacy_cursor_usage_limit_adopted)
            if adopted != bool(legacy_cursor_usage_limit_adopted):
                continue
            matches.append((path, state))
            continue
        if recovery.source_staged_patch_sha256 != staged_patch_sha256:
            continue
        if checkpoint == "staging":
            if recovery.cursor_output_fingerprint_sha256 != cursor_output_fingerprint_sha256:
                continue
            adopted = bool(recovery.legacy_cursor_output_adopted)
            if adopted != bool(legacy_cursor_output_adopted):
                continue
        matches.append((path, state))
    return matches


def _find_conflicting_cursor_successors(
    *,
    project: str,
    source_run_id: str,
    iteration: int,
    usage_limit_fingerprint_sha256: str,
    source_prompt_sha256: str,
    cursor_model_fallback: str,
    legacy_cursor_usage_limit_adopted: bool = False,
) -> list[tuple[Path, RunState]]:
    """Find active cursor successors for the same checkpoint with a different model."""

    conflicts: list[tuple[Path, RunState]] = []
    for path, state in list_run_directories(project=project):
        recovery = state.recovery
        if recovery is None:
            continue
        if recovery.source_run_id != source_run_id:
            continue
        if recovery.source_iteration != iteration:
            continue
        if recovery.recovered_checkpoint != "cursor":
            continue
        if recovery.usage_limit_fingerprint_sha256 != usage_limit_fingerprint_sha256:
            continue
        if recovery.source_prompt_sha256 != source_prompt_sha256:
            continue
        if recovery.cursor_model_fallback == cursor_model_fallback:
            continue
        adopted = bool(recovery.legacy_cursor_usage_limit_adopted)
        if adopted != legacy_cursor_usage_limit_adopted:
            continue
        if state.status in TERMINAL_STATUSES:
            continue
        conflicts.append((path, state))
    return conflicts


def _require_recovery_checkpoint_fields(analysis: RecoveryAnalysis) -> None:
    if analysis.checkpoint is None or analysis.iteration is None:
        joined = "; ".join(analysis.blockers) if analysis.blockers else "missing_checkpoint_fields"
        raise ValidationError(f"run is not recoverable: {joined}")
    if analysis.checkpoint == "cursor":
        if (
            not analysis.usage_limit_fingerprint_sha256
            or not analysis.source_prompt_sha256
            or not analysis.requested_cursor_model
        ):
            joined = (
                "; ".join(analysis.blockers)
                if analysis.blockers
                else "missing_cursor_checkpoint_fields"
            )
            raise ValidationError(f"run is not recoverable: {joined}")
        return
    if analysis.staged_patch_sha256 is None:
        joined = "; ".join(analysis.blockers) if analysis.blockers else "missing_checkpoint_fields"
        raise ValidationError(f"run is not recoverable: {joined}")


def _validate_successor_for_reuse(
    successor_dir: Path,
    successor: RunState,
    *,
    source: RunState,
    analysis: RecoveryAnalysis,
) -> None:
    """Ensure a matching successor preserves source identity and checkpoint artifacts."""

    recovery = successor.recovery
    if recovery is None:
        raise ValidationError("matching successor is missing recovery lineage")
    if recovery.source_run_id != source.run_id:
        raise ValidationError("matching successor source_run_id does not match the failed source")
    if analysis.checkpoint is not None and recovery.recovered_checkpoint != analysis.checkpoint:
        raise ValidationError(
            "matching successor recovered checkpoint does not match current recovery analysis"
        )
    if analysis.iteration is not None and recovery.source_iteration != analysis.iteration:
        raise ValidationError(
            "matching successor recovered iteration does not match current recovery analysis"
        )
    if (
        analysis.staged_patch_sha256 is not None
        and recovery.source_staged_patch_sha256 != analysis.staged_patch_sha256
    ):
        raise ValidationError(
            "matching successor staged-patch lineage does not match current recovery analysis"
        )
    if analysis.checkpoint == "cursor":
        if recovery.usage_limit_fingerprint_sha256 != analysis.usage_limit_fingerprint_sha256:
            raise ValidationError(
                "matching successor usage-limit fingerprint does not match current recovery analysis"
            )
        if recovery.source_prompt_sha256 != analysis.source_prompt_sha256:
            raise ValidationError(
                "matching successor source prompt hash does not match current recovery analysis"
            )
        if recovery.cursor_model_fallback != analysis.requested_cursor_model:
            raise ValidationError(
                "matching successor fallback Cursor model does not match the requested model"
            )
        if bool(recovery.legacy_cursor_usage_limit_adopted) != bool(
            analysis.legacy_cursor_usage_limit_adopted
        ):
            raise ValidationError(
                "matching successor legacy adoption state does not match current recovery analysis"
            )
        if successor.cursor.model != analysis.requested_cursor_model:
            raise ValidationError(
                "matching successor frozen Cursor model does not match the requested fallback"
            )

    if successor.codex.session_id != source.codex.session_id:
        raise ValidationError(
            "matching successor Codex session id does not match the failed source"
        )
    if successor.cursor.chat_id != source.cursor.chat_id:
        raise ValidationError("matching successor Cursor chat id does not match the failed source")

    if successor.repository.root != source.repository.root:
        raise ValidationError("matching successor repository root does not match the failed source")
    if successor.repository.git_common_dir != source.repository.git_common_dir:
        raise ValidationError("matching successor git common dir does not match the failed source")
    if successor.repository.git_dir != source.repository.git_dir:
        raise ValidationError("matching successor git dir does not match the failed source")
    if successor.repository.branch != source.repository.branch:
        raise ValidationError("matching successor branch does not match the failed source")
    if successor.repository.initial_head != source.repository.initial_head:
        raise ValidationError("matching successor initial HEAD does not match the failed source")

    if successor.plan.sha256 != source.plan.sha256:
        raise ValidationError("matching successor plan hash does not match the failed source")
    if successor.plan.repository_path != source.plan.repository_path:
        raise ValidationError("matching successor plan path does not match the failed source")
    if successor.prompt.sha256 != source.prompt.sha256:
        raise ValidationError("matching successor prompt hash does not match the failed source")
    if successor.prompt.source_repository_path != source.prompt.source_repository_path:
        raise ValidationError("matching successor prompt path does not match the failed source")

    iteration = recovery.source_iteration
    checkpoint = recovery.recovered_checkpoint
    label = iteration_label(iteration)

    if not successor.cursor.chat_id:
        raise ValidationError("matching successor is missing cursor chat id")
    chat_path = successor_dir / "cursor" / "chat.json"
    if not chat_path.is_file():
        raise ValidationError("matching successor is missing cursor chat artifact")
    try:
        chat_payload = json.loads(chat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError("matching successor cursor chat artifact is unreadable") from exc
    if (
        not isinstance(chat_payload, dict)
        or chat_payload.get("chat_id") != successor.cursor.chat_id
    ):
        raise ValidationError("matching successor cursor chat id does not match chat artifact")
    if chat_payload.get("chat_id") != source.cursor.chat_id:
        raise ValidationError(
            "matching successor chat artifact does not match the failed source Cursor chat id"
        )

    if checkpoint == "cursor":
        if cursor_turn_complete(successor_dir, iteration):
            raise ValidationError(
                f"matching cursor successor unexpectedly has a completed Cursor turn for {label}"
            )
        fingerprint_path = successor_dir / str(recovery.usage_limit_fingerprint_path)
        if not fingerprint_path.is_file():
            raise ValidationError(
                f"matching cursor successor is missing usage-limit fingerprint for {label}"
            )
        if not recovery.continuation_envelope_path:
            raise ValidationError("matching cursor successor is missing continuation envelope path")
        envelope_path = successor_dir / recovery.continuation_envelope_path
        if not envelope_path.is_file():
            raise ValidationError("matching cursor successor is missing continuation envelope")
        if not recovery.continuation_envelope_sha256:
            raise ValidationError("matching cursor successor is missing continuation envelope hash")
        if sha256_file(envelope_path) != recovery.continuation_envelope_sha256:
            raise ValidationError(
                "matching cursor successor continuation envelope no longer matches recovery lineage"
            )
        return

    if not cursor_turn_complete(successor_dir, iteration):
        raise ValidationError(
            f"matching successor is missing a completed Cursor turn for iteration {label}"
        )

    if checkpoint == "staging":
        if staging_complete_for_iteration(successor, successor_dir, label):
            raise ValidationError(
                f"matching staging successor unexpectedly has completed staging for {label}"
            )
        fingerprint_path = successor_dir / f"git/cursor-output/{label}.json"
        if not fingerprint_path.is_file():
            raise ValidationError(
                f"matching staging successor is missing cursor output fingerprint for {label}"
            )
        try:
            payload = json.loads(fingerprint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise ValidationError(
                "matching successor cursor output fingerprint is unreadable"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("aggregate_sha256") != recovery.cursor_output_fingerprint_sha256
        ):
            raise ValidationError(
                "matching successor cursor output fingerprint no longer matches recovery lineage"
            )
        return

    if not staging_complete_for_iteration(successor, successor_dir, label):
        raise ValidationError(
            f"matching successor is missing completed staging artifacts for iteration {label}"
        )

    patch_candidates = [
        successor_dir / f"git/diffs/{label}.patch",
    ]
    entry = next((item for item in successor.iterations if item.get("number") == iteration), None)
    if isinstance(entry, dict):
        git_section = entry.get("git")
        if isinstance(git_section, dict):
            staged_rel = git_section.get("staged_diff_path")
            if isinstance(staged_rel, str):
                patch_candidates.insert(0, successor_dir / staged_rel)
    patch_path = next((path for path in patch_candidates if path.is_file()), None)
    if patch_path is None:
        raise ValidationError(
            f"matching successor is missing staged patch artifact for iteration {label}"
        )
    if sha256_file(patch_path) != recovery.source_staged_patch_sha256:
        raise ValidationError(
            "matching successor staged patch hash no longer matches recovery lineage"
        )

    if checkpoint == "process_review" and not review_result_available(successor_dir, iteration):
        raise ValidationError(
            f"matching successor is missing a valid review result for iteration {label}"
        )


def _handle_existing_successors(
    analysis: RecoveryAnalysis,
    matches: list[tuple[Path, RunState]],
    *,
    source: RunState,
) -> RecoveryResult | None:
    if not matches:
        return None
    if not analysis.eligible:
        raise ValidationError(
            "run is not recoverable: "
            + ("; ".join(analysis.blockers) if analysis.blockers else "structural_checks_failed")
        )
    if len(matches) > 1:
        ids = ", ".join(state.run_id for _, state in matches)
        raise ValidationError(
            f"multiple matching recovery successors exist for this source checkpoint: {ids}"
        )
    successor_dir, successor = matches[0]
    analysis.existing_successor_run_id = successor.run_id
    analysis.existing_successor_status = successor.status.value
    if successor.status not in TERMINAL_STATUSES:
        recovery = successor.recovery
        if recovery is None:
            raise ValidationError("matching successor is missing recovery lineage")
        _validate_successor_for_reuse(
            successor_dir,
            successor,
            source=source,
            analysis=analysis,
        )
        resolved = resolved_runtime_from_successor(successor)
        analysis.reused_existing_successor = True
        analysis.resolved_runtime = resolved
        analysis.session_runtime_action = resolved.session_runtime_action
        analysis.reason_code = recovery.reason_code
        chat_id = successor.cursor.chat_id or ""
        return RecoveryResult(
            source_run_id=analysis.source_run_id,
            recovery_run_id=successor.run_id,
            checkpoint=recovery.recovered_checkpoint,
            iteration=recovery.source_iteration,
            cursor_chat_id_short=_shorten_id(chat_id),
            session_model=resolved.session_model,
            session_reasoning_effort=resolved.session_reasoning_effort,
            runtime_migration=resolved.runtime_migration,
            resume_command=f"ai_dev_loop resume {successor.run_id}",
            recommended_resume_command=None,
            reused_existing_successor=True,
            reason_code=recovery.reason_code,
            staged_patch_sha256=recovery.source_staged_patch_sha256,
            cursor_output_fingerprint_sha256=recovery.cursor_output_fingerprint_sha256,
            legacy_cursor_output_adopted=bool(recovery.legacy_cursor_output_adopted),
            legacy_cursor_usage_limit_adopted=bool(recovery.legacy_cursor_usage_limit_adopted),
            requested_cursor_model=recovery.cursor_model_fallback,
            source_cursor_model=recovery.source_cursor_model,
            usage_limit_fingerprint_sha256=recovery.usage_limit_fingerprint_sha256,
            continuation_envelope_path=recovery.continuation_envelope_path,
        )
    if successor.status == RunStatus.FAILED:
        raise ValidationError(
            f"matching recovery successor {successor.run_id} is itself failed; "
            "recover that successor explicitly to keep lineage as a clear chain"
        )
    raise ValidationError(
        f"matching recovery successor {successor.run_id} already finished with status "
        f"{successor.status.value}; refusing to create another successor for the same checkpoint"
    )


def _annotate_existing_successors_for_analysis(
    analysis: RecoveryAnalysis,
    *,
    project: str,
    source: RunState,
) -> None:
    if analysis.checkpoint is None or analysis.iteration is None:
        return
    if analysis.checkpoint == "cursor":
        if (
            not analysis.usage_limit_fingerprint_sha256
            or not analysis.source_prompt_sha256
            or not analysis.requested_cursor_model
        ):
            return
        matches = _find_matching_successors(
            project=project,
            source_run_id=analysis.source_run_id,
            iteration=analysis.iteration,
            checkpoint=analysis.checkpoint,
            staged_patch_sha256=None,
            usage_limit_fingerprint_sha256=analysis.usage_limit_fingerprint_sha256,
            source_prompt_sha256=analysis.source_prompt_sha256,
            cursor_model_fallback=analysis.requested_cursor_model,
            legacy_cursor_usage_limit_adopted=analysis.legacy_cursor_usage_limit_adopted,
        )
    else:
        if analysis.staged_patch_sha256 is None:
            return
        matches = _find_matching_successors(
            project=project,
            source_run_id=analysis.source_run_id,
            iteration=analysis.iteration,
            checkpoint=analysis.checkpoint,
            staged_patch_sha256=analysis.staged_patch_sha256,
            cursor_output_fingerprint_sha256=analysis.cursor_output_fingerprint_sha256,
            legacy_cursor_output_adopted=analysis.legacy_cursor_output_adopted,
            legacy_cursor_usage_limit_adopted=analysis.legacy_cursor_usage_limit_adopted,
        )
    if not matches:
        return
    if len(matches) > 1:
        ids = ", ".join(state.run_id for _, state in matches)
        analysis.warnings.append(f"multiple_matching_successors:{ids}")
        return
    successor_dir, successor = matches[0]
    analysis.existing_successor_run_id = successor.run_id
    analysis.existing_successor_status = successor.status.value
    # Only treat a successor as reusable when current structural checks still pass.
    if not analysis.eligible:
        return
    if successor.status not in TERMINAL_STATUSES and successor.recovery is not None:
        try:
            _validate_successor_for_reuse(
                successor_dir,
                successor,
                source=source,
                analysis=analysis,
            )
            resolved = resolved_runtime_from_successor(successor)
        except ValidationError as exc:
            analysis.warnings.append(f"existing_successor_not_reusable:{exc}")
            return
        analysis.reused_existing_successor = True
        analysis.resolved_runtime = resolved
        analysis.session_runtime_action = resolved.session_runtime_action
        analysis.reason_code = successor.recovery.reason_code


def _attach_runtime_resolution(
    analysis: RecoveryAnalysis,
    state: RunState,
    run_directory: Path,
) -> None:
    try:
        resolved = resolve_recovery_runtime(state, run_directory)
        analysis.resolved_runtime = resolved
        analysis.session_runtime_action = resolved.session_runtime_action
        if resolved.runtime_migration == "phase9_session_capture":
            warning = (
                "phase9_runtime_captured_at_recovery; "
                "values reflect current session metadata, not a prepare-time freeze"
            )
            if warning not in analysis.warnings:
                analysis.warnings.append(warning)
    except ValidationError as exc:
        message = str(exc).lower()
        if "session" in message or "rollout" in message or "ambiguous" in message:
            code = "session_runtime_unavailable"
        else:
            code = "review_runtime_unresolved"
        if code not in analysis.blockers:
            analysis.blockers.append(code)
        analysis.session_runtime_action = SessionRuntimeAction.UNRESOLVED
        analysis.resolved_runtime = None
    analysis.eligible = len(analysis.blockers) == 0


def _create_successor_run(
    *,
    source_dir: Path,
    source: RunState,
    analysis: RecoveryAnalysis,
) -> RecoveryResult:
    assert analysis.checkpoint is not None
    assert analysis.iteration is not None
    assert analysis.reason_code is not None
    assert analysis.resolved_runtime is not None
    if analysis.checkpoint != "cursor":
        assert analysis.staged_patch_sha256 is not None
    else:
        assert analysis.requested_cursor_model is not None
        assert analysis.usage_limit_fingerprint_sha256 is not None
        assert analysis.source_prompt_path is not None
        assert analysis.source_prompt_sha256 is not None
        assert analysis.usage_limit_fingerprint_path is not None

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".recovering-{recovery_run_id}-{secrets.token_hex(4)}"
    if temp_dir.exists():
        raise ValidationError(f"temporary recovery directory already exists: {temp_dir}")

    try:
        _create_run_layout(temp_dir)
        copied = _copy_selected_artifacts(
            source_dir,
            temp_dir,
            iteration=analysis.iteration,
            checkpoint=analysis.checkpoint,
        )
        resolved = analysis.resolved_runtime
        session_runtime_payload = _build_session_runtime_artifact(
            session_id=source.codex.session_id,
            resolved=resolved,
        )
        session_runtime_path = temp_dir / "codex" / "session-runtime.json"
        atomic_write_json(session_runtime_path, session_runtime_payload, sensitive=True)

        successor_codex = apply_resolved_runtime_to_codex(source.codex, resolved)
        successor_cursor = source.cursor.model_copy(deep=True)
        if analysis.checkpoint == "cursor":
            successor_cursor = successor_cursor.model_copy(
                update={"model": analysis.requested_cursor_model}
            )

        recovery_kwargs: dict[str, object] = {
            "source_run_id": source.run_id,
            "source_status": RunStatus.FAILED.value,
            "source_iteration": analysis.iteration,
            "recovered_checkpoint": analysis.checkpoint,
            "source_staged_patch_sha256": analysis.staged_patch_sha256,
            "created_at": now,
            "runtime_migration": resolved.runtime_migration,
            "reason_code": analysis.reason_code,
        }
        if analysis.checkpoint == "staging":
            recovery_kwargs["cursor_output_fingerprint_sha256"] = (
                analysis.cursor_output_fingerprint_sha256
            )
            recovery_kwargs["previous_staged_patch_sha256"] = analysis.previous_staged_patch_sha256
            if analysis.legacy_cursor_output_adopted:
                recovery_kwargs["legacy_cursor_output_adopted"] = True

        continuation_rel: str | None = None
        continuation_hash: str | None = None
        if analysis.checkpoint == "cursor":
            _source_prompt_rel, source_prompt_text = source_prompt_for_usage_limit_recovery(
                source,
                source_dir,
                analysis.iteration,
            )
            continuation_rel, continuation_hash = write_usage_limit_continuation_envelope(
                temp_dir,
                iteration_number=analysis.iteration,
                source_prompt=source_prompt_text,
            )
            copied.append(continuation_rel)
            recovery_cursor_kwargs: dict[str, object] = {
                "source_cursor_model": analysis.source_cursor_model or source.cursor.model,
                "cursor_model_fallback": analysis.requested_cursor_model,
                "source_prompt_path": analysis.source_prompt_path,
                "source_prompt_sha256": analysis.source_prompt_sha256,
                "usage_limit_fingerprint_sha256": analysis.usage_limit_fingerprint_sha256,
                "usage_limit_fingerprint_path": analysis.usage_limit_fingerprint_path,
                "continuation_envelope_path": continuation_rel,
                "continuation_envelope_sha256": continuation_hash,
            }
            if analysis.legacy_cursor_usage_limit_adopted:
                recovery_cursor_kwargs["legacy_cursor_usage_limit_adopted"] = True
            recovery_kwargs.update(recovery_cursor_kwargs)
            analysis.continuation_envelope_path = continuation_rel
            analysis.continuation_envelope_sha256 = continuation_hash

        recovery = RecoveryState(**recovery_kwargs)  # type: ignore[arg-type]
        iterations = _sanitize_iterations_for_successor(
            source.iterations,
            iteration=analysis.iteration,
            checkpoint=analysis.checkpoint,
        )
        if analysis.checkpoint == "cursor" and continuation_rel is not None:
            for entry in iterations:
                if entry.get("number") == analysis.iteration:
                    cursor_section = entry.setdefault("cursor", {})
                    if isinstance(cursor_section, dict):
                        cursor_section["continuation_envelope_path"] = continuation_rel
                        cursor_section["prompt_path"] = continuation_rel
                    break

        successor = RunState(
            run_id=recovery_run_id,
            project=source.project,
            status=RunStatus.INTERRUPTED,
            created_at=now,
            updated_at=now,
            repository=source.repository.model_copy(deep=True),
            plan=source.plan.model_copy(deep=True),
            prompt=source.prompt.model_copy(deep=True),
            codex=successor_codex,
            cursor=successor_cursor,
            workflow=source.workflow.model_copy(
                update={"current_review_iteration": max(analysis.iteration - 1, 0)}
                if analysis.checkpoint == "cursor"
                else {"current_review_iteration": analysis.iteration}
            ),
            iterations=iterations,
            result=(
                f"Recovered from failed run {source.run_id} at "
                f"{analysis.checkpoint} iteration {analysis.iteration}."
            ),
            last_error=None,
            recovery=recovery,
        )

        # Persist an adopted fingerprint into the successor when the source lacked one.
        if (
            analysis.checkpoint == "staging"
            and analysis.legacy_cursor_output_adopted
            and analysis.adopted_fingerprint_payload is not None
        ):
            fingerprint_rel = f"git/cursor-output/{iteration_label(analysis.iteration)}.json"
            atomic_write_json(
                temp_dir / fingerprint_rel,
                analysis.adopted_fingerprint_payload,
                sensitive=True,
            )
            if fingerprint_rel not in copied:
                copied.append(fingerprint_rel)
            # Record fingerprint path on the current iteration git section.
            for entry in successor.iterations:
                if entry.get("number") == analysis.iteration:
                    git_section = entry.setdefault("git", {})
                    if isinstance(git_section, dict):
                        git_section["cursor_output_fingerprint_path"] = fingerprint_rel
                    break
        if (
            analysis.checkpoint == "cursor"
            and analysis.legacy_cursor_usage_limit_adopted
            and analysis.adopted_usage_limit_payload is not None
            and analysis.requested_cursor_model is not None
        ):
            adopted_payload = analysis.adopted_usage_limit_payload
            adopted_rel = usage_limit_adopted_fingerprint_rel_path(analysis.iteration)
            content_payload = adopted_payload["content_fingerprint_payload"]
            if not isinstance(content_payload, dict):
                raise ValidationError(
                    "legacy adoption analysis is missing content fingerprint payload"
                )
            adopted_artifact = build_usage_limit_adopted_payload(
                iteration_number=analysis.iteration,
                source_run_id=str(adopted_payload["source_run_id"]),
                source_iteration=int(str(adopted_payload["source_iteration"])),
                source_metadata_path=str(adopted_payload["source_metadata_path"]),
                source_metadata_sha256=str(adopted_payload["source_metadata_sha256"]),
                source_stderr_path=str(adopted_payload["source_stderr_path"]),
                source_stderr_sha256=str(adopted_payload["source_stderr_sha256"]),
                source_after_cursor_status_path=str(
                    adopted_payload["source_after_cursor_status_path"]
                ),
                source_after_cursor_status_sha256=str(
                    adopted_payload["source_after_cursor_status_sha256"]
                ),
                source_prompt_path=str(adopted_payload["source_prompt_path"]),
                source_prompt_sha256=str(adopted_payload["source_prompt_sha256"]),
                cursor_model_fallback=analysis.requested_cursor_model,
                content_fingerprint_payload=content_payload,
                aggregate_sha256=str(adopted_payload["aggregate_sha256"]),
            )
            atomic_write_json(temp_dir / adopted_rel, adopted_artifact, sensitive=True)
            if adopted_rel not in copied:
                copied.append(adopted_rel)
            for entry in successor.iterations:
                if entry.get("number") == analysis.iteration:
                    git_section = entry.setdefault("git", {})
                    if isinstance(git_section, dict):
                        git_section["usage_limit_fingerprint_path"] = adopted_rel
                        git_section["usage_limit_fingerprint_sha256"] = str(
                            adopted_payload["aggregate_sha256"]
                        )
                    break

        manifest_artifacts: list[ManifestArtifact] = []
        for rel in copied:
            path = temp_dir / rel
            if path.is_file():
                manifest_artifacts.append(ManifestArtifact(path=rel, sha256=sha256_file(path)))
        if "codex/session-runtime.json" not in {item.path for item in manifest_artifacts}:
            manifest_artifacts.append(
                ManifestArtifact(
                    path="codex/session-runtime.json",
                    sha256=sha256_file(session_runtime_path),
                )
            )
        manifest = RunManifest(
            run_id=recovery_run_id,
            project=project,
            created_at=now,
            artifacts=manifest_artifacts,
        )
        atomic_write_json(temp_dir / "state.json", serialize_run_state(successor), sensitive=True)
        atomic_write_json(
            temp_dir / "manifest.json",
            json.loads(manifest.model_dump_json(by_alias=True)),
        )
        atomic_write_text(
            temp_dir / "logs" / "ai_dev_loop.log",
            (
                f"{now.isoformat()} recover created successor {recovery_run_id} "
                f"from source {source.run_id} "
                f"checkpoint={analysis.checkpoint} iteration={analysis.iteration} "
                f"reason={analysis.reason_code} migration={resolved.runtime_migration}\n"
            ),
            sensitive=True,
        )
        event_detail: dict[str, object] = {
            "source_run_id": source.run_id,
            "checkpoint": analysis.checkpoint,
            "reason_code": analysis.reason_code,
            "runtime_migration": resolved.runtime_migration,
        }
        if analysis.staged_patch_sha256 is not None:
            event_detail["staged_patch_sha256"] = analysis.staged_patch_sha256
        if analysis.checkpoint == "cursor":
            event_detail["cursor_model_fallback"] = analysis.requested_cursor_model
            event_detail["usage_limit_fingerprint_sha256"] = analysis.usage_limit_fingerprint_sha256
            if analysis.legacy_cursor_usage_limit_adopted:
                event_name = "cursor_usage_limit_legacy_output_adopted"
            else:
                event_name = "cursor_usage_limit_recovery_successor_created"
        else:
            event_name = "recover_created"
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event=event_name,
            level=EventLevel.INFO,
            status=RunStatus.INTERRUPTED.value,
            iteration=analysis.iteration,
            detail=event_detail,
        )

        os.rename(temp_dir, final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    chat_id = successor.cursor.chat_id or ""
    return RecoveryResult(
        source_run_id=source.run_id,
        recovery_run_id=recovery_run_id,
        checkpoint=analysis.checkpoint,
        iteration=analysis.iteration,
        cursor_chat_id_short=_shorten_id(chat_id),
        session_model=resolved.session_model,
        session_reasoning_effort=resolved.session_reasoning_effort,
        runtime_migration=resolved.runtime_migration,
        resume_command=f"ai_dev_loop resume {recovery_run_id}",
        recommended_resume_command=None,
        reused_existing_successor=False,
        reason_code=analysis.reason_code,
        staged_patch_sha256=analysis.staged_patch_sha256,
        cursor_output_fingerprint_sha256=analysis.cursor_output_fingerprint_sha256,
        legacy_cursor_output_adopted=analysis.legacy_cursor_output_adopted,
        legacy_cursor_usage_limit_adopted=analysis.legacy_cursor_usage_limit_adopted,
        requested_cursor_model=analysis.requested_cursor_model,
        source_cursor_model=analysis.source_cursor_model or source.cursor.model,
        usage_limit_fingerprint_sha256=analysis.usage_limit_fingerprint_sha256,
        continuation_envelope_path=continuation_rel,
    )


def _allows_omitting_cursor_model(
    analysis: RecoveryAnalysis,
    *,
    dry_run: bool,
    adopt_current_cursor_output: bool,
) -> bool:
    """Permit missing --cursor-model only for legacy prospective dry-run inspection.

    Normal Phase 13 cursor checkpoints always require an explicit fallback model,
    including dry-run. Historical usage-limit candidates may omit the model on
    dry-run without adoption so operators can inspect the explicit-adoption
    blocker before choosing a fallback.
    """

    if not dry_run or adopt_current_cursor_output:
        return False
    return "legacy_cursor_usage_limit_requires_explicit_adoption" in analysis.blockers


def recover_run(
    run_id: str,
    *,
    dry_run: bool = False,
    adopt_current_cursor_output: bool = False,
    cursor_model: str | None = None,
) -> RecoveryAnalysis | RecoveryResult:
    source_dir, source = load_run(run_id)
    # Structural analysis first so idempotent reuse does not depend on live rollouts.
    analysis = analyze_recovery(
        source,
        source_dir,
        resolve_runtime=False,
        adopt_current_cursor_output=adopt_current_cursor_output,
    )
    if cursor_model is not None:
        cleaned_model = cursor_model.strip()
        if not cleaned_model:
            raise ValidationError("--cursor-model must be a non-empty model identifier")
        if analysis.checkpoint != "cursor":
            raise ValidationError(
                "--cursor-model is only valid for cursor usage-limit recovery checkpoints"
            )
        analysis.requested_cursor_model = cleaned_model
    elif analysis.checkpoint == "cursor" and not _allows_omitting_cursor_model(
        analysis,
        dry_run=dry_run,
        adopt_current_cursor_output=adopt_current_cursor_output,
    ):
        raise ValidationError(
            "cursor usage-limit recovery requires --cursor-model <model> "
            "(for example --cursor-model auto)"
        )

    _annotate_existing_successors_for_analysis(
        analysis,
        project=source.project.name,
        source=source,
    )

    if dry_run:
        if (
            analysis.eligible
            and analysis.reused_existing_successor
            and analysis.resolved_runtime is not None
        ):
            return analysis
        if analysis.eligible or (
            analysis.checkpoint is not None
            and analysis.iteration is not None
            and (analysis.checkpoint == "cursor" or analysis.staged_patch_sha256 is not None)
            and not analysis.reused_existing_successor
        ):
            # Preview migration/runtime only when no reusable successor is available.
            _attach_runtime_resolution(analysis, source, source_dir)
        return analysis

    if not analysis.eligible:
        if (
            analysis.checkpoint is not None
            and analysis.iteration is not None
            and (analysis.checkpoint == "cursor" or analysis.staged_patch_sha256 is not None)
            and not analysis.reused_existing_successor
        ):
            _attach_runtime_resolution(analysis, source, source_dir)
        joined = "; ".join(analysis.blockers) if analysis.blockers else "unknown"
        raise ValidationError(f"run is not recoverable: {joined}")

    _require_recovery_checkpoint_fields(analysis)

    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=source.run_id,
        repository_path=source.repository.root,
        started_at=utc_now(),
    )
    # Lock order matches start/resume/abort: source run lock, then repository lock.
    with RunLocks(source_dir, metadata):
        source = load_run_state(source_dir / "state.json")
        analysis = analyze_recovery(
            source,
            source_dir,
            resolve_runtime=False,
            adopt_current_cursor_output=adopt_current_cursor_output,
        )
        if cursor_model is not None:
            cleaned_model = cursor_model.strip()
            if analysis.checkpoint != "cursor":
                raise ValidationError(
                    "--cursor-model is only valid for cursor usage-limit recovery checkpoints"
                )
            analysis.requested_cursor_model = cleaned_model
        elif analysis.checkpoint == "cursor":
            raise ValidationError(
                "cursor usage-limit recovery requires --cursor-model <model> "
                "(for example --cursor-model auto)"
            )
        if not analysis.eligible:
            joined = "; ".join(analysis.blockers) if analysis.blockers else "unknown"
            raise ValidationError(f"run is not recoverable under lock: {joined}")
        if analysis.checkpoint is None or analysis.iteration is None:
            joined = (
                "; ".join(analysis.blockers) if analysis.blockers else "missing_checkpoint_fields"
            )
            raise ValidationError(f"run is not recoverable under lock: {joined}")
        if analysis.checkpoint == "staging" and not analysis.cursor_output_fingerprint_sha256:
            raise ValidationError(
                "run is not recoverable under lock: missing_cursor_output_fingerprint"
            )
        if analysis.checkpoint == "cursor":
            if (
                not analysis.usage_limit_fingerprint_sha256
                or not analysis.source_prompt_sha256
                or not analysis.requested_cursor_model
            ):
                raise ValidationError(
                    "run is not recoverable under lock: missing_cursor_checkpoint_fields"
                )
            conflicts = _find_conflicting_cursor_successors(
                project=source.project.name,
                source_run_id=source.run_id,
                iteration=analysis.iteration,
                usage_limit_fingerprint_sha256=analysis.usage_limit_fingerprint_sha256,
                source_prompt_sha256=analysis.source_prompt_sha256,
                cursor_model_fallback=analysis.requested_cursor_model,
                legacy_cursor_usage_limit_adopted=analysis.legacy_cursor_usage_limit_adopted,
            )
            if conflicts:
                ids = ", ".join(state.run_id for _, state in conflicts)
                raise ValidationError(
                    "an active cursor recovery successor already exists for this source "
                    f"checkpoint with a different fallback model: {ids}; "
                    "resume or recover that successor instead of creating a parallel one"
                )
        elif analysis.staged_patch_sha256 is None:
            joined = (
                "; ".join(analysis.blockers) if analysis.blockers else "missing_checkpoint_fields"
            )
            raise ValidationError(f"run is not recoverable under lock: {joined}")

        checkpoint = analysis.checkpoint
        iteration = analysis.iteration
        staged_patch_sha256 = analysis.staged_patch_sha256

        matches = _find_matching_successors(
            project=source.project.name,
            source_run_id=source.run_id,
            iteration=iteration,
            checkpoint=checkpoint,
            staged_patch_sha256=staged_patch_sha256,
            cursor_output_fingerprint_sha256=analysis.cursor_output_fingerprint_sha256,
            legacy_cursor_output_adopted=analysis.legacy_cursor_output_adopted,
            usage_limit_fingerprint_sha256=analysis.usage_limit_fingerprint_sha256,
            source_prompt_sha256=analysis.source_prompt_sha256,
            cursor_model_fallback=analysis.requested_cursor_model,
            legacy_cursor_usage_limit_adopted=analysis.legacy_cursor_usage_limit_adopted,
        )
        reused = _handle_existing_successors(analysis, matches, source=source)
        if reused is not None:
            return reused

        _attach_runtime_resolution(analysis, source, source_dir)
        if not analysis.eligible:
            joined = "; ".join(analysis.blockers) if analysis.blockers else "unknown"
            raise ValidationError(f"run is not recoverable under lock: {joined}")
        return _create_successor_run(
            source_dir=source_dir,
            source=source,
            analysis=analysis,
        )


def _checkpoint_label(checkpoint: str) -> str:
    if checkpoint == "staging":
        return "staging"
    if checkpoint == "reviewing":
        return "Codex review"
    if checkpoint == "process_review":
        return "Codex review processing"
    if checkpoint == "cursor":
        return "Cursor usage-limit continuation"
    return checkpoint


def _recovery_mode_label(analysis: RecoveryAnalysis) -> str | None:
    if analysis.checkpoint != "cursor":
        return None
    if analysis.legacy_cursor_usage_limit_adopted:
        return "legacy_usage_limit_adoption"
    if analysis.eligible or analysis.usage_limit_fingerprint_sha256:
        return "phase13_usage_limit_recovery"
    return None


def render_recovery_analysis(analysis: RecoveryAnalysis, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "dry_run": True,
            "eligible": analysis.eligible,
            "source_run_id": analysis.source_run_id,
            "source_status": analysis.source_status,
            "checkpoint": analysis.checkpoint,
            "iteration": analysis.iteration,
            "blockers": list(analysis.blockers),
            "warnings": list(analysis.warnings),
            "staged_patch_sha256": analysis.staged_patch_sha256,
            "cursor_output_fingerprint_sha256": analysis.cursor_output_fingerprint_sha256,
            "previous_staged_patch_sha256": analysis.previous_staged_patch_sha256,
            "post_cursor_fingerprint_available": analysis.post_cursor_fingerprint_available,
            "legacy_cursor_output_adopted": analysis.legacy_cursor_output_adopted,
            "legacy_cursor_usage_limit_adopted": analysis.legacy_cursor_usage_limit_adopted,
            "verified_staging_state": analysis.verified_staging_state,
            "session_runtime_action": analysis.session_runtime_action.value,
            "reason_code": analysis.reason_code,
            "existing_successor_run_id": analysis.existing_successor_run_id,
            "existing_successor_status": analysis.existing_successor_status,
            "reused_existing_successor": analysis.reused_existing_successor,
            "requested_cursor_model": analysis.requested_cursor_model,
            "source_cursor_model": analysis.source_cursor_model,
            "usage_limit_fingerprint_sha256": analysis.usage_limit_fingerprint_sha256,
            "source_prompt_sha256": analysis.source_prompt_sha256,
            "continuation_envelope_path": analysis.continuation_envelope_path,
            "recovery_mode": _recovery_mode_label(analysis),
            "runtime": None
            if analysis.resolved_runtime is None
            else {
                "session_model": analysis.resolved_runtime.session_model,
                "session_reasoning_effort": analysis.resolved_runtime.session_reasoning_effort,
                "review_model": analysis.resolved_runtime.review_model,
                "review_reasoning_effort": analysis.resolved_runtime.review_reasoning_effort,
                "review_model_source": analysis.resolved_runtime.review_model_source,
                "review_reasoning_source": analysis.resolved_runtime.review_reasoning_source,
                "runtime_migration": analysis.resolved_runtime.runtime_migration,
            },
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        f"Source run: {analysis.source_run_id}",
        f"Source status: {analysis.source_status}",
        f"Eligible: {'yes' if analysis.eligible else 'no'}",
    ]
    if analysis.checkpoint is not None:
        lines.append(
            f"Recovered checkpoint: {_checkpoint_label(analysis.checkpoint)}, "
            f"iteration {analysis.iteration}"
        )
    if analysis.reason_code:
        lines.append(f"Reason code: {analysis.reason_code}")
    if analysis.session_runtime_action != SessionRuntimeAction.UNRESOLVED:
        lines.append(f"Session runtime action: {analysis.session_runtime_action.value}")
    if analysis.resolved_runtime is not None:
        lines.append(
            "Session runtime: "
            f"{analysis.resolved_runtime.session_model} / "
            f"{analysis.resolved_runtime.session_reasoning_effort}"
        )
        lines.append(f"Runtime migration: {analysis.resolved_runtime.runtime_migration}")
    if analysis.checkpoint == "cursor":
        mode = _recovery_mode_label(analysis)
        if mode:
            lines.append(f"Recovery mode: {mode}")
        if analysis.source_cursor_model:
            lines.append(f"Source Cursor model: {analysis.source_cursor_model}")
        if analysis.requested_cursor_model:
            lines.append(f"Requested fallback Cursor model: {analysis.requested_cursor_model}")
        if analysis.legacy_cursor_usage_limit_adopted:
            lines.append("Current repository status: matches recorded after-Cursor status")
            lines.append("Current content fingerprint: captured at recovery time")
            lines.append(
                "Historical content fingerprint: unavailable; explicit user adoption recorded"
            )
        elif analysis.usage_limit_fingerprint_sha256 and analysis.eligible:
            lines.append("Usage-limit fingerprint: verified at failure time")
        if analysis.source_prompt_sha256:
            lines.append("Source prompt: verified")
        lines.append("Repository identity: checked")
        lines.append("Git mutation performed by recover: none")
        lines.append("Note: dry-run does not claim the fallback model is currently available.")
    elif analysis.checkpoint == "staging":
        if analysis.legacy_cursor_output_adopted:
            lines.append(
                "Cursor output: explicitly adopted from matching historical after-cursor status"
            )
        elif analysis.verified_staging_state == "post_normalization":
            lines.append("Cursor output: post-normalization fingerprint verified")
        elif analysis.post_cursor_fingerprint_available:
            lines.append("Cursor output fingerprint: verified")
        else:
            lines.append("Cursor output fingerprint: missing (explicit adoption required)")
        if analysis.previous_staged_patch_sha256:
            lines.append("Previous staged patch: verified")
        lines.append("Repository identity: checked")
        lines.append("Git mutation performed by recover: none")
    elif analysis.staged_patch_sha256:
        lines.append("Repository and staged patch: verified")
    if analysis.existing_successor_run_id:
        lines.append(f"Existing successor: {analysis.existing_successor_run_id}")
        if analysis.existing_successor_status:
            lines.append(f"Existing successor status: {analysis.existing_successor_status}")
        if analysis.reused_existing_successor:
            lines.append("Would reuse existing matching successor: yes")
    if analysis.blockers:
        lines.append("Blockers:")
        for blocker in analysis.blockers:
            lines.append(f"  - {blocker}")
    if analysis.warnings:
        lines.append("Warnings:")
        for warning in analysis.warnings:
            lines.append(f"  - {warning}")
    if analysis.eligible:
        lines.append("Dry run only; no successor was created.")
    return "\n".join(lines) + "\n"


def render_recovery_result(result: RecoveryResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "source_run_id": result.source_run_id,
            "recovery_run_id": result.recovery_run_id,
            "checkpoint": result.checkpoint,
            "iteration": result.iteration,
            "cursor_chat_id_short": result.cursor_chat_id_short,
            "session_model": result.session_model,
            "session_reasoning_effort": result.session_reasoning_effort,
            "runtime_migration": result.runtime_migration,
            "resume_command": result.resume_command,
            "recommended_resume_command": result.recommended_resume_command,
            "reused_existing_successor": result.reused_existing_successor,
            "reason_code": result.reason_code,
            "staged_patch_sha256": result.staged_patch_sha256,
            "cursor_output_fingerprint_sha256": result.cursor_output_fingerprint_sha256,
            "legacy_cursor_output_adopted": result.legacy_cursor_output_adopted,
            "legacy_cursor_usage_limit_adopted": result.legacy_cursor_usage_limit_adopted,
            "requested_cursor_model": result.requested_cursor_model,
            "source_cursor_model": result.source_cursor_model,
            "usage_limit_fingerprint_sha256": result.usage_limit_fingerprint_sha256,
            "continuation_envelope_path": result.continuation_envelope_path,
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        f"Source run: {result.source_run_id}",
        f"Recovery run: {result.recovery_run_id}",
        (
            f"Recovered checkpoint: {_checkpoint_label(result.checkpoint)}, "
            f"iteration {result.iteration}"
        ),
        f"Cursor chat: {result.cursor_chat_id_short}",
        f"Session runtime: {result.session_model} / {result.session_reasoning_effort}",
        f"Runtime migration: {result.runtime_migration}",
    ]
    if result.checkpoint == "cursor":
        if result.legacy_cursor_usage_limit_adopted:
            lines.append("Recovery mode: legacy_usage_limit_adoption")
            lines.append("Current repository status: matches recorded after-Cursor status")
            lines.append("Current content fingerprint: captured at recovery time")
            lines.append(
                "Historical content fingerprint: unavailable; explicit user adoption recorded"
            )
        else:
            lines.append("Recovery mode: phase13_usage_limit_recovery")
            lines.append("Usage-limit fingerprint: verified at failure time")
        if result.source_cursor_model:
            lines.append(f"Source Cursor model: {result.source_cursor_model}")
        if result.requested_cursor_model:
            lines.append(f"Fallback Cursor model: {result.requested_cursor_model}")
        if result.continuation_envelope_path:
            lines.append(f"Continuation envelope: {result.continuation_envelope_path}")
        lines.append("Repository identity: verified")
        lines.append("Git mutation performed by recover: none")
    elif result.checkpoint == "staging":
        if result.legacy_cursor_output_adopted:
            lines.append(
                "Cursor output: explicitly adopted from matching historical after-cursor status"
            )
        else:
            lines.append("Cursor output fingerprint: verified")
        lines.append("Repository identity: verified")
        lines.append("Git mutation performed by recover: none")
    else:
        lines.append("Repository and staged patch: verified")
    lines.append(f"Next command: {result.resume_command}")
    if result.reused_existing_successor:
        lines.insert(2, "Reused existing matching successor: yes")
    if result.recommended_resume_command:
        lines.append(f"Optional: {result.recommended_resume_command}")
    lines.append(
        "Note: recover never updates tools or invokes agents; "
        "pass --update-tools to resume when needed."
    )
    return "\n".join(lines) + "\n"
