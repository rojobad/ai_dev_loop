"""Recover failed GitHub PR-review runs without re-posting a trigger.

Supports artifact-driven checkpoints:

- ``external_adjudication`` (Phase 15.7): schema-incompatible adjudication before
  Cursor/GitHub side effects.
- ``reviewing`` (Phase 15.8): Cursor correction and staging completed, but the
  local Codex structured result artifact was never persisted.
- ``external_feedback_cursor`` (Phase 15.12 / 15.16): external adjudication
  completed with an actionable fix prompt, but the fresh Cursor iteration never
  started. An empty ``cursor/iterations/NN`` directory alone is not partial
  Cursor evidence; durable iteration entries or any files under that path are.
- ``publication_pre_commit`` (Phase 15.13): local Cursor/Codex accepted the
  staged fix and publication reached ``pre_commit``, but commit never happened
  (historically a terminal ValidationError from ssh-agent identity preflight).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_dev_loop.commands.pr_review import (
    WORKER_LAUNCHER_REL,
    _copy_identity_artifacts,
    _create_successor_layout,
    _load_publication_text,
    _run_locks,
)
from ai_dev_loop.commands.recover import (
    _copy_selected_artifacts,
    _copy_sensitive_file,
    _sanitize_iterations_for_successor,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.iterations import (
    derive_external_cursor_iteration_for_recovery,
    iteration_label,
    max_iteration_number,
    next_external_cursor_iteration,
)
from ai_dev_loop.paths import ensure_dir, run_dir, runs_dir, set_sensitive_file_mode
from ai_dev_loop.recovery_planner import (
    analyze_recovery,
    apply_resolved_runtime_to_codex,
)
from ai_dev_loop.response_schema import events_indicate_adjudication_schema_rejection
from ai_dev_loop.resume_planner import (
    TERMINAL_STATUSES,
    cursor_turn_complete,
    review_result_available,
)
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.codex import (
    classify_codex_review_output_artifact_failure,
    load_review_result_from_artifacts,
)
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.github import (
    filter_eligible_threads,
    get_pull_request,
    list_review_threads,
)
from ai_dev_loop.runners.publish import (
    publication_staged_patch_fingerprint,
    validate_clean_worktree,
)
from ai_dev_loop.runners.staging import staging_complete_for_iteration
from ai_dev_loop.state import (
    RecoveryState,
    RunState,
    RunStatus,
    atomic_write_json,
    generate_run_id,
    save_run_state,
    sha256_file,
    utc_now,
)


@dataclass
class PrReviewRecoveryAnalysis:
    source_run_id: str
    eligible: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checkpoint: str | None = None
    reason_code: str | None = None
    cycle_number: int | None = None
    iteration: int | None = None
    staged_patch_sha256: str | None = None
    source_prompt_sha256: str | None = None
    pr_number: int | None = None
    bound_head_sha_prefix: str | None = None
    expected_eligible_thread_ids: list[str] = field(default_factory=list)
    expected_thread_count: int = 0
    existing_successor_run_id: str | None = None
    existing_successor_status: str | None = None
    reused_existing_successor: bool = False
    has_controller: bool = False
    # Validated controller A UUID used only to render an executable resume command.
    # Never copied into public events or pr-review/status JSON fields.
    controller_session_id: str | None = None
    trigger_present: bool = False
    historical_schema_rejection: bool = False
    # Analysis-only: obsolete GPR hash replaced by proven live raw fingerprint.
    # Not persisted; successor lineage records the adopted live hash.
    historical_patch_fingerprint_adopted: bool = False


@dataclass(frozen=True)
class PrReviewRecoveryResult:
    source_run_id: str
    recovery_run_id: str | None
    dry_run: bool
    analysis: PrReviewRecoveryAnalysis
    message: str
    resume_command: str | None


def _active_worker_pid(run_directory: Path) -> int | None:
    launcher = run_directory / WORKER_LAUNCHER_REL
    if not launcher.is_file():
        return None
    try:
        payload = json.loads(launcher.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _cycle_events_path(run_directory: Path, cycle_number: int) -> Path:
    return run_directory / f"github/cycles/{cycle_number:02d}/codex.events.jsonl"


def _has_local_loop_progress(state: RunState, run_directory: Path) -> bool:
    if max_iteration_number(state) >= 1:
        return True
    return (run_directory / "cursor" / "iterations" / "01").exists()


def _has_adjudication_side_effects(gpr: Any, state: RunState) -> list[str]:
    """Block external-adjudication recovery when side effects already occurred."""

    blockers: list[str] = []
    if gpr.processed_thread_ids:
        blockers.append("processed_threads_present")
    if gpr.replied_thread_ids:
        blockers.append("replied_threads_present")
    if gpr.resolved_thread_ids:
        blockers.append("resolved_threads_present")
    if gpr.last_external_result_path:
        blockers.append("external_result_present")
    if gpr.external_fix_prompt_path:
        blockers.append("external_fix_prompt_present")
    if gpr.continue_comment_id:
        blockers.append("continue_comment_present")
    if state.iterations:
        blockers.append("cursor_iterations_present")
    if gpr.origin == "independent_pr" and state.cursor.chat_id:
        blockers.append("cursor_chat_already_created")
    if gpr.lifecycle in {
        "fixing_external_feedback",
        "publishing_external_fix",
        "waiting_for_user_attention",
        "completed",
        "max_external_cycles_reached",
    }:
        blockers.append(f"lifecycle_past_adjudication:{gpr.lifecycle}")
    return blockers


def _reviewing_github_side_effect_blockers(gpr: Any) -> list[str]:
    """Block reviewing recovery when GitHub publication/reply side effects started."""

    blockers: list[str] = []
    if gpr.processed_thread_ids:
        blockers.append("processed_threads_present")
    if gpr.replied_thread_ids:
        blockers.append("replied_threads_present")
    if gpr.resolved_thread_ids:
        blockers.append("resolved_threads_present")
    if gpr.continue_comment_id:
        blockers.append("continue_comment_present")
    if gpr.publication_phase is not None:
        blockers.append("publication_started")
    if gpr.lifecycle in {
        "publishing_external_fix",
        "publishing_initial",
        "waiting_for_user_attention",
        "completed",
        "max_external_cycles_reached",
    }:
        blockers.append(f"lifecycle_past_local_review:{gpr.lifecycle}")
    return blockers


def _classify_schema_incompatible_source(
    state: RunState,
    run_directory: Path,
) -> tuple[bool, bool]:
    """Return (eligible_classification, historical_artifact_match)."""

    gpr = state.github_pr_review
    assert gpr is not None
    if gpr.worker_outcome == "adjudication_schema_incompatible":
        return True, False
    events = _cycle_events_path(run_directory, gpr.cycle_number)
    if events_indicate_adjudication_schema_rejection(events):
        return True, True
    return False, False


def _fill_common_pr_fields(analysis: PrReviewRecoveryAnalysis, state: RunState) -> None:
    gpr = state.github_pr_review
    assert gpr is not None
    analysis.cycle_number = gpr.cycle_number
    analysis.pr_number = gpr.pr_number
    analysis.bound_head_sha_prefix = gpr.bound_head_sha[:12]
    analysis.has_controller = state.controller is not None
    analysis.trigger_present = bool(gpr.request_comment_id and gpr.request_marker)
    expected = list(gpr.expected_eligible_thread_ids or gpr.eligible_thread_ids)
    if expected:
        analysis.expected_eligible_thread_ids = expected
        analysis.expected_thread_count = len(expected)

    if state.controller is not None:
        try:
            analysis.controller_session_id = require_codex_session_id(
                state.controller.controller_session_id
            )
        except ValidationError:
            analysis.blockers.append("controller_session_invalid")
            analysis.controller_session_id = None
        else:
            if analysis.controller_session_id == state.codex.session_id:
                analysis.blockers.append("controller_equals_reviewer")


def _verify_remote_pr(
    analysis: PrReviewRecoveryAnalysis,
    state: RunState,
    *,
    verify_remote: bool,
    require_thread_set: bool = False,
) -> None:
    gpr = state.github_pr_review
    assert gpr is not None
    if not verify_remote or gpr.pr_number is None:
        return
    if "not_a_pr_review_cycle" in analysis.blockers:
        return
    try:
        from ai_dev_loop.commands.pr_review import _require_github_config

        config = _require_github_config(Path(state.repository.root))
        assert config.github is not None
        pr = get_pull_request(
            config.github.command,
            cwd=state.repository.root,
            pr_number=gpr.pr_number,
        )
        if pr.state != "OPEN":
            analysis.blockers.append("pr_not_open")
        if pr.head_sha != gpr.bound_head_sha:
            analysis.blockers.append("head_sha_drift")
        if require_thread_set and analysis.expected_eligible_thread_ids:
            threads = list_review_threads(
                config.github.command,
                cwd=state.repository.root,
                pr_number=gpr.pr_number,
            )
            eligible = filter_eligible_threads(
                threads,
                reviewer_logins=config.github.reviewer_logins,
                bound_head_sha=gpr.bound_head_sha,
                already_processed_thread_ids=set(gpr.processed_thread_ids),
                request_created_at=gpr.request_created_at,
            )
            observed = [thread.thread_id for thread in eligible]
            if set(observed) != set(analysis.expected_eligible_thread_ids):
                analysis.blockers.append("eligible_thread_set_drift")
    except Exception as exc:
        analysis.blockers.append(f"remote_pr_unreadable:{type(exc).__name__}")


def _analyze_external_adjudication(
    analysis: PrReviewRecoveryAnalysis,
    state: RunState,
    run_directory: Path,
    *,
    verify_remote: bool,
) -> PrReviewRecoveryAnalysis:
    gpr = state.github_pr_review
    assert gpr is not None
    analysis.checkpoint = "external_adjudication"
    analysis.reason_code = "github_adjudication_schema_incompatible"

    if state.status != RunStatus.FAILED:
        analysis.blockers.append("source_not_failed")
    classified, historical = _classify_schema_incompatible_source(state, run_directory)
    analysis.historical_schema_rejection = historical
    if not classified:
        analysis.blockers.append("not_adjudication_schema_incompatible")

    if gpr.pr_number is None:
        analysis.blockers.append("pr_not_bound")
    if not analysis.trigger_present:
        analysis.blockers.append("trigger_missing")
    if not analysis.expected_eligible_thread_ids:
        analysis.blockers.append("eligible_threads_missing")

    analysis.blockers.extend(_has_adjudication_side_effects(gpr, state))

    if _active_worker_pid(run_directory) is not None:
        analysis.blockers.append("active_worker_present")

    _verify_remote_pr(analysis, state, verify_remote=verify_remote)
    analysis.eligible = len(analysis.blockers) == 0
    _attach_matching_successor(analysis, state)
    return analysis


def _current_cycle_thread_ids(gpr: Any) -> set[str]:
    expected = list(gpr.expected_eligible_thread_ids or gpr.eligible_thread_ids or [])
    return set(expected)


def _external_feedback_cursor_side_effect_blockers(gpr: Any) -> list[str]:
    """Block when publication or current-cycle thread side effects already began.

    A non-null ``continue_comment_id`` is not a blocker: nested Phase 15.10/15.11
    lineage may have already consumed an authorized continue while clearing a
    prior-cycle freeze before the current external adjudication.
    """

    blockers: list[str] = []
    current = _current_cycle_thread_ids(gpr)
    if gpr.publication_phase is not None:
        blockers.append("publication_started")
    if current and set(gpr.processed_thread_ids) & current:
        blockers.append("current_threads_processed")
    if current and set(gpr.replied_thread_ids) & current:
        blockers.append("current_threads_replied")
    if current and set(gpr.resolved_thread_ids) & current:
        blockers.append("current_threads_resolved")
    if gpr.lifecycle in {
        "publishing_external_fix",
        "publishing_initial",
        "waiting_for_user_attention",
        "completed",
        "max_external_cycles_reached",
        "awaiting_bot_review",
        "evaluating_bot_feedback",
    }:
        blockers.append(f"lifecycle_past_external_cursor:{gpr.lifecycle}")
    return blockers


def _cursor_iteration_dir_has_execution_evidence(cursor_dir: Path) -> bool:
    """True when the iteration directory contains any file (partial Cursor evidence).

    An empty directory created before preflight is compatibility noise only and
    must not classify the source as a partial Cursor attempt or as reviewing.
    """

    if not cursor_dir.exists():
        return False
    if cursor_dir.is_file():
        return True
    if not cursor_dir.is_dir():
        return True
    return any(path.is_file() for path in cursor_dir.rglob("*"))


def _iteration_prefixed_name(name: str, label: str, *, status_style: bool) -> bool:
    """True when ``name`` is a durable artifact for iteration ``label``."""

    if status_style:
        return name.startswith(f"{label}-")
    return name == label or name.startswith(f"{label}.")


def _has_iteration_prefixed_durable_artifact(run_directory: Path, label: str) -> bool:
    """Conservatively detect any iteration-prefixed durable artifact on disk.

    Scans known per-iteration roots rather than a short hand-maintained filename
    list so staging/review artifacts (before/after staging status, ``.stat``,
    ``.name-only.txt``, post-normalization fingerprints, review metadata, etc.)
    cannot be omitted by accident.
    """

    roots: tuple[tuple[str, bool], ...] = (
        ("git/status", True),
        ("git/cursor-output", False),
        ("git/diffs", False),
        ("codex/reviews", False),
        ("codex/events", False),
    )
    for relative_root, status_style in roots:
        directory = run_directory / relative_root
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and _iteration_prefixed_name(
                path.name, label, status_style=status_style
            ):
                return True
    return False


def _has_partial_external_cursor_attempt(
    run_directory: Path,
    pending_iteration: int,
    *,
    state: RunState | None = None,
) -> bool:
    """True when durable evidence shows the pending external Cursor iteration started.

    An empty ``cursor/iterations/NN`` directory alone is compatibility noise only
    when there is no durable ``iterations[NN]`` entry and no iteration-prefixed
    artifact under the known staging/review roots.
    """

    label = iteration_label(pending_iteration)
    if state is not None:
        for entry in state.iterations:
            raw_number = entry.get("number")
            try:
                number = int(raw_number)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if number == pending_iteration:
                return True
    cursor_dir = run_directory / "cursor" / "iterations" / label
    if _cursor_iteration_dir_has_execution_evidence(cursor_dir):
        return True
    return _has_iteration_prefixed_durable_artifact(run_directory, label)


def _looks_like_external_feedback_cursor_source(
    state: RunState,
    run_directory: Path,
) -> bool:
    """Structured pre-filter: actionable external feedback, Cursor not started.

    Does not use ``last_error`` or result text as eligibility evidence.
    Requires a fresh iteration number strictly greater than any persisted
    iteration so completed local Cursor/staging progress routes to reviewing.
    """

    gpr = state.github_pr_review
    if gpr is None:
        return False
    if state.status != RunStatus.FAILED:
        return False
    if gpr.lifecycle != "fixing_external_feedback":
        return False
    if not gpr.external_fix_prompt_path or not gpr.last_external_result_path:
        return False
    if not state.cursor.chat_id:
        return False
    pending = derive_external_cursor_iteration_for_recovery(state)
    if pending is None:
        return False
    historical_max = max_iteration_number(state)
    if pending <= historical_max:
        return False
    if _has_partial_external_cursor_attempt(run_directory, pending, state=state):
        return False
    # Historical sources without a typed external_cursor_iteration may still need
    # reviewing recovery when the latest local Cursor+staging completed but the
    # Codex result is missing. Typed fresh external turns must classify as
    # external_feedback_cursor first and must not be forced into reviewing by a
    # published historical patch that simply lacks a local review artifact copy.
    if historical_max >= 1 and gpr.external_cursor_iteration is None:
        label = iteration_label(historical_max)
        if (
            cursor_turn_complete(run_directory, historical_max)
            and staging_complete_for_iteration(state, run_directory, label)
            and not review_result_available(run_directory, historical_max)
        ):
            return False
    return True


def _load_actionable_external_result(
    run_directory: Path,
    result_rel: str,
) -> GithubPrReviewResult | None:
    result_path = run_directory / result_rel
    if not result_path.is_file():
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        review = GithubPrReviewResult.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not review.all_actionable:
        return None
    if not review.cursor_fix_prompt or not review.cursor_fix_prompt.strip():
        return None
    return review


def _analyze_external_feedback_cursor(
    analysis: PrReviewRecoveryAnalysis,
    state: RunState,
    run_directory: Path,
    *,
    verify_remote: bool,
) -> PrReviewRecoveryAnalysis:
    gpr = state.github_pr_review
    assert gpr is not None
    analysis.checkpoint = "external_feedback_cursor"
    analysis.reason_code = "external_feedback_cursor_not_started"

    if state.status != RunStatus.FAILED:
        analysis.blockers.append("source_not_failed")
    if gpr.lifecycle != "fixing_external_feedback":
        analysis.blockers.append(f"lifecycle_not_fixing_external_feedback:{gpr.lifecycle}")
    if not state.cursor.chat_id:
        analysis.blockers.append("cursor_chat_missing")
    if not state.codex.session_id:
        analysis.blockers.append("codex_session_missing")

    if gpr.pr_number is None:
        analysis.blockers.append("pr_not_bound")
    if not analysis.trigger_present:
        analysis.blockers.append("trigger_missing")
    if not analysis.expected_eligible_thread_ids:
        analysis.blockers.append("eligible_threads_missing")

    prompt_rel = gpr.external_fix_prompt_path
    result_rel = gpr.last_external_result_path
    if not prompt_rel:
        analysis.blockers.append("external_fix_prompt_missing")
    if not result_rel:
        analysis.blockers.append("external_result_missing")

    review = None
    if result_rel:
        review = _load_actionable_external_result(run_directory, result_rel)
        if review is None:
            analysis.blockers.append("external_result_invalid_or_not_actionable")
        elif set(review.eligible_thread_ids) != set(analysis.expected_eligible_thread_ids):
            analysis.blockers.append("external_result_thread_set_mismatch")

    if prompt_rel:
        prompt_path = run_directory / prompt_rel
        if not prompt_path.is_file():
            analysis.blockers.append("external_fix_prompt_missing_file")
        else:
            try:
                text = prompt_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                analysis.blockers.append("external_fix_prompt_unreadable")
            else:
                if not text.strip():
                    analysis.blockers.append("external_fix_prompt_empty")
                else:
                    analysis.source_prompt_sha256 = sha256_file(prompt_path)
                    if review is not None and review.cursor_fix_prompt != text:
                        analysis.blockers.append("external_fix_prompt_body_mismatch")

    pending = derive_external_cursor_iteration_for_recovery(state)
    if pending is None:
        analysis.blockers.append("external_cursor_iteration_unresolved")
    else:
        analysis.iteration = pending
        if pending <= max_iteration_number(state):
            analysis.blockers.append("external_cursor_iteration_not_fresh")
        if _has_partial_external_cursor_attempt(run_directory, pending, state=state):
            analysis.blockers.append("partial_external_cursor_attempt")

    analysis.blockers.extend(_external_feedback_cursor_side_effect_blockers(gpr))

    if _active_worker_pid(run_directory) is not None:
        analysis.blockers.append("active_worker_present")

    try:
        repo_info = discover_repository(Path(state.repository.root))
    except Exception as exc:
        analysis.blockers.append(f"baseline_unreadable:{type(exc).__name__}")
    else:
        if state.repository.initial_head != gpr.bound_head_sha:
            analysis.blockers.append("initial_head_drift")
        if repo_info.head != gpr.bound_head_sha:
            analysis.blockers.append("head_advanced_past_bound")
        if repo_info.branch != gpr.head_branch:
            analysis.blockers.append("branch_drift")
        try:
            validate_clean_worktree(Path(state.repository.root))
        except ValidationError:
            analysis.blockers.append("baseline_not_clean")
        except Exception as exc:
            analysis.blockers.append(f"baseline_unreadable:{type(exc).__name__}")

    _verify_remote_pr(
        analysis,
        state,
        verify_remote=verify_remote,
        require_thread_set=True,
    )

    analysis.eligible = (
        len(analysis.blockers) == 0
        and analysis.iteration is not None
        and analysis.source_prompt_sha256 is not None
        and bool(analysis.expected_eligible_thread_ids)
    )
    _attach_matching_successor(analysis, state)
    return analysis


def _publication_commit_fields_are_carried_forward(gpr: Any) -> bool:
    """True when recorded commit SHAs are absent or equal the bound head.

    Historical PR-review cycles may carry ``local_commit_sha`` /
    ``publication_commit_sha`` forward from the previously published bound HEAD
    while ``publication_phase`` remains ``pre_commit``. That is not evidence of a
    new commit from this publication attempt. A SHA that differs from
    ``bound_head_sha`` means a post-bound commit was recorded and is ineligible.
    """

    bound = gpr.bound_head_sha
    for value in (gpr.local_commit_sha, gpr.publication_commit_sha):
        if value is not None and value != bound:
            return False
    return True


def _publication_pre_commit_side_effect_blockers(gpr: Any) -> list[str]:
    """Block when commit/push/trigger or current-cycle thread writes already began."""

    blockers: list[str] = []
    if gpr.publication_phase != "pre_commit":
        blockers.append(f"publication_phase_not_pre_commit:{gpr.publication_phase}")
    if not _publication_commit_fields_are_carried_forward(gpr):
        if gpr.local_commit_sha is not None and gpr.local_commit_sha != gpr.bound_head_sha:
            blockers.append("local_commit_diverged_from_bound")
        if (
            gpr.publication_commit_sha is not None
            and gpr.publication_commit_sha != gpr.bound_head_sha
        ):
            blockers.append("publication_commit_diverged_from_bound")
    current = _current_cycle_thread_ids(gpr)
    if current and set(gpr.replied_thread_ids) & current:
        blockers.append("current_threads_replied")
    if current and set(gpr.resolved_thread_ids) & current:
        blockers.append("current_threads_resolved")
    return blockers


def _load_accepted_local_review(
    run_directory: Path,
    iteration_number: int,
) -> CodexReviewResult | None:
    if not review_result_available(run_directory, iteration_number):
        return None
    try:
        review = load_review_result_from_artifacts(run_directory, iteration_label(iteration_number))
    except ValidationError:
        return None
    if review.has_actionable_findings:
        return None
    return review


def _looks_like_publication_pre_commit_source(
    state: RunState,
    run_directory: Path,
) -> bool:
    """Structured pre-filter for failed publication stopped at ``pre_commit``.

    Eligibility is checkpoint- and artifact-driven. Never uses ``last_error``,
    logs, or agent/Git stderr text.
    """

    gpr = state.github_pr_review
    if gpr is None:
        return False
    if state.status != RunStatus.FAILED:
        return False
    if gpr.lifecycle != "failed":
        return False
    if gpr.publication_phase != "pre_commit":
        return False
    if gpr.pr_number is None:
        return False
    # Allow null commit fields or carried-forward SHAs equal to bound_head_sha.
    # Reject only when a recorded SHA proves a post-bound commit.
    if not _publication_commit_fields_are_carried_forward(gpr):
        return False
    if not state.cursor.chat_id or not state.codex.session_id:
        return False
    iteration = max_iteration_number(state)
    if iteration < 1:
        return False
    label = iteration_label(iteration)
    if not (
        cursor_turn_complete(run_directory, iteration)
        and staging_complete_for_iteration(state, run_directory, label)
    ):
        return False
    if _load_accepted_local_review(run_directory, iteration) is None:
        return False
    text_rel = (
        gpr.publication_text_path or f"github/cycles/{gpr.cycle_number:02d}/publication-text.json"
    )
    return (run_directory / text_rel).is_file()


def _analyze_publication_pre_commit(
    analysis: PrReviewRecoveryAnalysis,
    state: RunState,
    run_directory: Path,
    *,
    verify_remote: bool,
) -> PrReviewRecoveryAnalysis:
    gpr = state.github_pr_review
    assert gpr is not None
    analysis.checkpoint = "publication_pre_commit"
    analysis.reason_code = "publication_pre_commit_interrupted"

    if state.status != RunStatus.FAILED:
        analysis.blockers.append("source_not_failed")
    if gpr.lifecycle != "failed":
        analysis.blockers.append(f"lifecycle_not_failed:{gpr.lifecycle}")
    if not state.cursor.chat_id:
        analysis.blockers.append("cursor_chat_missing")
    if not state.codex.session_id:
        analysis.blockers.append("codex_session_missing")
    if gpr.pr_number is None:
        analysis.blockers.append("pr_not_bound")
    if not analysis.trigger_present:
        analysis.blockers.append("trigger_missing")
    if not analysis.expected_eligible_thread_ids:
        analysis.blockers.append("eligible_threads_missing")

    analysis.blockers.extend(_publication_pre_commit_side_effect_blockers(gpr))

    iteration = max_iteration_number(state)
    if iteration < 1:
        analysis.blockers.append("local_iteration_missing")
    else:
        analysis.iteration = iteration
        label = iteration_label(iteration)
        if not cursor_turn_complete(run_directory, iteration):
            analysis.blockers.append("cursor_incomplete")
        if not staging_complete_for_iteration(state, run_directory, label):
            analysis.blockers.append("staging_incomplete")
        review = _load_accepted_local_review(run_directory, iteration)
        if review is None:
            if review_result_available(run_directory, iteration):
                analysis.blockers.append("local_review_has_actionable_findings")
            else:
                analysis.blockers.append("local_review_missing_or_invalid")

        patch_artifact = run_directory / f"git/diffs/{label}.patch"
        repo_root = Path(state.repository.root)
        if not patch_artifact.is_file():
            analysis.blockers.append("staged_patch_artifact_missing")
        else:
            try:
                live_hash = publication_staged_patch_fingerprint(repo_root, patch_artifact)
            except ValidationError as exc:
                message = str(exc).lower()
                if "no longer matches" in message or "staged index" in message:
                    analysis.blockers.append("staged_patch_artifact_drift")
                elif (
                    "non-empty staged" in message
                    or "unstaged" in message
                    or "untracked" in message
                    or "empty" in message
                ):
                    analysis.blockers.append("staged_baseline_invalid")
                else:
                    analysis.blockers.append("staged_baseline_invalid")
            except Exception as exc:
                analysis.blockers.append(f"staged_baseline_unreadable:{type(exc).__name__}")
            else:
                durable_hash = gpr.staged_patch_sha256
                # Publication fingerprint is always the raw live patch hash after
                # normalized artifact equivalence. An obsolete GPR hash alone is
                # not drift when the artifact still matches (e.g. trailing newline).
                analysis.staged_patch_sha256 = live_hash
                if durable_hash is not None and durable_hash != live_hash:
                    analysis.historical_patch_fingerprint_adopted = True
                    analysis.warnings.append("historical_publication_patch_fingerprint_adopted")

    text_rel = (
        gpr.publication_text_path or f"github/cycles/{gpr.cycle_number:02d}/publication-text.json"
    )
    text_path = run_directory / text_rel
    if not text_path.is_file():
        analysis.blockers.append("publication_text_missing")
    else:
        try:
            text = _load_publication_text(run_directory, text_rel)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            analysis.blockers.append("publication_text_invalid")
        except ValidationError:
            analysis.blockers.append("publication_text_invalid")
        else:
            if not text.commit_subject.strip() or not text.pr_title.strip():
                analysis.blockers.append("publication_text_incomplete")

    try:
        repo_info = discover_repository(Path(state.repository.root))
    except Exception as exc:
        analysis.blockers.append(f"repository_unreadable:{type(exc).__name__}")
    else:
        if repo_info.head != gpr.bound_head_sha:
            analysis.blockers.append("head_advanced_past_bound")
        if state.repository.initial_head != gpr.bound_head_sha:
            analysis.blockers.append("initial_head_drift")
        if repo_info.branch != gpr.head_branch:
            analysis.blockers.append("branch_drift")

    if _active_worker_pid(run_directory) is not None:
        analysis.blockers.append("active_worker_present")

    _verify_remote_pr(
        analysis,
        state,
        verify_remote=verify_remote,
        require_thread_set=True,
    )

    analysis.eligible = (
        len(analysis.blockers) == 0
        and analysis.iteration is not None
        and analysis.staged_patch_sha256 is not None
        and bool(analysis.expected_eligible_thread_ids)
    )
    _attach_matching_successor(analysis, state)
    return analysis


def _analyze_reviewing(
    analysis: PrReviewRecoveryAnalysis,
    state: RunState,
    run_directory: Path,
    *,
    verify_remote: bool,
) -> PrReviewRecoveryAnalysis:
    gpr = state.github_pr_review
    assert gpr is not None
    analysis.checkpoint = "reviewing"
    analysis.reason_code = "codex_review_result_artifact_missing"

    if state.status != RunStatus.FAILED:
        analysis.blockers.append("source_not_failed")

    local = analyze_recovery(state, run_directory, resolve_runtime=True)
    for blocker in local.blockers:
        if blocker not in analysis.blockers:
            analysis.blockers.append(blocker)
    for warning in local.warnings:
        if warning not in analysis.warnings:
            analysis.warnings.append(warning)

    analysis.iteration = local.iteration
    analysis.staged_patch_sha256 = local.staged_patch_sha256

    if local.checkpoint == "process_review" or (
        local.iteration is not None and review_result_available(run_directory, local.iteration)
    ):
        analysis.blockers.append("review_result_already_present")
    elif local.checkpoint == "reviewing":
        if local.reason_code == "codex_review_result_invalid":
            analysis.blockers.append("review_result_invalid")
        elif local.reason_code != "codex_review_result_artifact_missing":
            analysis.blockers.append("not_codex_output_artifact_failure")
    elif local.checkpoint == "staging":
        analysis.blockers.append("staging_incomplete")
    elif local.checkpoint == "cursor":
        analysis.blockers.append("cursor_incomplete")
    elif local.checkpoint is not None:
        analysis.blockers.append(f"unsupported_local_checkpoint:{local.checkpoint}")
    else:
        analysis.blockers.append("local_checkpoint_unresolved")

    iteration_number = local.iteration
    if iteration_number is not None:
        label = iteration_label(iteration_number)
        result_path = run_directory / f"codex/reviews/{label}.json"
        if result_path.is_file() and "review_result_already_present" not in analysis.blockers:
            if local.reason_code == "codex_review_result_invalid":
                if "review_result_invalid" not in analysis.blockers:
                    analysis.blockers.append("review_result_invalid")
            elif review_result_available(run_directory, iteration_number):
                analysis.blockers.append("review_result_already_present")
        elif (
            not classify_codex_review_output_artifact_failure(run_directory, label)
            and "not_codex_output_artifact_failure" not in analysis.blockers
        ):
            analysis.blockers.append("not_codex_output_artifact_failure")

    if gpr.pr_number is None:
        analysis.blockers.append("pr_not_bound")
    if not analysis.trigger_present:
        analysis.blockers.append("trigger_missing")
    if not analysis.expected_eligible_thread_ids:
        analysis.blockers.append("eligible_threads_missing")
    if not gpr.last_external_result_path:
        analysis.blockers.append("external_result_missing")
    if not gpr.external_fix_prompt_path:
        analysis.blockers.append("external_fix_prompt_missing")

    analysis.blockers.extend(_reviewing_github_side_effect_blockers(gpr))

    if _active_worker_pid(run_directory) is not None:
        analysis.blockers.append("active_worker_present")

    _verify_remote_pr(
        analysis,
        state,
        verify_remote=verify_remote,
        require_thread_set=True,
    )

    # Eligible only for the durable Codex output-artifact-missing checkpoint.
    analysis.eligible = (
        len(analysis.blockers) == 0
        and local.checkpoint == "reviewing"
        and local.reason_code == "codex_review_result_artifact_missing"
        and analysis.iteration is not None
        and analysis.staged_patch_sha256 is not None
    )
    _attach_matching_successor(analysis, state)
    return analysis


def analyze_pr_review_recovery(
    run_id: str,
    *,
    verify_remote: bool = True,
) -> PrReviewRecoveryAnalysis:
    run_directory, state = load_run(run_id)
    analysis = PrReviewRecoveryAnalysis(source_run_id=state.run_id)
    gpr = state.github_pr_review
    if gpr is None:
        analysis.blockers.append("not_a_pr_review_cycle")
        return analysis

    _fill_common_pr_fields(analysis, state)

    if _looks_like_publication_pre_commit_source(state, run_directory):
        return _analyze_publication_pre_commit(
            analysis,
            state,
            run_directory,
            verify_remote=verify_remote,
        )
    if _looks_like_external_feedback_cursor_source(state, run_directory):
        return _analyze_external_feedback_cursor(
            analysis,
            state,
            run_directory,
            verify_remote=verify_remote,
        )
    if _has_local_loop_progress(state, run_directory):
        return _analyze_reviewing(
            analysis,
            state,
            run_directory,
            verify_remote=verify_remote,
        )
    return _analyze_external_adjudication(
        analysis,
        state,
        run_directory,
        verify_remote=verify_remote,
    )


def _attach_matching_successor(analysis: PrReviewRecoveryAnalysis, source: RunState) -> None:
    matches: list[tuple[Path, RunState]] = []
    expected = set(analysis.expected_eligible_thread_ids)
    for path, candidate in list_run_directories():
        recovery = candidate.recovery
        if recovery is None:
            continue
        if recovery.source_run_id != source.run_id:
            continue
        if recovery.recovered_checkpoint != analysis.checkpoint:
            continue
        if recovery.reason_code != analysis.reason_code:
            continue
        if candidate.github_pr_review is None:
            continue
        if candidate.github_pr_review.cycle_number != analysis.cycle_number:
            continue
        if candidate.github_pr_review.pr_number != analysis.pr_number:
            continue
        if candidate.github_pr_review.bound_head_sha[:12] != analysis.bound_head_sha_prefix:
            continue
        if analysis.checkpoint == "external_adjudication":
            if set(recovery.expected_eligible_thread_ids or []) != expected:
                continue
        elif analysis.checkpoint == "reviewing":
            if recovery.source_iteration != analysis.iteration:
                continue
            if recovery.source_staged_patch_sha256 != analysis.staged_patch_sha256:
                continue
            if set(candidate.github_pr_review.expected_eligible_thread_ids or []) != expected:
                continue
        elif analysis.checkpoint == "external_feedback_cursor":
            if recovery.source_iteration != analysis.iteration:
                continue
            if recovery.source_prompt_sha256 != analysis.source_prompt_sha256:
                continue
            if set(recovery.expected_eligible_thread_ids or []) != expected:
                continue
            if set(candidate.github_pr_review.expected_eligible_thread_ids or []) != expected:
                continue
            if (
                candidate.github_pr_review.external_cursor_iteration is not None
                and candidate.github_pr_review.external_cursor_iteration != analysis.iteration
            ):
                continue
        elif analysis.checkpoint == "publication_pre_commit":
            if recovery.source_iteration != analysis.iteration:
                continue
            if recovery.source_staged_patch_sha256 != analysis.staged_patch_sha256:
                continue
            if set(recovery.expected_eligible_thread_ids or []) != expected:
                continue
            if set(candidate.github_pr_review.expected_eligible_thread_ids or []) != expected:
                continue
            if candidate.github_pr_review.publication_phase != "pre_commit":
                continue
        else:
            continue
        matches.append((path, candidate))
    if not matches:
        return
    if len(matches) > 1:
        ids = ", ".join(item.run_id for _, item in matches)
        analysis.warnings.append(f"multiple_matching_successors:{ids}")
        return
    _path, successor = matches[0]
    analysis.existing_successor_run_id = successor.run_id
    analysis.existing_successor_status = successor.status.value
    if not analysis.eligible:
        return
    if successor.status not in TERMINAL_STATUSES:
        analysis.reused_existing_successor = True


def _copy_pr_review_external_context(
    source_dir: Path,
    dest_dir: Path,
    *,
    cycle_number: int,
    gpr: Any,
) -> list[str]:
    """Copy only state-referenced external artifacts required for continuation."""

    del cycle_number  # cycle paths come from gpr state fields when present
    copied: list[str] = []
    allowlisted: list[str] = []
    if gpr.last_external_result_path:
        allowlisted.append(gpr.last_external_result_path)
    if gpr.last_snapshot_path:
        allowlisted.append(gpr.last_snapshot_path)
    if gpr.external_fix_prompt_path:
        allowlisted.append(gpr.external_fix_prompt_path)
    if gpr.publication_text_path:
        allowlisted.append(gpr.publication_text_path)

    for rel in allowlisted:
        src = source_dir / rel
        if not src.is_file():
            continue
        _copy_sensitive_file(src, dest_dir / rel)
        copied.append(rel)
    return copied


def _create_publication_pre_commit_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    assert analysis.cycle_number is not None
    assert analysis.iteration is not None
    assert analysis.staged_patch_sha256 is not None
    assert analysis.expected_eligible_thread_ids
    gpr = source.github_pr_review
    assert gpr is not None

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".pr-review-recover-{recovery_run_id}-{secrets.token_hex(4)}"
    try:
        _create_successor_layout(temp_dir)
        ensure_dir(temp_dir / "codex" / "reviews")
        ensure_dir(temp_dir / "codex" / "events")
        ensure_dir(temp_dir / "git" / "diffs")
        ensure_dir(temp_dir / "git" / "status")
        ensure_dir(temp_dir / "git" / "cursor-output")
        ensure_dir(temp_dir / "cursor" / "iterations")
        ensure_dir(temp_dir / "github" / "cycles" / f"{analysis.cycle_number:02d}")

        _copy_identity_artifacts(source_dir, temp_dir)
        copied = _copy_selected_artifacts(
            source_dir,
            temp_dir,
            iteration=analysis.iteration,
            checkpoint="process_review",
        )
        copied.extend(
            _copy_pr_review_external_context(
                source_dir,
                temp_dir,
                cycle_number=analysis.cycle_number,
                gpr=gpr,
            )
        )
        # Ensure publication text is present even when only the default path exists.
        text_rel = (
            gpr.publication_text_path
            or f"github/cycles/{analysis.cycle_number:02d}/publication-text.json"
        )
        if text_rel not in copied and (source_dir / text_rel).is_file():
            _copy_sensitive_file(source_dir / text_rel, temp_dir / text_rel)
            copied.append(text_rel)

        expected = list(analysis.expected_eligible_thread_ids)
        recovery = RecoveryState(
            source_run_id=source.run_id,
            source_status=RunStatus.FAILED.value,
            source_iteration=analysis.iteration,
            recovered_checkpoint="publication_pre_commit",
            source_staged_patch_sha256=analysis.staged_patch_sha256,
            created_at=now,
            runtime_migration="none",
            reason_code="publication_pre_commit_interrupted",
            expected_eligible_thread_ids=expected,
        )
        iterations = _sanitize_iterations_for_successor(
            source.iterations,
            iteration=analysis.iteration,
            checkpoint="process_review",
        )
        successor = source.model_copy(deep=True)
        successor.run_id = recovery_run_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.INTERRUPTED
        successor.result = (
            f"Recovered publication pre_commit checkpoint from failed run {source.run_id} "
            f"at iteration {analysis.iteration:02d}. Resume publishes only; no Cursor, "
            "Codex adjudication, replies, resolves, or @codex review."
        )
        successor.last_error = None
        successor.iterations = iterations
        successor.recovery = recovery
        successor.workflow = successor.workflow.model_copy(
            update={"current_review_iteration": analysis.iteration}
        )
        successor.github_pr_review = gpr.model_copy(
            update={
                "lifecycle": "publishing_external_fix",
                "worker_outcome": None,
                "eligible_thread_ids": expected,
                "expected_eligible_thread_ids": expected,
                "publication_phase": "pre_commit",
                "publication_text_path": text_rel,
                "staged_patch_sha256": analysis.staged_patch_sha256,
                "local_commit_sha": None,
                "publication_commit_sha": None,
            }
        )

        assert successor.codex.session_id == source.codex.session_id
        assert successor.cursor.chat_id == source.cursor.chat_id
        if source.controller is not None:
            assert successor.controller is not None
            assert (
                successor.controller.controller_session_id
                == source.controller.controller_session_id
            )

        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": recovery_run_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [
                    {"path": rel, "sha256": sha256_file(temp_dir / rel)}
                    for rel in copied
                    if (temp_dir / rel).is_file()
                ],
                "recovery": {
                    "source_run_id": source.run_id,
                    "recovered_checkpoint": "publication_pre_commit",
                    "reason_code": "publication_pre_commit_interrupted",
                    "source_iteration": analysis.iteration,
                    "source_staged_patch_sha256": analysis.staged_patch_sha256,
                    "expected_thread_count": len(expected),
                    "pr_number": gpr.pr_number,
                    "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                    "trigger_reposted": False,
                },
            },
            sensitive=True,
        )
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event="pr_review_publication_pre_commit_recovery_successor_created",
            status=successor.status.value,
            detail={
                "source_run_id": source.run_id,
                "checkpoint": "publication_pre_commit",
                "reason_code": "publication_pre_commit_interrupted",
                "source_iteration": analysis.iteration,
                "staged_patch_sha256": analysis.staged_patch_sha256,
                "expected_thread_count": len(expected),
                "pr_number": gpr.pr_number,
                "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                "trigger_reposted": False,
            },
        )
        if final_dir.exists():
            raise ValidationError(f"recovery run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return recovery_run_id


def _create_external_adjudication_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    assert analysis.cycle_number is not None
    assert analysis.expected_eligible_thread_ids
    gpr = source.github_pr_review
    assert gpr is not None

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".pr-review-recover-{recovery_run_id}-{secrets.token_hex(4)}"
    try:
        _create_successor_layout(temp_dir)
        _copy_identity_artifacts(source_dir, temp_dir)
        # Audit-only copy of the failed adjudication events (no result body).
        events_src = _cycle_events_path(source_dir, analysis.cycle_number)
        if events_src.is_file():
            events_dest = _cycle_events_path(temp_dir, analysis.cycle_number)
            events_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(events_src, events_dest)
            set_sensitive_file_mode(events_dest)

        expected = list(analysis.expected_eligible_thread_ids)
        recovery = RecoveryState(
            source_run_id=source.run_id,
            source_status=RunStatus.FAILED.value,
            source_iteration=analysis.cycle_number,
            recovered_checkpoint="external_adjudication",
            source_staged_patch_sha256=None,
            created_at=now,
            runtime_migration="none",
            reason_code="github_adjudication_schema_incompatible",
            expected_eligible_thread_ids=expected,
        )
        successor = source.model_copy(deep=True)
        successor.run_id = recovery_run_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.INTERRUPTED
        successor.result = (
            f"Recovered GitHub adjudication from failed run {source.run_id} "
            "without re-posting @codex review."
        )
        successor.last_error = None
        successor.iterations = []
        successor.recovery = recovery
        successor.github_pr_review = gpr.model_copy(
            update={
                "lifecycle": "interrupted",
                "worker_outcome": None,
                "eligible_thread_ids": expected,
                "expected_eligible_thread_ids": expected,
                "processed_thread_ids": [],
                "replied_thread_ids": [],
                "resolved_thread_ids": [],
                "last_external_result_path": None,
                "external_fix_prompt_path": None,
                "continue_comment_id": None,
            }
        )
        assert successor.codex.session_id == source.codex.session_id
        if source.controller is not None:
            assert successor.controller is not None
            assert (
                successor.controller.controller_session_id
                == source.controller.controller_session_id
            )
        if gpr.origin == "source_run":
            assert successor.cursor.chat_id == source.cursor.chat_id
        else:
            successor.cursor = successor.cursor.model_copy(update={"chat_id": None})

        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": recovery_run_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [],
                "recovery": {
                    "source_run_id": source.run_id,
                    "recovered_checkpoint": "external_adjudication",
                    "reason_code": "github_adjudication_schema_incompatible",
                    "expected_thread_count": len(expected),
                },
            },
            sensitive=True,
        )
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event="pr_review_adjudication_recovery_successor_created",
            status=successor.status.value,
            detail={
                "source_run_id": source.run_id,
                "checkpoint": "external_adjudication",
                "reason_code": "github_adjudication_schema_incompatible",
                "expected_thread_count": len(expected),
                "pr_number": gpr.pr_number,
                "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                "trigger_reposted": False,
            },
        )
        if final_dir.exists():
            raise ValidationError(f"recovery run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return recovery_run_id


def _create_reviewing_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    assert analysis.cycle_number is not None
    assert analysis.iteration is not None
    assert analysis.staged_patch_sha256 is not None
    assert analysis.expected_eligible_thread_ids
    gpr = source.github_pr_review
    assert gpr is not None

    local = analyze_recovery(source, source_dir, resolve_runtime=True)
    if local.resolved_runtime is None:
        raise ValidationError("reviewing recovery requires resolved Codex runtime")
    if local.checkpoint != "reviewing":
        raise ValidationError("reviewing recovery revalidation lost the reviewing checkpoint")

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".pr-review-recover-{recovery_run_id}-{secrets.token_hex(4)}"
    try:
        _create_successor_layout(temp_dir)
        ensure_dir(temp_dir / "codex" / "reviews")
        ensure_dir(temp_dir / "codex" / "events")
        ensure_dir(temp_dir / "git" / "diffs")
        ensure_dir(temp_dir / "git" / "status")
        ensure_dir(temp_dir / "git" / "cursor-output")
        ensure_dir(temp_dir / "cursor" / "iterations")

        copied = _copy_selected_artifacts(
            source_dir,
            temp_dir,
            iteration=analysis.iteration,
            checkpoint="reviewing",
        )
        copied.extend(
            _copy_pr_review_external_context(
                source_dir,
                temp_dir,
                cycle_number=analysis.cycle_number,
                gpr=gpr,
            )
        )

        resolved = local.resolved_runtime
        successor_codex = apply_resolved_runtime_to_codex(source.codex, resolved)
        recovery = RecoveryState(
            source_run_id=source.run_id,
            source_status=RunStatus.FAILED.value,
            source_iteration=analysis.iteration,
            recovered_checkpoint="reviewing",
            source_staged_patch_sha256=analysis.staged_patch_sha256,
            created_at=now,
            runtime_migration=resolved.runtime_migration,
            reason_code="codex_review_result_artifact_missing",
        )
        iterations = _sanitize_iterations_for_successor(
            source.iterations,
            iteration=analysis.iteration,
            checkpoint="reviewing",
        )
        successor = source.model_copy(deep=True)
        successor.run_id = recovery_run_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.INTERRUPTED
        successor.result = (
            f"Recovered local Codex review from failed run {source.run_id} "
            f"at reviewing iteration {analysis.iteration:02d} without re-running Cursor "
            "or re-posting @codex review."
        )
        successor.last_error = None
        successor.codex = successor_codex
        successor.iterations = iterations
        successor.recovery = recovery
        successor.workflow = successor.workflow.model_copy(
            update={"current_review_iteration": analysis.iteration}
        )
        successor.github_pr_review = gpr.model_copy(
            update={
                "lifecycle": "fixing_external_feedback",
                "worker_outcome": None,
            }
        )

        assert successor.codex.session_id == source.codex.session_id
        assert successor.cursor.chat_id == source.cursor.chat_id
        if source.controller is not None:
            assert successor.controller is not None
            assert (
                successor.controller.controller_session_id
                == source.controller.controller_session_id
            )

        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": recovery_run_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [
                    {"path": rel, "sha256": sha256_file(temp_dir / rel)}
                    for rel in copied
                    if (temp_dir / rel).is_file()
                ],
                "recovery": {
                    "source_run_id": source.run_id,
                    "recovered_checkpoint": "reviewing",
                    "reason_code": "codex_review_result_artifact_missing",
                    "source_iteration": analysis.iteration,
                    "source_staged_patch_sha256": analysis.staged_patch_sha256,
                    "pr_number": gpr.pr_number,
                    "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                },
            },
            sensitive=True,
        )
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event="pr_review_reviewing_recovery_successor_created",
            status=successor.status.value,
            detail={
                "source_run_id": source.run_id,
                "checkpoint": "reviewing",
                "reason_code": "codex_review_result_artifact_missing",
                "source_iteration": analysis.iteration,
                "staged_patch_sha256": analysis.staged_patch_sha256,
                "pr_number": gpr.pr_number,
                "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                "trigger_reposted": False,
            },
        )
        if final_dir.exists():
            raise ValidationError(f"recovery run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return recovery_run_id


def _create_external_feedback_cursor_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    assert analysis.cycle_number is not None
    assert analysis.iteration is not None
    assert analysis.source_prompt_sha256 is not None
    assert analysis.expected_eligible_thread_ids
    gpr = source.github_pr_review
    assert gpr is not None
    assert gpr.external_fix_prompt_path is not None

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".pr-review-recover-{recovery_run_id}-{secrets.token_hex(4)}"
    try:
        _create_successor_layout(temp_dir)
        ensure_dir(temp_dir / "codex" / "reviews")
        ensure_dir(temp_dir / "codex" / "events")
        ensure_dir(temp_dir / "git" / "diffs")
        ensure_dir(temp_dir / "git" / "status")
        ensure_dir(temp_dir / "git" / "cursor-output")
        ensure_dir(temp_dir / "cursor" / "iterations")

        historical_max = max_iteration_number(source)
        _copy_identity_artifacts(source_dir, temp_dir)
        copied: list[str] = []
        if historical_max >= 1:
            copied.extend(
                _copy_selected_artifacts(
                    source_dir,
                    temp_dir,
                    iteration=historical_max,
                    checkpoint="process_review",
                )
            )
            iterations = _sanitize_iterations_for_successor(
                source.iterations,
                iteration=historical_max,
                checkpoint="process_review",
            )
        else:
            iterations = []

        copied.extend(
            _copy_pr_review_external_context(
                source_dir,
                temp_dir,
                cycle_number=analysis.cycle_number,
                gpr=gpr,
            )
        )

        expected = list(analysis.expected_eligible_thread_ids)
        pending_iteration = analysis.iteration
        # Guard against colliding with a derived number that somehow drifted.
        if pending_iteration <= historical_max:
            pending_iteration = next_external_cursor_iteration(source)
        recovery = RecoveryState(
            source_run_id=source.run_id,
            source_status=RunStatus.FAILED.value,
            source_iteration=pending_iteration,
            recovered_checkpoint="external_feedback_cursor",
            source_staged_patch_sha256=None,
            created_at=now,
            runtime_migration="none",
            reason_code="external_feedback_cursor_not_started",
            expected_eligible_thread_ids=expected,
            source_prompt_path=gpr.external_fix_prompt_path,
            source_prompt_sha256=analysis.source_prompt_sha256,
        )
        successor = source.model_copy(deep=True)
        successor.run_id = recovery_run_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.INTERRUPTED
        successor.result = (
            f"Recovered external feedback Cursor turn from failed run {source.run_id} "
            f"at iteration {pending_iteration:02d} without re-posting @codex review "
            "or re-adjudicating the same threads."
        )
        successor.last_error = None
        successor.iterations = iterations
        successor.recovery = recovery
        successor.workflow = successor.workflow.model_copy(
            update={
                "current_review_iteration": historical_max,
                # Fresh local review budget for the post-external Cursor segment.
                "local_review_count": 0,
            }
        )
        successor.github_pr_review = gpr.model_copy(
            update={
                "lifecycle": "fixing_external_feedback",
                "worker_outcome": None,
                "eligible_thread_ids": expected,
                "expected_eligible_thread_ids": expected,
                "external_cursor_iteration": pending_iteration,
                "external_fix_prompt_path": gpr.external_fix_prompt_path,
                "last_external_result_path": gpr.last_external_result_path,
                "last_snapshot_path": gpr.last_snapshot_path,
                "publication_phase": None,
                # Preserve an already-consumed continue comment from prior-cycle
                # freeze clearance; it is not a current-cycle side effect.
            }
        )

        assert successor.codex.session_id == source.codex.session_id
        assert successor.cursor.chat_id == source.cursor.chat_id
        if source.controller is not None:
            assert successor.controller is not None
            assert (
                successor.controller.controller_session_id
                == source.controller.controller_session_id
            )

        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": recovery_run_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [
                    {"path": rel, "sha256": sha256_file(temp_dir / rel)}
                    for rel in copied
                    if (temp_dir / rel).is_file()
                ],
                "recovery": {
                    "source_run_id": source.run_id,
                    "recovered_checkpoint": "external_feedback_cursor",
                    "reason_code": "external_feedback_cursor_not_started",
                    "source_iteration": pending_iteration,
                    "source_prompt_sha256": analysis.source_prompt_sha256,
                    "expected_thread_count": len(expected),
                    "pr_number": gpr.pr_number,
                    "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                    "trigger_reposted": False,
                },
            },
            sensitive=True,
        )
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event="pr_review_external_feedback_cursor_recovery_successor_created",
            status=successor.status.value,
            detail={
                "source_run_id": source.run_id,
                "checkpoint": "external_feedback_cursor",
                "reason_code": "external_feedback_cursor_not_started",
                "source_iteration": pending_iteration,
                "expected_thread_count": len(expected),
                "pr_number": gpr.pr_number,
                "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                "trigger_reposted": False,
            },
        )
        if final_dir.exists():
            raise ValidationError(f"recovery run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return recovery_run_id


def _create_recovery_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    if analysis.checkpoint == "publication_pre_commit":
        return _create_publication_pre_commit_successor(
            source_dir=source_dir,
            source=source,
            analysis=analysis,
        )
    if analysis.checkpoint == "external_feedback_cursor":
        return _create_external_feedback_cursor_successor(
            source_dir=source_dir,
            source=source,
            analysis=analysis,
        )
    if analysis.checkpoint == "reviewing":
        return _create_reviewing_successor(
            source_dir=source_dir,
            source=source,
            analysis=analysis,
        )
    if analysis.checkpoint == "external_adjudication":
        return _create_external_adjudication_successor(
            source_dir=source_dir,
            source=source,
            analysis=analysis,
        )
    raise ValidationError(f"unsupported pr-review recovery checkpoint: {analysis.checkpoint}")


def recover_pr_review_cycle(run_id: str, *, dry_run: bool = False) -> PrReviewRecoveryResult:
    """Create or reuse an immutable PR-review recovery successor for a failed cycle."""

    run_directory, source = load_run(run_id)
    analysis = analyze_pr_review_recovery(run_id, verify_remote=True)

    if dry_run:
        successor_token = analysis.existing_successor_run_id or "<successor-run-id>"
        resume_hint = (
            _resume_command_for(analysis, successor_run_id=successor_token)
            if analysis.eligible
            else None
        )
        if analysis.eligible:
            if analysis.checkpoint == "reviewing":
                message = (
                    "Dry-run only: no successor created, no GitHub writes, Cursor will not rerun, "
                    "and trigger will not be re-posted."
                )
            elif analysis.checkpoint == "external_feedback_cursor":
                message = (
                    "Dry-run only: no successor created, no GitHub writes, "
                    "Cursor will open on a fresh iteration after resume, "
                    "and trigger will not be re-posted."
                )
            elif analysis.checkpoint == "publication_pre_commit":
                message = (
                    "Dry-run only: no successor created, no GitHub writes, "
                    "resume will publish only (no Cursor/Codex/adjudication), "
                    "and trigger will not be re-posted before a successful publication."
                )
            else:
                message = (
                    "Dry-run only: no successor created, no GitHub writes, "
                    "trigger will not be re-posted."
                )
        else:
            message = f"Dry-run: run is not recoverable ({', '.join(analysis.blockers)})."
        return PrReviewRecoveryResult(
            source_run_id=source.run_id,
            recovery_run_id=None,
            dry_run=True,
            analysis=analysis,
            message=message,
            resume_command=resume_hint,
        )

    if not analysis.eligible:
        raise ValidationError(
            "pr-review recover rejected: " + ", ".join(analysis.blockers or ["not_eligible"])
        )

    if analysis.reused_existing_successor and analysis.existing_successor_run_id:
        resume_command = _resume_command_for(
            analysis, successor_run_id=analysis.existing_successor_run_id
        )
        label = _checkpoint_label(analysis.checkpoint)
        return PrReviewRecoveryResult(
            source_run_id=source.run_id,
            recovery_run_id=analysis.existing_successor_run_id,
            dry_run=False,
            analysis=analysis,
            message=(
                f"Reused existing {label} recovery successor "
                f"{analysis.existing_successor_run_id}; trigger will not be re-posted."
            ),
            resume_command=resume_command,
        )

    if (
        analysis.existing_successor_run_id
        and analysis.existing_successor_status == RunStatus.FAILED.value
    ):
        raise ValidationError(
            f"matching recovery successor {analysis.existing_successor_run_id} is itself failed; "
            "recover that successor explicitly to keep lineage as a clear chain"
        )

    locks = _run_locks(
        run_directory,
        run_id=source.run_id,
        repository_path=source.repository.root,
    )
    locks.acquire()
    try:
        # Re-validate under lock; source must remain failed/immutable.
        fresh = analyze_pr_review_recovery(run_id, verify_remote=True)
        if not fresh.eligible:
            raise ValidationError(
                "pr-review recover rejected after revalidation: " + ", ".join(fresh.blockers)
            )
        reused_id = fresh.existing_successor_run_id
        if fresh.reused_existing_successor and reused_id is not None:
            analysis = fresh
            resume_command = _resume_command_for(analysis, successor_run_id=reused_id)
            label = _checkpoint_label(analysis.checkpoint)
            return PrReviewRecoveryResult(
                source_run_id=source.run_id,
                recovery_run_id=reused_id,
                dry_run=False,
                analysis=analysis,
                message=(
                    f"Reused existing {label} recovery successor "
                    f"{reused_id}; trigger will not be re-posted."
                ),
                resume_command=resume_command,
            )
        if (
            fresh.existing_successor_run_id
            and fresh.existing_successor_status == RunStatus.FAILED.value
        ):
            raise ValidationError(
                f"matching recovery successor {fresh.existing_successor_run_id} is itself failed; "
                "recover that successor explicitly to keep lineage as a clear chain"
            )
        recovery_run_id = _create_recovery_successor(
            source_dir=run_directory,
            source=source,
            analysis=fresh,
        )
        analysis = fresh
    finally:
        locks.release()

    # Source stays failed; touch only to prove immutability was preserved.
    source_after = load_run(run_id)[1]
    if source_after.status != RunStatus.FAILED:
        raise ValidationError("source run status changed unexpectedly during recovery")

    resume_command = _resume_command_for(analysis, successor_run_id=recovery_run_id)
    if analysis.checkpoint == "reviewing":
        message = (
            f"Created reviewing recovery successor {recovery_run_id} from "
            f"{source.run_id}. Source remains failed. Cursor will not rerun and "
            "trigger will not be re-posted."
        )
    elif analysis.checkpoint == "external_feedback_cursor":
        message = (
            f"Created external_feedback_cursor recovery successor {recovery_run_id} from "
            f"{source.run_id}. Source remains failed. Resume opens Cursor on a fresh "
            "iteration; trigger will not be re-posted and threads will not be re-adjudicated."
        )
    elif analysis.checkpoint == "publication_pre_commit":
        message = (
            f"Created publication_pre_commit recovery successor {recovery_run_id} from "
            f"{source.run_id}. Source remains failed. Resume publishes only; no Cursor, "
            "Codex adjudication, replies, resolves, or @codex review before publication."
        )
    else:
        message = (
            f"Created adjudication recovery successor {recovery_run_id} from "
            f"{source.run_id}. Source remains failed. Trigger will not be re-posted."
        )
    return PrReviewRecoveryResult(
        source_run_id=source.run_id,
        recovery_run_id=recovery_run_id,
        dry_run=False,
        analysis=analysis,
        message=message,
        resume_command=resume_command,
    )


def _checkpoint_label(checkpoint: str | None) -> str:
    if checkpoint == "reviewing":
        return "reviewing"
    if checkpoint == "external_feedback_cursor":
        return "external_feedback_cursor"
    if checkpoint == "publication_pre_commit":
        return "publication_pre_commit"
    return "adjudication"


def _resume_command_for(analysis: PrReviewRecoveryAnalysis, *, successor_run_id: str) -> str:
    if analysis.has_controller:
        controller_id = analysis.controller_session_id
        if not controller_id:
            raise ValidationError(
                "A/B recovery resume command requires a validated controller session id"
            )
        return (
            f"ai_dev_loop pr-review resume {successor_run_id} "
            f"--controller-session-id {controller_id}"
        )
    return f"ai_dev_loop pr-review resume {successor_run_id}"


def render_pr_review_recovery_analysis(
    result: PrReviewRecoveryResult,
    *,
    output: str = "text",
) -> str:
    analysis = result.analysis
    payload = {
        "schema_version": 1,
        "dry_run": result.dry_run,
        "source_run_id": result.source_run_id,
        "eligible": analysis.eligible,
        "blockers": analysis.blockers,
        "warnings": analysis.warnings,
        "checkpoint": analysis.checkpoint,
        "reason_code": analysis.reason_code,
        "cycle_number": analysis.cycle_number,
        "iteration": analysis.iteration,
        "staged_patch_sha256": analysis.staged_patch_sha256,
        "pr_number": analysis.pr_number,
        "bound_head_sha_prefix": analysis.bound_head_sha_prefix,
        "expected_thread_count": analysis.expected_thread_count,
        "historical_schema_rejection": analysis.historical_schema_rejection,
        "historical_patch_fingerprint_adopted": analysis.historical_patch_fingerprint_adopted,
        "existing_successor_run_id": analysis.existing_successor_run_id,
        "trigger_will_be_reposted": False,
        "resume_command": result.resume_command,
        "message": result.message,
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Source: {result.source_run_id}",
        f"Eligible: {analysis.eligible}",
        f"Checkpoint: {analysis.checkpoint}",
        f"Reason: {analysis.reason_code}",
        f"Expected threads: {analysis.expected_thread_count}",
        "Trigger will be re-posted: false",
        result.message,
    ]
    if analysis.iteration is not None:
        lines.insert(4, f"Iteration: {analysis.iteration:02d}")
    if analysis.historical_patch_fingerprint_adopted:
        lines.append(
            "Publication patch fingerprint: adopted live raw hash after normalized "
            "artifact equivalence (source GPR hash left unchanged)"
        )
    if analysis.blockers:
        lines.append("Blockers: " + ", ".join(analysis.blockers))
    if result.resume_command:
        lines.append(f"Next: {result.resume_command}")
    return "\n".join(lines) + "\n"


def render_pr_review_recovery_result(
    result: PrReviewRecoveryResult,
    *,
    output: str = "text",
) -> str:
    analysis = result.analysis
    payload = {
        "schema_version": 1,
        "dry_run": result.dry_run,
        "source_run_id": result.source_run_id,
        "recovery_run_id": result.recovery_run_id,
        "reused_existing_successor": analysis.reused_existing_successor,
        "checkpoint": analysis.checkpoint,
        "reason_code": analysis.reason_code,
        "iteration": analysis.iteration,
        "staged_patch_sha256": analysis.staged_patch_sha256,
        "pr_number": analysis.pr_number,
        "bound_head_sha_prefix": analysis.bound_head_sha_prefix,
        "expected_thread_count": analysis.expected_thread_count,
        "trigger_will_be_reposted": False,
        "resume_command": result.resume_command,
        "message": result.message,
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        result.message,
        f"Source remains failed: {result.source_run_id}",
        "Trigger will be re-posted: false",
    ]
    if result.recovery_run_id:
        lines.append(f"Successor: {result.recovery_run_id}")
    if result.resume_command:
        lines.append(f"Next: {result.resume_command}")
    return "\n".join(lines) + "\n"
