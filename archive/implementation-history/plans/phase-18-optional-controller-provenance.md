# Phase 18: Operator-Owned Runs, Iteration Timeline, and Precise Timer Cadence

## Goal

Make scheduler operation operator-owned rather than controller-A-owned, expose
safe per-phase iteration timing for a completed or in-progress run, and make
the packaged systemd timer's documented approximately-30-second cadence
explicit.

An operator must be able to submit and explicitly start a run without knowing,
recording, or presenting a controller session ID. The result must retain every
other scheduler safety boundary: immutable inputs, repository reservation,
preflight, durable state transitions, bounded Cursor/Codex work, exact B
reviewer identity after binding, and explicit abort.

The read-only status surface must make the frozen review budget and completed
review count visible. A new bounded read-only timeline surface must let an
operator or an assistant build a table of each Cursor and Codex attempt by
iteration, including its safe status, timestamps, and observed duration.

The packaged timer must contain AccuracySec=1s as well as
OnUnitActiveSec=30. This narrows systemd's default one-minute coalescing
window to one second; it is an approximately-30-second operational cadence,
not a real-time or no-delay guarantee.

Existing A-bearing runs remain readable and immutable. No existing state,
artifact, user-global installation, active Parish360 run, or timer state may
be rewritten or operated by this phase.

## Non-Goals

- Do not weaken, remove, recreate, substitute, or infer the exact B reviewer
  identity. Fresh B creation and subsequent resume behavior remain unchanged.
- Do not make queued runs auto-start, create a background daemon, change tick,
  capacity, reservation, preflight, abort, recovery, timeout, output-bound,
  maximum-review enforcement, or systemd service process-budget behavior.
- Do not implement a general telemetry system, raw event/artifact browser,
  live subprocess monitor, queue-latency dashboard, billing meter, or
  cross-run reporting.
- Do not add authentication, multi-user access control, a synthetic operator
  identity, or an arbitrary local-session fallback.
- Do not execute real Cursor/Codex, enable/disable/install a real timer, alter
  the active Parish360 run, or run a live timer acceptance test.
- Do not modify target-repository YAML, target repositories, Desktop/WSL
  bridges, hook trust, session bridges, or any user-global installed file.
- Do not commit, push, reset, clean, stash, rebase, unstage, or perform
  destructive Git operations.

## Scope

- Scheduler submit, start, controller-status, and associated CLI/application
  contracts, making controller provenance optional.
- Controller provenance fields in current scheduler state, events,
  checkpoints, schemas, validation, and snapshots, only to accept null for
  fresh runs while preserving historical values.
- Scheduler status/list/controller-read projections, queued safe-action text,
  summary review-budget fields, and a new bounded scheduler timeline command.
- Read-only timeline projection from existing central scheduler attempt rows;
  it must not add attempt storage, mutate state, or require a migration.
- The packaged handoff and controller skill assets only where their documented
  command examples would otherwise require controller A. Update their package
  asset tests, but do not install them or alter integration mechanics.
- Packaged timer template, template validator, timer-operation tests, timer
  documentation, and Phase 18 findings.
- Submission idempotency material, only to ensure optional controller
  provenance does not alter the identity of otherwise identical frozen work.
- Typed state/event validation, read compatibility, unit/integration/privacy
  regressions, documentation, and a Phase 18 findings artifact.

## Out of Scope

- A database migration, rewriting existing rows, schema-version bump solely
  for history, or normalization of historical A-bearing snapshots/events.
- Any new persistent timing field, attempt ID exposure, backend/unit identity,
  artifact path, prompt, review content, raw process output, full session ID,
  environment, or secret in public output.
- A timer interval lower than 30 seconds, a change to OnBootSec, randomized
  delay, persistent catch-up semantics, system-wide systemd units, or changes
  to the one-shot service PATH and timeout envelope from Phase 17.8/17.10.
- A guarantee that ticks occur exactly every 30 seconds under suspend, heavy
  scheduling, a still-running service, or a systemd/OS timer-slack delay.
- Changes to historical legacy controller commands outside the current
  scheduler/controller compatibility read surface, cutover cleanup, or
  non-scheduler products.

## Required Context

Read all of the following before editing:

- AGENTS.md.
- Every file under .cursor/rules/, especially:
  ai-dev-loop-governance.mdc,
  ai-dev-loop-orchestrator-contracts.mdc,
  ai-dev-loop-state-and-schema-contracts.mdc,
  ai-dev-loop-loop-and-resume-contracts.mdc,
  ai-dev-loop-codex-review-contracts.mdc,
  ai-dev-loop-abort-contracts.mdc,
  ai-dev-loop-docs-acceptance-contracts.mdc, and
  ai-dev-loop-global-integrations-contracts.mdc.
