# Phase 16.1 - A/B Local Loop Regression Barrier

## Goals

Establish an explicit, durable regression barrier for the local A/B implementation
workflow before Phase 16.2 extracts the reusable Cursor -> staging -> Codex loop.

The phase must:

- inventory the observable contracts of `prepare`, `launch`, `start`, `resume`,
  `recover`, `extend`, and `abort` for the local workflow;
- prove with automated tests that the local workflow operates with GitHub PR review
  disabled and without creating GitHub/PR-review side effects;
- freeze controller A, reviewer B, Cursor chat, Codex session, prompt, iteration,
  staging, checkpoint, recovery, abort, privacy, and result-status behavior;
- record the current couplings from the local workflow engine and iteration planner
  into the legacy PR-review lifecycle so Phase 16.2 can remove them deliberately;
- capture a reproducible pre-change and post-change validation baseline for all later
  Phase 16 subphases.

## Non-Goals

- Do not extract, redesign, or introduce the reusable local-loop boundary planned for
  Phase 16.2.
- Do not implement PR review v2 domain models, reducer, SQLite store, effects, worker,
  gateways, commands, or migrations.
- Do not repair, extend, or add compatibility/recovery branches to legacy Phase 15 PR
  review.
- Do not remove GitHub fields, statuses, schemas, commands, tests, or legacy modules.
- Do not change public CLI behavior, persisted state/schema shapes, command output,
  artifact layouts, result semantics, or A/B role ownership.
- Do not perform a real Cursor, Codex, GitHub, SSH, publication, commit, push, or
  integration-install operation.
- Do not turn historical test count `817` or the existing pytest cache into an
  acceptance assertion. Measure the live suite in the execution environment.

## Scope

- A new explicit integration/regression suite for the local A/B workflow, using
  disposable Git repositories, isolated HOME/XDG roots, and fake external CLIs.
- Minimal test fixture/helper changes needed to make that suite deterministic and
  readable.
- A durable Phase 16.1 findings/baseline artifact containing:
  - the observable contract matrix;
  - the local-to-legacy dependency inventory;
  - pre-change and post-change validation commands and outcomes;
  - current test counts and any skips/environment limitations;
  - discrepancies or residual risks.
- Focused execution of existing local-loop tests to prove the new barrier agrees
  with the already implemented contract.

Production source changes are not expected in this phase. If a characterization test
demonstrates that current product behavior violates the authoritative docs, rules, or
safety contract, stop and report the evidence. Do not fix the behavior or encode the
defect as the desired contract without an amended plan.

## Out of Scope

- Functional edits under `src/ai_dev_loop/`, including
  `workflow_engine.py`, `iterations.py`, `state.py`, `resume_planner.py`,
  `recovery_planner.py`, `commands/pr_review*.py`, `external_adjudication.py`, and
  GitHub/publication runners.
- Changes to `src/ai_dev_loop/schemas/` or any persisted model/schema version.
- Changes to `ai_dev_loop.yaml`, package-owned/global skills, hooks, integration
  installers, `.cursor/rules/`, or this planning skill.
- User-facing documentation rewrites under `docs/`; discrepancies belong in the
  Phase 16.1 findings artifact for a later explicitly scoped decision.
- Historical run migration, mutation, adoption, resume, recovery, or cleanup.
- Writes to the real user HOME, Codex/Cursor homes, XDG run history, or a real target
  repository.
- Staging, committing, pushing, resetting, cleaning, stashing, or otherwise rewriting
  repository Git state.

## Required Context

Read before implementation:

- `pr-review-v2-restructure-context.md`, especially the decisions, A/B guardrail,
  Phase 16.1 scope, transversal testing strategy, and continuation rules.
- Current product documentation:
  - `docs/guia/flujo-handoff.md`
  - `docs/guia/flujo-completo.md`
  - `docs/operacion/prepare-start-resume-abort.md`
  - `docs/operacion/estado-artefactos.md`
  - `docs/operacion/seguridad-privacidad.md`
  - `docs/operacion/observabilidad.md`
  - `docs/operacion/troubleshooting.md`
  - `docs/referencia/cli.md`
  - `docs/referencia/configuracion.md`
