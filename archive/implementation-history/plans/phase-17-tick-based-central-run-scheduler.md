# Phase 17 — Central tick scheduler master plan

## Goal

Replace the long-lived, per-run local A/B launcher model with the final central
scheduler. There is no legacy-run compatibility or data migration requirement.
A submitted run is durable data plus protected artifacts; a short, periodically
invoked `tick` reconciles every non-terminal run and exits. It must never wait
for a Cursor or Codex turn to finish.

The only processes that may remain alive between ticks are the process-manager
owned Cursor or Codex attempts themselves (and the host process manager, such
as systemd). A run-specific Python workflow/supervisor must not stay alive for
hours while an agent is working.

The target execution sequence is:

```text
submit -> queued
tick   -> preflight -> launch Cursor -> exit
tick   -> Cursor still active -> exit
tick   -> Cursor completed -> validate -> stage -> launch Codex -> exit
tick   -> Codex still active -> exit
tick   -> Codex completed -> validate decision -> next Cursor or terminal -> exit
```

Every boundary above is durable, fenced, auditable, and recoverable after a
tick, scheduler, terminal, or WSL interruption. A completed Cursor or Codex
attempt must not be run again merely because the scheduler restarted.

## Non-Goals

- Do not create a permanently running Python scheduler daemon.
- Do not poll model APIs, parse human Markdown to drive workflow decisions, or
  use an agent's text as executable input.
- Do not weaken exact Cursor chat continuity, one-reviewer-per-run continuity
  after fresh Codex B bootstrap, plan/prompt/HEAD binding, staging contracts, or
  review-result schemas.
- Do not make tool updates, model fallback, destructive Git operations, or
  user-continuation decisions automatic.
- Do not run real Cursor, Codex, systemd, GitHub, or network operations from
  automated tests.
- Do not claim that WSL can wake itself after the entire WSL VM has stopped.

## Scope

This master phase replaces the local A/B Cursor -> stage -> Codex review loop.
It includes:

- a single authoritative central SQLite scheduler ledger and protected
  content-addressed artifacts outside target repositories;
- durable submission, effects, agent attempts, repository reservations,
  cancellation, status, and tick control;
- a one-shot `scheduler tick` command that visits due runs one at a time without
  sleeping or waiting for agents;
- a process-manager adapter that starts and later observes detached Cursor and
  Codex attempts using a stable attempt identity;
- extraction of the synchronous local loop into small preflight, agent-launch,
  staging, review-finalization, and terminalization effects;
- a systemd-user timer that invokes one short tick every 30 seconds, and
  deterministic transient units that own agent attempts;
- exhaustive fake-process crash, restart, fencing, and privacy coverage;
- active CLI, configuration, operations, and security documentation.

The final scheduler preserves the A/B identity boundary: controller A submits a
run with A's exact controller session ID plus explicit Codex reviewer model and
reasoning effort. Submission creates no B. At the first review boundary, the
worker creates one fresh read-only Codex B, captures its exact identity, and
resumes only that B for later reviews in the run. A explicitly starts the run
and may query its state without owning a long-lived worker.

## Frozen Product Decisions

- **Agent-led orchestration:** the scheduler exists to make the Codex reviewer
  and Cursor implementer collaborate through durable, reviewable hand-offs. It
  carries out staging and, where an explicitly approved phase provides it,
  commit decisions that those agents have made; it must never invent a Git
  decision or replace their technical judgment with broad worktree policing.
  This principle alone does not add a commit operation to a child phase.
- **Scope:** only the local A/B Cursor -> stage -> Codex loop moves to the new
  scheduler. `pr-review`/PR-review v2 are retired at cutover; they are not
  migrated into this phase and no GitHub workflow is implemented here.
- **Authority:** a new generic SQLite authority is created at
  `$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3`, with one protected artifact tree
  at `$XDG_STATE_HOME/ai_dev_loop/artifacts/`. It is the only mutable authority
  for the final product workflow.
