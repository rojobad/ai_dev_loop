# Phase 16.6 - PR Review v2 Idempotent Writes And Reconciliation

Status: proposed for approval

Depends on:

- Phase 16.5 being accepted, committed, and merged as the clean baseline;
- the Phase 16.3 pure reducer remaining the sole workflow decision authority;
- the Phase 16.4 SQLite journal, outbox, claims, leases, timers, and completion fencing
  remaining authoritative;
- the Phase 16.5 read-only GitHub gateway, protected observation artifacts, typed
  failure/backoff mapping, and bounded lease renewal remaining intact; and
- the Phase 16.1 A/B regression barrier remaining green.

Entry conditions are satisfied on 2026-07-21:

- `main` and `origin/main` point to merge commit `e2aa214`, which contains Phase 16.5;
- the worktree and index are clean;
- `archive/implementation-history/findings/phase-16-5-to-16-6-handoff.md`
  records zero actionable findings from the detached five-turn implementation review;
- that handoff records 383 focused and 1211 full-suite tests passing, plus clean Ruff,
  Mypy, MkDocs, package-build, and diff validation; and
- the user confirmed on 2026-07-21 that `GeneratePublicationTextEffect` remains wholly
  Phase 16.7 work. Phase 16.6 must not implement, reclassify, or fake that `LOCAL`
  effect.

The handoff's older entry-baseline text naming `31ca91f` is historical. The clean
current branch and merged Phase 16.5 tree are authoritative. Cursor must re-check the
baseline before editing and stop if it has drifted or contains unrelated changes.

## Goals

Implement production-capable, constructor-injected Git and GitHub write executors for
the seven existing PR review v2 `MUTATING` effects, plus a read-only reconciliation
executor for their seven existing strategies, without adding public orchestration or
duplicating durable intent.

This phase must:

- execute `CommitPatchEffect`, `PushCommitEffect`, `CreateOrUpdatePrEffect`,
  `RequestBotReviewEffect`, `PostThreadReplyEffect`, `UpdatePrTextEffect`, and
  `ResolveThreadEffect` behind narrow typed gateways;
- route `ReconcileWriteEffect` to `FIND_COMMIT_AT_HEAD`, `FIND_REMOTE_REF`,
  `FIND_PR_BY_HEAD_BASE`, `FIND_REVIEW_MARKER`, `FIND_THREAD_REPLY`, `FIND_PR_TEXT`, or
  `FIND_THREAD_RESOLVED` using only predefined read operations;
- keep the claimed Phase 16.4 dispatch/outbox row as the sole durable write intent;
  do not add another database, intent file, or shadow state machine;
- verify the current claim/lease authority immediately before every mutating process
  start, while retaining the Phase 16.5 heartbeat coordinator and final
  `complete_claim()` fencing;
- serialize repository mutation with the existing generic repository-lock convention,
  stored outside the target repository, without importing legacy PR lifecycle code;
- verify repository identity, repository root, current branch, full HEAD SHA, staged
  patch bytes/hash, artifact hashes, PR binding, remote ref, and thread identity before
  the corresponding mutation;
- use stable effect/idempotency identities and operation-specific markers so replay,
  restart, timeout, and explicit resume do not duplicate commits, pushes, PRs,
  comments, replies, text updates, or resolutions;
- distinguish a deterministic pre-write rejection from a transient pre-write failure
  and from an ambiguous post-write result;
- emit `WriteOutcomeUncertain` after timeout, connection loss, process termination,
  malformed mutation response, lease loss, or another result that may have applied;
  never retry an ambiguous write directly;
- produce only strong, strategy-specific `APPLIED`, `PROVEN_NOT_APPLIED`, or
  `UNRESOLVED` reconciliation evidence;
- return every effect result through the original claim and
  `PrReviewEngine.complete_claim()`, preserving every existing fence;
- read sensitive patch, commit-message, publication-text, and reply artifacts through
  a protected hash-verifying reader and keep their contents out of argv, SQLite,
  journal events, status, logs, and safe exceptions;
- preserve direct argv, `shell=False`, explicit cwd/timeouts, separated stdout/stderr,
  process-group termination, no-force push, and fail-closed drift behavior; and
- prove idempotency and recovery using injected fake GitHub processes, local temporary
  Git repositories/bare remotes, real temporary SQLite files, deterministic clocks,
  and fault hooks, with no real credentials or external network.

## Non-Goals

- Do not implement `GeneratePublicationTextEffect`. The user explicitly assigned the
  entire effect to Phase 16.7, including deterministic or model-backed generation.
- Do not implement `AdjudicateThreadsEffect` or `RunLocalFixEffect`; do not invoke
  Cursor, Codex, the extracted local review loop, or any model.
- Do not change `ObserveBotReviewEffect` semantics or fold polling into reconciliation.
- Do not add a public `pr-review-v2` CLI, daemon, controller route, worker lifecycle,
  ProjectConfig surface, or end-to-end workflow wiring. Phase 16.7 owns orchestration.
- Do not use real GitHub credentials, a real authenticated `gh`, external network, or a
  real PR/remote during implementation or automated validation. Phase 16.8 owns
  controlled live acceptance.
- Do not migrate, repair, call, or add compatibility branches to Phase 15 PR review.
- Do not merge, close, retarget, approve, dismiss, or auto-merge a PR; request
  reviewers; edit labels; mutate reactions; delete comments; or mutate any ref other
  than the exact non-force head-branch push described by the claimed effect.
- Do not apply, stage, unstage, reset, clean, stash, checkout, switch, merge, rebase, or
  amend repository content. `CommitPatchEffect` commits only an already-staged patch
  whose exact bytes match its protected artifact.
