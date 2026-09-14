# Phase 20.3 — Reviewed Checkpoint Commits and Atomic Phase Handoff

## Goal

Automatically advance a started linear sequence after an accepted non-final
phase by creating one exact, durable checkpoint commit and materializing and
authorizing the next frozen phase without ever leaving the repository
reservation unowned.

Both accepted review outcomes advance:

- `completed`;
- `completed_with_residual_risk`.

Residual risk must be preserved explicitly in sequence/entry state and final
reporting, but it must not block advancement. All other terminal or blocked
outcomes stop before commit and successor materialization.

Checkpoint commits must contain exactly the staged tree reviewed by Codex, use
the phase's frozen public commit message, execute no Git hooks or signing, and
be recoverable across a process death before or after Git reference mutation.
The final sequence phase remains staged and creates no automatic commit, push,
PR, or merge.

## Non-Goals

- Do not advance from `max_iterations_reached`, `blocked`, `aborted`, malformed
  review output, uncertain agent identity, failed admission, or any unaccepted
  review result.
- Do not treat `completed_with_residual_risk` as a clean test outcome; preserve
  the exact residual-risk classification while allowing advancement.
- Do not run Git hooks, commit signing, `git commit` porcelain, push, fetch,
  merge, rebase, checkout/switch, reset, clean, stash, tag, unstage, force
  update, or remote operations.
- Do not create a commit after the final phase.
- Do not release the reservation between predecessor completion and successor
  authorization.
- Do not materialize multiple future phases, implement branching/conditions,
  skip phases, reorder entries, or modify frozen inputs.
- Do not add generic sequence retry/recover, public sequence abort, final PR
  automation, or the complete reporting/docs acceptance work assigned to
  Phase 20.4.
- Do not alter Phase 19 capacity probing or resume semantics.

## Scope

- Add a non-terminal accepted-review checkpoint state for a sequence-bound
  non-final run, carrying the accepted outcome and exact reviewed artifacts
  needed to request a checkpoint safely.
- Add typed sequence checkpoint intent/result persistence, protected evidence,
  events/reducers, tick eligibility, safe actions, and idempotent reconciliation.
- Add a narrow injectable Git checkpoint port and production adapter using
  bounded direct Git argv calls to verify the reviewed staged snapshot, create
  an unsigned/no-hook commit object, and compare-and-swap the exact branch ref.
- Change the sequenced non-final completion path so it retains the run-owned
  repository reservation until checkpoint and handoff finish.
- Materialize the next preassigned run from its frozen Phase 20.1 entry and, in
  one ledger transaction, finalize the predecessor, record the checkpoint,
  transfer reservation ownership, insert/authorize the successor, and advance
  the sequence current entry.
- Change final-phase accepted completion so it remains staged, releases the
  reservation, records `awaiting_finalization`, and creates no commit.
- Add the narrowly scoped governance-rule and product-documentation changes
  needed to replace the categorical no-commit claim with the explicit reviewed
  sequence-checkpoint exception.
- Add comprehensive failure-injection, real-temporary-Git, integration,
  compatibility, and privacy tests plus Phase 20.3 findings.

## Out of Scope

- `ai_dev_loop.yaml`, planning/review skill content, package-owned
  `ai-dev-loop-controller`/`ai-dev-loop-handoff` assets, global integrations,
  account credentials, timer installation/enablement, and real workstation
  scheduler operation.
- Changing standalone successful runs: they must still end staged, release
  their reservation, and never commit.
- User-visible sequence retry, skip, resume-after-terminal-failure, deletion,
  list/history, automatic PR creation, or final commit.
- Running the new commit adapter against this source repository during
  implementation or validation. Git mutation tests use disposable repositories.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file. Identify every categorical
  no-commit statement that must receive the same narrow exception.
- Phase 20.1 and 20.2 plans, findings, migrations, schemas, frozen artifacts,
  sequence state, materializer, provenance binding, start transaction, and tests.
- Final Phase 19 code/findings for `waiting_codex_capacity`, reservation
  retention, exact B resume, attempt ingestion, tick behavior, and privacy.
- Phase 17.4–17.6, 17.8–17.10 plans/findings for staging snapshots, review
  decisions, terminal transitions, effects, attempts, capacity, fencing,
  reservations, abort, and crash reconciliation.
- Historical publication/commit findings only for proven reusable Git identity,
  SSH/signing, staged-patch, or update-ref lessons; do not restore removed PR or
  publication engines.
- `src/ai_dev_loop/runners/git.py` and all current staged-patch/fingerprint
  validation helpers.
