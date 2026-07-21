# Phase 16.5 -> Phase 16.6 Handoff

Date: 2026-07-21

## Executive status

Phase 16.5 added an isolated, read-only GitHub observation boundary for PR review v2:
typed `gh api`/GraphQL transport, identity/trigger/thread/no-findings validation,
constructor-injected read policy and backoff, protected sanitized artifacts, an
`ObserveBotReviewEffect`-only executor, and bounded in-flight lease renewal that still
submits every typed result through `complete_claim()`.

No GitHub/Git writes, public CLI/daemon, Cursor/Codex adapters, ProjectConfig wiring,
SQLite schema changes, or legacy PR/A-B lifecycle changes were introduced.

Correction turns after the initial staged slice also:

- made observation artifacts content-addressed and collision-safe;
- tightened transport failure classification (rate-limit before ordinary 403; `gh` exit 4;
  allowlisted network failures without raw stderr);
- replaced unbounded `gh --paginate` reactions with serial page-by-page REST GETs and
  enforced overall-deadline-capped per-call timeouts on every remote read;
- completed the event-driven worker lease-renewal safety matrix.

### Independent implementation/review run

The detached A/B implementation run
`ai-dev-loop-20260721T190029Z-11dbff` completed all 5/5 review turns. Review findings
decreased from 5 -> 3 -> 3 -> 1 -> 0. The fifth review reported no actionable
findings.

The orchestrator disposition was `completed_with_residual_risk` only because the final
reviewer recorded `tests_status=blocked_environment`: focused validation was available
to that review, while broader revalidation was partially blocked by its sandbox. The
implementation executor's final staged validation recorded 383 focused and 1211 full
tests passing, plus clean static, docs, and packaging checks as listed below. There is
no known actionable implementation finding to carry into Phase 16.6.

## Entry baseline

- Branch: `main` at merge commit `31ca91f` (Phase 16.4)
- Worktree started clean aside from the approved Phase 16.5 plan file
- Python (uv): 3.11.x; Pydantic 2.x

Expected Phase 16.6 entry condition, per the user handoff instruction:

- Phase 16.5 has been accepted and incorporated into the branch ancestry;
- the next agent receives a clean worktree and clean index; and
- the clean branch/HEAD presented to that agent is authoritative over this historical
  staged-run snapshot.

Do not copy, reapply, or reconstruct the Phase 16.5 staged diff when those entry
conditions hold. Re-run the focused baseline from the clean branch before beginning
Phase 16.6.

## Public API additions

Under `ai_dev_loop.pr_review_v2`:

| Module | Role |
|---|---|
| `application/github_read.py` | `GitHubReadPolicy`, backoff calculator, observation DTOs, error mapping |
| `infrastructure/gh_transport.py` | Direct argv typed read transport (`identity` / comment / thread / reaction pages) |
| `infrastructure/github_read_gateway.py` | Complete read-only observation + fail-closed matchers |
| `infrastructure/review_artifacts.py` | Atomic protected observation artifact store |
| `infrastructure/paths.py` | Safe hashed run artifact roots (additive helpers) |
| `workers/github_read_executor.py` | `ObserveBotReviewEffect`-only `EffectExecutor` |
| `workers/effect_worker.py` | Bounded lease renewal coordinator around external reads |

Transport public methods (no arbitrary `graphql` / `rest_get`):

- `fetch_pull_request_identity`
- `fetch_issue_comments_page`
- `fetch_review_threads_page`
- `fetch_issue_comment_reactions_page`

Domain vocabulary additions (additive only):

- `PauseReasonKind.NOT_FOUND`, `PauseReasonKind.BRANCH_DRIFT`
- `TransientErrorKind.HTTP_500`, `TransientErrorKind.OTHER_HTTP_5XX`

Engine introspection (read-only):

- `PrReviewEngine.lease_ttl`
- `PrReviewEngine.clock`

Default reference policy values (constructor-injected, not `ProjectConfig`):

- reviewer login: `chatgpt-codex-connector`
- poll interval: 60s
- GitHub attempts/batch: 6 (domain-fixed)
- local retry delays: 10/30/90/180/300s with injectable +/-20% jitter
- max server-directed wait: 3600s (absolute ceiling; injected values above 3600 are clamped)
- no-findings detection: disabled unless prefixes are supplied
- pagination bounds: `max_pages` / `max_items`
- overall observation deadline + per-call timeout; each process call uses
  `min(per_call_timeout, remaining_overall_deadline)`

## Error / backoff mapping

