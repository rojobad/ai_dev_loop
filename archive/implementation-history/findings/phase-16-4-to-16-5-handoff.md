# Phase 16.4 -> Phase 16.5 Handoff

Date: 2026-07-21

## Executive status

Phase 16.4 added an isolated durable SQLite engine for PR review v2 that executes
the Phase 16.3 pure reducer over a versioned journal, effect outbox, retry timers,
and monotonic worker leases. No GitHub, Git, Cursor, Codex, subprocess, credential,
or model activity was introduced. Legacy PR review and the A/B loop remain untouched.

## Entry baseline

- Branch: `rba/phase_16-4` (Phase 16.3 present at merge commit `f266565` on ancestry)
- Python (uv): 3.11.15
- Pydantic: 2.13.4
- Pre-change focused baseline: **202** domain unit tests; **12** Phase 16.1 A/B barrier
  tests
- Accepted Phase 16.3 residual: the independent-review environment residual recorded in
  `phase-16-3-to-16-4-handoff.md` was explicitly accepted by the user on 2026-07-21 as
  the Phase 16.4 entry condition (see main Phase 16.4 plan)
- Historical note: older lines in the Phase 16.3 handoff that still describe Phase 16.3
  as staged/unmerged are stale snapshot text; merged `main` ancestry is authoritative

## Public application API

Package root additions under `ai_dev_loop.pr_review_v2`:

| Module | Role |
|---|---|
| `application/contracts.py` | Frozen DTOs/enums, ports (`Clock`, `IdFactory`, `FaultHook`, `EffectExecutor`) |
| `application/engine.py` | `PrReviewEngine` durable orchestration |
| `application/status.py` | Read-only `PrReviewStatus` projection |
| `infrastructure/paths.py` | XDG `pr-review-v2/` path helpers |
| `infrastructure/runtime.py` | UTC encode/decode, IDs, fault injector |
| `infrastructure/sqlite_store.py` | stdlib `sqlite3` store + migration bootstrap |
| `infrastructure/migrations/0001_initial.sql` | Schema v1 |
| `workers/effect_worker.py` | Bounded one-step fake-backed worker |

### Engine surface

```text
PrReviewEngine.create_run(...)
PrReviewEngine.apply_event(...)
PrReviewEngine.apply_fatal_failure(...)
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

Default database path:
`$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/engine.sqlite3` (constructor-injectable).

Schema: `PRAGMA user_version = 1` plus `pr_review_schema_migrations` checksum audit.

## Normative clarifications applied

Companion
`archive/implementation-history/plans/phase-16-4-pr-review-v2-durable-engine-clarifications.md`:

- Effect-result events (`EffectSucceeded`, `EffectRetryableFailure`, `EffectBlocked`,
  `WriteOutcomeUncertain`) enter only via `complete_claim()` or expired-claim recovery
- `RetryDue` enters only via `fire_due_timers()`
- `AbortRequested` enters only via `abort_run()`
- `FatalFailureDetected` enters via `apply_fatal_failure()`
- Reducer rejection on a fenced completion leaves the dispatch `claimed` (no
  `result_rejected` status)
- `create_run()` accepts only validated `PreparedState`

## Transaction / fencing protocol (summary)

- Every mutation uses `BEGIN IMMEDIATE` with explicit commit/rollback
- Accepted events atomically journal, CAS snapshot `version -> version+1`, reconcile
  retry timers, and insert outbox dispatches with separate `dispatch_id` identity
  (`dispatch:{source_event_id}:{effect_ordinal}`)
- Caller-stable `submission_id` deduplicates after unknown commit outcomes; payload/run
  collision fails closed
- Lease acquire increments generation; heartbeat extends expiry without changing
  generation; old generations are fenced on claim/complete/heartbeat/release
- Expired claims: mutating -> `WriteOutcomeUncertain` + reconcile only; read-only /
  reconciling -> same dispatch requeued; local -> `EffectBlocked` pause for operator
  inspection
- Terminal transitions cancel live dispatches/timers and invalidate the lease while
  preserving generation history

## Isolation

- Domain remains free of `sqlite3` / I/O / nondeterminism imports
- Application/infrastructure/workers forbid legacy PR / A/B lifecycle imports
- No changes to `state.py`, workflow engine, legacy PR commands, runners, config, CLI,
  control-plane rules/skills, or integrations

## Validation evidence

Focused Phase 16.4 + domain + A/B barrier after acceptance-matrix correction:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 tests/integration/test_phase16_4_*.py \
  tests/integration/test_phase16_1_ab_regression_barrier.py
=> 292 passed
```

