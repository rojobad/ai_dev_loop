# Phase 16.3 - PR Review v2 Pure Domain And Reducer

Status: proposed for approval

Depends on:

- Phase 16.2 being accepted, committed, and merged as the baseline;
- the documented Phase 16.2 independent-review environment residual being explicitly
  accepted by the user; and
- the Phase 16.2 A/B boundary and Phase 16.1 regression barrier remaining intact.

The user confirmed all three entry conditions on 2026-07-20.

## Goals

Define the complete PR review v2 workflow as pure, typed domain logic before any
infrastructure exists.

This phase must:

- add Pydantic v2 models that are independent of legacy `RunState` and Phase 15;
- represent the authoritative workflow state as one discriminated union whose variants
  cannot form invalid lifecycle combinations;
- define typed events and declarative effects with stable correlation and idempotency
  data but no live side effects;
- implement one deterministic reducer with the contract
  `state + event -> applied transition or typed rejection`;
- make external-cycle limits, retry batches, normal bot waiting, user attention,
  ambiguous-write reconciliation, pause/failure reasons, and terminal behavior explicit;
- carry the late-result fencing inputs needed by Phase 16.4 without implementing their
  transactional enforcement yet;
- prove the complete pure-domain paths to `completed`, `paused`, `failed`, and `aborted`;
  and
- leave a small public domain API that Phase 16.4 can persist and Phase 16.7 can drive.

## Non-Goals

- Do not add SQLite, migrations, tables, a store, journal, outbox, leases, fencing
  enforcement, worker, scheduler, heartbeats, or durable timers. Those begin in 16.4.
- Do not call GitHub, GraphQL, `gh`, Git, SSH, Cursor, Codex, or the extracted local loop.
- Do not implement CLI commands, status rendering, polling processes, publication,
  adjudication execution, local fixes, or user notifications.
- Do not implement concrete gateway error parsing or retry-header parsing. Events receive
  already typed classifications and absolute eligible times.
- Do not persist v2 state or add a persisted JSON schema. Phase 16.3 proves model/schema
  generation and serialization in memory; Phase 16.4 owns the first durable schema.
- Do not migrate, resume, or reinterpret Phase 15 runs.
- Do not add compatibility adapters between legacy PR state and v2.
- Do not clean up legacy PR code; cleanup remains Phase 16.9 work.
- Do not change the A/B public workflow, local review boundary, configuration, or docs to
  claim that v2 is operational.

## Scope

Add an isolated package rooted at:

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
```

Add focused tests under:

```text
tests/unit/pr_review_v2/
  test_models.py
  test_serialization.py
  test_reducer_matrix.py
  test_reducer_paths.py
  test_architecture.py
