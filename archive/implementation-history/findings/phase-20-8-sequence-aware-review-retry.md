# Phase 20.8 Findings — Sequence-Aware Manual Review Retry

## Summary

Phase 20.8 wires the existing `ai_dev_loop scheduler review retry <run-id>` command into blocked multi-phase sequences with durable replacement intents, fenced pending successors, sequence CAS adoption, and idempotent replay that survives later successor and sequence lifecycle changes. Correction passes fixed blocked-sequence abort lineage validation, abort-coordinated replacement cancellation, concurrency-test isolation, and loser retry convergence after the winning successor has already advanced past pending adoption.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry_concurrency.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry_corrections.py \
  tests/unit/scheduler/test_phase20_8_sequence_blocked_abort.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_phase20_3_handoff_recovery.py \
  tests/unit/scheduler/test_phase20_4_sequence_abort.py \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/integration/test_phase20_8_sequence_review_retry.py \
  tests/integration/test_phase20_1_reviewer_retry.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration

uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
```

## Validation Results

- Phase 20.8 + plan regression scheduler set: **82 passed**
- Full `tests/unit/scheduler` + `tests/integration` suite (single process): **715 passed, 2 skipped** in 1258.03s
- `mypy src`: **pass**
- `ruff check`, `ruff format --check`: **pass**

## Implementation Notes

- **Migration 11:** `cancelled_at` on `scheduler_sequence_execution_replacements` for abort-coordinated cancellation of outstanding intents.
- **Publication replay:** `authenticate_durable_sequence_replacement_publication` keys off `published_at`, recovery-successor row, successor run existence, and historical lineage attempt — not the live sequence leaf or successor `awaiting_codex_review` state. Replay returns the successor's actual state and safe action without new events.
- **Loser convergence:** `complete_sequence_review_recovery` reauthenticates publication under an immediate transaction before materialization; `_materialize_pending_successor` and `_adopt_sequence_recovery_successor` reauthenticate before rejecting successors that have left pending adoption, returning `changed=False` when publication is already durable. Genuine orphan, cancellation, identity, and integrity contradictions remain fail-closed.
- **Evidence chain:** Bootstrap and inherited reviewer binding artifacts authenticate against `evidence_run_id`; the blocked leaf's failed Codex attempt authenticates against `blocked_run_id` / its artifact root.
- **Artifacts:** `publish_or_verify_bytes` atomically publishes or verifies identical content for concurrent recovery materialization.
- **Blocked abort:** `SequenceAbortService._abort_blocked` transitions `BlockedSequenceState` → `AbortedSequenceState` in one step with preserved blocked leaf lineage; outstanding replacement intents are cancelled.
- **Concurrency tests:** Slow-thread materialization gate with `patch.object` installed outside workers; fast thread publishes and ticks successor into `waiting_codex_capacity` or `completed` before the slow caller resumes; both retries converge with one `changed=True` and one `changed=False`.

## Phase Boundary Confirmation

- No Phase 20.6 paths, new public recovery commands, real agents, timers, or external Git actions were introduced.
