# Phase 20.6.5 — Authenticated Review Rollover and Capacity Auto-Resume

## Goals

- Supersede, without rewriting, a `max_iterations_reached` scheduler source
  whose authenticated staged snapshot needs a fresh Cursor and Codex context.
- Permit an explicitly authorized rollover only after authenticating the capped
  source run, its final staged snapshot, repository identity, frozen inputs,
  and absence of live attempts or ambiguous effects.
- Materialize the successor in an isolated package-managed worktree at the
  frozen parent, seed the exact authenticated staged patch, start with a fresh
  Codex review, and lazily create one fresh Cursor chat only if that review has
  findings. The old Cursor and reviewer identities must never be reused.
- Correct the Codex App Server capacity parser so a present
  `rateLimitReachedType: null` means “not reached”, not malformed. While the
  installed scheduler timer keeps ticking, a valid available observation must
  resume a `waiting_codex_capacity` run automatically with its same bound
  reviewer; unavailable observations remain fail-closed.
- Carry the remaining Phase 20.6 P1 into the fresh candidate: reconcile an
  abort-requested pre-CAS checkpoint hold only when durable intent plus live
  Git evidence proves that CAS did not occur; preserve holds for ambiguity and
  finish integration when CAS is proven.
- After a fresh accepted review, integrate one exact reviewed composite tree
  through the existing authenticated Phase 20.6 integration authority. Mark
  the capped source as superseded through immutable lineage, never accepted.

## Non-Goals

- Do not turn ordinary `max_iterations_reached` runs into automatic retries.
- Do not resume, message, inspect, or reuse the old Cursor chat or Codex
  reviewer session.
- Do not infer capacity from provider prose, arbitrary JSON, stale reset
  estimates, or a failed probe. A malformed/unavailable probe never resumes a
  run.
- Do not accept, checkpoint, commit, merge, or install the capped Phase 20.6
  source by itself.
- Do not add generic multi-branch, parallel, cross-repository, or arbitrary
  dirty-worktree salvage.

## Scope

- Add a first-class, explicitly authorized rollover definition and lifecycle
  for an authenticated `max_iterations_reached` source with a valid latest
  staged snapshot and final validated review result containing findings.
- Freeze source/repository/config/plan/prompt/runtime bindings, parent HEAD,
  source staged-patch and tree identities, target reservation identity,
  accepted-result policy, deterministic managed-worktree/private-ref names,
  and the explicit composite commit message before local effects.
- Reuse the Phase 20.6 managed-worktree, patch seeding, fresh-review,
  correction, integration, reservation, cleanup, abort, fencing, and sequence
  projection machinery only where its invariants remain valid for a source
  that already has prior reviews.
- Add the narrowly scoped null-marker capacity parser correction and tests for
  automatic recovery from `waiting_codex_capacity` when the App Server returns
  valid available windows.
- Repair pre-CAS abort hold convergence through production reconciliation,
  never through a test-only/manual hold release.
- Add versioned state/schema/migration/artifact/status/reporting support only
  for the selected public rollover surface.
- Update operational documentation and a Phase 20.6.5 findings artifact.

## Out of Scope

- Changes to `ai_dev_loop.yaml`, model catalogs/defaults, package-owned skills,
  SessionStart hooks/bridges, timer installation, credentials, remotes, PRs,
  signing, pushing, or deployment.
- Real Cursor/Codex, provider-account, timer, or remote-Git activity in tests.
- Broader changes to Phase 20.5 failure classification other than interpreting
  the already-returned null reached marker correctly.
- Rewriting the immutable source run, its artifacts, or the original staged
  worktree.

## Required Context

- `AGENTS.md`; all `.cursor/rules/*.mdc`; current `docs/`; and archived master
  plan, Phase 20.5/20.6 plans, findings, and review artifacts.
- Phase 20.6 source analysis, prepare/start/integration/reconcile/abort/
  worktree services; state/events/reducers; SQLite store/migrations; sequence
  materialization, reporting, reservation and checkpoint code; Git runners;
  protected artifacts; and capacity probe/workflow services.
- The final P1 evidence: pre-CAS abort reconciliation must converge by proving
  no CAS occurred, not by deleting the hold manually.
