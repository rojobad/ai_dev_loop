# Phase 13.5 - Adopt Historical Cursor Usage-Limit Failures

## Goal

Extend the Phase 13 Cursor usage-limit recovery flow so an explicitly authorized user
can recover a historical run that failed **before** Phase 13 persisted
`failure_code: cursor_usage_limit` and its post-failure content fingerprint.

The target outcome is to recover this evidence-only incident after the implementation is
reviewed and installed:

```text
crypto-sentinel-20260712T205916Z-b2d828
```

The resulting successor must preserve the exact Cursor chat, freeze an explicitly
selected fallback model such as `auto`, create the normal Phase 13 continuation envelope,
and resume the incomplete Cursor iteration. It must not create a new Cursor chat or edit
the source run.

The explicit user command after implementation will be:

```text
ai_dev_loop recover crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output \
  --cursor-model auto
```

The user then runs the returned `ai_dev_loop resume <recovery-run-id>` command.

## Why This Is Needed

Phase 13 correctly requires durable contemporaneous evidence for normal usage-limit
recovery:

- cursor iteration metadata containing `failure_code: cursor_usage_limit`;
- `git/cursor-output/NN.usage-limit-failure.json` captured immediately after failure;
- a content fingerprint that proves the current partial work exactly matches what Cursor
  left behind.

The motivating run predates that behavior. It has a nonzero Cursor iteration,
`cursor/iterations/01/stderr.txt` containing the Cursor `ActionRequiredError` usage-limit
response, `git/status/01-after-cursor.txt`, the exact source prompt, and the persisted
Cursor chat ID, but it has neither the structured failure code nor a contemporaneous
content fingerprint. Current `recover --dry-run` therefore reports
`cursor_turn_incomplete`, and `--cursor-model auto` is rejected before it can identify a
usage-limit checkpoint.

There is no cryptographic way to prove now that current file contents are identical to
the historical worktree merely from matching status paths. This phase must preserve that
distinction. It introduces a **user-attested legacy adoption**, never an automatic
recovery or a claim that historical content was cryptographically verified.

## Non-Goals

- Do not weaken normal Phase 13 recovery. New runs must still require their durable
  `failure_code` and contemporaneous failure fingerprint.
- Do not edit, backfill, or otherwise mutate the historical source run to make it look
  like a Phase 13 run.
- Do not automatically offer this adoption from `start` or `resume`; it is only an
  explicit `recover` action after the run is already terminal.
- Do not recover ordinary nonzero Cursor exits, timeouts, auth failures, missing model
  failures, malformed output, or generic `ActionRequiredError` messages.
- Do not use `RunState.last_error` as adoption evidence.
- Do not accept a matching `git status` as proof of matching file contents. It is only a
  prerequisite to an explicit user attestation.
- Do not stage, unstage, discard, reset, clean, stash, commit, or otherwise mutate the
  target repository from `recover`.
- Do not create a new Cursor chat, invoke Cursor/Codex/updaters from `recover`, or
  bypass the usual staging/Codex review loop after the successor's Cursor turn succeeds.
- Do not modify or schedule Phase 14 work, including
  `plan-14-remote-controller-and-review-fork.md` and its prompt.
- Do not run real model calls, recover/resume the motivating run, update CLIs, commit,
  push, or release while implementing or testing this phase.

## Scope

- Legacy usage-limit evidence classification from protected source artifacts.
- Explicit extension of `--adopt-current-cursor-output` to the historical Phase 13
  cursor checkpoint.
- Safe generation and persistence of an adoption-time fingerprint in the successor.
- Recovery state/schema/artifact/idempotency support for legacy cursor adoption.
- Dry-run, status, inspect, logs, CLI help, docs, rules, and fake integration tests.
- Full validation, package build, and WSL `ai_dev_loop` installation.

## Required Context

Read before editing:

- `archive/implementation-history/master-plan.md`
- `plan-13-cursor-usage-limit-auto-recovery.md` if retained, otherwise the committed
  Phase 13 implementation/docs/tests as the source of truth.
- `archive/implementation-history/plans/phase-11-recover-failed-runs.md`
- `archive/implementation-history/plans/phase-12-cursor-index-mutations-and-staging-recovery.md`
- `src/ai_dev_loop/runners/cursor_failure.py`
- `src/ai_dev_loop/runners/cursor_output.py`
- `src/ai_dev_loop/recovery_planner.py`
- `src/ai_dev_loop/commands/recover.py`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/iterations.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- `src/ai_dev_loop/cli.py`
- `src/ai_dev_loop/resume_planner.py`
- `tests/integration/test_phase11_recover.py`
- `tests/integration/test_phase12_index_mutations_and_staging_recovery.py`
- `tests/integration/test_phase13_cursor_usage_limit_recovery.py`
- Existing state/schema, privacy, docs, fake-CLI, recovery, and workflow tests.

