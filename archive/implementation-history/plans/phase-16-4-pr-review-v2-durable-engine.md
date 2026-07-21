# Phase 16.4 - PR Review v2 Durable SQLite Engine

Status: proposed for approval

Depends on:

- Phase 16.3 being accepted, committed, and merged as the clean baseline;
- the Phase 16.3 pure domain and reducer remaining the authoritative workflow
  decision boundary; and
- the Phase 16.1 A/B regression barrier remaining intact.

Entry conditions are satisfied on 2026-07-21:

- `main` contains Phase 16.3 at merge commit `f266565`;
- the user instructed Codex to proceed with the Phase 16.4 plan after the documented
  Phase 16.3 independent-review environment residual was presented, which records
  explicit acceptance of that residual as the Phase 16.4 entry condition; and
- the current Phase 16.3 handoff reports zero actionable findings, 202 focused domain
  tests, 12 A/B barrier tests, and 1042 executor-side full-suite tests passing.

The older lines in the Phase 16.3 handoff that still describe Phase 16.3 as staged and
unmerged are historical snapshot text. Current Git state and merged code are authoritative.

## Goals

Execute the PR review v2 pure state machine over a durable, isolated SQLite engine
without integrating any real GitHub, Git, Cursor, Codex, or local-fix side effect.

This phase must:

- add a versioned SQLite v1 schema for PR review v2 runs, event journal, effect
  outbox, durable timers, worker leases, and migration metadata;
- keep the Phase 16.3 discriminated state/event/effect models and
  `reduce_pr_review()` as the sole workflow decision authority;
- atomically persist each accepted event, snapshot/version update, timer changes,
  and all emitted effects in one transaction;
- audit rejected and stale submissions without changing workflow state/version or
  creating effects;
- deduplicate retried submissions and emitted work after unknown commit outcomes;
- claim and finalize effects under a current unexpired per-run lease with monotonic
  generation fencing;
- reconstruct pending work, retry eligibility, claims, and status from SQLite alone
  after closing and reopening the database;
- recover expired claims conservatively according to effect classification, never
  blindly repeating an ambiguous write or local execution;
- make v2 abort atomic with event reduction, outbox/timer cancellation, lease
  invalidation, and late-result fencing;
- expose a typed read-only application status DTO, without adding a public CLI
  command yet;
- provide a bounded worker/executor port exercised only with deterministic fakes;
  and
- prove crash, restart, duplicate, timer, concurrency, fencing, privacy, and abort
  guarantees with real temporary SQLite databases.

## Non-Goals

- Do not implement GitHub reads, GraphQL, `gh`, review polling adapters, error/header
  parsing, or rate-limit backoff calculation. Phase 16.5 owns the read gateway.
- Do not implement GitHub/Git writes, publication, commit, push, PR creation/update,
  replies, thread resolution, or real reconciliation queries. Phase 16.6 owns those
  idempotent gateways.
- Do not invoke Cursor, Codex, the extracted local review loop, or any real model.
  Phase 16.7 owns application adapters for those effects.
- Do not add or activate `pr-review-v2` CLI commands, daemon management, public
  status routing, legacy cutover, or production worker launch.
- Do not add public project configuration for database paths, lease duration,
  backoff, retry headers, or worker concurrency. Phase 16.4 uses constructor-injected
  runtime values and safe internal defaults; later integration phases may expose
  approved configuration.
- Do not migrate or reinterpret legacy Phase 15 PR-review runs, legacy `RunState`,
  JSON run directories, or A/B run history.
- Do not remove, clean up, or redirect legacy PR-review code. Cutover and cleanup
  remain Phase 16.9 work.
- Do not add an external FSM, ORM, migration framework, queue, scheduler, or durable
  workflow dependency. Use Python 3.11+, Pydantic, and stdlib `sqlite3`.
- Do not claim end-to-end PR review v2 operation. This phase executes only fake
  effects and stops at the durable engine boundary.

## Scope

Add an isolated application/infrastructure/worker slice under:

```text
src/ai_dev_loop/pr_review_v2/
  application/
    __init__.py
    contracts.py
    engine.py
    status.py
  infrastructure/
    __init__.py
    paths.py
    runtime.py
    sqlite_store.py
    migrations/
      0001_initial.sql
  workers/
    __init__.py
    effect_worker.py
```

The exact split among the named new modules may be adjusted to avoid a demonstrated
import cycle, but preserve these boundaries:

- `domain/`: pure workflow vocabulary and reducer;
- `application/`: orchestration, typed receipts/status, ports, and recovery policy;
- `infrastructure/`: SQLite, XDG paths, system clock/ID adapters, migration bootstrap;
- `workers/`: bounded execution of one fake/port-backed effect claim.

Add focused tests under:

```text
tests/unit/pr_review_v2/
  test_durable_contracts.py
  test_sqlite_schema.py
  test_durable_status.py

tests/integration/
  test_phase16_4_durable_engine.py
  test_phase16_4_crash_restart.py
  test_phase16_4_leases_and_fencing.py
```

