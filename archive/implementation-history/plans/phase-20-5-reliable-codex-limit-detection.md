# Phase 20.5 — Reliable Codex Limit Detection and Capacity-Probe Recovery

## Goals

- Make the production Codex capacity probe compatible with the real
  `codex app-server --stdio` lifecycle by keeping the JSONL transport alive
  until the correlated initialization and `account/rateLimits/read` responses
  have been received or a bounded failure has been established.
- Recognize Codex reviewer usage/rate-limit failures with an explicit bias
  toward false positives over false negatives, while inspecting only
  authenticated terminal provider-error wrappers and never arbitrary agent or
  repository text.
- Preserve the Phase 20.1.1 rule that any operational reviewer failure is
  manually retryable and that an otherwise generic failure is treated as
  capacity-related whenever the exact review environment reports an exhausted
  limit.
- Keep automatic recovery on the exact bound reviewer B, frozen review model
  and reasoning effort, existing staged snapshot, ordinary scheduler effects,
  and current repository reservation.
- Add enough safe, bounded evidence to distinguish structured limit evidence,
  provider-message evidence, exhausted-capacity inference, available capacity,
  and unavailable telemetry during later diagnosis.

## Non-Goals

- Do not redeem reset credits, buy credits, log in or out, switch accounts,
  modify Codex credentials, update Codex, or predict/schedule a provider reset
  time.
- Do not add model fallback, a replacement reviewer, a new Cursor chat, a new
  scheduler timer, a different timer cadence, notifications, or remote workers.
- Do not alter Phase 20 sequence advancement, checkpoint commits, finalization,
  PR behavior, or the separately reserved Phase 21 scope.
- Do not turn `ai_dev_loop` into a general JSON-RPC framework or broaden the
  public account-management surface.
- Do not treat credits being absent, a high-but-sub-100 percentage, or an
  arbitrary occurrence of the word `limit` in agent output as sufficient
  evidence by itself.
- Do not invoke real Cursor/Codex model turns, mutate a real provider account,
  or enable/change the real workstation timer during implementation or tests.

## Scope

- Replace the one-shot/EOF-driven capacity-probe exchange with a narrow,
  bounded, interactive stdio App Server client owned by the capacity adapter.
- Harden capacity parsing to use all documented server-classified exhaustion
  signals, including applicable primary/secondary windows and
  `rateLimitReachedType`, with a false-negative-averse decision order.
- Extend Codex review failure classification with bounded message-only markers
  from recognized terminal `error` and `turn.failed` wrappers.
- Refine the post-failure workflow decision table so strong structured evidence,
  provider-message evidence, and an exhausted post-failure probe all reach the
  existing `waiting_codex_capacity` recovery path without weakening integrity
  checks.
- Prevent inferred false positives from creating an unbounded generic-failure
  retry loop after capacity is available again.
- Preserve or extend explicit manual retry so a bound, non-mutating reviewer can
  always be retried from a safe operational/capacity checkpoint after the
  existing staged-snapshot and repository-identity verification.
- Add safe observation reason codes and update current status/history,
  troubleshooting, observability, tests, and Phase 20.5 findings as required by
  the implemented behavior.

## Out of Scope

- `ai_dev_loop.yaml`, model catalogs/defaults, package-owned Codex skills,
  SessionStart hooks, desktop bridges, global integration installation, user
  shell profiles, and user credentials.
- Cursor usage-limit behavior, Git admission/staging/checkpoint algorithms,
  sequence state/entry semantics, PR/GitHub integrations, and legacy workflow
  engines.
- Reading or copying Windows or WSL auth databases, symlinking Codex homes, or
  exposing account identifiers, email addresses, percentages, reset timestamps,
  credits, raw App Server messages, or provider prose in public output.
- Replaying or modifying the historical Phase 20.3 incident. It is diagnostic
  evidence only; tests must reconstruct its protocol and event shapes
  hermetically.
- A real-account compatibility test inside the automated Cursor execution.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file.
- `archive/implementation-history/master-plan.md` only for the still-applicable
  scheduler, reviewer-identity, process, privacy, and recovery contracts.
- Phase 19 plan/findings and Phase 20.1.1 plan/findings, especially the capacity
  probe, native-WSL environment policy, inferred-capacity retry guard,
  attempt-unique artifacts, and manual review retry semantics.
- Phase 20.3/20.4 plan/findings only to verify that capacity waiting and manual
  reviewer recovery do not disturb sequence reservation, advancement, abort, or
  finalization behavior.
