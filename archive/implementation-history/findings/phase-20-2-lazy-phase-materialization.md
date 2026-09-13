# Phase 20.2 Findings — Lazy First-Phase Materialization and Authorization

## Implemented scope

- Submitted-context schema version 4 with typed `SequenceRunBinding` (sequence ID,
  ordinal, total phases, frozen entry hash). Historical context versions 1–3 remain
  readable unchanged.
- `ActiveSequenceState` with immutable definition, `started_at`, current ordinal/run
  projection, and materialized-entry records.
- `SequenceRunMaterializer` copies verified sequence-entry bytes into the deterministic
  run artifact layout with write-or-verify semantics, orphan-tree validation, and no
  symlinks.
- `SequenceStartService` and `ai_dev_loop scheduler sequence start <sequence-id>` with
  stable text/JSON output, idempotent replay, reservation-conflict fail-closed behavior,
  and process-free/Git-free execution.
- Ledger transaction `insert_materialized_sequence_run`: authorized run row, ordered
  `run_submitted`/`run_authorized` events, active reservation owned by ordinal 1, and
  sequence CAS transition `prepared -> active`.
- Updated `scheduler sequence status` and run status/list projections expose safe sequence
  provenance without prompts, plans, configs, or full agent IDs.
- Honest Phase 20.2 checkpoint-boundary messaging only for `completed` and
  `completed_with_residual_risk` when later phases remain unmaterialized; blocked,
  aborted, and `max_iterations_reached` runs retain their ordinary safe actions.
- Concurrent `write_text_or_verify` accepts identically published bytes instead of failing
  on a publish race between existence check and write.
- Run artifact materialization uses a process-safe `materialization-locks/` flock with
  verifiable ownership metadata. The critical section now spans orphan validation, frozen
  artifact publication, and the final ledger commit/rollback. Waiting sequence starts
  block on the lock, recheck durable sequence state after the preceding owner finishes,
  and replay idempotently without orphan scanning when the sequence is already active.

## Explicit non-goals preserved

- No checkpoint commits, reservation handoff, or materialization of phase 2+.
- No sequence list/history, abort surface, automatic retry, or end-to-end multi-phase
  execution claims.
- Standalone `scheduler submit` / `start` behavior unchanged except for normal reservation
  conflicts with an active sequence run.

## Validation commands

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/unit/scheduler/test_start.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_sqlite_store.py \
  tests/integration/test_phase20_2_sequence_start.py \
  tests/integration/test_phase17_1_submit.py \
  tests/integration/test_phase17_2_tick_control.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Focused Phase 20.2 regressions (138 tests), the scheduler unit/integration suites,
`ruff`, `mypy`, and `mkdocs build --strict` passed after extending the materialization
lock through ledger commit.

## Residual risks

- Filesystem run artifacts and SQLite sequence/run rows are not one atomic transaction;
  replay relies on deterministic run IDs, hash verification, and idempotent sequence start.
- A successful terminal phase-1 run remains staged without automatic checkpoint commit or
  phase-2 materialization until Phase 20.3.
- Orphaned run artifacts for a planned ID block materialization when bytes disagree with
  the frozen entry or when unexpected files are present; matching orphans are adopted via
  write-or-verify without deleting diagnostic evidence.
- Interrupted artifact publication before ledger insertion remains recoverable through
  deterministic replay; CAS loss rolls back the ledger transaction and leaves the sequence
  prepared.

## Next phase boundary

Phase 20.3 owns reviewed checkpoint commits, reservation handoff, and lazy materialization
of the next sequence phase after an accepted intermediate outcome.