Names may be consolidated when that makes fixtures clearer, but unit and real-SQLite
integration evidence must remain distinct.

Add the implementation handoff:

```text
archive/implementation-history/findings/phase-16-4-to-16-5-handoff.md
```

The implementation may update `src/ai_dev_loop/pr_review_v2/__init__.py` only to
export a deliberately small v2 application API. It should not widen the Phase 16.3
domain package root unnecessarily.

## Out of Scope

Cursor must not modify:

- `src/ai_dev_loop/state.py`, `src/ai_dev_loop/schemas/run-state-v1.json`, legacy
  `state.json` persistence, manifests, or legacy run discovery;
- `src/ai_dev_loop/local_review_loop.py`, `workflow_engine.py`, `iterations.py`,
  `resume_planner.py`, or any A/B start/resume/recover implementation;
- `src/ai_dev_loop/commands/pr_review*.py`, `pr_review_worker.py`,
  `external_adjudication.py`, `github_pr_review_result.py`, or legacy PR status;
- `src/ai_dev_loop/runners/github.py`, `publish.py`, `codex_github.py`, Git runners,
  Cursor/Codex runners, or subprocess helpers;
- `src/ai_dev_loop/config.py`, `src/ai_dev_loop/schemas/project-config-v1.json`,
  `ai_dev_loop.yaml`, CLI routing/help, or public documentation that claims v2 is
  operational;
- `.cursor/rules/`, `.cursor/skills/`, `.agents/skills/`, integrations, hooks,
  SessionStart assets, or the Codex session bridge;
- existing Phase 16.3 domain behavior merely to simplify persistence. If a concrete
  domain defect blocks 16.4, stop and request a plan amendment rather than weakening
  a validator or adding an infrastructure-owned transition; or
- Git history, staging policy, remotes, branches, user-global integrations, target
  repository contents, or real external services.

`src/ai_dev_loop/paths.py` should be reused, not changed: the new v2 infrastructure
path helper composes `state_dir()`, `ensure_dir()`, and
`set_sensitive_file_mode()` inside the new package. If any out-of-scope production
file appears necessary, stop before editing it and request review of the boundary.

## Required Context

Read before implementation:

1. `pr-review-v2-restructure-context.md` in full, especially SQLite, effect
   protocol, retries, observability, and Phase 16.4 sections.
2. `archive/implementation-history/findings/phase-16-3-to-16-4-handoff.md` in full.
3. `archive/implementation-history/plans/phase-16-3-pr-review-v2-domain-and-reducer.md`.
4. `src/ai_dev_loop/pr_review_v2/domain/common.py`, `effects.py`, `events.py`,
   `state.py`, `reducer.py`, and `domain/__init__.py`.
5. All tests under `tests/unit/pr_review_v2/`; they are executable contracts for
   the persisted union and reducer behavior.
6. `src/ai_dev_loop/paths.py` and its tests for XDG and permission conventions.
7. `src/ai_dev_loop/locking.py` only as evidence of legacy advisory locking. Do not
   use `fcntl` locks as durable lease or fencing authority.
8. `src/ai_dev_loop/redaction.py`, current privacy docs, and state/schema rules for
   safe summaries and sensitive artifact boundaries.
9. `tests/conftest.py` fixtures for isolated XDG roots and permission checks.
10. `tests/integration/test_phase16_1_ab_regression_barrier.py` for the protected
    legacy/A-B boundary.
11. Current `/docs`, `archive/implementation-history/master-plan.md`, and current
    code/tests where they define stronger active behavior than archived history.

No existing production SQLite convention exists in this repository. The schema and
transaction protocol in this plan are therefore authoritative for the new v2 database.

## Cursor Rules And Skills

All repository Cursor rules are `alwaysApply`; follow all of them:

- `.cursor/rules/ai-dev-loop-governance.mdc`;
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`; and
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`.

There is no repository-root `AGENTS.md` and no repo-local `.cursor/skills/`
directory.

The external `ai_dev_loop` orchestrator owns post-implementation review through:

- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`.

Cursor must not invoke that review skill, run real Codex/Cursor activity, or modify
it. Cursor implements and validates the phase; the orchestrator later presents the
staged diff to the exact resumed Codex reviewer.

## Architecture Guardrails

### 1. One workflow authority

- Persisted `PrReviewState` is the only lifecycle source of truth.
- Every lifecycle change, including start, retry timer, abort, expired mutating
  claim recovery, and expired local claim pause, must be represented by a typed
  `PrReviewEvent` and passed through `reduce_pr_review()`.
- The engine/store may enforce transactional fences and classify journal
  disposition, but must not reproduce the reducer transition table or directly
  manufacture replacement states.
- Parse every persisted state/event/effect payload through the Phase 16.3 public
  adapters on both write and read. Never use `model_construct`, unchecked dicts,
  or relaxed `extra="forbid"` behavior.

### 2. Layer and import boundaries

- `domain/` remains independent of application, infrastructure, workers,
  `sqlite3`, paths, clocks, IDs, and I/O.
- `application/` may import the domain and its own ports/contracts, but not legacy
  PR commands or A/B lifecycle state.
- `infrastructure/` implements application ports and may import stdlib `sqlite3`,
  shared XDG path/permission helpers, and domain adapters.
- `workers/` depends on application ports and domain effects; it contains no real
  GitHub/Git/Cursor/Codex executor.
- No v2 module imports legacy `RunState`, `GithubPrReviewState`, legacy recovery,
  legacy status, GitHub gateway, or command modules.

### 3. Database location and permissions

- Default database directory:
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/` or the platformdirs equivalent.
- Default database file:
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/engine.sqlite3`.
- The database is global to PR review v2, not stored inside a target repository,
  legacy `runs/` directory, or Codex/Windows SQLite location.
- Accept an explicit database path in the store constructor for tests and future
  application wiring.
- Create managed directories as `0700` and apply `0600` to the database and known
  `-wal`/`-shm` sidecars where supported. The containing `0700` directory is also
  mandatory protection against sidecar exposure.
- Never open, inspect, copy, link, or migrate a Codex SQLite database.

### 4. SQLite connection and transaction policy

- Use stdlib `sqlite3` only.
- Own one connection per store operation/worker thread or process; never share one
  mutable global connection across threads.
- Configure every connection with:
  - `foreign_keys=ON`;
  - `journal_mode=WAL`;
  - `synchronous=FULL`;
  - a bounded `busy_timeout` (default 5000 ms, constructor-injectable); and
  - explicit transaction control (`isolation_level=None`).
- Every mutating operation uses `BEGIN IMMEDIATE`, explicit commit, and rollback on
  any exception. Reads use read transactions/snapshots as needed.
- Do not catch a SQLite error and continue with a partially applied logical
  operation. Surface typed safe application errors and preserve the database.

### 5. Time, identity, and deterministic testing

- Store timestamps as one canonical, timezone-aware UTC text form with fixed-width
  microseconds so SQL ordering equals instant ordering. Validate conversion with
  `coerce_utc_instant` at boundaries.
- Inject a `Clock` port. Production infrastructure may use system UTC; tests use a
  deterministic fake. Do not call legacy `utc_now()` inside the engine/store.
- Require caller-stable submission IDs for externally supplied application events.
  Inject factories for internal timer/recovery IDs and worker/claim identities.
- Do not generate new domain `effect_id` or `idempotency_key` values in the durable
  engine. Preserve exactly what the reducer emitted.
- A retried call after an unknown commit outcome must reuse the same submission ID.

### 6. Privacy and artifact policy

- SQLite may contain complete validated domain state/event/effect payloads because
  raw prompts, patches, replies, outputs, and GitHub bodies are represented only by
  `ArtifactRef` path/hash values.
- Never persist auth tokens, API keys, full environments, full model transcripts,
  raw agent output, patch bodies, prompt bodies, comment bodies, or arbitrary
  transport payloads.
- Persist only typed `ErrorSummary`/safe rejection fields. Apply existing redaction
  to any infrastructure-generated safe diagnostic before storage.
- Human/status DTOs expose safe summaries and shortened SHA/session-like values;
  they must not expose lease owner identity, PID identity, raw JSON payloads, or
  sensitive artifacts.

### 7. No duplicate dispatches

- `effect_id` and `idempotency_key` are stable logical identities and intentionally
  survive retries and explicit resume batches.
- Do not use `(run_id, effect_id, attempt)` as the outbox primary key: explicit
  resume resets an effect to attempt 1 and would collide with historical attempt 1.
- Give each emitted outbox occurrence a durable `dispatch_id`, deterministically
  tied to `(source_event_id, effect_ordinal)`. Store logical effect ID,
  idempotency key, attempt, and full payload alongside it.
- Enforce one insertion per `(source_event_id, effect_ordinal)` and at most one live
  pending/claimed dispatch per run. Preserve historical dispatch rows.

### 8. Conservative recovery

- A claimed mutating effect whose lease expired is never returned directly to
  `pending`. Under a new current lease, apply a deterministic
  `WriteOutcomeUncertain` event for the exact original effect and emit only its
  reconciliation effect.
- Expired `READ_ONLY` and `RECONCILING` claims may be returned to eligibility with
  the same dispatch, effect identity, idempotency key, and attempt because they do
  not perform a write.
- Expired `LOCAL` claims are not automatically repeated. Apply `EffectBlocked` with
  `PauseReasonKind.REQUIRED_OPERATOR_ACTION` and
  `SafeActionKind.INSPECT_ARTIFACTS`, preserving the reducer's resumable state for
  explicit later action. Phase 16.7 may add artifact-specific recovery only under a
  new approved plan.
- If the expired dispatch does not exactly match the active validated state/effect,
  fail closed as corruption. Do not guess from logs or partial payloads.

### 9. Phase boundary

- The worker is a bounded application service/port demonstration, not a daemon or
  public command.
- Automated tests use fake effect executors only. No network, Git, target-repository
  mutation, subprocess, Cursor, Codex, or credentials are allowed.
- Keep legacy A/B and PR review behavior byte-for-byte compatible. If shared A/B or
  legacy files change, stop, explain why isolation failed, and request plan review.

## Durable Schema v1

Implement `infrastructure/migrations/0001_initial.sql` and a migration runner with
an atomic v0-to-v1 bootstrap. Use `PRAGMA user_version = 1` plus a migration audit
table. Reject a database whose `user_version` is newer than supported. If a v0
database is non-empty or contains a partial/corrupt expected schema, fail closed;
never drop or recreate tables automatically.

### `pr_review_schema_migrations`

- `version INTEGER PRIMARY KEY CHECK (version > 0)`
- `name TEXT NOT NULL`
- `checksum TEXT NOT NULL`
- `applied_at TEXT NOT NULL`

The migration checksum is computed from the packaged SQL. A current-version
database whose recorded checksum or required table/index set differs fails closed.

### `pr_review_runs`

- `run_id TEXT PRIMARY KEY`
- `state_kind TEXT NOT NULL`
- `state_payload TEXT NOT NULL`
- `state_payload_sha256 TEXT NOT NULL`
- `version INTEGER NOT NULL CHECK (version >= 1)`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

`state_kind` is an indexed projection for discovery only; the parsed state payload
is authoritative and must match it. Do not add a competing lifecycle/status column.

### `pr_review_events`

- `event_id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id)`
- `sequence INTEGER NOT NULL CHECK (sequence >= 1)`
- `event_kind TEXT NOT NULL`
- `event_payload TEXT NOT NULL`
- `event_payload_sha256 TEXT NOT NULL`
- `disposition TEXT NOT NULL CHECK (disposition IN ('accepted','rejected','stale'))`
- `expected_run_version INTEGER`
- `observed_run_version INTEGER NOT NULL`
- `resulting_run_version INTEGER`
- `rejection_code TEXT`
- `safe_detail TEXT`
- `created_at TEXT NOT NULL`
- `UNIQUE (run_id, sequence)`

Accepted rows require `resulting_run_version = observed_run_version + 1` and null
rejection fields. Rejected/stale rows require no resulting version. Enforce feasible
shape checks in SQL and complete cross-field validation in typed code.

A duplicate call with the same `event_id`, run ID, and payload hash returns the prior
receipt and inserts no row/effect. Reuse of an event ID with different run/payload is
an explicit collision error, not a duplicate.

### `pr_review_effects`

- `dispatch_id TEXT PRIMARY KEY`
- `source_event_id TEXT NOT NULL REFERENCES pr_review_events(event_id)`
- `effect_ordinal INTEGER NOT NULL CHECK (effect_ordinal >= 0)`
- `run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id)`
- `effect_id TEXT NOT NULL`
- `idempotency_key TEXT NOT NULL`
- `effect_kind TEXT NOT NULL`
- `effect_payload TEXT NOT NULL`
- `effect_payload_sha256 TEXT NOT NULL`
- `classification TEXT NOT NULL`
- `attempt INTEGER NOT NULL CHECK (attempt >= 1)`
- `max_attempts INTEGER NOT NULL CHECK (max_attempts >= attempt)`
- `status TEXT NOT NULL`
- `available_at TEXT NOT NULL`
- `claimed_run_version INTEGER`
- `claim_id TEXT`
- `claim_owner_id TEXT`
- `claim_lease_generation INTEGER`
- `claimed_at TEXT`
- `completed_at TEXT`
- `last_error_kind TEXT`
- `last_error_summary TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `UNIQUE (source_event_id, effect_ordinal)`

