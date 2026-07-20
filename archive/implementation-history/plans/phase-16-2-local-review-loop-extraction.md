# Phase 16.2 - Reusable Local Review/Fix Loop Extraction

Status: proposed for approval
Depends on: the approved Phase 16.1 recover/controller follow-on being present and
the Phase 16.1 barrier being green.

## Goals

Extract Cursor -> staging -> Codex into a typed, reusable local application
capability that does not import, inspect, or mutate GitHub PR-review domain state.

The phase must:

- introduce a typed request/result boundary for starting or resuming a local
  review/fix execution;
- keep existing A/B commands, outputs, state, artifacts, identities, checkpoints,
  recovery, extension, abort, and result semantics unchanged;
- make prompt selection, an optional caller-scheduled first Cursor turn,
  pre-Cursor validation, and accepted-result handling explicit caller inputs instead
  of decisions derived from `RunState.github_pr_review` inside the local engine;
- move the minimum Phase 15 bridge logic needed by the existing suite behind a
  one-way legacy adapter whose direction is PR review -> local capability;
- prove the boundary works without constructing `GithubPrReviewState` and without
  GitHub/publication behavior; and
- leave a boundary that PR review v2 can call later without sharing state machines.

## Non-Goals

- Do not implement PR review v2 domain, reducer, SQLite, effects, leases, fencing,
  retries, gateways, worker, commands, or migrations.
- Do not redesign public A/B commands or migrate A/B persistence to SQLite.
- Do not improve legacy PR review, add recovery paths, or preserve undocumented
  Phase 15 behavior.
- Do not remove Phase 15 fields, statuses, schemas, commands, tests, or artifacts;
  cleanup belongs to Phase 16.9 after v2 acceptance.
- Do not migrate historical runs or change local compatibility policy.
- Do not use real Cursor, Codex, GitHub, SSH, commit, push, or publication in tests.

## Scope

- New typed, non-persisted request, scheduled-turn, outcome, result, and caller
  finalization contracts.
- Refactor of the workflow engine, local iteration helpers, and resume planner.
- Thin adapters for `start`, `resume`, detached `launch`, and legacy PR callers.
- Relocation of external-feedback scheduling/prompt helpers into a transitional
  legacy adapter.
- Unit, integration, regression, recovery, abort, and dependency-boundary tests.
- A findings/handoff artifact with final signatures and validation evidence.

Expected files:

- add `src/ai_dev_loop/local_review_loop.py`;
- add `src/ai_dev_loop/legacy_pr_review_local_adapter.py`;
- update `src/ai_dev_loop/workflow_engine.py`;
- update `src/ai_dev_loop/iterations.py`;
- update `src/ai_dev_loop/resume_planner.py`;
- update `src/ai_dev_loop/commands/start.py` and `commands/resume.py`;
- update `src/ai_dev_loop/launch_worker.py` only if needed to preserve its boundary;
- update `src/ai_dev_loop/commands/pr_review.py` and `pr_review_recover.py` only to
  consume the legacy adapter;
- add focused unit tests and
  `tests/integration/test_phase16_2_local_review_boundary.py`;
- add `archive/implementation-history/findings/phase-16-2-to-16-3-handoff.md`.

File placement may change only to resolve an evidenced import cycle. Preserve the
dependency direction and record any adjustment in the findings artifact.

## Out of Scope

- Functional expansion of `commands/pr_review*.py`, `external_adjudication.py`,
  `pr_review_worker.py`, or GitHub/publication runners.
- Deletion or reinterpretation of `RunState.github_pr_review`, PR-specific statuses,
  `external_cursor_iteration`, `external_fix_prompt_path`, or
  `WorkflowState.local_review_count`.
- Changes to `schemas/run-state-v1.json`. If a persisted-shape change proves
  unavoidable, stop and request a plan amendment.
- Changes to `ai_dev_loop.yaml`, `.cursor/rules/`, global/package skills, hooks,
  integrations, or review-skill configuration.
- Public CLI additions, renames, output redesign, or exit-code changes.
- Public documentation claiming PR review v2 exists before cutover.
- Staging, committing, pushing, resetting, cleaning, stashing, or rewriting Git
  history during implementation.