- The repository contains no separate .cursor/skills directory; use the rules
  above and the package-owned skill assets named below.
- archive/implementation-history/master-plan.md.
- archive/implementation-history/plans/phase-17-1-central-ledger-and-submission.md.
- archive/implementation-history/plans/phase-17-2-tick-control-reservations-and-controller-status.md.
- archive/implementation-history/plans/phase-17-5-codex-review-and-bounded-scheduler-loop.md.
- archive/implementation-history/plans/phase-17-7-clean-cutover-and-acceptance.md.
- archive/implementation-history/plans/phase-17-9-fresh-submit-after-terminal-run.md.
- archive/implementation-history/plans/phase-17-10-reviewer-status-and-timer-path.md.
- archive/implementation-history/findings/phase-17-8-codex-attempt-bounds.md.
- archive/implementation-history/findings/phase-17-9-findings.md.
- archive/implementation-history/findings/phase-17-10-reviewer-status-and-timer-path.md.
- The official systemd.timer AccuracySec documentation:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html.
- src/ai_dev_loop/cli.py.
- src/ai_dev_loop/commands/scheduler.py and
  src/ai_dev_loop/commands/controller.py.
- src/ai_dev_loop/scheduler/application/submission.py,
  start.py, status.py, controller_read.py, contracts.py, history.py, and
  safe_actions.py.
- src/ai_dev_loop/scheduler/domain/state.py, events.py, reducer.py, and
  iterations.py.
- src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py and migrations.
- src/ai_dev_loop/scheduler/infrastructure/systemd_assets.py and
  src/ai_dev_loop/scheduler/assets/ai-dev-loop-scheduler-tick.timer.
- src/ai_dev_loop/integrations/codex/skill/SKILL.md and
  src/ai_dev_loop/integrations/codex/controller_skill/SKILL.md, plus their
  asset/install tests.
- tests/integration/test_phase17_1_submit.py,
  test_phase17_2_controller_lookup.py, test_phase17_2_tick_control.py,
  test_phase17_5_scheduler_review_loop.py, and test_phase17_6_privacy.py.
- tests/unit/scheduler/test_start.py,
  test_phase17_7_timer_ops.py, and
  test_phase17_10_reviewer_status_and_timer_path.py.
- Existing scheduler history tests and all tests that construct scheduler
  submitted contexts, authorization events, or checkpoints.
- docs/referencia/cli.md, docs/referencia/configuracion.md,
  docs/operacion/seguridad-privacidad.md,
  docs/operacion/prepare-start-resume-abort.md,
  docs/operacion/troubleshooting.md, docs/operacion/observabilidad.md,
  docs/guia/flujo-completo.md, docs/guia/guia-rapida.md, and
  docs/guia/flujo-handoff.md.

## Cursor Rules And Skills

- Follow AGENTS.md and every repository-local Cursor rule listed in Required
  Context. In particular, preserve state/schema compatibility, repository
  reservation, exact B identity, bounded loop behavior, privacy redaction,
  systemd safety, and non-destructive Git behavior.
- The relevant package-owned skill assets are ai-dev-loop-handoff and
  ai-dev-loop-controller. Update only their controller-ID command guidance
  required by this plan; do not install them, change SessionStart, bridges,
  hook files, or global integration flow.
- Use fake Cursor and Codex executables, temporary repositories, temporary
  XDG/config homes, injected clocks, and injected stores/runners in tests.
  Do not invoke real models, user systemd, external services, or the active
  Parish360 run.
- Keep changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Operator authority and reviewer identity

- A controller session ID is optional provenance only. It is not an
  authorization credential for submit, start, tick, status, history, timeline,
  abort, controller status, or timer operations.
- The explicit operator command scheduler start run-id remains the sole
  authorization transition from queued to authorized. It must remain
  transactional, idempotent, reservation-checked, and process-free.
- Never manufacture a placeholder controller ID such as operator, local, or
  an all-zero UUID. New A-free snapshots record absence as null.
- If an optional controller session ID is supplied, validate its UUID format
  and preserve it only as redacted provenance. It must not be required by
  later commands and must not grant or deny authority.
- Exact reviewer B identity remains a mandatory runtime/recovery boundary.
  This phase must not alter its fresh bootstrap, authenticated binding,
  redacted projection, resume target, model, reasoning effort, or no-last
  guarantee.

### Persistence and compatibility

