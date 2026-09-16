# Phase 20.6.5 — Authenticated Rollover and Capacity Auto-Resume Findings

## Scope Implemented

- Authenticated rollover aggregate domain models, migration `0010_authenticated_rollover.sql`, and SQLite store operations.
- Process-free `scheduler rollover prepare` with authenticated `max_iterations_reached` source analysis and idempotent definitions.
- `scheduler rollover start` with managed worktree creation, exact patch seeding, fresh Codex reviewer binding, and lazy Cursor chat.
- Pre-CAS abort hold convergence through production tick reconciliation (`rollover_pre_cas_abort_hold_reconciled`).
- Null `rateLimitReachedType` capacity parsing correction and automatic resume from `waiting_codex_capacity` on available probe during tick.
- JSON schema `scheduler-authenticated-rollover-definition-v1.json` including `source_final_review_result_path` / `source_final_review_result_sha256`.
- Operational documentation at `docs/operacion/authenticated-rollover.md`.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_6_5_*.py \
  tests/unit/scheduler/test_v10_migration.py \
  tests/integration/test_phase20_6_5_authenticated_rollover.py
```

## Validation Results

- Phase 20.6.5 unit and integration tests cover domain/schema v10 migration, prepare idempotency and negative paths, fresh reviewer start without source session reuse, capacity marker parsing, capacity auto-resume, and pre-CAS abort hold convergence via tick reconcile without manual hold release.
- Automated tests use fake `agent`/`codex` executables, isolated XDG paths, disposable git repositories, and native `/tmp`.

## Unperformed Real Acceptance

- No real Cursor/Codex rollover against a disposable repository on an installed workstation build.
- No real timer or model-backed smoke for rollover integration.

## Residual Risks

- Target integration after managed-worktree fresh review remains the highest-risk boundary; expand crash/reconciliation coverage before production use.
- Sequence rollover projections and non-final phase handoff require additional tick wiring and tests beyond the standalone prepare/start slice validated here.

## Phase 20.5 Regression

- Capacity null-marker parsing fix preserves fail-closed behavior for malformed siblings and unavailable probes.
- Structured usage-limit classification and manual retry semantics unchanged outside the null-marker correction.