- Do not add a new retry counter, scheduler, write-ahead file, or SQLite schema. The
  existing reducer and durable engine own attempts, timers, uncertainty, and outbox.
- Do not claim daemon continuity, cross-process cancellation of every already-issued
  remote request, or end-to-end v2 completion.

## Scope

Add an isolated write/reconciliation slice under
`src/ai_dev_loop/pr_review_v2/`. Exact filenames may be adjusted to avoid a demonstrated
import cycle, but preserve the responsibility split below:

```text
src/ai_dev_loop/pr_review_v2/
  application/
    write_contracts.py             # frozen policies, DTOs, ports, proof outcomes
    write_reconciliation.py        # pure marker/proof/error mapping helpers
  infrastructure/
    input_artifacts.py             # protected ArtifactRef readers and typed schemas
    git_write_transport.py         # predefined direct Git/SSH read+write operations
    git_publication_gateway.py     # commit/push preflight, confirmation, reconciliation
    gh_write_transport.py          # predefined gh mutation and reconciliation operations
    github_write_gateway.py        # PR/comment/reply/text/thread validation and writes
    write_evidence_artifacts.py    # atomic safe trigger/write evidence when required
  workers/
    effect_executor_router.py      # route READ_ONLY/MUTATING/RECONCILING only
    write_executor.py              # seven MUTATING effects -> typed events
    reconcile_write_executor.py    # seven strategies -> typed reconciliation events
    effect_worker.py               # narrow pre-write authority integration only
```

Cursor may make narrow, explicitly required changes to:

- `domain/effects.py`, `domain/state.py`, `domain/reducer.py`, and domain exports to add
  an expected branch to `CommitPatchEffect` and populate/validate it on initial and fix
  publication paths. This is the only pre-approved domain-shape addition: it is needed
  to prove branch alignment before commit rather than after mutation;
- `application/contracts.py` and `application/engine.py` for a read-only, typed claim
  authority check/port used immediately before mutation. It must not expose raw SQL or
  hold a SQLite transaction during process execution;
- `infrastructure/paths.py` only for safe run-scoped write-evidence paths that reuse
  the Phase 16.5 hashed run root and permission conventions;
- package `__init__.py` files for a deliberately small write/reconciliation API;
- existing Phase 16.3-16.5 tests where the additive expected-branch field, specialized
  executor contract, or architecture boundary requires updates; and
- architecture tests to keep read-only Phase 16.5 modules mutation-free and prevent
  new v2 modules from importing legacy PR/config/runner/state/lifecycle modules.

Expected new focused tests and fixtures:

```text
tests/unit/pr_review_v2/
  test_write_contracts.py
  test_input_artifacts.py
  test_git_write_transport.py
  test_git_publication_gateway.py
  test_github_write_transport.py
  test_github_write_gateway.py
  test_write_executor.py
  test_reconcile_write_executor.py
  test_write_authority.py

tests/integration/
  test_phase16_6_git_writes.py
  test_phase16_6_github_writes.py
  test_phase16_6_reconciliation.py
  test_phase16_6_crash_restart_and_fencing.py
  test_phase16_6_privacy_and_isolation.py

tests/fixtures/pr_review_v2/github_writes/
  *.json
  *.txt
```

Names may be consolidated, but unit contract tests, fake-process GitHub integration,
real-temporary-SQLite workflows, and local temporary Git repository/bare-remote tests
must remain distinguishable.

Add the implementation handoff:

```text
archive/implementation-history/findings/phase-16-6-to-16-7-handoff.md
```

The handoff must report the final public API, exact commands/results, per-effect proof
rules, marker formats, artifact schemas, fault-injection coverage, test inventory,
isolation evidence, honest boundary, and residual risks. It must not claim real GitHub,
credential, daemon, Cursor, Codex, or end-to-end acceptance.

## Out of Scope

Cursor must not modify:

- `src/ai_dev_loop/state.py`, legacy schemas, legacy run directories, or legacy
  `RunState`/`GithubPrReviewState` persistence;
- `src/ai_dev_loop/config.py`, `src/ai_dev_loop/schemas/project-config-v1.json`,
  `ai_dev_loop.yaml`, CLI routing/help, public config, or public command docs;
- `src/ai_dev_loop/commands/pr_review*.py`, `pr_review_worker.py`,
  `external_adjudication.py`, `github_pr_review_result.py`, or legacy PR status;
- `src/ai_dev_loop/runners/github.py`, `runners/publish.py`, legacy Git runners, or
  legacy error/recovery classification. They are reference evidence only and must not
  be imported or called by v2;
- `src/ai_dev_loop/local_review_loop.py`, `workflow_engine.py`, `iterations.py`,
  `resume_planner.py`, or A/B prepare/start/resume/recover/extend/abort behavior;
- `src/ai_dev_loop/pr_review_v2/infrastructure/gh_transport.py` or
  `github_read_gateway.py` with any mutation operation. Phase 16.5 remains strictly
  read-only; new write operations belong in separately named modules;
- the Phase 16.4 migration SQL, `PRAGMA user_version = 1`, SQLite tables, journal,
  snapshot CAS, timer reconciliation, outbox identity, or completion transaction
  protocol merely to simplify a gateway;
- `src/ai_dev_loop/process.py`, `redaction.py`, `locking.py`, or shared `paths.py` unless
  a concrete defect prevents the scoped implementation and the plan is amended;
- `.cursor/rules/`, `.cursor/skills/`, `.agents/skills/`, integrations, hooks,
  SessionStart assets, controller/handoff skills, or the Codex session bridge;
- public MkDocs/README claims that PR review v2 writes are available; or
- Git history, commits/pushes against this repository, tags, branches, remotes, final
  staging, user-global files, real credentials, or real external services.

