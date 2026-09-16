# Phase 20.6.5 — Normal-Run Rollover Completion

## Goals

- Complete and independently validate the combined Phase 20.6 and 20.6.5
  implementation in a normal `ai_dev_loop` scheduler run with a fresh Cursor
  implementer and a fresh Codex reviewer.
- Use the frozen manual prototype only as an integrity-bound implementation
  seed; it is not accepted evidence and must receive the ordinary full review.
- Provide a distinct, explicitly authorized
  `scheduler rollover {prepare,start,status,abort}` lifecycle for an
  authenticated `max_iterations_reached` source without mutating that source.
- Correct Codex capacity probing so a present
  `rateLimitReachedType: null` is valid evidence that the limit is not reached,
  while malformed or unavailable observations remain fail-closed.
- Resolve the Phase 20.6 pre-CAS abort/reconciliation P1 through public
  production paths, without a test-only hold release.
- Complete sequence projection, non-final phase handoff, integration fencing,
  crash recovery, documentation, and automated validation omitted or identified
  as residual risk by the prototype.

## Non-Goals

- Do not accept, commit, install, or integrate the frozen seed merely because it
  applies or its focused tests pass.
- Do not resume or reuse the Cursor or Codex identities from the exhausted
  Phase 20.6 run or the manual prototype execution.
- Do not automatically retry arbitrary `max_iterations_reached` runs.
- Do not infer capacity from prose, stale reset estimates, failed probes, or
  unknown provider payloads.

## Scope

- Verify and apply the exact content-addressed prototype patch described under
  Required Context before implementation work begins.
- Review every seeded change against the Phase 20.6 and 20.6.5 contracts;
  simplify, replace, or remove prototype code when required for correctness.
- Finish the rollover domain, persistence, migration, schemas, protected
  artifacts, CLI, status, safe actions, worktree seeding, fresh-agent boundary,
  correction loop, integration, reconciliation, abort, cleanup, reservation,
  sequence projection, and operational documentation.
- Fix known prototype validation failures, including the two unused
  `SCHEMA_VERSION` imports reported by Ruff.
- Add or repair tests for all behavior and failure boundaries promised here.

## Out of Scope

- Changes to `ai_dev_loop.yaml`, model defaults, package-owned skills,
  SessionStart hooks, credentials, remotes, PRs, pushes, deployment, or real
  workstation timer enablement.
- Real Cursor, Codex, provider-account, or remote-Git activity inside tests.
- Destructive Git operations or cleanup of the exhausted source worktree.
- Generic dirty-worktree salvage, multi-repository recovery, or parallel
  rollover execution.

## Required Context

- Read `AGENTS.md`, every `.cursor/rules/*.mdc`, the Phase 20.5 and Phase 20.6
  plans/findings, the recovery implementation, and the Phase 20.6 review tests.
- Frozen prototype seed:
  `/home/rojobad/.local/state/ai_dev_loop/bootstrap-seeds/3efc11696bd3316cd32241bd4411cba82f7249cdbababfecd6106098e79cffe2.patch`
- Required seed SHA-256:
  `3efc11696bd3316cd32241bd4411cba82f7249cdbababfecd6106098e79cffe2`.
- Required seed size: `982226` bytes.
- Before applying the seed, independently verify both its SHA-256 and size.
  Stop without modifying the repository if either differs. Apply that exact
  binary patch with Git; do not reconstruct it from the manual worktree.
