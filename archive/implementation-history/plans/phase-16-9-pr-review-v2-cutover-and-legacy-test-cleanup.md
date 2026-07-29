# Phase 16.9 — PR-review v2 cutover and legacy/test cleanup

Date: 2026-07-28

## Goal

Make the accepted PR-review v2 workflow the only public `ai_dev_loop pr-review`
workflow, remove the obsolete v1 PR-review implementation and the tests that
exist solely to cover it, and reduce demonstrably redundant v2 test coverage
without weakening the accepted safety contracts.

Phase 16.8 accepted the `existing-PR` happy path only. This phase is a
cutover/cleanup phase, not a resilience expansion.

## Non-Goals

- Do not implement, broaden, close, or add production/tests for any item in
  `PHASE_16_8_DEFERRED_ISSUES.md`.
- Do not attempt a numeric test-count target or delete v2 tests simply because
  their filenames contain historical phase/correction terminology.
- Do not add a v1-state migration, legacy read adapter, legacy active-run
  compatibility mode, or command compatibility aliases.
- Do not rename the accepted `pr_review_v2` configuration section, its schema
  key, SQLite tables, artifact layout, or Python package merely because the
  public CLI namespace is changing.
- Do not alter the accepted live PR, branch, original dirty checkout, retained
  Gate B evidence, or user-global/XDG run history.
- Do not perform deferred crash/recovery, protected-write, privacy-E2E, or
  SourceRunOrigin resilience work as incidental cleanup.

## Scope

1. Route `ai_dev_loop pr-review` to the exact accepted v2 command surface:
   `create`, `prepare`, `start`, `status`, `history`, `resume`, and `abort`.
   Preserve their v2 argument names, defaults, read/write gates and output
   semantics; this is a namespace move, not a reimplementation.
2. Remove the temporary public `pr-review-v2` namespace after the public route
   has v2 parity. Remove legacy-only `pr-review` subcommands rather than
   retaining misleading compatibility shims.
3. Delete v1-only PR-review engine code, state/schema types, workers,
   recovery-only code, adapters, runners, fixtures, and tests after proving
   that no supported production or test consumer remains.
4. Update README, MkDocs CLI/configuration/flow/troubleshooting/operations
   documentation so it documents only the public v2 workflow and does not
   call it temporary/pre-cutover.
5. Reduce test count in two safe tiers:
   - remove tests and helper/fixture code that becomes unreachable with v1;
   - consolidate/delete v2 tests only when a written contract-to-test matrix
     demonstrates another retained test covers the same behavior, boundary,
     failure mode and assertion strength.

## Out of Scope

- `PHASE_16_8_DEFERRED_ISSUES.md` and every listed deferred issue, except for
  retaining its documentation and ensuring the new docs do not misrepresent
  it as completed.
- Control-plane changes: `ai_dev_loop.yaml`, the package handoff/controller
  skills, this planning skill, and the staged-review skill.
- General orchestrator commands unrelated to PR review, global integrations,
  Codex Desktop bridge behavior, or generic recovery features that still have
  non-PR-review consumers.
- Historical archive plans/findings, other than adding the normal Phase 16.9
  findings/handoff artifact if the repository workflow requires it.

## Required Context

Read before editing:

- `archive/implementation-history/findings/phase-16-8-to-16-9-handoff.md`;
- `PHASE_16_8_DEFERRED_ISSUES.md`;
- `archive/implementation-history/master-plan.md` and Phase 16.1--16.8 plans
  as needed for deleted-contract provenance;
- current `src/ai_dev_loop/cli.py`, `commands/pr_review_v2.py`,
  `pr_review_v2/`, `config.py`, `state.py`, and `schemas/`;
- current public docs under `README.md` and `docs/`;
- the current code and tests are authoritative if archival material conflicts.

## Cursor Rules And Skills