If an out-of-scope production file appears necessary, Cursor must stop before editing
it and request a plan amendment. Do not weaken a safety invariant or import legacy code
to avoid that stop.

## Required Context

Read before implementation:

1. `pr-review-v2-restructure-context.md` in full, especially effect execution,
   reads/writes, Phase 16.6, fault injection, privacy, and agent rules.
2. `archive/implementation-history/findings/phase-16-5-to-16-6-handoff.md` in full.
3. `archive/implementation-history/plans/phase-16-3-pr-review-v2-domain-and-reducer.md`.
4. `archive/implementation-history/plans/phase-16-4-pr-review-v2-durable-engine.md`
   and its clarifications companion.
5. `archive/implementation-history/plans/phase-16-5-pr-review-v2-github-read-gateway.md`.
6. All code under `src/ai_dev_loop/pr_review_v2/`, especially:
   - mutating/reconciling effect models and stable identities;
   - success/uncertain/reconciliation events;
   - reducer publication, reply, resolution, uncertainty, retry, and reconciliation
     transitions;
   - engine claim/completion/recovery/abort/lease methods;
   - Phase 16.5 typed transport, error/backoff helpers, artifact paths/store, read
     executor, and lease-renewing worker.
7. All `tests/unit/pr_review_v2/`, all `tests/integration/test_phase16_4_*.py`, and all
   `tests/integration/test_phase16_5_*.py`.
8. `tests/integration/test_phase16_1_ab_regression_barrier.py`.
9. `src/ai_dev_loop/runners/publish.py`, `runners/github.py`, and their tests only as
   legacy evidence for branch/patch checks, SSH safety, non-force push, marker matching,
   direct argv, and response validation. Do not import them into v2.
10. `src/ai_dev_loop/process.py`, `redaction.py`, `locking.py`, and `paths.py` for generic
    process-group, stdin, redaction, repository-lock, XDG, atomic-write, and permission
    conventions.
11. Current `/docs`, `archive/implementation-history/master-plan.md`, and current code,
    tests, and schemas where they define stronger behavior than archived history.
12. Official transport documentation:
    - <https://cli.github.com/manual/gh_api>;
    - <https://docs.github.com/en/rest/pulls/pulls>;
    - <https://docs.github.com/en/rest/issues/comments>;
    - <https://docs.github.com/en/rest/pulls/comments>;
    - <https://docs.github.com/en/graphql/reference/mutations#addpullrequestreviewthreadreply>;
    - <https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread>; and
    - <https://git-scm.com/docs/git-commit>, <https://git-scm.com/docs/git-push>, and
      <https://git-scm.com/docs/git-ls-remote>.

Use only documented structured response fields and stable command semantics. Do not
classify lifecycle or write application from human-readable CLI prose.

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

### 1. Durable intent, reducer, and completion authority

- The claimed Phase 16.4 dispatch is the only durable write intent. Do not create a
  second intent log, sidecar state machine, database, or gateway checkpoint.
- `PrReviewState` and `reduce_pr_review()` remain the only lifecycle decision source.
  A gateway may validate/execute/prove an operation but may not advance state, schedule
  retries, or choose successor effects.
- A write executor may construct only a typed `EffectSucceeded`,
  `EffectRetryableFailure`, `EffectBlocked`, or `WriteOutcomeUncertain` for the exact
  current token. A reconciliation executor may additionally construct
  `EffectSucceeded(ReconciliationResolvedOutcome)`.
- Every result enters through `PrReviewEngine.complete_claim()`. Never call
  `apply_event()` for an effect result and never mutate SQLite directly.
- Preserve dispatch, claim, owner, generation, claimed run version, effect ID,
  attempt, cycle, original-write equality, and bound full-SHA fences.

### 2. Layer and import boundaries

- `domain/` stays pure and free of Git, GitHub, subprocess, filesystem, clocks,
  randomness, SQLite, and legacy imports.
- `application/` owns frozen DTOs, policies, ports, proof enums, marker derivation, and
  centralized typed error mapping; it does not run processes or read files.
- `infrastructure/` owns protected artifact reads/writes, Git/SSH/`gh` argv, stdin,
  timeouts, parsing, and strategy-specific remote/local evidence.
- `workers/` routes claimed effects and adapts gateway results to domain events. It has
  no raw database access and does not interpret response bodies.
- No v2 module imports legacy PR commands/config/state/runners/workers/recovery or the
  extracted local Cursor/Codex loop. Generic shared `process`, `redaction`, `locking`,
  and `paths` utilities may be used without changing them.

### 3. Minimal domain amendment

- Add a required expected branch field to `CommitPatchEffect`; populate it from the
  source origin on initial publication and from the current PR binding on fix
  publication.
- Validate the branch as non-empty, argv-safe data and include it in variant
  persistence/serialization tests.
- Do not reclassify any effect, add write behavior to a `LOCAL`/`READ_ONLY` effect, or
  widen `MutatingEffect`/reconciliation strategy membership.
- No other domain-shape addition is authorized unless an executable safety invariant
  proves it necessary; stop and amend this plan before adding one.

### 4. Pre-write authority and repository serialization

- The worker owns claim/lease identity. Introduce a narrow authority-aware executor
  contract or callback that lets a mutating executor request an engine-owned current
  claim check immediately before process start.
- The authority check validates the current active lease owner/generation, unexpired
  lease, claimed dispatch/claim, claimed run version, effect identity, attempt, cycle,
  and bound SHA. It returns only a typed boolean/result, not rows or a connection.
- Do not hold a SQLite transaction while waiting for a repository lock or process.
- Acquire the existing generic repository lock before final local/remote preflight and
  retain it through the one write attempt and immediate confirmation read. Lock
  metadata is safe/redacted and stored under XDG state, never in the repository.
