# Phase 16.5 - PR Review v2 GitHub Read Gateway

Status: proposed for approval

Depends on:

- Phase 16.4 being accepted, committed, and merged as the clean baseline;
- the Phase 16.3 pure domain/reducer remaining the sole workflow decision authority;
- the Phase 16.4 SQLite journal, outbox, timers, claims, leases, and fencing protocol
  remaining authoritative; and
- the Phase 16.1 A/B regression barrier remaining intact.

Entry conditions are satisfied on 2026-07-21:

- `main` and `origin/main` point to merge commit `31ca91f`, which contains Phase 16.4;
- the worktree and index are clean;
- `archive/implementation-history/findings/phase-16-4-to-16-5-handoff.md`
  records zero actionable findings from the detached five-turn implementation review;
- that handoff records 292 focused tests and 1120 full-suite tests passing, plus clean
  Ruff, Mypy, MkDocs, build, and `git diff --check` validation; and
- the user approved the Phase 16.5 design decisions recorded below on 2026-07-21.

The handoff text that says Phase 16.4 remains staged is a historical snapshot. Current
Git state and merged code are authoritative. Cursor must still re-check the clean
baseline before editing and stop if it has drifted.

## Goals

Integrate a production-capable, read-only GitHub observation boundary for PR review v2
without adding any GitHub/Git write, public CLI, daemon, or local model adapter.

This phase must:

- add one isolated `gh api`/GraphQL read transport and one GitHub read gateway for
  repository, PR, head/base branch, full head SHA, trigger comment, review threads,
  issue comments, reactions, and pagination metadata;
- add a narrow `EffectExecutor` implementation that accepts only a claimed
  `ObserveBotReviewEffect` and returns only a typed `EffectSucceeded`,
  `EffectRetryableFailure`, or `EffectBlocked` event;
- verify repository identity, PR number, open state, non-fork topology, head/base
  branch, full head SHA, cycle, poll sequence, and exact trigger marker before
  interpreting any bot observation;
- distinguish successful no-feedback polling from transport failure so normal polling
  never consumes the transient failure budget;
- implement the approved six-attempt GitHub failure batch: one initial attempt plus up
  to five retries using 10/30/90/180/300-second local delays with injectable
  deterministic +/-20% jitter;
- honor trustworthy `Retry-After` and `X-RateLimit-Reset` metadata ahead of local
  backoff, while clamping server-directed eligibility to at most one hour after the
  current observation time;
- map authentication, permission, not-found, closed PR, fork/branch/repository/head
  drift, malformed or contradictory evidence, timeout/network, rate limit, and
  temporary server/CLI failures to explicit typed outcomes;
- persist absolute UTC eligibility times only through the existing reducer and durable
  timer protocol; the SQLite layer must remain free of GitHub policy;
- renew the current worker lease at a bounded interval while an external read is in
  progress and still submit every result through `complete_claim()` for final fencing;
- atomically persist protected, run-scoped, sanitized observation artifacts with
  relative paths and SHA-256 hashes before emitting no-findings or eligible-thread
  outcomes;
- keep raw GitHub bodies and response payloads ephemeral and prevent credentials,
  secrets, raw prompts, raw review bodies, full session identifiers, and owner tokens
  from reaching SQLite, logs, status DTOs, exceptions, or persisted artifacts; and
- prove the gateway, retry, polling, artifact, restart, lease, privacy, and fencing
  behavior using sanitized fixtures, injected transports, and fake `gh` executables
  only.

## Non-Goals

- Do not post the bot review trigger, create/update PRs, commit, push, reply, resolve
  threads, update PR text, mutate reactions, mutate refs, or perform any other
  GitHub/Git write. Phase 16.6 owns write gateways.
- Do not execute `ReconcileWriteEffect`. Phase 16.5 may expose narrowly reusable
  read-only primitives, but the reconciliation executor and strategy routing remain
  Phase 16.6 work.
- Do not implement `find_commit_at_head`, `find_remote_ref`, publication recovery, or
  write-outcome reconciliation under the guise of polling.
- Do not invoke Cursor, Codex, the extracted local review loop, or any real model.
  Phase 16.7 owns those application adapters and complete v2 orchestration.
- Do not add a public `pr-review-v2` CLI command, daemon, background service, webhook,
  controller route, status command, or automatic worker lifecycle.
- Do not wire v2 into `ProjectConfig`, `ai_dev_loop.yaml`, legacy PR review, A/B
  prepare/start/resume/recover, or public documentation that claims v2 is operational.
- Do not use real GitHub credentials, a real authenticated `gh` session, network
  access, or a real PR in automated or implementation-time validation.
- Do not migrate legacy Phase 15 run state or reuse its state machine, command layer,
  GitHub runner, error classifier, or recovery paths.
- Do not change the SQLite schema or `PRAGMA user_version = 1`; Phase 16.5 stores
  artifacts in protected files and uses the existing validated event/effect payloads.
