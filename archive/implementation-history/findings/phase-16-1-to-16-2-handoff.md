# Phase 16.1 -> Phase 16.2 Handoff

Date: 2026-07-20

This is the entry handoff for the agent that will analyze and plan Phase 16.2.
It summarizes what Phase 16.1 delivered, what evidence exists, and the unresolved
gate that must be decided before the local-loop extraction begins.

## Executive status

Phase 16.1 delivered the requested A/B regression barrier and coupling inventory,
but it did **not** reach a green acceptance state.

- Implementation commit: `035cacb` (`test: add phase 16.1 A/B regression barrier`)
- Integrated through PR #11
- Merge commit on `main`: `af7bdd7`
- Automated A/B run: `ai-dev-loop-20260720T112506Z-997d97`
- Run outcome: `completed_with_residual_risk`
- Current working context when this handoff was written:
  `rba/phase_16-2`, clean, based on the merged Phase 16.1 result

The residual risk is concrete: the new barrier has one intentional failing test
because local `recover` drops controller A metadata from the successor run. This is
an A/B control-plane contract conflict, not an environment-only validation issue.

## Mandatory reading order

Before proposing or executing Phase 16.2, read:

1. `pr-review-v2-restructure-context.md`
2. `archive/implementation-history/plans/phase-16-1-ab-regression-barrier.md`
3. `archive/implementation-history/findings/phase-16-1-ab-regression-baseline.md`
4. `tests/integration/test_phase16_1_ab_regression_barrier.py`
5. Current A/B documentation and rules:
   - `docs/guia/flujo-handoff.md`
   - `docs/operacion/prepare-start-resume-abort.md`
   - `.cursor/rules/ai-dev-loop-governance.mdc`
   - `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
   - `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
   - `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
   - `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
   - `.cursor/rules/ai-dev-loop-abort-contracts.mdc`

Current code, schemas, tests, CLI help, and current docs are authoritative over
archived historical plans. The Phase 16 restructuring context controls the new
phase boundaries and the explicit decision to replace, rather than repair, legacy
PR review.

## What Phase 16.1 delivered

### Explicit A/B barrier

Added:

```text
tests/integration/test_phase16_1_ab_regression_barrier.py
```

The file is the named regression barrier that later Phase 16 subphases should run
whenever they touch the local loop or shared orchestration code.

It characterizes:

- A/B `prepare` with distinct controller A and reviewer B;
- GitHub absent and GitHub explicitly disabled;
- detached `launch` and launcher idempotency;
- one Cursor chat reused across implementation and fixes;
- exact reviewer B resumed for every Codex review, never A and never `--last`;
- Cursor -> staging -> Codex across multiple iterations;
- `completed`, `completed_with_residual_risk`, `max_iterations_reached`,
  `interrupted`, `failed`, and `aborted` outcomes;
- `extend` followed by detached continuation;
- local failed-run `recover` and successor continuation expectations;
- abort during detached Cursor and Codex boundaries;
- status/logs/inspect privacy surfaces;
- absence of `gh`, PR-review worker, GitHub artifacts, publication, commit, push,
  or remotes during a local-only run.

The acceptance scenarios use disposable repositories, isolated HOME/XDG roots,
sanitized Codex rollouts, fake Cursor/Codex CLIs, and a fail-fast fake `gh`.
No real model, GitHub, SSH, or network activity belongs in this barrier.

### Durable baseline and contract inventory

Added:

```text
archive/implementation-history/findings/phase-16-1-ab-regression-baseline.md
```

It records:

- the observable contract matrix for prepare/launch/start/resume/recover/extend/
  abort and read-only surfaces;
- executable evidence for all six local result statuses;
- the symbol-level local-to-legacy PR-review coupling inventory;
- the live pre-change test baseline;
- post-change focused results;
- the blocking recover/controller finding and available decisions.

### Phase plan

Added:

```text
archive/implementation-history/plans/phase-16-1-ab-regression-barrier.md
```

Phase 16.1 intentionally made no production, schema, config, public-doc,
integration, or legacy PR-review edits.

## Validation evidence

### Pre-change baseline

- Focused local-loop set: **163 passed**
- Full live collection: **817 tests**
- Full suite: **817 passed**
- Ruff format/check: passed
- mypy strict package check: passed
- MkDocs strict build: passed
- sdist/wheel build: passed

The historical/cache count was not assumed; the `817` count was measured live.

### Post-change barrier

Focused command:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

Result: **1 failed, 11 passed**.

The sole failure is:

```text
test_ab_recover_successor_preserves_identities_and_skips_completed_cursor
```

Focused Ruff checks and `git diff --cached --check` passed. The plan explicitly
required execution to stop on a contract conflict, so no green post-change full
suite was claimed.

## Unresolved entry gate: recover drops controller A

### Expected A/B contract

An eligible local failed run may create a distinct immutable recovery successor.
That successor must preserve:

- controller A ownership;
- reviewer B session identity;
- Cursor chat identity;
- frozen Codex runtime;
- required recovery artifacts and lineage.

Controller A must be able to discover and control the successor through
`controller status` and, when the successor checkpoint allows it, detached
`launch`.

### Current behavior

`src/ai_dev_loop/commands/recover.py::_create_successor_run` constructs the
successor `RunState` without copying `source.controller`.

Consequences:

- source A/B run retains its controller;
- successor persists `controller: null`;
- reviewer B, Cursor chat, runtime, and completed-work checkpoint are preserved;
- direct `resume <successor-id>` can still work;
- `controller status --run-id <successor-id>` rejects the successor because it no
  longer has A/B controller metadata;