Acceptance-matrix additions (2026-07-21 third correction):

- `tests/integration/test_phase16_4_acceptance_matrix.py` — reopen status/next-action
  matrix (pending, claimed, retry-waiting, uncertain/reconciling, paused, completed,
  failed, aborted); independent completion fences (stale run version, dispatch ID,
  claim ID, lease generation, effect ID, attempt, cycle, bound head SHA) with stale
  audit + unchanged snapshot/timers/outbox; deterministic abort/completion races via
  independent engines, fault-hook write-txn hold, and Event coordination covering both
  serialization orders; resume-batch
  attempt-1 new dispatch with stable effect/idempotency identity; expired RECONCILING
  claim requeue without write; reconstructible read-only status; fake-executor
  workflows to every required terminal/waiting outcome; `create_run` PreparedState-only.
- `tests/integration/test_phase16_4_variant_persistence.py` — every Phase 16.3
  top-level state, event, and effect kind serializes into real SQLite rows, validates,
  and reloads.
- `tests/integration/phase16_4_matrix_helpers.py` — shared drive/claim/fingerprint
  helpers for the matrix suite.

Correction-focused additions (earlier):

- `tests/integration/test_phase16_4_correction_findings.py` covers complete_claim
  accepted/stale post-commit retries, abort clock-advance idempotency with reason
  collision checks, historical duplicate receipts with validated effect rows,
  classification/max_attempts corruption fencing, generation-scoped worker completion
  identities, and reclaim-after-restart completion ID separation.
- `tests/unit/pr_review_v2/test_sqlite_schema.py::test_mid_migration_fault_rolls_back`
  proves v0-to-v1 bootstrap rollback leaves no partial schema.

Static and packaging (post-matrix / deterministic-race correction):

```text
uv run python -m ruff format --check .
=> 184 files already formatted
uv run python -m ruff check .
=> All checks passed
uv run python -m mypy src
=> Success: no issues found in 90 source files
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
=> 1120 tests collected
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
=> 1120 passed in 387.91s
uv run mkdocs build --strict
=> Documentation built
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m build
=> Successfully built ai_dev_loop-0.1.0.tar.gz and ai_dev_loop-0.1.0-py3-none-any.whl
git diff --check
=> clean
```

### Independent implementation review

The detached `ai_dev_loop` implementation/review run
`ai-dev-loop-20260721T155141Z-95cae0` completed all **5/5** review turns. Its final
disposition reported no actionable findings and no execution error. The Phase 16.4
changes remain staged for the user to accept and commit; no commit, push, merge, or
publication action was performed by the implementation run or while preparing this
handoff.

### Correction semantics (2026-07-21)

- `complete_claim()` deduplicates `submission_id` before fencing; post-commit retries
  return durable prior receipts for accepted and stale completions.
- Stale-completion receipts use the same journaled `expected_run_version` (token value)
  on both the returned receipt and the event row.
- `abort_run()` ignores regenerated `occurred_at` on same-ID retries but requires the
  stored abort `reason` to match; a changed reason is a typed collision.
- Migration DDL runs statement-by-statement inside one `BEGIN IMMEDIATE` (no
  `executescript` implicit commit).
- Effect row projections validate `classification`, `max_attempts`, and `run_id` before
  claim/recovery; duplicate accepted receipts re-validate historical effect rows.
- Worker completion IDs use
  `complete:{run_id}:{dispatch_id}:{claim_id}:gen-{lease_generation}:{result_key}` so
  recovered/reclaimed work under a new lease generation cannot collide with a late
  prior completion when claim-id factories reset.
- Accepted journal rows persist `resulting_state_payload` so duplicate receipts return
  the historical state, not the current snapshot.

No real GitHub, Git publication, Cursor, Codex, subprocess, credential, or model
activity was used.

### Test inventory (new)

Unit:

- `tests/unit/pr_review_v2/test_durable_contracts.py`
- `tests/unit/pr_review_v2/test_sqlite_schema.py`
- `tests/unit/pr_review_v2/test_durable_status.py`
- helpers: `tests/unit/pr_review_v2/durable_helpers.py`

Integration (real temporary SQLite files):

- `tests/integration/test_phase16_4_durable_engine.py`
- `tests/integration/test_phase16_4_crash_restart.py`
- `tests/integration/test_phase16_4_leases_and_fencing.py`
- `tests/integration/test_phase16_4_correction_findings.py`
- `tests/integration/test_phase16_4_acceptance_matrix.py`
- `tests/integration/test_phase16_4_variant_persistence.py`
- helpers: `tests/integration/phase16_4_matrix_helpers.py`

