"""Phase 15.17: durable external adjudication checkpoint model and helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.external_adjudication import (
    active_external_prompt_guard_sha256,
    build_external_adjudication_checkpoint,
    discover_current_cycle_artifacts,
    load_external_review_result,
    materialize_derived_external_artifacts,
    recovery_matches_active_external_round,
)
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.state import (
    EXTERNAL_ADJUDICATION_APPLICATION_STATUSES,
    CodexState,
    CursorState,
    ExternalAdjudicationCheckpoint,
    ExternalReplyIntent,
    GithubPrReviewState,
    PlanState,
    ProjectRef,
    PromptState,
    RecoveryState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    sha256_text,
    utc_now,
)

HEAD_SHA = "c29e15e6608a1111222233334444555566667777"
THREAD_A = "PRRT_A"
THREAD_B = "PRRT_B"
PROMPT_05 = "Please fix the two cycle-5 threads exactly.\n"


def _actionable_payload(thread_ids: list[str], prompt: str = PROMPT_05) -> dict:
    return {
        "eligible_thread_ids": thread_ids,
        "thread_decisions": [
            {
                "thread_id": thread_id,
                "decision": "actionable",
                "inline_reply": None,
                "summary": f"finding-{thread_id}",
            }
            for thread_id in thread_ids
        ],
        "all_actionable": True,
        "review_markdown": "report body\n",
        "cursor_fix_prompt": prompt,
        "tests_status": "not_applicable",
        "summary": "all actionable",
        "residual_risk_comment": None,
        "highest_severity": "P2",
    }


def _non_actionable_payload(thread_ids: list[str]) -> dict:
    return {
        "eligible_thread_ids": thread_ids,
        "thread_decisions": [
            {
                "thread_id": thread_ids[0],
                "decision": "not_applicable",
                "inline_reply": "@rojobad this does not apply",
                "summary": "na",
            },
            {
                "thread_id": thread_ids[1],
                "decision": "uncertain",
                "inline_reply": "@rojobad need clarification",
                "summary": "unc",
            },
        ],
        "all_actionable": False,
        "review_markdown": "mixed report\n",
        "cursor_fix_prompt": None,
        "tests_status": "not_applicable",
        "summary": "needs attention",
        "residual_risk_comment": None,
        "highest_severity": None,
    }


def _seed_cycle_artifacts(
    run_directory: Path,
    *,
    cycle_number: int,
    payload: dict,
    write_prompt: bool = True,
    write_report: bool = True,
    prompt_text: str | None = None,
) -> None:
    label = f"{cycle_number:02d}"
    cycle_dir = run_directory / "github" / "cycles" / label
    cycle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cycle_dir / "result.json", payload, sensitive=True)
    atomic_write_json(
        cycle_dir / "threads.snapshot.json",
        {
            "threads": [
                {"thread_id": thread_id, "body_sha256": "a" * 64}
                for thread_id in payload["eligible_thread_ids"]
            ]
        },
        sensitive=True,
    )
    if write_report:
        atomic_write_text(
            cycle_dir / "report.md",
            str(payload["review_markdown"]),
            sensitive=True,
        )
    if write_prompt and payload.get("cursor_fix_prompt"):
        prompt_path = run_directory / "prompts" / "fixes" / f"github-{label}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            prompt_path,
            prompt_text if prompt_text is not None else str(payload["cursor_fix_prompt"]),
            sensitive=True,
        )


def test_checkpoint_model_round_trip_and_status_enum() -> None:
    assert "cursor_pending" in EXTERNAL_ADJUDICATION_APPLICATION_STATUSES
    checkpoint = ExternalAdjudicationCheckpoint(
        cycle_number=5,
        bound_head_sha=HEAD_SHA,
        eligible_thread_ids=[THREAD_A, THREAD_B],
        result_path="github/cycles/05/result.json",
        result_sha256="a" * 64,
        snapshot_path="github/cycles/05/threads.snapshot.json",
        snapshot_sha256="b" * 64,
        report_path="github/cycles/05/report.md",
        report_sha256="c" * 64,
        fix_prompt_path="prompts/fixes/github-05.txt",
        fix_prompt_sha256="d" * 64,
        application_status="cursor_pending",
    )
    restored = ExternalAdjudicationCheckpoint.model_validate(checkpoint.model_dump())
    assert restored.cycle_number == 5
    assert restored.application_status == "cursor_pending"


def test_checkpoint_rejects_invalid_combinations() -> None:
    with pytest.raises(PydanticValidationError):
        ExternalAdjudicationCheckpoint(
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A],
            result_path="github/cycles/04/result.json",
            result_sha256="a" * 64,
            snapshot_path="github/cycles/05/threads.snapshot.json",
            snapshot_sha256="b" * 64,
            application_status="cursor_pending",
            fix_prompt_path="prompts/fixes/github-05.txt",
            fix_prompt_sha256="d" * 64,
        )
    with pytest.raises(PydanticValidationError):
        ExternalAdjudicationCheckpoint(
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A],
            result_path="github/cycles/05/result.json",
            result_sha256="a" * 64,
            snapshot_path="github/cycles/05/threads.snapshot.json",
            snapshot_sha256="b" * 64,
            application_status="cursor_pending",
        )
    with pytest.raises(PydanticValidationError):
        ExternalAdjudicationCheckpoint(
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A],
            result_path="github/cycles/05/result.json",
            result_sha256="a" * 64,
            snapshot_path="github/cycles/05/threads.snapshot.json",
            snapshot_sha256="b" * 64,
            application_status="replies_pending",
            fix_prompt_path="prompts/fixes/github-05.txt",
            fix_prompt_sha256="d" * 64,
        )
    with pytest.raises(PydanticValidationError):
        ExternalAdjudicationCheckpoint(
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A],
            result_path="../escape/result.json",
            result_sha256="a" * 64,
            snapshot_path="github/cycles/05/threads.snapshot.json",
            snapshot_sha256="b" * 64,
            application_status="replies_pending",
            reply_intents=[
                ExternalReplyIntent(
                    thread_id=THREAD_A,
                    decision="not_applicable",
                    inline_reply_sha256="e" * 64,
                )
            ],
        )


def test_historical_gpr_without_checkpoint_loads() -> None:
    payload = {
        "schema_version": 1,
        "origin": "independent_pr",
        "lifecycle": "interrupted",
        "cycle_number": 5,
        "max_external_cycles": 8,
        "pr_number": 45,
        "repository_name_with_owner": "acme/demo",
        "head_branch": "main",
        "base_branch": "master",
        "bound_head_sha": HEAD_SHA,
        "last_external_result_path": "github/cycles/04/result.json",
    }
    state = GithubPrReviewState.model_validate(payload)
    assert state.external_adjudication is None


def test_materialize_missing_prompt_from_valid_result(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    payload = _actionable_payload([THREAD_A, THREAD_B])
    _seed_cycle_artifacts(
        run_directory, cycle_number=5, payload=payload, write_prompt=False, write_report=False
    )
    review = load_external_review_result(run_directory, "github/cycles/05/result.json")
    report_rel, prompt_rel = materialize_derived_external_artifacts(
        run_directory, review, cycle_number=5
    )
    assert report_rel == "github/cycles/05/report.md"
    assert prompt_rel == "prompts/fixes/github-05.txt"
    assert (run_directory / prompt_rel).read_text(encoding="utf-8") == PROMPT_05


def test_materialize_rejects_diverged_prompt(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    payload = _actionable_payload([THREAD_A, THREAD_B])
    _seed_cycle_artifacts(
        run_directory,
        cycle_number=5,
        payload=payload,
        prompt_text="wrong prompt\n",
    )
    review = GithubPrReviewResult.model_validate(payload)
    with pytest.raises(ValidationError, match="drifted from structured"):
        materialize_derived_external_artifacts(run_directory, review, cycle_number=5)


def test_discover_current_cycle_and_prompt_guard_scoping(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    payload = _actionable_payload([THREAD_A, THREAD_B])
    _seed_cycle_artifacts(run_directory, cycle_number=5, payload=payload)
    discovered = discover_current_cycle_artifacts(
        run_directory,
        cycle_number=5,
        bound_head_sha=HEAD_SHA,
        expected_thread_ids=[THREAD_A, THREAD_B],
    )
    assert discovered is not None
    _review, checkpoint = discovered
    assert checkpoint.cycle_number == 5
    assert checkpoint.application_status == "cursor_pending"
    assert checkpoint.fix_prompt_sha256 == sha256_text(PROMPT_05)

    now = utc_now()
    state = RunState(
        run_id="fixture-20260719T021751Z-66d03c",
        project=ProjectRef(name="fixture"),
        status=RunStatus.INTERRUPTED,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
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
            current_review_iteration=4,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        recovery=RecoveryState(
            source_run_id="old-source",
            source_status=RunStatus.FAILED.value,
            source_iteration=3,
            recovered_checkpoint="external_feedback_cursor",
            source_staged_patch_sha256=None,
            created_at=now,
            runtime_migration="none",
            reason_code="external_feedback_cursor_not_started",
            expected_eligible_thread_ids=["PRRT_OLD"],
            source_prompt_path="prompts/fixes/github-03.txt",
            source_prompt_sha256="f" * 64,
        ),
        github_pr_review=GithubPrReviewState(
            origin="independent_pr",
            lifecycle="interrupted",
            cycle_number=5,
            max_external_cycles=8,
            pr_number=45,
            repository_name_with_owner="acme/demo",
            head_branch="main",
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A, THREAD_B],
            expected_eligible_thread_ids=[THREAD_A, THREAD_B],
            last_external_result_path="github/cycles/04/result.json",
            external_fix_prompt_path="prompts/fixes/github-04.txt",
            external_cursor_iteration=4,
            external_adjudication=checkpoint,
        ),
    )
    assert recovery_matches_active_external_round(state, state.recovery) is False
    assert active_external_prompt_guard_sha256(state) == checkpoint.fix_prompt_sha256


def test_build_checkpoint_for_non_actionable(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    payload = _non_actionable_payload([THREAD_A, THREAD_B])
    _seed_cycle_artifacts(run_directory, cycle_number=2, payload=payload, write_prompt=False)
    review = GithubPrReviewResult.model_validate(payload)
    checkpoint = build_external_adjudication_checkpoint(
        run_directory,
        review,
        cycle_number=2,
        bound_head_sha=HEAD_SHA,
        eligible_thread_ids=[THREAD_A, THREAD_B],
        application_status="replies_pending",
    )
    assert checkpoint.application_status == "replies_pending"
    assert len(checkpoint.reply_intents) == 2
    assert checkpoint.fix_prompt_path is None
    assert checkpoint.result_sha256 == sha256_file(run_directory / "github/cycles/02/result.json")


def test_schema_includes_external_adjudication_checkpoint() -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ai_dev_loop"
        / "schemas"
        / "run-state-v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    gpr = schema["properties"]["github_pr_review"]["properties"]
    assert "external_adjudication" in gpr
    checkpoint = gpr["external_adjudication"]["anyOf"][0]
    assert set(checkpoint["required"]) >= {
        "cycle_number",
        "bound_head_sha",
        "eligible_thread_ids",
        "result_path",
        "result_sha256",
        "snapshot_path",
        "snapshot_sha256",
        "application_status",
    }
    assert set(checkpoint["properties"]["application_status"]["enum"]) == set(
        EXTERNAL_ADJUDICATION_APPLICATION_STATUSES
    )
    reply_status = checkpoint["properties"]["reply_intents"]["items"]["properties"]["status"]
    assert set(reply_status["enum"]) == {"pending", "writing", "written", "ambiguous"}


def test_build_checkpoint_rejects_missing_and_empty_snapshot(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    payload = _actionable_payload([THREAD_A, THREAD_B])
    review = GithubPrReviewResult.model_validate(payload)
    cycle_dir = run_directory / "github" / "cycles" / "05"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cycle_dir / "result.json", payload, sensitive=True)
    (run_directory / "prompts" / "fixes").mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        run_directory / "prompts" / "fixes" / "github-05.txt",
        PROMPT_05,
        sensitive=True,
    )
    atomic_write_text(cycle_dir / "report.md", str(payload["review_markdown"]), sensitive=True)

    with pytest.raises(ValidationError, match="snapshot missing"):
        build_external_adjudication_checkpoint(
            run_directory,
            review,
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A, THREAD_B],
            application_status="cursor_pending",
        )

    (cycle_dir / "threads.snapshot.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="non-empty threads list"):
        build_external_adjudication_checkpoint(
            run_directory,
            review,
            cycle_number=5,
            bound_head_sha=HEAD_SHA,
            eligible_thread_ids=[THREAD_A, THREAD_B],
            application_status="cursor_pending",
        )
