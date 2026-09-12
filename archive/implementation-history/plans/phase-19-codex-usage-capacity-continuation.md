# Phase 19 — Durable Codex Usage-Capacity Continuation

## Goal

Allow a central-scheduler run whose exact Codex reviewer B reaches a confirmed
Codex account usage limit to wait without guessing a reset time, repeatedly
observe the currently active WSL Codex account's available capacity, and resume
the same reviewer when capacity is present again.

This must support an operator applying a manual Codex reset or changing the
authenticated WSL Codex account between ticks, provided that `codex resume`
is available for operator inspection and `codex exec resume` continues to
accept the already-bound reviewer session ID. A run may cross any
number of provider five-hour or weekly windows. It must not create a second
reviewer B, change models, re-run Cursor, or mutate the target repository.

Every scheduler-launched Codex subprocess must resolve the native WSL Codex
home even when Codex Desktop has propagated a Windows/DrvFS `CODEX_HOME` or
`CODEX_SQLITE_HOME` into the controller shell. The Windows Desktop home remains
separate; only the already-supported nested sessions bridge may expose Desktop
rollouts by exact ID.

## Non-Goals

- Do not predict, calculate, persist as scheduling authority, or sleep until a
  provider reset timestamp.
- Do not add credits, redeem a reset, log in/out, switch accounts, update Codex,
  or otherwise change external account state.
- Do not retry generic Codex failures, transient rate errors, malformed review
  output, schema errors, authentication errors, process uncertainty, or timeouts.
- Do not add model fallback, a reviewer replacement, a new Cursor chat, a new
  Cursor turn, commits, pushes, PR work, or Git cleanup.
- Do not make a public CLI command for changing accounts or for reading account
  usage outside the scheduler's internal, read-only probe.
- Do not edit shell startup files, Windows environment variables, Codex Desktop
  settings, or user credentials. Interactive-shell remediation is an explicit
  operator action documented separately from scheduler behavior.
- Do not enable, install, alter, or invoke a real user-systemd timer, real
  Codex/Cursor model, desktop bridge, hook, or user-global integration in tests.

## Scope

- Add a small, bounded Codex app-server usage-probe adapter. It must speak
  JSON-RPC over `codex app-server --stdio`, initialize the server, send
  `account/rateLimits/read`, and validate only the response needed to decide
  capacity.
- Add or reuse one narrow Codex subprocess-environment policy for both review
  attempts and the usage probe. It must remove inherited `CODEX_HOME` and
  `CODEX_SQLITE_HOME` only when they resolve under `/mnt/*`, preserve absent or
  native-WSL overrides, and leave unrelated environment variables unchanged.
- Add strict, structured Codex usage-limit classification from captured Codex
  review JSONL events, separate from all other review failures.
- Add durable scheduler state, event, reducer, SQLite/state-kind support,
  status/history safe actions, and tick processing for
  `waiting_codex_capacity`.
- Resume the existing review effect only after a successful probe establishes
  nonzero remaining capacity in every applicable primary/secondary window.
- Cover the feature with fake Codex app-server and review-process tests,
  integration/regression tests, documentation, schemas, and a Phase 19 findings
  artifact.

## Out of Scope

- Changes to Phase 18. Phase 18 must be independently tested, persisted, and
  present as the clean baseline before this phase begins.
- `ai_dev_loop.yaml`, model catalogs, submit-time defaults, preflight,
  repository admission, staging semantics, timer cadence, timer installation,
  capacity/reservation ownership, abort semantics, or legacy engines.
- New database migrations or rewriting existing snapshots/events. Existing runs
  must remain readable exactly as they are.
- Reading, copying, or storing Codex authentication files, account identity,
  tokens, raw app-server traffic, full event streams in public state, or provider
  billing details.
- Sharing, symlinking, migrating, or inspecting the Windows Codex home, its
  SQLite databases, auth files, indexes, or configuration. Do not make the
  interactive `codex resume` picker a product-managed surface.
