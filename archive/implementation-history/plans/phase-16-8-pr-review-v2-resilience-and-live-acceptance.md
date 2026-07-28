# Phase 16.8 — `pr-review-v2` resilience and controlled live acceptance

Date: 2026-07-23

## Goals

Phase 16.8 must turn the Phase 16.7 simulated orchestration into an
evidence-backed, production-boundary-tested and live-accepted
`pr-review-v2` implementation.

The phase must:

1. build a complete effect contract and crash-window matrix for READ, LOCAL,
   MUTATING, RECONCILING, timer, lease, supervisor, control and abort
   boundaries;
2. exercise those boundaries through persistent SQLite reopen, real
   application/runtime assembly, stateful fake CLIs/processes and temporary Git
   repositories rather than only same-process mocked E2E tests;
3. prove that every known crash window either replays/reconciles idempotently
   or leaves one precise durable safe action without duplicating a write;
4. add an opt-in, fail-closed no-findings rule for the real Codex review bot's
   thumbs-up reaction on the exact review-trigger comment;
5. preserve privacy, exact Codex session and Cursor chat continuity, protected
   artifacts, reducer purity, durable outbox/claim fencing and explicit
   `start` authority;
6. keep the Phase 16.1 A/B implementation loop and all legacy `pr-review`
   behavior unchanged;
7. complete one controlled multi-round live cycle on a new PR in
   `rojobad/parish360-poc`; and
8. produce an evidence-based Phase 16.8 -> 16.9 handoff without performing the
   Phase 16.9 cutover.

Phase 16.8 has two ordered acceptance gates:

- **Gate A — implementation and automated acceptance:** Cursor may edit
  `ai_dev_loop`, add tests and run fake/local validation. It must not call live
  GitHub, Cursor or Codex for the controlled PR.
- **Gate B — controlled live acceptance:** only after Gate A is green and the
  staged A/B review has no actionable findings may the controller execute the
  separately authorized live run against `rojobad/parish360-poc`.

Phase 16.8 is not complete until both gates pass. Gate A alone must be reported
honestly as `live acceptance pending`.

## Non-Goals

- Do not make `pr-review-v2` the default or replace the public legacy
  `pr-review` namespace.
- Do not implement Phase 16.9 cutover, legacy lifecycle deletion, state cleanup
  or migration.
- Do not migrate or resume historical legacy PR-review runs.
- Do not add a distributed workflow engine, external state-machine library,
  cloud worker or multi-user support.
- Do not make GitHub/model calls part of automated tests.
- Do not make live fault injection intentionally corrupt GitHub, Git history or
  the user's existing PR #1.
- Do not generalize reaction semantics beyond the narrowly verified
  no-findings thumbs-up rule needed by the controlled bot.
- Do not improve or modernize the Parish360 application except for minimal,
  isolated acceptance fixtures and repository-local agent/config assets needed
  for the live cycle.
- Do not merge, close or retarget the controlled PR automatically.

## Scope

### Public/configuration contract

Extend the optional `pr_review_v2.no_findings` configuration with one
disabled-by-default boolean:

```yaml
pr_review_v2:
  no_findings:
    enabled: false
    accepted_comment_prefixes: []
    accept_bot_thumbs_up: false
    reviewed_commit_prefix_length: 12
```

Required semantics:

- omission preserves all Phase 16.7 behavior;
- `enabled: true` requires at least one configured evidence rule:
  non-empty `accepted_comment_prefixes` or
  `accept_bot_thumbs_up: true`;
- `accept_bot_thumbs_up` recognizes only GitHub reaction content `+1`;
- the matching reaction must be on the exact trigger comment selected by the
  current effect marker;
- its actor must match the configured reviewer allowlist;
- its timestamp must be present and strictly after the trigger;
- the observation must contain no eligible review threads;
- zero matches means normal bot polling, not success;
- multiple matching reactions, conflicting no-findings evidence, missing
  provenance or malformed evidence fail closed;
- a reaction on an older trigger, another comment, another cycle or another
  actor never completes the run.

The verified observation artifact must preserve bounded, owner-protected,
hash-verified and privacy-safe reaction evidence sufficient to audit the
decision. Do not infer no-findings merely from the presence of an arbitrary
reaction ID. Keep comment-prefix evidence working unchanged.

The additive persisted execution-context/observation changes must have an
explicit compatibility strategy. Prefer optional fields with safe defaults
under the current v1 artifact contract only if tests prove old Phase 16.7
artifacts remain readable and retain their old semantics. Otherwise introduce
an explicit versioned reader; do not silently reinterpret old artifacts.

