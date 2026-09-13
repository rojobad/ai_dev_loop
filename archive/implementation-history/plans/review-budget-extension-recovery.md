# Review Budget Extension Recovery

## Goal

Restore an explicit, scheduler-native way for an operator to grant a higher
Codex review ceiling to the same run after it reaches
`max_iterations_reached`. The resumed run must keep its frozen submit context,
run identity, Cursor chat, Codex reviewer identity, staged patch, and protected
artifact history.

The public operation is an absolute, idempotent target rather than an additive
counter:

```text
ai_dev_loop scheduler extend <run-id> --max-review-iterations <higher-total>
```

For the motivating run, raising the effective ceiling from 7 to 10 must recover
the exact validated review-07 fix prompt and schedule Cursor correction 08 in
the existing chat. The normal timer-driven scheduler then continues the same
run and resumes the same reviewer for later reviews.

## Non-Goals

- Do not automatically extend review budgets.
- Do not revive `completed`, `completed_with_residual_risk`, `aborted`,
  `blocked`, or any other terminal/stopped state.
- Do not create a successor run, replacement Cursor chat, or replacement Codex
  reviewer.
- Do not change a submitted run's immutable context, effective-config artifact,
  plan, prompt, model, reasoning effort, timeouts, or Git identity.
- Do not add generic sequence retry, skip, recovery, checkpoint commit, push,
  merge, or PR behavior.
- Do not invoke Cursor or Codex directly from `scheduler extend`.
- Do not modify SQLite by an operational one-off script or special-case the
  motivating run ID in product code.

## Scope

- Add a typed `review_budget_extended` domain event and a centralized reducer
  transition from `MaxIterationsReachedState` to
  `WaitingForCursorFixState`.
- Derive an effective review ceiling from the immutable submitted ceiling plus
  validated extension events; do not rewrite the submitted context.
- Add an application service and `scheduler extend` CLI surface with concise
  text/JSON receipts and idempotent absolute-target semantics.
- Validate and recover the exact final-review result, fix prompt, correction
  envelope, staged patch, repository identity, agent identities, attempt state,
  and worktree reservation before scheduling a correction.
- Reacquire the released worktree reservation transactionally and enqueue the
  existing Cursor correction effect without launching a process.
- Update scheduler status/controller projections, safe next actions, redacted
  history, active product docs, applicable Cursor rules, automated tests, and a
  findings handoff.
- Preserve compatibility with scheduler state already persisted by the Phase
  20.2 baseline, including the motivating seven-review run.

## Out of Scope

- Phase 20.3 reviewed checkpoint commits and sequence handoff implementation.
- Phase 20.4 sequence lifecycle and abort behavior.
- Changes to `ai_dev_loop.yaml` defaults.
- Changes to Cursor/Codex subprocess command construction, model selection,
  capacity probing, or reviewer retry classification.
- Legacy `ai_dev_loop extend`, `prepare`, `launch`, `resume`, or legacy XDG run
  restoration.
- Real workstation package installation, timer unit mutation, real run
  extension, Git commit, push, merge, reset, clean, stash, or unstaging. Those
  remain controller/operator rollout actions after automated validation and
  manual acceptance.

## Required Context

Read before implementation:

- `AGENTS.md`.
- All `.cursor/rules/*.mdc`, especially state/schema, orchestrator, Codex
  review, loop/resume, abort, and docs/acceptance contracts.
- `archive/implementation-history/master-plan.md`.
- `archive/implementation-history/plans/phase-17-tick-based-central-run-scheduler.md`.
- `archive/implementation-history/plans/phase-17-5-codex-review-and-bounded-scheduler-loop.md`.
- `archive/implementation-history/findings/phase-17-central-scheduler-handoff.md`.
- `archive/implementation-history/plans/phase-20-1-1-retryable-reviewer-failures.md`
  and its findings, if present under the repository's actual naming.
- `archive/implementation-history/plans/phase-20-2-lazy-phase-materialization.md`
  and its findings.
- Historical commit `517f13b` and its deleted
  `src/ai_dev_loop/commands/extend.py` for behavioral intent only. Do not copy
  the legacy storage/worker design into the central scheduler.
- Current scheduler domain events/state/reducer, Codex workflow service,
  attempt/effect persistence, reservation operations, protected artifacts,
  repository/Git validation, status/controller/history projections, CLI, and
  their tests.

The current implementation is authoritative where archived legacy material
conflicts with scheduler code, schemas, tests, or active docs.

## Cursor Rules And Skills