- Re-check authority after lock acquisition and directly before the mutating process.
  If authority is lost, perform no write and let the original completion be fenced;
  do not make a second external call.
- Heartbeat loss during a call does not prove whether the write applied. Stop after the
  bounded call, offer the eventual typed result once, and rely on final fencing plus
  expired-claim reconciliation. Never execute a second write in that worker step.

### 5. Direct process and sensitive-input contract

- Use direct argv, `shell=False`, explicit cwd and timeout, separated stdout/stderr,
  and process-group termination/reaping on timeout.
- Pass PR text, comment bodies, replies, GraphQL mutation documents/variables, and Git
  commit messages through stdin (`gh api --input -`, `git commit --file -`, or an
  equivalently tested direct form), never command-line argv.
- Do not inherit or persist a full process environment. Build only the minimum
  allowlisted environment needed for Git/SSH/`gh`, and never log auth variables or
  `SSH_AUTH_SOCK` paths.
- Raw stdout/stderr and mutation response bodies remain ephemeral. Safe errors contain
  only typed classifications and bounded redacted summaries.
- Fault hooks may raise at named checkpoints but must not alter production semantics or
  persist sensitive payloads.

### 6. Protected input and evidence artifacts

- Resolve every `ArtifactRef` beneath the Phase 16.5 hashed run root; reject absolute
  paths, traversal, symlinks, non-regular files, unsafe modes, missing files, size
  overflow, and SHA-256 mismatch before mutation.
- Treat patch and reply artifacts as opaque sensitive UTF-8/binary content with exact
  byte hashes. Define strict versioned JSON DTOs for commit message and publication
  text (`title`/`body`) so Phase 16.7 has one writer contract.
- `CommitPatchEffect` requires the live `git diff --cached --binary` bytes to match the
  referenced patch exactly and be non-empty. Do not apply or restage the artifact.
- Write only minimal sanitized content-addressed evidence needed by domain outcomes,
  especially trigger comment identity/time/hash. Reuse the Phase 16.5 run-root,
  `0700` directories, `0600` files, atomic publication, and relative `ArtifactRef`.
- Never persist raw patches, publication text, replies, commit messages, response
  bodies, auth material, or full process output in SQLite/events/status/logs.

### 7. Stable markers and write payloads

- Preserve the exact `RequestBotReviewEffect.marker` required by Phase 16.5 observation
  in the trigger HTML comment. The review command body remains constructor-injected,
  with `@codex review` as the reference default; it is not read from `ProjectConfig`.
- Derive other public idempotency markers from a domain-stable operation label plus a
  SHA-256 digest of `effect.idempotency_key`; do not expose raw run IDs, owner tokens,
  claim IDs, lease generations, or session IDs in those markers.
- Append exactly one owned marker to PR bodies and inline replies. Reconciliation must
  compare the exact marker and the hash of the intended unmarked content.
- Append a deterministic safe trailer derived from the commit effect idempotency key to
  the commit message. Reconciliation must also verify parent and exact patch/tree
  evidence; the trailer alone is insufficient.
- Marker derivation is pure, versioned, deterministic, and stable across attempts,
  dispatch occurrences, restart, and explicit resume.

### 8. Git commit and push invariants

- Before commit, require the injected repository cwd to be the resolved repository
  root, the current branch to equal the effect's expected branch, `HEAD` to equal
  `expected_head_sha`, no tracked unstaged or untracked non-ignored content, and the
  staged binary diff to equal `patch_ref` bytes/hash.
- Commit exactly once using the verified commit-message artifact plus owned trailer.
  After a successful process, verify new `HEAD`, single parent equal to expected HEAD,
  exact committed diff/patch hash, branch, and trailer before returning
  `CommitRecordedOutcome`.
- For push, require local `HEAD == effect.commit_sha`, current branch and normalized
  destination ref equal `effect.remote_ref`, `force is False`, and the configured
  remote URL path matches `effect.repository`.
- Preserve the legacy-safe default remote policy (`origin`, SSH-only, usable effective
  agent identity) behind constructor injection; do not import the legacy publisher.
- Read the remote ref before mutation. Only an absent ref or the exact commit parent is
  eligible for a non-force push; the exact commit is already applied. Any other SHA is
  drift and blocks before mutation.
- Invoke an explicit non-force refspec from the exact commit to
  `refs/heads/<remote_ref>`; never use `--force`, `--force-with-lease`, wildcard
  refspecs, remote default push behavior, or delete syntax. Confirm the remote ref is
  the exact commit after success.

### 9. GitHub mutation boundary

- Keep `gh_transport.py` and `github_read_gateway.py` strictly read-only. Add a separate
  transport with only predefined methods for create/update PR, create trigger comment,
  reply to a review thread, update PR text, and resolve a thread, plus narrowly named
  reconciliation reads.
- Every operation names owner/repository/PR/thread/ref explicitly; do not use ambient
  `{owner}/{repo}` placeholders or expose generic `graphql`, `rest`, endpoint, method,
  or arbitrary-query public methods.
- Before a GitHub mutation, verify repository/PR/full-head/branches/open/non-fork
  binding and the operation-specific target. Validate the returned resource identity
  immediately after a structured success.
- `CreateOrUpdatePrEffect` uses a complete unique lookup by repository/head/base. Zero
  exact PRs permits create; one exact open PR permits idempotent adoption/update;
  multiple, closed, cross-repository, or contradictory candidates block before write.
- Request trigger and reply operations search complete paginated evidence for their
  exact markers first. One exact prior match is success without a write; duplicates or
  contradictory matches block.
- PR text update succeeds without a write when the exact title/body hash and owned
  marker are already present. Thread resolution succeeds without a write when the
  exact bound thread is already resolved.
- Never add merge, close, retarget, delete, reaction, label, reviewer-request, or
  arbitrary mutation capability.