### Systematic automated resilience

Add deterministic coverage for:

- GitHub read/polling and observation persistence;
- publication-generation and external-adjudication LOCAL effects;
- the real Phase 16.2 local carrier boundary;
- every Git/GitHub mutation and its reconciler;
- retry timers and explicit retry-batch reset;
- leases, heartbeat loss and competing workers;
- detached supervisor launch/restart/repair;
- start/resume/waiting-user/abort control paths;
- corrupt/drifted protected evidence and repository/PR identity; and
- privacy, permissions and human-output redaction.

### Controlled live target

The authorized external target is `rojobad/parish360-poc`. PR #1 is read-only
calibration evidence and must not be mutated by this phase.

Planning-time evidence:

- PR #1 is open, same-repository, base `main`, head `rba-test`;
- the Codex connector produced an actionable inline thread;
- the connector's GraphQL login is `chatgpt-codex-connector`;
- the bot describes a clean review as a thumbs-up reaction;
- the original local checkout at
  `/home/rojobad/Projects/parish360-poc` contains unrelated modified/untracked
  user files and is behind the observed remote `main`;
- no repository-local `AGENTS.md`, Cursor rules/skills, `ai_dev_loop.yaml` or CI
  workflow exists there at planning time.

Gate B must use a new branch and PR in a dedicated clean checkout. It must not
stage, overwrite, stash, reset, clean, switch or otherwise alter the dirty
original checkout.

## Out of Scope

- Legacy PR review modules, states, recovery paths and schemas except for
  regression verification that they remain unchanged.
- Phase 16.9 command routing and deletion of legacy code.
- Changes to the Phase 16.2 public local-review boundary unless a failing
  production-boundary test proves a narrowly scoped correctness defect.
- Changes to shared process, abort, CLI or configuration behavior unrelated to
  `pr_review_v2`.
- Any secret, credential, token, SSH key or full Codex session ID in plans,
  prompts, docs, logs, SQLite, GitHub comments or test fixtures.
- The user's dirty Parish360 checkout and existing PR #1.
- Merge, force-push, rebase, branch retarget, branch deletion, PR closure or
  destructive rollback.
- Installing global skills or modifying global Codex/Cursor configuration.

## Required Context

Cursor must read these before editing:

1. `pr-review-v2-restructure-context.md`, in full, especially Phase 16.8,
   Phase 16.9 and the cross-phase questions;
2. `archive/implementation-history/findings/phase-16-7-to-16-8-handoff.md`;
3. `archive/implementation-history/plans/phase-16-7-pr-review-v2-complete-orchestration.md`;
4. the approved plans and handoffs for Phases 16.1 through 16.6 when a touched
   seam originated there;
5. all of `src/ai_dev_loop/pr_review_v2/`, its schemas and all unit/integration
   tests under `tests/unit/pr_review_v2` and `tests/integration/test_phase16_*`;
6. `src/ai_dev_loop/local_review_loop.py`,
   `src/ai_dev_loop/pr_review_v2_carrier.py`,
   `src/ai_dev_loop/pr_review_v2_supervisor_worker.py`,
   `src/ai_dev_loop/pr_review_v2/runtime_factory.py`,
   `src/ai_dev_loop/commands/pr_review_v2.py`,
   `src/ai_dev_loop/config.py` and
   `src/ai_dev_loop/schemas/project-config-v1.json`;
7. the shared process/launcher/abort modules only when a failing test requires a
   shared change;
8. `docs/referencia/cli.md`, `docs/referencia/configuracion.md`,
   `docs/operacion/seguridad-privacidad.md`,
   `docs/operacion/observabilidad.md`,
   `docs/operacion/prepare-start-resume-abort.md` and
   `docs/operacion/troubleshooting.md`; and
9. for Gate B only, the clean isolated checkout of
   `rojobad/parish360-poc`, its exact prepared branch/PR binding and the
   protected exact Codex reviewer session supplied by the user.

Legacy PR-review modules may be inspected only to detect prohibited coupling or
regression. They are not implementation dependencies for v2.

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

No repository `.cursor/skills/` directory or applicable repository
`AGENTS.md` exists at plan time. If either appears before execution, Cursor
must discover and obey it before editing.

Cursor must not invoke
`.agents/skills/review-staged-ai-dev-loop-execution`; the external A/B
orchestrator invokes that skill from the isolated reviewer session after
staging.

