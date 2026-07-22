# Phase 16.6 -> Phase 16.7 Handoff

Date: 2026-07-21

## Executive status

Phase 16.6 added an isolated, constructor-injected Git/GitHub write and reconciliation
boundary for PR review v2:

- seven `MUTATING` effect executors (commit, push, create/update PR, request review,
  post reply, update PR text, resolve thread);
- seven matching `ReconcileWriteEffect` strategies with strategy-specific
  `APPLIED` / `PROVEN_NOT_APPLIED` / `UNRESOLVED` proofs;
- worker-supplied claim authority immediately before mutation;
- repository `FileLock` without holding SQLite transactions across process I/O;
- opaque self-binding public markers; content-bound PR/reply markers; commit trailer;
- durable pre-commit remote-ref baseline carried into push;
- ambiguous post-start outcomes become `WriteOutcomeUncertain` and must reconcile
  before another write;
- every typed result still enters state only through `complete_claim()` fencing.

No public CLI/config/daemon, Cursor/Codex/`LOCAL` adapters (including
`GeneratePublicationTextEffect`), SQLite schema/migration changes, or legacy PR/A-B
lifecycle changes were introduced. Phase 16.5 read modules remain mutation-free.

Boundary choice (clarifications): leave the entire `LOCAL` effect surface, including
`GeneratePublicationTextEffect`, to Phase 16.7. Integration tests traverse publication
effects only through validated fake results / protected fixtures.

## Entry baseline

- Branch: `rba/phase_16-6` atop Phase 16.5 merge `e2aa214`
- Plan commit present at `f5e79a8` (`docs: add phase 16.6 implementation plan`)
- Worktree carries the Phase 16.6 implementation as staged (not committed) changes
- Python (uv): 3.11.x; Pydantic 2.x

## Public API additions

Under `ai_dev_loop.pr_review_v2`:

| Module | Role |
|---|---|
| `application/write_contracts.py` | Frozen write policies, DTOs, markers, authority ports, proof enums |
| `application/write_reconciliation.py` | Pure uncertain/retry/block/proof helpers |
| `application/engine.py` | `PrReviewEngine.check_claim_authority()` (short read TX only) |
| `infrastructure/input_artifacts.py` | Hash-verifying patch/reply/commit-message/publication readers |
| `infrastructure/write_evidence_artifacts.py` | Content-addressed trigger evidence store |
| `infrastructure/git_write_transport.py` | Named Git ops; stdin commit; non-force push |
| `infrastructure/git_publication_gateway.py` | Commit/push + FIND_COMMIT_AT_HEAD / FIND_REMOTE_REF |
| `infrastructure/gh_write_transport.py` | Named REST/GraphQL mutations + reconcile reads (separate from 16.5) |
| `infrastructure/github_write_gateway.py` | PR/trigger/reply/text/resolve + FIND_* strategies |
| `workers/write_executor.py` | `WriteExecutor` for the seven mutating effects |
| `workers/reconcile_write_executor.py` | `ReconcileWriteExecutor` (`requires_authority=True`, ignores for reads) |
| `workers/effect_executor_router.py` | Classification router; rejects `LOCAL` |
| `workers/effect_worker.py` | Always passes authority/claim; `AuthorityLostError` → blocked fencing |

Package exports added: `GitWritePolicy`, `GitHubWritePolicy`, `WriteExecutor`,
`ReconcileWriteExecutor`, `EffectExecutorRouter`.

Domain amendments (additive, clarifications-authoritative):

- `CommitPatchEffect.expected_branch` (argv-safe)
- Upstream `SourceRunOrigin.head_branch` / `base_branch` and
  `PullRequestBinding.head_branch` / `base_branch` share the same argv-safe branch
  invariant so invalid source/existing-PR branches fail at DTO/state construction
  (and load) rather than during reducer/`complete_claim` publication success