Use an explicit typed status vocabulary containing at least:

```text
pending, claimed, succeeded, retry_wait, uncertain,
blocked, result_rejected, cancelled, superseded
```

Add indexes for run/effect history, eligible pending work
`(status, available_at, run_id)`, and current claims. Add a partial unique index that
permits at most one `pending` or `claimed` dispatch per run. Validate every column
projection against the parsed effect payload before insert/read.

### `pr_review_timers`

- `timer_id TEXT PRIMARY KEY`
- `source_event_id TEXT NOT NULL REFERENCES pr_review_events(event_id)`
- `run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id)`
- `timer_kind TEXT NOT NULL CHECK (timer_kind = 'retry_due')`
- `due_at TEXT NOT NULL`
- `target_effect_id TEXT NOT NULL`
- `expected_run_version INTEGER NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('pending','fired','cancelled','superseded'))`
- `fired_event_id TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Add an eligibility index and a partial unique index permitting one pending retry
timer per run. Timer identity is deterministic from the accepted source event. Normal
bot observation `not_before` remains outbox `available_at`, not a retry timer and not
retry budget consumption.

### `pr_review_worker_leases`

- `run_id TEXT PRIMARY KEY REFERENCES pr_review_runs(run_id)`
- `owner_id TEXT`
- `generation INTEGER NOT NULL CHECK (generation >= 0)`
- `status TEXT NOT NULL CHECK (status IN ('inactive','active','expired','aborted'))`
- `acquired_at TEXT`
- `heartbeat_at TEXT`
- `expires_at TEXT`
- `updated_at TEXT NOT NULL`

Preserve the row and generation after release, expiry, or abort. Every acquire or
reacquire increments generation exactly once; heartbeat extends expiry without
changing generation. Do not expose `owner_id` in default status output.

## Application Contracts

Define frozen typed contracts/enums for:

- event submission and application receipt;
- accepted/rejected/stale/duplicate disposition;
- lease acquisition/heartbeat/release result;
- effect claim, including `dispatch_id`, `claim_id`, parsed effect,
  `EffectCompletionToken`, owner, generation, and expiry;
- timer firing receipt;
- safe store/engine error categories; and
- read-only `PrReviewStatus`.

The public Phase 16.4 application surface should be small and equivalent to:

```python
PrReviewEngine.create_run(...)
PrReviewEngine.apply_event(...)
PrReviewEngine.acquire_lease(...)
PrReviewEngine.heartbeat_lease(...)
PrReviewEngine.release_lease(...)
PrReviewEngine.recover_expired_claims(...)
PrReviewEngine.claim_next_effect(...)
PrReviewEngine.complete_claim(...)
PrReviewEngine.fire_due_timers(...)
PrReviewEngine.abort_run(...)
PrReviewEngine.get_status(...)
```

Names may improve, but do not expose raw SQLite connections or untyped dictionaries as
the normal application API.

## Transaction Protocols

### Create run

In one `BEGIN IMMEDIATE` transaction:

1. validate the supplied initial state through `PR_REVIEW_STATE_ADAPTER`;
2. require its `run_id` to equal the requested run ID;
3. insert snapshot version 1 with no journal event or outbox work;
4. initialize an inactive lease row at generation 0; and
5. commit.

Duplicate run IDs fail safely. Do not reinterpret an existing run as an idempotent
create unless an exact later API explicitly requests lookup.

### Apply ordinary event

For `apply_event(submission_id, run_id, expected_version, event)`:

1. enter `BEGIN IMMEDIATE`;
2. deduplicate `submission_id` by exact run/payload hash;
3. read and validate the current snapshot and version;
4. allocate the next per-run journal sequence inside the transaction;
5. if `expected_version` differs, append `stale`, commit, and return no state/effects;
6. call `reduce_pr_review(current_state, event)`;
7. on `TransitionRejected`, append `rejected`, commit, and do not update snapshot,
   version, timers, leases, or effects;
8. on `TransitionApplied`, append `accepted`, CAS-update the snapshot from the
   observed version to `version + 1`, reconcile retry timers, insert every emitted
   effect dispatch once, and commit; and
9. return a typed receipt. Never return mutable internal row objects.

The snapshot CAS must affect exactly one row. Zero or multiple rows is corruption or
concurrency failure and rolls back.

### Effect insertion and eligibility

- Set an emitted dispatch's expected completion run version to the resulting snapshot
  version.
- `ObserveBotReviewEffect.not_before` becomes `available_at`; other emitted effects
  are immediately eligible at the accepted event time unless the domain supplies a
  later eligibility value.
- A transition into `WaitingRetryState` inserts one pending `retry_due` timer for the
  exact `next_attempt_at`. Leaving that state cancels/supersedes any pending timer.
- Historical effect/timer rows are never deleted to make retry easier.

### Lease acquire and heartbeat

- Acquire/reacquire in one transaction after reading canonical UTC `now` from the
  injected clock.
- If another owner has an unexpired active lease, reject without mutation.
- Otherwise increment generation, set the caller's validated opaque owner ID, and
  set heartbeat/expiry from an injected positive lease TTL.
- Heartbeat and release require exact run, owner, generation, active status, and
  non-expiry. A stale heartbeat/release changes nothing.
- Acquiring a new generation fences every earlier claim/result even when owner text
  is reused.

### Claim effect

In one transaction:

1. verify exact current active lease owner/generation and non-expiry;
2. verify the run is nonterminal and not aborted/paused without an active effect;
3. recover any expired prior claim according to the conservative policy before
   selecting new work;
4. select exactly one `pending` dispatch whose `available_at <= now` and whose
   parsed effect equals the active state effect;
5. atomically mark it `claimed`, store a new `claim_id`, current run version, owner,
   lease generation, claim/expiry times; and
6. return an `EffectCompletionToken` constructed from persisted run version,
   generation, effect ID, cycle, and bound head SHA.

Two independent connections racing to claim must produce one winner. Never execute
an effect before the claimed row is committed.

### Complete effect claim

Completion takes `dispatch_id`, `claim_id`, owner/generation, a stable submission ID,
and a typed domain result event. In one transaction, before reducer application:

1. load and validate run snapshot and dispatch payload;
2. verify dispatch is still `claimed` by that exact claim/owner/generation;
3. verify current active lease is exact and unexpired;
4. verify claimed run version equals current run version and the event token's
   `expected_run_version`;
5. verify effect ID, cycle, bound head SHA, attempt, and active-state effect identity;
6. stale-fence any mismatch by journaling `stale` only; and
7. otherwise apply the event through the ordinary atomic reducer path, update the
   completed dispatch status according to event/result, and insert successor work in
   the same transaction.

A reducer rejection is journaled `rejected`, creates no state/effect change, and does
not silently accept the claim. Keep sufficient safe status for diagnosis; do not
convert invalid trusted output into success. The same current worker may submit a
new, uniquely identified corrected typed result while its lease/claim remains valid.

### Expired claim recovery

Recovery requires a newly acquired current lease generation and one transaction per
run:

- `MUTATING`: create a deterministic recovery submission/reconciliation identity
  from run, dispatch, attempt, and expired generation; apply
  `WriteOutcomeUncertain` with the exact original effect and a safe timeout summary;
  mark the old dispatch `uncertain`; insert only the emitted reconciliation dispatch.
- `READ_ONLY` or `RECONCILING`: clear old claim fields, restore the same dispatch to
  `pending`, preserve effect/idempotency/attempt, and keep original eligibility no
  later than recovery time.
- `LOCAL`: apply a typed `EffectBlocked` with required operator inspection; mark the
  dispatch `blocked`; emit no replacement execution.

Do not recover a claim while the old lease remains current and unexpired. Late old
worker completions are journaled stale and cannot mutate state/outbox.

### Fire durable retry timer

- Query due pending timers by canonical UTC `due_at`, then process each under its own
  `BEGIN IMMEDIATE` transaction.
- Recheck timer status, current run version, and exact `WaitingRetryState` target.
- If superseded, mark only the timer `superseded` and emit no work.
- Otherwise use a deterministic event ID from `timer_id`, apply `RetryDue` through the
  reducer, atomically mark the timer fired, advance snapshot/version, and insert the
  emitted retry dispatch.
- Crash before commit leaves the timer pending; retry after an unknown post-commit
  outcome deduplicates by deterministic event ID.

### Abort

`abort_run()` reads the current version and applies `AbortRequested` under one
`BEGIN IMMEDIATE` transaction so abort can serialize after a just-finished event
without requiring the caller to guess a version. When applied, atomically:

1. journal the abort event;
2. persist `AbortedState` and increment run version;
3. mark all pending/claimed/retry-wait dispatches cancelled without deletion;
4. cancel all pending timers; and
5. mark the lease aborted/inactive while preserving and fencing its generation.

If abort commits first, an in-flight completion fails run-version/lease fencing. If a
completion commits first, abort applies to the resulting current state. Terminal-state
abort rejection is audited and does not rewrite the terminal snapshot.

## Status Contract

`get_status(run_id)` is read-only and reconstructs a typed `PrReviewStatus` from the
validated snapshot plus outbox/timer/lease rows. Include at least:

- run ID, state kind, run version, cycle number, and updated time;
- repository/PR identity when bound and a shortened head SHA;
- active or pending effect kind, status, attempt/max attempts, and next eligibility;
- safe last error kind/summary when present;
- lease active/liveness boolean, generation, heartbeat, and expiry, but not owner ID
  or PID;
- whether an ambiguous write/reconciliation is pending;
- typed safe action/condition for paused or waiting-for-user states; and
- a stable machine-readable next-action category.

Do not add CLI rendering in this phase. Tests consume the DTO directly.

## Bounded Worker Port

Define an `EffectExecutor` protocol that accepts a parsed claimed effect and completion
token and returns one typed domain completion/failure/block/uncertain event. Provide a
bounded worker service that can:

1. acquire/heartbeat a lease;
2. recover expired claims;
3. claim at most one eligible effect;
4. commit the claim before calling the executor outside the transaction;
5. complete the exact claim through the fenced engine API; and
6. return a typed step result.

Production code includes no concrete external executor. Automated tests provide
deterministic fakes that record calls and return predefined events. No background
thread, daemon loop, sleep, signal handling, or subprocess is required.

## Implementation Plan

### 1. Confirm and record the accepted baseline

- Confirm clean Phase 16.3 merge `f266565`, current branch, Python/Pydantic versions,
  test collection, and absence of pre-existing SQLite v2 files in source.
- Record the accepted Phase 16.3 environmental residual and stale historical handoff
  lines accurately in the Phase 16.4 handoff.
- Inventory imports before editing and preserve unrelated user changes.

### 2. Add application contracts and deterministic ports

- Add typed frozen DTOs/enums for submissions, receipts, leases, claims, worker step
  results, safe errors, and status.
- Add injectable clock and ID/fault-hook ports plus no-op/default implementations in
  infrastructure.
- Add canonical UTC encode/decode helpers that validate through Phase 16.3 time
  contracts.
- Unit-test invalid identity/time/status shapes and deterministic IDs.

### 3. Add secure XDG path and SQLite bootstrap

- Add v2-only path helpers composing shared XDG/permission utilities.
- Add migration SQL, checksum/version verification, secure database creation, PRAGMA
  setup, connection ownership, busy handling, and fail-closed future/corrupt schema
  behavior.
- Test empty bootstrap, reopen current v1, future version rejection, partial schema,
  checksum drift, constraints/indexes, WAL behavior, and permissions where supported.

### 4. Implement typed persistence and journal/outbox primitives

- Serialize/parse all domain payloads only through public adapters.
- Implement run creation, snapshot loading, sequence allocation, journal rows,
  dispatch insertion, timer reconciliation, payload hashes, and duplicate event
  receipts.
- Enforce exact row/payload projection equality and source-event/ordinal uniqueness.
- Test every state/event/effect variant round-trips after close/reopen.

### 5. Implement the atomic application engine

- Implement ordinary event application with version CAS and reducer call inside the
  transaction.
- Journal accepted/rejected/stale dispositions exactly as specified.
- Insert emitted dispatches and retry timers in the same commit.
- Add named fault-hook checkpoints around read, journal, snapshot CAS, timer/outbox
  insert, pre-commit, and post-commit for deterministic crash tests.
- Prove rollback and submission-ID recovery after unknown commit outcomes.

### 6. Implement leases, claims, fencing, and completion

- Implement monotonic acquire/reacquire, heartbeat, release, expiry, and generation
  checks.
- Implement eligible claim selection and claim token construction.
- Implement completion fencing across run version, dispatch/claim, owner/generation,
  effect ID, attempt, cycle, and bound head SHA before reducer application.
- Audit stale completions without changing snapshot/version/outbox.
- Test two-store races, expired old workers, duplicate completions, and every stale
  fence independently.

### 7. Implement conservative expired-claim recovery

- Implement mutating -> uncertain/reconcile, read-only/reconciling -> same dispatch
  requeue, and local -> paused/operator-inspection behavior.
- Make recovery event/reconciliation IDs deterministic and restart-idempotent.
- Test crash-after-claim for every classification and prove no duplicate mutating or
  local executor call.

### 8. Implement durable timers and abort

- Implement due-timer scan/fire with exact state/version recheck and deterministic
  timer event IDs.
- Prove retry attempt/budget and absolute eligibility survive database reopen.
- Implement atomic abort cancellation and lease invalidation.
- Test abort-before-completion and completion-before-abort races using independent
  connections/barriers.

### 9. Implement read-only status and bounded fake worker path

- Build the typed status DTO from SQLite without legacy state/discovery imports.
- Add the `EffectExecutor` port and one-step worker service.
- Drive representative fake workflows through pending, claimed, retry, uncertain,
  paused, completed, failed, and aborted states without external I/O.
- Verify status never leaks owner identity, raw payloads, prompts, patches, or unsafe
  error text.

### 10. Complete acceptance and handoff

- Run focused, A/B barrier, and full validation below.
- Write `phase-16-4-to-16-5-handoff.md` with exact schema version/path, public API,
  transaction/fencing protocol, test counts, blocked checks, residual risks, and Phase
  16.5 read-gateway entry contract.
- State honestly that only fake effects execute and no v2 CLI/gateway is operational.
- Leave all intended implementation changes staged for independent Codex review, but
  do not commit, push, reset, clean, stash, or invoke the review skill yourself.

## Testing Criteria

Automated tests are mandatory because this phase introduces persistence, migration,
concurrency, recovery, timers, and error handling.

### Unit: contracts, schema, serialization, and status

Prove:

- DTO/enums reject invalid IDs, statuses, non-UTC timestamps, unsafe summaries, and
  cross-field mismatches;
- canonical UTC text round-trips and sorts by actual instant;
- v1 migration/schema/index/checksum contract and future/corrupt DB rejection;
- XDG path isolation and `0700`/`0600` permissions where supported;
- every top-level and nested Phase 16.3 state/event/effect variant persists and
  validates after reopen;
- row projections cannot disagree with parsed payload kind/identity/attempt;
- status fields and next actions are correct and privacy-safe; and
- static architecture checks forbid domain-to-infrastructure imports and legacy PR/A-B
  coupling from the new v2 engine.

### Integration: real SQLite transactions

Use actual temporary database files, not mocked SQLite connections. Provide:

- an isolated XDG/database fixture;
- deterministic fake clock and ID sequences;
- named fault injector;
- fake executor that records exact calls and returns typed events; and
- two independent store/engine instances/connections with barriers for races.

Prove at least:

1. accepted event atomically journals, advances version/snapshot, creates timers, and
   inserts all emitted dispatches;
2. reducer rejection is audited with no snapshot/version/outbox/timer change;
3. stale expected version is audited with no lifecycle mutation;
4. same submission ID returns the prior receipt after reopen and creates no duplicate
   event/dispatch; different payload under the same ID fails;
5. fault injection before and after each logical commit boundary produces either the
   complete old state or complete new state, never a partial transition;
6. unknown post-commit failure is recovered by resubmitting the same ID;
7. two workers racing to claim produce one committed claim/executor call;
8. reacquired lease generation fences heartbeat, release, claim, and completion from
   the old generation;
9. stale run version, dispatch, claim ID, lease generation, effect ID, attempt, cycle,
   and head SHA each prevent state/outbox mutation;
10. pending, claimed, retry-waiting, uncertain, paused, completed, failed, and aborted
    state survives close/reopen with correct next action;
11. retry timer fires only at/after its UTC eligibility, once, and preserves attempt
    budget across restart;
12. normal observation `not_before` delays claim without consuming retry budget;
13. resume batch attempt 1 creates a new dispatch despite historical attempt 1 while
    preserving logical effect/idempotency identity;
14. expired mutating claim enters reconciliation and never directly re-executes the
    write;
15. expired read-only/reconciliation claim requeues the same dispatch and attempt;
16. expired local claim pauses for operator inspection and does not re-execute;
17. abort atomically cancels live work/timers, invalidates lease, and fences late
    completion;
18. abort/completion races serialize safely in both orderings;
19. no database value contains raw fake prompt/patch/body/token/environment content;
20. status is reconstructible from SQLite and read-only; and
21. a representative fully simulated workflow reaches each terminal/waiting outcome
    without network, Git, subprocess, Cursor, or Codex activity.

### Regression and static architecture

- Existing `tests/unit/pr_review_v2` remain green unchanged except for additive shared
  fixtures where justified.
- Phase 16.1 A/B regression barrier remains green.
- Full suite remains green.
- Domain architecture tests continue to prove no I/O/nondeterminism imports.
- New engine architecture tests prove no imports from legacy PR lifecycle, legacy
  state, commands, runners, local loop, or integration modules.

### Fake strategy

- No fake executable is needed because Phase 16.4 has no subprocess integration.
- The executor is an in-process typed fake implementing `EffectExecutor`.
- Clocks, IDs, lease owners, and faults are injected deterministic fakes.
- Concurrency tests use real SQLite files and independent connections/threads, not a
  mocked lock or monkeypatched claim function.
- Never use real credentials, repositories, GitHub, Cursor, or Codex.

## Validation

Before editing, record a focused baseline:

```bash
git status --short
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 --tb=line
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

