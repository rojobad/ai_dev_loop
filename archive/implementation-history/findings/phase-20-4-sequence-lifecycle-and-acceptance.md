# Phase 20.4 Findings — Sequence Lifecycle, Abort, Observability, and Acceptance

## Implemented scope

- Sequence lifecycle states: `abort_pending`, `blocked`, `aborted`, and existing
  `awaiting_finalization`, with typed payloads and CAS transitions in
  `scheduler_sequences`.
- `SequenceReconcileService` reconciles materialized run `blocked` and sequence-aborted
  outcomes into sequence-level `blocked` or `aborted` without successor
  materialization; `max_iterations_reached` leaves the sequence `active` so review-budget
  extension can recover the same materialized run without advancing phases. Active
  sequences with an aborted materialized run defer `sequence_blocked` reconciliation and
  reservation release while process/checkpoint holds remain. Sequence abort finalization
  requires `AbortedState`, cleared abort holds, and run cleanup; reservation release
  verifies ownership. `reconcile_pending_sequences` runs independently of tick-eligible
  run selection.
- `SequenceAbortService` and `ai_dev_loop scheduler sequence abort <sequence-id>`:
  prepared abort (cancel all planned entries, no runs), active abort (durable
  `abort_pending` then delegate to existing run abort), idempotent replay, refusal on
  `awaiting_finalization` and `blocked`. Durable `abort_pending` replays the injected
  run-abort service after restart; ticks advance pending aborts before run work.
- Checkpoint/handoff fencing honors `abort_pending` and blocks successor creation.
  `_try_record_applied_checkpoint_reconciliation_only` verifies the live commit object
  (tree/parent/message/identity/HEAD) before recording results, resolves holds, and
  completes recorded abort without new Git mutations or successor materialization.
- Direct run abort cleanup retains reservations while an active/abort-pending sequence
  still governs the run or checkpoint/process holds remain (`sequence_reservation.py`).
- Enriched `scheduler sequence status` with per-entry materialization/acceptance/
  residual-risk/checkpoint prefixes, aggregate counts, and safe next actions.
- Deterministic `reports/completion-v1.json` is published only after
  `awaiting_finalization` and `finalized_at` are durably committed; filesystem writes
  and `completion_report_sha256` ledger recording happen via replayable
  `reconcile_completion_report_publication` outside the finalization transaction (tick
  reconciles at start and end of each visit). Checkpoint report hashes bind to verified
  commit objects in the repository; incomplete or conflicting evidence fails closed.
- Shared lifecycle invariant validation for `abort_pending`, `blocked`, and `aborted`
  projections is enforced on sequence CAS.
- Tick integration reconciles pending sequences before and after run visits and replays
  pending sequence aborts before further run work.

## Validation commands

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py \
  tests/unit/scheduler/test_phase20_4_sequence_abort.py \
  tests/unit/scheduler/test_phase20_4_sequence_status.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/integration/test_phase17_6_abort_lifecycle.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
```

## Validation results (2026-09-13 correction, round 3)

- Phase 20.4 corrections + e2e: **9 passed, 1 skipped** in
  `test_phase20_4_sequence_corrections.py` plus integration e2e (`TMPDIR=/tmp`).
- Prior full automated suite: **888 passed, 2 skipped** (`TMPDIR=/tmp`).
- `ruff format --check`, `ruff check`, `mypy src`, and `mkdocs build --strict`: **clean**.
- Disposable repositories and fake agents only; no real model, timer, or remote Git
  validation performed in this phase.

## Unperformed real validation

- Multi-day unattended timer operation.
- Real Cursor/Codex model-backed sequence runs.
- Real workstation systemd timer enablement.
- Final operator commit/push/PR (explicitly outside scheduler automation).

## Residual risks

- Sequence abort after a successful checkpoint ref CAS leaves the intermediate commit
  immutable; operators must inspect blocked/aborted sequences manually.
- `awaiting_finalization` intentionally releases scheduler ownership; later manual Git
  work is outside the sequence ledger.
- Residual-risk phases accumulate visibly in status and completion reports but still
  advance autonomously by product decision.

## Governance impact

- No broadening of Phase 20.3 checkpoint authority; only lifecycle presentation,
  abort coordination, status/reporting, and non-success reconciliation added.
- Documentation updated in `docs/referencia/cli.md` and existing Phase 20.3 no-commit
  exception language preserved in `docs/index.md`.