The controlled target currently has no repository agent rules or skills. Gate B
may add only two minimal target-local skills if the frozen configuration
requires them:

- one read-only staged-change review skill compatible with the structured local
  review contract; and
- one external-feedback adjudication skill compatible with the structured
  adjudication contract.

Those target skills must be simple, test-only, contain no secrets or session
IDs and must not grant GitHub write authority to an agent.

## Architecture Guardrails

1. **Reducer purity.** No filesystem, SQLite, Git, GitHub, process, clock,
   random or agent behavior enters the domain reducer.
2. **SQLite authority.** Snapshot, event journal, outbox, timers, claims and
   leases remain the workflow source of truth. Test call ledgers and launcher
   metadata never replace durable state.
3. **One effect path.** Every READ, LOCAL, MUTATING and RECONCILING result still
   reaches state only through `EffectWorker` and `complete_claim`.
4. **Persist intent first.** No write occurs before its effect/claim is durable.
   Ambiguous writes reconcile before any retry.
5. **Fencing.** Run version, dispatch, claim, effect, attempt, lease generation,
   cycle and bound head SHA are validated after long calls. Late results after
   lease loss, restart or abort cannot advance state.
6. **Exact agent identity.** Always resume the exact prepared Codex session and
   exact Cursor chat. Never use `--last`, infer a session/chat or create a
   replacement chat after progress exists.
7. **Deterministic local carrier.** A restart reopens the same per-cycle carrier.
   It cannot seed a second carrier, duplicate a chat or bypass the sole
   `run_local_review_fix()` boundary.
8. **Protected evidence.** Large/sensitive evidence stays in bounded,
   no-follow, regular, owner-protected, atomically written and hash-verified
   artifacts. SQLite/events/status/history carry only safe summaries and refs.
9. **No-findings fail-closed.** A thumbs-up is success only when author,
   content, timestamp, exact trigger, cycle/head binding and absence of eligible
   threads all verify. A generic reaction is acknowledgment only.
10. **No test-only production escape hatch.** Prefer stateful fake executables,
    process termination and injected constructor protocols. Any new crash hook
    must be narrow, default-no-op, unavailable through project YAML and outside
    the reducer.
11. **Production-boundary tests.** Integration tests must assemble real v2
    gateways/executors/runtime/store and use fake CLI processes or temporary
    local Git remotes; monkeypatching the function under test is insufficient.
12. **Explicit live gate.** Cursor's Gate A implementation may not create a
    live PR, call live `gh`, invoke real Cursor/Codex or start a v2 run. Gate B
    is a controller operation after staged acceptance.
13. **Target isolation.** Never touch the dirty
    `/home/rojobad/Projects/parish360-poc` worktree. Gate B uses a new clean,
    pinned checkout and leaves it plus the PR/branch available for audit.
14. **No destructive live action.** No merge, force-push, retarget, reset,
    clean, stash, unstage, branch deletion or automatic rollback.
15. **Abort first.** Persist abort before signaling only exactly owned process
    groups. Leave remote and local evidence intact for manual rollback.
16. **A/B stability.** Shared local loop, process, launcher, abort, CLI or
    config changes require Phase 16.1 regression-barrier evidence and a manual
    acceptance review.
17. **No legacy coupling.** V2 must not import or call legacy PR lifecycle,
    recovery, worker or state code.
18. **Honest completion.** Automated green tests do not imply live acceptance;
    a live run that pauses safely is evidence of safety but not a successful
    Phase 16.8 exit.

## Implementation Plan

### 1. Freeze the baseline and write the effect/crash contract matrix

- Confirm the `ai_dev_loop` worktree and staged index before editing. Preserve
  unrelated user changes if the baseline differs from planning time.
- Run the Phase 16.1 A/B barrier and focused Phase 16.3-16.7 suites before
  changes.
- Add a durable Phase 16.8 test matrix/helper that enumerates every effect by:
  authority class, immutable inputs, protected output, idempotency target,
  retry/reconcile rule, lease-expiry behavior, abort behavior and crash
  checkpoints.
- Make the matrix executable/parameterized where practical so a future effect
  cannot be added without selecting a resilience policy.
- Record intended shared-file changes. Keep them empty unless a failing test
  proves a shared correction is necessary.

Expected new test areas:

- `tests/integration/phase16_8_helpers.py`
- `tests/integration/test_phase16_8_fault_matrix.py`
- `tests/integration/test_phase16_8_local_restart.py`
- `tests/integration/test_phase16_8_supervisor_restart.py`
- `tests/integration/test_phase16_8_privacy.py`

Names may be adjusted to repository conventions, but do not put all scenarios
in one unreviewable file.

### 2. Implement verified bot thumbs-up no-findings evidence

- Add `pr_review_v2.no_findings.accept_bot_thumbs_up` with default `false` to
  the Pydantic config and `project-config-v1.json`.
- Change validation so enabled no-findings requires a textual-prefix rule or
  the explicit reaction rule.
- Freeze the value in the protected execution context and keep old v1 context
  artifacts readable with old semantics.
- Select a reaction only after fetching the reactions for the exact selected
  trigger comment.
- Require reviewer allowlist match, content `+1`, a non-null timestamp after
  the trigger and no eligible threads.
- Treat multiple matches, comment/reaction contradictions, missing provenance,
  wrong actor/content, old-trigger reactions and reactions without a usable
  timestamp as non-success or typed fail-closed evidence, as appropriate.
- Persist a typed sanitized reaction evidence record, not only a reaction ID.
  Keep existing comment evidence readable and unchanged.
- Ensure `VerifiedNoFindingsOutcome` continues to reference a hash-verified
  observation bound to the current head/cycle.
- Update execution-context/protected-result compatibility tests, config tests,
  gateway tests, artifact tests, executor tests, schema tests and docs.
- Add a regression proving an `eyes` acknowledgment never becomes no-findings.

Likely source touchpoints:

- `src/ai_dev_loop/config.py`
- `src/ai_dev_loop/schemas/project-config-v1.json`
- `src/ai_dev_loop/commands/pr_review_v2.py`
- `src/ai_dev_loop/pr_review_v2/application/execution_context.py`
- `src/ai_dev_loop/pr_review_v2/application/github_read.py`
- `src/ai_dev_loop/pr_review_v2/infrastructure/github_read_gateway.py`
- `src/ai_dev_loop/pr_review_v2/infrastructure/review_artifacts.py`
- `src/ai_dev_loop/pr_review_v2/runtime_factory.py`

Do not change the domain outcome merely to encode transport-specific reaction
details if the existing verified observation ref is sufficient.

### 3. Build production-boundary deterministic fakes

- Reuse Phase 16.1 fake-agent/fake-Codex patterns, Phase 16.4 persistent SQLite
  helpers and Phase 16.6 temporary Git/bare-remote helpers.
- Add a stateful fake `gh` executable or process runner that exercises the
  actual argv/parsing gateway boundary and durably records sanitized call
  counts. It must support:
  - REST/GraphQL reads and bounded pagination;
  - timeout, DNS/network-like exit, HTTP 429, primary/secondary rate limit and
    5xx fixtures;
  - apply-then-timeout ambiguous writes;
  - APPLIED, PROVEN_NOT_APPLIED and UNRESOLVED reconciliation;
  - malformed, duplicate and contradictory evidence; and
  - trigger reactions including `eyes`, `+1`, wrong actor and wrong trigger.
- Use real local Git repositories and bare SSH-like remotes for commit/push
  behavior. Do not contact a real remote in automated tests.
- Add blocking fake Cursor/Codex children with explicit readiness markers so
  tests can kill/restart at exact boundaries without long sleeps.
- Persist fake call ledgers outside SQLite workflow truth and assert them only
  as side-effect-count evidence.
- Do not expose fake/fault controls through production project configuration.

### 4. Cover READ and observation boundaries

Parameterize the read path over:

- failure before the transport call;
- timeout/network/429/rate-limit/502/503/504;
- server-directed wait clamping;
- crash after the sanitized snapshot is persisted but before claim completion;
- lease expiry and generation replacement;
- PR closed/not found/auth/permission failures;
- repository/base/head/trigger drift;
- eligible thread omission/addition/duplicate and contradictory no-findings;
- bot still waiting versus transport retry; and
- restart with persistent DB/artifact roots.

Each case must assert:

- the exact durable state/version, journal event and dispatch status;
- retry attempt and `next_attempt_at` without early timer firing;
- one accepted observation artifact with correct hash/mode/binding;
- read-only same-dispatch recovery where allowed;
- zero LOCAL/model/write calls after deterministic block/drift; and
- privacy-safe status/history/error output.

### 5. Cover publication/adjudication LOCAL boundaries

