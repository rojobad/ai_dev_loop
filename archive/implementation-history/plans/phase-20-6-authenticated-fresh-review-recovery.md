# Phase 20.6 — Authenticated Fresh-Review Recovery and Automatic Integration

## Goals

- Add a first-class recovery workflow for a scheduler run whose Cursor work and
  staged snapshot completed, but whose existing reviewer continuation cannot be
  used safely or successfully.
- Replace the manual fallback of creating a clean Git worktree and teaching
  Cursor to copy an existing patch with a deterministic, authenticated workflow:
  freeze recovery evidence, create an isolated managed worktree, seed the exact
  protected patch without an initial Cursor turn, obtain a fresh Codex review,
  run normal bounded Cursor corrections only when findings require them, and
  integrate the accepted tree automatically.
- Make `scheduler recovery start` the single explicit operator authorization for
  all local effects in that recovery: managed worktree/private-ref creation,
  patch seeding, fresh review, bounded corrections, exact local commit, target
  branch compare-and-swap, sequence continuation when applicable, and safe
  cleanup.
- Advance on both `completed` and `completed_with_residual_risk`, preserving the
  latter visibly in recovery and sequence reporting.
- Reuse the reviewed-tree, commit-object, reservation-handoff, abort, fencing,
  reconciliation, and sequence materialization guarantees implemented in Phases
  20.3 and 20.4 instead of creating a second weaker Git path.

## Non-Goals

- Do not recover arbitrary dirty worktrees, incomplete Cursor turns, unstaged or
  untracked salvage, hand-authored patch files, unauthenticated review content,
  or repository state that cannot be tied to a protected staged snapshot.
- Do not infer that recovery is appropriate. The operator explicitly chooses a
  source run and authorizes the prepared recovery.
- Do not rebase, merge, cherry-pick, resolve conflicts, reset, clean, stash,
  checkout over user work, force-update refs, or overwrite target drift.
- Do not retry or reuse the failed reviewer, copy its transcript, guess a
  session ID, use `--last`, or trust an unbound historical Cursor final response.
- Do not rerun Cursor before the seeded patch receives its first fresh review.
- Do not push, fetch, create/update a PR, merge, tag, sign commits, invoke Git
  hooks, mutate remotes, or perform production deployment.
- Do not make ordinary standalone runs or ordinary final sequence phases commit
  automatically. Automatic final integration is authorized only by this
  recovery workflow's separately frozen intent and explicit `recovery start`.
- Do not add conditional, parallel, cross-repository, reordered, skipped, or
  dynamically extended phase sequences.
- Do not modify Phase 20.5 Codex limit classification or capacity transport.

## Scope

- Add a typed, versioned recovery aggregate with prepared, active,
  integration-pending, cleanup-pending, blocked, abort-pending, aborted, and
  integrated outcomes, plus immutable lineage to the source run and any source
  sequence entry.
- Add the public commands:
  - `ai_dev_loop scheduler recovery prepare <source-run-id>`;
  - `ai_dev_loop scheduler recovery start <recovery-id>`;
  - `ai_dev_loop scheduler recovery status <recovery-id>`;
  - `ai_dev_loop scheduler recovery abort <recovery-id>`.
- Make prepare process-free and Git-read-only. It authenticates the source
  checkpoint, freezes all inputs and integration policy, assigns deterministic
  recovery/run/worktree identities, and performs no worktree/ref/index mutation.
- Support only sources with a completed Cursor turn and authenticated non-empty
  staged patch whose next technical action is a fresh review. The source may be
  standalone or the current materialized run of a blocked linear sequence.
- Create an isolated package-managed worktree and private recovery branch at the
  exact frozen parent HEAD after start, admit it, and apply the authenticated
  source patch to its index/worktree with bounded direct Git commands.
- Seed the new scheduler run directly at the first Codex review boundary. Use a
  fresh reviewer with the source-frozen model/reasoning and review skill. If the
  review returns findings, lazily create one fresh Cursor chat and continue the
  ordinary bounded correction loop using exact Codex-authored fix prompts.
- After an accepted review, create one unsigned/no-hook commit containing the
  exact accepted tree; reconcile any correction delta into the original target
  worktree only while its frozen source tree remains exact; and compare-and-swap
  the target branch from the frozen parent to that commit.
