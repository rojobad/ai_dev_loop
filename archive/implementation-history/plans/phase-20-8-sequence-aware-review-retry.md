# Phase 20.8 — Sequence-Aware Manual Review Retry

## Goals

- Make the existing explicit `ai_dev_loop scheduler review retry <run-id>`
  recovery successor safe for the current phase of a sequence.
- Atomically adopt the authenticated successor as the current run for the same
  frozen ordinal so its accepted outcome can use the ordinary sequence handoff.
- Support more than one successive reviewer-recovery generation while preserving
  every terminal source run and the Phase 20.7 lineage.
- Preserve same-reviewer, same-worktree, same-staged-snapshot semantics and all
  Phase 20.5 capacity/retry behavior.
- Interoperate with, but do not reimplement, the Phase 20.6 fresh-review recovery
  workflow and its specialized Git/worktree integration authority.

## Non-Goals

- Do not retry automatically. Every new recovery generation requires the existing
  explicit authenticated `scheduler review retry` operator command.
- Do not make arbitrary run failures recoverable, restart Cursor, create a new
  Cursor chat, choose another reviewer, change model/reasoning, or copy an
  unauthenticated artifact.
- Do not replace Phase 20.6 `scheduler recovery prepare/start/status/abort` or its
  fresh-review managed-worktree path.
- Do not add a new public sequence-recovery command when the existing review-retry
  command expresses the operator decision.
- Do not change review ceilings, capacity-wait semantics, sequence phase order,
  checkpoint commit construction, finalization, or accepted outcomes.

## Scope

- Extend review-retry eligibility for a blocked source that is the authenticated
  current leaf of the current ordinal in one exact non-aborted sequence.
- Add a durable, typed sequence-attempt replacement intent/claim before successor
  creation or sequence reactivation.
- Materialize or idempotently reuse the existing review-recovery successor, bind
  it to the same sequence/ordinal and exact frozen context, append it to the Phase
  20.7 lineage, and compare-and-swap the sequence's current leaf.
- Reacquire or transfer the exact target-worktree reservation with no unowned gap
  before the successor becomes tick-eligible.
- Permit a sequence blocked for the allowlisted operational reviewer failure to
  re-enter active state only through this explicit content-bound transition.
- Make ordinary handoff/finalization consume the current accepted leaf rather
  than require the original planned run ID.
- Preserve idempotent command replay and support recovery of a later failed
  successor by targeting that successor directly.
- Add typed state/events, safe actions, schemas, migrations if required, focused
  tests, and truthful CLI/status text for this behavior.

## Out of Scope

- Fresh reviewer creation, managed recovery worktrees/private refs, patch seeding,
  automatic target integration, correction-delta application, and cleanup owned
  by Phase 20.6.
- Recovery of incomplete Cursor turns, integrity failures, arbitrary failed runs,
  older/non-current sequence ordinals, aborted sequences, terminal accepted
  phases, or worktrees that no longer match the authenticated staged snapshot.
- Git commits or ref mutations beyond the already-approved ordinary Phase 20.3
  handoff after the successor itself obtains an accepted review.
- `ai_dev_loop.yaml`, model defaults/catalogs, limit detection, global skills,
  hooks/bridges, timer installation, credentials, GitHub, PRs, pushes, and remotes.
- Editing the archived Phase 20.5, 20.6, or 20.7 plan artifacts.

## Required Context

Before editing, read:

- `AGENTS.md`, every `.cursor/rules/*.mdc`, and current relevant `/docs`.
- The committed Phase 20.5–20.7 implementations, plans, findings, schemas, and
  migrations.
- Phase 20.1.1 reviewer-recovery plan/findings, especially authenticated evidence,
  immutable source runs, replay order, same reviewer identity, exact copied
  artifacts, and multi-generation recovery.
- Phase 20.2 findings on lock lifetime, owned filesystem publication, concurrent
  replay, and crash adoption.
- Phase 20.3/20.4 handoff, reservation, abort, reconciliation, report, lifecycle,
  and fault-injection findings.
- `review_retry.py`, `review_recovery.py`, review checkpoint authentication,
  scheduler tick/effect/attempt services, safe actions, status/history, abort,
  and reservation services.
- Sequence domain/lifecycle/store APIs from Phase 20.7 plus sequence reconcile,
  handoff, materializer, checkpoint evidence, reservation, abort, status, and
  report services.
- Phase 20.6 recovery aggregate boundaries so this phase never treats a managed
  fresh-review recovery as an ordinary same-worktree retry without its owner's
  authorization.
- Existing Phase 20.1.1 and Phase 20 sequence tests, fake-agent fixtures,
  disposable repository helpers, and historical schema tests.

