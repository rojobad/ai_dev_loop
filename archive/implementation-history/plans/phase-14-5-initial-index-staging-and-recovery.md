# Phase 14.5 - Initial Index Staging And Recovery

## Goal

Correct the initial-iteration staging contract so an empty index is required at the
trusted pre-Cursor boundary, not reinterpreted after Cursor completes. Cursor may
mutate the index during the initial implementation turn just as it may during a
correction turn. `ai_dev_loop` must still normalize with `git add -A`, persist the
complete staged snapshot, and have Codex review that snapshot.

Extend failed-run recovery so an initial Cursor-complete/staging-incomplete run can
create an immutable staging successor and continue directly with normalization and
Codex review. The real incident
`crypto-sentinel-20260715T111106Z-1dcdd9` is acceptance evidence only; this phase
must not mutate, recover, resume, unstage, or otherwise alter it.

## Non-Goals

- Do not make Cursor the authoritative final stager; `ai_dev_loop` still runs the
  configured `stage_mode: all` normalization.
- Do not allow Cursor or the orchestrator to commit, amend, reset, checkout/switch,
  stash, clean, merge, rebase, tag, push, or change repository identity.
- Do not relax the clean-index/worktree baseline required before the first Cursor
  turn, nor the strict patch checkpoint before a correction Cursor turn.
- Do not add selective staging modes, path exclusions, automatic unstaging, or
  changes to `stage_mode` configuration.
- Do not alter recovery for incomplete Cursor turns, review checkpoints, usage-limit
  recovery, external integrations, or target-application code.
- Do not invoke real Cursor/Codex models, update CLIs, install packages globally, or
  touch the real incident run during implementation or tests.

## Scope

- Initial post-Cursor staging validation and its tests.
- Staging-checkpoint recovery analysis, successor lineage/state/schema validation,
  and idempotent resume for initial iteration failures.
- Current product docs, CLI/recovery documentation, and relevant Cursor contracts.
- Unit and fake-CLI integration/regression tests.

## Out of Scope

- `/home/rojobad/Projects/crypto-sentinel`, including its Phase 10 plan/prompt and
  the already staged 11-file implementation.
- Mutating historical runs or adding migration code that rewrites their source
  artifacts.
- Changing target-repository prompt text supplied by users. A target prompt may ask
  Cursor to stage; that action must be harmless under the corrected contract.
- Any new external service, database, network, desktop bridge, or GitHub workflow.

## Required Context

Read before implementation:

- `docs/`, especially `docs/operacion/prepare-start-resume-abort.md`,
  `docs/operacion/seguridad-privacidad.md`, `docs/operacion/troubleshooting.md`,
  `docs/operacion/estado-artefactos.md`, and `docs/referencia/cli.md`.
- `archive/implementation-history/master-plan.md` and
  `archive/implementation-history/plans/phase-12-cursor-index-mutations-and-staging-recovery.md`.
- `src/ai_dev_loop/runners/staging.py`, `src/ai_dev_loop/runners/git.py`,
  `src/ai_dev_loop/workflow_engine.py`, `src/ai_dev_loop/resume_planner.py`,
  `src/ai_dev_loop/recovery_planner.py`, `src/ai_dev_loop/commands/recover.py`,
  `src/ai_dev_loop/state.py`, and `src/ai_dev_loop/schemas/run-state-v1.json`.
- `tests/integration/test_start_staging.py`,
  `tests/integration/test_phase12_index_mutations_and_staging_recovery.py`,
  `tests/unit/test_staging_correction.py`, `tests/unit/test_recovery_planner.py`,
  and the fixture/fake-CLI helpers they use.
- The incident artifacts only as read-only evidence: baseline status is index-empty;
  iteration 1 Cursor completed successfully; its post-Cursor fingerprint records a
  nonempty cached diff and empty worktree diff; staging then failed before any Codex
  review because Cursor had staged the changes.

## Cursor Rules And Skills