Read the current user-facing recovery documentation before modifying it. Existing
`--adopt-current-cursor-output` language refers only to historical staging failure; this
phase deliberately broadens it, but only under the contracts below.

## Cursor Rules And Skills

Cursor must follow all repository-local rules, especially:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use relevant repository-local documentation/acceptance skills. Update affected rules in
the same change.

## Safety And Recovery Contract

### Eligibility

Legacy adoption applies only when all of the following are true:

1. The source is terminal `failed`, has a valid persisted Cursor chat ID, valid
   repository identity, plan/prompt contracts, and a latest incomplete iteration.
2. The source iteration metadata proves a nonzero exit and `timed_out` is false.
3. The protected source `cursor/iterations/NN/stderr.txt` exists and the same narrow
   Phase 13 classifier recognizes it as `cursor_usage_limit`. If Phase 13 supports a
   structured stream-json error artifact, it may be corroborating evidence, but raw
   stderr remains required for this legacy path.
4. `git/status/NN-after-cursor.txt` exists and exactly matches current normalized
   porcelain status. Do not compare human-readable output loosely.
5. The exact source prompt can be resolved and hashed: `prompts/cursor-initial.txt` for
   iteration 1 or the exact prior Codex findings artifact for a correction.
6. The current repository still matches root, Git common dir, Git dir, branch, and HEAD
   captured at prepare. Recompute the current binary-safe Cursor-output fingerprint and
   reject unsupported/special files or unreadable content.
7. For a correction iteration, require all existing Phase 13 correction prerequisites:
   prior review result, exact findings prompt, previous staged patch, and correction
   checkpoint integrity.
8. The user supplies **both** `--adopt-current-cursor-output` and a non-empty
   `--cursor-model <model>`.

The classifier must operate on the original protected subprocess evidence, not on a
user-editable status line, normal log prose, or `last_error`.

### Attestation Semantics

`--adopt-current-cursor-output` means:

> I attest that the current repository contains the partial work left by the recorded
> Cursor turn, and I accept that the orchestrator can verify exact current status and
> current content only from recovery time, not prove the historical interval.

The command must make this visible in text and JSON output as a legacy adoption. It must
not say the historical output fingerprint was verified.

Without the flag, dry-run must identify the prospective `cursor` checkpoint and return a
stable blocker/warning such as:

```text
legacy_cursor_usage_limit_requires_explicit_adoption
```

The message must direct the user to rerun with both flags after confirming that they did
not manually alter the repository since Cursor stopped.

### Successor Evidence

The source remains untouched. During actual recovery, after rechecking all conditions
under the standard locks, write a new sensitive adoption artifact in the **successor**,
for example:

```text
git/cursor-output/01.usage-limit-adopted.json
```

It must record only safe operational metadata:

- the current Phase 13-style content fingerprint and aggregate SHA-256;
- source run ID/iteration and source artifact relative paths;
- hashes of the source metadata, stderr, after-Cursor status, and source prompt;
- the fallback model requested by the user;
- `legacy_cursor_usage_limit_adopted: true` and timestamp.

Do not copy file contents, stderr contents, prompt text, chat IDs, account/billing data,
or raw model error prose into this artifact. Preserve restrictive permissions and include
it in the successor manifest.

The successor's recovery lineage must distinguish:

- normal Phase 13 `usage_limit_fingerprint` captured at failure time;
- legacy usage-limit adoption fingerprint captured during recovery;
- existing Phase 12 `legacy_cursor_output_adopted` staging adoption.

Do not overload the Phase 12 boolean so inspection remains unambiguous. Add a typed
cursor-checkpoint field, for example `legacy_cursor_usage_limit_adopted`, whose true
value is valid only for `recovered_checkpoint == "cursor"` and
`reason_code == "cursor_usage_limit"`.

### Workflow Continuity

The successor must:

- preserve the exact source `cursor.chat_id` and never call `create-chat`;
- freeze the requested fallback model in `successor.cursor.model`;
- preserve frozen Codex session/runtime unchanged;
- write the existing Phase 13 continuation envelope, embedding the source prompt
  exactly and instructing Cursor to inspect/preserve partial work;
- resume the same incomplete iteration; and
- run normal pre-Cursor identity checks, successful post-Cursor staging, and Codex review
  only after Cursor returns successfully.

`recover` itself remains read-only to the target repository and never invokes agents.

### Idempotency