- Permanent blocks: auth, permissions, not-found, closed PR, fork, repository/head/branch
  drift, HTTP 422, missing `gh`, malformed/contradictory evidence, artifact failures
- Transient retries: timeout, network family, HTTP 429, primary/secondary rate limit,
  HTTP 500/502/503/504, other structured 5xx, temporary CLI failure
- Rate-limit evidence (headers / GraphQL body) wins over ordinary 403 permissions
- `gh` exit code 4 without an HTTP envelope maps to authentication
- Precedence for eligibility: valid `Retry-After` > exhausted `X-RateLimit-Reset` >
  local jittered delay; server-directed waits are never jittered and are clamped to 1h
- Polling success (`BotStillWaitingOutcome`) never consumes the transient failure budget
- Attempt 6 transient failure emits schema-valid `EffectRetryableFailure`; reducer pauses
  with `RETRY_EXHAUSTED` and creates no seventh timer/dispatch

## Artifacts

- Root: `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/runs/<sha256(run_id)>/`
- Observation path pattern (content-addressed):
  `observations/cycle-NN/poll-MMMM/<sha256>.json`
- Competing claimants for the same cycle/poll write distinct content-addressed objects;
  an accepted `ArtifactRef` remains immutable and hash-verifiable
- Modes: directories `0700`, files `0600` where chmod is supported
- Persist only sanitized content + hashes/metadata; raw GitHub bodies remain ephemeral
- SQLite continues to store only `ArtifactRef` path/hash and typed safe summaries

## Pagination and deadlines

- GraphQL comments/threads and REST reactions paginate serially with cursor/page progress,
  `max_pages`, `max_items`, duplicate-ID rejection, and completeness checks
- Reactions use explicit `repos/{owner}/{name}/.../reactions?per_page=&page=` GET pages;
  no `--paginate` and no ambient `{owner}/{repo}` placeholders
- Exhausted overall deadline raises the typed timeout transient before another remote call

## Lease behavior

- Default lease TTL remains 30s; default heartbeat interval is 10s (injectable)
- Renewal uses independent short engine heartbeats and never holds a SQLite transaction
  across network I/O
- Stop/join is deterministic after executor return/error via a stoppable `IntervalWait`
  contract (`wait` + `wake_for_stop`); `stop()` must not discard a still-live thread
- Lost heartbeat / abort / replacement generation still offers the eventual typed result
  once to `complete_claim()` for fencing; no second external call after lease loss
- Heartbeat exceptions durably relinquish the original lease when possible, and always
  set ``lease_authority_lost`` on the eventual ``complete_claim()`` offer so fencing is
  ``STALE``/``REJECTED`` even if best-effort ``release_lease()`` itself fails
- Event-driven unit coverage: heartbeat rejection, heartbeat exception (including
  release-also-fails), abort during blocked fake read, lease replacement + late return,
  executor exception, short execution, bounded stop/join, and the
  no-second-external-call invariant

## Isolation evidence

- Architecture tests forbid v2 imports of legacy PR/config/runner/state/lifecycle modules
- Architecture tests forbid GraphQL `mutation(` / REST write methods in the v2 read path
- Architecture tests require the typed read-only transport public surface
- Domain remains free of GitHub/subprocess/filesystem/SQLite
- Automated tests use fake transports/process runners only; no real `gh`, credentials,
  network, Cursor, Codex, or model activity

## Validation results

Validation against the final corrected staged state (2026-07-21):

Focused Phase 16.5 + 16.4 + domain + A/B barrier:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2 \
  tests/integration/test_phase16_4_*.py \
  tests/integration/test_phase16_5_*.py \
  tests/integration/test_phase16_1_ab_regression_barrier.py
