# Phase 16.7 — Complete `pr-review-v2` orchestration

## Goals

Implement the first complete, locally testable `pr-review-v2` orchestration path on top of the Phase 16.3 domain, Phase 16.4 durable engine, Phase 16.5 GitHub read gateway, Phase 16.6 idempotent write/reconciliation layer, and the Phase 16.2 extracted local review loop.

The phase must:

- expose a temporary, isolated top-level CLI namespace `ai_dev_loop pr-review-v2`;
- add an optional, isolated top-level project configuration section named `pr_review_v2`;
- support both origins already modeled by the domain:
  - `SourceRunOrigin`, created from an existing completed source run;
  - `ExistingPrOrigin`, prepared from an already-open pull request;
- make `create` and `prepare` read-only preparation operations that end in a durable `PreparedState` and never start workers, agents, GitHub writes, commits, pushes, or model calls;
- make explicit `start RUN_ID` the only transition from prepared data to externally effective work;
- provide durable `start`, `status`, `history`, `resume`, and `abort` control operations;
- execute every production effect type through the same durable outbox/claim/fencing engine, including the three LOCAL effects:
  - `GeneratePublicationTextEffect`;
  - `AdjudicateThreadsEffect`;
  - `RunLocalFixEffect`;
- preserve the exact frozen Codex reviewer session, exact Cursor chat continuity, exact review snapshot/binding, and exact effective configuration across crashes and resumes;
- complete a fully simulated multi-round workflow for both origins without invoking the legacy `pr-review` implementation;
- produce an evidence-based handoff to Phase 16.8.

The following product decisions are frozen for this plan:

1. The public namespace is `pr-review-v2`; the legacy `pr-review` namespace remains unchanged.
2. Configuration lives under `pr_review_v2`; the legacy `github` configuration remains unchanged.
3. Both `create SOURCE_RUN_ID` and `prepare ...` only validate, freeze inputs, and create/reuse a durable prepared run.
4. `start RUN_ID` is the sole gate for starting the worker or allowing external effects.
5. Resume of a `waiting_for_user` run requires an explicit operator acknowledgement flag; it must not silently manufacture user continuation.
6. No SQLite migration is added unless implementation proves an existing invariant impossible; such a discovery is a stop condition, not permission to improvise a migration.

## Non-Goals

- Do not perform Phase 16.8 live acceptance, real GitHub fault injection, or real end-to-end agent execution.
- Do not perform the Phase 16.9 cutover, compatibility cleanup, or removal of the legacy `pr-review` path.
- Do not change legacy public behavior, legacy state files, legacy recovery semantics, or legacy configuration meanings.
- Do not add merge, close, retarget, force-push, branch deletion, or other destructive Git/GitHub operations.
- Do not redesign the Phase 16.3 reducer or add a second orchestration state machine outside it.
- Do not make log text, PID existence alone, or artifact directory presence the source of workflow truth.
- Do not allow LOCAL executors to bypass the durable outbox, claim token, completion fence, protected artifact rules, or reducer.
- Do not use `resume --last` or any implicit Codex session selection.
- Do not run real Cursor, Codex, model, network, GitHub write, commit, or push operations in automated tests.
- Do not invoke the staged-change review skill from Cursor. The external A/B orchestrator owns staging and review.

## Scope

### Public CLI contract

Add a separate Typer subtree at `ai_dev_loop pr-review-v2` with these operations. Exact option spelling may follow existing CLI conventions, but the semantics below are mandatory and must be documented and tested.

1. `create SOURCE_RUN_ID`
   - Resolve and validate the source run locally.
   - Freeze its plan, prompt, repository identity, base/head identity, Cursor chat identity, Codex reviewer session, effective runtime settings, limits, and required artifact hashes.
   - Create or idempotently reuse a `SourceRunOrigin` v2 run in `PreparedState`.
   - Perform no agent/model invocation, worker launch, GitHub write, commit, push, or remote mutation.

2. `prepare --repo OWNER/REPO --pr NUMBER`
   - Use only typed read-only GitHub/local Git discovery needed to adopt the existing PR.
   - Reject ambiguous, closed, mismatched, unsafe, or conflicting identities before creating the run.
   - Freeze an `ExistingPrOrigin`, plan/prompt inputs, current binding, sessions, effective settings, limits, and artifact hashes.
   - Create or idempotently reuse a durable `PreparedState`.
   - Perform no agent/model invocation, worker launch, GitHub write, commit, push, or remote mutation.