For this checkpoint, matching successor identity includes source run, iteration,
checkpoint, source prompt hash, adoption-time fingerprint hash, requested fallback
model, and `legacy_cursor_usage_limit_adopted` state.

Repeated identical adoption reuses the active matching successor. A request for another
model cannot create a parallel successor while one is active; recover the failed
successor in a new lineage generation instead. Never skip recovery generations.

## Implementation Plan

### 1. Separate normal and legacy usage-limit analysis

Refactor `_analyze_cursor_usage_limit_checkpoint` in `recovery_planner.py` into clear
normal and legacy paths, sharing only trusted validation helpers:

- Keep the normal Phase 13 path unchanged: it requires `failure_code`, source
  fingerprint artifact, and fingerprint equality.
- When the structured code/fingerprint are absent, inspect legacy metadata and protected
  stderr. Recognize the legacy path only if it satisfies the Eligibility contract.
- Return a prospective `checkpoint="cursor"` and `reason_code="cursor_usage_limit"`
  for a narrowly proven legacy usage-limit candidate even before adoption is supplied.
  This lets `recover --dry-run --cursor-model auto` report the right reason rather than
  incorrectly rejecting the model option as unrelated.
- Without adoption, add the stable explicit-adoption blocker and warning; do not set
  `eligible=true`.
- With adoption, recompute current content evidence but do not write it during analysis.
  Place the recomputed fingerprint and source-artifact hashes in an internal typed
  analysis result so the real recovery can write them only after locked revalidation.
- Continue to reject ordinary incomplete Cursor turns as `cursor_turn_incomplete`.

Create focused helpers for reading a protected legacy stderr artifact, checking legacy
metadata, hashing source evidence, and comparing current status. Do not duplicate regex
logic; use the existing Phase 13 failure classifier.

### 2. Extend recovery models and schema precisely

Update `RecoveryAnalysis`, `RecoveryState`, recovery result structures,
`run-state-v1.json`, serialization/loading tests, fixtures, and manifest handling.

- Add optional typed fields for legacy usage-limit adoption and adoption artifact
  path/hash, valid only for the `cursor` recovery checkpoint.
- Require normal cursor recovery to carry its Phase 13 source fingerprint and reject the
  adoption fields there.
- Require legacy cursor adoption to carry the successor adoption artifact, aggregate
  fingerprint hash, source-prompt evidence, fallback model, and explicit adoption flag.
- Keep `legacy_cursor_output_adopted` exclusive to the Phase 12 staging checkpoint.
- Preserve historical state compatibility. Existing completed/failed runs without these
  fields must still load; only newly created successors require the appropriate fields.
- Update matching-successor and conflicting-successor checks with the explicit legacy
  adoption discriminator.

Do not weaken the required/non-null contracts of staging/review recovery fields.

### 3. Write the successor adoption artifact transactionally

Extend `commands/recover.py`:

- Add the required source evidence files to the cursor-checkpoint copy selection without
  exposing their contents in output.
- On actual legacy recovery, re-run analysis under source/repository locks. If status or
  fingerprint changed since dry-run, refuse before successor creation.
- Create the normal successor layout and continuation envelope, then write the adoption
  artifact from the locked analysis data in the successor before writing final state and
  manifest.
- Use existing atomic/sensitive write helpers. Clean up a partially created successor on
  failure using the same transaction/cleanup rules as existing recovery creation.
- Populate typed lineage and safe events, including a stable event such as
  `cursor_usage_limit_legacy_output_adopted`.
- Ensure the successor workflow resolves its fingerprint from its own adoption artifact
  when it validates a usage-limit recovery pre-Cursor boundary.

### 4. Extend CLI and rendering contracts

Keep the existing flag name but broaden its accurate help text:

```text
--adopt-current-cursor-output
  Explicitly attest to matching current output for historical staging failures or
  historical Cursor usage-limit failures that lack a contemporaneous fingerprint.
```

Implement all combinations deterministically:

| Source checkpoint | `--adopt-current-cursor-output` | `--cursor-model` | Result |
| --- | --- | --- | --- |
| Normal Phase 13 cursor usage limit | absent | required | Normal fingerprint recovery |
| Normal Phase 13 cursor usage limit | present | required | Reject redundant/invalid adoption |
| Historical cursor usage limit | absent | optional for dry-run / required for recovery | Show explicit-adoption blocker; no successor |
| Historical cursor usage limit | present | absent | Reject: fallback model required |
| Historical cursor usage limit | present | supplied | Eligible only after every legacy check passes |
| Historical staging | present | absent | Preserve Phase 12 behavior |
| Historical staging | any | supplied | Preserve rejection of `--cursor-model` |
| Review/process-review | any | supplied or adoption flag | Preserve existing rejection behavior |