- `CommitRecordedOutcome.expected_remote_sha_before_push` and
  `PushCommitEffect.expected_remote_sha_before_push` are **required-but-nullable**:
  omitted serialized fields fail closed; explicit `null` is the authoritative
  absent-ref baseline captured under the repository lock
- `opaque_public_marker` / `build_opaque_trigger_marker` → `adl-v1:{sha256(logical_identity)}`
  (no raw `run_id` in the public trigger marker)
- Content-bound PR/reply markers use parseable **`adl-v2`** public-safe evidence
  (operation, target, idempotency-key hash, canonical content hash)

## Marker formats

| Kind | Format | Binding |
|---|---|---|
| Trigger | `adl-v1:{sha256(logical review-trigger identity)}` wrapped as `<!-- … -->` | Stable logical identity only; Phase 16.5 matches the exact resulting marker |
| PR body / reply | `adl-v2:{target}:{operation}:{key_sha256}:{content_sha256}` wrapped as `<!-- … -->` | Operation + target + effect idempotency key hash + unmarked content hash (PR uses `canonicalize_publication_text(title, body)`) |
| Commit trailer | `ADL-Idempotency: {sha256(idempotency_key)}` | Idempotency key only; insufficient alone for APPLIED (needs parent/branch/patch) |

Overwrite of existing PR text is allowed only when an intact owned **v2** preimage
is present exactly once and title/body (or reply) hash verifies. Foreign, copied,
duplicate, tampered, or legacy `adl-v1` markers never authorize overwrite and
reconcile as `UNRESOLVED`.

## Artifact schemas

Protected input artifacts (hash-verified under hashed run roots):

- `ai_dev_loop.pr_review_v2.commit_message` v1 — `{subject, body}`
- `ai_dev_loop.pr_review_v2.publication_text` v1 — `{title, body}`
- reply text — UTF-8 bytes, no NUL, non-empty after strip
- patch — exact byte artifact for staged commit verification

Write evidence (content-addressed, collision-safe):

- Path pattern: `writes/triggers/<sha256>.json`
- Schema: `ai_dev_loop.pr_review_v2.trigger_evidence` v1 — marker, comment id,
  created_at, body_sha256, head_sha, pr_number, repository (no raw comment body)
- Modes: directories `0700`, files `0600` where chmod is supported

## Per-effect proof rules (implemented)

| Strategy | `APPLIED` | `PROVEN_NOT_APPLIED` | `UNRESOLVED` |
|---|---|---|---|
| commit at head | HEAD trailer + parent + branch + patch/tree | HEAD still expected, staged patch exact, lock held, and remote absent **or** equal/ancestor of expected head; `next_attempt_at` from approved backoff on original attempt | other HEAD / third-or-drifted remote SHA / mismatch / Git ambiguity |
| remote ref | remote equals intended commit on **`GitWritePolicy.remote_name`**; local validates | absent or still intended parent on the same configured remote; backoff `next_attempt_at` | other SHA / incomplete / remote mismatch |
| PR by head/base | unique open **same-repo** PR with SHA + text + marker; create proves head tip == `bound_head_sha` before authorize | complete absence (after head-branch ownership), or owned preimage safe to update; backoff `next_attempt_at` | multiple / fork or cross-repo candidate / human unowned / drift / incomplete ownership |
| review marker | exactly one exact opaque trigger marker + evidence; PR binding validated | complete comment pages prove absence; backoff `next_attempt_at` | duplicates / incomplete / binding incomplete |
| thread reply | one reply with marker + unmarked body hash; thread PR ownership; **every** comment/resolution page `data.node.id` must equal the requested thread ID | complete pages prove absence on an unresolved owned thread; backoff `next_attempt_at` | duplicates / content drift / resolved-without-match / wrong node id / missing PR ownership |
| PR text | title/body hash + owned marker; PR binding validated | owned preimage without intended content; backoff `next_attempt_at` | human unowned / conflict / incomplete ownership |
| thread resolved | exact owned thread is resolved; thread PR ownership; node id must match | exact owned thread present and unresolved; backoff `next_attempt_at` | missing / drift / wrong node id / missing PR ownership |