Follow every local rule in `.cursor/rules/`:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`;
- `ai-dev-loop-docs-acceptance-contracts.mdc`;
- `ai-dev-loop-global-integrations-contracts.mdc`.

No repository-local `.cursor/skills/` or `AGENTS.md` is present at planning
time. The relevant planning/governance context is the local rules above.

## Architecture Guardrails

- The v2 reducer remains pure; durable SQLite is authoritative for v2 run,
  effect, claim, lease, timer, journal and write-reconciliation state.
- `prepare`/`create` remain non-mutating preparation paths. `start` remains
  the sole external-effects gate. Do not create a compatibility command that
  accidentally moves this gate.
- Preserve exact Codex session and Cursor chat continuity. Never infer either
  identity and never use `--last`.
- Keep protected artifacts owner-only and keep status/history/docs/privacy
  surfaces redacted. Do not expose prompts, raw reviews, patches, credentials
  or full identifiers while restructuring commands.
- Preserve explicit argv subprocess execution, no `shell=True`, direct
  non-force Git operations, no reset/clean/stash/rebase/force-push, and the
  current abort/lease ownership boundaries.
- Do not delete shared helpers based only on their name. Delete them only
  after searching all production/test/documentation references and confirming
  no supported v2 or non-PR workflow consumes them.
- Deleting the v1 `RunState.github_pr_review` contract intentionally makes
  legacy v1 run state unsupported. Do not silently deserialize, migrate or
  reinterpret it.
- Keep `pr_review_v2` config/schema keys as the accepted internal contract in
  this phase. Public command cutover does not authorize config migration.

## Implementation Plan

### 1. Establish the deletion inventory before changing behavior

1. Verify the worktree is clean and the baseline is the committed Phase 16.8
   state. If it is not, stop; do not stage, commit, discard, or absorb
   unrelated work.
2. Produce a temporary, reviewable inventory mapping each candidate legacy
   module/type/schema/fixture/test to all import and invocation sites. At a
   minimum inspect:
   - `commands/pr_review.py`, `commands/pr_review_independent.py`, and
     `commands/pr_review_recover.py`;
   - `pr_review_worker.py`, `legacy_pr_review_local_adapter.py`,
     `external_adjudication.py`, `github_pr_review_result.py` and their
     legacy-only runners;
   - `GithubPrReviewState`, `RunState.github_pr_review`, legacy JSON schemas,
     legacy CLI rendering and all Phase 15 PR-review tests/fixtures;
   - shared `state`, `config`, Git, process and runner code for non-legacy
     consumers.
3. Record a compact contract-to-test matrix for retained v2 coverage before
   deleting any v2 test. Use it to classify every candidate as:
   `legacy-only delete`, `duplicate consolidate`, or `required retain`.
   The matrix may be included in the Phase 16.9 findings/handoff rather than
   becoming a permanent runtime artifact.

### 2. Move the public CLI namespace to v2

1. Make `pr_review_app` expose the existing v2 command implementations with
   the exact existing v2 behavior and help text adapted only to remove the
   temporary wording.
2. Remove registration of `pr_review_v2_app` and all `pr-review-v2` help,
   command and documentation references. Do not leave aliases that preserve
   the retired temporary namespace.
3. Remove v1-only public subcommands (`set-cursor-model`, `continue`,
   `recover`, and any other legacy-only surface) rather than mapping them to
   a different v2 operation. Ensure unknown retired subcommands fail clearly
   through normal CLI parsing.
4. Retain the separate read-only `ai_dev_loop github doctor` command if it has
   no legacy engine dependency. Move its implementation out of a deleted v1
   module only if required, preserving its read-only behavior and tests.
5. Add focused CLI tests proving the public namespace has the v2 command set,
   the temporary namespace is absent, `prepare` remains read-only and `start`
   remains the only effects gate.

### 3. Delete the v1 implementation only after the route is proven

1. Remove legacy commands, worker entry points, recovery/adjudication/local
   adapters, models and result schemas that the inventory proves v1-only.
2. Remove `github_pr_review` from the legacy `RunState` model and its schema
   only when all production consumers have been removed. Update schema/model
   alignment tests accordingly. Do not add a reader for historical v1 state.
3. Remove v1 fixtures and test helpers together with their sole consumers.
   Keep common fake GitHub/process utilities if v2 tests still use them.
4. After each deletion group, search the repository for imports, strings,
   stale CLI examples and schema names. Resolve only live supported consumers;
   archive history remains historical and should not be rewritten.
5. Avoid opportunistic refactors in v2 infrastructure. If a shared helper
   needs a mechanical extraction to preserve a supported command, keep the
   behavior and add focused regression coverage.

### 4. Clean the tests without losing accepted v2 evidence

1. Delete every test that is exclusively tied to removed v1 behavior,
   including legacy Phase 15 PR-review flows and recovery paths, only after
   the import/use inventory confirms it has no retained contract.
2. Retain, at minimum, coverage for the accepted v2 public CLI, reducer
   purity, SQLite authority/durability, explicit start gate, protected
   artifact/privacy behavior, exact Codex/Cursor continuity, no-`--last`,
   non-force Git publication, write reconciliation, abort ownership and the
   Phase 16.1 A/B barrier.
3. For v2 consolidation, prefer merging parametrized cases into a single
   well-named test/module only when the replacement preserves the specific
   assertion strength and failure boundary. Keep regressions that uniquely
   cover a corrected production defect.
4. Do not delete any test because it covers a Phase 16.8 deferred issue;
   those tests were not accepted as full coverage and the issue itself is out
   of scope. Do not add new tests for the deferred matrix.
5. Report test collection counts before/after and list deleted legacy modules
   plus consolidated-v2 mappings. Treat the reduction as evidence, not the
   acceptance criterion.

### 5. Update user-facing documentation

1. Update README and MkDocs CLI, configuration, full-flow, operations,
   troubleshooting, security/privacy and traceability pages that mention
   `pr-review-v2`, temporary/pre-cutover behavior, or v1 commands.
2. Document only verified v2 behavior under `ai_dev_loop pr-review`, including
   the explicit start gate and safe status/history output. Do not document
   deferred resilience as implemented.
3. Explain the intentional compatibility boundary succinctly: legacy v1
   PR-review runs and commands are not supported after this cutover; no
   migration/read adapter is provided.
4. Keep historical plans/findings intact. Update navigation/configuration
   references if needed so documentation builds cleanly.

## Testing Criteria

Automated tests are required because this phase changes public CLI behavior,
removes persisted state/schema support, deletes recovery paths, and changes
documentation/packaging surfaces.

- Unit/CLI: public `pr-review` exposes the v2 command set and semantics;
  `pr-review-v2` and v1-only subcommands are unavailable; retired state/schema
  types are absent; config validation still accepts the unchanged
  `pr_review_v2` contract.
- Unit/domain/infrastructure: retain tests for reducer purity, SQLite
  persistence, claims/leases/timers, exact session/chat handling, protected
  result stores, read/write contracts, reconciliation, Git transport and
  privacy-safe status/history.
- Integration/regression: retain the Phase 16.1 A/B barrier and targeted v2
  supervisor/control/existing-PR happy-path coverage. Tests must use fake
  GitHub/Cursor/Codex processes only; do not contact real services or models.
- Negative checks: repository searches must show no live source/test/docs
  references to deleted v1 modules, `github_pr_review`, obsolete schemas, or
  `pr-review-v2`, except in explicitly historical archive records.
- Test reduction evidence: report collection before/after, deleted
  legacy-only tests, and every v2 consolidation with its retained successor.

## Validation

Run focused checks while iterating, then the complete relevant suite using a
native WSL temp directory:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q <public-cli-and-v2-focused-tests>
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/integration/test_phase16_1_ab_regression_barrier.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest --collect-only -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv build
uv run mkdocs build --strict
```

If a configured command is unavailable, report the exact blocker and run the
closest repository-supported equivalent; do not weaken the test criteria.
Review `git diff` and `git status` at the end, but do not stage, commit, push,
reset, clean or otherwise rewrite Git state.

## Risks Or Recovery Notes

- A namespace move can accidentally alter the effects gate or option
  semantics. Reuse v2 command functions and prove CLI parity with focused
  tests rather than duplicating implementations.
- Deleting `github_pr_review` is intentionally incompatible with historical
  v1 run-state reads. If current code reveals a required live consumer, stop
  and report it; do not invent a migration.
- Test-count reduction can create false confidence. A test with a similar name
  is not necessarily duplicate coverage; use the contract matrix and preserve
  unique failure assertions.
- Do not run a real live PR acceptance as part of this phase unless separately
  authorized. The accepted Phase 16.8 controlled PR remains untouched.
- If the cleanup exposes a deferred Phase 16.8 issue, record it without fixing
  it and stop only if the issue prevents preserving an accepted v2 contract.

## OpenQuestions

None. The user explicitly authorized the two-level cleanup: complete removal
of proven legacy-only code/tests in Phase 16.9, plus only evidence-backed v2
test consolidation; broader test-suite redesign remains future work.