### 10. Deterministic versus transient versus ambiguous failure

- A failure before any mutating process starts may be:
  - `EffectRetryableFailure` for a typed temporary preflight read/network/rate-limit
    failure; or
  - `EffectBlocked` for auth, permission, missing executable, drift, invalid artifact,
    malformed target, deterministic HTTP rejection, or required operator action.
- After a mutating process starts, only a complete structured response that proves
  rejection without application may use normal retry/block semantics.
- Timeout, connection loss/reset, process signal/crash, missing/unparseable mutation
  envelope, partial GraphQL mutation data, HTTP 5xx after dispatch, or success whose
  resource identity cannot be confirmed produces `WriteOutcomeUncertain` with the exact
  original write and a deterministic reconciliation identity.
- Once uncertain, do not perform an inline second write or hide reconciliation inside
  the write gateway. The reducer emits the sole `ReconcileWriteEffect`.
- Reconciliation reads may themselves retry through the ordinary six-attempt budget;
  they never create nested `WriteOutcomeUncertain` or another reconciliation effect.

### 11. Strategy-specific proof contract

Each strategy returns exactly one of the following:

- `APPLIED`: positive evidence identifies the same effect and constructs the exact
  confirmed outcome expected by the original write;
- `PROVEN_NOT_APPLIED`: complete authoritative evidence makes one retry of the original
  operation safe, with an absolute UTC `next_attempt_at`; or
- `UNRESOLVED`: evidence is absent, incomplete, conflicting, drifted, or cannot safely
  distinguish this effect from another actor. The reducer pauses for inspection.

Required proof rules:

| Strategy | `APPLIED` | `PROVEN_NOT_APPLIED` | `UNRESOLVED` |
|---|---|---|---|
| commit at head | HEAD has the exact owned trailer, expected parent, branch, and patch/tree hash | HEAD is still expected, staged patch is still exact, repository lock is held, and no commit/lock ambiguity remains | another HEAD, mismatched parent/tree/trailer, active Git ambiguity, or incomplete evidence |
| remote ref | exact remote ref equals intended commit and local commit validates | ref is absent or still exact intended parent, with local commit/branch/repository valid | any other ref SHA, multiple/invalid results, or incomplete remote evidence |
| PR by head/base | exactly one open same-repo PR has head/base/full SHA, exact text hash, and owned marker | complete lookup has no exact PR, or one exact bound PR lacks the intended marker/text and is safe to idempotently update | multiple candidates, fork/closed/drift, marker collision, or incomplete lookup |
| review marker | exactly one bound post contains the exact trigger marker; persist/verify trigger evidence | complete comment pagination proves no exact marker | duplicate/contradictory marker or incomplete comments |
| thread reply | exact bound thread contains exactly one reply with owned marker and intended body hash | complete thread-comment pagination proves absence while the thread/binding remains valid | duplicate marker, missing/drifted thread, resolved/changed target with uncertain attribution, or incomplete comments |
| PR text | exact bound PR title/body hash and owned marker match | exact bound PR is readable and lacks the intended marker/content, making the same PATCH idempotently safe | binding drift, conflicting marker/content, or incomplete response |
| thread resolved | exact bound thread is resolved | exact bound thread is present and unresolved | missing/drifted/contradictory thread identity or incomplete response |

Absence counts only after complete bounded pagination and exact binding validation. A
truncated page, repeated cursor, item/page cap, partial GraphQL response, timeout, or
unclassified response is never proof of non-application.

### 12. Retry, backoff, and outcome construction

- Reuse the Phase 16.5 typed 10/30/90/180/300-second, +/-20% jitter, server-directed
  delay, and one-hour cap helpers where applicable; do not duplicate policy inside
  SQLite or the gateway.
- Pre-write transient failures consume the current effect attempt. A
  `PROVEN_NOT_APPLIED` reconciliation schedules the next original-write attempt using
  the original write attempt and the reducer's existing budget.
- Normal idempotent preflight detection of an already-applied write is success and does
  not consume another write attempt.
- Construct confirmed outcomes with exact existing domain fields: commit/new head,
  commit/ref, PR binding, trigger evidence, thread/reply ref, PR binding/text ref, or
  thread ID.
- Attempt six still emits a schema-valid retryable/proven-not-applied result; the
  reducer owns exhaustion and must create no seventh timer/dispatch.

### 13. Privacy, logging, and artifacts

- Treat patch, publication text, reply text, commit message, remote bodies, stdout,
  stderr, environment values, URLs with credentials, SSH socket paths, and process
  stdin as sensitive.
- Safe summaries may include operation kind and typed classification, but not raw
  bodies, patches, prompts, output, auth headers, remote URLs, usernames, owner tokens,
  claim IDs, full run/session IDs, or arbitrary Git/GitHub prose.
- The journal, SQLite payloads, status DTO, default logs, exceptions, and handoff may
  contain only typed enums, stable safe IDs already required by the domain, hashes,
  relative artifact refs, and redacted bounded summaries.
- Automated fixtures contain synthetic/sanitized data only. Fake processes must clear
  inherited `GH_TOKEN`, `GITHUB_TOKEN`, credential helpers, and network routes where
  practical, and fail if an unexpected executable or endpoint is requested.

### 14. Phase and legacy isolation

- Preserve the complete Phase 16.1 A/B behavior and Phase 16.3-16.5 suites.
- Do not create a fallback from v2 to legacy publisher/GitHub/config/state/recovery.
- Do not add a placeholder `LOCAL` executor that returns success. The router must
  reject unsupported local effects without process/artifact/network activity.
- Do not stage, commit, or push the ai_dev_loop implementation repository. Local Git
  writes in automated tests are permitted only inside disposable temporary repositories
  and local bare remotes.