- Preserve durable field names and historical state/event shapes where
  practical. Expand controller-related typed fields and schemas to accept
  null; do not rename fields, rewrite old records, or add a migration.
- Existing snapshots/events with an A ID must validate and keep their redacted
  display. New snapshots without A must validate with null provenance.
- Preserve authorization timestamps, event ordering, state-version
  compatibility, hashes, immutable artifact behavior, and reducer/CAS
  invariants. An authorization event must continue to agree with the frozen
  context, including when both provenance values are null.
- Update every affected typed JSON schema, model, writer, reader, and
  compatibility test in the same change. Do not silently reinterpret old
  state.
- Submission idempotency represents frozen executable work, not optional A
  provenance. Otherwise-identical A-free and A-bearing submissions must
  resolve to the same idempotency key and original immutable context.

### Public read projections and timing

- Status/list/controller output may expose only
  review_iterations_completed, max_review_iterations, and redacted identity
  prefixes already permitted by the existing privacy contract. The completed
  value must use the same local-review budget semantics as maximum-review
  enforcement; do not invent a competing counter.
- Add scheduler timeline run-id as a bounded, read-only projection. Its
  machine-readable entries are one row per persisted scheduler attempt, with:
  iteration, phase (cursor or codex), phase_attempt ordinal, safe terminal or
  active status, launch_requested_at, completed_at when known, and
  observed_duration_seconds when both timestamps exist.
- Compute observed_duration_seconds from existing central attempt timestamps
  only. It is elapsed time from durable launch request to durable completion,
  not a promise of child CPU/runtime and not a separate persistent fact.
  Leave it null for incomplete attempts. Do not calculate a changing
  now-minus-start duration for active work.
- The timeline must use a strict documented limit, a hard maximum,
  deterministic oldest/newest ordering, and a truncated flag. It must never
  return attempt IDs, dispatch IDs, unit/backend identities, exit diagnostics,
  artifact paths, prompts, reviews, raw output, or full session IDs.
- A missing attempt, an uncertain attempt, a failed attempt, and a repeated
  attempt for the same iteration/phase must remain distinguishable without
  guessing a successful round. The derived phase_attempt ordinal is display
  metadata only and must be deterministic.
- Reuse the central scheduler database and application/infrastructure
  boundary. Do not parse XDG artifacts, scrape history text, or expose raw
  SQLite rows through the CLI.

### Timer cadence and packaging

- The packaged timer must retain OnBootSec=30 and OnUnitActiveSec=30 and add
  exactly AccuracySec=1s in its Timer section.
- The template validator must require the three scheduling directives and
  reject missing, bare, or incorrect AccuracySec values. It must retain all
  Phase 17.10 service PATH and ExecStart checks.
- Do not add RandomizedDelaySec, Persistent, WakeSystem, an additional timer,
  shell wrapping, a loop/sleep service, or a user-specific path.
- Document the practical contract honestly: systemd may trigger within the
  one-second accuracy window and later due to OS/service conditions. It does
  not guarantee exact wall-clock intervals.
- Timer install remains the only deliberate unit-refresh operation. Packaging
  the change must not modify existing installed units or enable/disable a
  timer during automated tests.

### Public CLI and skill behavior

- Scheduler submit must no longer require controller-session-id. It must still
  require the explicit frozen B review model and reasoning effort, and still
  reject codex-session-id.
- Scheduler start must have the form scheduler start run-id with no controller
  flag. The queued safe-next-action must render that same form.
- Controller status with an exact run-id and repository path must work without
  A. It must validate repository ownership and never choose a different run.
- Preserve legacy controller-ID discovery only as an optional compatibility
  selector. Without a run-id, a caller that does not supply A must receive a
  clear validation error instead of an automatic repository/timestamp choice.
- Package-owned handoff/controller skill examples must stop requiring A or a
  controller flag for current scheduler commands, while keeping their
  documented no-last, exact-B, target-config, and no-global-mutation
  boundaries intact.
- New A-free JSON and text output must render a null controller prefix or omit
  its optional human line consistently; it must never slice a null value.
- Do not reveal full controller/reviewer IDs, prompts, review content,
  artifacts, patch contents, environments, or raw process output in any new
  public output, documentation example, or test fixture.

## Implementation Plan

### 1. Map and evolve nullable controller provenance

Trace every controller session read/write/validation from CLI parsing through
submission, context construction, idempotency material, authorization event,
reducer, checkpoints, SQLite snapshot validation, status/list, controller
read, controller rendering, package-owned skill examples, and documentation.

