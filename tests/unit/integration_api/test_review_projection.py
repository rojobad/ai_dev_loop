"""Unit tests for Codex review integration projections and evidence schema."""

from __future__ import annotations

import json

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.integration_api.review_evidence_auth import AuthenticatedReviewEvidence
from ai_dev_loop.integration_api.review_projection import build_review_detail_data
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1


def _attempt_row() -> object:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE t (
            attempt_id TEXT, run_id TEXT, iteration INTEGER, phase_attempt INTEGER,
            status TEXT, created_at TEXT, launch_requested_at TEXT, completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO t VALUES (
            'att-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 'run-proj', 1, 1,
            'completed', '2026-09-20T12:00:00Z', NULL, '2026-09-20T12:05:00Z'
        )
        """
    )
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM t").fetchone()


def _auth(**overrides: object) -> AuthenticatedReviewEvidence:
    base = {
        "invocation": {
            "review_model": "gpt-5.6-sol",
            "review_reasoning_effort": "high",
        },
        "outcome": None,
        "validated": None,
        "result_state": "not_produced",
        "prompt_state": "not_recorded",
        "prompt_rel": None,
        "prompt_sha256": None,
        "prompt_size_bytes": None,
        "reviewer_session_id": None,
    }
    base.update(overrides)
    return AuthenticatedReviewEvidence(**base)  # type: ignore[arg-type]


def test_c05_historical_attempt_without_prompt_is_not_recorded() -> None:
    row = _attempt_row()
    detail = build_review_detail_data(
        run_id="run-proj",
        attempt_row=row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        auth=_auth(),
    )
    assert detail.content.prompt.available is False
    assert detail.content.prompt.reason == "not_recorded"


def test_c05_invalid_json_response_still_allows_response_bytes() -> None:
    row = _attempt_row()
    detail = build_review_detail_data(
        run_id="run-proj",
        attempt_row=row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        auth=_auth(
            outcome={
                "review_result_path": "codex/reviews/01.x.json",
                "review_result_sha256": "a" * 64,
            },
            result_state="invalid",
        ),
    )
    assert detail.result_state == "invalid"
    assert detail.response is None
    assert detail.findings_count is None
    assert detail.content.response.available is True
    assert detail.content.review_markdown.reason == "invalid_result"


def test_c05_data_integrity_prompt_state() -> None:
    row = _attempt_row()
    detail = build_review_detail_data(
        run_id="run-proj",
        attempt_row=row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        auth=_auth(prompt_state="data_integrity"),
    )
    assert detail.content.prompt.reason == "data_integrity"


def test_c06_schema_rejects_uppercase_hash() -> None:
    schema = json.loads(schema_path("scheduler-review-prompt-evidence-v1.json").read_text())
    sample = {
        "schema_version": 1,
        "run_id": "run",
        "attempt_id": "att",
        "review_iteration": 1,
        "prompt_path": "codex/reviews/01.att.prompt.txt",
        "prompt_sha256": "A" * 64,
        "prompt_size_bytes": 3,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(sample, schema)
    with pytest.raises(ValidationError):
        SchedulerReviewPromptEvidenceV1.model_validate(sample)


def test_c06_schema_requires_schema_version() -> None:
    with pytest.raises(ValidationError):
        SchedulerReviewPromptEvidenceV1.model_validate(
            {
                "run_id": "run",
                "attempt_id": "att",
                "review_iteration": 1,
                "prompt_path": "codex/reviews/01.att.prompt.txt",
                "prompt_sha256": "a" * 64,
                "prompt_size_bytes": 3,
            }
        )
