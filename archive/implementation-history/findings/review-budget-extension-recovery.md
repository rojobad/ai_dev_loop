# Review Budget Extension Recovery Findings

## Summary

Implemented explicit scheduler-native review budget extension for runs in
`max_iterations_reached`, then hardened the correction pass per Codex review:
extension events are digest-authenticated and chronologically validated; exhausted
review recovery binds fix/envelope artifacts byte-for-byte to the latest
schema-validated Codex attempt; all ceiling reads use one complete ordered
`review_budget_extended` ledger query (no 500-event truncation); and idempotent
replay after re-exhaustion recommends an explicit higher `scheduler extend` target
rather than `scheduler tick`.

## Commands Run

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest tests/unit/scheduler/test_review_budget_extend.py -q
uv run python -m ruff check src/ai_dev_loop/scheduler/application/review_budget*.py tests/unit/scheduler/test_review_budget_extend.py
uv run python -m mypy src/ai_dev_loop/scheduler/application/review_budget.py src/ai_dev_loop/scheduler/application/review_budget_artifacts.py src/ai_dev_loop/scheduler/application/review_budget_extend.py
```

## Validation Results

- Full pytest suite: 795 passed (fake agents, isolated XDG state).
- Focused extension tests: 14 passed (ledger validation, artifact binding,
  >500-event ledger completeness, re-exhaustion replay, reducer, integration loop).
- Ruff check on changed review-budget modules/tests: clean after import fix.
- Mypy on changed review-budget modules: clean.

## Correction Pass Evidence

1. **`review_budget.py`** — `load_validated_review_budget_extensions` verifies
   payload digests, run identity, sequence ordering, and
   `review_iteration == previous_effective_total` before folding. Corrupt grants
   raise `SchedulerEngineErrorKind.CORRUPTION`.
2. **`review_budget_artifacts.py`** — Recovers bindings from the latest recorded
   Codex attempt (not stale state prompt pointers). Validates protected relative
   paths, SHA-256 bindings, byte-for-byte `cursor_fix_prompt` agreement, and
   correction-envelope embedding before any extend-side mutation.
3. **Ledger completeness** — `effective_review_ceiling_for_run`,
   `load_review_budget_extensions`, extension service, Codex bindings, decision
   enforcement, and status/controller projections share the full ordered extension
   query.
4. **Idempotent replay** — `_replay_exact_target` on `max_iterations_reached`
   returns `max_iterations_reached_safe_next_action` (explicit extend command), not
   `scheduler tick`, without mutating versions, reservations, events, or effects.

## Implementation Evidence

- Domain event `review_budget_extended` with safe totals and protected artifact
  bindings only.
- Reducer `apply_review_budget_extended` transitions
  `max_iterations_reached -> waiting_for_cursor_fix` without double-counting the
  final review.
- `review_budget_extend.py` application service with idempotent absolute targets,
  Git/staged-patch verification, artifact authentication from latest Codex
  attempt, transactional reservation reacquire, event append, CAS, and effect
  insert.
- `codex_workflow_service` uses effective ceiling for exhaustion decisions and
  persists final review/fix/envelope bindings on max exhaustion.
- CLI: `ai_dev_loop scheduler extend <run-id> --max-review-iterations <N>`.
- Status/list/controller projections show effective ceiling and submitted base when
  extended.
- No new SQLite schema migration (remains at version 6; no collision with pending
  Phase 20.3 numbering).

## Compatibility Coverage

- Historical runs without extension events keep frozen-base ceiling behavior.
- `MaxIterationsReachedEvent` artifact fields are optional for historical event
  replay; extension recovery uses authenticated latest Codex attempt outcome.
- Phase 20.1 review retry, Phase 20.2 sequence prepare/start, and ordinary max
  exhaustion without extension remain covered by existing regression tests.

## Residual Risks

- Extension requires the worktree reservation to be `released` for the same run.
  If another run holds the reservation, extension fails without mutation.
- Historical maxed runs must still have a verifiable final Codex attempt outcome
  with actionable findings and matching fix prompt/envelope artifacts.
- Real operator rollout (package install, timer validation, manual extension on a
  production maxed run) remains outside automated validation and requires separate
  controller acceptance.

## Operator Rollout Steps

1. Install the validated package from the isolated worktree with the documented
   `uv tool install --force <path>` procedure after review approval.
2. Validate the scheduler timer unit is healthy.
3. Inspect the target maxed run with `ai_dev_loop scheduler status <run-id>`
   (no sensitive artifact dumping).
4. Run `ai_dev_loop scheduler extend <run-id> --max-review-iterations <higher-total>`.
5. Allow the normal timer-driven `scheduler tick` to execute the scheduled Cursor
   correction and subsequent Codex reviews in the same run/chat/reviewer identity.