The clean execution baseline must contain the independently reviewed and
committed Phase 20.7 implementation.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local rules:
  `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned handoff/controller skills, planning skill, or staged-review skill.
- Use fake agents, injected clocks/IDs/failure hooks, temporary native-WSL XDG
  state, and disposable Git repositories. Never invoke real Cursor/Codex, the
  real timer, global integrations, credentials, hooks, signing, or remotes.
- Leave implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Eligibility and authenticated source

- Recoverability remains ledger/checkpoint/artifact driven. Never classify it
  from `last_error`, report prose, review Markdown, provider text, filenames, or
  user attestation alone.
- Authenticate the exact source completion envelope, attempt/effect identity,
  review failure kind, original reviewer binding, plan/prompt/effective config,
  sequence binding, repository identity, branch/HEAD, staged patch/tree, sandbox,
  and all mandatory protected artifact digests before creating a new successor.
- Integrity, identity, digest, path, symlink, dispatch, sandbox, repository, or
  binding contradictions are hard refusals even if capacity is exhausted.
- Copy/read only bounded manifest-authenticated artifacts required by the existing
  deterministic review continuation. A digest-bound missing artifact is an
  integrity failure, not an operational retry condition.
- Preserve the exact reviewer B and its frozen review model/reasoning. Do not
  resolve identity from a failed resume attempt, use `--last`, or bootstrap a new
  reviewer in this workflow.

### Explicit recovery and lineage

- The operator targets the current terminal leaf. The source run remains terminal
  and immutable; the successor is a new run in the same ordinal with generation
  `source + 1` and direct source linkage.
- The source must be the exact current leaf when first authorizing recovery.
  Older attempts, sibling successors, future ordinals, accepted leaves, aborted
  sequences, and unrelated blocked reasons are rejected.
- An idempotent replay must look up and fully authenticate an existing replacement
  intent/successor before re-running fresh eligibility. It returns the successor's
  actual current state and safe action, never a hard-coded stale action.
- A failed successor may be recovered only by explicitly targeting that current
  failed successor. Never skip a generation or fork the lineage.
- The successor keeps the same frozen entry hash and sequence ordinal. Recovery
  metadata is execution lineage and must not mutate the prepared definition.

### Atomic authority transition

- Persist a typed replacement intent/claim before successor publication or
  sequence reactivation. Bind exact sequence/version/ordinal, source generation
  and run, successor ID/generation, recovery key, frozen entry hash, repository
  identity, staged patch/tree, reviewer binding, reservation owner transition,
  and intended postcondition.
- Hold the sequence mutation lock and appropriate repository reservation/claim
  through successor insertion, lineage append, sequence CAS, and durable
  authorization. Do not release filesystem/sequence ownership before the ledger
  commit that makes the successor tick-eligible.
- If the blocked sequence previously released its reservation, reacquire it only
  after exact worktree/branch/HEAD/staged-patch cleanliness verification. Never
  steal another owner's reservation or normalize/repair drift.
- A crash or CAS loser must converge to exactly one authenticated successor and
  one current leaf. Unexpected/orphan state blocks safely; adoption requires exact
  intent ownership, never filename or ID-prefix heuristics.
- The sequence may leave `blocked` only through this named explicit transition.
  Do not generally make blocked terminal states mutable.

### Scheduler, handoff, and abort boundary

- Successor creation/rebinding is process-free. The next ordinary bounded tick
  launches the same reviewer continuation; the retry command itself launches no
  model and performs no checkpoint Git action.
- Only the current lineage leaf may launch, checkpoint, hand off, finalize, or be
  aborted. Superseded source runs cannot drive sequence reconciliation.
- Ordinary sequence handoff must authenticate the current leaf and accepted review
  while continuing to use the original frozen entry commit message and next
  planned entry. Never derive authority from the source run's terminal summary.
- `completed` and `completed_with_residual_risk` are advancing outcomes; the latter
  records the ordinal once without duplication. Findings, retry waits, capacity
  waits, max-iteration exhaustion, failures, and aborts do not advance.
- Preserve Phase 20.5 same-run capacity waiting/manual retry and Phase 20.6
  fresh-review ownership. Do not convert either into an implicit new successor.
- Abort intent wins before successor authorization. Once the successor is current,
  sequence abort targets that leaf and preserves staged content and artifacts.

### Persistence, observability, and privacy

- All state/events/intents are typed, versioned, atomically written, validated on
  read, and protected by complete compare-and-swap predicates.
- Authority queries are complete and unpaginated. Public status may show safe run
  prefixes, generation, source prefix, and next action but no prompts, patches,
  review/provider prose, raw JSONL, full session IDs, Git identity, or secrets.
- Repeated status/tick/retry replays must not append duplicate events, probe
  capacity, or launch agents when no state transition is required.

## Implementation Plan

1. Add regression characterization for standalone review retry, blocked sequence
   behavior, Phase 20.6 fresh recovery, and Phase 20.7 lineage before changing
   transitions.
2. Define the sequence replacement intent/claim and exact eligibility/result/safe-
   action contracts, schemas, lifecycle invariants, and store CAS operations.
3. Extend authenticated review-recovery analysis with sequence-current-leaf checks
   while preserving existing standalone and integrity-vs-operational behavior.
4. Implement idempotent existing-intent/successor lookup before fresh eligibility,
   including actual successor-state safe-action derivation and generation chains.
5. Implement the fenced transaction/critical section for reservation reacquisition
   or transfer, successor insertion, lineage append, sequence current-leaf CAS,
   and active-state restoration.
6. Adapt sequence reconcile/handoff/checkpoint evidence to use the current leaf
   and accepted leaf instead of equating the ordinal with `planned_run_id`.
7. Adapt sequence abort and direct run abort coordination so only the current leaf
   is targeted and no reservation/hold is released while ownership is uncertain.
8. Wire the existing CLI command and status/history/controller rendering with
   privacy-safe exact-ID results and honest idempotent safe actions.
9. Add deterministic failure injection and concurrency tests at every intent,
   insertion, reservation, CAS, commit, and replay boundary.
10. Write
   `archive/implementation-history/findings/phase-20-8-sequence-aware-review-retry.md`
   with exact validation evidence and residual risks.

## Testing Criteria

- **Eligibility tests:** exact current leaf succeeds; old generation, wrong ordinal,
  wrong sequence, accepted phase, aborted sequence, unsupported block, live effect,
  active process, hold, reservation conflict, repository drift, staged-patch drift,
  and every integrity contradiction fail closed.
- **Identity/artifact tests:** same reviewer B/model/reasoning/sandbox; bounded
  authenticated artifact copy; missing/tampered final response, attempt, event,
  envelope, binding, or patch is classified according to its mandatory contract;
  unbound legacy prose is never trusted.
- **Lineage tests:** one and several generations; direct-source chain; no forks,
  skips, cycles, duplicate IDs, or parallel active successors; immutable sources;
  frozen definition and entry hash unchanged.
- **Replay tests:** reissue before/after each durable boundary; active, waiting,
  capacity-waiting, completed, residual-risk, blocked, max-iteration, and aborted
  successor states return their real safe actions without duplicate effects.
- **Concurrency tests:** deterministic barriers prove competing retries passed the
  same initial read; exactly one intent/successor/current leaf wins; reservation
  claim rollback and CAS losers leave no runnable orphan.
- **Sequence tests:** accepted recovered non-final phase creates one ordinary
  checkpoint and next planned run; final recovered phase reaches ordinary
  `awaiting_finalization`; residual risk advances once; non-accepted outcomes do
  not advance; Phase 20.6 specialized recovery remains unchanged.
- **Abort tests:** abort before intent, during materialization, after rebind, during
  attempt, and before handoff; current leaf is targeted; no staged content,
  artifacts, holds, or unrelated reservations are lost.
- **CLI/integration tests:** use `CliRunner` or the real public CLI boundary, not
  direct service calls presented as CLI coverage. Assert complete durable rows,
  events, lineage, reservations, and safe output.
- **Privacy/no-growth tests:** repeated no-op ticks/status/retries add no ledger
  growth and leak no protected content or full identities.

All automated tests use fakes, injected synchronization, temporary XDG state,
and disposable repositories. No real agents, timers, accounts, credentials,
hooks, signing, or remote operations are permitted.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry_concurrency.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_phase20_3_handoff_recovery.py \
  tests/unit/scheduler/test_phase20_4_sequence_abort.py \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/integration/test_phase20_8_sequence_review_retry.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

Use actual committed Phase 20.5–20.7 test filenames when they differ and record
every exact command/result in the findings artifact.

## Risks Or Recovery Notes

- The highest-risk window is between creating the successor and making it the
  sole authorized current leaf. Durable intent, one ownership boundary, exact
  CAS, and crash replay must make that window convergent.
- A false capacity classification is intentionally recoverable under Phase 20.5,
  but it must not bypass authentication or create a successor automatically.
- Do not treat the specialized Phase 20.6 managed recovery run as an ordinary
  target-worktree run. Its aggregate remains the authority for its worktree,
  integration, and cleanup lifecycle.
- If the target worktree changed after the sequence released its reservation,
  explicit review retry must refuse safely. It must not reset, stage, or repair
  the user's tree.

## OpenQuestions

None.
