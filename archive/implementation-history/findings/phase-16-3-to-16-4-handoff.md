# Phase 16.3 -> Phase 16.4 Handoff

Date: 2026-07-21

## Executive status

Phase 16.3 added an isolated pure-domain package for PR review v2:
immutable discriminated Pydantic models, declarative effects, typed events,
and one deterministic reducer with an explicit transition registry. No SQLite,
gateways, workers, CLI, or real Cursor/Codex/GitHub activity were introduced.
Legacy `RunState`, Phase 15, and the A/B local loop are untouched.

A correction turn on 2026-07-21 confirmed and fixed five review findings:
outcome binding to active effects/state, stronger operational-state progress
validators, timezone-aware UTC instants for retry ordering, externally
supplied future `next_attempt_at` for proven-not-applied reconciliation, and
exact poll-sequence checks for normal bot waiting.

A second correction turn on 2026-07-21 confirmed and fixed two further findings:
exact full-equality reconciliation identity (suspended active effect, top-level
`original_write`, and embedded `active_effect.original_write`, plus matching
run/cycle/repository/head/strategy on `reconcile_write`), and `with_attempt`
validation that rejects non-positive or above-maximum attempts while preserving
effect and idempotency identities.

A third correction turn on 2026-07-21 confirmed and fixed three further findings:
binding-bearing effects require `repository`/`bound_head_sha` to equal the
embedded binding; nested envelopes (`waiting_retry`, resumable `paused`,
`reconciling_write`) require outer run/origin/limits/cycle/binding to match the
nested state; and `CompletedState` requires verified-no-findings evidence to use
the exact final `binding.head_sha`.

The final independent Codex review found **no actionable findings**. The run
closed as `completed_with_residual_risk`, not because of a known Phase 16.3
defect, but because 10 full-suite tests were blocked by the restricted reviewer
environment. The executor reported the same full suite green outside those
restrictions. See **Acceptance disposition and residual risk** below; this
residual has not yet been explicitly accepted by the user.

## Entry baseline

- Branch: `rba/phase_16-3` at merge commit `1b5ed80` (Phase 16.2 merged to main)
- Python (uv): 3.11.15
- Pydantic: 2.13.4
- Pre-change collection baseline from Phase 16.2 handoff: **840 tests**
- Accepted Phase 16.2 residual: independent-review `blocked_environment` /
  `completed_with_residual_risk` was explicitly accepted by the user on
  2026-07-20 as an entry condition for this phase
- Current handoff state: all 18 Phase 16.3 files are staged on
  `rba/phase_16-3`; they are not committed or merged as of this handoff update
- Staged scope: 7,049 additions, all in the isolated v2 package, its tests, the
  approved Phase 16.3 plan, and this handoff; no control-plane file is staged

## Public domain API

Package root: `ai_dev_loop.pr_review_v2.domain`

### Modules

| Module | Role |
|---|---|
| `common.py` | Constrained IDs/SHAs, `UtcInstant`, origins, bindings, limits, tokens, reason enums, evidence |
| `effects.py` | Discriminated `PrReviewEffect` variants + identity/classification helpers |
| `state.py` | Discriminated `PrReviewState` variants + step/progress cross-field validators + adapters |
| `events.py` | Discriminated `PrReviewEvent` / outcome variants + adapters |
| `reducer.py` | `reduce_pr_review(state, event) -> TransitionApplied \| TransitionRejected` |

### Key exports

- State: `PrReviewState`, `PR_REVIEW_STATE_ADAPTER`, `parse_pr_review_state`
- Events: `PrReviewEvent`, `PR_REVIEW_EVENT_ADAPTER`, `parse_pr_review_event`
- Effects: `PrReviewEffect`, `PR_REVIEW_EFFECT_ADAPTER`, `parse_pr_review_effect`
- Reducer: `reduce_pr_review`, `TransitionApplied`, `TransitionRejected`,
  `TRANSITION_REGISTRY`, `accepted_transition_pairs`
- Helpers: `stable_effect_ids`, `classify_effect`, `is_mutating_effect`,
  `is_read_only_effect`, `is_local_effect`, `is_reconciling_effect`,
  `with_attempt`, `UtcInstant`, `coerce_utc_instant`

### State discriminators (`kind`)

`prepared`, `publishing_initial`, `waiting_for_bot`, `adjudicating`,
`waiting_for_user`, `running_local_fix`, `publishing_fix`,
`reconciling_write`, `waiting_retry`, `completed`, `paused`, `failed`, `aborted`