Absence requires complete bounded pagination and exact binding validation. Missing
thread `pullRequest` ownership fails closed. An already-resolved thread without a
matching owned reply must not authorize another reply (block on write; `UNRESOLVED`
on reconcile). Cross-repository / fork PR candidates are never silently filtered:
they block execution and reconcile as `UNRESOLVED` (including mixed same-repo + fork
lists).

`PROVEN_NOT_APPLIED.next_attempt_at` is always strictly later than the reconciliation
event `occurred_at`, derived from the approved retry/backoff policy using the original
write attempt. `complete_claim()` accepts that result into `waiting_retry` without an
immediate rewrite; at `max_attempts` the reducer pauses instead of scheduling a
seventh write.

## Authority and fencing

- Sole durable write intent remains the Phase 16.4 claimed outbox dispatch.
- Gateways do not open SQLite or invent a second intent log.
- Worker builds `ClaimAuthorityGuard` from `check_claim_authority` + claim snapshot.
- Authority is checked after repository lock acquisition and immediately before mutation.
- Commit normative order: local preflight → **authority** → durable remote baseline read
  → exactly one commit dispatch. Baseline failure remains a pre-mutation retry/block.
  The mutation-start flag is set only at actual process dispatch (not at authorize).
- `WriteExecutor` **propagates** `AuthorityLostError` (does not swallow to `EffectBlocked`);
  `EffectWorker` catches it and completes with `lease_authority_lost=True`.
- Lost authority / abort / replacement → zero writes; eventual result still offered once
  to `complete_claim()` for fencing.
- Idempotent preflight success does not call authorize and does not consume a write attempt.
- After authorization / mutation dispatch, timeout / connection loss / signal / HTTP 5xx /
  malformed or unconfirmed success envelopes / ordinary parser faults (`KeyError`,
  `ValueError`, `TypeError`, …) map to `AmbiguousWriteError` → `WriteOutcomeUncertain`,
  never an ordinary retry. `AuthorityLostError` still propagates. Only a complete
  structured rejection proving non-application may block or retry.
- Malformed GitHub **preflight** evidence (missing nested keys via `_dig`, invalid
  integers, …) raises typed `GhTransportError(MALFORMED_EVIDENCE)` before authorize →
  deterministic blocked execution; reconcile maps that class to `UNRESOLVED`. Raw
  `KeyError` must not escape nested GraphQL/REST walks.
- `CommitPatchEffect.expected_branch` and `PushCommitEffect.remote_ref` are argv-safe
  at normal DTO construction. Gateways still reject corrupted/`model_copy` payloads as
  typed `GitTransportError` blocks (not raw `ValueError`) so `WriteExecutor` /
  `complete_claim()` fencing cannot be bypassed. PR head/base branches, PR bindings,
  and configured remote names remain validated at the gateway/policy boundary before
  any transport call.
- `GitWritePolicy.remote_name` is the authoritative remote for baseline reads, push, and
  reconciliation; an explicit gateway override must match it exactly.
- Nonblocking repository `FileLock` contention maps to a typed pre-write transient
  (`EffectRetryableFailure`) in gateways and executors; no authority check and no
  mutation occur; the worker still fences the result through `complete_claim()`.
- GitHub mutations hold the repository `FileLock` from final preflight through authority,
  one write, and confirmation. Lock metadata persists only opaque
  `run:<sha256>` / `repo:<sha256>` identifiers (never raw run IDs or user repository
  paths) in lock files and contention summaries; no SQLite TX across I/O.
