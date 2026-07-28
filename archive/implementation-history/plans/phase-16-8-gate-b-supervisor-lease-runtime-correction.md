# Phase 16.8 Gate B Supervisor Lease Runtime Correction

## Goals

- Correct the production `pr-review-v2` supervisor assembly so the operational
  `PrReviewEngine` uses the frozen run context's
  `worker.lease_ttl_seconds`, rather than the engine constructor default.
- Reject an inconsistent heartbeat/lease runtime before a worker acquires a
  lease or claims an effect.
- Preserve crash-safe, at-most-once recovery for a mutating
  `request_bot_review` claim whose supervisor died before any external write.
- Make control-plane status report a safe and useful recovery action when an
  active nonterminal run has no live supervisor.
- Add production-boundary regression coverage for the exact Gate B failure and
  its recovery without touching the live acceptance run.

## Non-Goals

- Do not redesign leases, claims, the outbox, the supervisor protocol, or the
  persisted run schema.
- Do not change the Phase 16.8 no-findings policy, accepted Codex comment
  prefixes, reviewed-commit binding, or thumbs-up behavior.
- Do not change Cursor model selection or target-repository configuration.
- Do not add a broad supervisor-readiness handshake unless a new focused test
  proves it is required to fix this defect.
- Do not expand the correction into the deferred uncommon edge-case matrix.
- Do not create the Phase 16.8-to-16.9 handoff; live Gate B recovery must
  succeed first.

## Scope

Production code expected to be relevant:

- `src/ai_dev_loop/pr_review_v2/runtime_factory.py`
- `src/ai_dev_loop/pr_review_v2/workers/effect_worker.py`
- `src/ai_dev_loop/pr_review_v2/application/control.py`
- Small, directly related control/runtime contract changes only if required by
  the existing public enums and focused tests.

Tests expected to be relevant:

- `tests/integration/test_phase16_8_supervisor_entry.py`
- `tests/integration/test_phase16_8_supervisor_restart.py`
- `tests/integration/test_phase16_8_worker_supervisor_matrix.py`
- `tests/integration/test_phase16_8_fault_matrix.py`
- `tests/integration/test_phase16_8_control_matrix.py`
- Existing Phase 16.8 stateful fake GitHub helpers where needed.

Documentation expected to be relevant:

- Current CLI/troubleshooting documentation only if the externally observable
  recovery guidance changes.

## Out of Scope

- The live acceptance checkout
  `/home/rojobad/Projects/parish360-poc-phase16-8-acceptance`.
- GitHub PR `rojobad/parish360-poc#4`.
- The live run
  `prv2-617edf93c28019564a2a5d51653ba1a1`.
- Any real GitHub, Cursor, Codex, model, or network activity.
- Any read or mutation of the user's live XDG ai_dev_loop state.
- Starting, resuming, aborting, or otherwise controlling Gate B.
- Manual edits to SQLite, protected artifacts, claims, leases, or GitHub
  comments.
- Legacy PR review behavior.
- Git staging, commits, pushes, branch changes, resets, cleans, or stashes.

## Required Context

Read before implementation:

1. This complete plan.
2. `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`
3. `archive/implementation-history/findings/phase-16-7-to-16-8-handoff.md`
4. The production and test files listed under Scope.
5. All applicable `.cursor/rules/*.mdc` files.

Live failure evidence is provided here only to define the regression. Do not
inspect the live run or target repository:

- The run was prepared for PR #4 at HEAD
  `b80963bfd05f68d966c167bf8a07ee8e98992c93`.
- Frozen context:
  - `worker.lease_ttl_seconds: 120`
  - `worker.heartbeat_interval_seconds: 30`
- `start` durably moved the run to `waiting_for_bot` and claimed the
  `request_bot_review` effect.
- The detached supervisor exited before publishing a GitHub trigger.
- Its protected stderr reported:
  `heartbeat interval must be strictly less than lease_ttl`.
- After lease expiry the run was nonterminal, had no live supervisor, and was
  resumable, but status reported `next_action: none`.
- GitHub showed no review-trigger comment/review, so the external write was not
  applied.

Confirmed code-level cause:

- `assemble_supervisor_runtime()` opens `PrReviewEngine` without passing a
  lease TTL, so the engine uses its 30-second default.