- Any automatic recovery of an unbound bootstrap reviewer. A missing or
  ambiguous B identity remains a fail-closed block.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc`.
- `archive/implementation-history/master-plan.md`.
- `archive/implementation-history/plans/phase-17-4-cursor-staging-and-usage-limit-continuation.md`;
  `phase-17-5-codex-review-and-bounded-scheduler-loop.md`;
  `phase-17-8-codex-attempt-bounds-and-systemd-budget.md`; and
  `phase-18-optional-controller-provenance.md`.
- The corresponding Phase 17.4, 17.5, 17.8, 17.10, and Phase 18 findings.
- `src/ai_dev_loop/scheduler/codex_attempt_runner.py`,
  `application/codex_workflow_service.py`, `application/codex_evidence.py`,
  `application/attempt_service.py`, `application/tick.py`,
  `application/contracts.py`, `application/history.py`,
  `domain/codex_contract.py`, `domain/events.py`, `domain/state.py`,
  `domain/reducer.py`, and `infrastructure/sqlite_store.py`.
- `src/ai_dev_loop/process.py`,
  `src/ai_dev_loop/integrations/codex/session_runtime.py`, and
  `src/ai_dev_loop/scheduler/application/systemd_backend.py`.
- `src/ai_dev_loop/response_schema.py`, current scheduler schemas, and the
  protected-artifact helpers.
- Current scheduler/Codex fake CLI fixtures and the tests under
  `tests/unit/scheduler/`, especially `test_phase17_5_codex_corrections.py`,
  `test_schema.py`, `test_tick.py`, and
  `tests/integration/test_phase17_5_scheduler_review_loop.py`.
- `docs/referencia/configuracion.md`, `docs/referencia/cli.md`,
  `docs/referencia/codex-desktop-wsl-sessions.md`,
  `docs/operacion/timer-systemd-wsl.md`,
  `docs/operacion/troubleshooting.md`, `docs/operacion/observabilidad.md`, and
  the scheduler workflow guides.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local Cursor rules:
  `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The package-owned `ai-dev-loop-controller` and `ai-dev-loop-handoff` assets
  are not in scope; do not modify, install, or invoke them.
- Use only fakes, temporary repositories, temporary XDG/config homes, injected
  clocks, and injected subprocess/probe ports in automated tests. Keep changes
  unstaged and uncommitted for independent review.

## Architecture Guardrails

### Classification and reviewer identity

- Enter the new waiting path only when one recognized Codex JSONL error wrapper
  contains the structured provider code `usage_limit_exceeded`. Do not infer it
  from prose, exit status, `rate_limit_exceeded`, `last_error`, stderr, or an
  unavailable review result.
- Preserve the original failed attempt and its protected evidence. No raw error
  payload, event JSONL, or stderr may enter state summaries, events, history, or
  CLI output.
- A resume review always invokes the exact stored reviewer B session ID with the
  submission-frozen model/reasoning configuration. It never uses `--last`, a
  new bootstrap, a fork, a guessed ID, or a substitute reviewer.
- A bootstrap attempt may enter `waiting_codex_capacity` only after one exact B
  identity has been validated and durably bound. If quota failure occurs before
  that proof, retain the existing `codex_bootstrap_uncertain` block behavior.

### Probe contract and privacy

- Put app-server transport/parsing behind a narrow injectable infrastructure
  port; domain/reducer code receives a typed capacity result, never subprocess
  output or JSON dictionaries.
- Invoke the frozen configured WSL Codex command as an argv array ending in
  `app-server --stdio`; never use `shell=True`, shell interpolation, the
  Desktop app, a copied auth file, or a direct undocumented HTTP endpoint.
- Bound startup, read, shutdown, stdout, and stderr. Terminate and reap the
  probe process on timeout or protocol failure. It is a read-only control probe,
  never a model/review attempt and never an agent child that may mutate a repo.
- Send JSON-RPC `initialize`, the `initialized` notification, then
  `account/rateLimits/read`; accept only the response matching the request ID.
  Ignore only well-formed unrelated notifications. Any malformed, duplicate,
  missing, mismatched, or oversized response is unavailable, not capacity.
- Accept `rateLimitsByLimitId`, with legacy `rateLimits` only as a documented
  compatibility fallback. For every returned limit record, inspect each present
  `primary` and `secondary` window. `usedPercent` must be a finite numeric value
  in `[0, 100]`; available means `100 - usedPercent > 0`. Absent windows are not
  invented. Empty/invalid records or no applicable windows are unavailable.