- Git write policy enforces `overall_timeout_seconds` with a **operation-local** monotonic
  deadline (context-var budget) that starts **before** the initial repository-root Git
  subprocess and continues through lock acquisition, preflight, mutation, and
  confirmation. A lock-contending call on a shared gateway must not clear or reset an
  active operation's budget. Each subprocess timeout is capped by remaining budget.
  Expiration before mutation is a safe transient; expiration after mutation dispatch is
  write uncertainty (`WriteOutcomeUncertain`).
- Git reconciliation under the same lock validates branch, HEAD, worktree/patch, local
  commit, remote identity/ref (including remote baseline ancestry for
  `PROVEN_NOT_APPLIED`), and proof-table evidence before `APPLIED` /
  `PROVEN_NOT_APPLIED`; otherwise `UNRESOLVED`.
- `gh` uses an explicit minimal allowlisted environment; an explicitly empty mapping is
  preserved. Subprocess helpers (`run_process` / `run_process_bytes` / streaming) run in
  a terminated-and-reaped process group.
- Artifact reads walk the run-relative path through directory descriptors
  (`O_NOFOLLOW` / `O_DIRECTORY`) and open the final file relative to the verified parent
  FD, then `fstat`/hard-cap/exact-byte hash; versioned text DTOs reject prohibited
  control characters; Git diffs use byte-oriented capture.

## Isolation evidence

- Architecture tests: no mutation in Phase 16.5 `gh_transport` / `github_read_gateway`;
  write transports expose no generic GraphQL/REST escape hatches; domain stays free of
  GitHub/subprocess/filesystem/SQLite; no v2 imports of legacy PR/config/runners/state;
  no production `LOCAL` / `GeneratePublicationTextEffect` executor in 16.6.
- Automated tests use fake `gh`/SSH runners and disposable local Git repos/bare remotes
  only. No real credentials, network, Cursor, Codex, or model activity.
- Privacy tests assert trigger evidence, logs, and safe errors omit raw bodies, patches,
  publication text, replies, commit messages, owner tokens, and full session IDs.

## Test inventory

New/updated Phase 16.6-focused unit + integration modules (dedicated write/reconcile
files, `test_phase16_6_*.py`, and correction regressions):

Unit: `test_write_contracts`, `test_write_authority`, `test_write_executor`,
`test_reconcile_write_executor`, `test_input_artifacts`, `test_git_write_transport`,
`test_git_publication_gateway`, `test_github_write_transport`,
`test_github_write_gateway`, `test_phase16_6_write_units`,
`test_phase16_6_correction_regressions`, `test_phase16_6_round3_regressions`,
`test_phase16_6_round4_regressions`, `test_phase16_6_round5_regressions`, plus
architecture/domain updates for argv-safe `expected_branch` / `remote_ref` and
matching upstream origin/binding branches, opaque markers, and v2 content-bound markers.

Integration: `test_phase16_6_git_writes`, `test_phase16_6_git_publication`,
`test_phase16_6_github_writes`, `test_phase16_6_reconciliation`,
`test_phase16_6_crash_restart_and_fencing`, `test_phase16_6_privacy_and_isolation`.

Fixtures: `tests/fixtures/pr_review_v2/github_writes/*.json` (sanitized).

Helpers: `tests/unit/pr_review_v2/github_write_helpers.py`, `write_helpers.py`.

Fault coverage includes pre-write reject/transient, post-authorize uncertainty for all
applicable mutation families (including malformed success envelopes and `KeyError`),
required-nullable remote SHA omission fail-closed, pre-create head-branch SHA check,
thread PR-ownership fail-closed, reply content-hash / tampered-marker rejection,
commit remote third-SHA reconcile, Git overall deadline from root inspection through
confirmation, competing local `FileLock` → typed retry (executor + worker fencing),
opaque lock metadata, gh allowlisted/empty env, descriptor parent-walk / nofollow /
binary artifact races, worker `lease_authority_lost=True` fencing, crash/restart →
exact one reconcile dispatch, applied / proven-not (backoff `next_attempt_at`) /
unresolved matrices, attempt-six pause, cross-repo/fork PR fail-closed, malformed
preflight parsers (including `_dig` missing nested keys), commit
authority→baseline→dispatch order, exact thread node IDs, argv-safe DTO + gateway
branch/ref rejection with zero transport and worker fencing, shared-gateway
operation-local deadline retention under lock contention, and `complete_claim`
fencing of stale tokens, uncertain outcomes, and proven-not-applied retries.