- The actual App Server shape already observed in WSL: one valid limit record,
  numeric `primary`/`secondary.usedPercent`, and
  `rateLimitReachedType: null` when not reached.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local rule:
  `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- Do not modify package-owned Codex skills or use real agents in tests.
- The staged-review skill governs later independent review; it is not an
  implementation dependency.

## Architecture Guardrails

- The capped source remains terminal and immutable. Rollover lineage belongs
  solely to the new definition/run; no source state/artifact is rewritten.
- Rollover is opt-in and operator-authorized. It must not be offered for a
  missing/empty patch, a source without a validated actionable final review,
  a live attempt/effect/capacity claim, integrity contradiction, repository
  drift, or ambiguous source snapshot.
- Fresh agents are required: first action after exact seed is a new reviewer;
  Cursor is created lazily only from that new review's exact validated fix
  prompt. Never copy an old fix prompt into the fresh run.
- Preserve exact frozen model/reasoning/skill values in the new review; do not
  inherit WSL defaults or change runtime from a current CLI catalog.
- Use direct argv Git commands, no shell execution, no force/ref overwrite,
  no reset/clean/stash/rebase/merge/cherry-pick, and no target mutation before
  reviewed acceptance and all live fences pass.
- Every effect boundary must use independent persisted digest bindings. Never
  authenticate a mutable artifact by computing and accepting a new hash at
  retry time.
- Before each managed mutation and target CAS, recheck exact reservation
  ownership, active lease/deadline, recovery version, durable abort intent,
  and intent-owned hold state. Resolve only same-intent holds with proof; retain
  all ambiguous/conflicting holds.
- `rateLimitReachedType` is exhausted only for a nonempty string. `null` is a
  valid absent marker; malformed types, bad windows, timeouts, protocol errors,
  or mixed/unknown records remain unavailable and cannot launch a retry.
- The installed package may be refreshed only after the composite tree has a
  valid accepted review and an explicit authorized local commit. Verify the
  installed files match that exact commit; do not package the source worktree.

## Implementation Plan

1. Resolve the public rollover API question below, then add typed/versioned
   definition, state, events, reducer, migration, schemas, protected artifacts,
   status and safe actions for the chosen surface.
2. Authenticate capped-source eligibility from ledger/artifact evidence:
   terminal max state, exact latest staged patch/tree, actionable final review,
   frozen bindings, no active attempt/effect/capacity claim, exact source
   repository/parent/target reservation, and exact source worktree state.
3. Implement process-free prepare and explicitly authorized start. Start must
   acquire/transfer reservations safely, create the exact managed worktree and
   private ref, apply the protected patch without a shell, verify the seeded
   index/worktree/tree, then materialize the distinct fresh run directly at its
   first Codex review boundary.
4. Preserve the ordinary bounded correction loop inside the fresh run with new
   identities and only new review-authored fix prompts. Retain source lineage
   in all state, reports and sequence projections.
5. Integrate accepted composite trees through Phase 20.6's fenced CAS and
   cleanup lifecycle. Repair the pre-CAS abort-hold case so public abort and
   tick reconciliation prove non-CAS before releasing only the matching hold.
6. Correct capacity parsing and `waiting_codex_capacity` processing for valid
   null reached markers, retaining fail-closed behavior for all other invalid
   observations.
7. Update docs/findings and perform independent staged review before any
   explicit commit/install. Do not perform real workstation installation as part
   of automated validation.

## Testing Criteria

- Unit/contract tests for null, missing, empty-string, nonempty-string and
  malformed reached markers; both limit windows; mixed records; and no retry on
  unavailable capacity observations.
- Scheduler integration tests proving a valid available probe automatically
  transitions a waiting-capacity run to the ordinary same-reviewer retry path,
  while unavailable/exhausted remains waiting.
- Disposable-repository lifecycle tests for process-free rollover prepare,
  exact seed, fresh reviewer/no old session reuse, lazy fresh Cursor creation,
  source immutability, active-effect rejection, idempotent replay, reservation
  transfer, sequence non-final/final projection, cleanup, and target CAS.
- Failure-injection tests at every seed/publication/hold/abort/CAS boundary,
  including an abort before CAS with target still at parent; prove it converges
  through the public abort/tick path without a direct hold deletion.
- Negative tests for source patch/tree/digest/repository/plan/runtime mismatch,
  unbound/missing artifacts, fake `rateLimitReachedType` values, target drift,
  conflicting holds, lease loss, and abort between every mutation boundary.
- Use fake agents, clocks, IDs, Git ports, and temporary native-Wsl XDG/HOME
  fixtures only. Run focused tests plus `ruff format --check`, `ruff check`,
  `mypy src`, full pytest with native `/tmp`, `mkdocs build --strict`, and
  `uv build` when available.

## Validation

- `git diff --cached --check`
- `uv run python -m ruff format --check .`
- `uv run python -m ruff check .`
- `uv run python -m mypy src`
- `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q`
- `uv run mkdocs build --strict`
- `uv build`

## Risks Or Recovery Notes

- This phase broadens scheduler authority over a terminal capped source and
  creates a fresh-agent boundary. Human acceptance of the control-plane/Git
  authority diff remains mandatory before commit or installation.
- The original capped source worktree and artifacts are retained unchanged for
  audit. A failed rollover retains its managed worktree/private ref where
  ambiguity exists; it never deletes broad paths or touches unrelated work.
- The user-systemd timer is already operational infrastructure. Do not modify
  its installation/enablement in this phase; only package behavior can change.

## Resolved Decisions

- The public API is a distinct `scheduler rollover {prepare,start,status,abort}`
  command group. It is deliberately separate from `scheduler recovery`: a
  rollover supersedes a capped source run that has already produced valid review
  history and must create new agent identities, while recovery remains limited
  to its existing blocked-run contract.
