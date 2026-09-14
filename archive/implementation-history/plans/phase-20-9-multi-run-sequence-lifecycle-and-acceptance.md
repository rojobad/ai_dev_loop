# Phase 20.9 — Multi-Run Sequence Lifecycle and Acceptance

## Goals

- Complete and prove the end-to-end lifecycle of a linear sequence in which one
  or more phase ordinals contain multiple scheduler runs.
- Make reconciliation, handoff, abort, status, history, and completion reporting
  consistently follow the authenticated current/accepted run lineage.
- Prove interoperability between ordinary runs, Phase 20.8 same-reviewer retry
  successors, and Phase 20.6 fresh-review recovery results.
- Preserve advancement on both `completed` and
  `completed_with_residual_risk`, with residual risk visible through final
  reporting.
- Deliver deterministic crash/race coverage and truthful operator documentation
  before this functionality is used for another implementation sequence.

## Non-Goals

- Do not add another recovery mechanism, automatic retries, arbitrary run
  replacement, reviewer/model fallback, dynamic sequence mutation, branching,
  parallelism, skipping, reordering, or conditional phases.
- Do not broaden Git authority beyond Phase 20.3 ordinary non-final checkpoints
  and Phase 20.6 explicitly started authenticated fresh recovery.
- Do not automatically commit an ordinary final phase, push, fetch, create/update
  a PR, merge, deploy, or mutate remotes.
- Do not change review ceilings or classify new capacity/provider errors.
- Do not redesign the scheduler timer or global integration layer.

## Scope

- Centralize sequence lifecycle invariants around the Phase 20.7 attempt lineage
  and Phase 20.8 current-leaf transition.
- Ensure reconciliation discovers relevant current runs even when their terminal
  state is not ordinarily tick-eligible.
- Complete non-final checkpoint handoff and final-phase finalization using the
  accepted leaf while retaining the original frozen entry/commit message.
- Make sequence and direct-run abort cooperate with replacement intents, recovery
  aggregates, Git uncertainty holds, active processes, reservations, and the
  current leaf.
- Produce deterministic reports/status/history that show safe phase-level attempt
  counts, current/accepted run prefixes, recovery kinds, outcomes, residual risk,
  checkpoint commit prefixes, and remaining manual actions.
- Add a comprehensive hermetic end-to-end matrix, deterministic concurrency/fault
  injection, schema/read compatibility, public CLI coverage, documentation, and
  a Phase 20.9 findings artifact.
- Remove only obsolete internal compatibility branches proven unreachable on the
  Phase 20.9 schema; do not delete historical data, artifacts, migrations, or
  user-facing compatibility.

## Out of Scope

- `ai_dev_loop.yaml`, package-owned skills, planning/review skills, SessionStart
  hooks, desktop bridge, global skill installation, real systemd timer changes,
  credentials, account state, GitHub/PR APIs, remote Git actions, and deployment.
- Real Cursor/Codex executions or real recovery against this source repository as
  part of automated implementation or tests.
- Rewriting Phase 20.5–20.8 archived plans or findings.
- Cleanup of unowned, dirty, ambiguous, or diagnostically useful worktrees/refs.

## Required Context

Before editing, read:

- `AGENTS.md`, every `.cursor/rules/*.mdc`, and all current sequence/recovery
  documentation under `/docs`.
- The committed Phase 20.5–20.8 implementations, plans, findings, migrations,
  schemas, safe-action contracts, and focused test suites.
- Phase 20.1–20.4 plans/findings, including every reviewer correction involving
  artifact authentication, schema parity, concurrency, durable Git intent,
  reservation/abort fencing, reconciliation, and report publication.
- Review-budget extension plans/findings, especially authenticated authorization,
  unpaginated event lookup, current-state safe actions, deterministic concurrency,
  and real public-CLI coverage.
- Sequence prepare/start/materialize/handoff/checkpoint/reconcile/abort/report/
  status services; lifecycle validation; scheduler tick/status/history; direct
  run abort/retry; reservation/hold coordination; and Phase 20.6 recovery
  integration/cleanup services.
- Git checkpoint intent/evidence/result models and adapter, protected artifacts,
  SQLite store/migrations, JSON schemas, and historical read fixtures.
- All Phase 20 unit/integration tests and fake Cursor/Codex/disposable Git helpers.

The clean execution baseline must contain the independently reviewed and
committed Phase 20.8 implementation.

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
- Use fake agents, injected clocks/IDs/failure hooks, explicit synchronization,
  temporary native-WSL XDG homes, and disposable repositories/worktrees only.
  Never invoke real models, real provider/account state, the workstation timer,
  global integrations, credentials, user hooks/signing, or remotes.
- Leave implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### One lifecycle authority

