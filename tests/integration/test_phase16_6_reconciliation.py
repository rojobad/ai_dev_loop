"""Phase 16.6 reconciliation proof matrix integration tests."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.test_phase16_6_write_units import (
    FakeReadTransport,
    FakeWriteTransport,
    _gateway,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import WriteProofKind, html_comment_marker
from ai_dev_loop.pr_review_v2.domain.common import build_opaque_trigger_marker


def test_find_review_marker_applied_absent_duplicate(tmp_path):
    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    gateway = _gateway(tmp_path, FakeWriteTransport(), FakeReadTransport(comment_pages=[[]]))
    now = datetime.now(tz=UTC)
    absent = gateway.reconcile_request_review(effect, run_id=H.RUN_ID, now=now)
    assert absent.proof is WriteProofKind.PROVEN_NOT_APPLIED

    one = FakeReadTransport(
        comment_pages=[
            [
                {
                    "id": "1",
                    "body": f"@codex review\n{needle}",
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "bot"},
                }
            ]
        ]
    )
    applied = _gateway(tmp_path, FakeWriteTransport(), one).reconcile_request_review(
        effect, run_id=H.RUN_ID, now=now
    )
    assert applied.proof is WriteProofKind.APPLIED

    dup = FakeReadTransport(
        comment_pages=[
            [
                {
                    "id": "1",
                    "body": f"{needle}",
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "a"},
                },
                {
                    "id": "2",
                    "body": f"{needle}",
                    "createdAt": "2026-07-21T12:01:00Z",
                    "author": {"login": "b"},
                },
            ]
        ]
    )
    unresolved = _gateway(tmp_path, FakeWriteTransport(), dup).reconcile_request_review(
        effect, run_id=H.RUN_ID, now=now
    )
    assert unresolved.proof is WriteProofKind.UNRESOLVED


def test_find_thread_resolved_matrix(tmp_path):
    effect = H.resolve_thread_effect()
    now = datetime.now(tz=UTC)
    unresolved_thread = FakeWriteTransport(thread_resolved=False)
    proof = _gateway(
        tmp_path, unresolved_thread, FakeReadTransport(comment_pages=[])
    ).reconcile_resolve_thread(effect, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED
    resolved = FakeWriteTransport(thread_resolved=True)
    proof = _gateway(
        tmp_path, resolved, FakeReadTransport(comment_pages=[])
    ).reconcile_resolve_thread(effect, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.APPLIED