- controller-session discovery cannot find the successor;
- the documented A/B authority/control path is broken after local recovery.

This remains present in merged `main` at the time of this handoff. Do not weaken or
delete the failing assertion to make the barrier green.

### Decision required before Phase 16.2 extraction

The user/architect must choose one of these contracts:

1. **Preserve controller A on local recovery successors** (recommended because it
   matches current A/B docs and the approved Phase 16.1 plan). Implement a tiny,
   separately approved follow-on that deep-copies the optional controller state,
   adds/keeps the controller discovery and detached continuation assertions, and
   runs the barrier plus the full suite; or
2. **Intentionally drop controller ownership**, which requires an explicit public
   contract change describing how A discovers and controls recovered runs. That
   option would require plan, docs, status/control behavior, and test changes; it
   must not be inferred from the current omission.

Phase 16.2 must not silently absorb this repair into the extraction or treat the
current omission as frozen desired behavior.

## Local/legacy couplings Phase 16.2 must remove deliberately

The full inventory is in the Phase 16.1 baseline. The critical extraction points
are:

### `workflow_engine._run_cursor_turn`

- Interprets external PR lifecycle through
  `is_external_cursor_prompt_iteration(state)`.
- Calls `validate_external_feedback_pre_cursor(state)` for external feedback.
- Local correction preflight and external-feedback clean-baseline preflight must be
  separated without weakening ordinary staged-patch checks.

### `workflow_engine._apply_review_result`

- Contains the direct no-findings branch for
  `github_pr_review.lifecycle == fixing_external_feedback`.
- Dynamically imports publication helpers from `commands.pr_review`.
- Refreshes the publication fingerprint, transitions to
  `publishing_external_fix`, and spawns the PR worker.
- None of those GitHub/publication decisions belongs in the reusable local loop.

### `iterations.py`

Local prompt selection and review budgeting share a module with:

- external Cursor iteration allocation;
- external recovery derivation;
- GitHub fix-prompt routing;
- post-external local-review budget reset/counting.

Phase 16.2 must preserve local iteration numbering, exact prompts/envelopes,
staged-patch checkpoints, and review limits while removing GitHub lifecycle
interpretation from the reusable boundary.

### `RunState.github_pr_review`

The local engine currently branches on GitHub state stored in the same legacy
`RunState`. Phase 16.2 should stop the reusable local capability from importing or
interpreting that state, but it must not yet delete legacy fields/schemas or perform
the Phase 16.9 cleanup.

## Phase 16.2 target boundary

After the entry gate is resolved and Phase 16.1 is green, plan Phase 16.2 as one
bounded implementation phase with these outcomes:

- typed local request/result models;
- Cursor -> staging -> Codex as a reusable capability independent of GitHub models;
- no publication, polling, PR lifecycle, or post-acceptance GitHub decisions inside
  the local capability;
- an application boundary/callback through which callers receive the local result;
- existing A/B commands and observable behavior adapted to the new boundary;
- exact reviewer B, Cursor chat, runtime, prompts, hashes, artifacts, checkpoints,
  resume/recover/extend/abort behavior preserved;
- a test can invoke the local boundary without constructing
  `GithubPrReviewState`;
- full Phase 16.1 barrier and existing local-loop suite green.

Do not implement SQLite, PR review v2 state/reducer/effects, GitHub gateways,
legacy cleanup, or a public command redesign in Phase 16.2.

## Required execution discipline for the next agent

1. Resolve or explicitly replan the recover/controller gate first.
2. Run the Phase 16.1 barrier before touching shared local-loop code.
3. Inspect current code and tests; do not design 16.2 from this handoff alone.
4. Produce a dedicated Phase 16.2 plan and Cursor prompt with its own
   `OpenQuestions` before implementation.
5. Keep side effects and GitHub lifecycle decisions outside the reusable local
   result contract.
6. Prefer behavioral tests at public/application boundaries; do not freeze private
   helper structure that the extraction must change.
7. Run the Phase 16.1 barrier whenever shared code changes, followed by focused
   local-loop tests and the full suite.
8. Do not invoke real Cursor/Codex/GitHub in automated tests.
9. Do not repair or extend legacy PR review except for removal of the identified
   coupling required by the approved Phase 16.2 plan.
10. Do not begin Phase 16.9 cleanup or remove compatibility fields/tests.

## Next safe action

Obtain the explicit contract decision for recover successors. If controller A is to
be preserved, complete and validate the tiny follow-on first. Only after the Phase
16.1 barrier is green should the next agent analyze the repository and draft the
implementable Phase 16.2 extraction plan.

## Resolution update

This update supersedes the unresolved-gate language retained elsewhere above as
historical evidence. On 2026-07-20 the user selected the recommendation to preserve
controller A on local recovery successors. The separately scoped follow-on
deep-copies the optional controller state and keeps historical runs without a
controller unchanged.

- Phase 16.1 barrier: **12 passed**.
- Focused local-loop baseline: **163 passed**.
- Full live collection and suite: **829 collected, 829 passed**.
- Ruff format/check, mypy, MkDocs strict build, sdist, and wheel: passed.

The entry gate is resolved. The next safe action is to analyze the current
repository and draft the dedicated Phase 16.2 plan and Cursor prompt; extraction
must not begin until that plan and its `OpenQuestions` are reviewed and approved.
