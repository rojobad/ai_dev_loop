# Phase 16.8 Gate B Codex Adjudication Schema Correction

## Goals

- Make the PR review v2 external-adjudication JSON Schema compatible with the
  strict structured-output contract enforced by the current Codex API.
- Preserve nullable adjudication fields while requiring their keys to be
  present in model output.
- Add an offline contract regression that detects the exact live failure before
  any real Codex/model invocation.
- Preserve the paused live Gate B run so controller A can resume the same
  adjudication after this correction is accepted and installed.

## Non-Goals

- Do not redesign adjudication decisions, reply semantics, fix prompts, or the
  Pydantic domain models.
- Do not change retry budgets, pause/resume semantics, leases, claims, the
  outbox, or supervisor behavior.
- Do not add a new JSON Schema dependency solely for this correction.
- Do not add real Codex/API validation to automated tests.
- Do not expand this correction into generic stderr persistence or diagnostic
  redesign. Raw Codex stdout/stderr remains protected and must not be exposed
  through operational surfaces.
- Do not create a new PR review run or a new Parish360 PR.
- Do not create the Phase 16.8-to-16.9 handoff until Gate B succeeds.

## Scope

Expected implementation files:

- `src/ai_dev_loop/schemas/pr-review-v2-external-adjudication-v1.json`
- `tests/unit/pr_review_v2/test_codex_local_runners.py`

Additional directly related Phase 16.8 test files may be adjusted only if a
focused test proves that their fake payloads must include the now-required
nullable keys:

- `tests/integration/test_phase16_8_local_codex_boundaries.py`
- `tests/integration/phase16_8_local_engine_helpers.py`

## Out of Scope

- `src/ai_dev_loop/schemas/pr-review-v2-publication-generation-v1.json`; it
  already satisfies the strict required-property contract.
- Production subprocess, reducer, worker, supervisor, control-plane,
  persistence, GitHub gateway, and Cursor carrier code.
- `ai_dev_loop.yaml` or any public configuration/schema.
- The live acceptance checkout
  `/home/rojobad/Projects/parish360-poc-phase16-8-acceptance`.
- GitHub PR `rojobad/parish360-poc#4`.
- Live run `prv2-617edf93c28019564a2a5d51653ba1a1`.
- The user's live ai_dev_loop XDG state and protected Codex session records.
- Any real GitHub, Cursor, Codex, model, or network activity.
- Starting, resuming, aborting, or otherwise controlling Gate B.
- Git staging, commits, pushes, branch changes, resets, cleans, or stashes.

## Required Context

Read before implementation:

1. This complete plan.
2. `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`
3. `archive/implementation-history/plans/phase-16-8-gate-b-supervisor-lease-runtime-correction.md`
4. `src/ai_dev_loop/schemas/pr-review-v2-external-adjudication-v1.json`
5. `src/ai_dev_loop/schemas/pr-review-v2-publication-generation-v1.json`
6. `src/ai_dev_loop/pr_review_v2/infrastructure/codex_local_runners.py`
7. `tests/unit/pr_review_v2/test_codex_local_runners.py`
8. The relevant Phase 16.8 local Codex integration tests.
9. Every applicable `.cursor/rules/*.mdc` file.

Live evidence is included only to define the regression. Cursor must not inspect
the live run:

- The previous supervisor lease correction worked.
- The run recovered the expired `request_bot_review` claim, emitted one trigger,
  observed the Codex review, and reached LOCAL external adjudication.
- The model `gpt-5.6-terra` is available and the exact frozen Codex session
  exists for the correct checkout.
- Six adjudication attempts reached Codex but failed before inference with HTTP
  400:

  `invalid_json_schema: required must include every key in properties; missing reply_body`

- After exhausting attempts, the run durably paused with
  `safe_action_kind=inspect_artifacts`.
- Static inspection also proves that the top-level nullable
  `fix_prompt_text` key is absent from `required` and would be the next strict
  schema violation.
- The publication-generation schema has no missing required properties.

Confirmed cause:

- In the external-adjudication schema, `reply_body` is declared as
  `["string", "null"]` but is omitted from the decision object's `required`
  array.
- `fix_prompt_text` is declared as `["string", "null"]` but is omitted from the
  root object's `required` array.
- Current tests use injected fake Codex runners that write result JSON directly;
  they never submit the checked-in schema to the strict API validator, so the
  incompatibility was not exercised.

## Cursor Rules And Skills

Read and obey every applicable Cursor rule:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-local Cursor skill is required for this narrow schema
correction. If a new `OpenQuestion` would change the output contract, domain
semantics, persistence, privacy, or live recovery behavior, stop before
dependent changes and report it.

## Architecture Guardrails

- The checked-in JSON file passed to `codex exec resume --output-schema` is the
  source of truth for the model response format.
- Strict structured output requires every object property to appear in that
  object's `required` array.
- Optional business meaning is represented by a required key whose value may be
  `null`, not by omitting the key from `required`.
- Preserve `additionalProperties: false` at the root and decision-item levels.
- Preserve the existing decision enum, minimum lengths, `minItems`, and
  nullability.
- Preserve the Pydantic/domain invariants:
  - actionable decisions carry `reply_body: null`;
  - non-actionable/uncertain decisions carry a non-empty reply body;
  - all-actionable adjudication carries a non-empty fix prompt;
  - non-all-actionable adjudication carries `fix_prompt_text: null`.
- Do not weaken schema or parser validation to accept arbitrary fields.
- Tests must validate the checked-in packaged schema, not a copied test-only
  schema.
