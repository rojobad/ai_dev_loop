"""Real loopback dashboard server over a deterministic temporary scheduler ledger."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from tests.integration.test_phase26_1_dashboard_queries import (  # noqa: E402
    FIXED,
    _fixture_ledger,
    _insert_attempt,
    _insert_run,
)

from ai_dev_loop.dashboard.adapters.http import (  # noqa: E402
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.server import DashboardServer  # noqa: E402
from ai_dev_loop.integration_api.models import (  # noqa: E402
    IntegrationCodexCapacityData,
    IntegrationCodexCapacityLimit,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore  # noqa: E402


class FixtureCapacityService:
    """Deterministic fake provider boundary; no model or account process is invoked."""

    def observe(self) -> IntegrationCodexCapacityData:
        return IntegrationCodexCapacityData(
            status="available",
            reason=None,
            limits=(
                IntegrationCodexCapacityLimit(
                    id="primary",
                    window="5-hour window",
                    usedPercent=36,
                    remainingPercent=64,
                    windowDurationMinutes=300,
                    resetsAt="2026-01-03T02:00:00Z",
                ),
                IntegrationCodexCapacityLimit(
                    id="weekly",
                    window="Weekly window",
                    usedPercent=73,
                    remainingPercent=27,
                    windowDurationMinutes=None,
                    resetsAt=None,
                ),
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()

    xdg_state = args.state_root / "state"
    db_path = _fixture_ledger(xdg_state)
    store = SqliteSchedulerStore(db_path)
    malicious_project = '<img src=x onerror="window.__dashboard_xss=1">'
    event_id = _insert_run(
        store,
        "literal-label-run",
        project_name=malicious_project,
        submitted_at=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
    )
    _insert_attempt(
        store,
        run_id="literal-label-run",
        event_id=event_id,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED + timedelta(hours=1, minutes=10),
        completed=FIXED + timedelta(hours=1, minutes=20),
    )

    artifact_root = xdg_state / "ai_dev_loop" / "artifacts"
    factories = replace(
        default_http_service_factories(db_path=db_path, artifact_root=artifact_root),
        capacity=lambda: FixtureCapacityService(),
    )
    dispatcher = DashboardHttpDispatcher(factories)
    server = DashboardServer(args.port, dispatcher=dispatcher)
    try:
        server.serve_forever()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