## Honest boundary

Phase 16.4 executes only fake in-process effect results. There is no public
`pr-review-v2` CLI, no daemon, no GitHub read/write gateway, and no Cursor/Codex
application adapters. Status is a typed DTO only.

## Phase 16.5 entry contract

Phase 16.5 owns the GitHub **read** gateway (GraphQL/`gh`, review polling adapters,
error/header parsing, rate-limit backoff calculation) wired behind the existing
effect protocol. Its production executor scope is the current `READ_ONLY` effect,
`ObserveBotReviewEffect`. It must:

- consume a claimed `ObserveBotReviewEffect` through the fenced `complete_claim`
  path only;
- never bypass lease/generation/run-version fencing;
- verify repository identity, PR number, expected full head SHA, cycle, poll sequence,
  and trigger marker before interpreting remote observations;
- return only typed domain results: `BotStillWaitingOutcome`,
  `VerifiedNoFindingsOutcome`, `EligibleThreadsObservedOutcome`,
  `EffectRetryableFailure`, or `EffectBlocked` as applicable;
- not introduce write/publication adapters (Phase 16.6) or local Cursor/Codex
  adapters (Phase 16.7);
- keep backoff absolute eligibility times as typed event fields for the durable
  engine to schedule, without calculating GitHub policy inside the SQLite layer.

`ReconcileWriteEffect` is deliberately **not** a Phase 16.5 execution target. Phase
16.5 may expose reusable, read-only GitHub primitives that Phase 16.6 can later use to
prove write outcomes, but the reconciliation executor and its Git/GitHub strategy
routing remain Phase 16.6 scope. In particular, strategies such as
`find_commit_at_head` and `find_remote_ref` are not GitHub polling concerns and must not
be pulled into 16.5 accidentally.

### Required Phase 16.5 behavior

1. **Read gateway boundary.** Introduce one isolated GitHub read gateway/transport and
   a narrow `EffectExecutor` adapter that validates the claimed effect is
   `ObserveBotReviewEffect`. Do not add raw database access or a second state machine.
2. **Structured transport.** Invoke `gh` with direct argument vectors, no shell,
   explicit working directory and timeout, separated stdout/stderr, structured JSON or
   GraphQL parsing, and redaction. Never infer lifecycle state from human-readable CLI
   prose.
3. **Observation surface.** Read only repository/PR identity and state, full head SHA,
   review threads, comments, reactions, thread resolution state, and configured bot
   trigger/review markers. No PR/comment/reaction/thread/ref mutation is permitted.
4. **Typed failures.** Distinguish authentication, permission, not-found, closed PR,
   repository/head/branch drift, timeout/network, rate limit, and temporary server
   failure. Permanent/operator-action conditions block; transient conditions produce a
   retry event with an absolute future `next_attempt_at`.
5. **Polling is not retry.** A successful read with no feedback is
   `BotStillWaitingOutcome` with the exact next poll sequence and absolute
   `next_not_before`; it must not consume the transient-failure retry budget.
6. **Backoff ownership.** Use the configured 10/30/90/180/300-second transient retry
   sequence with deterministic injectable +/-20% jitter. A trustworthy `Retry-After`
   or rate-limit reset takes precedence, subject to an explicit maximum anomalous-wait
   cap. Persist only the resulting absolute UTC eligibility time; SQLite remains
   policy-free.
7. **Evidence quality.** Empty or incomplete thread/comment results are not, by
   themselves, proof of no findings. `VerifiedNoFindingsOutcome` requires explicit,
   configured evidence. Eligible threads must be frozen against repository, PR, head
   SHA, cycle, and trigger marker, with sanitized artifact references and hashes rather
   than raw review bodies in durable state.
8. **Privacy and fixtures.** Tests use sanitized GraphQL/JSON fixtures, an injected
   transport or fake `gh` executable, deterministic clocks/randomness, and no real
   credentials, network, GitHub writes, model calls, Cursor, or Codex activity.

### Integration constraints the next agent must resolve in its plan

- **Honest workflow entry.** The normal reducer path reaches
  `ObserveBotReviewEffect` only after the mutating `RequestBotReviewEffect` succeeds.
  Because trigger writes are outside 16.5, integration tests must reach observation by
  completing the preceding fake write through the normal claimed-effect and
  `complete_claim()` path (or by loading a validated persisted fixture). They must not
  insert raw SQLite rows, weaken `PreparedState`-only bootstrap, or pretend a production
  trigger adapter exists.