- Do not claim full 24-hour polling lifecycle, production daemon continuity, or
  end-to-end PR review v2 completion. Those require later orchestration phases.

## Scope

Add an isolated read slice under `src/ai_dev_loop/pr_review_v2/`. The exact filenames
may be adjusted to avoid a demonstrated import cycle, but preserve this responsibility
split:

```text
src/ai_dev_loop/pr_review_v2/
  application/
    contracts.py                 # narrow read ports/policy DTOs if shared
    github_read.py               # typed read policy and observation contracts
  infrastructure/
    gh_transport.py              # direct argv, timeout, headers, JSON/GraphQL parsing
    github_read_gateway.py       # read-only GitHub observation and validation
    review_artifacts.py          # atomic protected snapshot/evidence store
    paths.py                     # only narrow v2 artifact path additions if needed
  workers/
    effect_worker.py             # bounded in-flight lease renewal/fencing hardening
    github_read_executor.py      # ObserveBotReviewEffect-only executor
```

Cursor may make narrow, explicitly required changes to:

- `domain/common.py` and `domain/__init__.py` to add typed `NOT_FOUND` and
  `BRANCH_DRIFT` pause reasons and preserve serialization exports;
- `application/contracts.py` for read transport/artifact/lease-renewal ports or DTOs;
- `application/engine.py` only if a read-only lease duration/introspection contract is
  needed to validate the renewal interval; no transition or persistence logic may move
  out of the existing engine;
- `infrastructure/__init__.py`, `workers/__init__.py`, and the package root
  `__init__.py` for a deliberately small v2 read API; and
- existing Phase 16.3/16.4 tests where new enum variants or executor contracts require
  additive serialization/architecture coverage.

Expected new focused tests and fixtures:

```text
tests/unit/pr_review_v2/
  test_github_read_contracts.py
  test_github_read_parsing.py
  test_github_backoff.py
  test_review_artifacts.py
  test_github_read_executor.py
  test_effect_worker_heartbeat.py

tests/integration/
  test_phase16_5_github_read_gateway.py
  test_phase16_5_engine_workflows.py
  test_phase16_5_lease_and_restart.py
  test_phase16_5_privacy.py

tests/fixtures/pr_review_v2/github/
  *.json
  *.txt
```

Names may be consolidated, but unit contract evidence, fake-process integration
evidence, and real-temporary-SQLite workflow evidence must remain distinguishable.

Add the implementation handoff:

```text
archive/implementation-history/findings/phase-16-5-to-16-6-handoff.md
```

The handoff must report the final public API, exact commands/results, test inventory,
error/backoff mapping, artifact format and permissions, lease behavior, isolation
evidence, honest boundary, and residual risks. It must not claim real GitHub validation.

## Out of Scope

Cursor must not modify:

- `src/ai_dev_loop/state.py`, legacy schemas, legacy run directories, manifests, or
  legacy `RunState`/`GithubPrReviewState` persistence;
- `src/ai_dev_loop/config.py`, `src/ai_dev_loop/schemas/project-config-v1.json`,
  `ai_dev_loop.yaml`, CLI routing/help, or configuration docs;
- `src/ai_dev_loop/commands/pr_review*.py`, `pr_review_worker.py`,
  `external_adjudication.py`, `github_pr_review_result.py`, or legacy PR status;
- `src/ai_dev_loop/runners/github.py`, `publish.py`, `codex_github.py`, Git runners,
  Cursor/Codex runners, or legacy error classification;
- `src/ai_dev_loop/process.py`, `redaction.py`, or `paths.py`; reuse their safe generic
  helpers where compatible, but do not change shared behavior in this phase;
- `src/ai_dev_loop/local_review_loop.py`, `workflow_engine.py`, `iterations.py`,
  `resume_planner.py`, or any A/B start/resume/recover/abort implementation;
- the Phase 16.4 migration SQL, SQLite schema version, transaction protocol, journal,
  snapshot CAS, timer reconciliation, outbox identity, or completion fencing merely to
  simplify the GitHub adapter;
- `.cursor/rules/`, `.cursor/skills/`, `.agents/skills/`, integrations, hooks,
  SessionStart assets, controller/handoff skills, or the Codex session bridge;
- public MkDocs/README claims that the v2 workflow or real GitHub integration is
  available; or
- Git history, commits, pushes, tags, branches, remotes, staging policy, user-global
  integrations, credentials, or real external services.

If an out-of-scope production file appears necessary, Cursor must stop before editing
it and request a plan amendment. Reference legacy GitHub behavior for evidence only;
do not import or call legacy lifecycle or gateway modules from v2.

## Required Context

Read before implementation:

1. `pr-review-v2-restructure-context.md` in full, especially effects/persistence,
   retries, GitHub reads/writes, observability, Phase 16.5, and test strategy.
