# Phase 20.9 Findings — Multi-Run Sequence Lifecycle and Acceptance

## Summary

Phase 20.9 hardens multi-run sequence lifecycle validation, restart reconciliation,
terminal checkpoint replay authentication anchored to durable `sequence_checkpoint_requested`
events, savepoint-isolated handoff and finalization replay, unpublished Phase 20.8
replacement-intent resume on tick, checkpoint-hold retention until successful handoff,
residual-risk subset invariants, and lineage projection fail-closed behavior.
Phase 20.5/20.7/20.8 contracts are preserved.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_9_terminal_replay_bindings.py \
  tests/unit/scheduler/test_phase20_9_checkpoint_successor_chain.py \
  tests/unit/scheduler/test_phase20_9_concurrency_restart_acceptance.py \
  tests/unit/scheduler/test_phase20_9_review_retry_barrier.py \
  tests/unit/scheduler/test_phase20_9_restart_reconcile_batch.py \
  tests/unit/scheduler/test_phase20_9_multi_run_lifecycle.py \
  tests/unit/scheduler/test_phase20_9_multi_run_reconciliation.py \
  tests/unit/scheduler/test_phase20_9_multi_run_restart_reconcile.py \
  tests/unit/scheduler/test_phase20_9_multi_run_abort.py \
  tests/unit/scheduler/test_phase20_9_multi_run_status_report.py \
  tests/unit/scheduler/test_phase20_9_lineage_projection.py \
  tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py

uv run ruff check \
  src/ai_dev_loop/scheduler/application/sequence_handoff.py \
  src/ai_dev_loop/scheduler/application/sequence_review_recovery.py \
  src/ai_dev_loop/scheduler/infrastructure/sequence_run_lineage_store.py \
  tests/unit/scheduler/test_phase20_9_terminal_replay_bindings.py \
  tests/unit/scheduler/test_phase20_9_checkpoint_successor_chain.py \
  tests/unit/scheduler/test_phase20_9_concurrency_restart_acceptance.py \
  tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py

uv run mypy \
  src/ai_dev_loop/scheduler/application/sequence_handoff.py \
  src/ai_dev_loop/scheduler/infrastructure/sequence_run_lineage_store.py