- **Control:** `ai_dev_loop scheduler submit` creates a frozen, no-agent run;
  `ai_dev_loop scheduler start <run-id>` records the explicit authorization;
  only later ticks launch agents. Scheduler status for controller A is a
  read-only query keyed by the persisted exact controller session ID and target
  repository. `submit` requires `--codex-review-model` and
  `--codex-review-reasoning-effort`; their normalized values are frozen in the
  run and never inherited from YAML, a runtime session, or later CLI defaults.
- **Runtime:** WSL `systemd --user` is required. A timer invokes a one-shot tick
  every 30 seconds. Deterministic transient units own Cursor/Codex attempts and
  preserve liveness and exit status after the tick exits. WSL wake-up after a
  full shutdown is not a product requirement.
- **Concurrency:** one active Cursor/Codex attempt globally and one active run
  per resolved repository worktree. A conflicting worktree submission is
  rejected rather than queued.
- **Recovery:** the scheduler automatically advances only verified durable
  outcomes. Drift, malformed/partial output, tool incompatibility, external
  termination, and identity ambiguity block with a safe action. A recognized
  Cursor usage limit is special: preserve the exact chat and verified partial
  work fingerprint, schedule a durable retry for provider `retry-after` when
  available or five hours from observation otherwise, and launch the exact
  continuation on the first tick after it is due. It never selects a fallback
  model automatically.
