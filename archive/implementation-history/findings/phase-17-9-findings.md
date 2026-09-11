# Phase 17.9 findings — Fresh scheduler submission after a terminal run

## Summary

Corrected scheduler submit replay reporting for terminal runs and added explicit
`--resubmission-id` for intentional fresh submissions after abort (or other
terminal states). Review follow-up aligned submit replay with abort-aware safe
actions, blocked fresh resubmission before pending abort cleanup releases the
worktree reservation, and closed the admission-to-artifact-write race by
keeping reservation claim and protected-artifact publication in one
`BEGIN IMMEDIATE` transaction (ledger insert before artifact writes).

## Changes

- `SubmitOptions` and CLI accept optional validated UUID `--resubmission-id`.
- Idempotency key hashes the resubmission identifier (SHA-256) without persisting
  or rendering the raw UUID.
- Shared `safe_next_action_for_scheduler_state` matches status/controller-read
  abort projection (pending termination, pending cleanup tick, then terminal none).
- Fresh submission checks `get_worktree_reservation` before ledger/artifact writes
  and refuses while an aborted run still holds the reservation.
- Single transactional submit: admission, idempotency replay, reservation insert,
  and bounded frozen-artifact publication share one `BEGIN IMMEDIATE` scope.
- Concurrency regression instruments `begin_immediate` to prove competitors reach
  the admission lock before the primary is released; outcomes are keyed by worker.
- JSON `status` matches `state_kind`; text output distinguishes reused runs.

## Validation

Focused suite:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase17_1_submit.py \
  tests/unit/scheduler/test_start.py \
  tests/unit/scheduler/test_phase17_6_abort_corrections.py \
  tests/integration/test_phase17_6_restart_reconcile.py
```

Result: **43 passed**

Full suite and quality gates:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

Results:

- pytest: **578 passed**, 1 skipped
- ruff format/check: **pass**
- mypy: **pass**
- build: **pass**
- mkdocs `--strict`: **pass**
- `git diff --check`: **pass**

## Residual risks

- Operators must retain an explicit resubmission UUID only long enough to replay
  an uncertain submit; it is not stored by the orchestrator.
- Active worktree conflict rules are unchanged: a distinct resubmission while
  another non-terminal run holds the reservation still fails safely.
- A crash after ledger insert but before all artifact files finish writing can
  leave a run row without a complete artifact set; recovery remains manual
  inspection, not destructive cleanup.

## OpenQuestions

None.