## Implementation Plan

### 1. Freeze write, proof, policy, and authority contracts

- Add strict frozen DTOs/protocols for Git/GitHub policies, process results, protected
  input artifacts, mutation confirmations, typed preflight failures, ambiguous
  outcomes, reconciliation proof, authority checks, and safe fault hooks.
- Define one pure versioned marker derivation helper and strict publication/commit
  artifact schemas.
- Add the expected branch to `CommitPatchEffect`; update reducer constructors,
  validators, exports, serialization, variant-persistence, and invariant tests.
- Add architecture tests before gateways so Phase 16.5 read modules cannot gain a
  mutation and new v2 code cannot import legacy lifecycle/publisher modules.

### 2. Add protected input and write-evidence artifacts

- Implement safe run-root resolution and exact SHA-256 verification for patch, reply,
  commit-message, and publication-text references.
- Enforce size/encoding/schema/path/symlink/permission checks without copying sensitive
  content into errors.
- Add an atomic content-addressed writer/reader for minimal trigger/write evidence,
  reusing Phase 16.5 path and mode conventions.
- Prove crash after evidence write leaves at most a reusable orphan and cannot overwrite
  an accepted `ArtifactRef` with different bytes.

### 3. Implement the direct Git/SSH transport

- Expose only named repository inspection, staged-patch, commit, remote-ref, and push
  operations needed by this plan.
- Use direct argv and stdin for commit messages; bound and reap process groups.
- Implement strict repository-root, branch, HEAD, status, remote URL/NWO, SSH agent,
  ref, parent, trailer, and patch/tree parsers without human-prose lifecycle inference.
- Add fake-runner unit tests for argv/stdin/env/timeout/redaction and local-temporary-Git
  contract tests for actual Git semantics.

### 4. Implement commit execution and reconciliation

- Validate authority, acquire repository lock, repeat authority check, validate all
  commit preconditions, and invoke exactly one commit.
- Confirm exact new commit evidence before returning success; otherwise emit
  uncertainty after process start.
- Implement `FIND_COMMIT_AT_HEAD` with the proof table above and no repository mutation.
- Fault-inject before process start, after start, after commit before confirmation,
  after confirmation before completion, and during reconciliation.

### 5. Implement push execution and reconciliation

- Validate local commit/branch/repository/SSH and remote-ref baseline under the
  repository lock.
- Return success without mutation when the remote ref already equals the intended
  commit; otherwise issue one explicit non-force push only from absent/parent baseline.
- Confirm the exact remote SHA, or emit uncertainty.
- Implement `FIND_REMOTE_REF` and fault/restart coverage for every boundary.

### 6. Implement the predefined GitHub mutation transport

- Add fixed REST/GraphQL operations for PR create/update, issue-comment trigger,
  review-thread reply, PR text update, and thread resolution.
- Add fixed reconciliation reads for PR-by-head/base/text, complete thread comments,
  and thread resolution where Phase 16.5 primitives are insufficient.
- Use explicit owner/name/PR/thread inputs, stdin JSON, `--include`, allowlisted
  headers, bounded serial pagination, and centralized structured error parsing.
- Test exact mutation names/methods, response identity, partial GraphQL mutation data,
  rate limits, 4xx/5xx, timeout, malformed envelopes, and absence of generic methods.

### 7. Implement PR create/update and PR-text execution/reconciliation

- Read and verify the publication artifact; derive the owned marker and intended raw
  title/body hash.
- Validate remote head and unique head/base PR candidates before POST/PATCH.
- Confirm returned repository/number/open/head/base/full SHA and exact text/marker.
- Implement `FIND_PR_BY_HEAD_BASE` and `FIND_PR_TEXT` using complete typed reads and the
  proof table; do not infer from `gh` prose or an unbounded search.

### 8. Implement trigger, reply, and thread-resolution execution/reconciliation

- Validate the exact binding and target thread/comment evidence before mutation.
- Search for the exact stable marker before posting; one match is idempotent success,
  zero permits one write, and duplicates block.
- Persist safe trigger evidence before constructing
  `ReviewTriggerConfirmedOutcome`.
- Implement `FIND_REVIEW_MARKER`, `FIND_THREAD_REPLY`, and
  `FIND_THREAD_RESOLVED` with complete pagination and exact IDs/hashes.

### 9. Implement write and reconciliation executors/router

- Reject token/effect/original-write/strategy mismatches before artifact or process
  activity.
- Route the seven mutating effects to the correct gateway and map results to exact
  typed events.
- Derive stable reconciliation identity from run/effect/attempt/operation data without
  claim/generation-dependent public markers.
- Route the seven reconciliation kinds, centralize transient/block mapping, and compute
  absolute eligibility from an injected completion clock.
- Compose with `GitHubReadExecutor` through a narrow router; reject every `LOCAL` effect
  and any unsupported kind with zero external calls.

### 10. Integrate pre-write authority with the bounded worker

- Extend `EffectWorker` only enough to provide the authority-aware execution context to
  the mutating executor while preserving existing read executor behavior.
- Keep the Phase 16.5 renewal coordinator deterministic and leak-free.
- Prove authority re-check immediately before process start, abort/replacement/lost
  heartbeat behavior, one eventual `complete_claim()` offer, and no second write.
- Do not hold SQLite or repository locks across timer waits or between worker steps.

### 11. Add durable workflows, races, restart, and privacy coverage

- Reach every mutating/reconciling effect through validated reducer transitions and
  `complete_claim()`; do not insert raw SQLite rows.
- Use prepared protected fixtures for publication/commit/reply artifacts because the
  production `GeneratePublicationTextEffect` executor intentionally remains Phase 16.7.
- Reopen SQLite and repositories after each injected crash point; recover expired
  mutating claims through the existing uncertain/reconcile path.