- Archived architecture/history only for intent where current code/docs/tests are not
  sufficient:
  - `archive/implementation-history/master-plan.md`
  - `archive/implementation-history/plans/phase-5-bounded-review-fix-loop-and-resume.md`
  - `archive/implementation-history/plans/phase-6-real-abort.md`
  - `archive/implementation-history/plans/phase-11-recover-failed-runs.md`
  - `archive/implementation-history/plans/phase-14-5-initial-index-staging-and-recovery.md`
- Local workflow implementation and boundaries:
  - `src/ai_dev_loop/commands/{prepare,launch,start,resume,recover,extend,abort,controller}.py`
  - `src/ai_dev_loop/{launch_worker,launcher,workflow_engine,iterations}.py`
  - `src/ai_dev_loop/{resume_planner,recovery_planner,state,abort_control}.py`
  - `src/ai_dev_loop/runners/{cursor,codex,git,staging,probes}.py`
- Existing fixtures and regression evidence:
  - `tests/conftest.py`
  - `tests/fixtures/sample_repo/ai_dev_loop.yaml`
  - `tests/unit/test_controller_launch.py`
  - `tests/integration/test_launch_worker.py`
  - `tests/integration/test_prepare.py`
  - `tests/integration/test_start_cursor.py`
  - `tests/integration/test_start_staging.py`
  - `tests/integration/test_start_codex_review.py`
  - `tests/integration/test_phase5_loop_resume.py`
  - `tests/integration/test_phase11_recover.py`
  - `tests/integration/test_phase12_index_mutations_and_staging_recovery.py`
  - `tests/integration/test_phase13_cursor_usage_limit_recovery.py`
  - `tests/integration/test_phase14_5_initial_staging_recovery.py`
  - `tests/integration/test_abort.py`
  - `tests/integration/test_e2e_acceptance.py`

Current code, tests, schemas, CLI help, and current product docs are authoritative over
archived history. If those current sources disagree on safety or public behavior, stop
and record the conflict instead of choosing silently.

## Cursor Rules And Skills

Follow all repository-local Cursor rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-root `AGENTS.md` or repo-local `.cursor/skills/` directory is present.
No additional Cursor skill is required for implementation.

The configured staged-review boundary is
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. Cursor must not invoke
it directly; the existing A/B orchestrator will use it later to review staged changes.

## Architecture Guardrails

- Treat this phase as an observational barrier. Tests must freeze externally visible
  commands, durable state, protected artifacts, subprocess boundaries, and Git
  outcomes, not private helper call graphs that Phase 16.2 is expected to refactor.
- Preserve the exact A/B authority split: A and B are distinct validated session IDs;
  B prepares and becomes inactive; A alone launches, extends, aborts, and inspects;
  automated Codex reviews always resume exact B and never A or `--last`.
- Preserve one Cursor chat per run across initial work, fixes, resume, extend, and
  eligible recovery. Never create a replacement chat after workflow progress exists.
- Preserve exact prompts and deterministic envelopes, staged-patch/hash checkpoints,
  iteration numbering and kinds, frozen Codex runtime, relative artifact paths,
  permissions, redaction, locks, and non-destructive Git behavior.
- The local proof must use configuration where `github` is absent or explicitly
  disabled. Assert `github_pr_review` remains absent and that no `gh` command,
  PR-review worker, GitHub artifact tree, publication action, commit, or push occurs.
- Use fake `agent`, fake `codex`, and a fail-fast/logging fake `gh` when proving
  negative external behavior. Use disposable repositories and isolated HOME/XDG
  roots. Never rely on real credentials or network availability.
- Exercise public command/application boundaries and real subprocess fakes wherever
  practical. Internal monkeypatching is acceptable only for deterministic fault
  injection at boundaries such as timeouts or crash checkpoints; the acceptance path
  must not depend exclusively on monkeypatching private workflow functions.
- Keep `completed`, `completed_with_residual_risk`, `max_iterations_reached`,
  `interrupted`, `failed`, and `aborted` semantics exactly as currently documented
  for the local loop. Do not reinterpret an operational pause from future PR review
  v2 in this legacy local-state model.
- Preserve failed-source immutability and successor-based local `recover` behavior.
  The same-run retry policy described for future PR review v2 does not replace the
  current local recovery contract.