- Do not use `resetsAt` to set a timer or decide availability. Do not persist
  account name, limit ID, reset timestamp, raw JSON, or percentages. A safe
  observation may be returned in an ephemeral tick receipt for tests only; the
  durable state records only the waiting reason/iteration and timestamps needed
  for audit.

### Native WSL Codex environment

- Use the same explicit subprocess environment policy for the existing Codex
  review process and the new app-server probe. A probe and the resume it gates
  must observe the same native WSL Codex home and authenticated account.
- Treat inherited `CODEX_HOME` or `CODEX_SQLITE_HOME` values that resolve below
  `/mnt/*` as Codex Desktop/DrvFS contamination and omit those keys from the
  child environment so Codex falls back to native WSL defaults. Do not rewrite
  them to a Windows path, copy their contents, or follow whole-home/session-root
  symlinks.
- Preserve explicit overrides that resolve to native WSL paths and preserve all
  unrelated environment needed by Codex. Keep the policy deterministic,
  injectable, and independently unit tested; do not mutate `os.environ`.
- Interactive `codex resume` remains operator-owned. Documentation must explain
  that a newly opened WSL shell should have `CODEX_HOME` unset (or native), that
  `codex resume --all` removes repository filtering, and that scheduler-created
  `codex exec` sessions require `--include-non-interactive` in the picker.

### Durable scheduler semantics

- Add a typed `waiting_codex_capacity` state and named event/reducer transition.
  Update state unions, JSON schemas, SQLite accepted state kinds, readers,
  projections, and tests together. Do not rewrite historical rows; use a
  backward-compatible state/schema strategy and prove existing snapshots still
  validate.
- On confirmed usage limit, mark the completed attempt ingested, preserve the
  repository reservation, and transition atomically to waiting. Do not create a
  reset timer or a duplicate review effect.
- On each normal scheduler tick while waiting, make one bounded probe. If any
  applicable window is exhausted, leave all durable data unchanged and launch
  nothing. This prevents event/ledger growth every 30-second tick.
- If every applicable window has capacity, transition safely back to the review
  scheduling boundary and let the existing fenced effect/attempt path launch the
  exact resume review. Do not bypass effects, capacity claims, leases, abort,
  invocation evidence, or result ingestion.
- If probing is unavailable, malformed, or times out, fail closed to `blocked`
  with a concise typed reason. Do not guess capacity or spin on an unsupported
  Codex CLI protocol.
- A subsequent confirmed quota failure repeats the same wait cycle without
  consuming `max_review_iterations`; only schema-valid completed reviews count.
  An operator reset or account change needs no special command: the next tick
  reads the active WSL CLI credentials and may resume B if capacity is present.

### Safety boundaries

- Preserve existing global tick leases, run versions/CAS, effect idempotency,
  capacity claims, worktree reservation, stale-attempt fencing, and durable
  abort precedence.
- No probe may run after a run has become aborted, terminal, or blocked for a
  non-capacity cause. Probe outcomes must not revive terminal/blocked runs.
- Status/history/default CLI output must say only that the run is waiting for
  Codex capacity and its safe action is a future `scheduler tick`; no secrets,
  account details, raw provider errors, full IDs, prompts, patches, or review
  contents may appear.

## Implementation Plan

1. Characterize the current Codex JSONL error transport and the app-server
   JSON-RPC exchange with focused fake fixtures before changing scheduler flow.
   Extract a strict Codex usage-limit classifier and a typed usage-probe port
   plus bounded concrete runner. Keep the adapter independent of scheduler
   state and injectable in tests.
2. Implement or reuse the narrow native-WSL Codex subprocess-environment
   policy, then apply it identically to the current review runner and the new
   app-server probe. Do not modify user shell profiles or global Codex state.
3. Extend the Codex attempt outcome safely so a recognized
   `usage_limit_exceeded` remains authenticated, typed failure evidence. Keep
   all other nonzero, timeout, truncation, bootstrap-identity, schema, and
   malformed-result branches unchanged and fail-closed.
