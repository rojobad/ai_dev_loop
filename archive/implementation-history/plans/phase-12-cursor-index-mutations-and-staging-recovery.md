# Phase 12 - Cursor Index Mutations And Staging Recovery

## Goal

Allow Cursor correction turns to modify the Git index when a fix legitimately requires staging, unstaging, or removing an ignored path from the index, while preserving a deterministic final-review boundary owned by `ai_dev_loop`.

After every successful Cursor turn, the orchestrator must normalize the repository with its configured `stage_mode` (`git add -A` for `all`), validate the resulting repository/index state, persist a complete cumulative staged snapshot, and send that snapshot to Codex for review.

Extend `ai_dev_loop recover` to recover failed runs at the checkpoint:

```text
Cursor completed / staging incomplete
```

The motivating successor run `crypto-sentinel-20260711T133243Z-d32b0c` must become recoverable after implementation without rerunning Cursor. Because that historical run predates post-Cursor content fingerprints, recovery may require explicit user adoption through a narrowly scoped flag.

## Non-Goals

- Do not prohibit Cursor from running `git add`, `git restore --staged`, or `git rm --cached` during a correction.
- Do not make Cursor the final source of truth for the staged snapshot; `ai_dev_loop` still performs the final staging normalization and records the patch consumed by Codex.
- Do not allow Cursor to commit, amend, reset, checkout/switch branches, stash, clean, merge, rebase, tag, or push.
- Do not permit HEAD, branch, Git directory, or repository identity changes during a Cursor turn.
- Do not preserve an intentionally unstaged modification to a tracked, non-ignored file when `stage_mode: all` is active. `git add -A` stages all eligible worktree changes by design.
- Do not add selective staging or per-path exclusions in this phase.
- Do not silently adopt unverifiable historical worktree content.
- Do not mutate Git state from `recover`; staging occurs only when the successor is explicitly resumed.
- Do not rerun completed Cursor turns during recovery.
- Do not recover or mutate the real user run during implementation, tests, installation, or smoke validation.
- Do not run real Cursor/Codex model calls, real CLI updates, commits, pushes, or releases.

## Scope

- Correction pre-Cursor and post-Cursor Git contracts.
- Staging runner behavior when Cursor changed the index.
- Safe post-Cursor content fingerprints for future recovery.
- Recovery analysis and successor creation for `staging` checkpoints.
- Explicit legacy adoption for runs without a post-Cursor fingerprint.
- State, schemas, manifests, artifacts, lineage, idempotency, status/inspect/list/logs, tests, rules, and documentation.
- Fake end-to-end coverage of Cursor index mutation, staging normalization, recovery, and subsequent Codex review.
- Full validation, package build, and installation of the verified `ai_dev_loop` build in WSL.

## Out of Scope

- Changes to target-application business logic.
- Selective/pathspec staging modes beyond the existing `all` mode.
- Inferring which paths a user intended to exclude when they remain modified and non-ignored.
- Recovery after manual repository changes that cannot be matched to a persisted post-Cursor snapshot.
- Recovery of incomplete Cursor turns.
- Recovery across branch/HEAD/repository changes.
- Automatic execution of the recovered successor.
- Updating Codex Desktop or Cursor Desktop.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-5-bounded-review-fix-loop-and-resume.md`
- `archive/implementation-history/plans/phase-10-codex-session-runtime-and-cli-compatibility.md`
- `archive/implementation-history/plans/phase-11-recover-failed-runs.md`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/resume_planner.py`
- `src/ai_dev_loop/recovery_planner.py`
- `src/ai_dev_loop/commands/recover.py`
- `src/ai_dev_loop/runners/staging.py`
- `src/ai_dev_loop/runners/git.py`
- `src/ai_dev_loop/runners/cursor.py`
- `src/ai_dev_loop/iterations.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/locking.py`
- `src/ai_dev_loop/process.py`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- Existing staging, correction, resume, recovery, Phase 10/11, privacy, permission, fake CLI, and E2E tests.
- User-facing docs for workflow, recovery, Git safety, state/artifacts, observability, troubleshooting, CLI, security/privacy, and phase traceability.

Validated incident evidence:

- Recovery successor `crypto-sentinel-20260711T133243Z-d32b0c` resumed directly at Codex review iteration 1.
- Codex found two actionable findings and produced `prompts/fixes/01.txt`.
- Cursor correction iteration 2 completed successfully in the same chat and reported passing tests.
- Cursor legitimately executed `git add` multiple times while updating implementation, tests, and validation evidence.
- `ai_dev_loop` then failed before correction staging because the staged index no longer matched iteration 1's recorded patch.
- The current run has completed Cursor iteration 2 artifacts, `git/status/02-before-cursor.txt`, and `git/status/02-after-cursor.txt`, but no completed `git/diffs/02.patch`.
- The current repository contains Cursor's staged corrections plus one tracked unstaged date correction.
- Phase 11 `recover --dry-run` rejects it with `staging_incomplete` and `phase11_supports_post_staging_only`.

The real run is evidence only. Use sanitized fixtures and never mutate or recover it during implementation.

## Cursor Rules And Skills

Cursor must follow all repository-local rules, especially:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use repo-local skills when their descriptions match the task, including documentation acceptance/governance for documentation changes. No `AGENTS.md` was present when this plan was authored.

Update the relevant Cursor rules in the same change. Remove the obsolete invariant that correction staging must begin with an unchanged index after Cursor returns, while preserving the stricter pre-Cursor checkpoint invariant.

## Architecture Guardrails

- Before every correction Cursor turn, the current staged index must still match the previous iteration's recorded staged patch exactly, with no tracked unstaged changes or untracked non-ignored files. This remains the trusted starting checkpoint.
- During Cursor execution, index mutation is permitted. The orchestrator must not assume the post-Cursor index equals the pre-Cursor index.
- Cursor may use `git add`, `git restore --staged`, and `git rm --cached`. Cursor must not change Git history, HEAD, branch, remotes, stash state, worktrees, or repository identity.
- After Cursor completes successfully, validate that HEAD, branch, Git common directory, Git directory, and repository root remain exactly as prepared.
- Capture post-Cursor status and content evidence before staging normalization.
- For `stage_mode: all`, always execute `git add -A` after Cursor, even if Cursor already staged some or all changes.
- After `git add -A`, require no tracked unstaged changes and no untracked non-ignored files. Ignored files may remain and must not be surfaced as failures.
- The cumulative staged patch after orchestrator normalization is the only patch Codex reviews and the only patch used for the next correction checkpoint.
- Codex remains responsible for accepting or rejecting the complete staged snapshot. The orchestrator validates safety and determinism but does not reinterpret findings.
- `git add -A` may restage a tracked non-ignored file that Cursor merely unstaged. Documentation and correction headers must explain this. To exclude such a file, Cursor must restore/remove the underlying worktree change or make a newly untracked path genuinely ignored and remove it from the index.
- Plan and prompt hash protections remain unchanged. The prompt source must never be staged.
- Do not weaken staged-patch validation before Codex review or before the next Cursor correction.
- Post-Cursor fingerprints and patches are sensitive artifacts under the XDG run directory. Do not print their content by default.
- Recovery source runs remain terminal and immutable. Recovery creates a successor.
- A staging-checkpoint successor must reuse exact Cursor chat ID, Codex session ID, runtime, prompt/fix prompt, completed iteration state, and repository identity.
- `recover` must not run `git add -A`; successor `resume` owns staging.
- For historical sources lacking a content fingerprint, explicit adoption is allowed only when current status exactly matches the recorded post-Cursor status and every other repository/session/artifact invariant passes. The user must explicitly supply the adoption flag.
- Automated tests must use fake CLIs and temporary repositories. Do not invoke the real motivating run.

## Implementation Plan

### 1. Redefine Correction Index Ownership

Split the correction contract into two boundaries.

Pre-Cursor boundary:

- Keep `validate_correction_pre_cursor` or an equivalent helper.
- Require the current index to equal the previous staged patch.
- Reject tracked unstaged and untracked non-ignored paths.
- Capture/validate repository identity and HEAD.
- This proves the correction starts from the exact patch Codex reviewed.

Post-Cursor boundary:

- Remove the correction-only equality check from `validate_pre_staging` that currently raises:

```text
staged index changed before correction staging; expected the previous orchestrator-recorded staged patch
```

