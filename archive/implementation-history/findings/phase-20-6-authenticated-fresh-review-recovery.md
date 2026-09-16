# Phase 20.6 — Authenticated Fresh-Review Recovery Findings

## Scope Implemented

- Recovery aggregate domain models, migration `0009_fresh_review_recovery.sql`, and SQLite store operations.
- Process-free `scheduler recovery prepare` with authenticated source analysis and idempotent definitions.
- `scheduler recovery start` with managed worktree creation, exact patch seeding, and review-seed run materialization.
- `scheduler recovery status` and `scheduler recovery abort` read-only/abort paths.
- Review-seed Codex wrapper without trusted Cursor final response; lazy Cursor chat on findings only.
- Recovery integration service reusing Phase 20.3 git checkpoint adapter for target CAS.
- CLI commands under `ai_dev_loop scheduler recovery {prepare,start,status,abort}`.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase20_6_*.py tests/integration/test_phase20_6_*.py
uv run ruff check src tests
uv run mypy src/ai_dev_loop/scheduler/application/recovery_integration.py src/ai_dev_loop/scheduler/domain/reducer.py
uv run mkdocs build --strict
uv build
```

## Validation Results

- 35+ Phase 20.6 unit/integration tests pass (early replay classification, same-intent hold reconciliation, tick-lease fencing, abort via proven-integration reconciliation, sequence resolution status, worktree registration, hold retry convergence, integration lifecycle, abort, reconcile, prepare, artifacts).
- Checkpoint holds release only after verified standalone CAS success; integration fences reservation/abort before target delta and ref mutation.
- Abort integration reconciliation requires intent-bound commit identity verified via `checkpoint_verify_commit_identity`; branch-head alone is not proof.
- Final sequence recovery transitions to `recovery_integrated_finalization`, publishes a completion report, releases the target reservation, and exposes public status with push/PR/merge as remaining manual actions.
- Private ref deletion uses atomic `update-ref -d <ref> <expected-sha>`.
- Full-source mypy, ruff, mkdocs `--strict` succeed.

## Unperformed Real Acceptance

- No real Cursor/Codex recovery against a disposable repository on an installed workstation build.
- No real timer or model-backed smoke for recovery integration.

## Residual Risks

- Target integration after managed-worktree review remains the highest-risk boundary; automated crash/reconciliation coverage should be expanded before production use.
- Sequence recovery continuation and cleanup reconciliation require additional tick wiring and tests.
- Governance rule update for the narrow Phase 20.6 commit exception should be reviewed with the full diff.

## Phase 20.5 Regression

- Phase 20.5 capacity/retry behavior preserved; no changes to capacity transport classification.
