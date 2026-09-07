"""Scheduler journal event models."""

from __future__ import annotations

from pydantic import Field, TypeAdapter, field_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, Sha256Hex

SUBMITTED_EVENT_KIND = "run_submitted"


class RunSubmittedEvent(DomainModel):
    kind: str = Field(default=SUBMITTED_EVENT_KIND)
    run_id: str
    idempotency_key: Sha256Hex
    worktree_key: Sha256Hex
    reused_existing: bool

    @field_validator("kind")
    @classmethod
    def kind_is_run_submitted(cls, value: str) -> str:
        if value != SUBMITTED_EVENT_KIND:
            raise ValueError("kind must be run_submitted")
        return value


SchedulerEvent = RunSubmittedEvent

SCHEDULER_EVENT_ADAPTER: TypeAdapter[SchedulerEvent] = TypeAdapter(SchedulerEvent)


def parse_scheduler_event(payload: object) -> SchedulerEvent:
    return SCHEDULER_EVENT_ADAPTER.validate_python(payload)