- Do not add production imports or dependencies. In particular, do not introduce a
  state-machine/durable-workflow library or SQLite into the local A/B cycle.
- Do not leak full session IDs, prompts, patches, reviews, raw JSONL, stderr/stdout,
  tokens, or environments into default CLI output, event summaries, test failure
  messages, or the findings artifact.

## Implementation Plan

### 1. Capture the pre-change baseline without changing product state

- Confirm the worktree and staged state without modifying either. Preserve unrelated
  user changes if present and keep the phase diff isolated.
- Run the existing focused local-loop tests listed under `Testing Criteria`, then the
  full static/type/test validation baseline.
- Record the exact commands, Python/tool versions, collected/passed/skipped/xfailed
  counts, duration when available, and result in
  `archive/implementation-history/findings/phase-16-1-ab-regression-baseline.md`.
- Treat `.pytest_cache` and the historical `817` count as context only. If the live
  pre-change suite is not green, classify the failure as product, test, or environment
  evidence and stop before adding assertions that would conceal it.

### 2. Build a durable observable-contract matrix

- In the findings artifact, create a matrix with one row per public surface:
  `prepare`, `launch`, `start`, `resume`, `recover`, `extend`, `abort`, `status`,
  `controller status`, `logs`, and `inspect` where they expose the A/B loop.
- For each row record:
  - authorized actor/session;
  - accepted source states/checkpoints;
  - observable state/result transition;
  - identity and artifact invariants;
  - Git/repository invariants;
  - privacy/output invariants;
  - existing and new regression test node IDs that prove the contract.
- Include the six documented local outcomes and map each one to executable regression
  evidence. Do not claim an outcome is covered merely because an enum member exists.

### 3. Add an explicit A/B regression-barrier suite

- Add `tests/integration/test_phase16_1_ab_regression_barrier.py` as the named barrier
  that later 16.x phases can run directly.
- Keep A/B-specific setup local to that file unless an existing shared fixture is a
  clear fit. If `tests/conftest.py` must change, add only generic hermetic helpers and
  preserve all existing fixture behavior.
- Use distinct safe UUIDs for controller A, reviewer B, and Cursor chat. Use the
  existing sanitized session rollout and fake CLI infrastructure.
- Add a fake `gh` trap/log and assert it was never invoked. Also assert no PR-review
  worker launcher, `github/` run artifacts, GitHub lifecycle state, commit, or push
  appears during local-only scenarios.
- Cover at least these end-to-end/regression scenarios:
  1. A/B `prepare` with GitHub absent/disabled persists distinct A/B identities,
     frozen runtime, plan/prompt snapshots and hashes, safe JSON output, and the
     controller-owned `launch_command` without invoking an agent.
  2. An actual detached `launch` runs a multi-iteration
     findings -> Cursor fix -> no-findings cycle to `completed`; it creates exactly
     one Cursor chat, reuses that chat for all turns, resumes exact reviewer B for
     every Codex review, never resumes A or uses `--last`, leaves the accepted patch
     staged, and keeps orchestrator state outside the target repository.
  3. Wrong-controller and duplicate-live-launch behavior remains fail-safe and
     idempotent without creating a second worker.
  4. A/B `max_iterations_reached` -> `extend` -> detached `launch` continues the same
     run with the stored final fix prompt, same A/B identities, same Cursor chat,
     monotonic iterations, and the expected final result.
  5. An eligible local failure -> `recover` creates/reuses an immutable successor,
     preserves controller A, reviewer B, Cursor chat, runtime and required artifacts,
     does not invoke agents or mutate Git during recovery, and the controller can
     launch the successor so proven-complete work is not repeated.
  6. Abort during a detached fake Cursor or Codex boundary persists the abort request,
     reaches `aborted` when safe, starts no later action, and preserves repository,
     staged work, identities, artifacts, and privacy surfaces.
- Add smaller direct application-boundary tests in the same file where a detached
  process would add nondeterminism without increasing coverage. Reuse existing fault
  injection for `completed_with_residual_risk`, `interrupted`, and `failed` only as
  needed to make the result matrix explicit.
- Use bounded polling with diagnostic failure output and guaranteed child cleanup;
  do not add unbounded sleeps or leave detached fake workers alive after a failed
  assertion.