- It then builds the worker with the frozen 30-second heartbeat interval.
- Configuration validation correctly evaluated `30 < 120`, but the assembled
  production runtime actually evaluated `30 >= 30`.
- `EffectWorker.run_once()` currently claims the effect before
  `LeaseRenewalCoordinator` performs that runtime consistency check.
- Existing supervisor-entry coverage uses lease 30 / heartbeat 10 and therefore
  masks the non-default-TTL wiring defect.

## Cursor Rules And Skills

Read and obey every applicable Cursor rule, including:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-local Cursor skill is required for this correction. If Cursor
discovers a new `OpenQuestion` whose answer changes architecture, persistence,
or recovery semantics, stop before dependent changes and report it.

## Architecture Guardrails

- The frozen execution context remains authoritative for per-run worker timing.
- The engine, worker, heartbeat coordinator, recovery worker, and supervisor
  must agree on one operational lease TTL.
- Do not mutate private engine attributes after construction and do not change
  the engine's global default to make this one path pass.
- A bootstrap read of protected context may use an initially opened engine, but
  every operational component must receive the correctly constructed engine
  instance afterward.
- Runtime consistency must fail before lease acquisition, effect claim, or any
  external side effect. Keep lower-level validation as defense in depth.
- Never turn an uncertain or expired mutating claim directly back into a fresh
  write. It must pass through persisted reconciliation.
- Recovery must preserve run ID, dispatch identity, effect identity, immutable
  input bindings, and claim/lease fencing.
- When reconciliation proves `PROVEN_NOT_APPLIED`, exactly one subsequent
  trigger write is allowed. If it proves `APPLIED`, no duplicate is allowed. If
  it is `UNRESOLVED`, fail closed.
- `StartRequested` must not be reapplied during resume.
- Status guidance must not tell an operator to race a still-active lease.
- Do not leak secrets, full session IDs, prompts, patches, stderr, launcher
  tokens, or protected artifact contents into status/history/logs.
- Tests must use temporary XDG roots, production-boundary fakes, and persistent
  SQLite reopen coverage. No test-only fault controls belong in public project
  configuration.
- This is control-plane/runtime work and requires external staged A/B review
  after automated validation.

## Implementation Plan

### 1. Reproduce the assembly defect in a focused test

- Add a regression using a frozen execution context with lease TTL 120 and
  heartbeat 30.
- Assemble the real supervisor runtime through
  `assemble_supervisor_runtime()`.
- Assert the operational engine exposes a 120-second lease TTL and the worker
  can be constructed/run through a bounded no-op or fake boundary without the
  observed validation failure.
- Include at least one additional valid non-default pair if it makes the
  regression clearer.
- Ensure the test would fail against the current default-30 assembly.

### 2. Wire the frozen lease TTL into the operational engine

- Resolve and verify the protected execution context using the existing safe
  reader/binding path.
- Construct or reopen the operational `PrReviewEngine` for the same database
  with:

  `lease_ttl=timedelta(seconds=v2.worker.lease_ttl_seconds)`

- Pass that same operational engine to all assembled workers/services that
  participate in claims, heartbeat renewal, reconciliation, timers, and
  supervisor execution.
- Keep the bootstrap/context-resolution path minimal and read-only.
- Do not alter persisted schemas or private fields.

### 3. Validate heartbeat/lease consistency before claims

- Add an explicit construction-time or assembly-time invariant for
  `heartbeat_interval < engine.lease_ttl`.
- Keep `LeaseRenewalCoordinator` validation as defense in depth.
- Add a regression for an intentionally inconsistent runtime, such as engine
  TTL 30 with heartbeat 30.
- Prove the failure occurs before:
  - lease acquisition;
  - effect claim;
  - journal/outbox mutation attributable to dispatch;
  - fake external calls.
- Assert the effect remains pending and recoverable.

### 4. Prove persistent recovery of the exact mutating failure window

- Use persistent SQLite plus the production worker/runtime boundary and the
  stateful fake GitHub transport.
- Create/start a synthetic run and reach the `request_bot_review` mutating
  claim.
- Simulate supervisor death after durable claim but before external write.
- Let the claim/lease expire, close the process/runtime, and reopen from disk
  using the corrected assembly.
- Prove the expired mutating claim is routed into reconciliation rather than
  replayed blindly.
- Configure fake GitHub evidence to prove `PROVEN_NOT_APPLIED`.
- Prove the recovered run then emits exactly one authorized review-trigger
  mutation.