- Do not replace it with another post-Cursor “index unchanged” check.
- Validate repository identity, branch, HEAD, plan, and prompt invariants.
- Permit staged, unstaged, deleted, renamed, and untracked correction outputs that `git add -A` can normalize.
- Continue rejecting unsafe repository/history changes.

Update `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc` and related rules so they distinguish pre-Cursor checkpoint integrity from permitted post-Cursor index mutation.

### 2. Add A Deterministic Correction Execution Header

The exact Codex-produced `cursor_fix_prompt` remains stored unchanged in `prompts/fixes/NN.txt` and remains the machine/audit source.

When sending a correction to Cursor, construct a deterministic execution envelope that prepends operational constraints while embedding the exact stored fix prompt verbatim. Do not summarize, translate, reorder, or rewrite the findings.

The header should communicate:

```text
This is an ai_dev_loop correction turn.

You may stage or unstage files when required by the confirmed fixes, including updating
.gitignore and removing ignored files from the index.

Do not commit, amend, reset, checkout/switch branches, stash, clean, merge, rebase, tag,
push, or otherwise change Git history or repository identity.

The orchestrator will run git add -A after this turn and Codex will review the complete
resulting staged snapshot. Merely unstaging a tracked non-ignored modification will not
exclude it from the final snapshot.

Codex findings:

<exact cursor_fix_prompt>
```

Persist or identify both:

- exact fix-prompt artifact;
- actual execution-envelope artifact or deterministic reconstruction metadata.

Keep both sensitive. Update review/fix-prompt contracts and tests to prove the exact fix prompt appears byte-for-byte inside the sent payload.

### 3. Capture A Post-Cursor Content Fingerprint

Future staging recovery must prove the repository has not changed since Cursor completed. Status text alone is insufficient because content can change while paths/status remain the same.

Add a structured sensitive artifact for every successful Cursor turn before staging, for example:

```text
git/cursor-output/NN.json
```

The artifact should contain only deterministic Git/content evidence, such as:

- iteration number;
- repository root identity classification, not a new target-repository file;
- branch and HEAD;
- normalized porcelain-v2 status hash;
- cached diff hash using a binary-safe canonical representation;
- tracked worktree diff hash using a binary-safe canonical representation;
- untracked non-ignored path list plus per-file content hashes;
- optional submodule/status evidence if existing repository support requires it;
- aggregate fingerprint hash;
- capture timestamp.

Requirements:

- Use structured Git commands and hashing helpers; do not parse human prose.
- Support binary changed/untracked files without reading them as UTF-8.
- Bound file reads using streaming hashes.
- Do not store file contents in the JSON artifact.
- Treat paths and hashes as sensitive operational metadata and preserve user-only permissions.
- Reject unsupported/special file types conservatively.
- Recompute and compare this fingerprint during recovery before creating a successor.
- Record the relative artifact path in iteration metadata and manifest/discoverable conventions.

Capture must happen after Cursor exits successfully and after `NN-after-cursor.txt`, but before `git add -A`.

### 4. Normalize And Validate Final Staging

Update `run_git_staging`:

1. Validate post-Cursor repository identity and plan/prompt contracts.
2. Persist `NN-before-staging.txt`.
3. Run `git add -A` with `shell=False` through the existing runner.
4. Persist `NN-after-staging.txt`.
5. Verify:
   - HEAD and branch remain unchanged;
   - no tracked unstaged changes remain;
   - no untracked non-ignored files remain;
   - there is a non-empty staged patch;
   - staged paths satisfy plan/prompt safety;
   - `git diff --cached --check` equivalent validation runs if an existing structured helper supports it, or retain current workflow behavior without shell interpolation.
6. Write cumulative stat, name-only, and patch artifacts.
7. Record completed iteration metadata.

Do not reject because Cursor changed or emptied the index. The final `git add -A` is the normalization point.

Add safe event/log metadata indicating whether Cursor changed the index, without logging patch content. This is audit information, not a warning by itself.

### 5. Extend Recovery Checkpoints To `staging`

Add `staging` to recovery checkpoint enums/models/schemas and add a reason code such as:

```text
correction_staging_failed
```

Recovery analysis should select `checkpoint: staging` when:

- source status is `failed`;
- latest iteration is a correction iteration (`NN >= 2`) in the initial implementation;
- Cursor turn `NN` completed successfully;
- staging `NN` is incomplete;
- previous iteration staging and review/fix prompt are complete;
- source chat/session/runtime identity is complete;
- repository root/Git dirs/branch/HEAD/plan/prompt match;
- no active child process exists;
- current repository content matches the persisted post-Cursor fingerprint; or legacy adoption is explicitly authorized as described below.

For a verified staging checkpoint:

- copy completed Cursor iteration `NN` artifacts;
- copy `NN-before-cursor.txt`, `NN-after-cursor.txt`, post-Cursor fingerprint, and prior iteration staged/review/fix artifacts;
- do not invent `git/diffs/NN.patch` or mark staging complete;
- create successor with `status: interrupted` and recovery checkpoint `staging`;
- ensure `resume_planner` derives `WorkflowActionKind.STAGING`;
- on `resume`, run staging once, record iteration `NN`, and continue to Codex review `NN` without Cursor execution.

Do not broaden recovery to incomplete Cursor turns.

### 6. Support Explicit Legacy Cursor-Output Adoption

Historical runs, including the motivating run, have `NN-after-cursor.txt` but no content fingerprint. They cannot be proven unchanged automatically.

Add a recover option:

```bash
ai_dev_loop recover RUN_ID --adopt-current-cursor-output
```

And support it with `--dry-run`:

```bash
ai_dev_loop recover RUN_ID --dry-run --adopt-current-cursor-output
```

Without the flag:

- report blocker `post_cursor_fingerprint_missing`;
- explain that explicit adoption is required for this historical staging checkpoint;
- do not create a successor.

With the flag, require all of:

- source is otherwise eligible for staging recovery;
- recorded `NN-after-cursor.txt` exists and exactly matches current normalized porcelain status;
- current branch/HEAD/repository identity match;
- completed Cursor metadata/final response exist;
- previous staged patch and review/fix artifacts are valid;
- no active process metadata is live or ambiguous;
- no unsupported special files are present;
- the user supplied the flag explicitly; no TTY prompt or config/YAML authorization substitutes for it.

Then:

- compute a fresh post-Cursor fingerprint from the current repository;
- record recovery lineage as explicit adoption;
- create the successor without changing Git/index/worktree;
- copy the historical status and new fingerprint into the successor;
- let successor `resume` perform `git add -A`.

Document that explicit adoption attests that the user has not manually modified the repository since the recorded Cursor turn. Do not claim cryptographic proof for the historical interval.

### 7. Evolve Recovery Lineage And Idempotency

Update `RecoveryState` and `run-state-v1.json` compatibly.

Keep existing Phase 11 successors readable. Add optional/conditional fields such as:

```text
cursor_output_fingerprint_sha256
previous_staged_patch_sha256
legacy_cursor_output_adopted
```

For `reviewing`/`process_review`, preserve existing `source_staged_patch_sha256` semantics.

For `staging`:

- define `source_staged_patch_sha256` consistently as the previous completed iteration's staged patch hash for backward structural compatibility, or replace it with a clearly versioned generalized checkpoint fingerprint only if migration remains explicit and tested;
- require the post-Cursor aggregate fingerprint hash;
- require an explicit adoption marker when the fingerprint was captured during recovery rather than at Cursor completion.

Add Pydantic cross-field validation and matching JSON Schema conditions where practical.

Idempotency matching for staging successors must include:

- source run ID;
- source iteration;
- checkpoint `staging`;
- previous staged patch hash;
- post-Cursor aggregate fingerprint hash;
- adoption classification.

Repeated recovery returns the existing valid non-terminal successor. A failed successor must itself be recovered; do not skip lineage generations.

### 8. Update Successor Artifact Copying And Validation

Update `commands/recover.py` artifact selection and sanitization:

- support `staging` checkpoint without requiring a current-iteration staged patch;
- copy the current completed Cursor iteration and post-Cursor evidence;
- copy all prior completed staged/review/fix artifacts required for correction context;
- remove incomplete current-iteration Git staging metadata from successor state;
- preserve current-iteration Cursor metadata and started timestamp;
- do not copy failed staging logs into active successor staging paths unless preserved under explicit recovery-history names;
- preserve source run immutability.

Update matching-successor validation so it expects Cursor complete and staging incomplete for `staging`, rather than requiring `staging_complete_for_iteration` unconditionally.

The successor must pass ordinary `resume` preflight and derive staging without special command bypasses.

