# Phase 20.4 — Sequence Lifecycle, Abort, Observability, and Acceptance

## Goal

Complete the operator-facing lifecycle for frozen linear phase sequences so an
operator can prepare and start all phases once, leave the scheduler running for
days if necessary, inspect safe aggregate progress, abort non-destructively, and
return only when the final phase is staged and every executed phase has an
accepted Codex review.

The completed automation boundary is `awaiting_finalization`: every non-final
phase has one exact checkpoint commit, the final phase is accepted and remains
staged, the repository reservation is released, and the operator receives a
safe aggregate report for final manual review, commit, push, and PR creation.

Both `completed` and `completed_with_residual_risk` are advancement-success
outcomes. Residual-risk phases must remain visibly distinguished and accumulated
in sequence status/reporting; they never silently become clean completions.

## Non-Goals

- Do not automatically commit the final phase, push, create/update a PR, merge,
  rebase, tag, force-update, clean, reset, stash, unstage, delete artifacts, or
  roll back checkpoint commits.
- Do not add sequence retry/recover, skip, reorder, insert, delete, branch,
  conditional execution, parallel phases, or multi-repository sequences.
- Do not treat a residual-risk completion as test success or hide it from final
  reporting.
- Do not add notifications, email/chat integrations, a daemon, a new timer,
  different timer cadence, or real workstation timer enablement.
- Do not alter per-run Cursor/Codex identity, iteration budgets, capacity waits,
  timeouts, effects, attempts, or review decision semantics.
- Do not invoke real Cursor, Codex, provider accounts, hooks, signing, remotes,
  or user-global integration changes during implementation/tests.

## Scope

- Finalize typed sequence lifecycle states, events, reducers, safe actions, and
  reconciliation for prepared, active, capacity-waiting-through-active-run,
  checkpointing, blocked, abort-pending, aborted, and
  `awaiting_finalization` outcomes.
- Implement `ai_dev_loop scheduler sequence abort <sequence-id>` with durable
  abort precedence, safe delegation to the current run's existing abort
  machinery, future-entry cancellation, and no destructive Git behavior.
- Reconcile all non-success current-run terminal outcomes into a sequence-level
  blocked result without materializing a successor.
- Enrich exact-ID `scheduler sequence status` text/JSON with safe per-entry
  progress, active run/action, accepted outcome, residual-risk marker,
  checkpoint SHA prefix, timestamps, and finalization guidance.
- Produce a deterministic safe sequence completion/report artifact with base
  SHA, ordered run/checkpoint evidence, accumulated residual risks, final staged
  patch hash, and unperformed manual actions—without embedding protected code,
  prompts, reviews, identities, or raw output.
- Complete current MkDocs/CLI/security/operations/troubleshooting documentation
  for the end-to-end feature and update the top-level no-commit statement to the
  exact final contract implemented in Phase 20.3.
- Add full fake end-to-end, abort, restart, corruption, privacy, migration,
  backward-compatibility, packaging/docs, and regression tests.
- Write final Phase 20.4 findings and acceptance evidence.

## Out of Scope

- `ai_dev_loop.yaml`, model catalogs/defaults, package-owned Codex skills,
  planning/review skills, SessionStart hooks, bridges, global integrations,
  user credentials, real systemd state, or target repository configuration.
- New Git checkpoint mechanisms beyond hardening/correcting the Phase 20.3
  exact-tree adapter and recovery contract when tests expose an in-scope defect.
- Public sequence list/history/delete/retry/finalize commands. The public Phase
  20 surface remains `prepare`, `start`, `status`, and `abort`.
- Treating manually committed/pushed/merged work as scheduler-owned state. The
  scheduler stops at `awaiting_finalization` and does not poll GitHub or remotes.
- Real model-backed or multi-day acceptance runs.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file, including the narrow
  sequence-checkpoint exception introduced and reviewed in Phase 20.3.
- Phase 20.1–20.3 plans, findings, migrations, state/schema models, artifacts,
  materializer, Git checkpoint adapter, tick reconciliation, and all tests.
