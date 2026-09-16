"""Review-seed workflow tests for Phase 20.6."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.codex_contract import REVIEW_SEED_OPERATIONAL_ENVELOPE


def test_review_seed_envelope_states_no_trusted_final_response() -> None:
    assert "No trusted Cursor final response" in REVIEW_SEED_OPERATIONAL_ENVELOPE