### Event discriminators (`kind`)

`start_requested`, `effect_succeeded`, `effect_retryable_failure`,
`effect_blocked`, `write_outcome_uncertain`, `retry_due`, `resume_requested`,
`user_continuation_requested`, `abort_requested`, `fatal_failure_detected`

### Effect discriminators (`kind`)

`generate_publication_text`, `commit_patch`, `push_commit`,
`create_or_update_pr`, `request_bot_review`, `observe_bot_review`,
`adjudicate_threads`, `post_thread_reply`, `run_local_fix`, `update_pr_text`,
`resolve_thread`, `reconcile_write`

## Transition registry

- Explicit map keyed by `(state.kind, event.kind)` covering every pair
  (`13 × 10 = 130` entries)
- Unsupported pairs use a typed rejector that emits no state/effects
- Accepted pairs may still reject on substep mismatches (token/outcome/attempt/
  binding/poll-sequence/retry time)
- Terminal states reject every event with `RejectionCode.TERMINAL_STATE`

## Domain semantics proven in 16.3

- Normal bot polling (`bot_still_waiting`) requires the exact next poll
  sequence (`state.poll_sequence + 1`); stale/duplicate/forward-jump sequences
  reject without effects and keep attempt `1`
- GitHub retry budget is six attempts (1 + 5); exhaustion pauses with
  `retry_exhausted` and a resumable same-effect continuation reset to attempt 1
- Ambiguous mutating writes enter `reconciling_write` and emit only
  `reconcile_write`; never a duplicate write
- Reconciliation identity is exact full equality: the suspended active
  mutating effect, top-level `original_write`, and embedded
  `active_effect.original_write` must match, and the reconciliation effect's
  run ID, cycle, repository, bound head SHA, and typed strategy must match
  that write; same-ID divergent writes are rejected without effects
- Reconciliation `applied` replays the original success transition;
  `proven_not_applied` requires an externally supplied future
  `next_attempt_at` and enters `waiting_retry` for the original write identity;
  `unresolved` pauses
- `with_attempt` rebuilds through the validated effect boundary and rejects
  attempts `< 1` or `> max_attempts` while preserving `effect_id` /
  `idempotency_key`
- Binding-bearing effects (`request_bot_review`, `observe_bot_review`,
  `adjudicate_threads`, `post_thread_reply`, `run_local_fix`, `update_pr_text`,
  `resolve_thread`) require `repository == binding.repository` and
  `bound_head_sha == binding.head_sha`
- Nested envelopes bind exactly: `waiting_retry` and resumable `paused` require
  outer run ID, origin, limits, cycle, and binding to match the nested state;
  `reconciling_write` adds the same envelope binding on top of exact-write checks
- `CompletedState` requires `evidence.head_sha == binding.head_sha`
- Retry timers use timezone-aware UTC instants (`UtcInstant`); equivalent
  offset representations compare equal; early timers reject
- `EffectSucceeded` outcomes are bound to the active effect and current state
  (SHAs, remote ref, repository/PR/branches, trigger marker, artifact refs,
  frozen-thread provenance); mismatches return typed effect-free rejections
- Late-result fencing inputs are carried on `EffectCompletionToken`
  (`effect_id`, `expected_run_version`, `lease_generation`, `cycle_number`,
  `bound_head_sha`). Phase 16.3 rejects mismatched effect/cycle/SHA and
  validates positive run-version/lease-generation shape.
  **Transactional run-version and lease-generation fencing remains Phase 16.4.**

## Files added / updated

```text
src/ai_dev_loop/pr_review_v2/
  __init__.py
  domain/
    __init__.py
    common.py
    effects.py
    events.py
    state.py
    reducer.py

tests/unit/pr_review_v2/
  __init__.py
  conftest.py
  helpers.py
  test_models.py
  test_serialization.py
  test_reducer_matrix.py
  test_reducer_paths.py
  test_architecture.py
  test_correction_findings.py

archive/implementation-history/findings/phase-16-3-to-16-4-handoff.md
```

No existing production modules or persisted schemas outside the new v2 package
were modified.

## Validation evidence (post-correction-3, 2026-07-21)

### Executor validation

#### Focused domain suite

```bash
uv run python -m ruff format --check src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2
uv run python -m ruff check src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 --tb=line
```