- Final Phase 19 plan/findings and implementation for Codex capacity waiting,
  exact reviewer continuation, reservation retention, tick no-growth behavior,
  and abort precedence.
- Phase 17.2, 17.5–17.10, and Phase 18 plans/findings for reservation/capacity,
  terminal review outcomes, bounded loops, abort/restart, privacy, timer cadence,
  status/timeline, and operator ownership.
- Scheduler abort/abort-reconcile, tick, status, safe actions, history/timeline,
  Cursor/Codex workflow services, attempts/effects, domain events/reducers/state,
  SQLite store/migrations, sequence application services, protected artifacts,
  paths, and command rendering.
- Current unit/integration fixtures for long fake scheduler workflows, capacity
  transitions, aborting active children, crash reconciliation, privacy, schema,
  timer, packaging, and docs.
- All current user-facing docs, especially quick/full flow, CLI reference,
  observability, troubleshooting, safety/privacy, timer operation, and index.

Phase 20.3 must be independently reviewed, committed, and present as the clean
baseline before Phase 20.4 begins.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local `.cursor/rules/*.mdc` rule,
  including Phase 20.3's narrowly authorized intermediate checkpoint commits.
  Do not broaden that authority during lifecycle polishing.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned controller/handoff skills or the planning/staged-review skills.
- Use fake Cursor/Codex/account probes, temporary Git repositories and XDG/config
  homes, injected clocks/IDs/failure hooks, and no real systemd or network calls.
- Keep changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Complete sequence state machine

- Keep sequence state separate from per-run state. The sequence aggregates and
  coordinates runs; it does not replace the existing run reducer or invent
  agent decisions.
- Use named typed transitions and version/CAS fencing for at least:
  `prepared`, `active`, `checkpoint_pending` (or the Phase 20.3 equivalent),
  `abort_pending`, `blocked`, `aborted`, and `awaiting_finalization`.
- Do not use a terminal state to mean a retryable wait. Phase 19
  `waiting_codex_capacity` remains a non-terminal state of the current run while
  the sequence remains active.
- `awaiting_finalization` is terminal for scheduler automation but accurately
  states that manual Git/PR work remains. It holds no reservation, live effect,
  attempt, capacity claim, or pending successor.
- Sequence state must agree with ordered entry projections and the current
  materialized run. Missing, duplicate, out-of-order, future-materialized, or
  hash-conflicting state is corruption and fails closed.

### Advancement and blocked outcomes

- Continue advancing from both accepted outcomes and retain a per-entry
  `residual_risk` marker for `completed_with_residual_risk`.
- Never advance from `max_iterations_reached`, `blocked`, or `aborted`. When the
  current run reaches a non-success terminal outcome, persist one sequence-level
  blocked/aborted transition and leave every later entry unmaterialized.
- Do not parse error prose to classify the sequence. Use typed run states/events
  and authenticated entry/run bindings.
- A blocked sequence's safe action is inspection/manual intervention. There is
  no automatic retry, skip, replacement run, or mutation of its frozen entries.
- A checkpoint transition interrupted at a proven Phase 20.3 boundary remains
  automatically reconcilable; do not misclassify a recoverable exact checkpoint
  intent as a generic failed phase.

### Sequence abort

- `sequence abort` is explicit non-destructive process control. Persist the
  sequence abort request before signaling or delegating to the active run, and
  prevent all new checkpoint/ref/materialization activity once it is visible.
- For a prepared sequence, abort writes only ledger/sequence state and cancels
  all planned entries; there is no run, reservation, process, or Git mutation.
- For an active run, reuse the exact existing scheduler run-abort safety path,
  process metadata validation, TERM/KILL bounds, attempt/effect/timer invalidation,
  and cleanup reconciliation. Do not duplicate unsafe signaling logic.
- Future planned entries become cancelled sequence entries but never fake
  scheduler runs. Do not create `scheduler_runs` rows merely to represent their
  cancellation.