## Required Context

Read before implementation:

- `pr-review-v2-restructure-context.md`;
- `archive/implementation-history/findings/phase-16-1-to-16-2-handoff.md`;
- `archive/implementation-history/plans/phase-16-1-ab-regression-barrier.md`;
- `archive/implementation-history/findings/phase-16-1-ab-regression-baseline.md`;
- `tests/integration/test_phase16_1_ab_regression_barrier.py`;
- `docs/guia/flujo-handoff.md`;
- `docs/operacion/prepare-start-resume-abort.md`;
- `src/ai_dev_loop/workflow_engine.py`;
- `src/ai_dev_loop/iterations.py`;
- `src/ai_dev_loop/resume_planner.py`;
- `src/ai_dev_loop/state.py`;
- `src/ai_dev_loop/commands/{start,resume,launch,recover,extend,abort}.py`;
- `src/ai_dev_loop/launch_worker.py`;
- `src/ai_dev_loop/runners/{cursor,codex,git,staging}.py`;
- `src/ai_dev_loop/commands/pr_review.py` and `pr_review_recover.py`;
- Phase 15.12, 15.15, 15.16, and 15.17 tests touching the bridge.

Current code, tests, schemas, CLI help, and `/docs` are authoritative. If they
disagree on A/B safety or public behavior, stop and record the conflict.

## Cursor Rules And Skills

Follow:

- `.cursor/rules/ai-dev-loop-governance.mdc`;
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`;
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc` for findings evidence.

No repository-root `AGENTS.md` or repo-local `.cursor/skills/` directory exists.
Cursor must not invoke
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; the A/B orchestrator
owns staged review after implementation.

## Architecture Guardrails

1. Dependency direction is commands/PR callers -> local boundary -> local
   engine/planners/runners. Reusable local modules must not import PR commands,
   workers, GitHub/publication runners, or PR models.
2. Local code must not access `state.github_pr_review`, inspect PR lifecycle strings,
   choose `github-*` prompts, allocate external iterations, refresh publication
   fingerprints, transition to PR statuses, or spawn PR workers.
3. Use a non-persisted typed request for `run_id`, start/resume operation, tool
   policy, and optional scheduled first Cursor turn. That turn carries an explicit
   iteration number, relative prompt path, and generic read-only pre-Cursor validator.
   Frozen repository/session/chat/runtime values remain authoritative in `RunState`.
4. Define typed outcomes for at least `accepted`,
   `accepted_with_residual_risk`, `max_iterations_reached`, `interrupted`, `failed`,
   and `aborted`, with existing safe result/artifact metadata.
   `WorkflowResult`/`StartResult`/`ResumeResult` imports and rendered statuses remain
   compatible through adapters or aliases.
5. The local core may deliver a validated result to a typed caller handler, but it
   must not decide publication or subsequent PR lifecycle. A handler invoked under
   local locks may only perform deterministic state/artifact finalization; no GitHub,
   worker spawn, commit, push, or network I/O. External continuation happens after
   the local boundary returns and locks are released.
6. Default A/B finalization maps no-findings to current `completed` or
   `completed_with_residual_risk` and preserves current error/exit behavior.
7. `legacy_pr_review_local_adapter.py` may inspect Phase 15 state only to replace the
   removed coupling. Reusable local modules must never import it, and the adapter
   must not import `commands.pr_review`; command callers invoke the adapter and own
   worker spawn or other external continuation. It adds no new compatibility or
   recovery behavior.
8. Preserve state/schema shapes, iteration keys, paths, hashes, events, result
   messages, status transitions, permissions, and restart checkpoints.
9. Preserve exact controller A, reviewer B, Cursor chat, frozen runtime, and never
   use `--last`, inferred IDs, replacement chats, or new review sessions.
10. Preserve resume, successor recovery, usage-limit recovery, extend, locks,
    process metadata, and abort. Do not repeat completed turns or work after abort.