- Exercise two workers, abort, lease replacement, stale late results, retry/resume
  identity, and privacy surfaces.

### 12. Add final validation and the Phase 16.7 handoff

- Run the focused v2/16.4/16.5/16.6/A-B suites, static checks, full suite, strict docs,
  package build, and diff checks listed below.
- Inspect the final diff for scope, read/write separation, generic transport escape
  hatches, raw sensitive data, force/destructive Git flags, and SQLite changes.
- Write `phase-16-6-to-16-7-handoff.md` with honest evidence and the exact remaining
  `LOCAL`/CLI/orchestration boundary.
- Do not stage, commit, push, or modify control-plane files. Leave final Git handling
  and review to the external orchestrator/user workflow.

## Testing Criteria

Automated tests are mandatory because this phase adds repository mutation, GitHub
mutation, subprocess/stdin execution, protected artifact reads, authority checks,
ambiguous-outcome recovery, and external-state reconciliation.

### Cross-cutting unit and contract tests

1. **Models/policies:** frozen strict DTOs reject extras, unsafe repo/branch/ref/marker,
   invalid timeouts/caps, unsupported remote schemes, invalid artifact schemas, and
   mismatched original-write/strategy pairs.
2. **Authority:** no write runner is called before claim/lease/run/effect authority;
   rejection, expiry, abort, replacement generation, wrong version/effect/attempt/cycle,
   and authority loss after repository-lock acquisition produce zero writes.
3. **Process boundary:** exact direct argv, cwd, timeout, stdin, minimal env, process
   group stop/reap, separated output, no shell, no sensitive body/message in argv, and
   no raw output in safe errors.
4. **Artifacts:** exact byte/hash/schema validation, size bounds, canonical JSON,
   symlink/traversal/mode rejection, content-addressed evidence collision safety,
   atomic write faults, and no sensitive content on ordinary output surfaces.
5. **Markers:** deterministic/stable across attempts/restart/resume, distinct by
   operation/target, no raw run/session/claim/owner/generation identity except the
   already-approved trigger marker, exact one-marker canonicalization, and collision
   rejection.
6. **Classification:** pre-write transient/block versus post-start uncertainty for
   timeout, connection family, process crash/signal, malformed/partial response,
   HTTP/GraphQL 4xx/5xx/rate-limit, identity mismatch, and deterministic rejection.
7. **Reconciliation:** every strategy validates the exact embedded original write and
   independently exercises `APPLIED`, `PROVEN_NOT_APPLIED`, `UNRESOLVED`, transient
   read retry, permanent block, malformed evidence, and attempt-six exhaustion.
8. **Architecture:** domain purity, no legacy imports, no mutation in Phase 16.5 read
   modules, no generic GraphQL/REST/write method, no SQLite access in gateways/workers,
   and no local/model executor in 16.6.

### Per-effect acceptance matrix

For each of the seven mutating effects, tests must cover:

| Effect | Preflight and idempotent-success evidence | Ambiguous boundary | Reconciliation assertions |
|---|---|---|---|
| commit patch | repo/branch/HEAD/status/exact staged patch/message; prior exact marked commit | timeout/signal/crash or success before completion | exact parent+branch+patch/tree+trailer; old HEAD safe retry; drift unresolved |
| push commit | local commit/branch/remote NWO/SSH; ref already exact commit | push timeout/nonzero/connection loss or success before completion | exact remote commit applied; absent/parent safe retry; other SHA unresolved |
| create/update PR | remote head and unique head/base; exact marked PR idempotent | POST/PATCH timeout/5xx/partial response or success before completion | exact unique marked binding applied; complete safe absence/old text retry; ambiguity unresolved |
| request review | exact binding and complete marker search | POST timeout/5xx/malformed response or comment before completion | one exact marker applied; complete absence retry; duplicates/incomplete unresolved |
| post reply | exact binding/thread/reply artifact and marker search | mutation timeout/5xx/partial response or reply before completion | exact marker+body hash applied; complete absence retry; target drift/duplicates unresolved |
| update PR text | exact binding/publication artifact; exact marked content idempotent | PATCH timeout/5xx/partial response or update before completion | exact content+marker applied; readable old content retry; drift/conflict unresolved |
| resolve thread | exact binding/thread; already resolved idempotent | mutation timeout/partial response or resolution before completion | resolved applied; exact unresolved retry; missing/drifted/incomplete unresolved |

Each row also requires deterministic pre-write rejection, pre-write transient retry,
abort before process start, abort during blocked fake write, heartbeat loss, lease
replacement, stale late completion, duplicate completion submission, and no second
write from the same worker step.

### Integration tests

1. Use injected fake `gh`/SSH runners and sanitized structured envelopes; fail if a real
   executable, external hostname, inherited credential, or unexpected endpoint/method
   is requested.
2. Use local temporary Git repositories and local bare remotes for real commit/push
   semantics. Never address the ai_dev_loop checkout or an external remote.
3. Traverse honest reducer paths to each effect. For publication effects, complete the
   preceding `GeneratePublicationTextEffect` through a validated fake result with
   protected fixtures; do not add a production local executor or raw SQLite rows.
4. For each write, inject faults before process start, during process execution, after
   the write applies but before confirmation, after confirmation/evidence artifact but
   before `complete_claim()`, and during each reconciliation read.
5. Expire/recover a claimed mutating dispatch after a simulated crash and prove the
   engine emits exactly one reconciliation dispatch, never a duplicate original write.
6. Reopen SQLite and the temporary repository/remote between write and reconciliation;
   assert the exact durable effect/idempotency/reconciliation identities survive.
7. Drive every strategy through applied, proven-not-applied, and unresolved evidence
   and verify the reducer advances, schedules one original retry, or pauses respectively.