3. `start RUN_ID`
   - Require `PreparedState` and a hash-verified execution context.
   - Apply the existing domain start event durably before launching anything.
   - Spawn or reuse the dedicated v2 supervisor using validated launcher metadata.
   - Be idempotent after the state transition: if the run is already active and its owned supervisor is absent, repair only the supervisor; never duplicate the domain event or effect.
   - If spawning fails, preserve the already-durable pending effect and report the safe retry action.

4. `status RUN_ID`
   - Read SQLite snapshot/outbox/timer truth plus privacy-safe owned-launcher liveness.
   - Report state, origin, cycle/iteration counters, configured limits, pending/claimed/retry state, last durable transition, resumability, safe next action, and bounded recent history.
   - Never expose raw prompts, model output, thread bodies, tokens, full opaque identifiers, raw command lines, PID/PGID, or environment values.

5. `history RUN_ID`
   - Return a bounded, oldest/newest selectable durable journal view using redacted event/effect summaries.
   - Enforce a configured/hard maximum; no unbounded journal dump.

6. `resume RUN_ID`
   - For a paused resumable state, apply the existing resume event and spawn/reuse the supervisor.
   - For an active state with no owned live supervisor, repair only the supervisor.
   - Reject prepared runs with the exact next action `start`.
   - Reject terminal/non-resumable states with the exact safe action or explanation.
   - When state is `waiting_for_user`, require an explicit flag such as `--confirm-user-continuation`; persist protected operator-continuation evidence, then apply `UserContinuationRequested`. Without the flag, remain read-only and print the required action.
   - Never fire future timers early merely because resume was requested.

7. `abort RUN_ID`
   - Persist the v2 abort transition first.
   - Then terminate only an exactly validated, locally owned Cursor/Codex child or v2 supervisor using token, PID, PGID, process start time, and ownership metadata.
   - Never signal based on stale/partial metadata and never signal Git/GitHub write subprocesses after their durable result boundary is uncertain.
   - Be idempotent and report whether local process termination was performed, unnecessary, or safely refused.

### Configuration contract

Add optional `ProjectConfig.pr_review_v2` and update `src/ai_dev_loop/schemas/project-config-v1.json`. Keep configuration schema version `1`; this is an optional backward-compatible section.

Freeze and validate a shape equivalent to:

```yaml
pr_review_v2:
  enabled: false
  gh_command: gh
  git_command: git
  ssh_command: ssh
  remote_name: origin
  base_branch: master
  reviewer_logins:
    - chatgpt-codex-connector
  review_trigger_body: "@codex review"
  user_mention: rojobad
  external_review_skill: review-github-pr-feedback
  poll_interval_seconds: 60
  max_external_cycles: 8
  max_local_iterations: 3
  per_call_timeout_seconds: 60
  overall_timeout_seconds: 180
  max_pages: 20
  max_items: 500
  max_server_directed_wait_seconds: 3600
  no_findings:
    enabled: false
    accepted_comment_prefixes: []
    reviewed_commit_prefix_length: 12
  worker:
    lease_ttl_seconds: 30
    heartbeat_interval_seconds: 10
    idle_poll_seconds: 1
```

Implementation may reuse existing `cursor`, `codex`, and `workflow` settings as source values, but all effective values used by a prepared run must be copied into its protected execution context. Subsequent project-config edits must not mutate an existing run.

Validation must include:

- positive bounded durations/counts and `heartbeat_interval_seconds < lease_ttl_seconds`;
- command fields as executable names/paths, never shell snippets;
- argv-safe repository, branch, remote, login, mention, and prefix values;
- rejection of secret-like keys/inline credentials;
- SSH-only production publication transport, while tests may inject LOCAL fakes;
- non-empty allowlisted no-findings prefixes when no-findings completion is enabled;
- preservation of `extra="forbid"` behavior;
- disabled-by-default behavior with no impact on configurations that omit the section.

Do not edit the repository's `ai_dev_loop.yaml` as part of this phase.

### Durable preparation and conflict rules