2. `archive/implementation-history/findings/phase-16-4-to-16-5-handoff.md` in full.
3. `archive/implementation-history/plans/phase-16-3-pr-review-v2-domain-and-reducer.md`.
4. `archive/implementation-history/plans/phase-16-4-pr-review-v2-durable-engine.md`
   and its clarifications companion.
5. All code under `src/ai_dev_loop/pr_review_v2/`, especially domain effects/events,
   reducer waiting/retry paths, engine claim/completion/lease methods, path helpers,
   SQLite projections, and `workers/effect_worker.py`.
6. All tests under `tests/unit/pr_review_v2/` and all
   `tests/integration/test_phase16_4_*.py` files.
7. `tests/integration/test_phase16_1_ab_regression_barrier.py`.
8. `src/ai_dev_loop/runners/github.py` and its tests only as legacy evidence for
   marker matching, full-SHA checks, sanitized hashes, pagination, and fail-closed
   thread/no-findings selection. Do not import it into v2.
9. `src/ai_dev_loop/process.py`, `redaction.py`, and `paths.py` for generic subprocess,
   redaction, XDG, atomic-write, and permission conventions.
10. `src/ai_dev_loop/config.py` only as historical evidence for the approved bot login,
    60-second poll interval, and explicit no-findings opt-in. Do not couple the v2
    executor to `ProjectConfig` in this phase.
11. Current `/docs`, `archive/implementation-history/master-plan.md`, and current
    code/tests where they define stronger behavior than archived history.
12. Official GitHub documentation for the transport contract:
    - <https://cli.github.com/manual/gh_api>;
    - <https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api>;
    - <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>.

The official `gh api` contract confirms direct GraphQL access, `--include` response
headers, and pagination. GitHub's official rate-limit documentation confirms the
meaning and precedence of `Retry-After`, `X-RateLimit-Remaining`, and
`X-RateLimit-Reset`. Do not depend on undocumented human-readable CLI prose for
lifecycle or permanent-error decisions.

## Cursor Rules And Skills

All repository Cursor rules are `alwaysApply`; Cursor must read and follow all of them:

- `.cursor/rules/ai-dev-loop-governance.mdc`;
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`; and
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`.

There is no repository-root `AGENTS.md` and no repo-local `.cursor/skills/` directory.

The external `ai_dev_loop` orchestrator owns post-implementation review through:

- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`.

Cursor must not invoke or modify that skill, run real Cursor/Codex activity, or perform
the final acceptance review. Cursor implements and validates this plan; the
orchestrator later presents the staged diff to the exact resumed Codex reviewer.

## Architecture Guardrails

### 1. Reducer, engine, and completion authority

- `PrReviewState` and `reduce_pr_review()` remain the only lifecycle decision source.
- The GitHub gateway observes and validates remote data; it must not own a second state
  machine, write SQLite rows, advance polling, consume retries, or schedule timers.
- The executor may construct only a typed `EffectSucceeded`,
  `EffectRetryableFailure`, or `EffectBlocked` for the current claim token.
- Every result, including a late result after heartbeat loss, abort, recovery, or a new
  lease generation, must be submitted through `PrReviewEngine.complete_claim()`.
- Never call `apply_event()` for an effect result and never insert/update SQLite rows
  directly from the transport, gateway, artifact store, executor, or worker.
- Preserve every existing completion fence: dispatch ID, claim ID, owner, lease
  generation, claimed run version, effect ID, attempt, cycle, and bound full head SHA.

### 2. Layer and import boundaries

- `domain/` remains pure and free of GitHub, subprocess, filesystem, clocks, random,
  SQLite, and legacy imports. Only additive typed vocabulary changes are allowed.
- `application/` owns ports, frozen DTOs, policies, and orchestration contracts; it
  must not import legacy PR commands, config, state, runners, or A/B lifecycle code.
- `infrastructure/` owns `gh`, JSON/GraphQL/header parsing, XDG artifact I/O, system
  randomness adapters, and safe transport errors.
- `workers/` adapts a claimed effect to the read gateway and owns bounded lease
  renewal. It has no raw database access and no GitHub write operation.
- No v2 module may import `ai_dev_loop.runners.github`, legacy `RunState`,
  `GithubPrReviewState`, legacy recovery/status, command modules, or local review code.

### 3. Strict read-only transport

- Invoke the configured `gh` executable with direct argv, `shell=False`, explicit
  repository working directory, explicit timeout, and separated stdout/stderr.
- Allow only predefined `gh api` read operations. GraphQL documents must begin with a
  named `query`, contain no mutation operation, and request only the fields required by
  this plan. REST fallbacks, if demonstrably required, must force `--method GET`.
- Never expose a generic arbitrary-query or arbitrary-endpoint public method that later
  callers could use to bypass the read-only boundary.
- Use `--include` or an equivalently structured fake transport envelope to parse the
  HTTP status and allowlisted headers separately from JSON. Do not log headers wholesale.
- Treat response bodies as untrusted. Parse JSON into frozen typed DTOs with strict
  shape, full pagination, duplicate-ID, cursor-progress, nullability, and contradiction
  checks. Reject partial evidence rather than silently dropping malformed nodes.
- Pagination is serial and bounded by an overall observation deadline and explicit page
  and item limits. Missing/unchanged cursors, excessive pages/items, or partial GraphQL
  data that cannot prove completeness must block or retry according to its typed cause.
- Do not infer PR lifecycle, identity, findings, or authorization from human-readable
  stderr/stdout prose. Process timeout is taken from the typed process result; HTTP,
  GraphQL, headers, and stable CLI exit semantics drive other classifications. An
  otherwise unclassifiable nonzero `gh` exit becomes safe `TEMPORARY_CLI_FAILURE`.

### 4. Observation binding and fail-closed evidence

- Before interpreting feedback, require exactly one repository and PR matching the
  effect binding: name-with-owner, PR number, open state, head branch, base branch, and
  full 40-character head SHA.
- Preserve the legacy safety boundary that cross-repository/fork PRs are unsupported;
  return a typed block rather than guessing which repository identity is authoritative.
- Locate exactly one trigger comment containing the exact HTML marker derived from
  `effect.trigger_marker`. The marker is dynamic data, not executable text. Reject a
  missing, duplicated, malformed, or contradictory marker.
- Require the trigger comment timestamp to be valid. Only bot comments/threads from an
  allowlisted reviewer login, strictly after that trigger, and bound to the expected
  full head SHA may count.
- Eligible threads are unresolved, unique, have a valid root comment and provenance,
  and match the current head. Missing author, timestamp, commit SHA, root ID, or thread
  ID is incomplete evidence, not a reason to skip the node.
- Empty or incomplete thread/comment data is never proof of no findings.
- `VerifiedNoFindingsOutcome` requires explicit policy opt-in, an allowlisted author,
  a configured accepted prefix/rule, a valid post-trigger timestamp, and a structural
  reviewed-commit value matching the configured prefix of the full bound SHA.
- If verified no-findings evidence and eligible threads coexist, or multiple conflicting
  completion markers exist, emit `EffectBlocked` for operator inspection.
- If neither explicit no-findings evidence nor eligible threads exists after a complete,
  valid read, emit `BotStillWaitingOutcome` with exactly
  `effect.poll_sequence + 1` and `next_not_before = observation_time + 60 seconds`.

### 5. Typed policy and public behavior

- Introduce a frozen constructor-injected `GitHubReadPolicy`; do not read global or
  target-repository config inside the executor/gateway.
- The safe defaults/reference values for this phase are:
  - reviewer login: `chatgpt-codex-connector`;
  - poll interval: 60 seconds;
  - total GitHub attempts per batch: the existing domain-fixed value of 6;
  - local retry delays: 10, 30, 90, 180, and 300 seconds;
  - jitter range: inclusive `[-0.20, +0.20]`, from an injected source;
  - maximum server-directed wait: 3600 seconds;
  - no-findings detection: disabled unless accepted prefixes are explicitly supplied;
  - reviewed SHA prefix length when enabled: 12, validated within `[7, 40]`.
- Make command, repository working directory, per-call/overall timeout, page/item caps,
  reviewer logins, polling interval, accepted prefixes, prefix length, jitter source,
  and artifact root injectable for deterministic tests and future outer wiring.
- Do not add these values to `ProjectConfig` or a public CLI in Phase 16.5.

### 6. Error and safe-action mapping

- Add `PauseReasonKind.NOT_FOUND` and `PauseReasonKind.BRANCH_DRIFT`, plus
  `TransientErrorKind.HTTP_500` and `TransientErrorKind.OTHER_HTTP_5XX`; retain
  existing `AUTHENTICATION`, `PERMISSIONS`, `CLOSED_PR`, `REPOSITORY_DRIFT`,
  `HEAD_DRIFT`, `HTTP_VALIDATION_REJECTION`, and 502/503/504 variants.
- Map typed transient causes to the existing `TransientErrorKind` variants:
  timeout, DNS failure, connection refused/reset, network unavailable, HTTP 429,
  primary/secondary rate limit, HTTP 500/502/503/504, any other structured HTTP 5xx,
  or temporary CLI failure. `OTHER_HTTP_5XX` must retain the numeric status only in a
  safe private transport DTO; do not copy raw response text into the domain event.
- Authentication, permission, not-found, closed PR, unsupported fork, repository/head/
  branch drift, HTTP validation rejection, missing executable, malformed complete
  response, and contradictory evidence produce `EffectBlocked`, never blind retry.
- Use safe actions consistently:
  - authentication -> `FIX_AUTH_THEN_RESUME`;
  - permissions -> `FIX_PERMISSIONS_THEN_RESUME`;
  - repository/head/branch drift -> `OPEN_NEW_CYCLE_OR_STOP`;
  - closed PR -> `NO_FURTHER_WORK`;
  - missing configured `gh` executable -> `RESUME_SAME_EFFECT`, conditioned on
    installing/repairing that executable before explicit resume;
  - not-found -> `RESUME_SAME_EFFECT`, conditioned on verifying repository/PR
    visibility and the recorded binding before explicit resume;
  - unsupported fork -> `OPEN_NEW_CYCLE_OR_STOP`, conditioned on using a same-repository
    PR binding or stopping;
  - HTTP validation rejection -> `INSPECT_ARTIFACTS`, conditioned on inspecting the
    safe observation diagnostics and remote PR before resume;
  - malformed, incomplete, or contradictory remote evidence -> `INSPECT_ARTIFACTS`,
    conditioned on restoring one complete consistent observation before resume; and
  - artifact persistence/verification failure -> `INSPECT_ARTIFACTS`, conditioned on
    repairing the protected artifact root and verifying no corrupt artifact is reused
    before explicit resume.
- Safe summaries are short, redacted, and contain no raw CLI prose, URL query secrets,
  bodies, headers, reviewer content, owner IDs, or full response payloads.
- Domain/invariant corruption remains fatal through the existing engine boundary; do
  not relabel local corruption as a GitHub retry.

### 7. Backoff and durable timer ownership

- For failed attempts 1 through 5, choose the corresponding base delay
  10/30/90/180/300 seconds and apply injected jitter only to that local delay.
- A valid nonnegative `Retry-After` delta takes first precedence. Otherwise, when
  structured metadata proves exhaustion, a future `X-RateLimit-Reset` UTC epoch may
  direct eligibility. Do not jitter a trustworthy server-directed time.
- Ignore malformed, negative, or already-expired directed values and fall back to local
  backoff. Clamp any future directed wait beyond 3600 seconds to exactly one hour and
  retain only a safe classification, never the raw header set.
- Compute `next_attempt_at` from an injected, timezone-aware completion/observation
  clock, never from `claim.claimed_at` after a long call.
- Emit only the absolute UTC `next_attempt_at`. The existing reducer, journal, timer
  reconciliation, and `fire_due_timers()` remain the exclusive durable scheduler.
- Attempt 6 is the final allowed call. If it fails transiently, emit a schema-valid
  retryable failure for the current attempt; the existing reducer must pause with
  `RETRY_EXHAUSTED` and must not create another timer or attempt. Tests must explicitly
  prove five persisted retry waits followed by pause on the sixth failure.
- Explicit resume starts a new reducer-owned batch with attempt 1 and the same logical
  effect/idempotency identity. Do not implement a separate retry counter.

### 8. In-flight lease renewal and late results

- The worker, not the gateway, owns lease renewal because only the worker has claim
  owner/generation authority.
- Add a bounded renewal coordinator that heartbeats well before the default 30-second
  expiry (10 seconds by default), with constructor-injected timing/control for tests.
- Execute the external read outside SQLite transactions. Heartbeats use independent
  short engine operations/connections and must never hold a transaction across network
  I/O.
- Stop and join renewal deterministically after executor return/error; do not leak a
  thread or background task. Do not use unbounded sleeps in tests.
- A rejected heartbeat, abort, lease replacement, or heartbeat error marks the lease as
  lost. The external read remains bounded by its transport timeout. Its eventual typed
  result is still offered once to `complete_claim()`, which must fence it as stale or
  rejected without state/timer/outbox mutation.
- Never execute a second external call after lease loss within the same worker step.
- Preserve expired READ_ONLY claim recovery: restart/new owner requeues the same
  dispatch and logical effect identity; it does not synthesize a new raw database row.

### 9. Protected artifacts and privacy

- Store observation artifacts under a dedicated run-scoped v2 root beneath
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/`, never inside the target repository.
- Do not use raw `run_id` as a filesystem path component because its domain grammar is
  not a path-component contract. Derive a deterministic safe run directory key (for
  example SHA-256 of run ID) and resolve all `ArtifactRef` values relative to that run
  root.
