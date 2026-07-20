# Phase 16.2 -> Phase 16.3 Handoff

Date: 2026-07-20

## Executive status

Phase 16.2 extracted Cursor -> staging -> Codex into a typed reusable local
boundary. Reusable local modules no longer import, inspect, or mutate GitHub
PR-review domain state. Phase 15 scheduling/acceptance remains available only
through a one-way legacy adapter. Public A/B commands, statuses, identities,
checkpoints, and schemas are unchanged.

## Final signatures

### `ai_dev_loop.local_review_loop`

- `LocalReviewOperation` (`start` | `resume`)
- `ScheduledCursorTurn(iteration_number, prompt_path, pre_cursor_validator=None)`
- `AcceptedReviewDelivery` / `AcceptedFinalizationResult` / `AcceptedResultFinalizer`
- `LocalReviewFixRequest(run_id, operation, tool_policy=None, scheduled_first_cursor_turn=None, on_accepted=None)`
- `LocalReviewOutcome` (`accepted`, `accepted_with_residual_risk`, `max_iterations_reached`, `interrupted`, `failed`, `aborted`, plus mid-loop statuses)
- `LocalReviewFixResult` (public-compatible fields + `outcome` + `needs_external_continuation`)
- `validate_scheduled_cursor_turn(...)`
- `outcome_from_status(status)` maps `completed` -> `accepted`, etc.
- `run_local_review_fix(request) -> LocalReviewFixResult`

### `ai_dev_loop.workflow_engine`

- `execute_local_review_fix(request)` — authoritative locked local loop
- `start_run` / `resume_run` — thin default A/B adapters
- `WorkflowResult = LocalReviewFixResult`
- Internal helpers take optional `loop_ctx: _LocalLoopExecution`

### `ai_dev_loop.legacy_pr_review_local_adapter` (PR -> local only)

Moved from reusable local modules:

- `next_external_cursor_iteration`
- `pending_external_cursor_iteration`
- `derive_external_cursor_iteration_for_recovery`
- `is_external_cursor_prompt_iteration`
- `begin_external_local_review_budget`
- `refresh_external_publication_patch_fingerprint`
- `scheduled_cursor_turn_from_legacy_pr_state`
- `finalize_legacy_pr_accepted_local_result` (deterministic state/fingerprint under locks; no worker spawn)
- `build_legacy_pr_local_request` / `resume_legacy_pr_local_fix_loop`

`commands.pr_review` owns worker spawn after local return when
`needs_external_continuation is True`. The adapter does not import
`commands.pr_review`.

### `ai_dev_loop.resume_planner`

- `LocalInvocationContext(scheduled_first_cursor_turn=None)`
- `plan_next_action` / `active_cursor_iteration` / interrupted restore consume
  invocation context only (never `github_pr_review`)

### `ai_dev_loop.iterations`

- Local-only prompt APIs: `cursor_prompt_path` / `read_cursor_prompt` accept
  optional `scheduled_prompt_path`
- Budget helpers retained: `local_review_budget_used`, `record_local_review_for_budget`
- No `github_pr_review` access

## Dependency direction

```text
commands/PR callers
  -> legacy_pr_review_local_adapter (optional)
  -> local_review_loop / workflow_engine
  -> iterations / resume_planner / runners
```

Reusable local modules (`local_review_loop`, `workflow_engine`, `iterations`,
`resume_planner`) have no PR/GitHub/publication imports and no
`state.github_pr_review` access (enforced by unit AST assertion).

## Unchanged contracts

- Persisted `RunState` / `schemas/run-state-v1.json` shapes
- Exact controller A, reviewer B, Cursor chat, frozen Codex runtime
- Never `--last`, replacement chats, or new review sessions
- Resume / recover / usage-limit recover / extend / abort / locks
- Git identity and staged-patch checks; non-destructive Git policy
- Public CLI commands, exit codes, and rendered statuses
- Phase 15 fields/statuses remain; cleanup deferred to Phase 16.9

## Validation evidence

### Phase 16.1 barrier (pre-change and post-change)

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

- Pre-change: **12 passed**
- Post-change: **12 passed**