### 4. Record current local/legacy coupling without refactoring it

- Add a symbol-level dependency inventory to the findings artifact. At minimum cover:
  - `workflow_engine._run_cursor_turn` and its external-feedback preflight branch;
  - `workflow_engine._apply_review_result` and its direct transition/publication/
    PR-worker branch;
  - the external-feedback prompt, iteration allocation, and local-review-budget
    helpers in `iterations.py`;
  - their dependency on `RunState.github_pr_review` and legacy external statuses;
  - dynamic imports/calls into `commands.pr_review` and the runner/state boundaries
    they activate.
- For each coupling record the local contract that must survive extraction, the
  legacy responsibility that must not move into the reusable loop, and the regression
  evidence protecting the local side.
- This is an inventory, not an implementation design. Do not rename, move, delete,
  wrap, or redirect the dependencies during 16.1.

### 5. Run the barrier and finalize the baseline artifact

- Run the new named barrier by itself, then all focused existing local-loop tests,
  then the complete validation suite.
- Update the findings artifact with post-change results and compare them with the
  pre-change baseline. Record exact counts rather than copying the context document.
- Confirm the phase changed only tests/test helpers, its plan/prompt, and the findings
  artifact. If production/config/schema/rule/global-integration changes became
  necessary, stop and request a plan amendment.
- Review the final diff for accidental sensitive values, brittle private-helper
  assertions, GitHub legacy fixes, sleeps/process leaks, or claims unsupported by a
  passing command.

## Testing Criteria

### Required test levels

- **Integration/regression:** public/application commands with disposable Git repos,
  isolated HOME/XDG state, sanitized session rollouts, real subprocess fake CLIs, and
  detached-worker behavior.
- **Contract:** exact A/B/session/chat identities, command routing, durable
  state/artifacts/hashes, result statuses, redacted outputs, and GitHub-disabled
  negative assertions.
- **Recovery/abort regression:** immutable failed source, idempotent successor,
  resume/launch without repeated completed turns, process-group abort, and repository
  preservation.
- **Static dependency audit:** human-readable symbol-level inventory in the findings
  artifact; no brittle automated source-text snapshot is required.

### Focused commands

Run the new barrier first:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py
```

Then run the existing local-loop regression set:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
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
```

All automated tests must use fakes. No test may invoke real Cursor/Codex models,
GitHub, credentials, SSH, or network calls. If a required command cannot run, record
the exact blocker and do not mark that validation as passed.

## Validation

Run before final handoff:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
uv run python -m build
```

Inspect the final diff and findings evidence to confirm:

- the new suite can be selected and run independently;
- A/B prepare/launch and the local multi-iteration loop are proven with GitHub
  absent/disabled and zero GitHub/PR/publication effects;
- exact controller, reviewer, chat and runtime identities survive every covered
  continuation/recovery path;
- `resume`, `recover`, `extend`, and `abort` retain their documented local semantics;
- every documented local result has executable regression evidence;
- no behavior, schema, config, production source, public docs, global integration,
  or legacy PR-review code changed;
- no real processes/services were contacted and no sensitive content entered normal
  output or the findings artifact;
- pre-change and post-change counts/outcomes are recorded accurately.

## Risks Or Recovery Notes

- Existing coverage is broad but distributed across historical phase files. The new
  barrier should compose and strengthen observable assertions without mechanically
  copying every old test or depending on private function structure.
- Detached-worker tests can leak processes or become timing-sensitive. Use bounded
  waits, inspect launcher state for diagnostics, and terminate only process groups
  unambiguously owned by the disposable test run.
- A passing local loop with no `github` section is currently implicit in shared
  fixtures. The new barrier must make this an explicit assertion, including absence
  of GitHub subprocesses and PR-worker artifacts.
- The recorded pytest cache may contain stale node IDs/failures. Only commands run in
  this phase count as baseline evidence.
- If characterization exposes an existing contract defect, preserve the failing
  evidence and stop. Fixing it would be a separate approved change; weakening the
  test would defeat the purpose of 16.1.
- Do not use the new barrier to preserve legacy PR-review behavior. It protects only
  the local A/B capability and its public operational surfaces.

## OpenQuestions

None.