- Centralized lifecycle validation is authoritative for every prepared, active,
  blocked, recovery-pending, abort-pending, aborted, awaiting-finalization, and
  Phase 20.6 recovery-integrated sequence state. Validate persisted reads and
  every transition.
- The frozen definition answers what was approved. Per-ordinal lineage answers
  which runs attempted it. The accepted leaf plus authenticated checkpoint or
  recovery-integration evidence answers what actually advanced it. Do not infer
  one layer from summaries in another.
- Only the current leaf may launch work or request a checkpoint. Only an
  authenticated accepted leaf may advance. Superseded runs remain immutable and
  cannot re-enter reconciliation.
- A phase ordinal advances at most once. Replays, competing ticks, competing
  recovery commands, and restarts cannot duplicate commits, residual-risk marks,
  reports, next-run materialization, reservations, or events.

### Reconciliation and irreversible effects

- Reconciliation selection must include terminal current leaves and pending
  sequence/recovery/Git/abort intents even when those runs are not tick-eligible.
- Persist durable intent/authorization before every irreversible Git or lifecycle
  side effect. Bind exact sequence/version/ordinal/lineage generation, source and
  accepted run, repository/worktree identities, parent/tree/patch/review hashes,
  commit identity/message, refs, reservation owner, and intended successor.
- Preserve explicit states for side effect `not_applied`, `applied`, and
  `unknown`. A failed or ambiguous observation never weakens an uncertainty hold.
- Disable all user/system Git hooks and signing for authorized commit paths. Use
  bounded direct argv, `shell=False`, explicit cwd, and recompute remaining time
  from one absolute deadline before every subprocess.
- Authenticate the real Git commit object, tree, parent, message, identity, target
  ref/HEAD, index, and worktree postcondition before clearing a hold or reporting
  success. Metadata surrounding an unchecked object is not proof.
- Publish reports/artifacts only from committed authoritative state. If an
  artifact cannot share the database transaction, publish after commit using
  atomic write-or-verify and reconcile missing publication idempotently.

### Abort, reservation, and cleanup

- Abort persists intent first, prevents new launches/transitions, targets the
  exact current leaf or Phase 20.6 recovery owner, and never rewrites/deletes
  repository content or reverts an applied commit.
- Holds and reservations survive until process/ref/checkpoint/recovery uncertainty
  is reconciled. Never release a sequence reservation merely because one source
  run became terminal if a successor, abort, or irreversible effect is pending.
- Avoid nested writer transactions and recursive service calls while a SQLite
  writer lock is held. CAS losers reload committed state and derive the next safe
  action outside conflicting critical sections.
- Cleanup remains exact-owned and last. Dirty, ambiguous, unregistered, mismatched,
  or diagnostically needed Phase 20.6 worktrees/refs are retained. No force,
  reset, clean, broad glob, or unresolved path deletion is allowed.

### Reporting, privacy, and compatibility

- Completion reports use committed validated ledger state and authenticated full
  hashes internally. Public output may shorten IDs/hashes only after validation.
- Reports retain every attempt generation and identify the accepted run/recovery
  kind for each phase, while distinguishing ordinary staged finalization from
  Phase 20.6 recovery-integrated completion.
- Status/history/controller output is stable, read-only, and performs no Git,
  artifact reauthentication, model call, or state repair merely to render.
- Do not expose prompts, patches, reviews, fix prompts, provider prose, raw JSONL,
  full session IDs, account data, Git identity, or absolute managed paths.
- Historical databases and sequence reports remain readable through explicit
  version-aware adapters. Do not require latest tables during old read-only opens
  or silently reinterpret older completion facts.

### Supported outcome matrix

- `completed` advances.
- `completed_with_residual_risk` advances and records the ordinal exactly once.
- Capacity wait, manual-review-retry wait, actionable findings, and active
  corrections keep the current phase active.
- `max_iterations_reached`, unsupported operational failure, and ordinary failed
  or blocked outcomes do not advance; they expose only their authenticated manual
  recovery/extension action.
- Sequence abort/aborted never advances. An abort that races a proven applied
  checkpoint/integration records the immutable applied fact before terminalizing
  remaining sequence work; it never rolls Git back.
- An ordinary accepted final phase remains staged in `awaiting_finalization`.
  A Phase 20.6 explicitly authorized recovered final phase retains its distinct
  recovery-integrated terminal result.

## Implementation Plan

1. Build a lifecycle transition table for every sequence state, current-run state,
   recovery kind, accepted outcome, pending intent, hold, and abort combination;
   encode it in centralized validators and table-driven tests.
2. Refactor sequence reconciliation discovery to include terminal current leaves
   and all pending durable intents without repeatedly growing the ledger.
3. Complete ordinary checkpoint/handoff/finalization against the accepted lineage
   leaf, preserving exact Phase 20.3 Git evidence and Phase 20.4 materialization/
   reservation guarantees.