### Live collection

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
```

- Collected: **840 tests**

### Focused suites (plan commands)

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py \
  tests/integration/test_phase16_2_local_review_boundary.py \
  tests/unit/test_controller_launch.py \
  tests/integration/test_launch_worker.py \
  tests/integration/test_prepare.py \
  tests/integration/test_start_cursor.py \
  tests/integration/test_start_staging.py \
  tests/integration/test_start_codex_review.py \
  tests/integration/test_phase5_loop_resume.py \
  tests/integration/test_phase11_recover.py \
  tests/integration/test_phase12_index_mutations_and_staging_recovery.py \
  tests/integration/test_phase13_cursor_usage_limit_recovery.py \
  tests/integration/test_phase14_5_initial_staging_recovery.py \
  tests/integration/test_abort.py \
  tests/integration/test_e2e_acceptance.py \
  tests/unit/test_phase15_12_external_feedback_iteration.py \
  tests/unit/test_phase15_16_external_feedback_preflight.py \
  tests/integration/test_phase15_12_external_feedback_recovery.py \
  tests/integration/test_phase15_15_publication_patch_fingerprint.py \
  tests/integration/test_phase15_16_external_feedback_clean_baseline.py \
  tests/integration/test_phase15_17_durable_external_adjudication_flow.py \
  --tb=line
```

- Combined focused plan set: **232 passed**
- Additional unit contracts (`tests/unit/test_phase16_2_local_review_contracts.py`): included in full suite; direct run **21 passed** with boundary tests

### Full acceptance

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
- mypy: Success (72 source files)
- Full suite: **840 passed**
- MkDocs strict build: passed
- sdist/wheel build: passed (`ai_dev_loop-0.1.0`)
- `git diff --check`: passed

## Risks / residual notes

- Accepted-result finalization stays under local locks; external continuation
  (worker spawn) happens only after return. PR callers check
  `result is not None and result.needs_external_continuation is True` so
  MagicMock/`None` patches do not spuriously spawn.
- `workflow.local_review_count` remains the generic persisted budget counter;
  artifact iteration numbering stays monotonic and independent.
- Phase 15 tests now supply explicit scheduled-turn / finalizer inputs; do not
  reintroduce `github_pr_review` branching into reusable local modules.
- Do not begin Phase 16.3+ durable PR-review v2 work from this extraction.

## Independent review outcome

The A/B implementation run `ai-dev-loop-20260720T172226Z-281f85` completed
after one Codex review with **no actionable findings**. Its terminal status was
`completed_with_residual_risk`, not `completed`:

- Cursor's final validation reported 840 passed plus green Ruff, mypy, MkDocs,
  package build, and diff checks.
- Reviewer B reproduced Ruff and mypy, 160 focused tests, and the isolated
  launch-worker scenario.
- B's full-suite attempt reported 829 passed and 11 environment-related
  failures caused by restricted writable cache/home paths, Unix-socket
  permissions, subprocess `uv` access, and one launch-worker timeout that
  passed in isolation.
- B classified `tests_status` as `blocked_environment`; it did not request a
  corrective Cursor iteration and found no code defect in the staged patch.

Treat the 840-pass result as implementer evidence and the blocked-environment
result as independent-review residual risk. Before accepting Phase 16.2 as the
committed baseline, either accept that documented residual explicitly or rerun
the full suite in an environment that permits writable user state, Unix-domain
sockets, and `uv` subprocesses.

## Phase 16.3 entry brief

### Required context

Read these sources before planning or editing:

1. `pr-review-v2-restructure-context.md` in full, especially the already-made
   decisions, target architecture, state/effect semantics, Phase 16.3 scope,
   cross-phase testing strategy, and continuation rules.
2. This handoff and
   `archive/implementation-history/plans/phase-16-2-local-review-loop-extraction.md`.
3. The new typed local contracts in `src/ai_dev_loop/local_review_loop.py` only
   as a future integration boundary.
4. Current Phase 15 PR modules only as evidence for observable requirements and
   safety invariants, never as the state-machine design to preserve.

### Goal

Define PR review v2 as pure domain logic before any infrastructure exists:

- Pydantic v2 models independent from legacy `RunState`;
- one discriminated union of authoritative state variants;
- typed events and declarative effects;
- an explicit transition table and pure reducer;
- typed pause, failure, and retry reasons;
- explicit external-cycle identity and limits;
- exhaustive valid/invalid transition, invariant, and determinism tests.

Conceptual states from the direction document are `PREPARED`,
`PUBLISHING_INITIAL`, `WAITING_FOR_BOT`, `ADJUDICATING`,
`WAITING_RETRY`, `WAITING_FOR_USER`, `RUNNING_LOCAL_FIX`,
`PUBLISHING_FIX`, `COMPLETED`, `PAUSED`, `FAILED`, and `ABORTED`.
The Phase 16.3 plan must make their final names, discriminators, payloads, and
legal transitions explicit.

### Hard boundaries

- No SQLite, migrations, store, journal, outbox, leases, worker, scheduler, or
  durable timers; those begin in Phase 16.4.
- No GitHub/GraphQL/gh, Git publication, network, subprocess, Cursor, or Codex
  execution.
- Do not call `run_local_review_fix` in Phase 16.3. A declarative effect may
  describe a future local-fix request, but application integration belongs to
  Phase 16.7.
- Do not add fields to legacy `RunState`, reuse Phase 15 lifecycle/checkpoint
  models, repair legacy recovery, or remove the transitional adapter.
- Do not change the public A/B workflow. Run the Phase 16.1 barrier if any
  shared code is touched; prefer a new isolated v2 package so it is not.
- Do not add an FSM or durable-workflow dependency.

### Domain invariants to preserve

- Every valid `state + event` pair yields a new valid state plus zero or more
  declarative effects; the reducer performs no I/O.
- Invalid events cannot mutate state or emit effects.
- Reducer output is deterministic for identical inputs. Time, IDs, jitter, and
  other nondeterminism must arrive in typed event/input data or be deferred to
  later infrastructure.
- Orthogonal strings must not permit impossible state combinations.
- External cycle number, bound repository/PR/head identity, limits, pending
  effect identity, and safe continuation data are explicit where required.
- Normal bot waiting is distinct from retryable observation failure.
- Operationally recoverable conditions lead to retry/wait/pause semantics;
  `FAILED` is reserved for corruption, invariant violation, or an unsafe
  irreparable condition.
- Terminal `COMPLETED`, `FAILED`, and `ABORTED` states cannot silently
  generate further work.
- Effects contain intent and stable correlation/idempotency inputs, not live
  side effects or sensitive bodies/prompts/patches.

### Required proof for the Phase 16.3 plan

The next plan must define and test:

- the exact public domain modules and exported contracts;
- the complete state/event transition matrix, including rejected pairs;
- reducer result/error semantics and model immutability;
- serialization/discriminator round trips for every state, event, and effect;
- full simulated paths to `COMPLETED`, `PAUSED`, `FAILED`, and `ABORTED`;
- cycle-limit, retry-budget, wait-vs-retry, abort, stale-result, and terminal
  invariants at the pure-domain level;
- proof that reducer modules have no infrastructure or legacy PR imports;
- the Phase 16.1 barrier if implementation touches shared A/B code.

### Planning decisions that must not remain implicit

- Final package/module names and what constitutes the public v2 domain API.
- Exact state variants and minimal payload owned by each variant.
- Exact event/effect vocabulary and which identities are carried in each.
- Whether an invalid transition raises a typed domain exception or returns a
  typed rejection, and how that remains mutation/effect free.
- How cycle limits, attempt counters, retry reasons, resume eligibility, and
  safe next action are represented without importing Phase 16.4 persistence.
- How late-result fencing inputs (run version, effect, generation, cycle, bound
  SHA) are represented as domain data even though enforcement is added later.

## Next safe action

Do not layer Phase 16.3 on an ambiguous baseline. First accept/commit the staged
Phase 16.2 patch (or resolve its documented environment residual), then inspect
the current repository and create an implementable Phase 16.3-only plan with
its own tests, acceptance criteria, and `OpenQuestions`. Keep the new domain
isolated from both the A/B state machine and Phase 15; retain the legacy adapter
unchanged until Phase 16.9 cleanup.