```

Add the implementation handoff:

```text
archive/implementation-history/findings/phase-16-3-to-16-4-handoff.md
```

No existing production module should need modification. A file placement adjustment is
allowed only to resolve an evidenced import cycle inside the new v2 package; preserve the
public API and record the adjustment in the handoff.

## Out of Scope

Cursor must not modify:

- `src/ai_dev_loop/state.py` or `src/ai_dev_loop/schemas/run-state-v1.json`;
- `src/ai_dev_loop/local_review_loop.py`, `workflow_engine.py`, `iterations.py`, or
  `resume_planner.py`;
- `src/ai_dev_loop/legacy_pr_review_local_adapter.py`;
- `src/ai_dev_loop/commands/pr_review*.py`, `pr_review_worker.py`,
  `external_adjudication.py`, or `github_pr_review_result.py`;
- `src/ai_dev_loop/runners/github.py`, `publish.py`, `codex_github.py`, or Git runners;
- `src/ai_dev_loop/config.py`, `ai_dev_loop.yaml`, project-config schemas, CLI routing,
  README, MkDocs pages, or public command help;
- `.cursor/rules/`, `.cursor/skills/`, `.agents/skills/`, integration assets, or hooks; or
- Git history, staging, remotes, branches, user-global state, or real external services.

If an existing production file appears necessary, stop and request a plan amendment. Do
not broaden the phase to make integration convenient.

## Required Context

Read before implementation:

1. `pr-review-v2-restructure-context.md` in full.
2. `archive/implementation-history/findings/phase-16-2-to-16-3-handoff.md`.
3. `archive/implementation-history/plans/phase-16-2-local-review-loop-extraction.md`.
4. `src/ai_dev_loop/local_review_loop.py` only to understand the future boundary; do not
   import or call it in 16.3.
5. The PR-review portions of `docs/guia/flujo-completo.md`,
   `docs/referencia/cli.md`, `docs/operacion/seguridad-privacidad.md`, and
   `docs/operacion/estado-artefactos.md` as evidence of observable requirements.
6. `src/ai_dev_loop/state.py`, `github_pr_review_result.py`, and
   `external_adjudication.py` only as evidence for identity, privacy, frozen-thread, and
   safety invariants. Do not reproduce their aggregate lifecycle design.
7. `tests/unit/test_phase16_2_local_review_contracts.py` and
   `tests/integration/test_phase16_1_ab_regression_barrier.py` for the protected boundary.

Current code, tests, schemas, CLI help, and `/docs` are authoritative for existing A/B
behavior. The Phase 16 direction document is authoritative for the new v2 architecture.
If they conflict on a safety guarantee, stop and report the conflict before dependent work.

## Cursor Rules And Skills

All repository rules are marked `alwaysApply`; follow them:

- `.cursor/rules/ai-dev-loop-governance.mdc`;
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`; and
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc` for acceptance evidence.

No repository-root `AGENTS.md` and no repo-local `.cursor/skills/` directory exist.
Cursor must not invoke `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`;
the A/B orchestrator owns staged review after implementation. No real model activity is
authorized by this plan.

## Architecture Guardrails

1. `ai_dev_loop.pr_review_v2.domain` is the only public Phase 16.3 API. Its modules may
   import only the standard library, Pydantic, and sibling v2 domain modules.
2. The v2 domain must not import `RunState`, `RunStatus`, config, legacy PR models,
   commands, runners, workers, gateways, stores, the local review loop, or Phase 15
   result/checkpoint helpers.
3. Domain modules must not import or call `sqlite3`, filesystem APIs, subprocess APIs,
   network clients, Git/GitHub helpers, `datetime.now`, project `utc_now`, `uuid`, or
   randomness. Timestamps and other nondeterministic values arrive in events.
4. Every domain model uses `ConfigDict(extra="forbid", frozen=True)`. Collection fields
   use immutable tuples, not mutable lists or dictionaries. Reducer inputs are never
   mutated.
5. `PrReviewState`, `PrReviewEvent`, `PrReviewEffect`, nested origins, effect outcomes,
   and adjudication/user evidence are discriminated unions. Do not replace them with
   unvalidated strings or free-form payload dictionaries.
6. State is the sole lifecycle source of truth. Effects describe intent; they are not a
   second lifecycle. An operational state may own only the effect kinds explicitly valid
   for that variant.
7. The reducer performs no I/O and returns `TransitionApplied` or
   `TransitionRejected`. Invalid transitions do not return a new state and emit no
   effects. Exceptions are reserved for construction-time model validation or an actual
   programmer invariant, not ordinary invalid events.
8. Every effect has a deterministic `effect_id`, stable `idempotency_key`, cycle,
   attempt number, maximum attempts, and bound repository/head preconditions. Reissuing
   an effect after retry or explicit resume keeps the same identity and idempotency key.
9. Every effect result event carries an `EffectCompletionToken` with `effect_id`,
   `expected_run_version`, `lease_generation`, `cycle_number`, and `bound_head_sha`.
   Phase 16.3 rejects mismatched effect/cycle/SHA. Phase 16.4 will enforce run version and
   lease generation transactionally; do not simulate a store in the reducer.
10. The default GitHub retry policy is six total attempts: initial attempt `1` plus five
    retries. Retry timing and jitter are never calculated in the reducer; the failure
    event supplies an absolute `next_attempt_at` selected by later infrastructure.
11. A successful GitHub observation with no feedback is normal polling. It advances a
    poll sequence and schedules another observation without increasing the failed-attempt
    count or entering `waiting_retry`.
12. A mutating effect with an ambiguous result transitions to
    `reconciling_write` and emits a read-only reconciliation effect. It must never emit a
    duplicate write directly. Confirmed applied advances as success; proven not applied
    may retry; unresolved ambiguity pauses safely.
13. Operationally correctable conditions use `paused` or `waiting_for_user`. `failed`
    is restricted to corruption, hash/invariant violation, invalid trusted result shape,
    or another condition unsafe to resume.
14. `completed`, `failed`, and `aborted` are terminal and reject every later event.
    Abort from any non-terminal state emits no further work.
15. Frozen eligible thread IDs are non-empty, unique, bound to one PR, cycle, trigger,
    and full head SHA. Adjudication must cover exactly that frozen set.
16. All sensitive bodies, prompts, patches, publication text, model output, and raw
    comments remain in protected artifacts. Domain state/effects contain safe relative
    artifact references and lowercase SHA-256 hashes, never raw contents, tokens, full
    environments, or transcript data.
17. The domain never merges, closes, retargets, force-pushes, resets, cleans, stashes,
    unstages, or invents recovery successors.
18. Phase 16.3 must not change existing A/B or Phase 15 behavior. Architecture tests
    enforce isolation rather than monkeypatching legacy internals.

## Public Domain Contract

### Common models

Define in `common.py` and re-export from `domain/__init__.py`:

- constrained aliases for non-empty identifiers, 40-character lowercase Git SHA, and
  64-character lowercase SHA-256;
- `ArtifactRef(relative_path, sha256)`, with a lexical safe run-relative path and no
  filesystem access;
- `RepositoryIdentity(name_with_owner)`;
- `PullRequestBinding(repository, pr_number, head_branch, base_branch, head_sha)`;
- `SourceRunOrigin(kind="source_run", source_run_id, repository, head_branch,
  base_branch, expected_head_sha, accepted_patch, execution_context_ref)`;
- `ExistingPrOrigin(kind="existing_pr", binding, execution_context_ref)`;
- `PrReviewOrigin`, discriminated by `kind`;
- `WorkflowLimits(max_external_cycles, max_local_iterations,
  github_max_attempts_per_batch=6)`;
- `EffectCompletionToken(effect_id, expected_run_version, lease_generation,
  cycle_number, bound_head_sha)`;
- typed `TransientErrorKind`, `PauseReasonKind`, `FailureReasonKind`, and
  `SafeActionKind` enums; and
- safe `ErrorSummary(kind, safe_summary)` and `SafeAction(kind, condition)` models.

`execution_context_ref` is a protected artifact reference or similarly opaque safe
reference. It prevents raw Cursor/Codex identity and prompt material from becoming
ordinary domain/event output while giving Phase 16.7 a stable integration handle.

The typed reason vocabulary must cover at least:

- transient timeout, DNS, connection refused/reset, network unavailable, HTTP 429,
  primary/secondary rate limit, HTTP 502/503/504, and temporary CLI failures;
- pause for retry exhaustion, authentication, permissions, closed PR, repository/head/
  patch/thread drift, HTTP validation rejection, unresolved ambiguous write, external
  cycle limit, local-fix pause/limit, and required operator action; and
- failure for corrupt artifact, hash mismatch, invariant violation, invalid trusted
  effect result, or internal corruption.

### Effects

Define the following immutable discriminated variants in `effects.py`:

| Discriminator | Intent | Classification |
|---|---|---|
| `generate_publication_text` | Produce commit and PR text artifacts from referenced evidence | local/model, non-write |
| `commit_patch` | Commit one exact referenced patch at an expected HEAD | reconciliable Git write |
| `push_commit` | Push one exact commit without force to an expected remote ref | reconciliable Git write |
| `create_or_update_pr` | Bind/create/update the PR for exact repo/head/base | reconciliable GitHub write |
| `request_bot_review` | Post one marker-derived review trigger | reconciliable GitHub write |
| `observe_bot_review` | Read PR/head, completion evidence, and eligible threads | GitHub read |
| `adjudicate_threads` | Produce a referenced structured decision for a frozen thread set | local/model effect |
| `post_thread_reply` | Post one referenced inline reply to one frozen thread | reconciliable GitHub write |
| `run_local_fix` | Request the extracted local capability through an opaque context and prompt ref | future application effect |
| `update_pr_text` | Update title/body from referenced publication text | reconciliable GitHub write |
| `resolve_thread` | Resolve one confirmed corrected thread | reconciliable GitHub write |
| `reconcile_write` | Read external state to determine whether one original write applied | read-only reconciliation |

Every effect carries common identity/precondition fields. Effect-specific payloads use
typed models and artifact references. `reconcile_write` embeds or references the exact
original mutating effect and a typed reconciliation strategy; it never contains a new
write intent.

Expose pure helpers for stable effect/idempotency identity and for determining whether
an effect is read-only, mutating, local, or reconciliating. Identity uses run ID, cycle,
operation, and stable target identity; retries and reconciliation must not invent new
write identities.

### State union

Define and re-export these final top-level variants from `state.py`:

| Class | `kind` | Minimal owned payload |
|---|---|---|
| `PreparedState` | `prepared` | run/origin/limits, cycle 1, entered time; no effect |
| `PublishingInitialState` | `publishing_initial` | source origin, patch/publication progress, exactly one allowed publication effect |
| `WaitingForBotState` | `waiting_for_bot` | bound PR, cycle, trigger evidence if confirmed, poll sequence, active request/observe effect |
| `AdjudicatingState` | `adjudicating` | binding, frozen thread tuple and snapshot ref, active adjudication effect |
| `WaitingForUserState` | `waiting_for_user` | binding, frozen decisions/result refs, ordered remaining reply intents, optional active reply effect, safe continuation action |
| `RunningLocalFixState` | `running_local_fix` | binding, exact actionable thread set, fix-prompt ref, active local-fix effect |
| `PublishingFixState` | `publishing_fix` | old/new head evidence, accepted patch, corrected thread queue, publication progress, exactly one allowed publication/resolve effect |
| `ReconcilingWriteState` | `reconciling_write` | suspended operational state, exact original write, active read-only reconciliation effect, safe last error |
| `WaitingRetryState` | `waiting_retry` | suspended operational/reconciliation state with the next attempt, retrying effect identity, attempt/max, `next_attempt_at`, safe last error |
| `CompletedState` | `completed` | final binding, cycle, typed verified-no-findings evidence, completion time; no effect |
| `PausedState` | `paused` | subject/binding when available, cycle, typed reason, safe action, optional resumable state/effect, pause time; no emitted work |
| `FailedState` | `failed` | subject/binding when available, cycle, typed fatal reason and safe summary, failure time; no effect |
| `AbortedState` | `aborted` | subject/binding when available, cycle, user-request reason and time; no effect |

`ReconcilingWriteState` is intentionally added to the conceptual state list from the
direction document. It makes the mandatory ambiguous-write protocol observable instead
of hiding it inside `publishing_*` or `waiting_retry`.

Use restricted effect unions per operational state rather than a second free-form
`phase` string. Use finite nested unions for suspended/resumable states; do not permit
recursive values containing another `waiting_retry` or `paused` indefinitely.

Export:

```python
PrReviewState
PR_REVIEW_STATE_ADAPTER
parse_pr_review_state(payload: object) -> PrReviewState
```

### Events and effect outcomes

Define in `events.py`:

- `StartRequested`;
- `EffectSucceeded`, carrying a matching `EffectCompletionToken` and a discriminated
  success outcome;
- `EffectRetryableFailure`, carrying a token, typed transient error, failed attempt,
  and absolute `next_attempt_at`;
- `EffectBlocked`, carrying a token, typed pause reason, and safe action;
- `WriteOutcomeUncertain`, carrying a token, safe error, and deterministic
  reconciliation identity;
- `RetryDue`, carrying the pending effect ID and current time;
- `ResumeRequested`, for a resumable `paused` state and a new explicit retry batch;
- `UserContinuationRequested`, carrying typed evidence bound to the same PR/cycle/SHA;
- `AbortRequested`; and
- `FatalFailureDetected` with a typed fatal reason.

All events carry `occurred_at`. Event models must reject free-form raw output.

The `EffectSucceeded.outcome` union must contain exact result variants for:

- publication text artifacts prepared;
- commit recorded;
- push/ref confirmed;
- PR bound;
- review trigger confirmed;
- bot still waiting with the next normal poll time;
- verified no-findings evidence;
- eligible threads observed and frozen;
- adjudication recorded;
- thread reply confirmed;
- local fix finished;
- PR text update confirmed;
- thread resolution confirmed; and
- reconciliation resolved as `applied`, `proven_not_applied`, or `unresolved`.

Adjudication data stores only thread IDs, decision enums, safe summaries, and artifact
refs/hashes. It validates exact coverage of the frozen set. All-actionable data requires
a fix-prompt ref and no reply intents. Any `not_applicable` or `uncertain` decision
requires a referenced reply artifact and forbids a local-fix prompt.

Local-fix outcomes mirror, but do not import, the extracted boundary vocabulary:
`accepted`, `accepted_with_residual_risk`, `max_iterations_reached`, `paused`,
`failed`, and `aborted`. Phase 16.7 owns mapping to `LocalReviewFixResult`.

Export:

```python
PrReviewEvent
PR_REVIEW_EVENT_ADAPTER
parse_pr_review_event(payload: object) -> PrReviewEvent
```

### Reducer result

Define in `reducer.py`:

```python
reduce_pr_review(
    state: PrReviewState,
    event: PrReviewEvent,
) -> TransitionApplied | TransitionRejected
```

`TransitionApplied` contains the new state and an immutable tuple of newly requested
effects. `TransitionRejected` contains a typed rejection code plus only safe
state/event/effect identifiers; it contains no replacement state and no effects.

Maintain one explicit immutable transition registry keyed by state kind and event kind.
Handlers may additionally validate the active effect and outcome discriminator. Tests
must prove every state/event-kind pair is classified and no implicit fallback mutates
state.

## Transition Matrix

The following is the required top-level matrix. “Effect result” always requires exact
effect ID, cycle, SHA, expected outcome kind, and current attempt.

| Current state | Accepted event | New state / effects |
|---|---|---|
| `prepared` | `start_requested`, source-run origin | `publishing_initial`; emit `generate_publication_text` |
| `prepared` | `start_requested`, existing-PR origin | `waiting_for_bot`; emit `request_bot_review` |
| `publishing_initial` | publication-text success | remain; emit `commit_patch` |
| `publishing_initial` | commit success | remain; emit `push_commit` for exact commit |
| `publishing_initial` | push success | remain; emit `create_or_update_pr` |
| `publishing_initial` | PR-bind success | `waiting_for_bot`; emit `request_bot_review` |
| `waiting_for_bot` | review-trigger success | remain with trigger evidence; emit `observe_bot_review` |
| `waiting_for_bot` | bot-still-waiting success | remain, increment poll sequence; emit a new normal observation with supplied `not_before`; attempt stays 1 |
| `waiting_for_bot` | eligible-threads success | `adjudicating`; emit `adjudicate_threads` for the exact frozen set |
| `waiting_for_bot` | verified-no-findings success | `completed`; emit nothing |
| `adjudicating` | all-actionable success | `running_local_fix`; emit `run_local_fix` with exact prompt ref |
| `adjudicating` | any not-applicable/uncertain success | `waiting_for_user`; sequentially emit only the first `post_thread_reply` |
| `waiting_for_user` | reply success with more replies | remain; emit the next ordered `post_thread_reply` |
| `waiting_for_user` | final reply success | remain with no active effect and safe user-continuation action |
| `waiting_for_user` | valid user continuation after replies | `waiting_for_bot`; emit `observe_bot_review` for the same binding without duplicating the trigger |
| `running_local_fix` | accepted/residual local result | `publishing_fix`; emit `generate_publication_text` |
| `running_local_fix` | local limit/pause/failure result | `paused` with typed local reason and safe action; emit nothing |
| `running_local_fix` | aborted local result | `aborted`; emit nothing |
| `publishing_fix` | publication-text success | remain; emit `commit_patch` for the accepted fix |
| `publishing_fix` | commit success | remain; emit non-force `push_commit` |
| `publishing_fix` | push success | remain with confirmed new head; emit `update_pr_text` |
| `publishing_fix` | PR-update success | remain; emit the first ordered `resolve_thread`, or advance when none |
| `publishing_fix` | thread-resolution success with more threads | remain; emit the next ordered `resolve_thread` |
| `publishing_fix` | publication complete below cycle limit | increment cycle and move to `waiting_for_bot`; emit `request_bot_review` bound to new SHA |
| `publishing_fix` | publication complete at cycle limit | `paused` with `external_cycle_limit_reached`; emit nothing |
| any operational/reconciling state with active effect | retryable failure before max attempts | `waiting_retry`; preserve effect identity, increment attempt, store supplied time; emit nothing yet |
| any operational/reconciling state with active effect | retryable failure at attempt 6 (or effect maximum) | `paused` with retry-exhausted reason and a resumable same-effect continuation reset to attempt 1; emit nothing |
| any operational/reconciling state with active effect | typed blocked condition | `paused` with supplied typed reason/action; emit nothing |
| any operational state with mutating effect | ambiguous write outcome | `reconciling_write`; emit only `reconcile_write` |
| `reconciling_write` | reconciliation says applied with matching confirmed result | apply the original write's normal success transition; never repeat the write |
| `reconciling_write` | reconciliation proves not applied | `waiting_retry` for the original write if budget remains, otherwise `paused` |
| `reconciling_write` | reconciliation remains unresolved | `paused` with `ambiguous_write_unresolved`; retain reconciliation continuation |
| `waiting_retry` | matching `retry_due` at/after `next_attempt_at` | restore suspended state; emit same effect/idempotency at recorded next attempt |
| `paused` | valid `resume_requested` with resumable continuation | restore continuation, begin a new explicit attempt batch at 1, emit same effect/idempotency |
| any non-terminal state | `abort_requested` | `aborted`; emit nothing |
| any non-terminal state | `fatal_failure_detected` | `failed`; emit nothing |
| `completed`, `failed`, or `aborted` | any event | typed rejection; emit nothing |

Additional rejection rules:

- reject early retry timers, stale effect IDs, stale cycles, stale head SHAs, mismatched
  result kinds, wrong thread IDs, missing trigger evidence, duplicate completion, user
  continuation before reply completion, and resume without a resumable continuation;
- reject `WriteOutcomeUncertain` for read-only/local effects;
- reject generic retry-failure events whose reported attempt differs from the active
  effect attempt;
- reject no-findings evidence not bound to the current full head SHA;
- reject adjudication that does not cover exactly the frozen thread set; and
- leave the original input model byte-for-byte equivalent after every rejection and
  application.

## Implementation Plan

### 1. Confirm the isolated entry baseline

- Confirm the clean merged Phase 16.2 baseline and preserve unrelated changes.
- Record the branch, Python/Pydantic versions, current test collection, and accepted
  Phase 16.2 residual in the 16.3 handoff.
- Do not rerun Phase 16.2 implementation or alter its public contracts.
- Inventory imports before editing and stop if the proposed package name collides with
  existing code.

### 2. Implement immutable common types and validation

- Add the v2 package and common constrained models/enums.
- Centralize safe identifier, SHA, artifact-path, unique-tuple, origin/binding, and
  limits validation.
- Keep all models JSON serializable and free of runtime objects, callbacks, paths,
  handles, clients, or exceptions.
- Add unit tests for valid and invalid construction, immutable collections, forbidden
  extras, and safe artifact references.

### 3. Implement declarative effects and stable identities

- Add every effect variant and the discriminated `PrReviewEffect` adapter.
- Derive effect IDs/idempotency keys deterministically from stable domain inputs.
- Model initial attempt as 1 and GitHub max attempts as 6; do not calculate delay.
- Model write reconciliation explicitly and prevent a reconciliation effect from
  containing a replacement write.
- Test stable identity across retry/resume, per-cycle and per-thread uniqueness, effect
  classification, and absence of raw sensitive payload fields.

### 4. Implement state and event unions

- Add all state variants, restricted active-effect unions, suspended/resumable finite
  unions, typed evidence, and terminal models.
- Add all event/outcome variants and completion tokens.
- Add cross-field validators for origin/binding, cycle/SHA, frozen thread coverage,
  actionable versus reply paths, publication progress, retry attempt, and terminal
  shapes.
- Add adapters and parse helpers; round-trip every variant with its discriminator.

### 5. Implement the explicit pure reducer

- Add the immutable transition registry and typed reducer result.
- Implement common exact-active-effect/fence validation first.
- Implement initial publication, bot observation, adjudication, user-attention, local
  fix, fix publication, and terminal transitions.
- Implement sequential reply and resolution queues to avoid partially ordered batches.
- Implement retry, explicit resume, ambiguous-write reconciliation, abort, fatal
  failure, cycle-limit, and terminal rejection semantics.
- Keep helper functions pure and total over validated inputs. Do not catch model errors
  and reinterpret them as external failures.

### 6. Prove the matrix and full workflow paths

- Build representative valid fixtures for every state/event/effect/outcome variant.
- Parameterize every state/event-kind pair against the explicit registry. Test valid
  pairs and all rejected pairs.
- Test substep mismatches within accepted top-level pairs.
- Simulate complete source-run and existing-PR paths, including multiple external
  cycles, a local correction, no-findings completion, user attention, reconciliation,
  retry exhaustion/resume, fatal failure, and abort.
- Prove determinism by reducing the same serialized input twice and comparing serialized
  results exactly.
- Prove reducer input immutability and rejection effect-freedom.

### 7. Prove package isolation and hand off to 16.4

- Add AST/import assertions forbidding legacy, A/B, infrastructure, I/O, time-source,
  UUID, and randomness dependencies.
- Confirm no existing production file or persisted schema changed.
- Add the handoff with final exported symbols, discriminators, transition registry,
  schema/serialization evidence, test counts, and any implementation-only risks.
- Do not add SQLite scaffolding or a placeholder store.

## Testing Criteria

Required automated evidence:

- **Unit/model:** constraints, cross-field invariants, immutability, forbidden extras,
  safe refs/hashes, effect identity, reason vocabulary, and impossible-shape rejection.
- **Unit/serialization:** JSON-mode round trip for every top-level and nested state,
  event, effect, outcome, origin, retry, pause, and reconciliation variant; discriminator
  presence in generated schemas.
- **Unit/reducer matrix:** exhaustive state/event-kind classification plus active-effect
  and outcome substep mismatches; every rejection has no state/effects.
- **Unit/workflow paths:** complete source-run and existing-PR paths to every terminal or
  waiting outcome, with multiple cycles and local correction.
- **Unit/retry/reconciliation:** attempts 1 through 6, retry eligibility time, normal poll
  not consuming retry budget, same identity after resume, ambiguous applied/not-applied/
  unresolved outcomes, and no duplicate writes.
- **Unit/fencing:** stale effect ID, cycle, and SHA rejection; token validation for run
  version and lease generation fields; explicit note that transactional enforcement is
  a 16.4 acceptance requirement.
- **Unit/terminal/abort:** abort from every non-terminal representative, fatal transition
  from every non-terminal representative, and rejection of all events by terminal states.
- **Static architecture:** domain imports only standard library, Pydantic, and sibling
  v2 domain modules; no legacy/local/infrastructure imports or forbidden I/O/nondeterminism.
- **Regression:** Phase 16.1 barrier and full existing suite remain green. No external
  fake is needed for the new tests because Phase 16.3 performs no I/O.

Important edge cases include duplicate/empty thread IDs, wrong reviewed SHA, cycle 0,
limit 0, malformed hashes, escaping artifact paths, mismatched reply/fix-prompt shapes,
wrong effect result type, retry attempt mismatch, early retry timer, exhausted retry
resume, cycle-limit boundary, user continue before replies complete, late results after
abort, and nested retry of reconciliation rather than of the original write.

## Validation

Run focused checks first:

```bash
uv run python -m ruff format --check src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2
uv run python -m ruff check src/ai_dev_loop/pr_review_v2 tests/unit/pr_review_v2
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 --tb=line
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