- Define versioned, strict, secret-free execution-context and operator-continuation schemas.
- Store run-owned artifacts below the canonical Phase 16.6 hashed run root and apply the same no-follow, regular-file, owner-only mode, size-bound, canonical JSON, atomic replace, directory fsync, SHA-256, and re-read validation guarantees.
- Preparation order must be deterministic:
  1. parse CLI/config;
  2. validate local repository identity and clean/supported prerequisites;
  3. validate exact Cursor/Codex session/runtime inputs;
  4. obtain only the required read-only source/PR identity;
  5. freeze protected artifacts and hashes;
  6. construct the typed origin and `PreparedState`;
  7. atomically create/reuse the engine run.
- Add a `BEGIN IMMEDIATE` create-or-reuse operation that checks active conflicts and writes the initial snapshot/journal atomically. Content-addressed files created before the DB transaction may be orphaned safely, but SQLite must never reference a partial/unverified artifact.
- Exact same prepared identity returns the existing run. Conflicting active ownership must reject at least:
  - the same source run;
  - the same `(normalized owner/repository, PR number)`;
  - the same normalized repository/head publication branch where concurrent ownership would be unsafe.
- Do not infer ownership from logs or broad filesystem scans.

### LOCAL effect execution

Add an injected `LocalEffectExecutor` to `EffectExecutorRouter`. All three LOCAL effects must be routed in production and must execute under the same claim token/fence as read, write, and reconciliation effects.

For every LOCAL effect:

- load only the hash-verified frozen execution context and effect inputs;
- validate effect kind, state binding, cycle, head SHA, expected thread set, and schema version before any agent call;
- persist raw agent output only in bounded owner-only scratch storage if required for parsing, then remove it; never expose it through status/history/errors;
- persist a strict, canonical, protected result artifact before returning an event;
- on retry, reuse an already-complete result only after re-reading and verifying its hash/binding;
- classify temporary timeout/launch failures as retryable only when replay is safe;
- classify deterministic schema, identity, binding, provenance, and artifact failures as blocked/non-retryable with a safe action;
- let `EffectWorker.complete_claim` be the only path that commits the resulting event to the engine.

#### `GeneratePublicationTextEffect`

- Consume only the effect's protected `evidence_ref` and `patch_ref` plus the frozen execution context; never reconstruct prompt/session material from state, logs, or ambient configuration.
- Add a narrow v2 Codex publication runner that always resumes the exact frozen reviewer session.
- Invoke Codex with an explicit session identifier; never use `--last`.
- Require a versioned strict output schema, bounded stdout/stderr, timeout handling, and privacy-safe errors.
- Convert the validated model output into the exact Phase 16.6 publication/commit artifacts and feed them through their existing protected readers before returning the domain outcome.
- Never grant this runner Git/GitHub write authority.

#### `AdjudicateThreadsEffect`

- Consume only the Phase 16.5 frozen, sanitized review snapshot and exact Phase 16.3/16.6 binding.
- Resume the same exact reviewer session used for publication generation.
- Require an exact decision for every eligible thread and reject omissions, duplicates, additions, stale head/cycle values, invalid reply/fix combinations, or provenance drift.
- Persist a strict external-adjudication result whose decisions, fix text, and proposed replies can be transformed into the existing domain outcome without heuristics.
- Never read live GitHub thread state inside the model runner.

#### `RunLocalFixEffect` and the Phase 16.2 boundary

Use `run_local_review_fix()` as the sole local correction boundary. Do not import or call the legacy PR review adapter/state machine.

Freeze this adapter design:

- Each v2 external cycle owns one deterministic subordinate Phase 16.2 local carrier run. Its identity is derived from the v2 run ID, cycle, and `RunLocalFixEffect` identity, so crash recovery reopens the same carrier rather than creating a duplicate.
- The v2 SQLite outbox remains the sole scheduling truth. The subordinate carrier exists only to reuse the extracted local review loop's exact Cursor/Codex/chat/iteration contracts; it must not become a competing GitHub workflow.
- For `SourceRunOrigin`, seed the first carrier from the exact frozen source plan/prompt/runtime/chat/session inputs.
- For `ExistingPrOrigin`, seed the first carrier from the protected prepared plan/prompt/runtime inputs; a new Cursor chat may be created only inside the first local-fix execution.
- Carry the exact Cursor chat identifier and exact Codex reviewer session from one cycle's protected result into the next cycle's new deterministic carrier.
- Bind each carrier to the effect's current pre-commit HEAD, not to a stale source-run baseline.
- A successful local fix records `new_head_sha` as that exact unchanged pre-commit HEAD. The loop does not create the Git commit; the later `CommitRecordedOutcome` remains the only transition that installs the newly created commit SHA.
- The accepted finalizer must atomically persist a terminal subordinate carrier result with `needs_external_continuation=True`, then produce a protected v2 `local_fix_result` that includes the accepted patch/result references and carried-forward chat/session identity.
- Copy any carrier artifact needed by v2 into the v2 run root using hash verification and atomic protected writes. Never retain an unverified cross-root path reference.
- Map every `LocalReviewFixResult` explicitly: accepted/accepted-with-residual-risk require protected accepted patch, exact pre-commit HEAD, and result refs; max-iterations/paused/failed require typed reason and safe action; aborted carries no accepted patch or new head.
- `PAUSED` or interrupted local work must preserve a resumable `RunningLocalFixState` carrier. Apply the smallest domain/reducer correction needed so `LocalFixFinishedOutcome(PAUSED)` retains resumable state; `FAILED` and limit-exhausted results remain non-resumable unless the existing domain contract explicitly says otherwise.
- Enforce both domain `max_local_iterations` and local-loop iteration accounting. Never reset an iteration counter on resume.
- If the extracted boundary cannot support this subordinate-carrier contract without importing legacy PR review code or weakening exact-session guarantees, stop and record an `OpenQuestion`; do not invent a parallel local loop.

### Supervisor and runtime assembly

Add a dedicated v2 supervisor/worker entry point rather than routing through the legacy PR-review worker.

- Assemble runtime dependencies only from the hash-verified frozen execution context.
- Reuse the generic process-safety primitives where their contracts fit; do not change legacy launcher behavior to accommodate v2.
- Persist private launcher metadata with token, PID, PGID, process start time, run binding, and executable identity.
- On reuse/abort, validate every ownership field before signaling or trusting liveness.
- Process one claimed effect at a time, heartbeat long-running LOCAL effects, complete through the claim fence, fire only due timers for the selected run, and use bounded interruptible idle waits.
- Add a run-filtered engine timer operation if the current API can only fire timers globally.
- Never hold a SQLite transaction or effect lease operation open while sleeping or waiting on GitHub/agent subprocess I/O.
- Exit when terminal, safely paused, or waiting for explicit user continuation. Restart entirely from durable SQLite/artifact truth.
- A stale worker that returns after abort/lease loss must be unable to apply its result because `complete_claim` fencing rejects it.

### Status, history, resume, and abort

- Extend status DTOs without leaking domain internals or private artifact content.
- Derive status/history from snapshots, journal, outbox, timers, and validated launcher metadata.
- Include precise safe next actions such as `start`, `resume`, `resume --confirm-user-continuation`, wait-until timestamp, inspect protected failure class, or no action for terminal states.
- Keep journal rendering bounded and redact/hash opaque identifiers consistently.
- Persist abort before local process termination and preserve the durable late-result fence.
- Ensure restart/recovery can distinguish:
  - pending effect;
  - valid active claim;
  - expired claim recovered to the existing safe state;
  - scheduled retry timer;
  - waiting-for-user;
  - locally owned live supervisor;
  - absent/stale launcher metadata.

### Documentation and handoff

Update at least:

- `docs/referencia/cli.md`;
- `docs/referencia/configuracion.md`;
- `docs/operacion/seguridad-privacidad.md`;
- any existing architecture/operations page whose statements become incomplete.

Documentation must clearly label `pr-review-v2` as temporary/pre-cutover, explain the explicit `start` safety gate, distinguish preparation from execution, document recovery/abort/status semantics, and state that Phase 16.7 automated evidence is simulated rather than live GitHub acceptance.

Create:

`archive/implementation-history/findings/phase-16-7-to-16-8-handoff.md`

The handoff must include the exact CLI/config/schema contracts implemented, file inventory, subordinate local-carrier lifecycle, worker lifecycle, validation commands/results, simulated E2E evidence for both origins and multiple cycles, privacy/safety evidence, unresolved risks, and the live scenarios/fault injection reserved for Phase 16.8.

## Out of Scope

- Any edit to the legacy `pr-review` command implementation beyond registering the separate new CLI subtree.
- Any import from legacy PR review state/recovery/worker modules into `pr_review_v2`.
- Any edit to `ai_dev_loop.yaml`, `.cursor/rules`, `.agents/skills`, global integrations, installed hooks, or user/global configuration.
- SQLite schema migration or version bump without a demonstrated invariant failure and an amended approved plan.
- Real GitHub authentication, real network calls, real agent/model execution, commits, pushes, PR creation, comments, replies, reactions, or review requests during tests.
- Phase 16.8 live acceptance and Phase 16.9 migration/removal.