Follow all repository-local rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-root `AGENTS.md` or repo-local `.cursor/skills/` directory is present.
Use the rules above; no additional Cursor skill is required for this implementation.

## Architecture Guardrails

- The first pre-Cursor boundary is the sole place that rejects a pre-existing staged
  index. It must run before an agent can modify the repository and remain tied to the
  prepared baseline/identity checks.
- Once Cursor has started, the post-Cursor staging pass must not infer who staged an
  index entry. An index mutation is permitted in initial and correction turns.
- The orchestrator must always validate root, common Git dir, Git dir, branch, HEAD,
  plan hash, prompt-source protection, and safe staged paths; it must always run
  `git add -A` for `stage_mode: all` before Codex reviews the cumulative patch.
- The completed, normalized staged patch remains the only patch Codex reviews and
  the only next-correction checkpoint. Codex decides findings; the orchestrator does
  not reinterpret the review.
- Before every correction Cursor turn, retain exact equality with the preceding
  recorded staged patch and reject tracked unstaged/untracked non-ignored drift.
- Recovery is checkpoint- and artifact-driven, never based on parsing `last_error`.
  A source run remains terminal and byte-for-byte immutable; only a distinct
  successor may be `interrupted` and later resumed.
- An initial-staging recovery requires the current repository to match the protected
  post-Cursor fingerprint and must preserve exact Cursor chat, Codex session, frozen
  review runtime, prompt/plan contracts, repository identity, and iteration number.
  It has no prior staged patch by definition.
- `recover` is read-only with respect to the target repository/index. Only explicit
  successor `resume` runs staging; it must not rerun the already-complete Cursor turn.
- Keep all state/schema/artifact changes atomic, relative to the run directory, and
  sensitive where existing conventions require it. Default output must not expose
  prompts, patches, raw agent output, or full session IDs.

## Implementation Plan

### 1. Move the initial index check to the correct boundary

- Trace the existing prepare/start preflight so that the testable invariant is
  explicit: a staged index present before the first Cursor process starts is rejected.
  Preserve that behavior and its failure persistence.
- In `validate_pre_staging` / `run_git_staging`, remove the iteration-1 post-Cursor
  call that rejects `repo_info.staged_paths`. Do not replace it with a post-Cursor
  “Cursor must not stage” test.
- Retain all post-Cursor identity, plan-hash, prompt-source, status safety,
  `git add -A`, clean-after-normalization, fingerprint, and safe-path checks.
- Preserve the existing correction pre-Cursor patch-equality boundary. Confirm no
  code path weakens it while making the initial and correction post-Cursor behavior
  consistent.
- Update the loop/resume contract wording: reject pre-existing staging before the
  first Cursor turn, but allow index mutation after any successful Cursor turn.

### 2. Support initial staging recovery as a distinct, typed case

- Generalize staging recovery analysis so a completed iteration 1 with incomplete
  staging can be eligible when the current repository matches its persisted
  `git/cursor-output/01.json` fingerprint.
- Add a dedicated reason code, `initial_staging_failed`. Keep the checkpoint value
  `staging`; do not overload `correction_staging_failed`.
- Update `RecoveryState`, `RECOVERY_REASON_CODES`, the run-state JSON schema, and
  schema/model-alignment tests. For `initial_staging_failed`, require:
  - `source_iteration == 1`;
  - a valid post-Cursor fingerprint hash;
  - `source_staged_patch_sha256 is null` and
    `previous_staged_patch_sha256 is null`.
- Preserve the existing correction case unchanged: it requires iteration >= 2 and
  an equal source/previous staged-patch hash. Reject invalid combinations rather
  than silently treating an initial run as a correction.
- Update recovery planning, successor creation, lineage serialization, idempotency
  lookup, and human/JSON reporting so a verified initial case creates an
  `interrupted` successor at iteration 1. Copy only deterministic Cursor/status/
  fingerprint evidence; do not fabricate a staged diff or review artifact.
- Verify the existing resume planner routes that successor to staging, then Codex
  review, without a new Cursor process. Adjust it only if the initial no-prior-patch
  state exposes an actual gap.