- Managed directories use `0700`; files use `0600` where chmod is supported. Reject
  symlink/path traversal and paths that escape the configured root.
- Write canonical UTF-8 JSON to an exclusive temporary sibling, flush/fsync as
  supported, apply permissions, atomically replace the final file, and fsync the
  directory where supported. Return `ArtifactRef` only after publication and hash
  verification succeed.
- Define versioned frozen artifact DTOs. Every observation manifest binds repository,
  PR number, head/base branch, full head SHA, cycle, poll sequence, trigger marker,
  observed-at time, evidence kind, IDs, source hashes, and sanitization/schema version.
- The manifest plus its SHA-256 supplies repository/PR binding not currently carried as
  direct fields on `FrozenThreadSet`; do not widen that domain model merely for
  infrastructure convenience unless an executable invariant proves the manifest/hash
  binding insufficient and the plan is amended.
- Raw GitHub bodies may exist only in memory long enough for exact marker/prefix
  matching and sanitization. Persist only normalized, explicitly sanitized review text
  needed by Phase 16.7, plus original-content hashes and safe metadata.
- Sanitization must remove trigger HTML comments, redact known secret patterns and
  session-like identifiers, reject invalid/control-heavy text, enforce per-item and
  total bounds, and fail closed instead of truncating evidence that would later be
  adjudicated as complete.
