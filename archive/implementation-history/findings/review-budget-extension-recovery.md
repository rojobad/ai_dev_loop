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

Acceptance corrections close the remaining manual-acceptance gaps: exact-target
replay is mutation-free from every later scheduler state and returns the current
state's centralized safe next action; Git/reservation/conflict and injected
transaction failures leave durable data unchanged; CLI/status/controller/history
projections disclose only safe totals/actions; and sequence-bound runs do not
advance on grant alone.

## Commands Run

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest tests/unit/scheduler/test_review_budget_extend.py -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Validation Results

- Full pytest suite: 816 passed (fake agents, isolated XDG state).
- Focused extension tests: 35 passed (ledger validation, artifact binding,
  >500-event ledger completeness, re-exhaustion replay, progressed-state replay
  including `cursor_ready`, blocked/aborted cleanup actions, Git bypass on replay,
  HEAD/branch/staged-patch rejection, CLI grant/rejection contracts, reservation
  conflicts, transaction rollback with full record snapshots, identical and
  different-higher concurrent grants, projection privacy, sequence
  non-advancement, reducer, integration loop).
- Ruff format/check, mypy on `src`, package build, and MkDocs strict build:
  clean.

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
4. **Idempotent replay** — `_replay_exact_target` uses
   `safe_next_action_for_scheduler_state` for every scheduler state when the
   absolute target was already recorded. Replay is read-only from
   `waiting_for_cursor_fix`, `cursor_ready`, `awaiting_codex_review`, completed
   terminals, blocked and aborted states (including pending cleanup actions),
   re-exhausted `max_iterations_reached`, and other later states; it never appends
   events/effects or changes version/reservation and does not invoke Git/artifact
   verification.

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

## Acceptance Correction Evidence

1. **`review_budget_extend.py`** — `_replay_exact_target` accepts the full
   `SchedulerState` union and derives safe actions through the centralized
   projection instead of hard-coding `waiting_for_cursor_fix` replay behavior.
2. **Progressed-state replay** — repeating an already-recorded absolute target
   after timer-driven progress to `cursor_ready`, `awaiting_codex_review`, or
   `completed` returns the current safe action with unchanged state/event/effect
   records; replay from blocked, aborted, and aborted-with-pending-cleanup states
   returns the matching inspect/none/tick-cleanup actions.
3. **Replay Git bypass** — exact-target replay succeeds after repository drift and
   does not call `verify_review_retry_repository_checkpoint`.
4. **Git rejection on new grants** — untracked files, unstaged tracked edits to
   `docs/plans/sample-plan.md`, HEAD commits, branch switches, and staged-index
   drift reject extension without mutating durable scheduler records.
5. **Conflict and rollback tests** — active attempts, conflicting reservations,
   and injected reservation-claim/append/CAS/effect failures roll back complete
   state/event/effect/reservation snapshots.
6. **Concurrency** — overlapping identical grants produce one extension event and
   one extension-linked correction effect; overlapping different-higher targets
   from the same exhausted checkpoint use synchronized observation of the
   exhausted state before the first grant commits, exercise the transactional
   `state changed concurrently` recheck, and grant once with monotonic effective
   totals.
7. **CLI contracts** — `CliRunner` exercises successful new grants and lower/
   equal-unrecorded rejections with exit code `4`, text/JSON receipts, privacy
   redaction, and unchanged durable records on rejection.
8. **Projections and privacy** — status, controller candidate, and history output
   show effective/submitted ceilings without prompts, patches, or full session IDs.
9. **Sequence regression** — extending a sequence-bound maxed run does not advance
   sequence ordinal, current run, or version.

## Residual Risks

- Extension requires the worktree reservation to be `released` for the same run.
  If another run holds the reservation, extension fails without mutation.
- Historical maxed runs must still have a verifiable final Codex attempt outcome
  with actionable findings and matching fix prompt/envelope artifacts.
- Real operator rollout (package install, timer validation, manual extension on a
  production maxed run) remains outside automated validation and requires separate
  controller acceptance. Phase 20.3 worktree, installed package, timer, and real
  XDG scheduler state were not modified during this correction pass.

## Operator Rollout Steps

1. Install the validated package from the isolated worktree with the documented
   `uv tool install --force <path>` procedure after review approval.
2. Validate the scheduler timer unit is healthy.
3. Inspect the target maxed run with `ai_dev_loop scheduler status <run-id>`
   (no sensitive artifact dumping).
4. Run `ai_dev_loop scheduler extend <run-id> --max-review-iterations <higher-total>`.
5. Allow the normal timer-driven `scheduler tick` to execute the scheduled Cursor
   correction and subsequent Codex reviews in the same run/chat/reviewer identity.