- Official OpenAI App Server documentation for the persistent JSONL transport,
  initialization lifecycle, `account/rateLimits/read`, multi-bucket limits,
  `usedPercent`, `resetsAt`, and `rateLimitReachedType`:
  <https://learn.chatgpt.com/docs/app-server>.
- `src/ai_dev_loop/process.py`,
  `src/ai_dev_loop/scheduler/application/codex_capacity_probe.py`,
  `codex_subprocess_env.py`, `codex_workflow_service.py`, `attempt_service.py`,
  `tick.py`, `contracts.py`, `history.py`,
  `src/ai_dev_loop/scheduler/codex_attempt_runner.py`,
  `src/ai_dev_loop/response_schema.py`, and
  `src/ai_dev_loop/runners/codex_failure.py`.
- Scheduler Codex state/events/reducers/contracts, SQLite migrations and state
  kinds, protected artifact helpers, schemas, CLI review-retry services, abort
  handling, systemd backend/attempt evidence, and sequence reconciliation.
- Current fake Codex implementation in `tests/conftest.py`, process tests,
  `tests/unit/test_response_schema.py`,
  `tests/unit/scheduler/test_phase19_codex_capacity.py`,
  `tests/unit/scheduler/test_phase20_1_reviewer_retry*.py`, and relevant
  scheduler/sequence integration tests.
- Current capacity/reviewer sections in `docs/operacion/troubleshooting.md`,
  `docs/operacion/observabilidad.md`, `docs/referencia/cli.md`, and the complete
  workflow guides.

