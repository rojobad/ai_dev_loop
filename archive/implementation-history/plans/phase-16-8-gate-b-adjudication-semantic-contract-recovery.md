# Phase 16.8 Gate B Adjudication Semantic Contract Recovery

## Goals

- Make the PR review v2 external-adjudication prompt state the exact semantic
  relationships already enforced by `ExternalAdjudicationResultArtifact`.
- Convert a schema-valid but domain-invalid Codex adjudication into a typed,
  privacy-safe `CodexLocalRunnerError` so the LOCAL effect follows its existing
  bounded retry path instead of being blocked as an unexpected exception.
- Add an automated regression for the exact Gate B failure: one actionable
  decision with a non-null `reply_body` and a non-null `fix_prompt_text`.
- Preserve fail-closed domain validation, exact Codex session continuity, and
  recovery of the existing paused Gate B run after external review and CLI
  installation.

## Non-Goals

- Do not weaken, remove, or normalize away the cross-field invariants in
  `ExternalAdjudicationResultArtifact`.
- Do not silently discard an actionable `reply_body`, synthesize missing reply
  text, or rewrite model output into a domain-valid result.
- Do not redesign the adjudication payload or introduce a new schema version.
- Do not change retry limits, lease behavior, supervisor behavior, GitHub
  writes, or the PR review v2 state machine.
- Do not finish Gate B or declare Phase 16.8 complete from this correction turn.

## Scope

Expected production scope:

- `src/ai_dev_loop/pr_review_v2/infrastructure/codex_local_runners.py`
- `src/ai_dev_loop/schemas/pr-review-v2-external-adjudication-v1.json` only if
  concise property descriptions are added without changing its accepted shape
  or strict Structured Outputs compatibility.

Expected automated test scope:

- `tests/unit/pr_review_v2/test_codex_local_runners.py`
- the narrowest existing PR review v2 LOCAL executor/integration test file,
  preferably `tests/integration/test_phase16_8_local_codex_boundaries.py` when
  its fixtures fit the production boundary.

Documentation should change only if current operational documentation claims
that every schema-valid Codex adjudication is necessarily domain-valid or
misstates the retry behavior.

## Out of Scope

- The live PR review v2 XDG database, artifacts, Codex rollout, launcher
  metadata, lease, or paused run.
- The live `parish360-poc` repository, PR, comments, branches, reviews, or
  GitHub state.
- Real Codex, Cursor, GitHub, network, or model calls.
- `ai_dev_loop.yaml`, public configuration schemas, retry counts, worker
  configuration, or model selection.
- Publication generation, Git writes, GitHub writes, reconciliation, carrier
  behavior, and legacy `pr-review`.
- Commit, push, install, start, resume, or abort operations.
- Broad restructuring of `LocalEffectExecutor` or exception handling unrelated
  to external adjudication.

## Required Context

Read current code and tests before editing:

1. `src/ai_dev_loop/pr_review_v2/infrastructure/codex_local_runners.py`
2. `src/ai_dev_loop/pr_review_v2/workers/local_executor.py`
3. `src/ai_dev_loop/pr_review_v2/application/execution_context.py`
4. `src/ai_dev_loop/schemas/pr-review-v2-external-adjudication-v1.json`
5. `src/ai_dev_loop/schemas/pr-review-v2-publication-generation-v1.json`
6. `tests/unit/pr_review_v2/test_codex_local_runners.py`
7. `tests/integration/test_phase16_8_local_codex_boundaries.py`
8. Other existing LOCAL executor tests only when needed to reuse production
   fixtures rather than duplicate them.
9. `archive/implementation-history/plans/phase-16-8-gate-b-codex-adjudication-schema-correction.md`
10. `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`

Sanitized Gate B evidence defining the regression:

- The corrected strict JSON schema was accepted by Codex.
- The model turn completed successfully and returned both required top-level
  keys and all required decision keys.
- The frozen thread set matched exactly.
- The single decision was `actionable`.
- Both `reply_body` and `fix_prompt_text` were non-null.
- `ExternalAdjudicationResultArtifact` correctly rejected the result with the
  invariant `all-actionable adjudication forbids reply_body values`.