- Scheduler Cursor/Codex workflow services, tick, attempt service, contracts,
  safe actions, status/history, checkpoint helpers, events, reducer, state, and
  effects.
- SQLite store, migrations, reservation/capacity/attempt APIs, protected
  artifacts, sequence materializer, and repository admission target code.
- Current fake agent/Codex fixtures and scheduler unit/integration/privacy/abort
  tests, especially staged-patch drift and restart reconciliation coverage.
- Current Git safety, full workflow, CLI, observability, troubleshooting,
  security/privacy, and sequence docs.

Phase 20.2 must be independently reviewed, committed, and present as the clean
baseline before Phase 20.3 begins.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local `.cursor/rules/*.mdc` rule. The
  active Phase 20.3 plan is the explicit authority for the narrow sequence
  checkpoint exception; every other Git prohibition remains in force.
- Update affected repository-local Cursor rules consistently so later agents do
  not see contradictory categorical commit prohibitions. Do not weaken rules for
  standalone runs, arbitrary commits, remote Git, destructive Git, or agents.
- The repository has no `.cursor/skills` directory. Do not modify the package
  skills, planning skill, staged-review skill, or target configuration.
- Use fake agents and disposable temporary Git repositories/XDG homes. Never
  invoke real Cursor/Codex, real account probes, real systemd, a target remote,
  signing keys, user hooks, or this repository's Git mutation path in tests.
- Keep implementation changes unstaged and uncommitted for independent review.
  Because this phase changes Git authority and repository-local governance, its
  staged review and final human acceptance must call out those changes explicitly.

## Architecture Guardrails

### Accepted outcome and phase boundary

- For a sequence-bound non-final run only, a schema-valid no-findings Codex
  decision must transition to a dedicated non-terminal checkpoint boundary
  rather than the ordinary terminal helper that releases reservations.
- Persist whether the accepted outcome is `completed` or
  `completed_with_residual_risk`, the review iteration, latest review-result
  binding, staged-patch artifact path/hash, and immutable sequence entry binding.
- Do not reinterpret or erase residual test risk. Sequence and entry projections
  must retain it even after later phases succeed.
- A standalone run and the final run in a sequence keep the existing terminal
  state kinds. Final sequence completion updates the sequence to
  `awaiting_finalization`, releases its reservation, and leaves the final patch
  staged.
- `waiting_codex_capacity` remains an ordinary active wait. It creates no
  checkpoint and advances only after the exact reviewer eventually returns one
  of the two accepted outcomes.

### Exact reviewed snapshot

- Before any commit object or ref mutation, revalidate repository root, Git
  common directory, worktree Git directory, symbolic branch ref, expected HEAD,
  reservation owner, absence of active attempts/effects/capacity claims, and
  sequence/run versions.
- Recompute exact `git diff --cached --binary` bytes and require their hash to
  match the latest staged-patch artifact reviewed by Codex. Require a non-empty
  staged change and no tracked unstaged or untracked non-ignored work.
- Run `git write-tree` and persist the expected tree SHA. The committed tree must
  equal this exact reviewed index tree; never regenerate content from a patch or
  from live plan/prompt sources.
- Bind checkpoint intent to sequence ID, predecessor run ID/ordinal, next planned
  run ID/ordinal, expected branch ref, parent HEAD, reviewed patch SHA, tree SHA,
  exact frozen commit message, accepted outcome, and sequence/run versions.
- Store sensitive identity or diagnostic material only in protected artifacts.
  Public state/status/history may expose shortened commit/hash identifiers and
  safe reason codes, not patches, Git identity, prompts, or raw stderr.

### Commit mechanism and public history

- Use a dedicated bounded adapter based on direct argv calls equivalent to
  `git write-tree`, `git commit-tree`, and `git update-ref <ref> <new> <old>`.
  Never use `shell=True` or `git commit` porcelain.
- Do not execute hooks. Explicitly disable signing for the checkpoint object.
  Do not alter repository/user Git configuration to achieve either behavior.
- Resolve and validate author/committer identity before ref mutation. Freeze the
  exact identity and author/committer timestamps needed for deterministic retry
  in a protected checkpoint-intent artifact, then pass a minimal explicit child
  environment without mutating `os.environ`.
- Use exactly the single-line bounded `commit_message` frozen for the predecessor
  entry. Do not append sequence/run UUID trailers, prompts, model names, risk
  text, or private XDG metadata to public Git history.
- The only allowed ref mutation is a compare-and-swap of the already-checked-out
  local branch from the exact expected parent to the exact checkpoint commit.
  Detached HEAD, symbolic-ref drift, parent drift, alternate worktree identity,
  unexpected index/worktree changes, or CAS failure must fail closed.