For both `GeneratePublicationTextEffect` and
`AdjudicateThreadsEffect`, inject:

- failure before Codex spawn;
- timeout while stdin/stdout is active, including TERM refusal then KILL/reap;
- nonzero exit, malformed JSON, schema mismatch and oversized output;
- execution-context/snapshot/head/cycle/thread-set drift;
- valid result persisted before `complete_claim`, followed by crash/reopen;
- lease loss during the call; and
- explicit resume of an expired LOCAL claim.

Prove:

- exact session/model/reasoning/skill/sandbox argv and no `--last`;
- minimal environment and bounded capture;
- expired LOCAL work pauses with one precise same-effect action;
- a verified effect-bound cache prevents a second model call;
- an absent/unprovable result is never invented;
- cached replay validates all bindings before success; and
- model text never enters operational SQLite summaries.

### 6. Exercise the real subordinate local carrier across restarts

Use `FilesystemLocalCarrierRuntime`, the real
`run_local_review_fix()` boundary, persistent XDG state, a disposable Git
repository and fake `agent`/`codex` executables.

Kill/reopen at:

- before and after carrier seed;
- before Cursor starts;
- mid-Cursor;
- after Cursor before staging;
- after staging before local Codex review;
- mid-Codex;
- after accepted carrier finalization before the v2 result exists;
- after the v2 result exists before `complete_claim`; and
- after a terminal carrier is reopened.

Assert:

- deterministic carrier ID and no second carrier;
- null existing-PR chat becomes one exact new chat at most once;
- later cycles reuse that exact chat and the exact Codex session;
- iterations are monotonic and completed turns are not rerun;
- paused carrier state maps to a resumable v2 state;
- terminal reconstruction performs no agent call and requires complete,
  bounded, hash-verified patch/review evidence;
- path traversal, symlink, permission, size and content drift fail closed; and
- abort preserves staged/user files and fences late results.

### 7. Parameterize all mutations and reconciliation crash windows

Cover every current mutating effect:

- `commit_patch`
- `push_commit`
- `create_or_update_pr`
- `request_bot_review`
- `post_thread_reply`
- `update_pr_text`
- `resolve_thread`

For each, test:

- crash/failure before the external mutation: zero writes;
- ambiguous timeout/connection loss while the operation may apply;
- external apply followed by crash before durable completion;
- durable completion followed by worker-process failure; and
- stale authority, lease loss and abort during the call.

After DB/runtime reopen, drive the real reconciler through:

- APPLIED -> success without repeating the write;
- PROVEN_NOT_APPLIED -> retry the same logical effect/idempotency identity;
- UNRESOLVED -> one durable safe pause or the existing explicitly defined
  reducer action, never a blind write; and
- transient reconcile read failure -> bounded retry without mutation
  authority.

Assert exact effect IDs, idempotency keys, `ADL-Idempotency` trailers, commit
parents, remote SHAs, marker/ref/body hashes and one external application in
the fake call ledger. Initial and fix commits in one external cycle must remain
content-bound and distinct.

### 8. Exercise timers, leases, workers and supervisor restart

- Test crashes before/after timer firing transactions, duplicate concurrent
  firers, future timers, superseded/cancelled timers and per-run isolation.
- Prove `resume` never fires a future timer early.
- Exhaust six total attempts, pause with the documented action, then explicitly
  resume a new retry batch while retaining logical effect identity.
- Run two real worker instances against one persistent DB for every authority
  class.
- Inject heartbeat exception/expiry, acquire a new generation and submit the
  old result. The old result must be journaled/fenced without state mutation.
- Invoke the actual supervisor worker entry point with isolated XDG state and
  fake process dependencies.
- Test start transition followed by spawn failure, metadata persistence failure,
  absent/stale supervisor repair, live idempotent reuse, corrupt/reused PID
  metadata and clean restart from durable state.
- Do not use arbitrary sleeps; coordinate with readiness files/events and
  bounded deadlines.

### 9. Exercise control and abort ordering

Cover:

- concurrent/idempotent `start`;
- `resume` from active, paused, retry-waiting and `waiting_for_user`;
- required `--confirm-user-continuation` evidence;
- abort during long read, Codex publication/adjudication, Cursor, local Codex,
  mutation and reconciliation calls;
- launcher/child identity mismatch and stale metadata;
- failure to signal one owned child; and
- completion arriving after abort persistence.

Assert:

- abort is durable before any signal;
- signal order is carrier Cursor -> owned Codex children -> supervisor;
- only exact owned processes are signaled;
- external/manual kills without a durable abort are not called user aborts;
- late completion is fenced;
- retry timers and live effects are cancelled/superseded correctly; and
- repository contents, staged patch and artifacts remain intact.

### 10. Corruption, drift, permissions and privacy sweep

Create a parameterized corruption suite over execution context, source
plan/prompt, observation, adjudication, publication text, local result, commit
message, reply, patch and review evidence:

- missing/wrong hash;
- truncated/oversized/malformed JSON;
- wrong schema/binding/cycle/head/thread set;
- absolute/traversal path;
- final symlink or symlinked parent;
- non-regular or world-readable file; and
- post-finalization byte mutation.

Add sentinel secrets/full identifiers to controlled fake stdin/stdout/stderr,
prompts, patches, thread bodies, replies and environments. Scan:

- SQLite snapshots/events/outbox/timers;
- `status` and `history` text/JSON;
- safe errors and supervisor output;
- launcher/child metadata; and
- ordinary logs.

Only dedicated sensitive artifacts may contain required raw content, with
owner-only permissions and size bounds. Full session/chat IDs, raw model
output, tokens, secrets, bodies and patches must not leak.

### 11. Documentation and operator-safe actions

Update current product docs to describe only verified behavior:

- `pr_review_v2.no_findings.accept_bot_thumbs_up`;
- the exact fail-closed reaction binding;
- `start`, retry, pause, resume, waiting-user and abort behavior;
- supervisor/restart troubleshooting;
- protected evidence and privacy boundaries;
- Gate A simulated/production-boundary evidence versus Gate B live evidence;
  and
- the fact that v2 remains temporary until Phase 16.9.

Every tested failure class must map to one documented operator action. Do not
tell users to edit SQLite/artifacts, guess sessions/chats, rerun writes, reset
Git or delete launcher metadata.

### 12. Gate A validation and staged A/B review

- Run all focused Phase 16.8 tests directly.
- Run the Phase 16.1 A/B barrier, Phase 16.2 boundary tests and all existing
  v2 Phase 16.3-16.7 suites.
- Run the complete validation block below.
- Review the final effect matrix against implementation and docs.
- Leave intended `ai_dev_loop` changes staged for the external A/B reviewer.
- Do not commit, push or perform Gate B from Cursor.
- If the staged reviewer finds issues, correct them through the same exact
  Cursor chat/Codex reviewer loop until the review is clean.

### 13. Gate B controlled live acceptance

This step is controller/operator work after Gate A staged acceptance. Cursor
must not execute it during implementation.

#### 13.1 Resolve and record the live boundary

- Preserve `/home/rojobad/Projects/parish360-poc` exactly as found.
- Create a dedicated clean sibling checkout, for example
  `/home/rojobad/Projects/parish360-poc-phase16-8-acceptance`.
- Resolve/fetch `origin/main`, record its exact OID, and create one new
  `rba/phase-16-8-acceptance-*` branch from that pinned OID.
- Revalidate immediately before each write:
  `OWNER/REPO`, SSH remote, base, branch, HEAD, clean index/worktree, authenticated
  `gh`, SSH agent, open same-repository PR and bot identity.
- Record the user's approved scope: create branch/PR, normal commits and
  non-force pushes, trigger comments, replies, thread resolution and PR-text
  update. The user is the rollback owner; the controller may issue durable
  `abort`.
- Keep the full exact Codex session ID only in protected run state/CLI input.
  Never write it to the repository plan, prompt, docs or findings.
- Safely capture/confirm the exact session model and reasoning using the
  existing trusted session-runtime mechanism or explicit user-approved
  overrides. Never infer CLI defaults.

#### 13.2 Prepare the isolated test PR

- Add only minimal target-local acceptance assets:
  - `ai_dev_loop.yaml` with `pr_review_v2.enabled: true`,
    reviewer `chatgpt-codex-connector`, base `main`,
    no-findings enabled with `accept_bot_thumbs_up: true`, bounded cycles and
    local iterations;
  - a concise local implementation plan and prompt;
  - simple compatible local-review/external-adjudication skills if absent; and
  - a clearly synthetic, narrowly isolated credential-like defect modeled on
    the actionable pattern already observed in PR #1.
- Never copy or expose existing Parish360 credential-like values.
- Give the fixture a deterministic local validation that fails before the fix
  and passes after the synthetic value is removed/externalized.