4. Add the new durable state/event/reducer/store/schema/projection support.
   Preserve existing v2 snapshots and event history. Define safe status,
   history, and next-action wording for waiting capacity; do not expose raw
   quota telemetry.
5. Integrate the transition in `CodexWorkflowService`: bind B first when a
   bootstrap identity is provable, then wait on typed quota; otherwise block.
   Add waiting-state probe handling to tick processing. A positive result
   returns only through the established Codex review effect scheduler, so the
   next resume remains fenced and idempotent.
6. Update fake Codex support to emulate both review JSONL quota errors and the
   app-server protocol without ever using real credentials. Add docs that
   explain automatic capacity observation, manual reset/account-change behavior,
   non-use of reset times, blocking when the probe is unsupported, and the
   distinction from generic review failures. Document the inherited-DrvFS
   diagnosis and the interactive picker flags without making shell-profile
   mutation part of the product.
7. Write `archive/implementation-history/findings/phase-19-codex-usage-capacity-continuation.md`
   with the implemented contract, test evidence, unperformed real-account
   validation, and residual risk from the experimental app-server protocol.

## Testing Criteria

- **Adapter/classifier unit tests:** canonical recognized wrapper with
  `usage_limit_exceeded`; all near misses (`rate_limit_exceeded`, prose,
  split facts, malformed JSON, unrelated wrapper, generic nonzero) remain
  unclassified. Test initialize/initialized/request ordering, matching IDs,
  notifications, `rateLimitsByLimitId`, legacy fallback, primary-only,
  secondary-only, 0 versus positive capacity, empty/invalid windows, invalid
  percentages, EOF, nonzero exit, oversized output, and timeout/process-group
  cleanup.
- **Subprocess-environment unit/contract tests:** inherited DrvFS
  `CODEX_HOME`, DrvFS `CODEX_SQLITE_HOME`, both together, absent variables, and
  native WSL overrides. Prove only unsafe `/mnt/*` values are omitted, unrelated
  variables survive, `os.environ` is unchanged, and both the review fake and
  app-server fake receive the same sanitized environment.
- **State/schema tests:** valid waiting snapshots/events/transitions, rejected
  illegal transitions, schema/model alignment, old snapshots continuing to
  load, SQLite state-kind acceptance, status/list/history redaction, and the
  correct safe next action.
- **Codex workflow unit tests:** a bound reviewer quota failure transitions to
  waiting and ingests exactly once; unbound bootstrap quota failure blocks;
  all non-capacity review failures preserve current blocking; repeated quota
  failures do not advance review count or duplicate effects.
- **Tick/integration tests with fakes:** a zero primary or secondary window
  keeps the run waiting across repeated ticks and launches no review; changing
  fake active credentials/capacity to positive makes the next tick schedule and
  resume the same B exactly once; a second quota event waits again; no new B,
  Cursor turn, Git mutation, or real reset-time scheduling occurs.
- **Regression/privacy tests:** abort while waiting never probes/launches later;
  stale lease/CAS/effect races do not duplicate work; unavailable probe blocks;
  default output and persisted summaries omit account identity, raw responses,
  percentages, reset timestamps, full session IDs, prompts, patches, and raw
  Codex output.

## Validation

Run focused tests first, then the scheduler suite appropriate to the changed
fixtures and state surface. At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_response_schema.py \
  tests/unit/test_process.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_tick.py \
  tests/unit/scheduler/test_phase17_5_codex_corrections.py \
  tests/integration/test_phase17_5_scheduler_review_loop.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration/test_phase17_5_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

Do not run a real Codex app-server or model-backed test as Phase 19 validation.

## Risks Or Recovery Notes

`account/rateLimits/read` is an app-server protocol capability, not a Phase 19
authority to alter an account. Its absence or protocol drift must be a visible,
safe block rather than a hidden retry loop. Capacity observed as positive is a
point-in-time fact: a concurrent client may consume it before the resume; a new
confirmed usage-limit outcome simply returns the run to waiting.

The critical continuity boundary is B's exact session ID. A manual account
change is supported only because the user has established that the configured
CLI can resume that B; Phase 19 must still refuse to bootstrap a replacement B
when the original identity was never proven.

## OpenQuestions

None.