=> 383 passed
```

Static / packaging:

```text
uv run python -m ruff format --check .
=> clean
uv run python -m ruff check .
=> All checks passed
uv run python -m mypy src
=> Success: no issues found in 95 source files
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
=> 1211 tests collected
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
=> 1211 passed
```

Earlier intermediate handoff counts from pre-correction staging are superseded by this
section and must not be treated as authoritative for the final staged tree.

## Honest boundary / residual risks

- Phase 16.5 does **not** claim real GitHub acceptance, daemon continuity, or end-to-end
  v2 completion.
- No public `pr-review-v2` CLI, config surface, or write gateway exists yet.
- `ReconcileWriteEffect`, mutating GitHub writes, and local review adapters remain
  Phase 16.6 / 16.7.
- Sanitized fake fixtures cannot prove live org/account/PR behavior; Phase 16.8 owns
  user-approved real acceptance.
- Crash after artifact write before `complete_claim` can leave an orphaned identical
  content-addressed artifact; retry can reuse the same hash without broad cleanup.
- The final independent review could not repeat every broad validation command inside
  its sandbox. This is retained as provenance, not as a known code defect; the executor
  evidence above is the current final validation record.

## Phase 16.6 ownership

- Implement production executors for the existing `MUTATING` effects:
  `CommitPatchEffect`, `PushCommitEffect`, `CreateOrUpdatePrEffect`,
  `RequestBotReviewEffect`, `PostThreadReplyEffect`, `UpdatePrTextEffect`, and
  `ResolveThreadEffect`.
- Implement `ReconcileWriteEffect` strategy routing for the matching existing
  reconciliation kinds: commit-at-head, remote-ref, PR-by-head/base, review marker,
  thread reply, PR text, and thread resolution.
- Preserve the Phase 16.4 intent/outbox rule. The claimed durable dispatch is the
  persisted write intent; a gateway must not add a second database or shadow intent
  log.
- Every confirmed result or uncertain outcome enters through `complete_claim()` with
  the original claim token and all existing fences. Never call `apply_event()` for an
  effect result and never mutate SQLite directly from a gateway.
- A timeout, connection loss, lost lease authority, or crash after a write may mean the
  write was applied. Do not emit a normal retry. Route to `WriteOutcomeUncertain` and
  reconcile before another write attempt.
- Preserve stable effect/idempotency/reconciliation identities across crashes and
  explicit resume batches. Strategy-specific proof must distinguish `APPLIED`,
  `PROVEN_NOT_APPLIED`, and `UNRESOLVED`; absence of weak evidence is not proof.
- Reuse only the narrow typed read primitives from Phase 16.5 where they prove a write
  outcome. Do not widen the public transport to arbitrary GraphQL/REST and do not fold
  `ObserveBotReviewEffect` polling into reconciliation.
- Keep direct argv, explicit cwd/timeouts, separated stdout/stderr, redaction, protected
  artifacts, branch/repository/full-SHA checks, no-force policy, and bounded lease
  renewal. Lost lease authority must fence completion even if best-effort lease release
  also fails.
- Fault-inject before process start, after durable claim, during each external/local
  write, after remote/local success but before completion commit, and during
  reconciliation. Replaying any case must not duplicate commits, pushes, PRs, trigger
  comments, replies, PR text updates, or thread resolutions.
- Keep public CLI/config/daemon wiring, end-to-end orchestration, local Cursor/Codex
  adjudication/fix adapters, and legacy cutover out of Phase 16.6.
- Automated tests use fake Git/GitHub processes and temporary repositories only. Real
  credentials, real network, real PR writes, Cursor, Codex, and model calls remain
  prohibited until the later controlled acceptance phase.

### Phase-boundary decision the 16.6 plan must make explicitly

The restructuring context lists publication-text generation in Phase 16.6, while the
current domain classifies `GeneratePublicationTextEffect` as `LOCAL` and the approved
16.5 boundary reserves Cursor/Codex/local adapters for Phase 16.7. The Phase 16.6 plan
must explicitly choose one of these bounded interpretations before implementation:

1. implement only a deterministic, non-model publication-text materializer needed by
   the write gateway; or
2. leave the entire `LOCAL` effect for Phase 16.7.

Do not silently reclassify the effect, import Cursor/Codex activity, or broaden Phase
16.6 while resolving this boundary.

### Suggested Phase 16.6 acceptance matrix

- success, deterministic rejection, timeout, connection loss, process crash, late
  result, abort, and lease-generation replacement for every mutating effect;
- unknown-outcome recovery through the exact strategy-specific reconciliation effect;
- two workers/restarts cannot duplicate a write or apply a stale completion;
- stable identity across retry/resume with distinct durable dispatch occurrences;
- branch, repository, full head SHA, patch/artifact hash, PR, and thread drift fail
  closed before mutation;
- commit/push uses direct Git argv, expected HEAD, the recorded branch, no force, and no
  cleanup/reset/stash/merge;
- GitHub mutations use predefined operations only and verify returned resource identity;
- fake executors drive every `APPLIED`, `PROVEN_NOT_APPLIED`, and `UNRESOLVED` path;
- logs, events, SQLite, status, and artifacts expose no credentials, raw prompts,
  review bodies, patches, or session identifiers; and
- retain all Phase 16.3 domain, Phase 16.4 durability, Phase 16.5 read/lease/privacy,
  and Phase 16.1 A/B regression suites.