11. Preserve Git identity checks, staged-patch checks, `git add -A` normalization,
    and non-destructive behavior. Caller clean-baseline validation runs before attempt
    artifacts are created.
12. Keep sensitive prompts, patches, reviews, outputs, full IDs, tokens, and
    environments out of request/result summaries and normal output.
13. Tests are hermetic: disposable repos, isolated HOME/XDG, fake `agent`/`codex`,
    and fail-fast fake `gh`; no real external activity.

## Implementation Plan

### 1. Confirm the entry baseline

- Confirm the approved recover/controller follow-on is present and preserve unrelated
  user changes.
- Run the Phase 16.1 barrier before shared-code changes; require 12 passes.
- Record live collection and focused baseline in the Phase 16.2 findings artifact.
- Inventory public aliases/import callers before moving symbols.

### 2. Introduce the typed local contract

- Add `local_review_loop.py` with typed operation, scheduled-turn, outcome, result,
  and caller finalization contracts.
- Keep invocation types non-persisted. The `run_id` selects durable state; do not
  duplicate frozen identities/configuration in the request.
- Provide one authoritative entry point such as
  `run_local_review_fix(request) -> LocalReviewFixResult`, owning local locks,
  preflight/probes, checkpoint execution, and typed result delivery.
- Make the default request sufficient for A/B `start` and `resume`.
- Validate scheduled iteration/prompt as explicit, relative, non-empty, confined to
  the run, and non-colliding with a completed iteration.
- Separate deterministic accepted-state finalization from actions after return.

### 3. Make iteration/resume planning local-only

- Change local prompt APIs to consume scheduled-turn context instead of GitHub state.
- Preserve initial prompts, exact Codex fix prompts, correction/recovery envelopes,
  metadata, staged-patch lookup, and budget behavior.
- Move `next_external_cursor_iteration`, `pending_external_cursor_iteration`,
  `derive_external_cursor_iteration_for_recovery`, external prompt selection, and
  Phase 15 compatibility behavior into the legacy adapter.
- Change `active_cursor_iteration`, interrupted checkpoint derivation, and
  `plan_next_action` to consume local invocation context, never GitHub state.
- Keep `workflow.local_review_count` as the existing generic persisted budget counter;
  do not rename or alter its schema.

### 4. Extract the engine and preserve A/B adapters

- Refactor `workflow_engine.py` to execute only local Cursor, staging, Codex,
  result-processing, restart, failure, timeout, usage-limit, and abort behavior.
- Replace external preflight/prompt branching in `_run_cursor_turn` with the explicit
  scheduled-turn specification.
- Remove dynamic PR imports, publication fingerprint refresh, PR status mutation,
  and PR worker spawn from `_apply_review_result`.
- Deliver accepted review data through the typed result/finalization boundary;
  structured Codex JSON remains the decision source.
- Adapt `commands/start.py`, `commands/resume.py`, and `launch_worker.py` to build
  default requests and preserve current result rendering, exceptions, and exit codes.
- Preserve import compatibility without retaining a second loop implementation.

### 5. Add the one-way legacy bridge

- Convert Phase 15 scheduling fields into a generic local request only inside
  `legacy_pr_review_local_adapter.py`.
- Move/wrap clean-commit preflight there and invoke it read-only before Cursor
  attempt artifacts.
- After local acceptance, finalize legacy state and publication fingerprint outside
  reusable modules. Spawn/continue the PR worker from `commands/pr_review.py` only
  after local return and lock release.
- Update both current PR callers to consume the typed result explicitly.
- Move external recovery helper imports in `pr_review_recover.py` to the adapter;
  preserve behavior and add no recovery cases.
- Keep focused Phase 15 behavior green only through this substituted bridge.

### 6. Add boundary and dependency tests

- Add unit tests for request validation, scheduled turns, outcomes, finalization,
  budget accounting, resume planning, and backward-compatible adapters.
- Add `test_phase16_2_local_review_boundary.py`; invoke the boundary on a run with
  `github_pr_review is None` and never construct `GithubPrReviewState`.
- Prove implementation -> staging -> findings -> same-chat correction -> accepted,
  exact B/runtime, unchanged hashes/paths, and zero GitHub/publication/commit/push.