The clean baseline must contain the independently reviewed and committed Phase
20.4 implementation before Phase 20.5 begins.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local Cursor rule:
  `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned `ai-dev-loop-controller` or `ai-dev-loop-handoff` skills.
- Use fake executables, injected probes/clocks/IDs, temporary repositories, and
  temporary native-WSL XDG/config homes only. Do not use real model activity,
  credentials, account mutation, systemd enablement, or network calls in tests.
- Leave implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Integrity before classification

- Preserve the existing hard distinction between integrity failures and
  operational reviewer failures. Authenticate the completion envelope,
  dispatch/effect/attempt identity, protected artifact bindings, sandbox, exact
  reviewer B, and staged checkpoint before any message classification or
  capacity probe can authorize recovery.
- Identity, digest, path, symlink, dispatch, sandbox, reviewer-binding, and
  unbound/ambiguous bootstrap failures remain hard blocks even if untrusted text
  happens to mention a limit.
- Inspect only bounded messages carried by recognized terminal Codex JSONL
  wrappers: top-level `type=error.message` and
  `type=turn.failed.error.message`, plus the already-supported serialized API
  envelope. Never scan agent messages, review Markdown, prompts, patches,
  repository files, stderr as free text, or concatenated tokens across events.
- Preserve captured raw evidence in protected artifacts, but expose only typed,
  allowlisted classifications and reason codes outside those artifacts.

### False-negative-averse limit classification

- Keep a typed evidence distinction rather than one undifferentiated boolean.
  At minimum distinguish:
  `structured_usage_limit`, `provider_message_limit`,
  `post_failure_capacity_exhausted`, and `unknown`.
- Structured API codes such as `usage_limit_exceeded` remain the strongest
  evidence. Continue accepting their current supported bare/nested/serialized
  envelope shapes.
- Treat a single recognized terminal wrapper as provider-message limit evidence
  when its bounded, case-folded, whitespace-normalized message contains an
  explicit allowlisted marker such as `usage limit`, `usage_limit_exceeded`,
  `rate limit`, `rate_limit_exceeded`, `quota exceeded`,
  `insufficient_quota`, or `too many requests`. Marker matching is
  intentionally permissive: Phase 20.5 prefers a recoverable false positive to
  a missed capacity incident.
- Do not require both `error` and `turn.failed`; either authenticated terminal
  wrapper is sufficient. Do not classify from the generic word `limit` alone,
  from `try again` alone, or from arbitrary human/agent content.
- Keep the marker set centralized, named, bounded, and covered by positive and
  negative tests. Do not use unconstrained fuzzy similarity or execute/compile
  provider text.
- A false positive may cause only a capacity wait and a resume of the same
  read-only reviewer. It may never create Cursor work, change Git, replace B,
  change the model, bypass the review ceiling, or authorize a sequence
  checkpoint.

### Real App Server transport

- Keep App Server transport behind `CodexCapacityProbePort`; domain and reducer
  code receive only a typed safe observation, never subprocess handles or raw
  JSON dictionaries.
- Launch the frozen Codex command as an argv array ending in
  `app-server --stdio`, with `shell=False`, no repository working-directory
  dependency, and the exact same `sanitize_codex_subprocess_env()` policy used
  by reviewer attempts.
- Maintain stdin/stdout for the complete request/response exchange. Send one
  newline-delimited `initialize` request and `initialized` notification, then
  `account/rateLimits/read`; do not signal EOF merely because request bytes were
  written. Read until the matching initialization and limits response IDs are
  validated or one total bounded deadline/failure occurs.
- Support Codex's documented headerless wire form while continuing to accept a
  valid JSON-RPC 2.0 header. Ignore only well-formed notifications. Reject
  malformed JSON, protocol errors, unexpected response IDs, duplicate terminal
  responses, premature EOF, oversized lines/streams, excessive nesting, and
  ambiguous exchanges as `UNAVAILABLE`.
- Bound stdin writes, startup, reads, total stdout/stderr, JSON line size,
  nesting, shutdown, and total wall time. On success, error, timeout, parser
  failure, cancellation, or exception, close pipes and terminate/reap the exact
  probe process group. No child or app-server daemon may survive the probe.
- Prefer a narrow probe-owned interactive transport helper. If the generic
  process helper must change, preserve its one-shot stdin contract and add all
  large-stdin, partial-output, timeout, and process-group regressions required by
  the Cursor rules.

### Capacity decision semantics

- Parse `rateLimitsByLimitId` when present and use legacy `rateLimits` only as
  the documented compatibility fallback. Inspect every returned limit record
  without persisting its ID or label.
- Treat any non-empty, server-classified `rateLimitReachedType` as an exhausted
  record. Treat a finite numeric `usedPercent >= 100` in any present primary or
  secondary window as exhausted; a provider value above 100 must not turn clear
  exhaustion into `UNAVAILABLE`.
- Give independently valid exhaustion evidence precedence over malformed or
  unavailable sibling records. This deliberate false-negative-averse rule means
  one proven exhausted record returns `EXHAUSTED` even if another record cannot
  be interpreted.
- Return `AVAILABLE` only when at least one applicable window exists, every
  applicable record/window is valid, every percentage is finite and nonnegative,
  all percentages are below 100, and no reached-state marker is present.
  Otherwise, when no valid exhaustion signal exists, return `UNAVAILABLE`.
- Do not infer exhaustion merely because credits are absent or zero. A documented
  explicit spend-control/reached-limit boolean may count only after its exact
  current App Server schema is characterized and tested; do not guess field
  meaning.
- `resetsAt` may be parsed only for validation/diagnosis inside the adapter. It
  is not scheduling authority and must not be persisted or exposed by default.

### Workflow and retry matrix

- Strong structured evidence enters the existing `waiting_codex_capacity` path
  without requiring a second causal proof.
- Provider-message evidence is sufficient to enter capacity waiting even when
  the immediate probe is unavailable. This is the intentional false-positive
  preference requested for Phase 20.5.
- For any otherwise authenticated operational reviewer failure without a direct
  limit marker, perform one bounded post-failure probe. `EXHAUSTED` enters
  capacity waiting; `AVAILABLE` or `UNAVAILABLE` enters
  `waiting_codex_review_retry` with a safe diagnostic.
- When capacity waiting was inferred from a generic operational failure or
  message-only evidence, retain the original operational failure kind and the
  evidence source across the capacity-available transition. If the resumed
  reviewer fails again while a fresh probe is `AVAILABLE`, stop at manual retry
  rather than cycling automatically. If the fresh probe is `EXHAUSTED`, it may
  wait again because current capacity independently confirms the limit.
- A structured usage-limit code may repeat the existing wait/resume cycle
  without consuming a completed review iteration. Only a schema-valid Codex
  review decision increments the review count.
- An `UNAVAILABLE` probe while already waiting on direct provider limit evidence
  must preserve a safe recoverable checkpoint rather than convert the run into
  an irreversible terminal block. Repeated unavailable ticks must not grow the
  event ledger or launch agents.
- Extend `scheduler review retry <run-id>` if necessary so an operator can
  explicitly authorize one same-B review retry from both
  `waiting_codex_review_retry` and an eligible `waiting_codex_capacity`
  checkpoint. The command remains process-free, idempotent per generation, and
  must perform the existing read-only repository/staged-patch verification;
  the next tick launches through the ordinary fenced effect path.
- Preserve abort precedence, leases, capacity claims, attempt uniqueness,
  reservation ownership, sequence coordination, and exact same-B continuation.

### Safe observability and compatibility

- Extend `CodexCapacityObservation` with only allowlisted safe reason/evidence
  codes when needed, such as response received, exhausted window, reached-state
  marker, timeout, premature EOF, protocol error, malformed response, or
  process failure. Never include raw stderr/stdout, messages, account identity,
  limit IDs, percentages, credits, or reset times.
- Persist at most one safe post-failure observation status/source per failed
  attempt when needed for audit. Repeated capacity polling must retain Phase
  19's no-ledger-growth property while the observable result is unchanged.
- Keep historical Phase 19/20.1.1 snapshots and events readable. Add optional
  fields/defaults or a forward-only migration only when required; never rewrite
  historical rows or reinterpret prior evidence.
- Update typed models, events, reducers, schemas, SQLite accepted state kinds,
  status/history/controller projections, and tests together for any persisted
  change.
- Public output may say that a provider limit was detected/suspected, capacity
  is exhausted, or the probe is temporarily unavailable. It must not quote the
  provider message or expose raw account/protocol data.

## Implementation Plan

1. Add regression fixtures for the real failure before changing production
   code: a message-only `error`/`turn.failed` limit event and an App Server that
   emits correlated responses while stdin remains open but drops them after
   client EOF. Prove the current classifier returns unknown and the current
   one-shot probe returns unavailable.
2. Implement a narrow bounded interactive App Server transport inside or beside
   `codex_capacity_probe.py`. Keep stdin open through the correlated exchange,
   parse incrementally under one deadline, close/terminate/reap deterministically,
   and retain the current native-WSL environment policy.
3. Refactor capacity parsing so `rateLimitReachedType` and any independently
   valid window at or above 100 produce `EXHAUSTED` before invalid siblings can
   erase that evidence. Preserve strict `AVAILABLE` and fail-safe
   `UNAVAILABLE` semantics for all other shapes.
4. Replace the boolean-only Codex limit classifier with a typed evidence result.
   Preserve structured code detection and add permissive, bounded marker
   matching only for recognized terminal provider wrappers.
5. Centralize the authenticated operational-failure decision matrix in
   `CodexWorkflowService`: direct structured/message evidence, generic failure
   plus exhausted probe, inferred retry lineage, and manual retry fallback.
   Remove ordering shortcuts that could skip a fresh exhausted-capacity signal
   or create a generic automatic retry loop after capacity is restored.
6. Make probe-unavailable capacity checkpoints recoverable and ensure explicit
   manual review retry is available from every eligible bound-reviewer
   operational wait. Preserve exact staged-patch verification and launch only
   through the ordinary scheduler effect/tick path.
7. Add only the minimum backward-compatible state/event/schema/migration and
   safe-observability changes needed to retain evidence source, inference
   lineage, retry generation, and a redacted probe outcome.
8. Replace the EOF-driven fake App Server with a production-faithful
   line-by-line interactive fake. Add unit, contract, scheduler integration,
   restart, abort, privacy, and sequence-regression coverage.
9. Update current CLI/troubleshooting/observability/workflow documentation to
   explain the exact native-WSL account boundary, permissive recognition policy,
   automatic capacity waiting, unavailable-probe behavior, and manual retry
   escape hatch without documenting account internals.
10. Write
    `archive/implementation-history/findings/phase-20-5-reliable-codex-limit-detection.md`
    with the implemented decision table, protocol fix, test evidence,
    unperformed real-account validation, and remaining risk from the
    experimental App Server protocol and intentional false positives.

## Testing Criteria

- **Message classifier unit tests:** existing structured
  `usage_limit_exceeded`; bare/nested/serialized envelopes; each allowlisted
  message marker in both recognized wrapper shapes; mixed case and whitespace;
  either wrapper independently; bounded long messages; malformed JSON; arbitrary
  agent/review content containing the same phrases; generic `limit`, `try again`,
  split tokens/events, and unrelated errors. Prove only authenticated terminal
  wrappers participate.
- **Interactive transport contract tests:** the server responds before EOF;
  keeping stdin open succeeds; premature client EOF reproduces the historical
  dropped-response failure; initialize/initialized/limits order; headerless and
  JSON-RPC 2.0 responses; incremental/fragmented lines; interleaved valid
  notifications; matching IDs; duplicate/wrong/error responses; early server
  EOF; nonzero exit; stalled initialization; stalled limits response; stdout and
  stderr floods; oversized line/nesting; total timeout; TERM/KILL process-group
  cleanup; no surviving child.
- **Capacity parser tests:** multi-bucket and legacy forms; primary-only,
  secondary-only, and null windows; 0, fractional, below-100, exactly 100, and
  above-100 percentages; non-finite/negative/string/bool values;
  `rateLimitReachedType` with and without windows; exhausted valid record plus
  malformed sibling; all-valid available; no applicable data; credits absent or
  zero alone not exhausted. Assert exhaustion evidence wins, availability
  remains strict, and ambiguity without exhaustion is unavailable.
- **Environment tests:** reviewer and probe receive the same sanitized child
  environment; DrvFS `CODEX_HOME`/`CODEX_SQLITE_HOME` are removed; native WSL
  overrides and unrelated variables survive; `os.environ` is unchanged; the
  host/Desktop account is never queried as a substitute.
- **Workflow decision-table tests:** structured limit; message-only limit with
  exhausted/available/unavailable probe; generic operational failure with each
  probe status; repeated inferred failure before/after capacity restoration;
  direct repeated structured limit; no completed-review-budget consumption;
  exact same-B resume; no Cursor or Git mutation.
- **Manual retry tests:** explicit retry from ordinary retry wait and eligible
  capacity wait; process-free/idempotent authorization; retry generations;
  repository/branch/HEAD/staged-patch/frozen-input verification; drift refusal;
  next-tick same-B launch; repeated failure remains recoverable; abort wins.
- **State/schema/store tests:** optional evidence/source/reason fields round-trip;
  illegal combinations rejected; forward migration if required; historical v6
  and current Phase 20 snapshots/events still load; CAS/effect uniqueness;
  reservation retention; safe status/history/controller rendering.
- **No-growth/restart tests:** repeated exhausted or unavailable capacity ticks
  launch nothing and do not append duplicate events; restart at every transport,
  wait, capacity-available, retry-scheduled, and attempt-completed boundary does
  not duplicate probes, attempts, or reviewer effects.
- **Privacy tests:** raw App Server traffic, error prose, stderr, account/email,
  limit IDs, percentages, credits, reset timestamps, prompts, patches, review
  content, and full session IDs never enter default CLI/status/history/logs or
  ordinary persisted summaries.
- **Regression tests:** Phase 19 capacity continuation, Phase 20.1.1 reviewer
  retry/recovery, timeout reconciliation, abort, timer tick budget, standalone
  runs, full Phase 20 two-/four-phase sequence advancement, residual-risk
  advancement, sequence abort, checkpoint commits, and final staged
  `awaiting_finalization` remain unchanged.

All automated tests must use fakes and disposable state. They must not invoke a
real model, real account reset, real scheduler timer, or real target repository.

## Validation

Run focused behavior tests first, then the broader scheduler and sequence suites.
At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_process.py \
  tests/unit/test_response_schema.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_tick.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/integration/test_phase20_1_reviewer_retry.py \
  tests/integration/test_phase20_2_sequence_start.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

If implementation creates a dedicated Phase 20.5 test module or current test
filenames differ, run the actual equivalent coverage and record exact commands
and results in the findings artifact.

Do not run a real App Server/account probe as part of Cursor's automated
validation. After independent staged review, commit, and explicit operator
approval, a separate read-only workstation smoke check may verify that the
installed probe receives a correlated response from the configured native-WSL
Codex account; it must not launch a model or mutate account state.

## Risks Or Recovery Notes

- Message-only classification intentionally accepts some false positives. The
  safety case is bounded: the reviewer is read-only, Git remains unchanged, the
  same B and frozen staged snapshot are reused, and generic inferred failures
  fall back to explicit manual retry once capacity is available.
- The App Server interface is experimental and may drift. Protocol uncertainty
  must remain a typed recoverable condition, never fabricated `AVAILABLE` and
  never a reason to query an undocumented HTTP endpoint.
- The probe and reviewer must always use the same sanitized environment. A
  Desktop/host usage display may represent another authenticated identity and
  must not be used as scheduler evidence.
- Capacity is point-in-time evidence. A concurrent client can consume capacity
  between probe and resume; a subsequent independently exhausted probe may
  return the same reviewer to waiting without consuming a completed review
  iteration.
- If interactive transport cleanup cannot prove the exact process group was
  reaped, return a typed unavailable/blocked operational result and preserve
  diagnostics. Never leave a probe child alive or weaken abort/process
  ownership checks.
- If a persisted-shape change cannot be made backward-compatible, stop and
  document the exact migration requirement before changing current rows.

## OpenQuestions

None.