- **Lease duration versus network duration.** `EffectWorker.run_once()` currently
  heartbeats only after executor completion. The 16.5 plan must either prove the
  external request timeout is safely below the lease TTL or add bounded lease-renewal
  behavior around the external read while retaining generation fencing. A long network
  call must not silently outlive its claim.
- **Snapshot artifacts.** Phase 16.4 persists validated artifact references/hashes but
  does not provide a production protected review-artifact writer. Before emitting
  `EligibleThreadsObservedOutcome`, the 16.5 plan must define atomic, sanitized artifact
  persistence (XDG/run-scoped path, directory `0700`, file `0600`, relative reference,
  content hash) or explicitly narrow acceptance so it does not claim a frozen snapshot
  that was never durably written.
- **Baseline gate.** Phase 16.5 implementation should start only from an accepted,
  committed/merged Phase 16.4 baseline with a clean index. The currently staged Phase
  16.4 state is review-complete but is not itself that baseline transition.

### Suggested Phase 16.5 acceptance matrix

- adopt/observe the configured PR at the expected full head SHA without performing a
  write;
- map waiting, verified-no-findings, and eligible-thread observations to the exact
  domain outcomes and persist them only through `complete_claim()`;
- preserve every completion fence (dispatch, claim, owner, lease generation, claimed
  run version, effect ID, attempt, cycle, and head SHA);
- survive restart after a claimed read and recover/requeue the same read-only dispatch;
- exercise five consecutive transient failures with deterministic backoff and then
  pause/block according to the approved retry budget, without any write;
- honor reliable server-directed delays and cap anomalous values;
- treat normal no-feedback polling separately from failures;
- block on auth/permission/not-found/closed-PR/head or repository drift and on malformed
  or contradictory response data;
- prove timeout/lease behavior and stale late-completion rejection;
- prove fixtures, logs, durable rows, status DTOs, and artifacts do not expose secrets,
  raw prompts, raw review bodies, or session identifiers;
- retain the Phase 16.3 domain suite, Phase 16.4 durability suite, A/B barrier, static
  checks, strict docs build, and package build.

## Residual risks

1. **No production worker daemon.** The bounded `EffectWorker` proves one-step
   ordering/fencing only; process lifecycle remains later-phase work.
2. **Historical review note only.** The Phase 16.3 independent-review environment
   residual remains in its source handoff for provenance. It is not an open Phase 16.4
   blocker: the Phase 16.4 full suite passed and the detached five-turn review completed
   without actionable findings.
3. **WAL sidecars** receive `0600` when present; containing directory `0700` is the
   mandatory protection against sidecar exposure on chmod-capable filesystems.
4. **Correction turn (2026-07-21):** complete_claim/abort submission dedup, atomic
   migration bootstrap, projection validation, restart-safe worker completion IDs, and
   historical duplicate receipts were hardened after Codex review.
5. **Acceptance-matrix correction (2026-07-21):** mandatory reopen/fence/race/resume/
   RECONCILING/workflow/status coverage and full Phase 16.3 variant SQLite persistence
   round-trips were added.
6. **Deterministic race correction (2026-07-21):** probabilistic abort/completion loop
   replaced with fault-hook ordered concurrent coverage for completion-first and
   abort-first; focused suite **292 passed**, full suite **1120 passed**.
7. **Phase-boundary risk:** treating every `RECONCILING` effect as Phase 16.5 work would
   incorrectly import Phase 16.6 write-reconciliation responsibilities. Only reusable
   read primitives may cross that boundary in 16.5.
8. **External-call lease risk:** the bounded worker has no in-call heartbeat today;
   timeout/renewal semantics must be an explicit Phase 16.5 design and test decision.
9. **Frozen-evidence storage risk:** protected artifact persistence is not yet supplied
   by Phase 16.4 and must be resolved before claiming end-to-end eligible-thread
   snapshots.

## Files added / updated

```text
src/ai_dev_loop/pr_review_v2/application/
src/ai_dev_loop/pr_review_v2/infrastructure/
src/ai_dev_loop/pr_review_v2/workers/
src/ai_dev_loop/pr_review_v2/__init__.py
tests/unit/pr_review_v2/test_durable_*.py
tests/unit/pr_review_v2/test_sqlite_schema.py
tests/unit/pr_review_v2/durable_helpers.py
tests/unit/pr_review_v2/conftest.py
tests/integration/test_phase16_4_*.py
archive/implementation-history/findings/phase-16-4-to-16-5-handoff.md
archive/implementation-history/plans/phase-16-4-pr-review-v2-durable-engine-clarifications.md
```

No control-plane files (`.cursor/rules`, skills, integrations) were modified.