- For a recovered non-final sequence phase, atomically record the replacement
  review/commit, preserve residual risk, materialize and authorize the next
  frozen entry, and transfer target reservation ownership without an unowned
  gap. For a recovered final sequence phase, record a recovery-integrated
  terminal sequence result because the explicit recovery policy already created
  the final local commit. For a standalone source, mark the recovery integrated
  and release reservations.
- Remove only the exact clean package-owned recovery worktree and exact private
  ref after durable target integration. Retain them for diagnosis whenever
  identity, cleanliness, reachability, or reconciliation is uncertain.
- Add migrations, schemas, artifacts, status/safe actions, documentation,
  privacy protections, failure injection, and Phase 20.6 findings.

## Out of Scope

- Source runs that are still active, hold a live attempt/effect/capacity claim,
  remain in `waiting_codex_review_retry`, or require an explicit abort before
  their reservation can be released safely.
- Sources without authenticated repository identity, frozen plan/prompt/config,
  completed Cursor/staging events, protected staged-patch bytes/hash, or an exact
  target worktree match.
- Recovering an old/non-current sequence entry, reopening an aborted sequence,
  or rewriting a terminal source run. A sequence recovery may replace only its
  current blocked phase through explicit lineage; the source run stays immutable.
- Recovering integrity failures involving digest, path, dispatch, effect,
  repository, reviewer-binding, sandbox, schema, or artifact contradictions.
  Missing optional/unbound Cursor final text is not used and is therefore not an
  integration input; missing mandatory source evidence remains a hard refusal.
- User-selected target branches, alternate repositories, rebases onto newer
  HEADs, arbitrary commit messages for sequence entries, or model changes during
  recovery. Standalone recovery requires one explicit bounded commit message at
  prepare; sequence recovery uses the entry's already-frozen message.
- Changes to `ai_dev_loop.yaml`, model defaults/catalogs, package-owned Codex
  skills, SessionStart hooks/bridges, global installation, credentials, timer
  installation/enablement, GitHub/PR workflows, or remote publication.
- Real model, provider-account, systemd, hook, signing, or remote-Git activity in
  automated tests.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file, especially the narrow Phase
  20.3 commit exception, non-destructive abort rules, exact staged-review
  contract, and initial empty-index admission rule.
- Phase 20.1.1 plan/findings and current `scheduler review retry` implementation,
  including the deliberate rejection of historical unbound Cursor final text.
- Phase 20.1–20.4 plans/findings, sequence definitions/materialization/start,
  reviewed checkpoint intent/evidence, Git adapter, atomic handoff, blocked and
  abort lifecycle, completion report, and status contracts.
- Phase 20.5 plan and, when available on the execution baseline, its findings and
  implementation. Preserve its operational-vs-integrity failure classification,
  fresh capacity transport, same-reviewer retry behavior, and no-loop guard.
- Scheduler run state/events/reducers/contracts, attempts/effects, Cursor/Codex
  workflow services, staging/fingerprints, review-result validation, reservations,
  abort reconciliation, safe actions, controller status, history, and timeline.
- `scheduler/application/review_recovery.py`, `review_retry.py`,
  `review_checkpoint_verify.py`, `git_checkpoint.py`, `sequence_handoff.py`,
  `sequence_reconcile.py`, `sequence_reservation.py`, `sequence_materializer.py`,
  `sequence_report.py`, `scheduler_checkpoint.py`, and `tick.py`.
- Scheduler domain checkpoint/sequence/state/event models, SQLite migrations and
  store operations, protected artifact/path helpers, Git runners, CLI command
  rendering, schemas, and current documentation.
- Phase 20.1.1/20.3/20.4 unit and integration tests, fake Cursor/Codex fixtures,
  disposable Git repository helpers, restart/abort/privacy suites, and historical
  schema read-only tests.