## Required Context

Cursor must read these before editing:

1. `pr-review-v2-restructure-context.md` — especially Phase 16.7, cross-phase invariants, and the Phase 16.8/16.9 boundaries.
2. `archive/implementation-history/findings/phase-16-6-to-16-7-handoff.md`.
3. Approved plans and clarifications for Phases 16.1 through 16.6 under `archive/implementation-history/plans/`.
4. The complete `src/ai_dev_loop/pr_review_v2/` domain, application, infrastructure, workers, migrations, and tests.
5. `src/ai_dev_loop/local_review_loop.py`, its Phase 16.2 tests, and only the generic local workflow types/helpers it directly depends on.
6. `src/ai_dev_loop/cli.py`, `src/ai_dev_loop/config.py`, `src/ai_dev_loop/schemas/project-config-v1.json`, `src/ai_dev_loop/process.py`, and existing launcher/abort controls as reference contracts.
7. `docs/referencia/cli.md`, `docs/referencia/configuracion.md`, `docs/operacion/seguridad-privacidad.md`, and the relevant architecture/operations pages.
8. Legacy PR review modules may be inspected only to identify prohibited coupling or parity requirements; they are not reusable dependencies for v2.

## Cursor Rules And Skills

All repository Cursor rules are mandatory and `alwaysApply`:

- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`

No repository `.cursor/skills/` directory or applicable repository `AGENTS.md` exists at plan time. If either appears before execution, Cursor must discover and obey it before editing.

Cursor must not invoke `.agents/skills/review-staged-ai-dev-loop-execution`; the A/B orchestrator invokes review from the separate reviewer session after staging.

## Architecture Guardrails

1. **Reducer is pure.** No I/O, clocks, random IDs, process state, Git/GitHub calls, or agent calls in domain reduction.
2. **SQLite is workflow truth.** Artifacts carry immutable payload/evidence; launcher metadata carries local ownership only; neither replaces durable state.
3. **Single effect path.** LOCAL effects use the same outbox, claims, leases, heartbeat, retry, fencing, journal, and reducer path as all other effects.
4. **Explicit write authority.** Agent runners and local correction never receive GitHub write or publication authority. Existing Phase 16.6 gateways remain the only write paths.
5. **Explicit start gate.** Preparation may read required identities but cannot launch workers/agents or mutate Git/GitHub state.
6. **Exact session continuity.** Freeze and use explicit Cursor chat and Codex reviewer session IDs; no “latest/last” fallback.
7. **Exact binding.** Every result echoes and validates run/effect/cycle/head/thread-set/schema bindings before state transition.
8. **Protected artifacts.** Preserve Phase 16.5/16.6 hashed-root, no-follow, regular-file, owner-only, bounded, atomic, hash-verified rules.
9. **Private failures.** Do not persist raw stdout/stderr, prompts, thread bodies, replies, environment, argv, tokens, or secrets in SQLite/status/history/errors.
10. **No legacy coupling.** `pr_review_v2` must not import/call legacy PR review state, recovery, adapter, or worker code. Shared generic local-loop/process primitives are allowed only through their public contracts.
11. **No shell composition.** Build argv arrays, use explicit cwd/environment allowlists, bound output, and avoid `shell=True`.
12. **Crash-safe idempotency.** A crash at any boundary must replay/reconcile without duplicate writes, duplicate local carriers, duplicate chats, duplicate commits, or lost durable progress.
13. **Abort fence first.** Persist cancellation before signaling exactly owned local process groups; stale late completions must fail the claim fence.
14. **No migration by convenience.** Prefer existing journal/snapshot/outbox/timer schema. Stop if a new invariant truly requires a migration.
15. **Preserve A/B.** Shared local workflow, launcher, process, config, or CLI changes require Phase 16.1 and legacy regression evidence.

## Implementation Plan

1. **Baseline and dependency map**
   - Run the focused Phase 16.1–16.6 regression suites before editing.
   - Record a dependency/import map proving current v2 isolation and identify every intended shared-file change.
   - Confirm worktree state and preserve unrelated user changes.

2. **Configuration and public control contracts**
   - Add strict `pr_review_v2` config models and JSON Schema definitions with disabled defaults and cross-field validation.
   - Add typed command request/result DTOs for create/prepare/start/status/history/resume/abort.
   - Define stable privacy-safe exit/error categories and exact safe-next-action values.

3. **Protected execution/preparation artifacts**
   - Add versioned strict execution-context, operator-continuation, publication-generation, external-adjudication, and local-fix-result schemas/readers/writers.
   - Reuse/factor Phase 16.6 protected file primitives where safe; do not weaken existing readers.
   - Add tests for no-follow, modes, canonical encoding, atomic interruption, hash mismatch, size limits, unknown fields, secret-like data, and binding drift.

4. **Preparation services and atomic create/reuse**
   - Implement source-run and existing-PR preparation as application services with injected read-only dependencies.
   - Add atomic active-conflict detection plus prepared-run create/reuse to the durable store/engine without changing the SQLite schema.
   - Prove identical retries reuse a run and conflicting ownership cannot race into two active runs.
   - Assert zero worker/agent/write/publication calls during both preparation paths.

5. **Narrow exact-session Codex runners**
   - Implement separate v2 publication-generation and thread-adjudication runners.
   - Use exact explicit session resume, strict schemas, bounded subprocess results, safe environment, timeouts, and private errors.
   - Validate complete/exact thread coverage and transform only validated results into protected artifacts/domain outcomes.

6. **Subordinate Phase 16.2 local-fix adapter**
   - Implement deterministic per-cycle carrier identity and idempotent initialization/reopen.
   - Seed both origins correctly and carry chat/session identity across cycles.
   - Call only `run_local_review_fix()` and add a safe accepted finalizer that persists the terminal carrier plus protected v2 result.
   - Preserve current pre-commit HEAD semantics and copied-artifact verification.
   - Make the smallest reducer/state correction required for resumable `PAUSED` local work and add serialization/reducer regressions.

7. **LOCAL executor and router integration**
   - Add `LocalEffectExecutor` with injected runners/adapter/artifact stores.
   - Route all production LOCAL effects and map results to existing events.
   - Implement cached-result replay, retry classification, heartbeat cooperation, claim-loss handling, and private errors.
   - Keep `EffectWorker.complete_claim` as the only state-application boundary.

8. **Dedicated v2 supervisor and launcher**
   - Add a v2 worker entry point/runtime factory and a dedicated safe supervisor launcher.
   - Add run-filtered due-timer execution if needed.
   - Implement liveness/metadata validation, one-effect processing, bounded waits, durable restart, and safe exits.
   - Test idempotent start/resume launch, spawn failure recovery, stale metadata, process identity mismatch, and no transaction/lease held during waits.

9. **CLI control plane**
   - Register only the new `pr-review-v2` subtree in the shared CLI.
   - Wire exact command semantics and privacy-safe rendering.
   - Ensure `start` persists the state transition before spawn; `abort` persists abort before signal; `resume` never skips waiting-user acknowledgement or fires timers early.
   - Keep all legacy command help/output/behavior unchanged.

10. **Simulated multi-round integration workflow**
    - Build injected fake GitHub read/write transports, fake Git publication remote, fake Cursor/Codex runners, fake clock, and deterministic failure hooks.
    - Exercise both origins through prepare/create → start → publication/adoption → external review → adjudication → GitHub replies → local fix → commit/push → next external cycle → no-findings completion.
    - Include at least two external cycles and one local correction per origin.
    - Assert exact sessions, chat continuity, cycle/head/thread bindings, single writes, single commit/push intents, durable artifacts, and no legacy imports/calls.

11. **Recovery, limit, abort, and concurrency integration tests**
    - Restart before/after every LOCAL result-artifact boundary and before/after claim completion.
    - Test partial artifacts, cached complete artifacts, expired claims, late stale completion, retry timers, two competing workers, waiting-user acknowledgement, external/local limits, and non-resumable failures.
    - Test abort during long fake Cursor and Codex children, stale/mismatched launcher metadata, and refusal to signal unowned processes.

12. **Regression, docs, and Phase 16.8 handoff**
    - Run all focused and full validations below.
    - Update CLI/config/security/operations documentation without claiming live acceptance.
    - Create the Phase 16.7 → 16.8 handoff with exact evidence, residual risks, and live scenarios still required.
    - Do not stage, commit, push, or invoke review; leave intended changes for the external orchestrator.

## Testing Criteria

### Unit criteria

- Config omission/disabled defaults preserve old configs; invalid values, extras, secrets, unsafe argv fields, and invalid lease/heartbeat/no-findings combinations fail closed.
- Every protected artifact reader/writer passes path, symlink, mode, type, size, canonical JSON, hash, schema, binding, and interruption tests.
- Publication/adjudication runners prove exact session use and absence of `--last`, shell composition, ambient secret inheritance, and unbounded output.
- Adjudication rejects missing/duplicate/extra/stale thread decisions and invalid reply/fix combinations.
- Local adapter proves deterministic per-cycle carriers, source/adopted seeding, exact chat/session carry-forward, pre-commit HEAD semantics, iteration monotonicity, paused resumability, and verified artifact transfer.
- Router executes all LOCAL/read/write/reconciliation effect classes and rejects unknown/mismatched classes.
- Supervisor/launcher tests prove full process identity validation and privacy-safe status.
- Status/history are bounded, deterministic, state-derived, and redacted.

### Integration criteria

- Both origins produce a hash-verified `PreparedState` with zero external writes/agents/worker launch.
- `start` is the only external execution gate and is idempotent across spawn failure/retry.
- Both origins complete a fully simulated multi-round workflow including external adjudication, GitHub replies, local correction, publication, next-cycle review, and no-findings completion.
- Restart at each LOCAL boundary neither duplicates nor loses work.
- Two workers cannot both commit one effect result; a stale/aborted worker cannot apply a late result.
- Waiting-user continuation requires explicit acknowledgement evidence.
- Local/external limits and abort produce the exact documented terminal/paused state and next action.
- Legacy `pr-review` command/state/recovery tests remain unchanged and passing.

### Automated-test prohibitions

Automated tests must not use real network, GitHub credentials, Cursor, Codex, model calls, commits, pushes, PRs, comments, replies, or reactions. Use local bare repositories, injected transports/runners, fixtures, deterministic clocks, and fake subprocesses. Subprocess argv construction may be tested without executing the real binary.

## Validation

Run from the repository root using native temporary filesystems. Do not redirect SQLite/artifact/locking tests to `/mnt/c` or another DrvFS mount.

```bash
uv run pytest -q tests/integration/test_phase16_1_ab_regression_barrier.py
uv run pytest -q tests/unit/test_phase16_2_local_review_contracts.py tests/integration/test_phase16_2_local_review_boundary.py
uv run pytest -q tests/unit/pr_review_v2 tests/integration/test_phase16_4_durable_engine.py
uv run pytest -q tests/unit/test_config.py tests/unit/test_github_config.py tests/unit/test_process.py tests/unit/test_launcher_safety.py tests/unit/test_abort_control.py
uv run pytest -q tests/integration/test_abort.py
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
git status --short
```

Also run the new Phase 16.7 focused unit/integration files directly and record their exact names/results in the handoff. If shared launcher, abort, local-workflow, or process code changes, add and run the corresponding focused legacy regression suites before the full suite.

Validation is not complete if tests assert only final state. Multi-round/recovery tests must also assert journal/outbox counts, artifact hashes/bindings, exact sessions/chat, write idempotency keys, worker fencing, and absence of legacy v2 imports.

## Risks Or Recovery Notes

- The largest seam is adapting a locally terminal Phase 16.2 review run into a multi-cycle external v2 workflow. The deterministic per-cycle subordinate carrier keeps the generic loop unchanged while preserving exact chat/session continuity; do not replace it with legacy PR state or an untracked loop.
- `new_head_sha` at local acceptance is deliberately the current pre-commit HEAD. Predicting the future commit SHA would corrupt the reducer binding; only the later commit-recorded event may advance it.
- Preparation needs read-only identity discovery but no execution. Dependency-injection tests must prove a worker, model call, or write cannot be reached before `start`.
- Agent subprocess timeout/abort can leave a late result. Durable abort plus claim fencing must make it harmless even if OS termination is safely refused.
- Content-addressed artifacts written before atomic run creation can be orphaned after a crash. That is acceptable; a database reference to a partial or unverified artifact is not.
- No-findings completion remains allowlist-driven and disabled unless explicitly configured; never infer success from silence.
- If current SQLite tables cannot express atomic conflict ownership or bounded history without a migration, stop, document the concrete invariant and proposed migration, and request plan amendment.
- If repository conditions or new rules contradict this plan, stop without weakening safety contracts and record the conflict for the user.

## OpenQuestions

None.