- If abort arrives before checkpoint ref mutation, it wins and no commit or
  successor is created. If the exact ref CAS already succeeded, reconcile and
  record that immutable fact without reset/revert; do not materialize a successor
  after the sequence abort request. Release the reservation only after active
  process and checkpoint ambiguity are safely resolved.
- Repeated abort is idempotent. Concurrent abort/tick/start/checkpoint operations
  must converge without duplicated signals, events, commits, runs, or releases.
- Refuse or safely no-op abort for `awaiting_finalization` according to the
  existing terminal-abort convention; never alter its staged final work.

### Observability and final report

- Exact sequence status must remain read-only and usable while locks/leases are
  held. It may project current run state and safe action but must not inspect raw
  agent artifacts or execute Git.
- Return ordered entries with safe phase name, ordinal/total, planned/materialized
  state, materialized run ID where present, accepted outcome, residual-risk bool,
  checkpoint commit prefix for non-final successes, and safe timestamps.
- Show aggregate counts: planned, materialized, accepted, residual-risk,
  checkpointed, cancelled, and remaining. Preserve a stable versioned JSON
  response and deterministic ordering.
- The final safe report must bind the original first-phase admission/base SHA,
  each materialized run and accepted review-result hash, each intermediate
  checkpoint parent/tree/commit hash, every residual-risk phase, final staged
  patch hash, and `awaiting_finalization` timestamp.
- Store only safe hashes/identifiers in the report. Reference sensitive review,
  patch, prompt, or agent artifacts by protected relative path only if needed;
  never embed their contents or expose those paths in default CLI output.
- Final guidance must say that final commit, push, PR review, and merge were not
  performed. Do not claim the work is integrated or production-ready.

### Reservation and final repository boundary

- Preserve continuous direct reservation ownership across every successful
  intermediate handoff. A sequence blocked before handoff must never permit a
  successor to claim through an unverified release/reacquire window.
- On accepted final phase completion, require the ordinary reviewed staged patch
  to remain exact, update run/entry/sequence state and release the reservation
  transactionally where possible, and perform no Git commit/ref mutation.
- `awaiting_finalization` intentionally allows the operator to take over the
  repository. Later manual Git changes do not rewrite immutable sequence history.
- Standalone runs continue to end staged and are not retroactively associated
  with sequences.

### Documentation and operational truth

- Document the authoring/preparation/start flow, manifest format, 2–32 bound,
  immutable order, just-in-time materialization, per-phase agent identities,
  success outcomes, residual-risk behavior, exact unsigned/no-hook checkpoint
  commits, final staged boundary, abort, capacity waits, and failure handling.
- State clearly that prepare does not reserve Git; start reserves through the
  first run; later baselines are prior checkpoint commits.
- Update any remaining categorical “never commits” statement to the exact
  sequence exception without weakening standalone/non-destructive rules.
- Do not instruct users to enable or modify the real WSL timer as part of phase
  implementation. Link to the existing timer runbook for separately authorized
  workstation operation.

## Implementation Plan

1. Audit Phase 20.1–20.3 state transitions and add invariant tests for every
   allowed/rejected aggregate lifecycle edge before expanding public behavior.
2. Implement typed reconciliation from current run terminal states into sequence
   blocked/aborted/final states, preserving the Phase 20.3 exact-checkpoint
   recovery path and preventing all successor creation on non-success outcomes.
3. Implement `scheduler sequence abort` as a durable sequence-first coordinator
   over the existing run abort/reconciliation service. Cover prepared, active
   agent, capacity-waiting, checkpoint-before-CAS, checkpoint-after-CAS,
   blocked, repeated, concurrent, and final states.
4. Complete exact-ID sequence status projections and stable text/JSON rendering.
   Add aggregate counts, safe per-entry status, residual risks, current run/action,
   checkpoint prefixes, and terminal guidance without artifact leakage.
5. Produce/verify the deterministic final sequence report when the last accepted
   phase reaches `awaiting_finalization`. Make report creation idempotent and
   content-bound to ledger/run/checkpoint evidence.