- **Cutover cleanup:** after all implementation phases and manual acceptance,
  the only deletable legacy state roots are
  `$XDG_STATE_HOME/ai_dev_loop/runs/` and
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/`, including their SQLite WAL/SHM
  sidecars and artifacts. Preserve config, cache, `codex-sessions`, hooks, and
  installed skills.

## Out of Scope

- GitHub PR review, publication, pushes, comments, or external-write recovery.
- A Windows Scheduled Task or any promise to awaken an entirely stopped WSL VM.
- Actual systemd enablement or user-global deletion in automated tests.
- A cron fallback: cron may never be treated as an equivalent attempt owner
  because it cannot by itself retain a detached child's exit evidence.
- Keeping `prepare`, `launch`, `resume`, `recover`, `pr-review`, legacy run
  schemas, or PR-review persistence as supported public behavior after cutover.
- Autonomous or unapproved commits; push, reset, clean, stash,
  checkout/switch, unstage, merge, rebase, tag, or target-repository cleanup.

## Required Context

Read these materials before changing code. Current code, schemas, CLI help,
tests, and `docs/` are authoritative when they conflict with archived history.

- All eight `.cursor/rules/*.mdc` files listed below.
- `README.md`, `docs/referencia/cli.md`, `docs/referencia/configuracion.md`,
  `docs/operacion/estado-artefactos.md`, `docs/operacion/seguridad-privacidad.md`,
  and `docs/operacion/troubleshooting.md`.
- `archive/implementation-history/master-plan.md`, especially the A/B loop,
  XDG, process, locking, and recovery contracts.
- `archive/implementation-history/plans/phase-14-remote-controller-and-review-fork.md`,
  `phase-16-4-pr-review-v2-durable-engine.md`,
  `phase-16-7-pr-review-v2-complete-orchestration.md`, and
  `phase-16-9-pr-review-v2-cutover-and-legacy-test-cleanup.md` for the existing
  durable-engine and cutover lessons.
- `PHASE_16_8_DEFERRED_ISSUES.md`, which documents unresolved durability risks
  that must not be silently copied into a new scheduler.
- `src/ai_dev_loop/workflow_engine.py`, `state.py`, `resume_planner.py`,
  `launcher.py`, `launch_worker.py`, `locking.py`, `abort_control.py`, and
  `process.py`.
- `src/ai_dev_loop/runners/cursor.py`, `runners/codex.py`, `runners/staging.py`,
  `runners/git.py`, and `commands/{prepare,launch,start,resume,abort,controller}.py`.
- `src/ai_dev_loop/pr_review_v2/{application,infrastructure,workers}/`, in
  particular its SQLite transaction, event/outbox, claim, lease, timer, and
  protected-artifact patterns. Reuse proven primitives only where their
  semantics fit; do not couple the local loop to the PR-review reducer.
- Existing test fixtures and regressions: `tests/conftest.py`,
  `tests/unit/test_launcher_safety.py`, `tests/unit/test_process.py`,
  `tests/unit/test_locking.py`, `tests/unit/test_iterations_and_resume.py`,
  `tests/integration/test_launch_worker.py`, `tests/integration/test_start_cursor.py`,
  `tests/integration/test_start_codex_review.py`, and
  `tests/integration/test_phase5_loop_resume.py`.

## Cursor Rules And Skills

Follow these repository-local Cursor rules, all of which are present and
`alwaysApply`:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Relevant repository skills:

- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` governs the
  later, independent staged review; it must not be invoked by the implementation
  itself.
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` governs
  evidence-grounded operational documentation and acceptance handoff.

There is no repository `AGENTS.md` and no `.cursor/skills/` directory at plan
creation time.

## Architecture Guardrails

### Authority and persistence

- `engine.sqlite3` and its validated artifact references are the sole workflow
  truth. Do not keep a mutable `state.json` projection, legacy run index, or
  PR-review database as a second authority.
- A scheduler run must have typed, versioned state; append-only durable events;
  version/CAS fencing; an outbox of effects; and an immutable, hash-verified
  input context. A persisted summary may be an output projection only; it must
  never become an unsynchronized second authority.
- Use SQLite transactions (`BEGIN IMMEDIATE` for ownership/mutations), WAL,
  `foreign_keys=ON`, `synchronous=FULL`, user-only permissions, schema audit,
  and migration tests. Never hand-edit SQLite rows or infer state from logs.
- Store plan/prompt bytes, frozen effective configuration, agent output, staged
  patches, session IDs, process metadata, and result artifacts outside the
  target repository. Use safe relative references, size limits, atomic writes,
  hash re-reads, and `0700`/`0600` permissions.

### Tick semantics

- `scheduler tick` acquires one global scheduler-leader claim, processes a
  finite snapshot of eligible runs deterministically/fairly, and exits. It never
  has a `sleep`, `while waiting`, or per-run long-lived supervisor loop.
- A tick may perform bounded local work (preflight, artifact validation, staging
  and durable transitions) and may launch at most one long-running agent attempt
  per run. It must commit each completed boundary before beginning the next.
- It must not hold a SQLite transaction, database claim, ordinary file lock, or
  process pipe while waiting for an external agent. Tick overlap, repeated timer
  delivery, and manual concurrent invocation must be harmless.
- An active attempt is observed through a backend-specific, validated identity,
  not by a bare PID, log presence, elapsed time, or command-line substring.

### Agent-attempt protocol

- Represent every external invocation as a typed durable attempt with a random
  non-secret attempt ID, effect ID, run ID, component, iteration, immutable
  invocation hash, launch nonce, timeout/deadline, backend job identity,
  validated PID/PGID/start-time when available, and safe artifact references.
- The attempt lifecycle is explicit: `launch_requested -> active ->
  exit_observed -> result_verified -> finalized`, with terminal blocked,
  cancelled, and uncertain forms as necessary. The scheduler must reconcile a
  `launch_requested` attempt before ever starting another process.
- Use a stable backend job/unit name derived from the attempt ID so a crash after
  launching but before recording the response cannot create a duplicate agent.
  The next tick asks the backend for that exact identity and either adopts the
  same job or proves that no job was started.
- The backend must preserve an authoritative exit result and separately captured
  stdout/stderr. A detached child reparented without an exit-status owner is not
  sufficient. Do not implement a cron-only design that loses this evidence.
- Never use `shell=True`, shell snippets, inherited full environments, or raw
  prompts in metadata. Use argv arrays, explicit CWD, redacted summaries, exact
  stdin delivery, process groups/cgroups, timeouts, and bounded output files.
- The selected backend must permit abort to stop only the exact owned agent
  group/unit after durable cancellation is recorded. Late results must fail the
  attempt/effect fence and never mutate an aborted/superseded run.

### Repository integrity and agent identity

- A durable repository reservation, plus an OS-level lock held for the duration
  of a live mutating agent when required by the selected backend, prevents two
  scheduler runs or a legacy run from working on one worktree. A file lock held
  only by a finished tick is not a reservation.
- Preflight reuses the existing identity, plan, prompt, frozen reviewer binding,
  and tool-compatibility contracts. A non-interactive tick never prompts or
  implicitly runs a tool updater.
- Preflight is a one-time admission check before the first Cursor launch: it may
  reject an unexpectedly dirty starting worktree when frozen
  `require_clean_worktree` is true, verify the submitted repository target still
  resolves to the same root, capture branch/HEAD/status as a protected admission
  artifact, and verify the frozen inputs. Submit binds only the repository target;
  it does not capture Git baseline semantics. Once admitted, the scheduler is not
  a continuous worktree-control system. The Codex reviewer and Cursor implementer
  decide, through their durable exchange, what work is accepted, staged,
  corrected, or committed when that operation is in scope. Later ticks retain
  reservation, artifact-integrity, and non-destructive safety boundaries, but must
  not add broad Git-semantic emulation or reject incidental worktree evolution
  merely to police it.
- Cursor chat creation and every Cursor turn must be distinct durable effects;
  after a chat exists, reuse that exact ID only. Codex creates one new reviewer
  B at the first review, durably captures that exact ID, and subsequently resumes
  only it. It never uses `--last`, a fork, or a replacement reviewer.
- Preserve the current iteration definitions, post-Cursor fingerprints,
  agent-approved `git add -A` normalization, staged-patch verification, review
  schema, and maximum-review behavior. The reducer/workflow state determines
  decisions; Markdown is never scraped.
- If a completed output, repository fingerprint, target HEAD, staged patch, or
  process identity is ambiguous, stop in a typed blocked/uncertain state with a
  safe action. Do not blindly relaunch an agent or reinterpret partial output.

### Privacy, compatibility, and documentation

- Default CLI/status/history/log output remains redacted: no prompts, patches,
  raw agent output, review Markdown, auth data, full session IDs, raw PIDs/PGIDs,
  or full environments.
- Replace the legacy command surface only in the final cutover phase. Until
  then, implementation may retain legacy code as an internal refactoring aid but
  must not create new legacy runs or claim it is supported.
- `ai_dev_loop.yaml`, installable skills, and integration/bridge files are
  control-plane surfaces. Any later change needs a manual acceptance decision;
  automated scheduler tests alone are insufficient.

## Implementation Plan

### 0. Follow the phase boundary sequence

The product decisions above are fixed. Execute the child plans serially; each
phase must pass its focused and regression suites before the next begins. Do
not combine phases in one Cursor handoff or skip a required acceptance boundary.

1. [Phase 17.1 — Central ledger and A/B submission](phase-17-1-central-ledger-and-submission.md)
2. [Phase 17.1.5 — Fresh Codex reviewer bootstrap and scheduler alignment](phase-17-1-5-ephemeral-cursor-reviewer-contract.md)
3. [Phase 17.1.75 — Agent-led worktree admission](phase-17-1-75-agent-led-worktree-admission.md)
4. [Phase 17.2 — Tick control, reservations, and controller observability](phase-17-2-tick-control-reservations-and-controller-status.md)
5. [Phase 17.3 — Systemd attempt executor](phase-17-3-systemd-attempt-executor.md)
6. [Phase 17.4 — Cursor, staging, and usage-limit continuation](phase-17-4-cursor-staging-and-usage-limit-continuation.md)
7. [Phase 17.5 — Codex review and bounded scheduler loop](phase-17-5-codex-review-and-bounded-scheduler-loop.md)
8. [Phase 17.6 — Abort, recovery hardening, and operations](phase-17-6-abort-recovery-and-operations.md)
9. [Phase 17.7 — Clean cutover and acceptance](phase-17-7-clean-cutover-and-acceptance.md)

### 1. Define the isolated scheduler domain and persistence boundary

After the authority decision:

1. Add an isolated scheduler package, for example
   `src/ai_dev_loop/scheduler/`, with typed domain states/events/effects,
   reducer, application service, store, artifact reader/writer, process-backend
   protocol, and tick runner. Keep CLI rendering and system integration outside
   the domain.
2. Define a strict `SubmittedRunContext` that freezes: target repository
   identity inputs, plan/prompt snapshots and hashes, effective config and its
   hash, explicit Codex reviewer model/reasoning binding, requested Cursor
   configuration, controller identity when applicable, and configured
   iteration/time limits. It contains no B session before the first review; the
   first review later adds one validated protected B identity. It must be
   complete enough for a future tick to run without rereading mutable YAML or
   prompt/plan source files.
3. Create a versioned central schema. At minimum it needs migration audit,
   scheduler runs/snapshots, journal events, durable effects, timers/due times,
   tick-leader leases, per-run/effect claims, agent attempts, and repository
   reservations. Put protected large/sensitive content in hashed artifact roots,
   with only validated references and digests in SQLite.
4. Implement bootstrap, schema checksum, downgrade/newer-schema rejection,
   safe migration transactionality, atomic create-or-reuse, compare-and-swap
   snapshot update, and fence-aware attempt/effect completion. Do not copy the
   PR-review schema verbatim. The final generic engine is not a PR-review
   compatibility layer.
5. Add a read-only status/history projection from the central ledger. Its
   `safe_next_action` must be mechanically consistent with the control operation
   that accepts it.

### 2. Add durable submission without launching agents

1. Add the approved public submission surface:
   `ai_dev_loop scheduler submit`) and typed command service. It reads the exact
   prompt from stdin and writes only the central ledger plus protected snapshots.
2. Submission may validate CLI shapes, repository/path confinement, plan/prompt
   bytes, controller-session syntax, explicit reviewer model/reasoning values,
   and duplicate/conflict keys, but it must not perform start preflight, tool
   probes, model calls, agent launches, staging, Git mutation, or a network
   operation.
3. Calculate an idempotency key from the frozen submission identity. An identical
   live submission reuses its run; a conflicting active reservation must report
   the precise safe conflict without choosing by timestamp.
4. Persist `queued` only after every referenced snapshot has been written,
   fsynced/re-read, and hash verified. Orphaned content-addressed artifacts may
   be harmless; a database row must never reference a partial artifact.
5. Freeze the reviewer model/reasoning values explicitly supplied at submission.
   Do not capture, read, or infer B runtime/session data there. The first review
   bootstrap must later persist one identity from a strict structured Codex event;
   if that event cannot be validated, block rather than create or resume B.

### 3. Implement a bounded, deterministic scheduler tick

1. Add `ai_dev_loop scheduler tick` as a non-interactive one-shot command. It
   acquires an expiring global leader claim, captures the eligible-run set, and
   releases the claim on every exit path. A second tick returns a safe busy/no-op
   result rather than overlapping work.
2. For each eligible run, apply this order using short transactions around state
   only:

   ```text
   cancellation request -> active-attempt reconciliation -> due timer/retry
   -> result ingestion/finalization -> synchronous local effect -> agent launch
   -> next run
   ```

   Do not hold a transaction across filesystem reads, Git, process-manager calls,
   agent launch, or subprocess output parsing.
3. An active backend job produces only an observation. The tick does not renew a
   fictional process heartbeat, block for completion, or replace a job because a
   fixed interval elapsed. A missing/unknown backend result goes through the
   attempt reconciliation state machine.
4. After a verified Cursor result, persist the Cursor-completed checkpoint and
   post-Cursor fingerprint. It may then perform the bounded staging effect and
   launch a Codex attempt in the same tick only after the staging artifacts and
   state transition are durable. Each boundary must remain independently
   recoverable if the tick exits between them.
5. Enforce the selected fairness, maximum-runs-per-tick, maximum-launches, and
   same-repository rules. A run blocked on an agent cannot starve unrelated
   eligible repositories.

### 4. Implement the process-manager adapter and attempt reconciler

1. Introduce a narrow `AgentProcessBackend` interface with `launch`, `observe`,
   and `terminate` operations. Production uses the systemd backend; tests use a
   deterministic fake backend with controllable
   liveness, result, crash, timeout, and stale-identity cases.
2. For the systemd design, derive an opaque deterministic transient-unit
   identity from the attempt ID. Launch must be idempotent against that identity;
   observe must obtain active/inactive state, main exit result, and validated
   unit/process ownership without exposing raw identifiers to user output.
3. Persist launch intent before the backend call. If the tick dies after the
   backend accepts the unit but before the response is stored, the next tick
   discovers that exact unit by identity and adopts it. If no unit exists, only
   then may it launch the recorded intent. This closes the Popen/register crash
   window that the current per-run launcher handles synchronously.
4. Redirect stdout/stderr to attempt-specific owner-only artifacts before agent
   execution; write an atomic completion envelope containing backend outcome,
   exit status, timeout/cancellation classification, and artifact hashes. Never
   treat a partial output file as success.
5. Carry process-group/cgroup ownership, timeout, abort, and output limits at
   the backend boundary. A timeout or external kill without a durable abort
   request is an interruption/uncertain failure, never user abort.
6. Reserve the repository before an agent launch and release it only after the
   terminal attempt result is fenced and all immediately dependent bounded work
   has completed. During transition development, detect active legacy locks and
   fail closed rather than claiming a target already controlled by a legacy
   worker; after cutover no legacy launcher may be created.

### 5. Refactor the local workflow into scheduled effects

1. Extract reusable, bounded primitives from `workflow_engine.py`; do not call
   `_continue_workflow`, `start_run`, or `resume_run` from a tick. Their current
   synchronous `while` loop and process-wait behavior are incompatible with this
   architecture.
2. Model at least these scheduler effects/checkpoints:

   ```text
   preflight_and_probes
   create_cursor_chat
   run_cursor_turn
   ingest_cursor_result
   normalize_and_record_staging
   bootstrap_or_resume_codex_review
   ingest_review_result
   terminalize_or_schedule_cursor_fix
   ```

   Keep agent launch/observation distinct from result ingestion. The review
   decision must come solely from the existing schema-validated result.
3. Reuse, rather than duplicate, existing validation functions for repository
   identity, plan/prompt hashes, correction preconditions, Cursor output
   fingerprints, `git add -A`, staged patch capture, Codex runtime, and result
   parsing. Refactor only after characterization tests prove the existing
   behavior.
4. Keep Cursor chat creation a durable external effect. Its successful chat ID
   must be persisted and bound before an implementation/fix turn is launchable.
   A scheduler restart cannot create a second chat when artifacts prove the
   first request succeeded.
5. Preserve current review-budget semantics exactly: review 01 follows the
   initial implementation; reviews 02+ follow exact Codex-authored fix prompts;
   no Cursor turn launches after the maximum review iteration; terminal source
   runs remain immutable.
6. Treat unsupported automatic cases (tool update request, malformed/partial
   output, identity drift, ambiguous process result,
   or an unproven interrupted Cursor turn) as typed `blocked`/`needs_operator`
   states. Print a redacted, specific recovery action; do not reuse the current
   automatic interactive prompt behavior inside a timer. For a classified Cursor
   usage limit, write a protected fingerprint/continuation envelope and schedule
   `retry_due` at provider `retry-after` or the frozen five-hour fallback. On the
   first due tick, run that continuation in the exact existing chat and model;
   reclassify a repeated limit without choosing another model.

### 6. Implement control, abort, and compatibility surfaces

1. Add scheduler status, history/list, and abort controls. Status must identify
   a queued run, due local work, active agent attempt, retry time, terminal
   result, blocker class, and safe next action without leaking sensitive data.
2. Scheduler abort first appends a durable cancellation event and invalidates
   claims; then it asks the selected backend to stop only the exact owned unit
   or process group. Preserve target contents, staged changes, artifacts, and
   partial output. A late result cannot cross the cancellation fence.
3. `controller status` must query the central ledger by exact controller-session
   ID and repository, while ordinary scheduler status queries by run ID. It must
   never guess an ambiguous run by timestamp. Legacy controls are removed only
   by the cutover child plan.
4. Do not alter root `ai_dev_loop.yaml` during implementation. If an optional
   `scheduler` configuration section is approved, update the typed config model,
   JSON schema, config validation, test fixtures, CLI docs, and manual
   control-plane acceptance checklist together.

### 7. Add the selected periodic trigger as a packaging boundary

1. Keep `scheduler tick` fully usable manually; it is the only behavior
   automated tests need to execute.
2. If systemd user timer is selected, package a minimal, parameter-free service
   and timer template that invoke the installed `ai_dev_loop scheduler tick`.
   It must not contain user paths, prompts, secrets, repository paths, shell
   composition, or a Python sleep loop. Document explicit install/enable,
   disable, logs, WSL availability, and that a stopped WSL instance needs an
   external Windows trigger if that behavior is desired.
3. Unit installation/enabling is a user-global mutation. It needs a separate
   explicit command/confirmation contract, idempotency/ownership checks, and
   manual acceptance; do not perform it as a side effect of submission or tests.

### 8. Document, validate, and hand off honestly

1. Update active docs only after implementation: CLI reference, configuration,
   artifacts/state layout, operations/recovery, security/privacy, troubleshooting,
   and the main workflow guide. Explain scheduler-vs-legacy ownership and the
   fact that a tick does not wait for agents.
2. Document backend assumptions and stop conditions accurately. Do not claim
   automatic recovery of ambiguous attempts, WSL wake-up, or full PR-review
   integration without acceptance evidence.
3. Create `archive/implementation-history/findings/phase-17-central-scheduler-handoff.md`
   with the exact commands/tests run, fake-backend evidence, manual systemd
   validation status, residual risks, migration/cutover status, and unimplemented
   follow-up work.

## Testing Criteria

Automated tests are mandatory. Use temporary native-WSL XDG/HOME directories,
fake `agent`/`codex` executables, injected fake clocks, fake process backend,
and disposable Git repositories. Do not invoke real models, systemd, GitHub,
network, user-global services, or the real user's XDG state.

### Unit and schema coverage

- Add scheduler-domain/reducer tests for every accepted/rejected event and
  state/effect transition, including same-tick Cursor completion -> staging ->
  Codex launch.
- Add schema/model alignment and migration tests: pristine bootstrap, schema
  checksum drift, newer DB rejection, mid-migration fault rollback, foreign-key
  constraints, compare-and-swap conflict, and concurrent submission/tick
  serialization.
- Test idempotent submit/reuse, active conflict detection, immutable execution
  context, artifact hash/path validation, traversal/symlink/FIFO refusal, and
  `0600`/`0700` handling where supported.
- Test attempt launch windows: crash before launch, after intent, after backend
  starts, after result exists, after result verification, and after effect
  completion. Assert one stable backend job and no duplicated Cursor/Codex call.
- Test liveness/identity result combinations: active, completed-success,
  completed-nonzero, timeout, cancellation, backend unavailable, stale identity,
  missing exit envelope, and backend job already removed. All ambiguous cases
  must block or reconcile safely rather than relaunch.
- Test tick leadership, no overlap, bounded ordering/fairness, due/retry
  handling, per-run launch limits, and absence of sleeps/waits in the tick path.

### Local-loop and process integration coverage

- Create focused scheduler integration tests (for example under
  `tests/integration/test_phase17_scheduler_*.py`) that drive a fake backend
  through multiple independent ticks. Prove a tick exits while fake Cursor or
  Codex is still active, then the next tick observes completion and performs the
  exact next transition.
- Cover initial Cursor and correction Cursor turns, exact chat reuse, one fresh
  Codex B bootstrap followed by exact resume of that identity, frozen
  model/reasoning argv, absence of `--last`/fork/replacement B, structured
  no-finding and finding decisions, review-budget exhaustion, and no extra
  agent turn.
- Prove existing post-Cursor identity/fingerprint/staging behavior remains
  identical: Cursor index mutation normalization, agent-approved `git add -A`,
  patch capture, staged-patch equality before review/fix, initial-preflight
  baseline/identity refusal, and plan/prompt integrity. Do not turn this
  coverage into continuous worktree policing after agents have begun their
  reviewed exchange.
- Test a tick crash at every durable boundary, then a new tick. Verify no
  completed agent invocation is rerun, no second Cursor chat appears, and the
  expected result is either progressed or safely blocked.
- Test concurrent runs on distinct repositories make fair progress; same
  repository runs respect the selected reservation policy; an active legacy
  worker blocks scheduler mutation of its worktree.
- Test scheduler abort while an agent is active, before launch, after exit but
  before ingestion, and with stale metadata. It must preserve repository state,
  refuse unrelated signals, and fence late results.
- Test non-interactive tool incompatibility, model usage-limit, malformed output,
  and incomplete turns become truthful operator-action states. No timer path may
  prompt, update a tool, or select a fallback model.
- Keep the legacy launcher, A/B controller, resume/recover, PR-review v2, and
  global-integration suites passing unchanged unless a separately approved
  compatibility step edits their public behavior.

### Documentation and acceptance coverage

- Assert CLI help/status JSON are stable, bounded, and redacted.
- If configuration changes are approved, test absent-section backward
  compatibility, strict `extra=forbid`, ranges/cross-field validation, schema
  alignment, and frozen per-run effective settings.
- Test unit-template rendering/ownership/argument safety with a fake filesystem;
  do not enable a real unit in CI. Manual WSL/systemd acceptance is required
  before claiming the timer production-ready.

Run at least the focused suites introduced by this phase, then:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

Run the complete suite with the native `/tmp` variables after focused tests
pass. If an environment issue blocks a command, report the full command, exact
failure, and which focused evidence remains valid; do not convert it to a skip.

## Validation

Before implementation is considered complete, demonstrate and record these
fake-backend traces:

1. Submit writes only the central run/context/artifacts; no preflight, probe,
   Git, Cursor, Codex, or backend call occurred.
2. Tick 1 preflights and launches initial Cursor, commits its attempt record,
   and exits. The worker process is not a long-lived run orchestrator.
3. Tick 2 observes that Cursor is active and exits without an extra launch.
4. Tick 3 observes a verified Cursor result, captures the fingerprint, stages
   safely, records the staged patch, launches the one fresh read-only Codex B
   review attempt using the frozen model/reasoning binding, and exits.
5. Subsequent ticks execute a findings correction using the same Cursor chat,
   then terminate at no findings or the exact review limit.
6. Crash/restart between every launch/result/finalization boundary preserves
   one backend job, one chat, one review decision, and no duplicate effects.
7. Cancellation, stale/ambiguous process evidence, repository conflict, plan
   drift, and malformed agent output fail closed without destructive Git action.
8. All human/JSON surfaces are inspected for prompt, patch, review, token,
   session-ID, and process-identity redaction.
9. If systemd is selected, perform a separately authorized manual WSL test of
   one enabled timer and one fake agent unit; record the unit lifecycle,
   `systemctl --user` observation, restart/reconciliation behavior, disable
   cleanup, and any inability to wake a stopped WSL VM.

## Risks Or Recovery Notes

- A periodic tick does not by itself create resilience. The hard problem is
  exactly-once/at-most-once reconciliation of an external process launched just
  before a crash. Stable backend job names, launch intent, observed exit status,
  attempt fencing, and verified artifacts are mandatory.
- A detached process with no retained parent exit status cannot distinguish a
  normal completion from an external kill. Do not use raw `Popen(...,
  start_new_session=True)` plus PID polling as the production backend unless the
  approved design supplies an equivalent durable exit-status owner.
- A database reservation alone does not protect against an external user
  process. The implementation must retain an agent-owned OS lock across ticks;
  during transition development it must also fail closed when it finds a legacy
  worker.
- Timers provide eventual, not instantaneous, progress. Completion latency is
  bounded by the selected interval plus one bounded tick. Backoff and retry due
  times must be durable; repeated timer delivery must be idempotent.
- If WSL is completely stopped, neither cron nor a systemd user timer in that
  distro runs. An external Windows trigger is a deployment decision, not a
  hidden product guarantee.
- The local loop currently uses `state.json` and a synchronous workflow engine;
  conversion is a deliberate breaking cutover. Do not delete the two approved
  legacy XDG roots until the Phase 17.7 explicit cleanup boundary has passed.
- New configuration, user systemd installation, root `ai_dev_loop.yaml`, skills,
  and Codex integration assets are control-plane changes. They require manual
  acceptance even after all fake-backend tests pass.

## OpenQuestions

None. The user approved all product decisions in **Frozen Product Decisions**.
If implementation discovers that a decision cannot satisfy an existing safety
contract, stop and report the concrete incompatibility rather than changing the
decision or adding a compatibility path unilaterally.
