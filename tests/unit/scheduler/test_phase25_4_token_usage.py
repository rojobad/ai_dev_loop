"""Phase 25.4 strict normalization and immutable publication contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ai_dev_loop.scheduler.application.token_usage_capture import (
    capture_codex_usage_safely,
    capture_cursor_usage_evidence_safely,
    capture_cursor_usage_safely,
)
from ai_dev_loop.scheduler.domain.token_usage import (
    normalize_codex_usage,
    normalize_cursor_usage,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.state import atomic_write_text


def test_u01_cursor_and_codex_normalization_uses_independent_expected_values() -> None:
    cursor_native, cursor = normalize_cursor_usage(
        {
            "input_tokens": 4820,
            "output_tokens": 139,
            "cache_read_tokens": 2560,
            "cache_write_tokens": 0,
            "total_tokens": 7519,
            "reasoning_tokens": 118,
        }
    )
    assert cursor.total_tokens == 7519
    assert cursor.reasoning_output == 118
    assert cursor.output_total == 139
    assert cursor.input_total == 7380
    assert cursor_native.total_tokens == 7519

    codex_native, codex = normalize_codex_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 10,
            "reasoning_output_tokens": 4,
        }
    )
    assert codex.total_tokens == 110
    assert codex.input_uncached == 40
    assert codex.cache_read == 60
    assert codex.cache_write is None
    assert codex_native.cached_input_tokens == 60


@pytest.mark.parametrize(
    "payload",
    [
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 1.5, "output_tokens": 1},
        {"input_tokens": "1", "output_tokens": 1},
        {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 1},
    ],
)
def test_u01_codex_rejects_non_strict_or_contradictory_counters(payload: object) -> None:
    with pytest.raises((ValidationError, ValueError)):
        normalize_codex_usage(payload)


def test_u01_publication_is_idempotent_partial_aware_and_conflict_safe(tmp_path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-usage"
    run_root = store.run_root(run_id)
    source = "cursor/iterations/01/attempt/sdk-metadata.json"
    atomic_write_text(run_root / source, "{}", sensitive=True)
    source_sha = __import__("hashlib").sha256(b"{}").hexdigest()
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": None,
    }
    first = capture_cursor_usage_safely(
        run_root=run_root,
        run_id=run_id,
        stage="initial",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="attempt",
        source_dispatch_id="dispatch",
        native_run_id="native",
        source_artifact_path=source,
        source_artifact_sha256=source_sha,
        usage=usage,
        capture_complete=False,
    )
    replay = capture_cursor_usage_safely(
        run_root=run_root,
        run_id=run_id,
        stage="initial",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="reconciliation-attempt",
        source_dispatch_id="dispatch",
        native_run_id="native",
        source_artifact_path=source,
        source_artifact_sha256=source_sha,
        usage=usage,
        capture_complete=False,
    )
    assert len(first.bindings) == len(replay.bindings) == 1
    assert first.bindings[0] == replay.bindings[0]
    observation = json.loads((run_root / first.bindings[0]["path"]).read_text())
    assert observation["availability"] == "partial"
    assert observation["normalized"]["total_tokens"] == 0

    conflicting = capture_cursor_usage_safely(
        run_root=run_root,
        run_id=run_id,
        stage="initial",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="reconciliation-attempt",
        source_dispatch_id="dispatch",
        native_run_id="native",
        source_artifact_path=source,
        source_artifact_sha256=source_sha,
        usage={**usage, "output_tokens": 1, "total_tokens": 1},
        capture_complete=False,
    )
    assert conflicting.bindings == ()
    assert conflicting.diagnostics == ("conflicting_usage_observation",)


def test_u01_multiple_codex_turns_keep_ordinals_and_malformed_usage_is_diagnostic(
    tmp_path,
) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-codex"
    run_root = store.run_root(run_id)
    source = "codex/events/01.attempt.jsonl"
    events = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 10,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": True, "output_tokens": 2},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 7},
                }
            ),
        ]
    )
    atomic_write_text(run_root / source, events, sensitive=True)
    digest = __import__("hashlib").sha256(events.encode()).hexdigest()
    result = capture_codex_usage_safely(
        run_root=run_root,
        run_id=run_id,
        role="reviewer",
        stage="review",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="attempt",
        source_dispatch_id="dispatch",
        source_artifact_path=source,
        source_artifact_sha256=digest,
        capture_complete=True,
    )
    assert len(result.bindings) == 2
    assert result.diagnostics == ("invalid_codex_usage_turn_2",)
    assert result.contribution_complete is False
    for binding in result.bindings:
        observation = json.loads((run_root / binding["path"]).read_text(encoding="utf-8"))
        assert observation["availability"] == "partial"


def test_f04_interrupted_codex_stream_retains_reported_lower_bound(tmp_path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-interrupted-codex"
    run_root = store.run_root(run_id)
    source = "codex/events/01.attempt.jsonl"
    events = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 10,
            },
        }
    )
    atomic_write_text(run_root / source, events, sensitive=True)
    digest = __import__("hashlib").sha256(events.encode()).hexdigest()

    result = capture_codex_usage_safely(
        run_root=run_root,
        run_id=run_id,
        role="implementer",
        stage="initial",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="attempt",
        source_dispatch_id="dispatch",
        source_artifact_path=source,
        source_artifact_sha256=digest,
        capture_complete=False,
    )

    assert result.observed_contributions == 1
    assert result.contribution_complete is False
    observation = json.loads((run_root / result.bindings[0]["path"]).read_text())
    assert observation["availability"] == "partial"
    assert observation["normalized"]["total_tokens"] == 110


def test_f05_malformed_cursor_source_keeps_valid_lower_bound_partial(tmp_path) -> None:
    store = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-cursor-mixed-evidence"
    run_root = store.run_root(run_id)
    source = "cursor/iterations/01/attempt/sdk-metadata.json"
    atomic_write_text(run_root / source, "{}", sensitive=True)
    source_sha = __import__("hashlib").sha256(b"{}").hexdigest()
    valid = {
        "input_tokens": 4820,
        "output_tokens": 139,
        "cache_read_tokens": 2560,
        "cache_write_tokens": 0,
        "total_tokens": 7519,
        "reasoning_tokens": 118,
    }

    result = capture_cursor_usage_evidence_safely(
        run_root=run_root,
        run_id=run_id,
        stage="initial",
        scheduler_attempt_id="attempt",
        publisher_attempt_id="attempt",
        source_dispatch_id="dispatch",
        native_run_id="native",
        source_artifact_path=source,
        source_artifact_sha256=source_sha,
        cumulative_usages=({**valid, "input_tokens": True}, valid),
        capture_complete=True,
    )

    assert result.observed_contributions == 1
    assert result.contribution_complete is False
    assert result.diagnostics == ("invalid_cursor_usage_evidence",)
    observation = json.loads((run_root / result.bindings[0]["path"]).read_text())
    assert observation["availability"] == "partial"
    assert observation["normalized"]["total_tokens"] == 7519