- Ruff format/check (focused): passed
- mypy (`src`): Success (79 source files)
- Domain tests: **202 passed** (includes all correction-turn regressions)

#### Phase 16.1 A/B barrier

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

- Result: **12 passed**

#### Full acceptance

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

- Ruff format/check: passed
- mypy: Success (79 source files)
- Collect-only: **1042 tests**
- Full suite: **1042 passed** in 319.72s
- MkDocs strict build: passed
- sdist/wheel build: passed (`ai_dev_loop-0.1.0`)
- `git diff --check`: passed

## Acceptance disposition and residual risk

- Run: `ai-dev-loop-20260721T000050Z-f4f45b`
- Final status: `completed_with_residual_risk`
- Review iterations: 4 of a maximum of 5
- Final review: **0 actionable findings**; no further Cursor correction requested
- Focused domain suite: **202 passed**
- Phase 16.1 A/B barrier: **12 passed**
- Ruff format/check: passed
- mypy: passed for 79 source files
- Full collection: **1042 tests**
- Independent full run: **1032 passed, 10 environment-blocked failures**
- Post-review staged status: unchanged
- Reviewer confirmed no control-plane files are staged

The 10 independent-review failures were outside the Phase 16.3 package:

- 2 lock tests could not `chmod` the sandboxed XDG state directory;
- 7 Phase 15.14 tests could not create Unix sockets in the reviewer sandbox;
- 1 packaging test failed while spawning its nested build command.

No focused domain, A/B barrier, lint, formatting, or type-check validation
failed. MkDocs and package builds were not repeated independently after the
environment-blocked full run; the executor reported both green.

## Phase 16.4 entry contract

Phase 16.4 must execute the pure reducer over durable SQLite state. It must not
duplicate state-machine decisions in persistence or worker code. The public
domain union and `reduce_pr_review` remain authoritative.

### Required durable records

The exact schema belongs in the Phase 16.4 plan, but it must represent at least:

- one versioned run snapshot containing the complete discriminated state payload;
- an append-only, per-run sequenced event journal;
- an effect outbox preserving the complete effect payload, stable `effect_id`,
  `idempotency_key`, attempt, eligibility time, status, and safe error metadata;
- worker leases with owner identity, monotonic generation, heartbeat, and expiry;
- timestamps as timezone-aware UTC values;
- artifact references by protected path/hash instead of sensitive bodies,
  prompts, patches, tokens, or credentials in SQLite.

Persisted states, events, and effects must round-trip through
`PR_REVIEW_STATE_ADAPTER`, `PR_REVIEW_EVENT_ADAPTER`, and
`PR_REVIEW_EFFECT_ADAPTER`. Do not weaken `extra="forbid"`, immutability, or any
cross-field validator to accommodate persistence.

### Required transactional boundaries

For an accepted domain event, one SQLite transaction must atomically:

1. read the current snapshot and monotonic run version;
2. verify the expected version;
3. call the pure reducer;
4. append the event with its next per-run sequence;
5. persist the new state snapshot and incremented version; and
6. insert every emitted effect into the outbox exactly once.

Effect claim and completion must also be transactional. Before applying an
effect result, re-read and verify at least:

```text
run_id + expected run version + effect_id + lease generation
+ cycle number + bound head SHA
```

The `EffectCompletionToken` already carries these fencing inputs. Phase 16.4
must enforce run version and lease generation at the store boundary; the Phase
16.3 reducer only enforces token shape plus effect/cycle/head identity. A stale
worker result must be auditable but must not mutate the run, create effects, or
repeat an external action.

### Leases, timers, restart, and abort

- A new or reacquired lease increments a monotonic fencing generation.
- Only the current unexpired lease owner may claim or finalize an effect.
- Expired claims must be recoverable without losing stable effect identity.
- `waiting_retry.next_attempt_at` is an absolute UTC eligibility instant;
  restart must reconstruct scheduling from SQLite alone.
- The GitHub retry budget remains one initial attempt plus five retries.
- Normal bot polling is scheduling, not retry, and must not consume that budget.
- Abort must atomically invalidate pending/claimed work and the active lease so
  late completions cannot advance the run.
- Recovery must use snapshots, journal, outbox, and leases—not logs, process
  memory, a new run, or inferred subprocess history.

### Crash and concurrency proofs required in 16.4

Use real temporary SQLite databases and fake effect executors to prove at least:

- crash before and after every transactional commit boundary;
- restart with pending, claimed, retry-waiting, uncertain, and terminal work;
- two workers racing to claim the same run/effect;
- an expired worker returning after a newer lease generation exists;
- a completion carrying a stale run version, effect ID, cycle, or head SHA;
- duplicate event/effect submission without duplicate outbox work;
- retry eligibility and attempt budget surviving database reopen;
- ambiguous mutating writes remaining in reconciliation without replaying the
  original write;
- abort racing with an in-flight completion;
- every state/effect/event variant serializing and validating after restart;
- basic status being reconstructed from SQLite.

If Phase 16.4 touches shared A/B code, rerun the Phase 16.1 barrier and prove no
observable A/B contract changed. Prefer keeping the durable engine isolated so
the barrier remains a regression check rather than an integration dependency.

### Explicit Phase 16.4 non-goals

- no real GitHub gateway or GitHub API calls (Phase 16.5/16.6);
- no real Cursor/Codex/local-fix execution (Phase 16.7);
- no PR-review v2 CLI cutover or legacy Phase 15 compatibility branches;
- no migration of legacy PR-review runs into the v2 database;
- no legacy deletion or production cutover (Phase 16.9);
- no external FSM or durable-workflow library without fresh user approval.

### Decisions the Phase 16.4 plan must make explicitly

- final SQLite schema, indexes, uniqueness constraints, and schema-versioning /
  migration bootstrap;
- database path, file permissions, connection ownership, busy handling, and
  transaction mode;
- injectable clock, ID generation, and worker/lease identity;
- exact claim recovery rule after lease expiry;
- whether and how rejected/stale events are journaled without changing state;
- safe error fields and artifact-reference policy;
- status DTO/query surface before any CLI is added;
- deterministic fault-injection seams for transaction and restart tests.

## Risks / notes for Phase 16.4

- Do not start Phase 16.4 on top of a partially staged Phase 16.3 worktree.
  First review/accept the residual risk, commit Phase 16.3, merge it, and start
  16.4 from that clean accepted baseline.
- Do not import this domain from legacy PR modules or A/B workflow code yet.
- Persistence must store the discriminated state payload plus run version;
  effect outbox rows must preserve `effect_id` / `idempotency_key` / attempt.
- Enforce `expected_run_version` and `lease_generation` transactionally when
  applying effect results; 16.3 only proves shape + effect/cycle/SHA checks.
- Retry timing, jitter, and `Retry-After` parsing belong in infrastructure;
  the reducer only consumes absolute timezone-aware `next_attempt_at` values.
- Map `run_local_fix` application results to the extracted
  `LocalReviewFixResult` in Phase 16.7 without importing
  `LocalReviewOutcome` into the v2 domain.
- `reconciling_write` is an intentional top-level state added beyond the
  original conceptual list so ambiguous-write protocol is observable.
- Proven-not-applied reconciliation must supply a future eligibility instant;
  infrastructure must never set it to the failure `occurred_at`.
- Reconciliation must compare full mutating-effect equality, not effect ID
  alone; infrastructure must not reconstruct partial original writes.
- Persisted nested envelopes must keep outer and nested run/origin/limits/
  cycle/binding identical; cross-run reconstruction is invalid at the model
  boundary.

## Suggested inspection order for the Phase 16.4 agent

1. Read `pr-review-v2-restructure-context.md` completely, especially state,
   persistence, effect protocol, Phase 16.4, testing, and continuation rules.
2. Read this handoff and the approved Phase 16.3 plan.
3. Inspect `domain/common.py`, `effects.py`, `events.py`, `state.py`, and
   `reducer.py`; treat tests as executable contract, not just examples.
4. Inspect existing repository SQLite, locking, runtime-path, and status-query
   patterns for reusable infrastructure, while avoiding new coupling to legacy
   PR-review semantics.
5. Write and approve a dedicated Phase 16.4 plan before implementation. Keep
   unresolved architectural choices in `OpenQuestions`; do not silently invent
   them during execution.

## Next safe action

1. Inspect the final review and staged repository state.
2. Obtain explicit acceptance of the Phase 16.3 environment residual.
3. Commit and merge Phase 16.3, then begin 16.4 from a clean baseline.
4. Create an approved, implementation-ready Phase 16.4 plan covering SQLite
   schema, transactional store, journal/outbox, leases, fencing, durable timers,
   restart, abort, and fault-injection acceptance.

Do not begin GitHub gateways, real external effects, or production CLI cutover
until the corresponding later phases and acceptance gates.