For text/JSON `recover --dry-run` and success output, label the result clearly as
`legacy_usage_limit_adoption` and distinguish:

- `Current repository status: matches recorded after-Cursor status`;
- `Current content fingerprint: captured at recovery time`;
- `Historical content fingerprint: unavailable; explicit user adoption recorded`.

Never print raw stderr, prompt contents, full chat IDs, billing information, source
artifact contents, or current fingerprint path contents. Do not trigger the interactive
start/resume fallback prompt for an old run; this path stays explicit.

### 5. Status, inspect, logs, rules, and docs

Update:

- `status` next safe action for a legacy-adoptable source;
- `inspect` recovery lineage and artifact classification;
- `list`, logs, events, and redaction tests;
- recovery/rules contracts so no future change treats adoption as historical proof;
- README, CLI reference, quick guide, execution/recovery guide, troubleshooting,
  observability, privacy/security, artifacts/state, and phase traceability docs.

Document the exact post-installation sequence for the motivating incident, but state
that it is a user-run operational command and must never be executed as implementation
validation:

```text
ai_dev_loop recover crypto-sentinel-20260712T205916Z-b2d828 \
  --dry-run \
  --adopt-current-cursor-output \
  --cursor-model auto

ai_dev_loop recover crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output \
  --cursor-model auto

ai_dev_loop resume <recovery-run-id>
```

Explain that the user must not manually edit the repository between dry-run and actual
recovery; actual recovery revalidates and will refuse drift.

### 6. Tests

Use fake CLIs and temporary repositories only. Add or extend tests for:

1. A normal Phase 13 usage-limit source remains recoverable without adoption and rejects
   the adoption flag.
2. A synthetic pre-Phase-13 usage-limit source with valid metadata/stderr/status/prompt/
   chat becomes a prospective cursor checkpoint, but is ineligible without explicit
   adoption.
3. The same synthetic source becomes eligible only with both adoption and fallback model,
   and creates a successor preserving chat/model/prompt/runtime.
4. Legacy stderr classifications: canonical error succeeds; generic limit, generic
   `ActionRequiredError`, missing stderr, timeout, zero exit, malformed metadata, and
   non-usage error all remain nonrecoverable.
5. Status mismatch, file-content mutation with same status, index mutation, branch/HEAD/
   repository identity drift, changed plan/prompt, source chat mismatch, missing source
   evidence, unsupported special files, and failed fingerprint calculation all refuse.
6. `recover --dry-run` creates no source/successor artifacts, locks, Git changes, or
   agent calls. Actual recovery writes only the successor run store, never source or
   target-repository files/index.
7. Adoption artifact has safe hashes/metadata, restrictive permissions, manifest entry,
   and no raw stderr/prompt/billing/account content.
8. Successor invokes Cursor with exact `--resume <same-chat-id>` and `--model auto`,
   uses the stored continuation envelope, and then follows normal staging/Codex review
   after the fake Cursor succeeds.
9. Repeated matching adoption reuses its successor; conflicting fallback model cannot
   create a parallel successor; chained successor recovery stays explicit.
10. Existing Phase 11/12 recovery, legacy staging adoption, non-adopted Phase 13
    recovery, CLI output/privacy, schema compatibility, and interactive current-run
    fallback tests remain unchanged.

## Validation And Delivery

Run the established repository checks:

```text
uv run ruff format --check
uv run ruff check
uv run mypy src
uv run pytest -q -s
uv run python -m build
uv run mkdocs build --strict
```

Also run focused Phase 11-13.5 recovery, state/schema, CLI, artifact/privacy, and fake
end-to-end tests. Verify help after installation:

```text
uv run ai_dev_loop recover --help
uv tool install --force .
ai_dev_loop recover --help
```

Report changed files grouped by recovery analysis, state/schema, successor creation,
CLI/rendering, tests/rules/docs; full validation results; build artifacts; installed WSL
path/version; and explicit confirmation that no real run, real agent, or updater was
invoked during validation.

## Acceptance Criteria

- Historical Cursor usage-limit evidence can be recovered only with explicit user
  adoption and an explicit fallback model.
- The successor preserves the exact Cursor chat and partial work while accurately
  recording that historical content could not be proven before recovery time.
- Normal Phase 13 recovery remains strict and does not silently use adoption.
- `recover` stays read-only to the target repository and source run; only a subsequent
  `resume` invokes Cursor and normalizes staging.
- All old recovery flows remain compatible, sensitive information stays protected, and
  the full validation suite passes.