Follow `AGENTS.md` and every repository rule under `.cursor/rules/`, including:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`

Use repository-native conventions and tests. Do not invoke the staged-review
skill during implementation. Automated tests must use fakes and temporary XDG
state; never call real Cursor or Codex.

This plan expressly narrows two older rule statements: only
`max_iterations_reached` may be left through the explicit extension command,
and that state must preserve/recover the latest validated fix prompt. Update
the applicable rules accurately in the same patch. All other terminal
immutability guarantees remain unchanged.

## Architecture Guardrails

- **Explicit authority only:** no tick, timer, config value, sequence, or agent
  result may grant more reviews. Only `scheduler extend` with a strictly higher
  absolute total may append the authorization event.
- **Frozen submit context:** `context.workflow.max_review_iterations` and all
  submitted artifacts remain byte-for-byte immutable. Compute the effective
  ceiling from the frozen base plus typed, digest-validated
  `review_budget_extended` events.
- **Idempotence:** an absolute target is authoritative. Repeating the same
  target must not add budget, duplicate an event/effect, increment versions, or
  launch/schedule another turn. A lower/equal target is a no-op or a clear
  validation result; it must never reduce the effective ceiling.
- **Narrow source state:** a new grant is valid only while the current state is
  `max_iterations_reached`, the requested total is greater than the current
  effective total, and the completed review count equals the exhausted
  checkpoint. An already-applied exact replay may return its prior receipt from
  a later state without mutation.
- **Exact final findings:** recovery must use the final exhausted review's
  schema-validated `cursor_fix_prompt`, never `codex.latest_fix_prompt_*` from
  an earlier review. Bind the review result, exact fix prompt, and exact
  correction envelope by protected relative path and SHA-256. Reject missing,
  malformed, mismatched, empty, or ambiguous artifacts.
- **Agent identity:** retain exactly the stored Cursor chat and Codex reviewer
  session. Never use `--last`, bootstrap a second reviewer, create a second
  Cursor chat, or expose full identities in output/events.
- **Git safety:** before mutation, validate the stored repository root,
  worktree key, Git common directory, Git directory, branch, HEAD, and current
  staged patch against the exhausted run's checkpoint. Require no tracked
  unstaged or untracked non-ignored drift. Do not stage, unstage, commit, reset,
  clean, stash, push, or otherwise rewrite Git state.
- **Reservation fencing:** require no active/nonterminal attempt and
  atomically reacquire the exact released worktree reservation. If another run
  owns it, fail with no event, state change, or effect. State CAS, reservation
  claim, event append, and correction-effect insert must commit or roll back as
  one transaction.
- **No direct execution:** successful extension ends in the normal existing
  `waiting_for_cursor_fix` checkpoint with one due Cursor correction effect.
  The timer/tick remains the only executor.
- **Budget enforcement everywhere:** all budget comparisons and user-visible
  projections must use one centralized effective-ceiling helper, including
  Codex decision processing, correction dispatch safeguards, attempt payloads,
  scheduler/controller status, and safe next actions. Do not leave a path that
  rereads only the frozen base and prematurely exhausts the extended run.
- **Privacy:** default CLI/status/history/event rendering may expose counts and
  safe action text only. Keep prompts, patches, full IDs, raw review JSON/JSONL,
  and absolute protected-artifact paths out of public output.
- **Compatibility:** do not require a database schema migration solely for the
  grant. The existing append-only event ledger is the durable source; old runs
  without extension events resolve to their frozen base ceiling. Persisted
  model/schema changes, if genuinely necessary, require explicit versioned
  compatibility and must not conflict with Phase 20.3's migration numbering.
- **Sequence behavior:** `max_iterations_reached` never counts as an accepted
  sequence outcome and cannot trigger a checkpoint commit or phase advance.
  Extension keeps the same phase/run. Only its later existing accepted outcome
  may allow normal sequence handling after the Phase 20.3 implementation is
  integrated.

## Implementation Plan

1. Characterize current max-budget behavior and the exact protected artifacts
   produced for the final review. Add failing regressions proving the current
   transition does not provide a safe continuation checkpoint and that the
   submitted context must remain immutable.
2. Add `ReviewBudgetExtendedEvent` to the typed event union. Include only safe,
   recovery-relevant fields: run ID, exhausted review iteration, previous and
   new effective totals, and protected relative path/hash bindings for the
   final review result, fix prompt, and execution envelope. Ensure history
   rendering redacts artifact details.
3. Add one centralized effective-review-ceiling query that starts with
   `state.context.workflow.max_review_iterations` and folds validated extension
   events monotonically. Reject decreasing, malformed, out-of-order, or
   state-inconsistent grants instead of silently accepting them.
4. Harden max-budget decision ingestion so the final schema-valid review
   result, exact fix prompt, and exact correction envelope are durably available
   before `max_iterations_reached` is persisted. Keep the changes staged and do
   not enqueue Cursor at exhaustion. Preserve compatibility with historical
   maxed runs whose attempt outcome already references write-once review- and
   fix-artifacts but whose state snapshot does not.
5. Implement a dedicated extension application service. It must load and
   verify the maxed snapshot and its latest ingested Codex attempt, validate
   final-review cross-fields/artifact hashes, perform read-only Git identity and
   staged-patch validation, and reject any active attempt or reservation owner.
6. In one immediate transaction, revalidate state/version/effective ceiling,
   claim the exact worktree reservation, append the grant event, reduce to
   `WaitingForCursorFixState` without double-counting review 07, CAS the state,
   and insert exactly one existing Cursor correction effect for iteration 08.
   Roll back every write on any conflict.
7. Add `ai_dev_loop scheduler extend <run-id>
   --max-review-iterations <integer> [--output text|json]`. Require an integer
   greater than the current effective ceiling. Produce stable receipts for
   applied, idempotent replay, conflict, ineligible state, and invalid/missing
   artifact cases without sensitive content.
8. Route all subsequent review-limit checks and projections through the
   effective-ceiling helper. Status should display completed/effective reviews
   and identify that the base was explicitly extended without changing the
   frozen source value. A maxed run's safe action should point to the explicit
   extension command; accepted/aborted/blocked states retain their existing
   actions.
9. Update active CLI/operations documentation and the applicable repository
   rules to document the narrow explicit exception, reservation conflict
   behavior, exact-final-prompt requirement, idempotence, and the fact that
   `extend` schedules no process itself.
10. Add
    `archive/implementation-history/findings/review-budget-extension-recovery.md`
    describing implementation evidence, commands run, compatibility coverage,
    residual risks, and the operator rollout steps. Do not include real run
    IDs, session IDs, prompts, patches, or protected artifact contents.

## Testing Criteria

Automated tests are mandatory.

- **Domain/unit tests:** valid maxed-to-waiting transition preserves run,
  context, reviewer/chat bindings, review count, and final artifact bindings;
  correction becomes iteration 08; invalid event totals and source states are
  rejected.
- **Budget tests:** no events use frozen base; one event raises to 10; repeated
  target 10 is idempotent; later exhaustion at 10 can extend to 12; lower
  targets never reduce the ceiling; corrupted event payload/digest fails safe.
- **Artifact tests:** historical maxed snapshot can recover review 07 from its
  latest ingested attempt and write-once artifacts; a stale review-06 prompt,
  missing review-07 prompt/envelope, hash mismatch, malformed result, or result
  without actionable findings is rejected without mutation.
- **Git/reservation integration tests:** exact staged patch and clean non-index
  worktree succeeds; HEAD/branch/repository/staged/unstaged/untracked drift,
  active attempt, or reservation conflict leaves state/events/effects unchanged.
- **Transaction/concurrency tests:** concurrent identical requests create one
  event/effect; concurrent different higher targets resolve monotonically
  without double scheduling; injected failures at reservation/event/CAS/effect
  boundaries roll back all writes.
- **Workflow integration test:** a fake run exhausts at review 3, extends to 5,
  sends the exact review-03 fix prompt to the same fake Cursor chat, resumes the
  same fake reviewer for review 4, and can complete normally. No direct agent
  invocation occurs inside the extend command.
- **Projection/privacy tests:** CLI text/JSON, scheduler status/list/history,
  and controller status show completed/effective counts and safe next actions
  without prompt bodies, patches, full session IDs, raw process output, or
  absolute protected paths.
- **Regression tests:** ordinary non-extended max exhaustion still schedules no
  extra turn; reviewer retry/capacity behavior remains unchanged; standalone
  submissions and Phase 20.1/20.2 sequence prepare/start behavior remain green;
  max exhaustion never advances a sequence.
- **Schema/compatibility tests:** historical scheduler state and databases from
  supported versions remain readable; current event/schema round trips stay
  aligned; no migration number collides with the pending Phase 20.3 work.

Use fake executables only. Run at minimum:

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

- Confirm the plan and prompt are the only baseline changes before the run.
- Confirm the automated implementation leaves all changes staged for review and
  makes no commit, push, timer, package-install, or real XDG-state mutation.
- Review the staged patch against this plan, with particular attention to final
  review-07 artifact selection, event-derived effective limits, reservation/CAS
  atomicity, repeat safety, privacy, and Phase 20.3 compatibility.
- After automated approval, perform a separate controller-side manual
  acceptance because repository rules and real local installation are
  control-plane changes.
- Real rollout must install the validated isolated-worktree package using the
  documented `uv tool install --force <path>` procedure, validate the timer,
  inspect the motivating run without exposing sensitive artifacts, and invoke
  an absolute extension to 10. Those actions are outside Cursor's run.

## Risks Or Recovery Notes

- The motivating run has already released its worktree reservation. Extension
  must fail cleanly if another scheduler run has claimed it. Never evict or
  rewrite another owner.
- Its persisted max-state snapshot may still reference the prior fix prompt,
  while protected review-07/fix-07 artifacts exist. Recovery must use the latest
  ingested Codex attempt and validate review iteration 07; selecting a prompt
  only from the snapshot risks replaying stale findings.
- Installing from the isolated branch is temporary self-hosting bootstrap. Keep
  the Phase 20.3 target worktree untouched until its original reviewer approves
  it. After Phase 20.3 is committed, integrate this correction commit and rerun
  the combined validation before pushing/merging.
- If final artifacts, staged patch, Git identity, or reservation cannot be
  proven, do not mutate the run. Report the precise safe blocker for manual
  diagnosis.
- Do not edit `engine.sqlite3` manually. The public command and transactional
  service are the only authorized mutation path.

## OpenQuestions

None.