- No real Codex/model call is permitted. Use static schema inspection and
  injected process fakes.
- Do not expose prompts, thread bodies, replies, session IDs, stderr, or model
  responses in test output or operational documentation.
- The paused live run is durable and recoverable; do not replace or mutate it.
- This correction remains subject to external staged A/B review before the CLI
  is installed and controller A resumes Gate B.

## Implementation Plan

### 1. Correct the external-adjudication schema

Update the root object:

```json
"required": ["decisions", "fix_prompt_text"]
```

Update each decision item:

```json
"required": ["thread_id", "decision", "safe_summary", "reply_body"]
```

Keep:

- `reply_body` typed as `["string", "null"]`;
- `fix_prompt_text` typed as `["string", "null"]`;
- all other constraints unchanged.

Do not mechanically regenerate unrelated schemas.

### 2. Add a strict structured-output contract test

In `tests/unit/pr_review_v2/test_codex_local_runners.py`, load the actual
packaged schema files through the repository's existing `schema_path()` helper.

Add a small test helper that recursively walks schema dictionaries/lists. For
every object schema with `properties`:

- assert `additionalProperties` remains `false` where the Codex output contract
  expects a closed object;
- assert `set(required) == set(properties)`;
- report the schema path/object path clearly on failure.

Run this contract over both schemas used by `codex_local_runners.py`:

- publication generation;
- external adjudication.

This test must fail against the pre-correction adjudication schema with both
missing keys identified.

Do not add a production dependency for this structural check.

### 3. Add explicit nullable-key assertions

Add focused assertions proving:

- the root `fix_prompt_text` key is required and accepts `null`;
- each item `reply_body` key is required and accepts `null`;
- a representative all-actionable result contains both keys with null where
  appropriate and passes the existing Pydantic parser/domain validation;
- a representative non-actionable/uncertain result includes a reply body and
  top-level `fix_prompt_text: null`.

Use no real session IDs or sensitive thread contents in fixtures.

### 4. Preserve runner bindings

Add or retain a focused runner assertion showing that adjudication uses:

- `pr-review-v2-external-adjudication-v1.json`;
- exact session resume, never `--last`;
- the injected fake process boundary.

Do not change argv construction unless a focused test exposes a separate
confirmed defect.

### 5. Run regression validation

Run the focused unit and LOCAL integration suites first. Confirm publication
generation remains green as the sibling structured-output path.

Run the Phase 16.1 A/B barrier and the complete repository suite afterward.

### 6. Preserve the live recovery handoff

Cursor must not execute live recovery.

In the final response, state that after:

1. tests pass;
2. staged A/B review is clean;
3. the correction is committed and pushed;
4. the local `ai_dev_loop` CLI is reinstalled and verified;

controller A may resume the same run once:

```bash
ai_dev_loop pr-review-v2 resume prv2-617edf93c28019564a2a5d51653ba1a1
```

The resumed run must retry the preserved adjudication continuation from attempt
1. It must not prepare/start a new run or repost the already-applied review
trigger.

## Testing Criteria

Automated evidence must prove:

1. Every property in every closed object of both Codex output schemas is listed
   in `required`.
2. `reply_body` is required but nullable.
3. `fix_prompt_text` is required but nullable.
4. The external-adjudication schema retains closed-object and enum constraints.
5. Valid all-actionable and reply/uncertain payload shapes still pass the
   existing parser and domain invariants.
6. Missing required nullable keys are rejected by the checked-in schema
   contract test.
7. Adjudication argv still uses the exact frozen session and external
   adjudication schema, without `--last`.
8. Publication-generation behavior and schema remain unchanged and green.
9. LOCAL retry/pause/cache, privacy, and protected artifact regressions remain
   green.
10. No test invokes real Codex, GitHub, Cursor, models, network, or live XDG
    state.

Avoid assertions that only prove JSON parses. The regression must explicitly
enforce the stricter structured-output rule that caused the live HTTP 400.

## Validation

Run focused validation:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/pr_review_v2/test_codex_local_runners.py \
  tests/integration/test_phase16_8_local_codex_boundaries.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/integration/test_phase16_8_privacy.py \
  tests/integration/test_phase16_1_ab_regression_barrier.py
```

Then run full repository validation:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check \
  tests/unit/pr_review_v2/test_codex_local_runners.py
uv run ruff check .
uv run mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
git status --short
```

The known unrelated formatting debt in
`tests/integration/phase16_8_existing_pr_happy_path_helpers.py` must not be
modified as part of this correction. If full `ruff format --check .` is run, it
may continue to report only that pre-existing non-staged file; run the targeted
format check above for the file changed by this correction and report the
global result honestly.

Cursor must report exact results and skips. Cursor must not stage, commit, push,
install the CLI, or access/control Gate B.

## Risks Or Recovery Notes

- Adding only `reply_body` to the nested `required` array is incomplete:
  `fix_prompt_text` would then become the next API rejection.
- Removing nullable types would break valid business outcomes; require the keys
  while retaining `null`.
- Weak fake-only tests will reproduce the original coverage gap unless the
  checked-in schema itself is recursively inspected.
- A new live run would lose the durable paused continuation and could duplicate
  the already-applied review trigger.
- The current run is safely paused with no live supervisor or active lease.
  Leave it untouched until corrected code is accepted and installed.
- The six failed Codex turns were rejected at schema validation before model
  inference; resuming the same exact session remains the intended continuity
  path.

## OpenQuestions

None.