## Validation results

Focused Phase 16.6 write/reconcile + correction regressions (post fifth Codex
correction turn):

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2/test_*write* \
  tests/unit/pr_review_v2/test_phase16_6_*.py \
  tests/unit/pr_review_v2/test_git_publication_gateway.py \
  tests/unit/pr_review_v2/test_input_artifacts.py \
  tests/unit/test_process.py \
  tests/integration/test_phase16_6_*.py
=> 304 passed
```

Static / packaging:

```text
uv run python -m ruff format --check .
=> clean
uv run python -m ruff check .
=> All checks passed
uv run python -m mypy src
=> Success: no issues found in 106 source files
uv run mkdocs build --strict
=> built
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m build
=> sdist + wheel built (ai_dev_loop-0.1.0)
git diff --cached --check
=> clean
```

Full suite:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
=> 1524 passed
```

No skip/xfail/xpass was introduced by this phase. Collection includes write/reconcile
coverage and all Codex correction regression matrices.

## Honest boundary / residual risks

- Phase 16.6 does **not** claim real GitHub acceptance, credentialed `gh`, live SSH,
  daemon continuity, or end-to-end v2 publication.
- External exactly-once is evidence-based: markers + preflight + durable outbox intent
  + conservative reconciliation; unresolved evidence pauses.
- Abort fences local completion but cannot roll back a commit/push/GitHub mutation
  already accepted remotely; no compensating destructive work.
- Host crash while a child may still run requires restart reconciliation before any
  second write; active Git lock or changing evidence stays unresolved.
- `GeneratePublicationTextEffect` and all other `LOCAL` effects remain unimplemented
  production executors (router refuses them).
- No public `pr-review-v2` CLI, ProjectConfig wiring, or control-plane surface yet.
- Sanitized fake fixtures cannot prove live org/account/PR behavior; Phase 16.8 owns
  user-approved real acceptance.

## Phase 16.7 canonical ownership

The authoritative target is the Phase 16.7 section of
`pr-review-v2-restructure-context.md`: connect the pure domain, durable engine,
GitHub read/write boundaries, exact Codex reviewer session, and the extracted local
Cursor -> staging -> Codex loop into one complete **v2** orchestration. This is broader
than merely adding publication-text generation.

Phase 16.7 owns all of the following:

1. Creation from a completed A/B source run and preparation/adoption of an existing
   independent PR.
2. Explicit v2 application/control-plane entry points for
   `prepare` / `create` / `start` / `status` / `resume` / `abort`, with a non-mutating
   prepare boundary and an explicit write/start gate where applicable.
3. Production execution of all three current `LOCAL` effects:
   `GeneratePublicationTextEffect`, `AdjudicateThreadsEffect`, and
   `RunLocalFixEffect`.
4. Exact reviewer-session adjudication, frozen-thread-set verification, correction
   through the extracted local loop, local acceptance, publication of the accepted
   patch, the next external cycle, and verified no-findings completion.
5. Enforcement of external-cycle and local-iteration limits plus safe status,
   history, next-action, resume, and abort behavior.
6. A fully simulated multi-round end-to-end path that includes a local correction,
   publishes the new SHA, and completes without invoking legacy PR lifecycle or
   recovery code.

Public v2 CLI/config/control-plane wiring is therefore legitimate 16.7 work and must
be designed explicitly in its plan. It is **not** the Phase 16.9 cutover: legacy
commands and schemas remain available and unchanged until the dedicated cutover and
cleanup phase.

### Exact LOCAL adapter contracts

