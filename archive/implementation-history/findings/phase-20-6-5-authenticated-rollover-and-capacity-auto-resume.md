# Phase 20.6.5 — Authenticated Rollover and Capacity Auto-Resume Findings

## Frozen Seed Verification

Before implementation, the content-addressed prototype seed was independently verified:

- Path: `/home/rojobad/.local/state/ai_dev_loop/bootstrap-seeds/3efc11696bd3316cd32241bd4411cba82f7249cdbababfecd6106098e79cffe2.patch`
- Size: `982226` bytes
- SHA-256: `3efc11696bd3316cd32241bd4411cba82f7249cdbababfecd6106098e79cffe2`
- Applied with `git apply` as an untrusted candidate; audited and completed against the Phase 20.6.5 plan.

## Scope Implemented

- Authenticated rollover aggregate domain models, migration `0010_authenticated_rollover.sql`, and SQLite store operations.
- Process-free `scheduler rollover prepare` with authenticated `max_iterations_reached` source analysis and idempotent definitions.
- `scheduler rollover start` with managed worktree creation, exact patch seeding, fresh Codex reviewer binding, lazy Cursor chat, and sequence block authorization for rollover-eligible sequence sources.
- Fresh successor review budgeting via `fresh_agent_successor.py`: successor reviews begin at iteration 1 with zero completed reviews, frozen ceiling from source effective authorized total (including extensions), cursor iteration reset, and definition-scoped staged patch bindings.
- Sequence-successor context construction downgrades `schema_version` to agent-led (3) when clearing `context.sequence`, preserving authenticated sequence lineage on rollover/recovery aggregate state and `fresh_rollover` routing for accepted-review bypass after Cursor corrections.
- Accepted sequence-successor routing bypasses `SequenceHandoffService` when `fresh_rollover` lineage is present; sequence continuation uses rollover integration and `SequenceRolloverResolution`.
- Pre-CAS abort hold convergence through ordinary `TickService.run_once` reconciliation: releasing the matching hold transitions `integration_pending` to `abort_pending` and converges to terminal `aborted` without a second operator abort call.
- Abort reservation release waits for conclusive successor termination (`successor_abort_ownership_release_allowed`) before releasing source target ownership; applies to recovery and rollover deferred abort, direct successor-aborted reconciliation, and `finalize_deferred_aborts`.
- One live recovery/rollover authority per source enforced at prepare and start; competing definitions are rejected while same-intent replay remains idempotent.
- Rollover-resolution support in sequence status and completion-report builders.
- Fail-closed `rateLimitsByLimitId` parsing: present `null` `rateLimitReachedType` with valid windows may indicate availability; a missing marker remains unavailable; legacy `rateLimits` shape retains backward compatibility.
- `review_result_artifact_path` on `RolloverIntegrationIntent` for correction-loop sequence checkpoint binding.
- SQLite bootstrap from schema version 7 reaches version 10 in one pass (including rollover tables); `compare_and_swap_sequence_state` accepts `RecoveryIntegratedFinalizationSequenceState` for final sequence integration.
- Disposable-repository sequence lifecycle tests for non-final and final rollover through prepare/start, correction, accepted review, target CAS, reservation handoff or final release, completion reports, cleanup, immutable source evidence, and replay idempotency.
- JSON schema `scheduler-authenticated-rollover-definition-v1.json` including `source_final_review_result_path` / `source_final_review_result_sha256`.
- Operational documentation at `docs/operacion/authenticated-rollover.md`.

## Commands Run

```bash
git diff --cached --check
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
uv build
```

Focused correction regressions:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest \
  tests/unit/scheduler/test_v10_migration.py \
  tests/unit/scheduler/test_phase20_6_5_rollover_abort_cas.py \
  tests/unit/scheduler/test_phase20_6_recovery_corrections_turn9.py \
  tests/unit/scheduler/test_phase20_6_5_rollover_abort_termination.py \
  tests/unit/scheduler/test_phase20_6_5_rollover_sequence_lifecycle.py \
  tests/unit/scheduler/test_phase20_6_5_rollover_start.py -q
```

## Validation Results

- **Focused correction regressions:** 15 passed.
- **Full suite:** 1074 passed, 3 skipped; one isolated flake in `test_protocol_error_leader_exits_and_term_ignoring_child_is_killed` (probe child PID still live at assertion time) passed on immediate isolated rerun.
- Automated tests use fake `agent`/`codex` executables, isolated XDG paths, disposable git repositories, and native `/tmp`.
- `ruff format --check`, `ruff check`, `mypy src`, `mkdocs build --strict`, and `uv build` pass.

## Unperformed Real Acceptance

- No real Cursor/Codex rollover against a disposable repository on an installed workstation build.
- No real timer or model-backed smoke for rollover integration.

## Residual Risks

- Target integration after managed-worktree fresh review remains the highest-risk boundary; additional crash/reconciliation coverage beyond the ported turn-1 slice should expand before production use.
- Probe cleanup timing flake in Phase 20.5 capacity tests may occasionally fail under heavy parallel load; isolated rerun is clean.

## Phase 20.5 Regression

- Capacity null-marker parsing fix preserves fail-closed behavior for missing `rateLimitReachedType` on `rateLimitsByLimitId` records while legacy `rateLimits` payloads remain readable.
- Structured usage-limit classification and manual retry semantics unchanged outside the null-marker correction.