- The seed is an untrusted candidate. Its focused suite previously reported
  22 passing tests, but Ruff found two unused imports and its findings disclosed
  incomplete sequence wiring and insufficient integration crash coverage.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local rules, especially
  `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- Do not modify package-owned Codex skills or use real agents in tests.
- The later staged-review skill is an independent acceptance boundary, not an
  implementation dependency.

## Architecture Guardrails

- The exhausted source run and its artifacts remain terminal and immutable.
  Rollover lineage belongs only to the successor definition/run.
- Rollover is operator-authorized and rejects missing or unauthenticated source
  patches, non-actionable final reviews, active attempts/effects/capacity
  claims, repository drift, conflicting reservations, and ambiguous Git state.
- A successor begins with a genuinely fresh Codex reviewer. A fresh Cursor chat
  is created lazily only from that reviewer's validated findings; never copy an
  old fix prompt or old agent identity.
- Freeze and authenticate repository, parent HEAD, patch/tree, config,
  plan/prompt, model/reasoning/skill, artifact, reservation, and commit-message
  bindings before effects.
- Use direct-argv Git operations. No shell Git execution, force ref updates,
  reset, clean, stash, rebase, merge, cherry-pick, push, or remote mutation.
- Recheck reservation ownership, leases, versions, abort intent, holds, target
  parent, and persisted digest bindings at every effect and CAS boundary.
- Resolve only same-intent holds with conclusive live evidence. Preserve
  ambiguous holds; finish integration when CAS is proven and converge abort
  when non-CAS is proven.
- Treat `rateLimitReachedType: null` as a valid absent marker and a nonempty
  string as exhausted. Missing fields, invalid types, malformed windows,
  protocol failures, timeouts, or mixed unknown records remain unavailable.
- Do not commit or install any result until the normal scheduler run receives
  an independent accepted Codex review and the operator authorizes the Git
  action.

## Implementation Plan

1. Verify the frozen seed path, size, and SHA-256, then apply the exact binary
   patch. Record the verification in the Phase 20.6.5 findings artifact.
2. Audit the entire seeded diff against this plan and repository rules. Treat
   the prototype as replaceable implementation, not as approved architecture.
3. Repair known lint failures and complete missing rollover behavior, including
   sequence-aware non-final/final projection and handoff, idempotent lifecycle
   transitions, fresh identity creation, and source supersession reporting.
4. Complete integration, abort, cleanup, and reconciliation fencing with
   failure injection before and after every durable intent, hold, worktree,
   private-ref, checkpoint, commit, CAS, and cleanup boundary.
5. Validate the null capacity marker and automatic capacity resume through the
   normal same-reviewer retry path while retaining fail-closed semantics.
6. Align typed state, events, reducer, SQLite migration/version, schemas,
   historical read-only behavior, CLI output, safe actions, docs, and findings.
7. Run focused tests and the complete validation suite. Leave all intended
   changes staged for the independent Codex reviewer; do not commit.

## Testing Criteria

- Unit and contract tests for rollover models, schemas, events, reducer,
  migration, store idempotency, CLI/status, safe actions, and source eligibility.
- Disposable-repository integration tests for exact seed, fresh reviewer first,
  lazy fresh Cursor creation, no source identity reuse, reservations, sequence
  non-final/final handoff, accepted integration, cleanup, and target CAS.
- Failure-injection tests for crashes and aborts at every mutation boundary,
  including pre-CAS holds with conclusive CAS and non-CAS outcomes.
- Capacity regression tests for null, missing, empty, nonempty, wrong-type, mixed
  records, both windows, unavailable probes, and automatic resume only on a
  valid available observation.
- Negative integrity tests for patch/tree/repository/plan/runtime mismatch,
  active effects, target drift, lease loss, conflicting holds, and ambiguous
  state.
- Use fake agents, clocks, IDs, Git ports, isolated native-Wsl XDG paths, and
  `/tmp`; no real external model or provider activity in tests.

## Validation

- `git diff --cached --check`
- `uv run python -m ruff format --check .`
- `uv run python -m ruff check .`
- `uv run python -m mypy src`
- `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q`
- `uv run mkdocs build --strict`
- `uv build`

## Risks Or Recovery Notes

- This is control-plane and Git-authority code. Independent Codex acceptance and
  explicit operator authorization remain mandatory before commit or install.
- If seed verification or application fails, stop and preserve the clean
  worktree; never substitute a newly generated patch implicitly.
- On ambiguous rollover effects, preserve the managed worktree, private ref,
  holds, source run, and artifacts for reconciliation. Never delete broad paths.
- The manual prototype worktree remains audit evidence and is not the source of
  truth for the normal run.

## OpenQuestions

None.
