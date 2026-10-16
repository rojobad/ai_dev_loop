"""Test-only concurrent ledger writer for the owned dashboard browser fixture."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from tests.integration.test_phase26_1_dashboard_queries import (  # noqa: E402
    _insert_attempt,
    _insert_run,
)

from ai_dev_loop.scheduler.infrastructure.sqlite_store import (  # noqa: E402
    SqliteSchedulerStore,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    db_path = args.state_root / "state" / "ai_dev_loop" / "engine.sqlite3"
    store = SqliteSchedulerStore(db_path)
    submitted = datetime(2026, 1, 3, 13, 30, tzinfo=UTC)
    event_id = _insert_run(
        store,
        "concurrent-refresh-run",
        project_name="map-project",
        submitted_at=submitted,
    )
    _insert_attempt(
        store,
        run_id="concurrent-refresh-run",
        event_id=event_id,
        suffix="execution",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=submitted + timedelta(minutes=1),
        completed=submitted + timedelta(minutes=11),
    )


if __name__ == "__main__":
    main()