- No-findings evidence artifacts contain rule ID, comment/reviewer stable identity,
  timestamps, reviewed SHA prefix, and body hash, not the raw completion body.
- SQLite stores only the existing `ArtifactRef` path/hash and typed safe summaries.
  Status/log/error surfaces must not render artifact contents.

### 10. Phase and legacy isolation

- Keep legacy A/B and PR review behavior unchanged. Phase 16.5 tests may read legacy
  implementations as reference but must exercise only v2 production code.
- Do not create compatibility fallbacks from v2 to legacy gateway/config/state.
- Do not add write-looking placeholders that return success. Unsupported effect kinds
  must be rejected by the narrow executor before any transport call.
- No automated test may access the network, inherit real GitHub credentials, or invoke
  a real `gh` executable. Fake transports/processes must fail if write/mutation command
  shapes are attempted.

## Implementation Plan

### 1. Freeze the typed read contracts

- Add frozen strict DTOs/protocols for transport request/result, normalized headers,
  PR identity, trigger evidence, threads, comments, reactions, observation snapshot,
  read policy, jitter, and artifact writing/resolution.
- Keep raw response DTOs private to infrastructure; expose only the narrow validated
  application observations required by the executor.
- Add the approved pause-reason and transient HTTP variants and update public
  exports/serialization tests without changing existing enum values.
- Add architecture tests that keep the domain pure and prevent new v2 modules from
  importing legacy PR/config/runner/state modules.

### 2. Implement the direct `gh` transport

- Build predefined read argv with explicit cwd and timeout using the existing generic
  process helper where its contract is sufficient.
- Parse `--include` status/headers and JSON bodies without logging raw output.
- Normalize only allowlisted case-insensitive headers needed for classification and
  backoff. Treat duplicate contradictory header values as invalid evidence.
- Map typed timeout/process/HTTP/GraphQL failures into a private gateway error union.
- Add deterministic unit tests for argv, no shell, cwd, timeouts, status/header splits,
  GraphQL errors with HTTP 200, invalid JSON, concatenated pages, partial data, and
  nonzero exits.

### 3. Implement complete read-only observation

- Fetch and validate repository/PR identity first, then exact trigger evidence, review
  threads, relevant comments, and reactions through bounded serial pagination.
- Enforce page progress, uniqueness, item limits, valid timestamps, full SHA values,
  and response completeness.
- Preserve raw bodies only in ephemeral local variables. Produce hashes and sanitized
  typed content before returning from the gateway.
- Implement pure matchers for exact trigger, eligible threads, explicit no-findings,
  and contradictions. Keep matchers independent of SQLite and process execution.

### 4. Implement retry/backoff policy

- Add a pure calculator for local jittered delays and trustworthy server-directed
  eligibility using the exact precedence and one-hour cap in this plan.
- Validate jitter input and UTC timestamps. Deterministic fakes supply boundary values
  `-0.20`, `0`, and `+0.20`.
- Map gateway error variants, including HTTP 500 and every otherwise-unrecognized 5xx,
  to exact domain transient/pause reasons, safe actions, and redacted summaries in one
  centralized table/function.
- Prove polling success never calls this failure-budget calculator.

### 5. Implement the protected artifact store

- Add typed versioned observation/no-findings/thread snapshot formats.
- Add safe run-root derivation, secure directory creation, atomic canonical JSON writes,
  SHA-256 verification, and relative `ArtifactRef` creation.
- Add read/verify helpers only as needed for tests and future consumers; they must
  reject hash mismatch, wrong binding, symlinks, traversal, wrong schema version, and
  invalid permissions/shape.
- Write the artifact before constructing a success outcome. Any expected filesystem,
  permission, atomic-publication, serialization, or post-write verification failure
  becomes `EffectBlocked` with `PauseReasonKind.REQUIRED_OPERATOR_ACTION` and
  `SafeActionKind.INSPECT_ARTIFACTS`, using the exact recovery condition defined in the
  error mapping above. It never becomes a transient GitHub retry or a success event.
  An unexpected Python programming defect is not caught as an artifact condition; it
  crosses the existing safe worker/application failure boundary without raw content.

### 6. Implement `ObserveBotReviewEffect` executor

- Reject every effect kind except `ObserveBotReviewEffect` before transport or artifact
  access.