During implementation, run focused checks:

```bash
uv run python -m ruff format --check \
  src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_*.py
uv run python -m ruff check \
  src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_*.py
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 tests/integration/test_phase16_4_*.py --tb=line
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

Then run full acceptance:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
uv run python -m build
git diff --check
```

Run no real model, GitHub, Git publication, global integration install, or target
repository mutation command. If a validation is blocked by environment, record the
exact command, failure class, focused evidence, and residual risk in the handoff; do
not claim it passed.

Phase 16.4 acceptance requires:

- schema bootstrap and current/future/corrupt version behavior proven;
- all atomicity, deduplication, restart, lease, fencing, timer, recovery, abort, and
  privacy criteria above proven with real SQLite files;
- zero duplicate write/local execution paths after expired claims;
- no weakening or duplication of the Phase 16.3 reducer;
- no real external side effects;
- focused suite, A/B barrier, static checks, and full acceptance green or an explicit
  independently reviewed environment residual;
- no control-plane files changed; and
- an accurate Phase 16.4-to-16.5 handoff.

## Risks Or Recovery Notes

- **Stable effect identity is not dispatch identity.** Resume can reset an existing
  logical effect to attempt 1. Preserve historical dispatches with a separate
  source-event-derived `dispatch_id`; never overwrite prior attempt rows.