Make controller provenance nullable for new scheduler contexts, authorization
events/states/checkpoints, and public projections. Keep historic non-null
payloads valid. Retain durable field names rather than performing cosmetic
renames that would require a migration. Remove only validation that makes A
mandatory or compares it as authorization; retain all non-identity integrity
checks such as reservation ownership and state-version/CAS fences.

Ensure null means absence, not redaction failure or a synthetic principal.
Update every caller that assumes a string, including redacted prefix
generation, text rendering, schema validation, and legacy lookup queries.

### 2. Decouple submit, start, and exact-run reads from A

Change CLI help, SubmitOptions handling, application validation, context
builders, command rendering, and safe-action rendering so:

- scheduler submit works without controller-session-id while requiring the
  explicit B model/reasoning pair;
- a supplied controller-session-id is validated and retained as optional
  provenance;
- scheduler start run-id authorizes a queued run without session lookup or a
  mismatch branch;
- start replay remains idempotent and non-queued states still use the existing
  lifecycle contract;
- queued safe actions, examples, errors, packaged skills, and documentation
  no longer ask for an exact controller ID.

Canonicalize idempotency independently from optional A provenance.
Prove that A-free and A-bearing submissions with otherwise identical frozen
work replay one run, while distinct resubmission IDs retain the Phase 17.9
fresh-submit semantics and active reservation conflicts remain fail-closed.

Keep controller status a read-only compatibility surface. With an exact run-id,
load by run ID plus canonical repository root and do not require or filter by
A. Without run-id, retain current discovery only for an explicit legacy A plus
repository root. With neither selector, return a safe validation error; never
infer a run by repository recency.

### 3. Add bounded review counters and attempt timeline

Add the two review-budget fields to the shared scheduler summary contract,
status/list renderers, controller read result, controller rendering, and
documented JSON/text output. Derive them from the validated frozen context and
state using existing local-review-budget semantics. Do not mutate a run or add
new state fields.

Add scheduler timeline run-id with bounded limit/order behavior parallel to
scheduler history, but implement it as its own typed application service and
contract rather than repurposing/redacting history strings. Add the narrow
store query necessary to fetch only the safe attempt columns for one run,
ordered deterministically and fetched with a one-record overflow sentinel.

Map each row into the public timeline entry contract described above. Derive
phase_attempt ordinals from the full oldest-first result before applying the
selected output order, so retries for the same iteration and phase remain
visible. Be explicit
in docs that the table covers scheduler Cursor/Codex attempt phases, that
observed duration includes orchestration observation time, and that queue and
preflight time are not reported as phase duration.

Use the standard CLI JSON and concise text renderer. Do not add a timer
invocation, state mutation, artifact read, current process lookup, or
cross-run query.

### 4. Make timer accuracy explicit

Add AccuracySec=1s to the packaged timer template. Extend
validate_packaged_assets and timer asset tests so missing, wrong, or duplicate
cadence directives fail closed while the accepted package content passes.
Retain the Phase 17.10 PATH and /usr/bin/env service contract unchanged.

Update the timer reference and troubleshooting documentation with the actual
installation/refresh flow: after package installation, the operator runs
scheduler timer validate and scheduler timer install (or install --enable when
they expressly want automatic ticks). Explain why the old observed
approximately-59-second interval was valid systemd default coalescing, and
why the new packaged unit narrows it to approximately 30 seconds plus its
one-second window and normal OS/service delays.

### 5. Preserve B, timer, and global-integration boundaries

Inspect fresh-B bootstrap, reviewer binding, resume evidence,
maximum-iteration handling, abort reconciliation, timer operations, and
package skill installation only enough to prove these changes do not alter
them. The package skill asset wording must reflect no-A scheduler commands,
but this phase must not install skills, mutate hooks, change Desktop/WSL
bridging, or refactor the handoff architecture.

Keep immutable artifacts and full session IDs in their existing protected
boundaries. All new status/timeline/controller projections remain read-only
and redacted. Do not execute a real model, install/enable a timer, or operate
a live run.

### 6. Document and record findings

Update the CLI, configuration, security/privacy, operation,
observability, quick-start, complete-flow, handoff, timer, and
troubleshooting documentation so it clearly distinguishes:

- optional A provenance from exact B reviewer identity;
- no-A submit/start commands and the still-required explicit B
  model/reasoning;
- controller status selection by run-id and legacy optional discovery by A;
- review counter meanings, the timeline command/table, and observed-duration
  limits;
- the timer AccuracySec=1s cadence contract and deliberate unit refresh;
- no implied user/account authentication beyond local operating-system access.