- Commit and push the initial fixture branch normally, then create a new,
  clearly labeled Phase 16.8 test PR targeting `main`.
- Do not request review manually; let the v2 `start` path create its exact
  idempotent trigger.

#### 13.3 Run the real v2 cycle

- Run live `prepare` for the new existing PR with the exact protected Codex
  session, target plan/prompt and explicit/frozen runtime.
- Prove `prepare` creates/reuses only `PreparedState`: no trigger, model call,
  chat, commit, push or worker.
- Inspect redacted `status`/`history`, then run `start` once.
- Monitor through:
  1. trigger publication;
  2. actionable bot thread on the synthetic defect;
  3. exact-session external adjudication;
  4. one Cursor chat creation and local correction;
  5. exact-session local Codex acceptance;
  6. content-bound commit and non-force push;
  7. bounded reply/thread resolution/PR update;
  8. next idempotent review trigger;
  9. bot `+1` reaction on that exact trigger with no eligible threads; and
  10. durable `completed`.
- Use only `status`, bounded `history`, documented `resume` and durable `abort`.
  Never manually replay a write or edit v2 SQLite/artifacts.
- If any branch/head/thread/patch/session/chat/evidence binding drifts, abort or
  leave the run paused with its documented safe action. Do not weaken a check
  to finish the demo.

#### 13.4 Record acceptance evidence

Record safe evidence without bodies, prompts, patches, tokens or full IDs:

- run ID and target PR/branch;
- commit/head prefixes and protected artifact hashes;
- state/version/journal sequence and effect-kind counts;
- retry/reconcile/claim behavior observed;
- exact-session/chat continuity as redacted prefixes or equality assertions;
- trigger/reply/resolve/commit/push counts;
- no-findings reaction rule and exact-trigger binding;
- final status/history and launcher/child liveness;
- commands/manual actions and timestamps; and
- any residual risk.

Leave the test PR and branch open for the user. Do not merge, close or delete
them.

### 14. Final handoff

Create
`archive/implementation-history/findings/phase-16-8-to-16-9-handoff.md`
only after Gate B.

It must distinguish:

- automated fake/process/local-Git evidence;
- staged A/B review evidence;
- actual live GitHub/Cursor/Codex evidence;
- any correction rounds;
- exact commands and summarized results;
- public/config/schema changes;
- file inventory;
- live target/PR and safe retained artifacts;
- every crash-window disposition;
- unresolved residual risks; and
- Phase 16.9 entry conditions.

Do not claim Phase 16.8 complete if the live cycle did not reach verified
`completed`.

## Testing Criteria

### Unit and contract criteria

- Config/model/schema tests cover omitted/default/disabled reaction behavior,
  enabled validation and old execution-context compatibility.
- Gateway contract tests cover exact trigger binding, allowed actor, `+1`,
  timestamp ordering, wrong actor/content/trigger, duplicate evidence,
  eligible-thread contradiction and `eyes` non-completion.
- Protected artifact tests cover typed reaction evidence, hash, mode, size,
  path, symlink and compatibility behavior.
- Every effect appears exactly once in the executable resilience matrix with an
  authority/recovery policy.
- Error classification stays typed; tests do not rely on arbitrary substring
  parsing in the engine.

### Integration criteria

- Tests reopen the real SQLite DB/artifact root across failures, not only
  recreate in-memory objects.
- Real v2 store, engine, router, executors, gateways, supervisor and control
  services are assembled wherever their boundary is under test.
- External network/model behavior uses stateful fake executables/processes;
  Git behavior uses temporary repositories/bare remotes.
- All mutation kinds prove at-most-once external application across
  apply-then-crash and restart/reconcile.
- READ and RECONCILING claims requeue safely; MUTATING claims reconcile; LOCAL
  claims pause/reopen according to their exact contract.
- Two workers and a lease-generation replacement cannot both apply one result.
- Timer firing is durable, isolated per run and never early.
- Abort ordering, ownership validation and stale late-completion fencing are
  proven with real child processes.
- The local carrier reuses exact carrier/chat/session identity across every
  restart checkpoint.
- Privacy scans cover SQLite, CLI/status/history, logs and ownership metadata.

### End-to-end criteria

- A production-assembled simulated multi-round workflow covers both source-run
  and existing-PR origins with at least one local correction and clean
  completion.
- It asserts journal/outbox/timer counts, idempotency keys, exact agent
  identities, protected hashes, write counts and safe next actions—not only the
  final state.