4. Complete interoperability projections for Phase 20.6 fresh recovery and Phase
   20.8 same-reviewer successors without creating a second integration authority.
5. Unify sequence/direct-run/recovery abort coordination around durable intent,
   current ownership, process/ref holds, CAS replay, and exact terminal outcomes.
6. Rebuild status, history, controller projections, and completion reports from
   committed validated state, with safe attempt/accepted-run visibility and
   idempotent post-commit artifact publication.
7. Add deterministic failure injection before/after every durable transition and
   irreversible boundary; add synchronized race tests for tick/retry/abort/
   handoff/recovery/cleanup competitors.
8. Add public CLI and full end-to-end suites covering two- and four-phase
   sequences, multiple recovered ordinals, repeated generations, residual risk,
   finalization, Phase 20.6 recovery, restarts, conflicts, and privacy.
9. Update current CLI reference, workflow, troubleshooting, observability, and
   sequence documentation. State exact manual actions and that no remote action
   occurs.
10. Write
   `archive/implementation-history/findings/phase-20-9-multi-run-sequence-lifecycle-and-acceptance.md`
   with exact tests, fault matrix, unperformed real operations, and residual risks.

## Testing Criteria

- **Lifecycle matrix:** every legal and illegal aggregate combination is table-
  tested; validators run on persisted reads; terminal transitions and accepted
  leaf consistency fail closed.
- **End-to-end sequences:** no recovery; one same-reviewer recovery; two or more
  generations in one ordinal; recoveries in multiple ordinals; Phase 20.6 fresh
  recovery; mixed recovery kinds; completed and residual-risk outcomes; ordinary
  final staged state; specialized recovery-integrated final state.
- **Checkpoint evidence:** actual staged tree, patch, review result, commit object,
  parent, message, identity, branch/ref/HEAD, successor, reservation, and sequence
  versions are independently authenticated; tampering each field blocks.
- **Crash matrix:** failures before/after intents, attempt adoption, reservation
  transfer, commit-tree, ref CAS, post-CAS observation, state CAS, next materialize,
  report publication, abort terminalization, and cleanup converge after restart.
- **Uncertainty tests:** ambiguous ref/process observations retain strong holds;
  proven not-applied effects clear/reconcile correctly; proven applied effects
  finish ledger/report/abort work without a second operator command.
- **Concurrency tests:** explicit barriers—not sleeps—prove the intended race
  window for competing ticks, retries, aborts, handoffs, and Phase 20.6 recovery;
  complete rows/events/reservations are asserted.
- **CLI tests:** exercise commands through `CliRunner`/public entrypoint, including
  JSON and human status/history/report output and actual safe-next-action replay.
  Do not count direct service tests as CLI coverage.
- **Compatibility tests:** read-only historical schemas, migrated Phase 20.1–20.6
  data, old one-run reports, and current multi-run reports; no query relies on a
  first-500/display pagination limit.
- **Privacy tests:** every error/status/history/report/event/log path excludes all
  protected contents and full sensitive identities.
- **Regression tests:** Phase 20.5 capacity behavior, Phase 20.6 standalone and
  sequence recovery, review-budget extension, ordinary standalone runs, timer
  budgets, abort process control, package build, and documentation remain green.

All automated tests are hermetic. No test may invoke real models, real account
telemetry, this repository's mutation path, the real timer, user hooks/signing,
global installation, or remotes.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_9_multi_run_lifecycle.py \
  tests/unit/scheduler/test_phase20_9_multi_run_reconciliation.py \
  tests/unit/scheduler/test_phase20_9_multi_run_abort.py \
  tests/unit/scheduler/test_phase20_9_multi_run_status_report.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

Use the actual committed Phase 20.5–20.8 focused filenames where they differ and
record exact commands, counts, skips, failures, and environmental residual risks
in the findings artifact. A conditionally missing fixture is a test failure, not
a permitted silent skip.

## Risks Or Recovery Notes

- Cross-resource atomicity remains the primary risk: SQLite, protected artifacts,
  Git refs, indexes, worktrees, processes, and reports cannot share one transaction.
  Durable intent plus exact postcondition reconciliation is mandatory.
- A report is not authority. If report publication disagrees with committed state,
  preserve the committed state, regenerate the exact report idempotently, and
  never reverse a proven Git result.
- Comprehensive acceptance can become superficial if it asserts only final state.
  Tests must prove intermediate durable rows, ownership, holds, events, and exact
  side-effect counts at controlled boundaries.
- Do not remove retained Phase 20.6 worktrees/refs merely to make cleanup tests
  pass. Ambiguous or dirty owned resources remain a visible manual recovery case.

## OpenQuestions

None.
