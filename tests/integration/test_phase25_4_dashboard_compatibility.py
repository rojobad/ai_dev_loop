"""Dashboard activity remains independent from Phase 25.4 token observations."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.integration.test_phase26_1_dashboard_queries import (
    FIXED,
    _insert_attempt,
    _insert_run,
)

from ai_dev_loop.integration_api.dashboard_service import default_dashboard_read_service
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def test_u05_codex_roles_keep_dashboard_activity_semantics_and_reads_are_immutable(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    event = _insert_run(writer, "both-codex", provider="codex")
    _insert_attempt(
        writer,
        run_id="both-codex",
        event_id=event,
        suffix="implementation",
        component="codex_implementer",
        effect_kind=BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
        iteration=1,
        launch=FIXED,
        completed=FIXED + timedelta(minutes=2),
    )
    _insert_attempt(
        writer,
        run_id="both-codex",
        event_id=event,
        suffix="review",
        component="codex",
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        iteration=1,
        launch=FIXED + timedelta(minutes=2),
        completed=FIXED + timedelta(minutes=3),
    )
    before = db.read_bytes()
    service = default_dashboard_read_service(db_path=db)
    kwargs = {
        "from_value": "2026-01-01T00:00:00Z",
        "to_value": "2026-01-02T00:00:00Z",
        "time_zone": "UTC",
    }
    assert service.activity(metric="review-rounds", **kwargs).total == 1
    assert service.activity(metric="process-hours", **kwargs).total == 3 * 60
    runs = service.runs(offset=0, limit=100, **kwargs)
    assert [item.run_id for item in runs.items] == ["both-codex"]
    assert db.read_bytes() == before