- After ref update, verify HEAD/ref equals the intended commit, the commit tree
  equals the reviewed tree, and index/worktree are clean. Never use reset,
  checkout, clean, or staging to force this postcondition.

### Durable recovery across Git and SQLite

- Persist a content-bound checkpoint intent before creating/updating Git objects
  or refs. SQLite and Git are not atomic; every crash point must be reconcilable
  from the intent, protected evidence, exact live ref/tree/index, and ledger.
- Required recovery cases:
  1. before `commit-tree`: safely retry the same deterministic intent;
  2. after object creation but before `update-ref`: reproduce/verify the same
     commit and perform the same CAS;
  3. after successful `update-ref` but before ledger handoff: recognize exact
     HEAD/commit/tree and finish ledger materialization without another commit;
  4. after ledger handoff: observe the authorized successor idempotently.
- Never adopt an arbitrary commit because its message or tree appears similar.
  Adoption requires the exact intent-bound parent, tree, commit object identity,
  branch ref, and protected hashes.
- A generic Git failure, ambiguous live state, conflicting artifact, changed
  identity, or unrelated ref movement blocks advancement and preserves evidence.
  It must not guess, reset, create a second successor, or silently release and
  reclaim the reservation.

### Atomic reservation handoff and successor materialization

- Keep the active reservation owned by the accepted predecessor throughout
  checkpoint creation and reconciliation. Do not call the ordinary terminal
  reservation-release path for a non-final sequenced phase.
- Prepare/verify the successor's deterministic run artifacts from its frozen
  entry, then perform one `BEGIN IMMEDIATE` ledger transaction that:
  - verifies sequence, predecessor, intent, versions, checkpoint identity, and
    active reservation owner;
  - transitions the predecessor to its recorded terminal success state;
  - records checkpoint commit SHA/tree/parent and residual-risk outcome;
  - inserts the next preassigned run and normal submission/authorization events;
  - transfers the existing active reservation row directly from predecessor to
    successor without a released state;
  - advances entry projections and sequence current ordinal/run;
  - creates no extra live effect, attempt, timer, capacity claim, or reservation.
- Add a dedicated store operation for this proven same-sequence consecutive
  handoff. Do not weaken ordinary `insert_submitted_run` conflict handling or
  permit an unrelated owner transfer.
- The successor is admitted on its next normal tick. Its clean baseline must be
  the checkpoint commit; the normal run engine remains responsible for Git
  admission, Cursor, reviewer B, corrections, and Phase 19 waits.

### Tick bounds, abort, and safety

- Process checkpoint reconciliation as a bounded scheduler action under the
  global tick lease and run/sequence fencing. Preserve abort precedence before
  every Git or materialization side effect.
- One tick action may durably request/reconcile a checkpoint and handoff, but it
  must not launch multiple agent phases in one unbounded call. The successor
  begins through a later ordinary tick.
- Do not commit while a child process, live attempt, pending review result, or
  capacity claim may still mutate the repository.
- Existing `scheduler abort <run-id>` must remain non-destructive. If requested
  before ref mutation, it prevents checkpoint creation; if ref mutation may
  already have occurred, reconcile exact intent first and never roll it back.
  Full sequence-abort presentation is Phase 20.4.

### Governance compatibility

- Replace categorical statements that the orchestrator never commits with one
  precise exception: an explicitly started sequence may create non-final local
  checkpoint commits only after an accepted exact staged review and under the
  immutable policy above.
- Retain prohibitions on autonomous inferred approval, standalone-run commits,
  final-phase commits, hooks/signing, tags, remotes, destructive operations,
  and commits initiated by Cursor or Codex.
- Documentation and rules must distinguish product capability from validation:
  implementation tests may commit only inside disposable repositories and must
  never exercise the feature against the active source worktree.

## Implementation Plan

1. Characterize the exact current review-completion, staged-patch validation,
   terminal reservation release, tick/effect, abort, and restart boundaries with
   focused regression tests before changing them.
2. Add typed accepted-review/checkpoint states, events, reducers, schemas, safe
   actions, and sequence entry projections. Route only non-final sequence runs
   into this boundary; prove standalone and final runs retain existing behavior.
3. Implement the narrow Git checkpoint port and production adapter with bounded
   commands, exact patch/tree/ref validation, deterministic protected intent,
   no-hook/no-sign behavior, safe identity handling, and exhaustive disposable
   repository tests.
