"""Phase 16.6 privacy and isolation checks for write/reconcile surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.test_phase16_6_write_units import (
    FakeReadTransport,
    FakeWriteTransport,
    _gateway,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import html_comment_marker
from ai_dev_loop.pr_review_v2.domain.common import build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhApiTransport
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport


def test_trigger_evidence_artifact_has_no_raw_body(tmp_path: Path):
    from datetime import UTC, datetime

    marker = build_opaque_trigger_marker(run_id="run-1", cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    secret_body = f"SECRET_PATCH_CONTENT\n{needle}"
    write = FakeWriteTransport(
        issue_comment_created={
            "id": "IC_9",
            "body": secret_body,
            "createdAt": "2026-07-21T12:00:00Z",
        }
    )
    gateway = _gateway(tmp_path, write, FakeReadTransport(comment_pages=[[]]))
    success = gateway.request_review(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC), authorize=lambda: None
    )
    ref = success.outcome.evidence.comment_ref
    # Find the evidence file under the hashed run root.
    matches = list(tmp_path.rglob(ref.relative_path.split("/")[-1]))
    assert matches
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    dumped = json.dumps(payload)
    assert "SECRET_PATCH_CONTENT" not in dumped
    assert "@codex review" not in dumped or "body" not in payload
    assert "body" not in payload
    assert payload["marker"] == marker


def test_write_transports_expose_no_generic_escape_hatches():
    for cls in (GhWriteTransport, GitWriteTransport, GhApiTransport):
        public = {
            name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name))
        }
        assert "graphql" not in public
        assert "rest_get" not in public
        assert "rest_write" not in public


def test_force_flags_rejected_by_contract():
    import pytest

    from ai_dev_loop.pr_review_v2.application.write_contracts import assert_no_force_argv

    with pytest.raises(ValueError):
        assert_no_force_argv(["git", "push", "--force", "origin", "HEAD:refs/heads/feature"])
    with pytest.raises(ValueError):
        assert_no_force_argv(["git", "push", "--force-with-lease", "origin", "HEAD:feature"])