- Require effect binding, trigger marker, cycle, poll sequence, and token alignment.
- Use an injected completion clock after the read, not claim time.
- Convert complete observations to exactly one of:
  - `EffectSucceeded(BotStillWaitingOutcome)`;
  - `EffectSucceeded(VerifiedNoFindingsOutcome)`;
  - `EffectSucceeded(EligibleThreadsObservedOutcome)`;
  - `EffectRetryableFailure`; or
  - `EffectBlocked`.
- Return no untyped exception containing raw GitHub output. Unexpected programming or
  invariant failures cross the existing safe engine/worker error boundary without
  being mislabeled as a transient GitHub error.

### 7. Harden the worker lease around external execution

- Add bounded in-flight renewal to `EffectWorker` without changing claim or completion
  transaction ownership.
- Make renewal timing deterministic/injectable and prove no leaked renewal activity.
- Always complete through the original claim/generation. Preserve the current stable
  completion submission ID semantics.
- Test heartbeat success, heartbeat rejection, abort during blocked fake read, new
  generation during late return, executor exception, and normal short fake execution.

### 8. Integrate only through the durable public API

- In engine integration tests, reach `ObserveBotReviewEffect` through the normal
  reducer path: create a validated run, claim the preceding `RequestBotReviewEffect`,
  complete that fake write through `complete_claim()` with typed trigger evidence, then
  claim the emitted observe effect.
- Do not insert raw SQLite rows, weaken `PreparedState`-only creation, or add a fake
  production trigger adapter.
- Verify observation results affect state/outbox/timers only through
  `complete_claim()` and preserve every fence.

### 9. Add acceptance, regression, and handoff evidence

- Add all automated tests described below with sanitized fixtures and no network.
- Run focused v2, Phase 16.4, Phase 16.1 A/B, static, full-suite, docs, and package
  validation.
- Write `phase-16-5-to-16-6-handoff.md` with honest results and remaining Phase 16.6
  write/reconciliation boundaries.
- Do not stage, commit, push, or modify control-plane files. Leave final Git handling
  and review to the external orchestrator/user workflow.

## Testing Criteria

Automated tests are mandatory because this phase changes subprocess execution,
external-data parsing, error handling, retries, artifacts, worker leases, and durable
workflow behavior.

### Unit and contract tests

1. **Policy/models:** strict frozen DTOs reject extra fields, invalid repos/SHA/PR,
   empty reviewers, invalid timeouts/caps, invalid jitter, and unsafe artifact paths.
2. **Read argv:** exact direct argv, explicit cwd/timeout, no shell, no mutation query,
   no REST write method, no secrets/env serialization, and unsupported effects produce
   zero transport calls.
3. **Headers/JSON/GraphQL:** valid status/header split, case-insensitive allowlist,
   duplicate conflict, malformed JSON, GraphQL errors under HTTP 200, partial results,
   null nodes, invalid pageInfo, unchanged cursor, duplicate IDs, and bounded pages/items.
4. **Identity/drift:** exact repository/PR/open/non-fork/branches/full-head acceptance;
   blocks for auth, permission, not-found, closed PR, fork, repository drift, head drift,
   branch drift, HTTP 422, and contradictory data; retries HTTP 500/502/503/504 and an
   otherwise-unrecognized structured 5xx under their exact transient variants.
5. **Trigger/evidence:** missing/duplicate trigger, invalid timestamp, pre-trigger bot
   data, wrong reviewer, wrong/missing commit SHA, resolved threads, duplicate thread
   IDs, malformed root comment, and explicit no-findings rules.
6. **Outcome precedence:** eligible threads, verified no-findings, waiting, and the
   no-findings-plus-threads contradiction each map to the exact typed event.
7. **Backoff:** attempts 1-5 at 10/30/90/180/300 with -20/0/+20% jitter;
   `Retry-After`; exhausted reset; malformed/past header fallback; one-hour clamp;
   timezone normalization; and no jitter on directed waits.
8. **Artifacts:** canonical bytes/hash, atomic replacement, relative reference, secure
   modes, safe hashed run root, symlink/traversal rejection, schema/binding/hash
   mismatch, no partial success, and cleanup/recovery after injected write faults.
9. **Privacy:** fixture secrets, raw marker/body text, auth-like headers, session IDs,
   owner IDs, and raw stdout/stderr do not appear in artifacts, safe summaries, status,
   logs, exceptions, or serialized domain events.
10. **Lease coordinator:** periodic heartbeat, bounded stop/join, heartbeat loss,
    executor error, no second call, and deterministic event-based tests without
    timing sleeps.

### Integration tests

1. Use a fake executable placed ahead of any real `gh` or an injected transport; fail
   the test if real credentials/network could be used.
2. Traverse the honest reducer path through a fake claimed/completed
   `RequestBotReviewEffect`; never seed raw SQLite rows.
3. Persist waiting, verified-no-findings, and eligible-thread outcomes only through
   `complete_claim()` and reopen SQLite to verify state/status/next action.