- Prove typed outcomes while public A/B adapters retain current statuses and failures.
- Add a focused dependency test/static assertion that reusable local modules have no
  PR/GitHub/publication imports or `github_pr_review` access. Do not freeze other
  private layout.
- Adapt Phase 15 tests to prove explicit scheduling/preflight input and caller-owned
  post-return acceptance handling.

### 7. Validate and hand off to 16.3

- Run the barrier after meaningful shared-engine changes and first in final acceptance.
- Run focused local/recovery/abort and legacy bridge sets, then full validation.
- Add `phase-16-2-to-16-3-handoff.md` with final signatures, dependency direction,
  moved legacy symbols, unchanged contracts, exact commands/counts, and risks.
- Inspect for schema drift, duplicate engines, reverse imports, sensitive values,
  weakened tests, or Phase 16.3+ work. Stop on conflict.

## Testing Criteria

Required levels:

- **Unit:** typed contracts, scheduled-turn validation, outcome mapping, finalization,
  budget, and resume planning.
- **Integration:** direct boundary with real local persistence and fake subprocesses;
  public start/resume/launch; restart/recovery/abort; transitional legacy adapter.
- **Regression:** the Phase 16.1 barrier and existing focused local baseline.
- **Static architecture:** no PR/GitHub/publication dependency or
  `github_pr_review` access in reusable local modules.

Cover missing/empty/absolute/escaping/drifted prompts; iteration collisions;
preflight failure before attempt artifacts; same chat/exact reviewer; findings,
accepted, residual, max; Cursor/Codex failure, timeout, usage-limit, abort; restart
from Cursor/staging/review/process-review; public result/error compatibility; and
legacy continuation after local return without duplicate worker handoff.

All tests use fake CLIs and isolated state. No real model, GitHub, credentials, SSH,
network, commit, or push.

Focused commands:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_2_local_review_boundary.py \
  tests/unit/test_controller_launch.py \
  tests/integration/test_launch_worker.py \
  tests/integration/test_prepare.py \
  tests/integration/test_start_cursor.py \
  tests/integration/test_start_staging.py \
  tests/integration/test_start_codex_review.py \
  tests/integration/test_phase5_loop_resume.py \
  tests/integration/test_phase11_recover.py \
  tests/integration/test_phase12_index_mutations_and_staging_recovery.py \
  tests/integration/test_phase13_cursor_usage_limit_recovery.py \
  tests/integration/test_phase14_5_initial_staging_recovery.py \
  tests/integration/test_abort.py \
  tests/integration/test_e2e_acceptance.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_phase15_12_external_feedback_iteration.py \
  tests/unit/test_phase15_16_external_feedback_preflight.py \
  tests/integration/test_phase15_12_external_feedback_recovery.py \
  tests/integration/test_phase15_15_publication_patch_fingerprint.py \
  tests/integration/test_phase15_16_external_feedback_clean_baseline.py \
  tests/integration/test_phase15_17_durable_external_adjudication_flow.py
```

## Validation

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
uv run python -m build
git diff --check
```

Acceptance requires direct execution without `GithubPrReviewState`; no GitHub access
or effects in local modules; barrier and full suite green; unchanged A/B public and
persisted contracts; exact identities/checkpoints; transitional Phase 15 tests green
without new legacy behavior; and accurate findings evidence.

## Risks Or Recovery Notes

- Acceptance finalization can create an ambiguous window. Keep deterministic state
  finalization under existing locks and external continuation after return; test the
  ordering.
- Extract one authoritative path incrementally; wrappers must not fork the engine.
- Preserve the distinction between monotonic artifact iteration and local review
  budget without reading GitHub state.
- Update obsolete Phase 15 private patch paths rather than retaining reverse imports.
- Before A/B handoff, isolate/commit the approved 16.1 follow-on so implementation
  starts from an intentional baseline with only configured plan/prompt exceptions.
- If crash-safe finalization requires a persisted-shape change, stop for a plan
  amendment; do not import Phase 16.3 durable-state design.

## OpenQuestions

None.