Acceptance requires:

- every declared model and transition test green;
- generated discriminators and JSON round trips for all union variants;
- no reducer I/O or nondeterminism;
- no invalid transition producing state or effects;
- normal polling distinct from retries;
- six-attempt retry and same-effect resume semantics;
- explicit ambiguous-write reconciliation with no duplicate write path;
- stale result and terminal-state rejection;
- unchanged A/B barrier and full suite;
- no changes to existing production modules or persisted schemas; and
- an accurate 16.3-to-16.4 handoff.

## Risks Or Recovery Notes

- Pydantic `frozen=True` does not make a nested mutable list immutable. Use tuples at
  every collection boundary and test attempted mutation.
- Suspended retry/reconciliation state can accidentally become recursively unbounded.
  Restrict it to a finite union that excludes `waiting_retry`, `paused`, and terminal
  variants; flatten retry exhaustion into a resumable operational/reconciliation state.
- A generic success event can accidentally accept the wrong result for an active effect.
  Validate the exact effect-kind/outcome-kind pair before any transition.
- Do not turn `expected_run_version` or `lease_generation` into fake in-memory fencing.
  Carry and validate their shape now; enforce them atomically in 16.4.
- Do not calculate retry headers, jitter, clocks, UUIDs, or effect-store status here.
  Those are application/infrastructure responsibilities.
- Do not import `LocalReviewOutcome` for convenience. Keep the v2 vocabulary independent
  and map it at the Phase 16.7 adapter.
- If the complete workflow requires another top-level lifecycle variant, stop and amend
  this plan instead of adding an undocumented orthogonal phase string.
- If any existing A/B or Phase 15 file changes, run the Phase 16.1 barrier immediately,
  explain why isolation failed, and request review before continuing.

## OpenQuestions

None.