### 3. Update documentation and contracts with implemented behavior

- Update only current documentation to describe initial staging recovery alongside
  correction staging recovery, including the fingerprint/identity requirements and
  the fact that `recover` does not alter the source or Git index.
- Update CLI reference, operations guide, troubleshooting, artifacts/state
  reference, and phase traceability as appropriate. Do not describe the feature as
  available until code and tests prove it.
- Ensure examples use sanitized run IDs and do not contain the real incident’s full
  session ID, raw Cursor output, or patch content.

### 4. Add regression coverage

- Replace the obsolete initial `stage_self` rejection expectation with an integration
  test that Cursor stages its own initial output, the orchestrator still normalizes
  it, persists `01.patch`, reaches fake Codex review, and completes with the expected
  staged snapshot.
- Keep/add a separate pre-Cursor test proving a user-preexisting staged index is
  rejected before Cursor is invoked.
- Add unit coverage for initial post-Cursor staging validation: staged index is
  allowed, while protected prompt modification, plan mismatch, repository identity
  drift, and unsafe paths remain rejected.
- Add fake-CLI integration recovery coverage that creates an iteration-1
  Cursor-complete/staging-incomplete source with a matching fingerprint. Assert
  `recover --dry-run` is eligible with `checkpoint: staging` and
  `reason_code: initial_staging_failed`; `recover` changes neither source nor target
  Git state; successor `resume` runs no Cursor turn, normalizes staging, invokes fake
  Codex, and completes.
- Add negative/recovery-state tests for fingerprint drift, missing/invalid
  fingerprint, staged/previous-hash fields incorrectly supplied for the initial
  reason, iteration != 1, repository identity drift, and idempotent reuse of an
  equivalent successor.
- Preserve existing correction-recovery and usage-limit-recovery tests unchanged in
  meaning; run them to prove no regression.

## Testing Criteria

- Unit: staging boundary helpers, recovery analysis, `RecoveryState` validation,
  reason-code/schema alignment, and status/identity/fingerprint rejection paths.
- Integration/regression: fake `agent` and `codex` only; no real model calls. Cover
  initial self-staging success, pre-Cursor staged rejection, initial recovery and
  resume-without-Cursor, correction recovery, and legacy/usage-limit recovery.
- Commands, after focused tests pass:

```bash
uv run python -m pytest -q tests/integration/test_start_staging.py \
  tests/integration/test_phase12_index_mutations_and_staging_recovery.py \
  tests/unit/test_staging_correction.py tests/unit/test_recovery_planner.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run mkdocs build --strict
uv run python -m build
```

If a command cannot run, report the exact blocker and do not claim validation passed.

## Validation

Before handoff, inspect the diff to confirm:

- Initial pre-Cursor staged work still fails before any agent launch.
- A Cursor-created initial staged index no longer aborts staging and the staged patch
  presented to Codex is the normalized cumulative result.
- No Git-history mutation, unstaging, manual cleanup, or target-repository state
  storage was introduced.
- `initial_staging_failed` is represented consistently in model, schema, recovery
  planner, successor lineage, CLI output, docs, and tests.
- `recover` remains repository read-only; `resume` does not rerun proven-complete
  Cursor work and preserves exact agent identities.
- No default output leaks protected artifacts or full session identifiers.

## Risks Or Recovery Notes

- Once Cursor is allowed to mutate the index, the post-Cursor index cannot prove
  which actor performed a stage. This is intentional: integrity comes from the
  pre-Cursor checkpoint, repository locks/identity validation, final normalization,
  persisted fingerprints, and Codex review of the full staged patch.
- The historical incident cannot be fixed merely by removing the validation check;
  it already has terminal status `failed`. The new typed recovery path is required
  to continue it safely after this implementation is installed and validated.
- Do not mutate the source run to manufacture artifacts. If its fingerprint, identity,
  or plan/prompt contract no longer matches at recovery time, recovery must refuse.

## OpenQuestions

None.