- Assert:
  - one trigger total;
  - no duplicate on another resume/tick;
  - same run, dispatch, and effect bindings;
  - monotonically fenced lease/claim generations;
  - no manual state edits;
  - no repeated `StartRequested`;
  - protected artifacts remain valid.
- Add/retain the complementary `APPLIED` and `UNRESOLVED` assertions if they
  are not already covered at this exact boundary.

### 5. Make dead-supervisor recovery guidance honest

- Align status output with the already-supported active-run `resume` behavior.
- For a nonterminal active run with no live supervisor and no unsafe live
  lease, report:
  - `resumable: true`;
  - `next_action: resume`.
- For a still-live lease/ownership window, do not advise an immediate competing
  resume. Preserve a safe wait/no-op indication until recovery is legal.
- Reuse existing status/next-action enums and contracts where possible. Do not
  add a persisted schema merely for presentation.
- Add tests for:
  - dead supervisor after lease expiry;
  - dead supervisor while the old lease is still active;
  - paused/waiting-user existing semantics;
  - terminal-run semantics;
  - `resume` repairing the supervisor without duplicate start or write.

### 6. Update current operational documentation if needed

- If the public status guidance changes, document that `resume` is the safe
  action only after the run reports it.
- Keep documentation explicit that an expired mutating claim is reconciled
  before retry.
- Do not claim the live Gate B run has passed.

### 7. Preserve the live recovery handoff

- Do not execute the live recovery.
- In Cursor's final response, state that after implementation, validation, and
  clean staged A/B review, controller session A may run:

  `ai_dev_loop pr-review-v2 resume prv2-617edf93c28019564a2a5d51653ba1a1`

- State that the controller must then verify the existing run reconciles the
  expired `request_bot_review` claim and publishes at most one trigger before
  continuing Gate B.

## Testing Criteria

Automated acceptance must prove all of the following:

1. A frozen 120/30 worker timing pair assembles an operational engine with TTL
   120, not the default 30.
2. Other valid non-default timing pairs remain valid.
3. Invalid heartbeat/engine-TTL wiring fails before any lease, claim, journal
   dispatch, or external call.
4. The exact crash-after-mutating-claim/before-write window survives SQLite
   close/reopen.
5. Expired mutating work enters reconciliation first.
6. `PROVEN_NOT_APPLIED` permits exactly one later trigger write.
7. `APPLIED` produces no duplicate and `UNRESOLVED` fails closed.
8. Resume preserves identities and does not reapply start.
9. Status recommends `resume` only when it is safe; an active old lease is not
   raced.
10. Existing timer, two-worker, stale-authority, abort, privacy, protected
    artifact, and Phase 16.1 A/B regressions remain green.
11. Tests make no real external calls and do not access the live acceptance
    repository or live XDG state.

Avoid weak assertions such as only checking that a method returned or that a
count is nonnegative. Assert durable state, identities, exact external mutation
counts, and reopen behavior.

## Validation

Run focused validation first:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_8_supervisor_entry.py \
  tests/integration/test_phase16_8_supervisor_restart.py \
  tests/integration/test_phase16_8_worker_supervisor_matrix.py \
  tests/integration/test_phase16_8_fault_matrix.py \
  tests/integration/test_phase16_8_control_matrix.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/pr_review_v2
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py
```

Then run the full repository validation:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
git status --short
```

Cursor must report exact results and any skips. Cursor must not stage, commit,
push, start/resume Gate B, or access real integrations.

## Risks Or Recovery Notes

- The existing live run must be preserved. Its failed supervisor left an
  expired mutating claim, which is precisely the state the recovery path is
  designed to reconcile.
- Starting a new run or manually posting the trigger would bypass the evidence
  this correction must validate and could create duplicates.
- Simply changing the global engine default from 30 to 120 would hide the
  assembly defect and break other callers; the TTL must come from the frozen
  run context.
- Validating only inside `LeaseRenewalCoordinator` is too late because the
  effect has already been claimed.
- A status recommendation of `resume` while old authority is still live could
  create an unnecessary race; tests must distinguish live and expired leases.
- If implementation uncovers a mismatch that requires a persisted schema
  change, a new public configuration field, or a different recovery protocol,
  stop and record it as an `OpenQuestion`.

## OpenQuestions

None.
