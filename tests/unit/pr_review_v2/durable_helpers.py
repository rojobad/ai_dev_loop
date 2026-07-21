"""Shared fixtures for Phase 16.4 durable engine tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectCompletionToken,
    EffectSucceeded,
    PreparedState,
    PrReviewEffect,
    PrReviewEvent,
    PublicationTextPreparedOutcome,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import FaultInjector, SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64
T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    _now: datetime = field(default_factory=lambda: T0)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, delta: timedelta | float) -> datetime:
        if isinstance(delta, (int, float)):
            delta = timedelta(seconds=float(delta))
        self._now = self._now + delta
        return self._now


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[PrReviewEffect] = []
        self._responses: list[PrReviewEvent] = []

    def queue(self, event: PrReviewEvent) -> None:
        self._responses.append(event)

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> PrReviewEvent:
        self.calls.append(effect)
        if not self._responses:
            raise AssertionError("RecordingExecutor has no queued responses")
        return self._responses.pop(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ids() -> SequenceIdFactory:
    return SequenceIdFactory(prefix="test")


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    xdg = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    path = tmp_path / "engine.sqlite3"
    return path


@pytest.fixture
def store(db_path: Path) -> SqlitePrReviewStore:
    return SqlitePrReviewStore(db_path)


@pytest.fixture
def engine(
    store: SqlitePrReviewStore,
    clock: FakeClock,
    ids: SequenceIdFactory,
    faults: FaultInjector,
) -> PrReviewEngine:
    return PrReviewEngine(
        store,
        clock=clock,
        ids=ids,
        fault_hook=faults,
        lease_ttl=timedelta(seconds=30),
    )


@pytest.fixture
def prepared() -> PreparedState:
    origin = SourceRunOrigin(
        source_run_id="local-run-001",
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        head_branch="feature",
        base_branch="main",
        expected_head_sha=SHA_A,
        accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256=HASH_1),
        execution_context_ref=ArtifactRef(
            relative_path="artifacts/execution-context.json",
            sha256=HASH_2,
        ),
    )
    return PreparedState(
        run_id="run-1",
        origin=origin,
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=T0,
    )


def start_run(engine: PrReviewEngine, prepared: PreparedState) -> None:
    engine.create_run(prepared.run_id, prepared)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition.value == "accepted"


def publication_success(effect: PrReviewEffect, token: EffectCompletionToken, now: datetime):
    return EffectSucceeded(
        occurred_at=now,
        token=token,
        outcome=PublicationTextPreparedOutcome(
            publication_text_ref=ArtifactRef(
                relative_path="artifacts/publication.md",
                sha256=HASH_1,
            ),
            commit_message_ref=ArtifactRef(
                relative_path="artifacts/commit-message.txt",
                sha256=HASH_2,
            ),
        ),
    )
