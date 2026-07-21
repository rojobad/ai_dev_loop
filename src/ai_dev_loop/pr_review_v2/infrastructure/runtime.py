"""Clocks, IDs, UTC encoding, and fault hooks for the durable engine."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from datetime import UTC, datetime

from ai_dev_loop.pr_review_v2.domain.common import NonEmptyId, coerce_utc_instant
from ai_dev_loop.redaction import redact_text


def encode_utc_instant(value: datetime | str) -> str:
    """Encode an instant as fixed-width microsecond UTC text for SQL ordering."""

    instant = coerce_utc_instant(value)
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_instant(value: str) -> datetime:
    return coerce_utc_instant(value)


def payload_sha256(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def safe_diagnostic(text: str) -> str:
    return redact_text(text.strip())


def dispatch_id_for(source_event_id: str, effect_ordinal: int) -> str:
    return f"dispatch:{source_event_id}:{effect_ordinal}"


def timer_id_for(source_event_id: str) -> str:
    return f"timer:retry_due:{source_event_id}"


def recovery_submission_id(
    *,
    run_id: str,
    dispatch_id: str,
    attempt: int,
    expired_generation: int,
    kind: str,
) -> str:
    return f"recovery:{kind}:{run_id}:{dispatch_id}:attempt-{attempt}:gen-{expired_generation}"


def timer_event_id(timer_id: str) -> str:
    return f"event:{timer_id}:retry_due"


def completion_submission_id(
    *,
    run_id: str,
    dispatch_id: str,
    claim_id: str,
    lease_generation: int,
    result_key: str = "primary",
) -> str:
    """Restart-safe completion identity bound to a persisted claim generation.

    ``lease_generation`` is required so a recovered/reclaimed dispatch under a new
    lease does not collide with a late completion from an earlier generation when
    claim-id factories reset after restart. ``result_key`` defaults to ``primary``;
    a corrected typed result for the same claim must use a distinct key.
    """

    return f"complete:{run_id}:{dispatch_id}:{claim_id}:gen-{lease_generation}:{result_key}"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class SequenceIdFactory:
    """Deterministic sequential IDs for tests and injectable production use."""

    def __init__(self, *, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        n = next(self._counter)
        return f"{prefix}:{self._prefix}-{n}"


class DeterministicIdFactory:
    """Stable IDs derived from an explicit seed plus monotonic counter."""

    def __init__(self, seed: str = "seed") -> None:
        self._seed = seed
        self._counter = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        n = next(self._counter)
        digest = hashlib.sha256(f"{self._seed}:{prefix}:{n}".encode()).hexdigest()[:16]
        return f"{prefix}:{digest}"


class FaultInjector:
    """Named checkpoint fault hook for crash/atomicity tests."""

    def __init__(self) -> None:
        self._hooks: dict[str, Callable[[], None]] = {}
        self._call_counts: dict[str, int] = {}

    def set(self, checkpoint: str, hook: Callable[[], None]) -> None:
        self._hooks[checkpoint] = hook

    def clear(self, checkpoint: str | None = None) -> None:
        if checkpoint is None:
            self._hooks.clear()
        else:
            self._hooks.pop(checkpoint, None)

    def maybe_raise(self, checkpoint: str) -> None:
        self._call_counts[checkpoint] = self._call_counts.get(checkpoint, 0) + 1
        hook = self._hooks.get(checkpoint)
        if hook is not None:
            hook()

    def call_count(self, checkpoint: str) -> int:
        return self._call_counts.get(checkpoint, 0)


def validate_owner_id(owner_id: str) -> NonEmptyId:
    """Validate opaque lease owner IDs through the domain NonEmptyId constraint."""

    from pydantic import TypeAdapter

    return TypeAdapter(NonEmptyId).validate_python(owner_id)