- **WAL is not the transaction model.** Correctness comes from explicit
  `BEGIN IMMEDIATE`, CAS, constraints, and idempotent submission IDs. WAL only
  improves local reader/writer behavior.
- **Unknown commit outcome is normal.** Callers must retry with the same submission
  ID and receive the durable prior receipt. Never generate a fresh ID merely because
  the caller did not observe the commit response.
- **SQLite busy is not reducer retry.** A bounded busy/locked database error is a safe
  infrastructure failure; it must not consume GitHub effect attempt budget or create
  `EffectRetryableFailure` automatically.
- **Lease expiry does not prove an effect was not applied.** Mutating work becomes
  uncertain/reconciling, read-only/reconciling work may be reclaimed, and local work
  pauses. Do not choose a more permissive rule for test convenience.
- **Do not create nested write reconciliation.** An expired reconciliation claim is
  read-only and may be reclaimed; it must not generate another reconcile-write
  around itself.
- **Do not calculate GitHub backoff here.** Persist and schedule absolute eligible
  times supplied by typed events. Gateway retry headers/jitter policy arrive later.
- **No process daemon exists yet.** The bounded worker proves ordering and fencing;
  Phase 16.7 or a later explicit plan owns process lifecycle and public command
  wiring.
- **Status is not a second state machine.** Derive it from validated snapshot plus
  durable rows; never persist a competing human status.
- **Fail closed on schema/payload corruption.** Do not delete, rebuild, or silently
  migrate a corrupt database. Preserve it for manual inspection and return a safe
  error.
- **Preserve Phase 16.3 contracts.** If persistence reveals an actual missing domain
  event or invariant, stop and amend the plan instead of direct state mutation.
- **Preserve unrelated changes.** Do not reset, clean, checkout, stash, or overwrite
  user work. Do not commit or push.

## OpenQuestions

None.