- Gate B completes a real existing-PR cycle on a new
  `rojobad/parish360-poc` PR, including actionable finding, local fix,
  commit/push, reply/resolve, second review and exact-trigger thumbs-up
  completion.

### Regression criteria

- Phase 16.1 A/B barrier remains green.
- Phase 16.2 local review contract/boundary remains green.
- Legacy `pr-review`, recovery, launch/resume/abort and global integrations
  suites remain green.
- No v2 module imports legacy PR lifecycle/recovery code.

### Automated-test prohibitions

Automated tests must not use:

- real network or GitHub credentials;
- real GitHub repositories, PRs, comments, replies or reactions;
- real Cursor/Codex/model calls;
- real user HOME/XDG/Codex state;
- the dirty Parish360 checkout; or
- unbounded sleeps/timeouts.

## Validation

Run from the `ai_dev_loop` repository root with native WSL temporary paths:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/test_phase16_2_local_review_contracts.py \
  tests/integration/test_phase16_2_local_review_boundary.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_durable_engine.py \
  tests/integration/test_phase16_4_acceptance_matrix.py \
  tests/integration/test_phase16_4_crash_restart.py \
  tests/integration/test_phase16_4_leases_and_fencing.py \
  tests/integration/test_phase16_5_engine_workflows.py \
  tests/integration/test_phase16_5_github_read_gateway.py \
  tests/integration/test_phase16_5_lease_and_restart.py \
  tests/integration/test_phase16_5_privacy.py \
  tests/integration/test_phase16_6_crash_restart_and_fencing.py \
  tests/integration/test_phase16_6_git_publication.py \
  tests/integration/test_phase16_6_git_writes.py \
  tests/integration/test_phase16_6_github_writes.py \
  tests/integration/test_phase16_6_privacy_and_isolation.py \
  tests/integration/test_phase16_6_reconciliation.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_8_fault_matrix.py \
  tests/integration/test_phase16_8_local_restart.py \
  tests/integration/test_phase16_8_supervisor_restart.py \
  tests/integration/test_phase16_8_privacy.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
git status --short
```

Adjust Phase 16.8 filenames if implementation uses clearer split files and
record the exact commands/results in the handoff.

Gate B validation is manual/controller-driven and must additionally record:

```text
ai_dev_loop config validate --repo <isolated-parish-checkout>
ai_dev_loop pr-review-v2 prepare ...
ai_dev_loop pr-review-v2 status <run-id> --output json
ai_dev_loop pr-review-v2 history <run-id> --limit <bounded> --output json
ai_dev_loop pr-review-v2 start <run-id>
ai_dev_loop pr-review-v2 resume <run-id> [only when status prescribes it]
ai_dev_loop pr-review-v2 abort <run-id> [only on stop/drift]
```

Do not place the exact session ID in shell history captured by the repository
or in the findings file.

## Risks Or Recovery Notes

- The original Parish360 checkout is dirty and stale. Any use of it for live
  execution risks mixing unrelated user work. The dedicated clean checkout is
  mandatory.
- Parish360 contains existing credential-like content outside the planned PR
  diff. Never copy, summarize or use those values. The acceptance defect must
  be synthetic and isolated.
- A reaction contains no reviewed-commit text. Its safe binding therefore
  depends on the exact content-bound trigger comment, current effect/cycle/head,
  reviewer identity, timestamp and absence of eligible threads. If any link is
  missing, do not complete.
- The bot may change timing or presentation. Poll normally; do not consume
  retry budget for a valid absence of feedback and do not broaden evidence
  matching to make the live run pass.
- Model calls can finish near a crash boundary. Expired LOCAL claims must pause
  or replay only from verified protected results; do not blindly create another
  exact-session turn.
- Process-level tests can become flaky if driven by sleeps. Use explicit
  readiness/acknowledgment files or IPC and bounded waits.
- Additive protected-artifact changes can accidentally reinterpret Phase 16.7
  evidence. Compatibility tests must prove missing new fields preserve old
  behavior.
- If a new SQLite invariant truly requires a migration, stop and document the
  invariant, compatibility strategy and migration before implementing it. Do
  not add a migration for test convenience.
- Live acceptance may pause on genuine drift or uncertain evidence. Preserve
  the run, PR, branch and artifacts; use the documented safe action or abort.
  Never reset/clean/force-push to recover.
- Any shared process/abort/local-loop change expands regression risk and
  requires the full A/B barrier plus manual staged review.

## OpenQuestions

None.