4. Integrate checkpoint request/reconciliation into tick under leases, versions,
   active-attempt/capacity checks, and abort precedence. Add injected crash
   points around intent, object creation, ref CAS, artifact writes, and ledger
   update.
5. Implement same-sequence successor materialization and the atomic reservation
   ownership transfer/finalization transaction. Reuse Phase 20.2 artifact/context
   materialization and normal run events without relaxing standalone admission.
6. Implement final-phase success projection: both accepted outcomes produce
   `awaiting_finalization`, preserve any residual risk, release the reservation,
   leave the patch staged, and create no commit or successor.
7. Update affected Cursor governance rules and current docs with the narrow
   explicit commit exception and actual Phase 20.3 behavior. Flag the authority
   change prominently for independent staged and final human review.
8. Write
   `archive/implementation-history/findings/phase-20-3-reviewed-checkpoint-handoff.md`
   with exact Git commands, crash/reconciliation evidence, tests, unperformed
   real-workstation validation, and residual risks.

## Testing Criteria

- **Review-transition unit tests:** non-final sequence `completed` and
  `completed_with_residual_risk` enter checkpoint pending and retain reservation;
  residual risk remains recorded; standalone/final successes use ordinary
  terminal behavior; findings/max/block/abort never request a checkpoint.
- **Git adapter tests in disposable repositories:** exact binary staged-patch
  hash, non-empty index, tree identity, bounded single-line message, author/
  committer determinism, unsigned commit, installed modifying/failing hooks not
  invoked, exact parent, checked-out branch CAS, clean postcondition, and no
  remote/destructive commands.
- **Git refusal tests:** staged drift, unstaged/untracked work, empty patch,
  detached HEAD, branch/root/git-dir/common-dir drift, parent/ref drift, missing
  identity, timeout/nonzero/malformed output, conflicting intent, and update-ref
  CAS failure all fail closed without successor creation.
- **Crash/reconciliation tests:** interruption before/after intent write,
  commit-tree, update-ref, successor artifact copy, and ledger transaction;
  retries converge to one checkpoint commit, one successor run, one direct
  reservation transfer, and one ordered event chain.
- **Store/concurrency tests:** transfer requires same sequence and consecutive
  ordinals, exact previous owner and versions; concurrent ticks cannot duplicate
  commit/materialization; unrelated submit never observes a released reservation
  gap; old run/reservation invariants remain valid.
- **Integration tests:** at least a three-phase fake sequence advances through
  separate Cursor chats and reviewer B sessions; later admission sees the prior
  checkpoint as clean HEAD; a middle residual-risk success advances and remains
  visible; Phase 19 capacity wait pauses then resumes without checkpointing
  early; the final phase remains staged and sequence becomes
  `awaiting_finalization` without a final commit.
- **Abort/safety tests:** abort before ref mutation prevents commit; abort during
  an ambiguous checkpoint does not reset/revert; no checkpoint occurs with a
  live attempt/effect/capacity claim; standalone abort behavior remains intact.
- **Governance/privacy regressions:** rule/docs language grants only the narrow
  exception; public outputs never reveal patch, Git identity, prompt, raw
  output, full agent IDs, or protected artifact contents; public commit contains
  only the frozen user message and normal Git identity.

## Validation

Run focused state/Git/handoff tests, all scheduler integration regressions, and
static/docs checks. At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_3_checkpoint_state.py \
  tests/unit/scheduler/test_phase20_3_git_checkpoint.py \
  tests/unit/scheduler/test_phase20_3_handoff.py \
  tests/unit/scheduler/test_tick.py \
  tests/unit/scheduler/test_tick_regressions.py \
  tests/unit/scheduler/test_phase17_6_abort.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/integration/test_phase17_6_restart_reconcile.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Do not run checkpoint validation on the source repository or invoke real agents,
real account probes, signing keys, hooks, remotes, or systemd.

## Risks Or Recovery Notes

The irreversible boundary is local branch ref movement. Writing a commit object
alone is not sufficient proof of advancement; only the exact intent-bound
compare-and-swap and subsequent verification permit ledger handoff. Conversely,
if the ref moved successfully before a crash, retry must adopt that exact
checkpoint rather than create another commit or roll it back.

By explicit product decision, `completed_with_residual_risk` advances. The
sequence must make accumulated residual-risk phases visible so the operator can
evaluate them during finalization. It must never relabel them as `completed`.

This phase intentionally bypasses hooks and signing for intermediate checkpoints
to preserve the reviewed tree exactly. That behavior is public and must remain
documented; final human commit/PR/CI policy remains outside this phase.

## OpenQuestions

None.