6. Add full fake two-, three-, four-, and bounded-long sequence integration tests,
   including restart at every durable boundary, Phase 19 capacity waits,
   residual-risk advancement, phase failure, abort, concurrent tick, and final
   staged repository verification.
7. Update current MkDocs documentation and CLI help comprehensively, then build
   docs strictly. Do not document unimplemented retry/PR/final-commit behavior.
8. Write
   `archive/implementation-history/findings/phase-20-4-sequence-lifecycle-and-acceptance.md`
   with the implemented state machine, automated evidence, unperformed real
   multi-day/model/timer validation, governance impact, and residual risks.

## Testing Criteria

- **State/reducer tests:** every allowed and rejected sequence transition;
  version/CAS conflicts; state/entry/current-run consistency; terminal immutability;
  old sequence/run snapshots; corrupted hashes/order/current pointers fail closed.
- **Advancement tests:** `completed` and `completed_with_residual_risk` both
  advance; residual risk accumulates visibly; max iterations, blocked, aborted,
  invalid review, and admission failure never commit or materialize successors.
- **Capacity integration tests:** repeated `waiting_codex_capacity` ticks retain
  the same run/reservation and do not grow sequence events; restored capacity
  resumes the same B and the sequence advances only after accepted review.
- **Abort unit/integration tests:** prepared abort; active fake Cursor/Codex abort;
  capacity-wait abort; pre/post-ref-CAS checkpoint abort; repeated/concurrent
  abort; stale process metadata; future entries never materialized; no reset,
  rollback, cleanup, new checkpoint, or successor after abort intent.
- **End-to-end fake sequences:** distinct Cursor chat/reviewer B per phase,
  identity reuse only within corrections, direct reservation transfer, clean
  checkpoint baseline for later admission, one commit per non-final phase, final
  staged patch, released reservation, and `awaiting_finalization` report.
- **Restart/fault tests:** restart during prepare artifact completion, first
  materialization, agent attempt, capacity wait, accepted review, checkpoint
  intent/object/ref, successor materialization/transfer, final report, and abort
  reconciliation converges without duplicate work.
- **Status/report/privacy tests:** deterministic order/counts, residual-risk and
  commit prefixes, safe next actions, JSON schema stability, read-only behavior,
  and absence of prompts, patches, review Markdown, raw JSONL/stderr, Git identity,
  auth/account details, full agent IDs, or protected artifact content.
- **Compatibility/regression tests:** standalone scheduler behavior, Phase 19,
  migrations, timer, cutover, controller lookup, history/timeline, package build,
  and all prior security/privacy regressions remain green.
- **Documentation tests:** strict MkDocs build and assertions that docs describe
  both accepted outcomes, no-hook/no-sign checkpoints, final staged/manual
  boundary, prepare-vs-start reservation semantics, abort, and no automatic PR.

## Validation

Run focused sequence lifecycle tests, the entire automated suite with native WSL
temporary directories, and all static/package/docs checks:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py \
  tests/unit/scheduler/test_phase20_4_sequence_abort.py \
  tests/unit/scheduler/test_phase20_4_sequence_status.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py \
  tests/integration/test_phase17_6_abort_lifecycle.py \
  tests/integration/test_phase17_6_restart_reconcile.py \
  tests/integration/test_phase17_6_privacy.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
uv build
git diff --check
```

Do not run real model-backed, provider-account, systemd-timer, hook, signing,
remote Git, or active-source-worktree acceptance tests.

## Risks Or Recovery Notes

An operator may return to a sequence containing several residual-risk approvals.
The final report must make those risks prominent and phase-specific because the
product decision intentionally favors autonomous progression over stopping on
test-environment failure.

Abort cannot erase an intermediate checkpoint whose branch ref was already
updated. Safe behavior is to record the exact commit, stop before creating the
next run, preserve repository state, and require manual follow-up—never reset or
revert automatically.

`awaiting_finalization` releases scheduler ownership intentionally. The operator's
later commit/push/PR activity is outside the immutable sequence ledger and must
not be inferred or polled by this phase.

## OpenQuestions

None.