```

Focused Phase 20.9 + plan regression set: **80 passed** in ~203s (hermetic `TMPDIR=/tmp`).
`ruff check` and `mypy` clean on touched scheduler modules and new tests.

Post-review operator validation ran the complete repository suite after updating
older Phase 20.3, 20.4, and 20.8 regression fixtures for the authoritative
Phase 20.9 lifecycle and tick-driven recovery semantics:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Result: **1080 passed, 2 skipped**; formatting, Ruff, and mypy passed.

## Demonstrated Acceptance Coverage

| Area | Evidence |
|------|----------|
| Checkpoint successor chain (no generation bypass) | `authenticate_checkpoint_successor_replacement_chain` binds durable intent successor to lineage root and `same_reviewer_retry` chain to materialized leaf; positive multi-retry and negative tampered/unrelated cases (`test_phase20_9_checkpoint_successor_chain.py`) |
| Terminal replay exception isolation | `PydanticValidationError` vs `ai_dev_loop.errors.ValidationError` separated; patch/review/intent/trusted-tree `ProtectedArtifactError` → `checkpoint_result_invalid` with hold/reservation retained (`sequence_handoff.py`, `test_phase20_9_terminal_replay_bindings.py`) |
| Hold-path trusted-tree read (pre-auth) | `_reconcile_terminal_checkpoint_hold_for_restart` catches `PydanticValidationError`, `OSError`, and `ProtectedArtifactError` on trusted-tree load; malformed JSON hold-path unit + real `TickService.run_once` batch isolation: invalid held terminal returns `checkpoint_result_invalid` with hold/reservation/no successor while unrelated `seq-peer-finalize` reaches `awaiting_finalization` via unmocked `reconcile_pending_restart_work` (`test_malformed_trusted_tree_json_invalidates_hold_path_reconcile`, `test_malformed_trusted_tree_tick_advances_unrelated_terminal_via_restart`) |
| Restart batch isolation | Invalid terminal leaf does not block subsequent reconcile targets (`test_phase20_9_restart_reconcile_batch.py`) |
| Tick invokes restart reconcile | `test_tick_run_once_invokes_restart_reconcile_work` |
| Abort vs review-retry adoption | Materialize-gated abort/retry races (`abort_wins` true/false); abort must persist (`idempotent_replay=False`); sequence ends `aborted` (never active/blocked); retry conflicts are `CONFLICT`; tick-driven terminal abort with lineage; ≤1 replacement (`test_abort_race_with_review_retry_preserves_single_successor`) |
| Abort after checkpoint commit, before handoff | Applied Git commit retained; deferred `checkpoint_pending` abort converges to `aborted` via tick without duplicate successor or HEAD rollback; hold retained through convergence (`test_abort_after_checkpoint_commit_before_handoff_preserves_hold`) |
| Competing terminal handoff replay | Two threads on savepoint handoff replay: exactly one `sequence_handoff_reconciled`, idempotent peer outcome, single successor run row (`test_competing_terminal_replay_threads_single_handoff_outcome`) |
| Barrier retry + tick adoption | Materialize-gate concurrent retries with tick advance; single successor (`test_barrier_retry_adoption_with_tick_advances_without_duplicate_successors`, `test_phase20_9_review_retry_barrier.py`) |
| Sequence abort without Git mutation | `test_sequence_abort_terminalizes_without_duplicate_git_effects` |
| Same-ordinal triple retry | Block source → retry → block first successor → second retry → complete with report (`test_same_ordinal_second_retry_after_blocking_first_successor_completes_sequence`) |
| Multi-ordinal dual recovery | Three-phase dual review-retry to `awaiting_finalization` (`test_three_phase_dual_review_recovery_reaches_awaiting_finalization`) |
| Explicit-barrier retry races | Competing retries and materialize-gate + tick advance (`test_phase20_9_review_retry_barrier.py`) |
| Restart fault injection | Flaky finalize + explicit `reconcile_terminal_current_leaf` (`test_restart_reconcile_fault_injection_converges_on_second_tick`, savepoint unit tests) |
| Durable-boundary faults (adoption / handoff / report) | Savepoint rollback on handoff/finalization engine errors; replacement-intent publication gates; report publication recovery patterns retained from Phase 20.4/20.8 suites included in regression set above |

## Implementation Notes

- **Durable checkpoint authorization:** `load_authoritative_sequence_checkpoint_requested_event`
  loads the latest `sequence_checkpoint_requested` row with digest verification; terminal
  replay binds intent/trusted-tree artifacts to event digests.
- **Checkpoint parent auth:** Predecessor result `successor_run_id` must match durable intent;
  current materialized leaf must equal the terminal lineage attempt reached via an unbroken
  `same_reviewer_retry` chain from that recorded successor (no `generation >= 2` bypass).
- **Same-reviewer retry lineage:** `materialized_entries` retain phase `materialized_at` on
  execution-leaf replacement; retry attempts reuse that timestamp for aggregate validation.
- **Terminal replay:** Malformed intent/result JSON uses `PydanticValidationError`; repository
  identity failures still use orchestrator `ValidationError` where appropriate.
- **Hold-path trusted tree:** Unguarded trusted-tree parse in hold reconciliation is wrapped
  so invalid evidence returns `checkpoint_result_invalid` without aborting the tick.

## Unperformed Real Validation

- Real Cursor/Codex model-backed multi-run sequences.
- Real workstation systemd timer changes.
- Operator final commit, push, PR, merge, or remote Git actions.

## Residual Risks / Limitations

- Hermetic acceptance does not exhaust every threaded abort/handoff race combination;
  explicit-barrier and savepoint tests cover the approved concurrency/restart matrix rows above.
- Filesystem artifacts written during replay before a savepoint rollback are not automatically
  reverted; convergence relies on idempotent artifact writers and ledger CAS.
- Predecessor checkpoint JSON still records the original handoff successor id; lineage chain
  and materialized-entry checks are authoritative for retry replacements.

## Phase Boundary Confirmation

- No Phase 20.6 worktree/ref cleanup, no new recovery commands, no remote Git automation,
  and no review-ceiling or capacity-classification changes.