### 9. Update CLI, Status, Inspect, Logs, And Error Guidance

Update `recover --help`, text output, and JSON output for:

- staging checkpoint;
- post-Cursor fingerprint availability;
- explicit adoption requirement/status;
- previous staged patch hash;
- next command.

Example:

```text
Source run: crypto-sentinel-...
Recovery run: crypto-sentinel-...
Recovered checkpoint: staging, iteration 2
Cursor output: explicitly adopted from matching historical after-cursor status
Repository identity: verified
Git mutation performed by recover: none
Next command: ai_dev_loop resume <recovery-run-id>
```

Update status/inspect/list/logs to distinguish:

- recovered review checkpoint;
- recovered staging checkpoint;
- fingerprint verified;
- historical output explicitly adopted.

Replace the current staging failure guidance with actionable recovery commands when analysis supports them. Default output must not include patch contents, full prompts, full session IDs, or raw Cursor events.

### 10. Update Documentation And Governance

Update at minimum:

- README;
- quick guide;
- prepare/start/resume/abort/recover operations page;
- Git staging and safety documentation;
- state/artifacts;
- observability;
- troubleshooting;
- security/privacy;
- CLI reference;
- phase traceability;
- target-repository integration plan/docs if they currently require Cursor never to stage.

Documentation must explain:

- Cursor may alter the index during correction.
- Which Git operations are permitted versus forbidden.
- `git add -A` remains the final normalization step.
- Codex reviews the complete cumulative staged snapshot.
- Why merely unstaging a tracked non-ignored change does not exclude it under stage mode `all`.
- Correct `.gitignore` plus index-removal workflow for mistakenly staged generated/ignored files.
- Pre-Cursor patch equality remains mandatory.
- Post-Cursor fingerprints and their privacy classification.
- Staging-checkpoint recovery.
- Legacy `--adopt-current-cursor-output` semantics and limitations.
- Exact recovery procedure for a sanitized equivalent of the motivating run.

Update Cursor rules so future plans do not reintroduce the post-Cursor unchanged-index assumption.

### 11. Automated Tests

Unit tests:

- Pre-Cursor correction still rejects staged patch drift, empty index, tracked unstaged changes, and untracked non-ignored files.
- Post-Cursor staging accepts Cursor-added staged changes.
- Post-Cursor staging accepts Cursor-unstaged changes and normalizes eligible worktree changes with `git add -A`.
- `.gitignore` plus `git restore --staged`/`git rm --cached` keeps a newly ignored path out of the final staged snapshot.
- A tracked non-ignored file that is merely unstaged is restaged, matching documented `stage_mode: all` behavior.
- Cursor partial staging plus remaining unstaged edits produces a fully staged cumulative patch.
- HEAD, branch, repository identity, prompt, and plan drift remain rejected.
- Post-Cursor fingerprint is deterministic and changes when staged, unstaged, untracked, binary, deleted, or renamed content changes.
- Fingerprints contain no file content.
- Special/unsupported file types fail safely.
- Recovery lineage/schema conditional validation for staging checkpoints.
- Legacy state compatibility.

Integration tests with fake Cursor/Codex:

- Correction Cursor runs `git add`; orchestrator stages normally and Codex review continues.
- Correction Cursor updates `.gitignore` and removes a generated file from index; final patch excludes it and includes `.gitignore`.
- Correction Cursor leaves both staged and unstaged changes; orchestrator stages all and records patch.
- Correction Cursor attempts a forbidden HEAD/branch/history change; workflow fails safely before review.
- A staging command failure after completed Cursor produces a recoverable failed run with persisted fingerprint.
- `recover --dry-run` identifies checkpoint `staging`.
- `recover` creates successor; successor `resume` invokes staging then Codex and does not invoke Cursor.
- Findings after recovered review continue in the same Cursor chat.
- Fingerprint drift blocks recovery.
- Historical staging failure without fingerprint requires `--adopt-current-cursor-output`.
- Adoption rejects status mismatch.
- Adoption creates no Git changes and successor resume performs staging.
- Repeated adoption recovery reuses matching successor.
- Failed successor recovery preserves lineage chain.
- Review/process-review recovery from Phase 11 remains unchanged.

Add a sanitized E2E scenario reproducing the motivating sequence:

1. Initial Cursor implementation and staging.
2. Codex findings.
3. Correction Cursor modifies files, runs `git add`, and leaves one tracked edit unstaged.
4. Orchestrator runs `git add -A` without rejecting index mutation.
5. Iteration 2 staged patch is recorded.
6. Codex review 2 completes.

Add a separate legacy-recovery fixture that begins from the old failure shape and proves explicit adoption plus successor resume does not rerun Cursor.

Privacy/permission tests:

- Post-Cursor fingerprint and copied recovery artifacts are user-only where supported.
- No file content, prompts, findings, patches, raw events, full session IDs, auth data, or environments leak through fingerprint JSON, recovery lineage, status, logs, or errors.
- `--dry-run`, with or without adoption, performs no persistent writes or Git mutations.

Use fake CLIs, temporary repositories, and sanitized fixtures. Never invoke real agents, model calls, updaters, or the real run.

### 12. Validation And WSL Installation

Run:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q -s
uv run python -m build
uv run mkdocs build --strict
uv run ai_dev_loop --help
uv run ai_dev_loop recover --help
uv run ai_dev_loop resume --help
uv run ai_dev_loop status --help
uv run ai_dev_loop inspect --help
uv run ai_dev_loop doctor
```

After every required check and fake E2E passes, install the verified package in WSL:

```bash
uv tool install --force .
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop recover --help
```

Do not run recovery against `crypto-sentinel-20260711T133243Z-d32b0c` during implementation or installation. After the user reviews the staged Phase 12 changes, real recovery will be a separate explicitly authorized operation.

## Testing Criteria

Acceptance requires automated evidence that:

- Cursor index mutation during corrections is supported and no longer causes a false staging failure.
- The previous staged patch remains strictly validated before Cursor starts.
- `git add -A` produces the complete final staged snapshot after Cursor.
- Ignored-file removal works when Cursor updates ignore rules and removes the path from the index.
- Tracked non-ignored unstage semantics are documented and tested.
- Repository history/identity mutations remain blocked.
- Future post-Cursor output is fingerprinted sufficiently for safe staging recovery.
- Failed runs with completed Cursor and incomplete staging can produce immutable-source successors.
- Successor resume stages and reviews without rerunning Cursor.
- Historical missing-fingerprint recovery requires explicit adoption and exact status matching.
- Existing review/process-review recovery remains compatible.
- No real run, agent, updater, or model call occurs during tests/installation.
- Full static checks, tests, package build, strict docs, E2E, and installed CLI smoke checks pass.

## Validation

Cursor's final response must report:

- Files changed, grouped by staging/Git contract, correction envelope, fingerprinting, recovery, state/schema, tests, rules, and docs.
- Exact pre-Cursor versus post-Cursor index invariants.
- Permitted and forbidden Git operations.
- Final `git add -A` semantics, including tracked unstage and ignored-file examples.
- Post-Cursor fingerprint fields and privacy treatment.
- Staging recovery eligibility and legacy adoption requirements.
- Evidence that recovered staging resume does not rerun Cursor.
- Full validation command results and test counts.
- Installed WSL path/version and `recover --help` output.
- Confirmation that the real motivating run was not mutated or recovered.

## Risks Or Recovery Notes

- Allowing Cursor to mutate the index increases flexibility but removes the index as a stable post-Cursor trust boundary. Preserve the strict pre-Cursor checkpoint, repository identity checks, post-Cursor fingerprint, final stage-all normalization, and Codex full-patch review.
- `git add -A` intentionally stages all eligible changes. Selective unstage is not durable for tracked non-ignored modifications under this mode.
- `.gitignore` does not automatically untrack an already indexed path. Cursor must remove it from the index when that is the confirmed fix.
- Status-only legacy evidence cannot prove file contents were unchanged. Explicit adoption is a user attestation and must be recorded as such.
- Binary and untracked-file fingerprinting can be expensive. Stream hashes and avoid retaining content; do not weaken correctness with path-only fingerprints.
- Recovery transaction and lock ordering from Phase 11 must remain unchanged unless a tested deadlock-safe extension is necessary.
- Source and earlier successor runs remain immutable even when a later successor completes successfully.
- If installation fails, preserve the prior WSL executable and report the failure; do not delete tool directories manually.

## OpenQuestions

None.