#### `GeneratePublicationTextEffect`

- Consume only its protected `evidence_ref` and `patch_ref` plus constructor-injected,
  frozen execution context. Do not recover raw prompt/session material from domain
  state or logs.
- Atomically write both required protected artifacts under the hashed v2 run root:
  `ai_dev_loop.pr_review_v2.publication_text` v1 (`title`, `body`) and
  `ai_dev_loop.pr_review_v2.commit_message` v1 (`subject`, `body`). Return their exact
  SHA-256-bound `ArtifactRef` values in `PublicationTextPreparedOutcome`.
- Match the Phase 16.6 reader constraints exactly: safe run-relative path, descriptor
  no-follow traversal, regular file, size/control/UTF-8 validation, `0700` directories,
  `0600` files where supported, and no raw text in events/status/logs/SQLite.
- A missing, malformed, mismatched, or partially published artifact must fail before
  `CommitPatchEffect` or any GitHub write can be emitted.

#### `AdjudicateThreadsEffect`

- Resume the **exact** frozen Codex reviewer session; never use `--last`, infer a
  replacement session, or create an implicit new review conversation.
- Bind the input to `binding`, `bound_head_sha`, `cycle_number`,
  `frozen_thread_ids`, `snapshot_ref`, and `execution_context_ref`; reject any drift
  before model execution and verify the returned decision set covers the frozen set
  exactly once.
- Map the validated result to `AdjudicationRecordedOutcome` / `AdjudicationEvidence`:
  all-actionable decisions require one protected `fix_prompt_ref` and forbid reply
  refs; any not-applicable/uncertain decision requires its protected `reply_ref` and
  forbids a local-fix prompt.
- Keep sensitive thread bodies and generated replies confined to protected artifacts;
  surface only safe summaries and hashes through ordinary operational output.

#### `RunLocalFixEffect`

- Adapt to `LocalReviewFixRequest` / `run_local_review_fix()` from
  `src/ai_dev_loop/local_review_loop.py`; do not import `LocalReviewOutcome` into the
  v2 domain and do not route through `legacy_pr_review_local_adapter.py`.
- The reusable local loop must remain GitHub-blind. The v2 caller owns frozen PR/thread
  validation before entry and external continuation only after the local boundary
  returns and local locks are released.
- Map `LocalReviewFixResult` to `LocalFixFinishedOutcome` exactly:
  accepted/accepted-with-residual-risk require a protected accepted patch ref, exact
  new head SHA, and result artifact; max-iterations/paused/failed require typed pause
  reason plus safe action; aborted carries no accepted patch or new head.
- Preserve the exact Cursor chat, reviewer session, tool runtime, run identity, prompt
  hashes, and monotonic local iteration numbering across resume.

### Orchestration and worker seams

- Extend `EffectExecutorRouter` with a constructor-injected `LocalEffectExecutor` (or
  equivalently narrow typed adapters); do not place Cursor/Codex/model calls in the
  reducer, SQLite store, GitHub gateway, or Phase 16.5 read transport.
- Every local result, including late/aborted/failed results, must still be offered to
  `complete_claim()` with the current completion token. Local execution does not need
  the mutation authority callback, but it does not bypass lease fencing, heartbeat,
  abort, or replacement checks.
- The sole durable workflow and effect intent remains the Phase 16.4 SQLite
  state/journal/outbox. Local runners may create protected artifacts but must not add
  a second state machine, intent log, or source of scheduling truth.
- Initial publication order remains publication text -> commit -> non-force push ->
  create/update PR -> review trigger. Fix publication remains accepted local patch ->
  publication text -> commit -> non-force push -> PR text update/replies/resolutions ->
  next review trigger, exactly as emitted by the reducer.
- Reuse Phase 16.5 reads and Phase 16.6 writes/reconciliation through their typed
  gateways. Do not add generic Git/REST/GraphQL escape hatches or direct external
  writes in command handlers.
