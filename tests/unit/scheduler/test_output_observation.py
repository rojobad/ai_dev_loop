"""Tests for Cursor output observation sidecar (Phase 21.5)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.output_observation import (
    OutputObservationError,
    load_cursor_output_observation,
    write_cursor_output_observation,
)
from ai_dev_loop.scheduler.domain.output_observation import SchedulerOutputObservationV1


def test_c05_output_observation_schema_and_writer(tmp_path: Path) -> None:
    observation_rel = write_cursor_output_observation(
        tmp_path,
        run_id="run-abc",
        attempt_id="att-1",
        iteration=1,
        stdout_stored_bytes=12,
        stderr_stored_bytes=3,
        stdout_truncated=False,
        stderr_truncated=True,
    )
    assert observation_rel.endswith("output-observation.json")
    loaded = load_cursor_output_observation(
        tmp_path,
        run_id="run-abc",
        attempt_id="att-1",
        iteration=1,
    )
    assert loaded is not None
    assert loaded.stdout.stored_bytes == 12
    assert loaded.stderr.truncated is True
    schema = json.loads(schema_path("scheduler-output-observation-v1.json").read_text())
    jsonschema.validate(loaded.model_dump(), schema)


def test_c05_missing_sidecar_returns_none(tmp_path: Path) -> None:
    assert (
        load_cursor_output_observation(
            tmp_path,
            run_id="run-abc",
            attempt_id="att-1",
            iteration=1,
        )
        is None
    )


def test_c05_identity_mismatch_fails(tmp_path: Path) -> None:
    write_cursor_output_observation(
        tmp_path,
        run_id="run-abc",
        attempt_id="att-1",
        iteration=1,
        stdout_stored_bytes=0,
        stderr_stored_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    with pytest.raises(OutputObservationError):
        load_cursor_output_observation(
            tmp_path,
            run_id="other-run",
            attempt_id="att-1",
            iteration=1,
        )


def test_c05_model_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        SchedulerOutputObservationV1.model_validate(
            {
                "schema_version": 1,
                "run_id": "run",
                "attempt_id": "att",
                "iteration": 1,
                "stdout": {"stored_bytes": "1", "truncated": False},
                "stderr": {"stored_bytes": 0, "truncated": False},
            }
        )