4. Verify waiting increments only poll sequence, schedules the 60-second absolute
   eligibility, and does not increment the transient attempt.
5. Drive six consecutive transient failures: five timer-backed retry waits with the
   exact same logical effect identity and attempts 2-6, then typed retry exhaustion
   pause after attempt 6 with no seventh dispatch/timer.
6. Resume the exhausted batch and prove attempt resets to 1 with stable logical
   identity and a distinct dispatch/completion occurrence where the existing reducer
   requires it.
7. Honor reliable server-directed delays and the one-hour anomalous cap across SQLite
   close/reopen and timer firing.
8. Claim a read, close/reopen, expire/recover it, and prove the same READ_ONLY dispatch
   is requeued without a write or duplicate logical effect.
9. Hold a fake read while renewing its lease, then test abort and a replacement lease;
   the late completion must be fenced with unchanged snapshot/timers/outbox.
10. Independently fence wrong dispatch, claim, owner, generation, claimed run version,
    effect ID, attempt, cycle, and bound head SHA.
11. Query durable rows/status and inspect protected artifacts to prove no credentials,
    raw prompts, raw review bodies, session identifiers, or owner tokens persist.
12. Assert fake process transcripts contain only read operations and no mutation,
    write method, Git, Cursor, Codex, or model invocation.

### Regression evidence

- All existing `tests/unit/pr_review_v2` domain/durable tests remain green.
- All `tests/integration/test_phase16_4_*.py` durability/fencing tests remain green.
- `tests/integration/test_phase16_1_ab_regression_barrier.py` remains green.
- Full collection count must not decrease unexpectedly; every skip/xpass/new warning is
  explained in the handoff.
- Architecture tests prove legacy PR/A-B modules did not gain v2 imports and v2 did not
  import forbidden legacy modules.

## Validation

Cursor should run, from the repository root, with native WSL temp paths:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_*.py \
  tests/integration/test_phase16_5_*.py \
  tests/integration/test_phase16_1_ab_regression_barrier.py

uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m build
git diff --check
git status --short
```

Validation must not run `gh auth`, `gh api` against GitHub, `github-doctor`, a real PR,
real Git/GitHub writes, real Cursor/Codex, or model-backed smoke tests. If the fake
transport cannot prove a required contract, record the exact residual for Phase 16.8;
do not silently substitute a real credentialed call.

Before handoff, Cursor must inspect the final diff and confirm:

- no out-of-scope/control-plane/legacy lifecycle files changed;
- no GraphQL `mutation` or REST write operation exists in the v2 read gateway;
- no raw response/body/header logging or persistence path exists;
- every effect result reaches state only through `complete_claim()`;
- no SQLite schema/migration/version change was introduced; and
- the implementation handoff states the real tested boundary honestly.

## Risks Or Recovery Notes

- **Lease concurrency:** renewal adds a thread/task boundary. Use independent engine
  operations, deterministic stop/join, bounded transport timeouts, and final completion
  fencing. A lost heartbeat never authorizes another call.
- **GraphQL partial data:** GitHub can return data and errors together. Do not interpret
  partial connections as empty/no-findings. Retry only typed temporary/resource errors;
  otherwise block for inspection.
- **Rate-limit ambiguity:** GraphQL primary exhaustion may use HTTP 200. Inspect
  structured GraphQL errors plus rate headers; never rely only on HTTP status.
- **Private-resource 404:** do not expose repository existence or guess credentials.
  Return a safe typed not-found/operator condition without raw remote detail.
- **Artifact usefulness versus privacy:** future adjudication needs sanitized review
  content, while raw bodies are prohibited. Fail closed if sanitization or size limits
  would make the frozen evidence incomplete; never truncate and claim completeness.
- **Crash after artifact write before event commit:** the artifact may be orphaned but
  no durable outcome points to missing data. Keep deterministic paths/hash content so a
  retry can reuse identical safe bytes; do not delete broad directories automatically.
- **Crash after claim:** existing READ_ONLY expired-claim recovery requeues the same
  dispatch. No write reconciliation is needed and no new effect identity is invented.
- **Policy wiring:** Phase 16.5 exposes constructor-injected policy only. Public config,
  CLI, daemon, and actual credential/session use remain later-phase work.
- **Legacy reference code:** matching logic may inform tests, but importing or modifying
  legacy gateway/state creates a prohibited coupling. Reimplement the narrow v2
  contract behind its own typed boundary.
- **No real GitHub acceptance:** sanitized fake contracts cannot prove current account,
  organization, or live PR behavior. Phase 16.8 owns user-approved real acceptance.

If implementation encounters an architecture, privacy, or public-behavior ambiguity
not resolved here, stop before dependent changes and request a plan amendment. Do not
weaken validation or cross into Phase 16.6/16.7 to make a test pass.

## OpenQuestions

None.
