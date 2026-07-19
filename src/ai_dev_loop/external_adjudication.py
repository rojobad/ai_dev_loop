"""Durable external GitHub adjudication checkpoint helpers (Phase 15.17)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.state import (
    ExternalAdjudicationCheckpoint,
    ExternalReplyIntent,
    RecoveryState,
    RunState,
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    sha256_text,
)


@dataclass(frozen=True)
class ExternalCycleArtifactPaths:
    """Deterministic relative paths for one external adjudication cycle."""

    cycle_number: int
    result_path: str
    snapshot_path: str
    report_path: str
    fix_prompt_path: str
    events_path: str
    stderr_path: str
    metadata_path: str

    @classmethod
    def for_cycle(cls, cycle_number: int) -> ExternalCycleArtifactPaths:
        if cycle_number < 1:
            raise ValidationError("cycle_number must be >= 1")
        label = f"{cycle_number:02d}"
        return cls(
            cycle_number=cycle_number,
            result_path=f"github/cycles/{label}/result.json",
            snapshot_path=f"github/cycles/{label}/threads.snapshot.json",
            report_path=f"github/cycles/{label}/report.md",
            fix_prompt_path=f"prompts/fixes/github-{label}.txt",
            events_path=f"github/cycles/{label}/codex.events.jsonl",
            stderr_path=f"github/cycles/{label}/codex.stderr.txt",
            metadata_path=f"github/cycles/{label}/codex.metadata.json",
        )


def assert_safe_run_relative_path(relative_path: str, *, run_directory: Path) -> Path:
    """Resolve a run-relative path or raise when it escapes the run directory."""

    if not relative_path or relative_path.startswith("/") or "\\" in relative_path:
        raise ValidationError("external adjudication path must be a safe run-relative path")
    parts = Path(relative_path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("external adjudication path must not contain '.' or '..'")
    resolved_root = run_directory.resolve()
    candidate = (run_directory / relative_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValidationError("external adjudication path escaped the run directory") from exc
    return candidate


def load_external_review_result(run_directory: Path, relative_path: str) -> GithubPrReviewResult:
    """Load and schema-validate an external adjudication result artifact."""

    path = assert_safe_run_relative_path(relative_path, run_directory=run_directory)
    if not path.is_file():
        raise ValidationError(f"GitHub PR review result missing: {relative_path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"GitHub PR review result is not valid JSON: {exc}") from exc
    try:
        return GithubPrReviewResult.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"GitHub PR review result validation failed: {exc}") from exc


def materialize_derived_external_artifacts(
    run_directory: Path,
    review: GithubPrReviewResult,
    *,
    cycle_number: int,
) -> tuple[str, str | None]:
    """Ensure report/prompt exist from a validated result; return relative paths.

    Missing derived artifacts are rewritten only from the structured result.
    An existing prompt that does not match ``cursor_fix_prompt`` byte-for-byte
    is a hard failure — never adopted or rewritten from another cycle.
    """

    paths = ExternalCycleArtifactPaths.for_cycle(cycle_number)
    report_abs = assert_safe_run_relative_path(paths.report_path, run_directory=run_directory)
    if not report_abs.is_file():
        atomic_write_text(report_abs, review.review_markdown, sensitive=True)
    else:
        existing_report = report_abs.read_text(encoding="utf-8")
        if existing_report != review.review_markdown:
            raise ValidationError(
                f"external report artifact drifted from structured result: {paths.report_path}"
            )

    fix_prompt_path: str | None = None
    if review.all_actionable:
        if not review.cursor_fix_prompt:
            raise ValidationError("all-actionable result is missing cursor_fix_prompt")
        prompt_abs = assert_safe_run_relative_path(
            paths.fix_prompt_path, run_directory=run_directory
        )
        if not prompt_abs.is_file():
            atomic_write_text(prompt_abs, review.cursor_fix_prompt, sensitive=True)
        else:
            existing = prompt_abs.read_text(encoding="utf-8")
            if existing != review.cursor_fix_prompt:
                raise ValidationError(
                    "external fix prompt drifted from structured cursor_fix_prompt; "
                    f"refusing to adopt {paths.fix_prompt_path}"
                )
        fix_prompt_path = paths.fix_prompt_path
    return paths.report_path, fix_prompt_path


def _parse_snapshot_thread_ids(payload: object) -> list[str] | None:
    threads = payload.get("threads") if isinstance(payload, dict) else payload
    if not isinstance(threads, list):
        return None
    if not threads:
        return []
    ids: list[str] = []
    for item in threads:
        if isinstance(item, dict) and item.get("thread_id"):
            ids.append(str(item["thread_id"]))
        elif isinstance(item, str) and item.strip():
            ids.append(item)
        else:
            raise ValidationError("external thread snapshot entries must include thread_id")
    if len(ids) != len(set(ids)):
        raise ValidationError("external thread snapshot contains duplicate thread IDs")
    return ids


def _snapshot_thread_ids(run_directory: Path, snapshot_rel: str) -> list[str]:
    """Load frozen thread IDs from a durable non-empty snapshot; never synthesize."""

    path = assert_safe_run_relative_path(snapshot_rel, run_directory=run_directory)
    if not path.is_file():
        raise ValidationError(f"external thread snapshot missing: {snapshot_rel}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"external thread snapshot is not valid JSON: {exc}") from exc
    ids = _parse_snapshot_thread_ids(payload)
    if ids is None:
        raise ValidationError("external thread snapshot must contain a threads list")
    if not ids:
        raise ValidationError("external thread snapshot must contain a non-empty threads list")
    return ids


def build_reply_intents(review: GithubPrReviewResult) -> list[ExternalReplyIntent]:
    intents: list[ExternalReplyIntent] = []
    for decision in review.thread_decisions:
        if decision.decision == "actionable":
            continue
        assert decision.inline_reply is not None
        intents.append(
            ExternalReplyIntent(
                thread_id=decision.thread_id,
                decision=decision.decision,
                inline_reply_sha256=sha256_text(decision.inline_reply),
                status="pending",
            )
        )
    return intents


def build_external_adjudication_checkpoint(
    run_directory: Path,
    review: GithubPrReviewResult,
    *,
    cycle_number: int,
    bound_head_sha: str,
    eligible_thread_ids: list[str],
    application_status: str,
) -> ExternalAdjudicationCheckpoint:
    """Build a typed checkpoint from a validated result and on-disk artifacts."""

    paths = ExternalCycleArtifactPaths.for_cycle(cycle_number)
    if set(review.eligible_thread_ids) != set(eligible_thread_ids):
        raise ValidationError(
            "structured result eligible_thread_ids do not match the frozen thread set"
        )
    snapshot_ids = _snapshot_thread_ids(run_directory, paths.snapshot_path)
    if set(snapshot_ids) != set(eligible_thread_ids):
        raise ValidationError("thread snapshot IDs do not match the frozen eligible thread set")
    report_rel, fix_prompt_rel = materialize_derived_external_artifacts(
        run_directory,
        review,
        cycle_number=cycle_number,
    )
    result_abs = assert_safe_run_relative_path(paths.result_path, run_directory=run_directory)
    if not result_abs.is_file():
        # Mocks / interrupted writers may hold a validated in-memory result
        # without the durable JSON yet; persist it before hashing.
        atomic_write_json(
            result_abs,
            review.model_dump(mode="json"),
            sensitive=True,
        )
    snapshot_abs = assert_safe_run_relative_path(paths.snapshot_path, run_directory=run_directory)
    report_abs = assert_safe_run_relative_path(report_rel, run_directory=run_directory)
    reply_intents: list[ExternalReplyIntent] = []
    fix_prompt_sha: str | None = None
    if application_status in {"cursor_pending", "cursor_scheduled"}:
        if not review.all_actionable or not fix_prompt_rel:
            raise ValidationError(
                "cursor checkpoint requires an all-actionable result and fix prompt"
            )
        fix_prompt_sha = sha256_file(
            assert_safe_run_relative_path(fix_prompt_rel, run_directory=run_directory)
        )
        if sha256_text(review.cursor_fix_prompt or "") != fix_prompt_sha:
            raise ValidationError("fix prompt hash does not match structured cursor_fix_prompt")
    elif application_status == "replies_pending":
        if review.all_actionable:
            raise ValidationError("replies_pending requires a non-actionable result")
        reply_intents = build_reply_intents(review)
    elif application_status != "consumed":
        raise ValidationError(f"unsupported application_status: {application_status}")

    return ExternalAdjudicationCheckpoint(
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        eligible_thread_ids=list(eligible_thread_ids),
        result_path=paths.result_path,
        result_sha256=sha256_file(result_abs),
        snapshot_path=paths.snapshot_path,
        snapshot_sha256=sha256_file(snapshot_abs),
        report_path=report_rel,
        report_sha256=sha256_file(report_abs),
        fix_prompt_path=fix_prompt_rel,
        fix_prompt_sha256=fix_prompt_sha,
        application_status=application_status,
        reply_intents=reply_intents,
    )


def verify_checkpoint_artifacts(
    run_directory: Path,
    checkpoint: ExternalAdjudicationCheckpoint,
) -> GithubPrReviewResult:
    """Re-validate checkpoint hashes/IDs against durable artifacts."""

    paths = ExternalCycleArtifactPaths.for_cycle(checkpoint.cycle_number)
    if checkpoint.result_path != paths.result_path:
        raise ValidationError("checkpoint result_path does not match cycle layout")
    if checkpoint.snapshot_path != paths.snapshot_path:
        raise ValidationError("checkpoint snapshot_path does not match cycle layout")
    result_abs = assert_safe_run_relative_path(checkpoint.result_path, run_directory=run_directory)
    snapshot_abs = assert_safe_run_relative_path(
        checkpoint.snapshot_path, run_directory=run_directory
    )
    if sha256_file(result_abs) != checkpoint.result_sha256:
        raise ValidationError("external result artifact hash drifted from checkpoint")
    if sha256_file(snapshot_abs) != checkpoint.snapshot_sha256:
        raise ValidationError("external snapshot artifact hash drifted from checkpoint")
    review = load_external_review_result(run_directory, checkpoint.result_path)
    if set(review.eligible_thread_ids) != set(checkpoint.eligible_thread_ids):
        raise ValidationError("structured result IDs drifted from checkpoint freeze")
    snapshot_ids = _snapshot_thread_ids(run_directory, checkpoint.snapshot_path)
    if set(snapshot_ids) != set(checkpoint.eligible_thread_ids):
        raise ValidationError("snapshot IDs drifted from checkpoint freeze")
    if checkpoint.report_path:
        report_abs = assert_safe_run_relative_path(
            checkpoint.report_path, run_directory=run_directory
        )
        if not report_abs.is_file():
            raise ValidationError(f"external report missing: {checkpoint.report_path}")
        assert checkpoint.report_sha256 is not None
        if sha256_file(report_abs) != checkpoint.report_sha256:
            raise ValidationError("external report hash drifted from checkpoint")
    if checkpoint.fix_prompt_path:
        prompt_abs = assert_safe_run_relative_path(
            checkpoint.fix_prompt_path, run_directory=run_directory
        )
        if not prompt_abs.is_file():
            raise ValidationError(f"external fix prompt missing: {checkpoint.fix_prompt_path}")
        assert checkpoint.fix_prompt_sha256 is not None
        if sha256_file(prompt_abs) != checkpoint.fix_prompt_sha256:
            raise ValidationError("external fix prompt hash drifted from checkpoint")
        if review.cursor_fix_prompt is None:
            raise ValidationError("checkpoint has fix prompt but result has null cursor_fix_prompt")
        if sha256_text(review.cursor_fix_prompt) != checkpoint.fix_prompt_sha256:
            raise ValidationError(
                "checkpoint fix prompt hash does not match structured cursor_fix_prompt"
            )
    return review


def recovery_matches_active_external_round(
    state: RunState,
    recovery: RecoveryState,
) -> bool:
    """True only when recovery lineage describes the currently pending external round."""

    gpr = state.github_pr_review
    if gpr is None:
        return False
    if recovery.recovered_checkpoint != "external_feedback_cursor":
        return False
    if recovery.reason_code != "external_feedback_cursor_not_started":
        return False
    if gpr.external_cursor_iteration != recovery.source_iteration:
        return False
    if gpr.external_fix_prompt_path != recovery.source_prompt_path:
        return False
    if not recovery.source_prompt_sha256 or not recovery.source_prompt_path:
        return False
    if not recovery.expected_eligible_thread_ids:
        return False
    expected = list(gpr.expected_eligible_thread_ids or gpr.eligible_thread_ids)
    return set(expected) == set(recovery.expected_eligible_thread_ids)


def active_external_prompt_guard_sha256(state: RunState) -> str | None:
    """Prompt hash guard for the active external round only.

    Prefers the durable checkpoint. Falls back to recovery lineage only when that
    lineage still describes the exact pending round.
    """

    gpr = state.github_pr_review
    if gpr is None:
        return None
    checkpoint = gpr.external_adjudication
    if (
        checkpoint is not None
        and checkpoint.application_status in {"cursor_pending", "cursor_scheduled"}
        and checkpoint.fix_prompt_sha256
    ):
        return checkpoint.fix_prompt_sha256
    recovery = state.recovery
    if recovery is not None and recovery_matches_active_external_round(state, recovery):
        return recovery.source_prompt_sha256
    return None


def discover_current_cycle_artifacts(
    run_directory: Path,
    *,
    cycle_number: int,
    bound_head_sha: str,
    expected_thread_ids: list[str] | None,
) -> tuple[GithubPrReviewResult, ExternalAdjudicationCheckpoint] | None:
    """Hydrate a checkpoint from deterministic current-cycle artifacts when present.

    Returns None when no valid current-cycle result exists. Fail-closed on any
    mismatch when a result file is present but inconsistent.
    """

    paths = ExternalCycleArtifactPaths.for_cycle(cycle_number)
    result_abs = run_directory / paths.result_path
    if not result_abs.is_file():
        return None
    # Presence of a result for this cycle means we must validate it exactly.
    review = load_external_review_result(run_directory, paths.result_path)
    if expected_thread_ids is not None and set(expected_thread_ids) != set(
        review.eligible_thread_ids
    ):
        raise ValidationError(
            "current-cycle external result thread set does not match frozen eligible IDs"
        )
    thread_ids = list(review.eligible_thread_ids)
    if not (run_directory / paths.snapshot_path).is_file():
        raise ValidationError(
            f"current-cycle external result exists but snapshot is missing: {paths.snapshot_path}"
        )
    status = "cursor_pending" if review.all_actionable else "replies_pending"
    checkpoint = build_external_adjudication_checkpoint(
        run_directory,
        review,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        eligible_thread_ids=thread_ids,
        application_status=status,
    )
    return review, checkpoint


def gpr_updates_for_checkpoint(
    checkpoint: ExternalAdjudicationCheckpoint,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GPR field updates that promote a checkpoint into operational pointers."""

    updates: dict[str, Any] = {
        "external_adjudication": checkpoint,
        "last_external_result_path": checkpoint.result_path,
        "last_snapshot_path": checkpoint.snapshot_path,
        "eligible_thread_ids": list(checkpoint.eligible_thread_ids),
        "expected_eligible_thread_ids": list(checkpoint.eligible_thread_ids),
    }
    if checkpoint.fix_prompt_path:
        updates["external_fix_prompt_path"] = checkpoint.fix_prompt_path
    if extra:
        updates.update(extra)
    return updates


def mark_checkpoint_status(
    checkpoint: ExternalAdjudicationCheckpoint,
    application_status: str,
    *,
    reply_intents: list[ExternalReplyIntent] | None = None,
) -> ExternalAdjudicationCheckpoint:
    payload = checkpoint.model_dump()
    payload["application_status"] = application_status
    if reply_intents is not None:
        payload["reply_intents"] = [item.model_dump() for item in reply_intents]
    return ExternalAdjudicationCheckpoint.model_validate(payload)