The execution baseline must contain the independently reviewed and committed
Phase 20.5 implementation. The Phase 20.5 and Phase 20.6 plans are intended to
be executed as one prepared sequence, with Phase 20.5 as the non-final entry and
Phase 20.6 as the final entry.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local Cursor rule:
  `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- Update the affected repository-local rules with one precise Phase 20.6
  exception for an explicitly started authenticated recovery. Do not weaken the
  prohibitions for normal runs, ordinary final sequence phases, arbitrary Git
  writes, remotes, destructive commands, or agent-authored commits.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned controller/handoff skills, the planning skill, or the staged
  review skill.
- Use fake agents, injected clocks/IDs/ports/failure hooks, temporary XDG/config
  homes, and disposable Git repositories/worktrees only. Never invoke real
  Cursor/Codex, a real capacity probe, the real scheduler timer, user hooks,
  signing, remotes, or this source repository's recovery Git mutation path.
- Leave all implementation changes unstaged and uncommitted for independent
  review. Because this phase broadens local Git authority and cleanup authority,
  the governance diff and final behavior require explicit human acceptance after
  the automated review.

## Architecture Guardrails

### Recovery definition and explicit authorization

- `recovery prepare` is read-only to every Git repository and process-free. It
  may write only its central ledger rows and protected XDG artifacts. It must not
  reserve the target, create a worktree/ref, apply/stage content, or launch an
  agent.
- Freeze an immutable recovery definition containing the source run and optional
  source sequence/ordinal, exact repository root/common-dir/git-dir, target
  branch ref, parent HEAD, source staged-patch path/hash and staged tree SHA,
  plan/prompt/effective-config bindings, review skill/model/reasoning, Cursor
  model, workflow limits, accepted outcomes, integration policy, commit message,
  deterministic managed-worktree/private-ref identities, and controller identity.
- For a non-final sequence entry, use its existing frozen commit message. A
  standalone or final-entry recovery requires a bounded explicit commit message
  at prepare because no earlier sequence checkpoint message exists. Never derive
  a public message from prompts, agent output, run IDs, or error text.
- Preparing the same exact recovery is idempotent. A different source generation,
  patch/tree hash, target HEAD, commit message, or policy creates a distinct
  identity or is rejected; it must not silently mutate a prepared definition.
- `recovery start` is the one explicit authorization for the complete frozen
  local lifecycle. Replays return the existing recovery/current run and never
  duplicate worktrees, refs, agent attempts, commits, integration, sequence
  successors, reservations, cleanup, or events.

### Source eligibility and authenticated inputs

- Eligibility is typed ledger/artifact driven, never based on `last_error`, block
  summaries, provider prose, current filenames alone, or user attestation.
- Require a completed Cursor attempt with completion signal, completed staging
  event, non-empty protected staged patch and hash, exact source plan/prompt/
  config bindings, exact repository identity/branch/HEAD, and no accepted review
  result for that staged snapshot.
- Accept only an allowlisted operational reviewer stop whose next safe technical
  action is independent fresh review. Identity, digest, path, dispatch, effect,
  sandbox, repository, or mandatory-artifact contradictions remain ineligible.
- Do not require, read, hash after the fact, copy, or place into the fresh review
  prompt an old Cursor `final.txt` that lacks an authenticated outcome binding.
  Fresh review is based on the approved plan/prompt and authenticated staged
  tree. If a current source has a bound final response, omitting it keeps the
  recovery contract uniform and avoids copying unnecessary agent prose.
- At prepare and again before start, require the original target worktree to be
  at the frozen parent HEAD with the exact source patch staged, no tracked
  unstaged changes, no untracked non-ignored files, and no active process/effect/
  capacity/checkpoint uncertainty. Do not normalize or repair it.
- The terminal source run and its protected artifacts remain immutable. Record
  lineage only in recovery-owned state and, for a sequence, an explicit recovery
  resolution projection.

### Managed worktree and exact patch seeding

- Use a deterministic package-owned path under native XDG state and a narrowly
  validated private local ref namespace. Never accept a caller-supplied worktree
  path or branch name in Phase 20.6.
- Before creation, validate repository common-dir identity, path containment,
  absence of symlinks/special files, target nonexistence, private-ref absence,
  and frozen parent object identity. Use direct bounded Git argv with
  `shell=False`; do not invoke shell scripts.
- Create the private ref at the exact parent and attach one managed worktree.
  A crash replay may adopt only an exact matching Git worktree registration,
  path, common-dir, private ref, HEAD, index, and ledger intent. Any collision or
  ambiguity blocks without deletion or `--force`.
- Admit the new worktree while clean, then apply the exact protected binary patch
  with a bounded index-and-worktree Git operation. Verify the resulting staged
  diff hash, `write-tree` SHA, branch/private-ref identity, absence of unstaged or
  untracked files, and unchanged parent HEAD before scheduling review.
- The source patch is trusted data only after artifact authentication; it is not
  executable text. Do not pass it through a shell or ask Cursor to reproduce it.
- Persist attempt-unique before/after status, patch/tree, worktree-registration,
  and seeding evidence. Never store repository contents or raw patches in public
  recovery summaries.

### Fresh review seed and bounded correction loop

- Materialize a new scheduler run with explicit recovery lineage and a distinct
  `review_seed` first iteration. It starts at Codex review only after worktree
  admission and exact patch seeding; it has no fabricated Cursor execution and
  no inherited Cursor chat.
- Create a fresh Codex reviewer through the ordinary scheduler review-bootstrap
  boundary, using the frozen review model, reasoning effort, review skill, plan,
  prompt, and exact staged patch. Never copy or resume the failed source reviewer.
- The deterministic wrapper states that no trusted Cursor final response is
  supplied and directs Codex to review the staged changes and frozen artifacts.
  The schema-valid structured result remains the sole decision source.
- If review 1 has no findings, finish with `completed` or
  `completed_with_residual_risk`. If it has findings and budget remains, lazily
  create exactly one new Cursor chat in the managed worktree, persist it, and run
  correction iteration 2 with the exact Codex-authored fix prompt. Thereafter use
  the ordinary stage/review/correction loop and identities.
- A source run's failed reviews consume none of the fresh recovery review budget.
  Freeze the source effective maximum as the new run's total budget; only valid
  recovery review decisions count. Preserve Phase 20.5 capacity waiting and
  manual same-reviewer retry behavior inside the recovery run.
- No accepted review means no commit or target mutation. `max_iterations_reached`,
  operational retry wait, capacity wait, block, abort, invalid output, or any
  uncertain evidence retains the recovery worktree and target source snapshot.

### Accepted tree and automatic target integration

- Before integration, authenticate the accepted review result and its complete
  latest staged patch/tree in the managed worktree. Both accepted outcomes are
  eligible; record residual risk without relabeling it.
- Persist an immutable integration intent before any commit/ref/target-worktree
  mutation. Bind source and recovery IDs, source sequence/ordinal if present,
  parent HEAD, source tree, accepted tree/patch, review-result hash, both refs,
  both worktree identities, exact commit message, Git identity/timestamps,
  accepted outcome, versions, and intended post-integration sequence action.
- Reuse/extract the Phase 20.3 `write-tree`/`commit-tree`/`update-ref` adapter and
  commit-identity verification. The commit is unsigned, invokes no hooks, has the
  exact bounded public message, one parent equal to the frozen parent HEAD, and a
  tree exactly equal to the accepted reviewed tree.
- Advance the private recovery ref to the exact commit first so its worktree is
  clean. This does not authorize target integration by itself.
- Immediately before modifying the target, reacquire/verify exclusive target
  reservation and require it still has parent HEAD, source staged tree/patch,
  no unstaged or untracked work, and exact source repository identity.
- When corrections changed the accepted tree, construct a protected binary delta
  from the authenticated source tree to accepted tree and apply it to the target
  index/worktree with a bounded direct Git operation. If there were no changes,
  perform no content write. In both cases require the target index tree and
  canonical staged patch to equal the accepted reviewed evidence before ref CAS.
- Compare-and-swap only the exact target branch ref from parent HEAD to the exact
  recovery commit. After CAS, require target HEAD/ref, index, and worktree to be
  clean and equal to the accepted commit tree. Never reset/checkout/clean to force
  the postcondition.
- Any pre-CAS target drift blocks integration and preserves both worktrees. An
  ambiguous CAS result creates a durable hold and must be reconciled from exact
  live ref/commit/tree evidence before abort, retry, cleanup, or continuation.

### Sequence continuation and standalone completion

- A recovery may replace only the current blocked run of one exact blocked
  sequence. Add typed lineage that keeps the original materialized run visible
  and records the recovery run, accepted review, outcome, patch/tree, and commit.
  Never transition or rewrite the terminal source run.
- For a recovered non-final entry, after target CAS perform one fenced ledger
  transaction that records the recovered entry/checkpoint, preserves residual
  risk, materializes/authorizes the next preassigned frozen run, transfers the
  target reservation directly to it, and returns the sequence to active state.
  The next run begins on a later ordinary tick from the integrated clean HEAD.
- For a recovered final entry, record a distinct recovery-integrated terminal
  sequence state/report: the final local commit was performed under explicit
  recovery authorization, while push, PR review, and merge remain unperformed.
  Do not misreport it as the ordinary `awaiting_finalization` staged boundary.
- For a standalone source, mark the recovery integrated after exact target CAS,
  release the target reservation, and report the local commit plus remaining
  push/PR/merge actions safely.
- Source sequence blocked state may leave terminality only through this explicit,
  content-bound recovery transition. Aborted sequences and unrelated blocked
  reasons remain immutable/ineligible.

### Crash recovery, fencing, and idempotency

- Git worktree metadata, two local refs, two indexes/worktrees, protected files,
  and SQLite are not one transaction. Every side effect needs a durable intent,
  exact ownership identity, bounded operation, postcondition evidence, and an
  idempotent reconciliation rule.
- Cover crashes before/after: recovery definition insertion, start authorization,
  private-ref/worktree creation, clean admission, patch apply, run insertion,
  reviewer bootstrap/attempt, correction, integration intent, commit-tree,
  private-ref CAS, target delta apply, target-ref CAS, sequence handoff/terminal
  recording, reservation release/transfer, worktree removal, and private-ref
  deletion.
- Replays may adopt only exact intent-bound artifacts, worktree registrations,
  trees, commits, refs, reservations, and ledger versions. Never adopt by message,
  path similarity, tree similarity, or branch-name prefix alone.
- Extend/reuse checkpoint holds so abort and cleanup cannot race a ref that may
  have advanced. Once target CAS is proven, never roll it back; finish ledger
  reconciliation and report the immutable integration fact.
- Scheduler ticks remain bounded. A tick may perform one recovery reconciliation
  action or launch one ordinary attempt, not the entire recovery synchronously.

### Reservation, abort, and cleanup safety

- Keep independent exact ownership for the managed recovery worktree and the
  original target worktree. Acquire target reservation before any target delta or
  ref mutation; never steal or overwrite an unrelated run's reservation.
- `recovery abort` persists recovery intent before delegating to the active run's
  ordinary abort path. It stops new review/correction/integration work and never
  resets, unstages, deletes changes, or reverts an applied commit.
- Before target CAS, abort leaves the source target untouched and preserves a
  changed managed worktree for inspection. A pristine, exact package-owned
  worktree may be cleaned only through the separately recorded cleanup state.
- After a possibly successful target CAS, reconcile exact live state first;
  proven integration is recorded, not reverted. Ambiguity retains reservations,
  worktree, private ref, and evidence for manual diagnosis.
- Automatic cleanup is part of the start authorization but may run only after
  durable integration and only when the managed worktree is registered at the
  exact owned path, clean at the exact integrated commit, has no process/hold,
  and the commit is reachable from the exact integrated target ref. Use no force
  option. Delete only the exact owned private ref after worktree removal and a
  compare-and-swap identity check.
- Cleanup failure is retryable and visible; it must not change an already
  integrated success into loss of commit evidence or attempt broad filesystem
  deletion.

### Persistence, observability, and privacy

- Add a forward-only migration plus typed models and versioned JSON schemas for
  recovery definitions/states, run lineage, sequence recovery resolution,
  integration intent/evidence/result, cleanup evidence, and public status.
  Historical databases, runs, and sequences remain readable without reinterpretation.
- Exact-ID recovery status is read-only and may show safe state, source/recovery
  run prefixes, sequence/ordinal, accepted outcome, residual risk, commit prefix,
  cleanup state, and next safe action. Do not inspect Git or raw artifacts merely
  to render status.
- Default CLI/status/history/controller/sequence output must not expose full
  session IDs, prompts, patches, review content, JSONL/provider messages, account
  telemetry, Git identity, absolute managed-worktree paths, or protected artifact
  contents. Store sensitive raw evidence only in protected artifacts with bounded
  sizes and user-only permissions.
- Update sequence completion reports so recovered entries retain both immutable
  source and recovery lineage and final reports distinguish staged ordinary
  finalization from recovery-integrated completion.

### Phase boundary and real acceptance

- Phase 20.6 implements the complete local recovery lifecycle through local
  integration and optional sequence continuation. Remote publication remains
  manual and outside the scheduler.
- The real Phase 20.5 -> 20.6 sequence is acceptance of Phase 20.1–20.4 sequencing,
  not proof that 20.6 can use itself before installation. Automated Phase 20.6
  tests must provide full hermetic recovery coverage.
- After the sequence is accepted, manually committed, and installed, a separate
  explicitly authorized controlled recovery against a disposable repository/run
  may validate real timer/model behavior. That is an acceptance exercise, not a
  new implementation phase, and must never use this source worktree as the
  mutation target.

## Implementation Plan

1. Characterize existing review-retry eligibility, blocked sequence projection,
   Phase 20.3 checkpoint/ref reconciliation, Phase 20.4 abort/report behavior,
   and reservation keys with regression tests before adding recovery behavior.
2. Add recovery aggregate/domain models, source and sequence lineage, events,
   reducers, migration/store operations, protected artifact models/paths,
   schemas, safe actions, and exact-ID status projection.
3. Implement process-free `recovery prepare`: authenticated source analysis,
   current target checkpoint verification, frozen definition/idempotency,
   sequence-current-entry checks, commit-message rules, and privacy-safe output.
4. Implement the managed-worktree adapter and `recovery start` materialization:
   private-ref/worktree ownership, crash adoption, clean admission, exact binary
   patch seeding, source-tree verification, recovery run insertion, and target/
   recovery reservation acquisition.
5. Add the `review_seed` run checkpoint and fresh-review scheduling path. Skip
   initial Cursor, bootstrap a fresh reviewer normally, and lazily create a fresh
   Cursor chat only when a valid review finding requires correction.
6. Implement immutable integration intent and reuse/extract the Phase 20.3 commit
   adapter for the accepted recovery tree. Add correction-delta application,
   target tree verification, dual-ref CAS reconciliation, holds, and exact clean
   postconditions.
7. Implement standalone completion and sequence recovery continuation/final
   integration transactions, including entry replacement lineage, residual risk,
   reservation transfer, next-entry materialization, terminal report variants,
   and source immutability.
8. Implement recovery abort and exact-owned cleanup reconciliation. Cover every
   pre/post-ref boundary without reset/revert/force removal or broad deletion.
9. Wire bounded recovery reconciliation into scheduler tick and CLI
   prepare/start/status/abort rendering. Update controller/sequence safe actions,
   history/timeline, privacy redaction, and deterministic reports.
10. Update affected governance rules and current MkDocs documentation with the
    narrow explicit recovery authority. Write
    `archive/implementation-history/findings/phase-20-6-authenticated-fresh-review-recovery.md`
    with commands, failure-injection evidence, unperformed real acceptance, and
    residual risks.

## Testing Criteria

- **Domain/schema/migration tests:** all recovery states and transitions;
  immutable source/recovery/sequence lineage; accepted outcomes; residual risk;
  historical schema/database reads; invalid IDs, ordinals, hashes, policies,
  refs, paths, and cross-object inconsistencies fail closed.
- **Prepare tests:** process-free and Git-read-only; exact current target patch/
  tree/HEAD/repository match; authenticated plan/prompt/config/staging/attempt
  evidence; allowlisted operational block; idempotent replay; rejection of live
  attempts/reservations/holds, partial Cursor, accepted review, empty patch,
  mandatory-artifact tampering, target drift, dirty files, wrong/current sequence
  entry, aborted sequence, and arbitrary target overrides.
- **Legacy-input test:** a historical source with authenticated staged patch but
  no bound Cursor final response can prepare because that optional file is never
  read/copied/used; replacing its unbound events/final files cannot change any
  recovery artifact, review input, identity, or outcome.
- **Managed-worktree tests in disposable repositories:** deterministic native-XDG
  path and private ref; exact parent; binary files, modes, renames, deletions,
  unicode paths, and subdirectory invocation; clean admission; patch/tree equality;
  path/ref/worktree collision refusal; symlink/special-file refusal; exact crash
  adoption; no shell, force, reset, clean, stash, checkout-overwrite, or remote.
- **Fresh-review tests:** no Cursor invocation/chat before review 1; fresh reviewer
  rather than source reviewer; frozen model/reasoning/skill; explicit absent-final
  wrapper; schema-valid no-findings/residual-risk decisions; capacity wait and
  manual retry preservation; finding creates one fresh Cursor chat and exact fix
  prompt; correction staging/review uses ordinary limits and identities.
- **Integration adapter tests:** source-to-accepted binary delta; no-op delta;
  exact reviewed tree; deterministic unsigned/no-hook commit; installed failing/
  modifying hooks not invoked; private-ref then target-ref CAS; target becomes
  clean at commit; correction changes propagated exactly; Git identity/message/
  parent/tree verification; no remote or destructive command.
- **Integration refusal tests:** original target patch/tree/HEAD/ref/repository
  drift, unstaged/untracked work, target reservation conflict, managed worktree
  drift, accepted patch/result tampering, commit-object mismatch, private/target
  CAS conflict, partial delta, timeout/nonzero/malformed Git output, and ambiguous
  ref observation never overwrite target or fabricate completion.
- **Crash/reconciliation tests:** inject failure before/after every durable step
  listed in the guardrails; restart converges to one managed worktree, one private
  ref, one recovery run/reviewer/chat, one accepted commit, at most one target
  ref update, one sequence successor/handoff, and one cleanup result.
- **Sequence tests:** recover only the current blocked phase; original run remains
  immutable; non-final recovery resumes the frozen next entry on a later tick;
  final recovery records integrated terminal status; residual risk accumulates;
  unrelated/aborted sequences stay terminal; no reservation-release gap.
- **Standalone tests:** accepted recovery creates and integrates one exact local
  commit, releases reservations, cleans owned recovery resources, reports remote
  actions unperformed, and is idempotent after restart.
- **Abort/cleanup tests:** abort before agents, during review/correction, before
  integration, during ambiguous ref mutation, and after proven CAS; no revert or
  target data loss; dirty/ambiguous managed worktrees retained; exact clean owned
  worktree/ref removed without force only after reachability and identity checks;
  repeated abort/cleanup does not duplicate effects.
- **Privacy/status/docs tests:** stable text/JSON, safe next actions, prefixes only,
  no prompts/patches/reviews/raw output/provider prose/account data/Git identity/
  full agent IDs/managed absolute paths; docs describe automatic local integration,
  accepted residual risk, stop-on-drift, no remote actions, and explicit start.
- **Regression tests:** Phase 20.5 capacity behavior, ordinary same-reviewer retry,
  standalone submit/start/tick/abort, sequence prepare/start/checkpoint/handoff/
  abort/finalization, review-budget extension, timer bounds, packaging, and all
  prior Git/abort/privacy protections remain green.

All tests use fake agents, injected ports, temporary native-WSL state, and
disposable repositories. No automated test may read the real historical run,
invoke real models, mutate this repository's Git metadata, or change real timer,
global integration, credentials, hooks, signing, or remotes.

## Validation

Run focused recovery, sequence, scheduler, static, package, and documentation
checks. At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_6_recovery_state.py \
  tests/unit/scheduler/test_phase20_6_recovery_prepare.py \
  tests/unit/scheduler/test_phase20_6_recovery_worktree.py \
  tests/unit/scheduler/test_phase20_6_recovery_review_seed.py \
  tests/unit/scheduler/test_phase20_6_recovery_integration.py \
  tests/unit/scheduler/test_phase20_6_recovery_abort.py \
  tests/integration/test_phase20_6_fresh_review_recovery.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py \
  tests/unit/scheduler/test_phase20_3_handoff_recovery.py \
  tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
uv build
git diff --check
```

