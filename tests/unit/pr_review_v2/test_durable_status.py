"""Unit tests for Phase 16.4 status projection and architecture isolation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import start_run

from ai_dev_loop.pr_review_v2.application.contracts import (
    EventSubmission,
    NextActionCategory,
    PrReviewEngineError,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    EffectBlocked,
    EffectCompletionToken,
    PauseReasonKind,
    PreparedState,
    ResumeRequested,
    SafeAction,
    SafeActionKind,
    StartRequested,
)

V2_ROOT = Path(__file__).resolve().parents[3] / "src" / "ai_dev_loop" / "pr_review_v2"

FORBIDDEN_LEGACY = (
    "ai_dev_loop.state",
    "ai_dev_loop.workflow_engine",
    "ai_dev_loop.local_review_loop",
    "ai_dev_loop.commands",
    "ai_dev_loop.runners",
    "ai_dev_loop.external_adjudication",
    "ai_dev_loop.github_pr_review_result",
    "ai_dev_loop.pr_review_worker",
    "ai_dev_loop.iterations",
    "ai_dev_loop.resume_planner",
)


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_status_prepared_and_after_start(engine: PrReviewEngine, prepared: PreparedState) -> None:
    status = engine.create_run(prepared.run_id, prepared)
    assert status.state_kind == "prepared"
    assert status.next_action is NextActionCategory.START
    assert status.lease_active is False
    assert status.lease_generation == 0
    assert "owner" not in status.model_dump_json()

    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.resulting_run_version == 2
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "publishing_initial"
    assert status.active_effect_kind == "generate_publication_text"
    assert status.next_action is NextActionCategory.EXECUTE_EFFECT
    assert status.head_sha_short is None or len(status.head_sha_short) <= 12


def test_status_does_not_leak_owner(engine: PrReviewEngine, prepared: PreparedState) -> None:
    start_run(engine, prepared)
    engine.acquire_lease(prepared.run_id, "secret-owner-token")
    dumped = engine.get_status(prepared.run_id).model_dump()
    assert "owner_id" not in dumped
    assert "secret-owner-token" not in str(dumped)


def test_fenced_apply_event_rejects_effect_results(
    engine: PrReviewEngine, prepared: PreparedState
) -> None:
    engine.create_run(prepared.run_id, prepared)
    token = EffectCompletionToken(
        effect_id="eff",
        expected_run_version=1,
        lease_generation=1,
        cycle_number=1,
        bound_head_sha="a" * 40,
    )
    with pytest.raises(PrReviewEngineError, match="cannot enter via apply_event"):
        engine.apply_event(
            EventSubmission(
                submission_id="bad",
                run_id=prepared.run_id,
                expected_version=1,
                event=EffectBlocked(
                    occurred_at=engine._clock.now(),  # noqa: SLF001
                    token=token,
                    reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
                    safe_action=SafeAction(
                        kind=SafeActionKind.INSPECT_ARTIFACTS,
                        condition="x",
                    ),
                    safe_summary="blocked",
                ),
            )
        )


def test_resume_allowed_via_apply_event(engine: PrReviewEngine, prepared: PreparedState) -> None:
    engine.create_run(prepared.run_id, prepared)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-resume",
            run_id=prepared.run_id,
            expected_version=1,
            event=ResumeRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition.value == "rejected"


def test_application_modules_forbid_legacy_imports() -> None:
    roots = [
        V2_ROOT / "application",
        V2_ROOT / "infrastructure",
        V2_ROOT / "workers",
    ]
    for root in roots:
        for path in _iter_py_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for forbidden in FORBIDDEN_LEGACY:
                        assert not module.startswith(forbidden), f"{path} imports {module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in FORBIDDEN_LEGACY:
                            assert not alias.name.startswith(forbidden)


def test_domain_still_has_no_sqlite_imports() -> None:
    domain_root = V2_ROOT / "domain"
    for path in _iter_py_files(domain_root):
        text = path.read_text(encoding="utf-8")
        assert "sqlite3" not in text
        assert "ai_dev_loop.pr_review_v2.infrastructure" not in text
