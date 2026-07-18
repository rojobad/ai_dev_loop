"""Unit coverage for Phase 15.10/15.11 legacy external-cycle freeze classification."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from ai_dev_loop.commands.pr_review import (
    LegacyExternalCycleFreeze,
    _classify_legacy_external_cycle_freeze,
)
from ai_dev_loop.errors import ValidationError
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
SOURCE_RUN_ID = "crypto-sentinel-20260718T024655Z-ff42cb"
CURRENT_RUN_ID = "crypto-sentinel-20260718T115934Z-176634"
REPO_ROOT = "/tmp/repo"
REPO_NWO = "acme/demo"


def _base_state(
    *,
    run_id: str = CURRENT_RUN_ID,
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
    source_run_id: str = SOURCE_RUN_ID,
    source_status: str = RunStatus.FAILED.value,
    include_recovery: bool = True,
    project_name: str = "crypto-sentinel",
    repo_root: str = REPO_ROOT,
    branch: str = "pr-45",
    head_branch: str | None = None,
    repository_name_with_owner: str | None = REPO_NWO,
    pr_number: int | None = 45,
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
            source_run_id=source_run_id,
            source_status=source_status,
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
        run_id=run_id,
        project=ProjectRef(name=project_name),
        status=status,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=repo_root,
            git_common_dir=f"{repo_root}/.git",
            git_dir=f"{repo_root}/.git",
            branch=branch,
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
            pr_number=pr_number,
            repository_name_with_owner=repository_name_with_owner,
            head_branch=head_branch if head_branch is not None else branch,
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


def _terminal_external_ancestor(
    *,
    run_id: str = SOURCE_RUN_ID,
    status: RunStatus = RunStatus.FAILED,
    cycle_number: int = 1,
    source_iteration: int = 1,
    expected: list[str] | None = None,
    recovery_expected: list[str] | None = None,
    recovery_checkpoint: str = "external_adjudication",
    reason_code: str | None = None,
    project_name: str = "crypto-sentinel",
    repo_root: str = REPO_ROOT,
    branch: str = "pr-45",
    head_branch: str | None = None,
    repository_name_with_owner: str | None = REPO_NWO,
    pr_number: int | None = 45,
    gpr_expected_override: list[str] | None = None,
) -> RunState:
    ids = list(expected if expected is not None else [THREAD_OLD_A, THREAD_OLD_B])
    recovery_ids = list(recovery_expected if recovery_expected is not None else ids)
    gpr_ids = list(gpr_expected_override if gpr_expected_override is not None else ids)
    now = utc_now()
    if reason_code is None:
        reason_code = (
            "github_adjudication_schema_incompatible"
            if recovery_checkpoint == "external_adjudication"
            else "codex_review_result_artifact_missing"
        )
    recovery = RecoveryState(
        source_run_id="original-failed-adjudication",
        source_status=RunStatus.FAILED.value,
        source_iteration=source_iteration,
        recovered_checkpoint=recovery_checkpoint,
        created_at=now,
        runtime_migration="none",
        reason_code=reason_code,
        expected_eligible_thread_ids=(
            recovery_ids if recovery_checkpoint == "external_adjudication" else None
        ),
        source_staged_patch_sha256=(
            None if recovery_checkpoint == "external_adjudication" else "a" * 64
        ),
    )
    state = _base_state(
        run_id=run_id,
        status=status,
        lifecycle="interrupted" if status == RunStatus.FAILED else "waiting_for_user_attention",
        worker_outcome="adjudication_schema_incompatible",
        cycle_number=cycle_number,
        expected=gpr_ids,
        processed=ids,
        resolved=ids,
        include_recovery=False,
        project_name=project_name,
        repo_root=repo_root,
        branch=branch,
        head_branch=head_branch,
        repository_name_with_owner=repository_name_with_owner,
        pr_number=pr_number,
        result="schema incompatible",
        last_error="schema incompatible",
    )
    state.recovery = recovery
    if status == RunStatus.FAILED:
        state.github_pr_review = state.github_pr_review.model_copy(
            update={"lifecycle": "interrupted"}
        )
    return state


def _nested_current(*, source_run_id: str = SOURCE_RUN_ID, **kwargs) -> RunState:
    defaults = {
        "recovery_checkpoint": "reviewing",
        "source_run_id": source_run_id,
        "source_iteration": 1,
        "include_recovery": True,
    }
    defaults.update(kwargs)
    return _base_state(**defaults)


def _loader_for(ancestor: RunState | None, *, raise_exc: Exception | None = None):
    def _load(run_id: str) -> tuple[Path, RunState]:
        if raise_exc is not None:
            raise raise_exc
        if ancestor is None or run_id != ancestor.run_id:
            raise ValidationError(f"run not found: {run_id}")
        return Path(f"/tmp/state/runs/{ancestor.project.name}/{run_id}"), ancestor

    return _load


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


def test_classifier_accepts_one_hop_nested_reviewing_lineage() -> None:
    ancestor = _terminal_external_ancestor()
    current = _nested_current()
    classified = _classify_legacy_external_cycle_freeze(
        current,
        load_run_fn=_loader_for(ancestor),
    )
    assert classified == LegacyExternalCycleFreeze(
        source_cycle=1,
        current_cycle=2,
        expected_thread_count=2,
    )


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

    nested = _nested_current(
        result="Eligible thread set no longer matches the frozen recovery set",
        last_error="eligible_thread_set_drift",
    )
    assert (
        _classify_legacy_external_cycle_freeze(
            nested,
            load_run_fn=_loader_for(_terminal_external_ancestor()),
        )
        is not None
    )
    nested_wrong_outcome = _nested_current(worker_outcome="validation_error")
    assert (
        _classify_legacy_external_cycle_freeze(
            nested_wrong_outcome,
            load_run_fn=_loader_for(_terminal_external_ancestor()),
        )
        is None
    )


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
        # reviewing without a loadable matching ancestor is a safe no-match.
        state = _base_state(include_recovery=False)
        state.recovery = RecoveryState(
            source_run_id=SOURCE_RUN_ID,
            source_status=RunStatus.FAILED.value,
            source_iteration=1,
            recovered_checkpoint="reviewing",
            created_at=utc_now(),
            runtime_migration="none",
            reason_code="codex_review_result_artifact_missing",
            source_staged_patch_sha256="a" * 64,
        )
        assert (
            _classify_legacy_external_cycle_freeze(
                state,
                load_run_fn=_loader_for(None),
            )
            is None
        )
        return
    assert _classify_legacy_external_cycle_freeze(_base_state(**kwargs)) is None


@pytest.mark.parametrize(
    "defect",
    [
        "missing_source",
        "self_reference",
        "load_error",
        "non_terminal",
        "status_mismatch",
        "identity_project",
        "identity_repo",
        "identity_branch",
        "identity_head_branch",
        "identity_pr",
        "identity_nwo",
        "identity_nwo_missing",
        "wrong_checkpoint",
        "wrong_reason",
        "cycle_not_advanced",
        "ancestor_cycle_mismatch",
        "sets_differ_gpr",
        "sets_differ_recovery",
        "ids_not_processed",
        "ids_not_resolved",
    ],
)
def test_classifier_rejects_invalid_nested_lineage(defect: str) -> None:
    ancestor = _terminal_external_ancestor()
    current = _nested_current()
    loader = _loader_for(ancestor)

    if defect == "missing_source":
        loader = _loader_for(None)
    elif defect == "self_reference":
        current = _nested_current(source_run_id=CURRENT_RUN_ID)
        loader = _loader_for(ancestor)
    elif defect == "load_error":
        loader = _loader_for(ancestor, raise_exc=RuntimeError("boom"))
    elif defect == "non_terminal":
        # Lineage still claims failed; the live ancestor is not terminal failed.
        ancestor = _terminal_external_ancestor(status=RunStatus.INTERRUPTED)
        loader = _loader_for(ancestor)
    elif defect == "status_mismatch":
        # Bypass RecoveryState validator to simulate mismatched lineage status.
        current = _nested_current()
        assert current.recovery is not None
        current.recovery = RecoveryState.model_construct(
            **{
                **current.recovery.model_dump(mode="python"),
                "source_status": "completed",
            }
        )
        loader = _loader_for(ancestor)
    elif defect == "identity_project":
        ancestor = _terminal_external_ancestor(project_name="other-project")
        loader = _loader_for(ancestor)
    elif defect == "identity_repo":
        ancestor = _terminal_external_ancestor(repo_root="/tmp/other-repo")
        loader = _loader_for(ancestor)
    elif defect == "identity_branch":
        ancestor = _terminal_external_ancestor(branch="other-branch")
        loader = _loader_for(ancestor)
    elif defect == "identity_head_branch":
        # Local repository.branch still matches; only persisted GitHub head differs.
        ancestor = _terminal_external_ancestor(head_branch="other-github-head")
        loader = _loader_for(ancestor)
    elif defect == "identity_pr":
        ancestor = _terminal_external_ancestor(pr_number=99)
        loader = _loader_for(ancestor)
    elif defect == "identity_nwo":
        ancestor = _terminal_external_ancestor(repository_name_with_owner="other/owner")
        loader = _loader_for(ancestor)
    elif defect == "identity_nwo_missing":
        ancestor = _terminal_external_ancestor(repository_name_with_owner=None)
        loader = _loader_for(ancestor)
    elif defect == "wrong_checkpoint":
        ancestor = _terminal_external_ancestor(recovery_checkpoint="reviewing")
        loader = _loader_for(ancestor)
    elif defect == "wrong_reason":
        # Keep external checkpoint but force a non-matching reason via rebuild.
        ancestor = _terminal_external_ancestor()
        assert ancestor.recovery is not None
        # github_adjudication_schema_incompatible is required by the model for
        # external_adjudication; simulate an incompatible ancestor by clearing
        # the recovery entirely (no external evidence).
        ancestor.recovery = None
        loader = _loader_for(ancestor)
    elif defect == "cycle_not_advanced":
        ancestor = _terminal_external_ancestor(cycle_number=2, source_iteration=2)
        loader = _loader_for(ancestor)
    elif defect == "ancestor_cycle_mismatch":
        # Both ancestor values are older than current cycle 3, but disagree.
        current = _nested_current(cycle_number=3)
        ancestor = _terminal_external_ancestor(cycle_number=1, source_iteration=2)
        loader = _loader_for(ancestor)
    elif defect == "sets_differ_gpr":
        ancestor = _terminal_external_ancestor(
            gpr_expected_override=[THREAD_OLD_A],
            recovery_expected=[THREAD_OLD_A, THREAD_OLD_B],
        )
        loader = _loader_for(ancestor)
    elif defect == "sets_differ_recovery":
        ancestor = _terminal_external_ancestor(recovery_expected=[THREAD_OLD_A])
        loader = _loader_for(ancestor)
    elif defect == "ids_not_processed":
        current = _nested_current(processed=[THREAD_OLD_A])
        loader = _loader_for(ancestor)
    elif defect == "ids_not_resolved":
        current = _nested_current(resolved=[THREAD_OLD_A])
        loader = _loader_for(ancestor)

    assert _classify_legacy_external_cycle_freeze(current, load_run_fn=loader) is None


def test_nested_classifier_does_not_follow_ancestor_source() -> None:
    """Only one hop: ancestor's own source_run_id must never be loaded."""

    loaded: list[str] = []
    ancestor = _terminal_external_ancestor()
    assert ancestor.recovery is not None
    deeper_id = ancestor.recovery.source_run_id
    current = _nested_current()

    def _load(run_id: str) -> tuple[Path, RunState]:
        loaded.append(run_id)
        if run_id == deeper_id:
            raise AssertionError("must not recurse into ancestor source")
        if run_id != ancestor.run_id:
            raise ValidationError(f"run not found: {run_id}")
        return Path(f"/tmp/state/runs/{ancestor.project.name}/{run_id}"), ancestor

    classified = _classify_legacy_external_cycle_freeze(current, load_run_fn=_load)
    assert classified is not None
    assert loaded == [SOURCE_RUN_ID]


def test_direct_match_preferred_over_nested_loader() -> None:
    """Direct external recovery must not call load_run."""

    calls: list[str] = []

    def _load(run_id: str) -> tuple[Path, RunState]:
        calls.append(run_id)
        raise AssertionError("direct match must not load ancestors")

    classified = _classify_legacy_external_cycle_freeze(_base_state(), load_run_fn=_load)
    assert classified is not None
    assert calls == []
