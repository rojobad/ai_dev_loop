"""Write and read Cursor per-attempt child output observation sidecars."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.scheduler.domain.cursor_contract import cursor_attempt_output_observation_rel
from ai_dev_loop.scheduler.domain.output_observation import (
    SchedulerOutputObservationV1,
    SchedulerOutputStreamObservationV1,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.state import atomic_write_json


class OutputObservationError(Exception):
    """Output observation artifact failed validation."""


def write_cursor_output_observation(
    run_root: Path,
    *,
    run_id: str,
    attempt_id: str,
    iteration: int,
    stdout_stored_bytes: int,
    stderr_stored_bytes: int,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> str:
    rel = cursor_attempt_output_observation_rel(iteration, attempt_id)
    payload = SchedulerOutputObservationV1(
        schema_version=1,
        run_id=run_id,
        attempt_id=attempt_id,
        iteration=iteration,
        stdout=SchedulerOutputStreamObservationV1(
            stored_bytes=stdout_stored_bytes,
            truncated=stdout_truncated,
        ),
        stderr=SchedulerOutputStreamObservationV1(
            stored_bytes=stderr_stored_bytes,
            truncated=stderr_truncated,
        ),
    )
    atomic_write_json(run_root / rel, payload.model_dump(), sensitive=True)
    return rel


def load_cursor_output_observation(
    run_root: Path,
    *,
    run_id: str,
    attempt_id: str,
    iteration: int,
) -> SchedulerOutputObservationV1 | None:
    rel = cursor_attempt_output_observation_rel(iteration, attempt_id)
    try:
        path = resolve_run_relative_path(run_root, rel)
    except ValueError:
        raise OutputObservationError("output observation path invalid") from None
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        observation = SchedulerOutputObservationV1.model_validate(payload)
    except (OSError, json.JSONDecodeError, PydanticValidationError) as exc:
        raise OutputObservationError("output observation invalid") from exc
    if observation.run_id != run_id or observation.attempt_id != attempt_id:
        raise OutputObservationError("output observation identity mismatch")
    if observation.iteration != iteration:
        raise OutputObservationError("output observation iteration mismatch")
    return observation
