# Review Budget Extension Acceptance Corrections

## Goal

Close the manual-acceptance gaps in commit `72950ec` before the scheduler-native
review budget extension is installed or used. Make an absolute extension target
idempotently replayable after the timer has advanced the run beyond
`waiting_for_cursor_fix`, and add the missing high-risk tests required by the
approved recovery plan.

## Non-Goals

- Do not redesign the extension event, artifact recovery, or effective-budget
  architecture that is already correct.
- Do not add another recovery command, automatic extension, or successor run.
- Do not install the package, mutate real XDG scheduler state, extend the real
  Phase 20.3 run, or touch its separate worktree.
- Do not implement Phase 20.3 or Phase 20.4 behavior.

## Scope

- Correct exact-target replay semantics for every later scheduler state after a
  grant has already been durably recorded.
- Return the current state's redacted safe next action on replay without
  appending events, changing state/version/reservation, or creating effects.
- Add focused CLI/projection, Git/reservation, transaction/concurrency, privacy,
  and sequence-bound regression tests omitted by the initial implementation.
- Correct documentation/findings if tests expose any inaccurate claim.

## Out of Scope

- Changes to frozen submit context, configuration defaults, schemas, database
  migration numbering, agent command construction, reviewer retry/capacity, or
  sequence checkpoint commits.
- Commits, pushes, merges, timer changes, real agents in tests, or destructive
  Git operations.

## Required Context

Read `AGENTS.md`, all `.cursor/rules/*.mdc`, the parent plan
`archive/implementation-history/plans/review-budget-extension-recovery.md`, its
findings, commit `72950ec`, and the changed review-budget application/domain/CLI
code and tests. Treat this correction plan as the active scope.

## Cursor Rules And Skills

Follow `AGENTS.md` and every `.cursor/rules/*.mdc`, especially governance,
state/schema, orchestrator, Codex review, loop/resume, abort, and
docs/acceptance contracts. Do not invoke the staged-review skill while
implementing. Use only fake agents and temporary repositories/XDG state in
tests.

## Architecture Guardrails

- A previously recorded absolute target is an idempotent replay in any current
  state, including `cursor_ready`, active Cursor checkpoints,
  `awaiting_codex_review`, review retry/capacity waits, `completed`,
  `completed_with_residual_risk`, later `max_iterations_reached`, `blocked`, or
  `aborted`. Replay must never reopen or alter those states.
- Idempotent replay returns the current state's existing safe next action, not a
  stale action derived from `waiting_for_cursor_fix`. Never tell an active,
  completed, blocked, aborted, or re-exhausted run to perform an invalid tick.
- Only an exact target already present in a validated extension event qualifies
  as replay. A higher new target remains eligible only from the current
  `max_iterations_reached` checkpoint; lower/unrecorded equal targets remain
  rejected.
- Replay performs no Git or artifact validation and no mutation because the
  durable grant is already authoritative. It must not append an event, insert
  an effect, claim/release a reservation, change state/version, or invoke an
  agent.
- Preserve event-digest validation before trusting a replay target. Corrupt
  extension history must fail closed.
- Tests must assert transaction-level absence of partial writes, not merely the
  returned status.
- Keep prompts, patches, full identities, artifact contents, and absolute
  protected paths out of CLI/status/history output.
- Preserve Phase 20.1 reviewer recovery and Phase 20.2 sequence behavior. A
  maxed sequence entry never advances because of an extension grant alone.

## Implementation Plan

1. Add failing tests that apply a grant, let fake scheduler ticks advance to at
   least `cursor_ready`/active, `awaiting_codex_review`, and a completed state,
   then repeat the identical absolute target. Assert an idempotent no-op with
   the current safe action and unchanged database counts/version/reservation.
2. Refactor `_replay_exact_target` to accept the complete scheduler state union
   and derive the safe action through the centralized current-state projection.
   Preserve the specialized re-exhaustion action requiring a higher target.
3. Add CLI tests for successful application, exact replay after progress,
   invalid lower/equal-unrecorded targets, JSON/text stability, and redaction.
4. Add Git/reservation tests proving staged-patch, HEAD/branch, unstaged,
   untracked, active-attempt, and conflicting-reservation failures produce no
   event/state/effect/reservation mutation.
5. Add transaction/concurrency tests for identical requests, different higher
   targets, and injected failures around reservation claim, event append, CAS,
   and effect insertion. Verify atomic rollback and at most one correction
   effect.
6. Add status/controller/history tests showing the effective and submitted
   ceilings accurately while preserving privacy.
7. Add a sequence regression proving a sequence-bound maxed run does not commit
   or advance merely because its budget is extended.
8. Update findings so every claimed validation has a corresponding test or
   remove the unsupported claim. Keep changes narrowly tied to acceptance.

## Testing Criteria

Automated tests are mandatory. Extend
`tests/unit/scheduler/test_review_budget_extend.py` and existing CLI/status,
controller, history, reservation/concurrency, and sequence test modules where
their fixtures provide stronger coverage.

Required evidence:

- exact replay from waiting, active/progressed, accepted-terminal, blocked or
  aborted, and re-exhausted states is read-only and state-appropriate;
- concurrent/repeated commands never double-grant or double-schedule;
- every validation/conflict/injected-failure path leaves durable data unchanged;
- Git drift and another active reservation fail closed;
- CLI and projections disclose only safe totals/actions;
- sequence state does not advance on grant;
- existing Phase 19, 20.1, and 20.2 regressions remain green.

Run:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Validation

Leave the correction staged for independent review. Confirm the worktree for
Phase 20.3 remains untouched and no real scheduler state, timer, package, or
agent was modified outside the orchestrated run.

## Risks Or Recovery Notes

- The initial implementation is intentionally retained as an isolated branch
  checkpoint but is not approved for installation until this correction passes.
- Avoid broad test-only mocks that make transaction tests pass without
  exercising SQLite commit/rollback behavior.
- If current safe-action projection cannot represent extension replay cleanly,
  stop rather than inventing a new public state or enum outside this plan.

## OpenQuestions

None.