If Phase 20.5 uses a different final test filename, select its actual focused
capacity/reviewer tests while preserving the stated regression coverage. Do not
run real recovery against the active source repository during implementation or
automated review.

## Risks Or Recovery Notes

The highest-risk boundary is target integration after review in another
worktree. Approval applies to the exact accepted tree, not to a similar live
worktree. The target may change only while its parent, source index tree, staged
patch, repository identity, reservation, and cleanliness still match the frozen
definition. Any contradiction stops before overwrite.

Git worktree metadata, private and target refs, two indexes, protected artifacts,
and SQLite cannot be updated atomically. Durable intent, exact compare-and-swap,
mutation holds, commit/tree verification, and restart reconciliation are required
at every boundary. Cleanup is never evidence of success and must remain last.

This phase intentionally introduces a second narrow local-commit authority: an
explicitly started authenticated recovery may integrate its accepted final tree.
It does not broaden ordinary run/sequence commits, does not authorize remotes,
and requires manual human acceptance of the governance and Git-authority changes
after automated review.

The real Phase 20.5 -> 20.6 sequence validates unattended sequencing but cannot
exercise newly implemented 20.6 against a real failure until the accepted final
patch is manually committed and installed. A later controlled acceptance run is
expected and is not a new phase.

## OpenQuestions

None.