8. Drive five timer-backed retries plus attempt-six pause for pre-write transient and
   reconciliation-read transient paths; explicit resume resets the attempt batch while
   preserving logical identity and using a distinct dispatch/completion occurrence.
9. Race completion versus abort and lease replacement with deterministic coordination;
   late completion is stale/rejected and never mutates snapshot/timers/outbox. If an
   external write already applied before abort, record only safe audit evidence and do
   not issue compensating/destructive work.
10. Run two engines/workers against the same SQLite/repository fixtures; prove leases,
    repository lock, preflight idempotency, and reconciliation prevent duplicate writes.
11. Verify write success and reconciliation receipts only through `complete_claim()`;
    independently fence wrong dispatch, claim, owner, generation, claimed version,
    effect ID, attempt, cycle, head SHA, original write, and strategy.
12. Query SQLite/status/journal, inspect artifacts and captured safe logs/errors, and
    prove no credentials, raw prompts, patches, publication text, replies, commit
    messages, review bodies, stdout/stderr, environments, SSH socket paths, owner
    tokens, or full session identifiers persist.
13. Assert fake process transcripts contain only the exact allowlisted Git/SSH/`gh`
    operations, no force/destructive Git flags, no arbitrary API call, and no Cursor,
    Codex, model, merge, close, retarget, or delete operation.

### Regression evidence

- All existing `tests/unit/pr_review_v2` domain/durable/read tests remain green.
- All `tests/integration/test_phase16_4_*.py` durability/fencing tests remain green.
- All `tests/integration/test_phase16_5_*.py` read/lease/privacy tests remain green.
- `tests/integration/test_phase16_1_ab_regression_barrier.py` remains green.
- Full collection count must not decrease unexpectedly; every skip, xfail, xpass, and
  new warning is explained in the handoff.
- Architecture tests prove legacy PR/A-B modules did not gain v2 imports and Phase 16.5
  read code remains mutation-free.

## Validation

Cursor should run from the repository root with native WSL temp paths:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_*.py \
  tests/integration/test_phase16_5_*.py \
  tests/integration/test_phase16_6_*.py \
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

Validation must not run real `gh auth`, a credentialed `gh api`, external `git
ls-remote`/push, a real PR mutation, GitHub doctor, real Cursor/Codex, or a model-backed
smoke test. Local Git commit/push is permitted only inside disposable test repositories
and local bare remotes created by the test suite.

Before handoff, Cursor must inspect the final diff and confirm:

- no out-of-scope, control-plane, config/CLI, public-doc, or legacy lifecycle file
  changed;
- Phase 16.5 `gh_transport.py`/`github_read_gateway.py` still contain no mutation;
- new transports expose no generic arbitrary endpoint/query/method or force/destructive
  Git operation;
- every mutating process is preceded by exact preflight plus current authority;
- every ambiguous write produces uncertainty/reconciliation rather than direct retry;
- every result reaches state only through `complete_claim()`;
- no raw sensitive body/patch/output/env logging or persistence path exists;
- no SQLite schema/migration/version change was introduced;
- no production executor exists for `GeneratePublicationTextEffect` or another `LOCAL`
  effect; and
- the implementation handoff states the real tested boundary honestly.

## Risks Or Recovery Notes

- **External exactly-once is evidence-based, not magical.** GitHub and Git do not offer
  one transaction with SQLite. Stable markers, exact preflight, durable intent, and
  conservative reconciliation prevent blind duplication; unresolved evidence pauses.
- **Crash while a child may still run:** process groups are bounded and reaped on
  handled timeout. A host/process crash can still leave an external operation in
  flight; restart must reconcile before any second write and treat active Git lock or
  changing evidence as unresolved.
- **Abort after dispatch:** abort fences local completion but cannot roll back a commit,
  push, or GitHub mutation already accepted remotely. Never compensate destructively.
  Preserve safe audit evidence and require manual inspection if external state is
  uncertain after terminal abort.
- **Repository lock versus engine lease:** the repository lock serializes local
  mutation; the engine lease authorizes one run generation. Neither replaces the
  other. Acquire without holding SQLite transactions and re-check authority after
  waiting for the repository lock.
- **Commit marker changes the commit message:** the protected artifact owns the human
  subject/body; the gateway adds one documented deterministic trailer solely for
  reconciliation. Phase 16.7 must generate artifacts against this contract.
- **PR/reply markers change remote bodies:** append only one hidden owned marker and
  compare unmarked content by hash. Never expose raw run/session/lease identity.
- **Authoritative absence requires completeness:** page/item caps, partial GraphQL,
  repeated cursor, timeout, or malformed evidence cannot prove non-application. Pause
  or retry instead of widening a negative inference.
- **Remote selection:** the reference policy uses validated SSH `origin`; a later
  public config phase may expose alternatives. Changing the constructor policy must
  still resolve to the same repository NWO and cannot change a durable logical target.
- **Artifact ownership:** Phase 16.6 reads publication/commit/reply artifacts and
  defines their schema but does not generate them. Phase 16.7 must supply protected
  writers/adapters and end-to-end preparation.
- **No real GitHub acceptance:** sanitized fakes and local Git remotes cannot prove live
  org/account permissions, bot behavior, API latency, or eventual consistency. Phase
  16.8 owns user-approved controlled acceptance.
- **Legacy reference code:** reuse ideas and test evidence, not imports or lifecycle.
  Any coupling to Phase 15 would recreate the architecture being replaced.

If implementation encounters an architecture, privacy, external-write, or
public-behavior ambiguity not resolved here, stop before dependent changes and request
a plan amendment. Do not weaken fencing, proof, privacy, or phase boundaries to make a
test pass.

## OpenQuestions

None.
