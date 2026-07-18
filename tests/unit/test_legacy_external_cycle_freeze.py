"""Unit coverage for Phase 15.10 legacy external-cycle freeze classification."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from ai_dev_loop.commands.pr_review import (
    LegacyExternalCycleFreeze,
    _classify_legacy_external_cycle_freeze,
)
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    GithubPrReviewState,
    PlanState,
    ProjectRef,
    PromptState,
    RecoveryState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    utc_now,
)

THREAD_OLD_A = "PRRT_CYCLE1_A"
THREAD_OLD_B = "PRRT_CYCLE1_B"
HEAD_SHA = "c29e15e6608a1111222233334444555566667777"


def _base_state(
    *,
    status: RunStatus = RunStatus.WAITING_FOR_USER_ATTENTION,
    lifecycle: str = "waiting_for_user_attention",
    worker_outcome: str | None = "eligible_thread_set_drift",
    cycle_number: int = 2,
    expected: list[str] | None = None,
    recovery_expected: list[str] | None = None,
    processed: list[str] | None = None,
    resolved: list[str] | None = None,
    recovery_checkpoint: str | None = "external_adjudication",
    source_iteration: int = 1,
    include_recovery: bool = True,
    result: str | None = "localized drift text must not decide recoverability",
    last_error: str | None = "eligible_thread_set_drift in last_error is not evidence",
) -> RunState:
    now = utc_now()
    expected_ids = list(expected if expected is not None else [THREAD_OLD_A, THREAD_OLD_B])
    recovery_ids = list(
        recovery_expected if recovery_expected is not None else [THREAD_OLD_A, THREAD_OLD_B]
    )
    processed_ids = list(processed if processed is not None else [THREAD_OLD_A, THREAD_OLD_B])
    resolved_ids = list(resolved if resolved is not None else [THREAD_OLD_A, THREAD_OLD_B])
    recovery = None
    if include_recovery and recovery_checkpoint is not None:
        recovery = RecoveryState(
            source_run_id="failed-source",
            source_status=RunStatus.FAILED.value,
            source_iteration=source_iteration,
            recovered_checkpoint=recovery_checkpoint,
            created_at=now,
            runtime_migration="none",
            reason_code=(
                "github_adjudication_schema_incompatible"
                if recovery_checkpoint == "external_adjudication"
                else "codex_review_result_artifact_missing"
            ),
            expected_eligible_thread_ids=(
                recovery_ids if recovery_checkpoint == "external_adjudication" else None
            ),
            source_staged_patch_sha256=(
                None if recovery_checkpoint == "external_adjudication" else "a" * 64
            ),
        )
    return RunState(
        run_id="crypto-sentinel-20260718T115934Z-176634",
        project=ProjectRef(name="crypto-sentinel"),
        status=status,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="pr-45",
            initial_head=HEAD_SHA,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256="a" * 64,
        ),
        prompt=PromptState(
            source_repository_path="docs/plans/prompt_sample-plan.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-0000000000bb",
            session_model=None,
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
            chat_id="019abc00-1111-2222-3333-444444444444",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        recovery=recovery,
        github_pr_review=GithubPrReviewState(
            origin="source_run",
            source_run_id="source-local",
            lifecycle=lifecycle,
            cycle_number=cycle_number,
            max_external_cycles=8,
            pr_number=45,
            head_branch="pr-45",
            bound_head_sha=HEAD_SHA,
            request_comment_id="9002",
            request_marker=f"ai_dev_loop-pr-review:run:cycle:{cycle_number}:{HEAD_SHA}",
            request_created_at="2026-07-18T12:00:00+00:00",
            expected_eligible_thread_ids=expected_ids,
            processed_thread_ids=processed_ids,
            resolved_thread_ids=resolved_ids,
            worker_outcome=worker_outcome,
        ),
        result=result,
        last_error=last_error,
    )


def test_classifier_accepts_exact_historical_shape() -> None:
    classified = _classify_legacy_external_cycle_freeze(_base_state())
    assert classified == LegacyExternalCycleFreeze(
        source_cycle=1,
        current_cycle=2,
        expected_thread_count=2,
    )
    # Safe result exposes only counts/cycle numbers.
    assert set(asdict(classified)) == {
        "source_cycle",
        "current_cycle",
        "expected_thread_count",
    }


def test_classifier_ignores_localized_error_text() -> None:
    positive = _base_state(
        result="totally different wording",
        last_error="unrelated failure narrative",
    )
    assert _classify_legacy_external_cycle_freeze(positive) is not None

    negative = _base_state(
        worker_outcome="validation_error",
        result="Eligible thread set no longer matches the frozen recovery set",
        last_error="eligible_thread_set_drift",
    )
    assert _classify_legacy_external_cycle_freeze(negative) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_iteration": 2, "cycle_number": 2},
        {"expected": None},  # replaced below
        {"recovery_expected": [THREAD_OLD_A]},
        {"expected": [THREAD_OLD_A], "recovery_expected": [THREAD_OLD_A, THREAD_OLD_B]},
        {"include_recovery": False},
        {"recovery_checkpoint": "reviewing"},
        {"processed": [THREAD_OLD_A], "resolved": [THREAD_OLD_A, THREAD_OLD_B]},
        {"processed": [THREAD_OLD_A, THREAD_OLD_B], "resolved": [THREAD_OLD_A]},
        {"status": RunStatus.AWAITING_BOT_REVIEW, "lifecycle": "awaiting_bot_review"},
        {"lifecycle": "interrupted", "status": RunStatus.INTERRUPTED},
        {"worker_outcome": "timeout"},
        {"worker_outcome": None},
    ],
)
def test_classifier_rejects_non_matching_shapes(kwargs: dict) -> None:
    if "expected" in kwargs and kwargs["expected"] is None:
        # Empty expected is invalid on the model; simulate absence via model_copy.
        state = _base_state()
        assert state.github_pr_review is not None
        state.github_pr_review = state.github_pr_review.model_copy(
            update={"expected_eligible_thread_ids": None}
        )
        assert _classify_legacy_external_cycle_freeze(state) is None
        return
    if kwargs.get("recovery_checkpoint") == "reviewing":
        # reviewing recovery requires staged-patch hash; build via helper defaults.
        state = _base_state(include_recovery=False)
        state.recovery = RecoveryState(
            source_run_id="failed-source",
            source_status=RunStatus.FAILED.value,
            source_iteration=1,
            recovered_checkpoint="reviewing",
            created_at=utc_now(),
            runtime_migration="none",
            reason_code="codex_review_result_artifact_missing",
            source_staged_patch_sha256="a" * 64,
        )
        assert _classify_legacy_external_cycle_freeze(state) is None
        return
    assert _classify_legacy_external_cycle_freeze(_base_state(**kwargs)) is None
