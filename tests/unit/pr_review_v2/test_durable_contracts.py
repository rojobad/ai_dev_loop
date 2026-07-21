"""Unit tests for Phase 16.4 durable contracts, UTC encoding, and IDs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    EventDisposition,
    EventSubmission,
    NextActionCategory,
    PrReviewEngineError,
    PrReviewEngineErrorKind,
)
from ai_dev_loop.pr_review_v2.domain import StartRequested
from ai_dev_loop.pr_review_v2.infrastructure.runtime import (
    DeterministicIdFactory,
    SequenceIdFactory,
    dispatch_id_for,
    encode_utc_instant,
    parse_utc_instant,
    payload_sha256,
    recovery_submission_id,
    timer_event_id,
    timer_id_for,
)


def test_dispatch_status_vocabulary() -> None:
    assert {s.value for s in DispatchStatus} == {
        "pending",
        "claimed",
        "succeeded",
        "retry_wait",
        "uncertain",
        "blocked",
        "cancelled",
        "superseded",
    }
    assert "result_rejected" not in {s.value for s in DispatchStatus}


def test_encode_utc_round_trip_and_ordering() -> None:
    early = datetime(2026, 7, 20, 12, 0, 0, 1, tzinfo=UTC)
    late = datetime(2026, 7, 20, 12, 0, 0, 2, tzinfo=UTC)
    early_text = encode_utc_instant(early)
    late_text = encode_utc_instant(late)
    assert early_text < late_text
    assert early_text.endswith("000001Z")
    assert parse_utc_instant(early_text) == early
    offset = encode_utc_instant("2026-07-20T08:00:00.000000-04:00")
    assert offset == "2026-07-20T12:00:00.000000Z"


def test_reject_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        encode_utc_instant(datetime(2026, 7, 20, 12, 0, 0))


def test_deterministic_ids() -> None:
    a = DeterministicIdFactory("seed")
    b = DeterministicIdFactory("seed")
    assert a.new_id("claim") == b.new_id("claim")
    seq = SequenceIdFactory(prefix="x")
    assert seq.new_id("claim") == "claim:x-1"
    assert dispatch_id_for("evt-1", 0) == "dispatch:evt-1:0"
    assert timer_id_for("evt-1") == "timer:retry_due:evt-1"
    assert timer_event_id("timer:retry_due:evt-1") == "event:timer:retry_due:evt-1:retry_due"
    assert "recovery:mutating" in recovery_submission_id(
        run_id="r1",
        dispatch_id="d1",
        attempt=2,
        expired_generation=3,
        kind="mutating-uncertain",
    )


def test_event_submission_rejects_blank_ids() -> None:
    with pytest.raises(ValidationError):
        EventSubmission(
            submission_id="",
            run_id="run-1",
            expected_version=1,
            event=StartRequested(occurred_at=datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)),
        )


def test_engine_error_kinds_are_safe() -> None:
    err = PrReviewEngineError(PrReviewEngineErrorKind.BUSY, "database is busy")
    assert err.kind is PrReviewEngineErrorKind.BUSY
    assert "busy" in str(err)


def test_payload_hash_stable() -> None:
    assert payload_sha256('{"a":1}') == payload_sha256('{"a":1}')
    assert payload_sha256('{"a":1}') != payload_sha256('{"a":2}')


def test_disposition_and_next_action_enums() -> None:
    assert EventDisposition.DUPLICATE.value == "duplicate"
    assert NextActionCategory.EXECUTE_EFFECT.value == "execute_effect"


def test_lease_ttl_positive(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
    from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

    store = SqlitePrReviewStore(tmp_path / "x.sqlite3")
    with pytest.raises(PrReviewEngineError, match="lease_ttl"):
        PrReviewEngine(store, lease_ttl=timedelta(seconds=0))