Create
archive/implementation-history/findings/phase-18-optional-controller-provenance.md.
Record scope, exact validation results, schema/read compatibility evidence,
timeline privacy/bounding evidence, timer-template evidence, package-skill
asset impact, non-actions, residual local-access/timing tradeoffs, and the
absence of any real-run/model/systemd acceptance in this phase. Do not include
full IDs, prompts, protected artifact contents, or host-specific paths.

## Testing Criteria

Add or update focused unit and integration tests. At minimum prove:

- A new submit with no A succeeds using explicit B model/reasoning and freezes
  null provenance without reading or guessing a Codex A session.
- An invalid optional A value is rejected before writes; a valid supplied one
  is redacted in public output only.
- A-free scheduler start succeeds, retains reservation/CAS protection, and its
  repeated invocation is idempotent.
- A-free queued safe-next-action, CLI help, documentation snippets, and
  package-owned skill assets do not require a controller flag.
- Existing A-bearing submitted/authorized/admitted/terminal snapshots and
  authorization events remain readable; their prefix remains redacted.
- A-free status/list/controller status render safely with null A provenance and
  never leak a full B or legacy A identifier.
- Controller status by exact run-id plus repository works without A, rejects a
  wrong repository, and does not auto-select when both run-id and A selector
  are absent. Existing A-based discovery remains compatible.
- A-free and A-bearing equivalent submits replay the same idempotency key/run;
  resubmission-ID replay and active reservation conflict behavior remain
  unchanged.
- Reducer/store validation preserves snapshot hashes, state versions,
  authorization/checkpoint consistency, and event ordering for null and
  legacy non-null controller values.
- Existing B bootstrap/binding/resume and maximum-review tests still pass
  unchanged in behavior.
- Status/list/controller summaries expose review_iterations_completed and
  max_review_iterations with values matching the actual bounded-loop
  calculation, including the existing compatibility/local-count path.
- Timeline returns only attempts for the requested run, has deterministic
  order, enforces its default and hard maximum, sets truncated correctly,
  handles no attempts, active attempts, completed/failed/uncertain attempts,
  and repeated attempts for one iteration/phase without hiding a retry.
- Timeline duration is null unless both durable timestamps exist and otherwise
  equals the safe timestamp delta. Assert public JSON/text has no attempt or
  dispatch IDs, unit/backend identities, artifacts, prompts, raw output, or
  full session IDs.
- The packaged timer contains exactly the required OnBootSec,
  OnUnitActiveSec, and AccuracySec=1s directives. Template validation rejects
  missing/wrong AccuracySec and retains Phase 17.10 PATH/ExecStart checks.
- Timer installation/status unit-content comparisons recognize the new
  packaged timer content using temporary homes/fake systemctl boundaries only;
  no test reaches a user systemd manager.

Use fake CLIs, temporary scheduler databases/repositories, injected timestamps,
and temporary integration homes only. Do not invoke real Cursor, Codex,
systemd, network access, user-global installation, or target-worktree mutation
in tests.

## Validation

Run focused submit/start/controller/schema/privacy/history/timeline/timer and
package-skill asset tests, then:

    TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
    uv run ruff format --check .
    uv run ruff check .
    uv run mypy src
    uv run python -m build
    uv run mkdocs build --strict
    git diff --check

Record exact outcomes and skip counts in the findings artifact. If a validation
gate fails, stop, preserve evidence, and do not report the phase as complete.

## Risks Or Recovery Notes

- This intentionally removes a same-user accidental-mismatch guard. A local
  process with access to scheduler state and an exact run ID can start a queued
  run; it could already invoke other local scheduler operations. This is not a
  new multi-user authorization model.
- Repository reservation, immutable inputs, preflight, explicit start, state
  transitions, bounded attempts, abort fences, and B identity are the
  compensating scheduler boundaries and must remain intact.
- Current A-bearing runs stay immutable and operational. Do not resubmit,
  restart, normalize, inspect protected artifacts, or alter the timer merely
  because Phase 18 changes fresh-run behavior.
- Timeline durations are observed durable intervals, not exact child runtime.
  Completion discovery may add scheduler/timer cadence delay; missing or
  unsettled timestamps must remain null rather than being estimated.
- AccuracySec=1s constrains the ordinary systemd timer coalescing window but
  cannot guarantee an exact 30-second tick in all machine states. Existing
  installed units retain their previous content until an operator deliberately
  refreshes them after package installation.
- The implementation run itself may use pre-Phase-18 handoff machinery that
  supplies A. That bootstrap detail neither changes the target product's no-A
  workflow nor authorizes changes to global integrations.

## OpenQuestions

None.