- That Pydantic domain validation escaped the runner-specific error boundary;
  `LocalEffectExecutor` caught it as an unexpected exception, persisted the
  generic summary `local effect failed`, and blocked the resumed effect at
  attempt 1 instead of using the existing bounded retry path.

Cursor must use only this sanitized evidence. It must not inspect the live run
or protected artifacts.

## Cursor Rules And Skills

Read and follow:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`

No repository-local Cursor skill is required for this narrow correction. If a
rule conflicts with current production code or current documentation in a way
that changes safety or recovery behavior, stop and report an `OpenQuestion`
before making dependent changes.

## Architecture Guardrails

- `ExternalAdjudicationResultArtifact` remains the source of truth for
  cross-field adjudication semantics.
- Preserve these invariants exactly:
  - `actionable` decisions require `reply_body: null`;
  - `not_applicable` and `uncertain` decisions require non-empty
    `reply_body`;
  - an all-actionable result requires non-empty `fix_prompt_text`;
  - a result containing any non-actionable decision requires
    `fix_prompt_text: null`;
  - decisions cover the frozen thread set exactly and without duplicates.
- Structured Outputs remains responsible for shape; Pydantic/domain validation
  remains responsible for semantic relationships that the accepted schema
  subset does not fully encode.
- Do not coerce, discard, repair, reinterpret, or synthesize model output.
  Domain-invalid output must fail closed.
- A domain-invalid model result is a model/LOCAL result failure, not an
  unsupported effect, state corruption, or generic Python failure.
- Convert the domain validation failure to a constant, privacy-safe
  `CodexLocalRunnerError`; never place raw Pydantic errors, model text,
  summaries, replies, prompts, full thread IDs, or session IDs in operational
  status/history/error surfaces.
- Preserve the existing `LocalEffectExecutor` rule that
  `CodexLocalRunnerError` is retryable only while `attempt < max_attempts` and
  becomes a bounded operator-visible block at exhaustion.
- Preserve exact `codex exec resume` session identity and never introduce
  `--last`.
- Keep schema properties required and nullable exactly as established by the
  preceding correction. Any descriptions added to the schema must remain
  compatible with strict Structured Outputs and must not alter its payload
  shape.
- Use injected/fake Codex process boundaries in tests. Never invoke a real
  model.
- Do not mutate Git state from Cursor: no commit, push, reset, clean, stash,
  checkout, branch, merge, rebase, or tag operations.

## Implementation Plan

### 1. Make adjudication semantics explicit to Codex

Update the deterministic adjudication wrapper prompt in
`codex_local_runners.py` to state all four output rules plainly:

1. `decision == "actionable"` requires `reply_body: null`.
2. `decision in {"not_applicable", "uncertain"}` requires a non-empty
   `reply_body`.
3. If every decision is actionable, `fix_prompt_text` must be a non-empty
   string.
4. If any decision is non-actionable, `fix_prompt_text` must be `null`.

Also retain:

- exactly one decision per frozen thread;
- no additional or omitted frozen thread;
- schema-constrained JSON only.

Keep the prompt deterministic and operational. Do not include live evidence,
raw findings, or the previous invalid model response.

Optionally add matching concise `description` fields to `reply_body` and
`fix_prompt_text` in the checked-in external-adjudication schema if this helps
the model comply. Do not use unsupported conditional schema keywords or alter
the already-correct required/nullable contract.

### 2. Keep domain validation inside the runner error boundary

In `ThreadAdjudicationRunner.adjudicate()`:

- include construction of `ExternalAdjudicationResultArtifact` in a focused
  exception boundary;
- catch the Pydantic validation failure raised by domain/cross-field
  invariants;
- raise a chained `CodexLocalRunnerError` with a constant safe message such as
  `codex adjudication result failed domain validation`;
- preserve existing typed handling for missing files, invalid JSON, payload
  schema validation, timeouts, nonzero exits, size bounds, and scratch cleanup.

Do not broaden catches so programming defects, filesystem corruption, or
unrelated exceptions are mislabeled as model validation failures.

### 3. Add the exact unit regression

Use `FakeCodexProcessRunner` to return:

- one frozen actionable decision;
- all required keys;
- a non-null `reply_body`;
- a non-null `fix_prompt_text`.

Prove:

- the strict payload parser accepts the shape;
- domain construction rejects the semantic contradiction;
- `ThreadAdjudicationRunner` exposes only the typed privacy-safe
  `CodexLocalRunnerError`;
- no raw reply, summary, prompt, session ID, or Pydantic input is included in
  the surfaced error.

Add assertions over `fake.last_stdin` proving the four semantic rules are sent
to Codex. Prefer stable semantic substrings over a brittle assertion of the
entire prompt.

Retain or add valid cases for:

- all actionable with all `reply_body` values null and a non-empty
  `fix_prompt_text`;
- at least one non-actionable decision with a non-empty reply and
  `fix_prompt_text: null`.

### 4. Prove bounded LOCAL retry classification

Through the production `LocalEffectExecutor` boundary and injected fake
runner/store fixtures, prove that the same domain-invalid result:

- produces `EffectRetryableFailure` before `max_attempts`;
- uses the existing safe transient/local failure classification;
- schedules a later attempt without completing the effect;
- does not persist an adjudication result artifact;
- does not leak model content into the safe error summary.

At exhaustion, prove it becomes the existing bounded
`EffectBlocked`/operator-action result with the typed safe summary rather than
`local effect failed`.

Do not implement an unbounded retry or change configured attempt counts.

### 5. Run focused and full regression validation

Run focused runner and LOCAL tests first. Then run the Phase 16.1 barrier, PR
review v2 unit/integration suites, and the complete repository validation.

### 6. Preserve live recovery ownership

Cursor must stop after code, tests, documentation if required, and validation.
It must not inspect or control the live run.

After external staged review, commit/push, and verified CLI reinstall,
controller A may resume the same paused Gate B run once. The controller must
not prepare/start a replacement run, repost the existing review trigger, or
maintain polling.

## Testing Criteria

Automated evidence must prove:

1. The deterministic adjudication prompt includes all four semantic rules.
2. The checked-in external schema still requires every property and keeps
   `reply_body`/`fix_prompt_text` nullable.
3. A schema-valid `actionable + non-null reply_body` result reproduces the
   domain rejection.
4. The runner converts that rejection to a constant, privacy-safe
   `CodexLocalRunnerError`.
5. The same failure is retryable before exhaustion and blocked only at the
   existing maximum attempt.
6. No invalid adjudication result artifact is persisted or reused.
7. Valid all-actionable and non-actionable result shapes still succeed.
8. Frozen thread coverage, exact session resume, no `--last`, size limits, and
   scratch cleanup remain intact.
9. Status/history/error surfaces contain no raw model output, replies, prompts,
   Pydantic input, full thread IDs, or full session IDs.
10. Tests use only injected fake process boundaries and temporary XDG/storage;
    no real Codex, Cursor, GitHub, network, credentials, or live run state.

## Validation

At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/pr_review_v2/test_codex_local_runners.py \
  tests/integration/test_phase16_8_local_codex_boundaries.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_8_privacy.py \
  tests/integration/test_phase16_1_ab_regression_barrier.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/pr_review_v2 \
  tests/integration/test_phase16_8_*.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
```

If global `ruff format --check .` still reports only the previously known
unrelated formatting debt in
`tests/integration/phase16_8_existing_pr_happy_path_helpers.py`, report it
honestly and do not modify that unrelated file solely to make this correction
green. All files changed by this correction must pass formatting checks.

## Risks Or Recovery Notes

- Prompt-only correction is insufficient because any future domain-invalid
  output would still be mislabeled and immediately blocked.
- Error-classification-only correction is insufficient because identical
  deterministic prompts may consume every retry with the same contradiction.
- Silently setting actionable `reply_body` to null would make the happy path
  advance but would weaken auditability and conceal model-contract drift.
- Adding complex conditional schema constructs risks another live
  `invalid_json_schema` failure; keep this correction within the already
  accepted strict schema subset.
- The existing paused run remains the recovery source. Do not create a new run
  or mutate its protected state during implementation or tests.
- After installation, one controller-owned resume should reuse the exact Codex
  session and frozen adjudication continuation. If it pauses again, inspect
  status/history and protected failure evidence before another resume.

## OpenQuestions

None.