- Preserve repository lock plus immediate pre-write authority, durable remote
  baseline, opaque/content-bound markers, no-force policy, write uncertainty, and
  strategy-specific reconciliation unchanged.

### Control-plane and coexistence boundary

- V2 prepare/create/adopt must validate and freeze repository, branches, head SHA,
  origin, execution context, exact reviewer/controller identity, limits, and artifact
  references before durable run creation or start.
- V2 start/resume must use the durable engine/outbox/worker protocol rather than call
  effects synchronously from CLI code. V2 abort must persist the abort event/request,
  fence late completion, and signal only clearly owned local child groups.
- Status/history output must come from v2 state/journal/outbox plus safe summaries and
  must expose lifecycle, current effect/classification/attempt, cycle/iteration limits,
  worker liveness, last safe error/result, and exact next safe action without prompts,
  raw bodies, patches, tokens, environments, or full session identifiers.
- Do not redirect the existing public `pr-review` lifecycle to v2 or delete Phase 15
  code in 16.7. Choose an explicit coexistence/namespace strategy in the 16.7 plan;
  final routing and legacy removal belong to 16.9.
- Avoid a SQLite migration unless an executable 16.7 invariant cannot be represented
  by the existing v2 snapshot/journal/outbox model. Any proposed schema change must be
  justified in the plan rather than introduced as adapter convenience.

### Decisions the Phase 16.7 plan must freeze

1. The temporary public command/namespace and configuration shape used before the
   Phase 16.9 cutover, including how it remains unmistakably separate from legacy.
2. The exact source-run and independent-PR preparation DTOs, validation order, and
   protected execution-context artifact schemas.
3. The concrete publication-text and external-adjudication runner boundaries, output
   schemas, model/runtime freezing, timeout/error mapping, and resume semantics.
4. The v2 adapter around `run_local_review_fix()`, including how a v2 run schedules a
   local run/chat without duplicating durable intent and how accepted artifacts are
   transferred or referenced safely between run roots.
5. Worker launch/supervision and ownership for read, local, mutating, and reconciling
   effects, including detached start/resume/abort and crash recovery entry points.

Do not guess these seams during implementation. If the current contracts cannot
represent one of them without weakening identity, privacy, fencing, or idempotency,
record an OpenQuestion and stop the dependent work.

### Required Phase 16.7 acceptance matrix

- Source-run creation and independent-PR adoption both enter the same v2 durable
  reducer/engine path with correctly frozen origins.
- Production executors cover all three `LOCAL` effect kinds and the router has no
  remaining supported-classification hole.
- Publication artifacts round-trip through the exact Phase 16.6 protected readers;
  missing/invalid/hash-drifted artifacts prevent all downstream writes.
- Adjudication uses the exact reviewer session and proves exact frozen-thread coverage;
  drift, duplicates, omissions, schema rejection, timeout, and late results fail safe.
- The local-fix adapter preserves the GitHub-blind local boundary, maps every terminal
  `LocalReviewFixResult`, honors local budgets, and resumes the same chat/session.
- A simulated initial publication reaches waiting-for-bot; a multi-round scenario
  freezes threads, adjudicates, performs and locally accepts a correction, publishes a
  new SHA without force, starts the next cycle, and completes on verified no-findings.
- Restart/resume/abort and two-worker fencing are exercised at every new LOCAL/control
  seam proportionally; systematic external fault injection and live GitHub acceptance
  remain Phase 16.8.
- Status/history/next-action output is complete and privacy-safe for running, waiting,
  retrying, paused, failed, aborted, uncertain/reconciling, and completed states.
- Phase 16.3–16.6 suites, the full Phase 16.1 A/B regression barrier, static checks,
  strict docs, package build, and full test suite remain green.
- No real GitHub credentials, external network, Cursor/Codex/model activity, or
  implementation-repository commit/push occurs in automated tests. Controlled live
  acceptance remains Phase 16.8.
