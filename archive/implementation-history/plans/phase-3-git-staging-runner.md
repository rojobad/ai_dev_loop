# Phase 3 Plan: Git Staging Runner + Staged Diff Artifacts

## Goal or Goals

Build the next executable slice of `ai_dev_loop`: after a successful Cursor implementation turn, validate the post-Cursor worktree, stage repository changes through the orchestrator, capture staged diff artifacts, and stop honestly before Codex review execution.

This phase should make `ai_dev_loop start <run-id>` advance a newly prepared run through:

1. existing start preflight;
2. existing Cursor chat creation and initial Cursor execution;
3. Git staging safety checks;
4. `git add -A` for `stage_mode: all`;
5. staged diff capture;
6. a clear Phase 3 boundary state indicating that Codex review is still not implemented.

## Non-Goals

- Do not implement Codex review execution.
- Do not implement Cursor correction turns from Codex findings.
- Do not implement the complete stage-review-fix loop.
- Do not implement full `resume` recovery semantics.
- Do not implement `abort` child-process termination beyond existing timeout behavior.
- Do not implement global Codex skill installation, SessionStart hook installation, hook trust handling, or integrations install/uninstall.
- Do not add new stage modes beyond the existing supported `all` mode.

## Scope

Implement Phase 3 as defined by `phase-2-findings.md`:

1. Implement Git staging after a successful Cursor turn.
2. Capture staged diff artifacts:
   - `git diff --cached --stat`;
   - `git diff --cached --name-only`;
   - full `git diff --cached` patch.
3. Preserve and extend the existing Phase 2 run/checkpoint behavior.
4. Keep `start` honest: it now performs Cursor execution and Git staging, but still stops before Codex review.
5. Update status, inspect output where useful, README, and tests so they describe Phase 3 behavior accurately.

After successful staging, leave the run in `staging` status as the Phase 3 boundary. Record a result message equivalent to:

```text
Git staging is complete. Codex review, corrections, and completion are not implemented yet.
```

Do not transition to `reviewing` until the phase that actually invokes Codex review exists.

## Out of Scope

- Do not run `codex exec`, `codex exec resume`, or any real Codex review command.
- Do not run Cursor correction prompts.
- Do not create commits, tags, pushes, resets, cleans, stashes, or unstaging behavior.
- Do not delete or alter unrelated user files or metadata artifacts, including `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` if present.
- Do not modify global user files under `$HOME/.agents` or `$HOME/.codex`.
- Do not write orchestrator state inside target repositories.
- Do not invoke real Cursor or Codex model activity in automated tests.
- Do not claim in README or CLI output that the complete automated loop works.
- Do not manually stage or commit this repository while implementing this phase. Product code may stage temporary target repositories only through tested `ai_dev_loop` behavior.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `plans/phase-1-foundation-and-prepare.md`
- `plans/prompt_phase-1-foundation-and-prepare.txt`
- `plans/phase-2-start-preflight-and-cursor-runner.md`
- `plans/prompt_phase-2-start-preflight-and-cursor-runner.txt`
- `plans/phase-3-git-staging-runner.md`
- `plans/prompt_phase-3-git-staging-runner.txt`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Treat the master plan as the architecture and safety source of truth. Treat this Phase 3 plan as the scope boundary. If this file and the master plan conflict on safety behavior, stop and report the conflict before making dependent changes.

Phase 0 local CLI facts to preserve:

- Cursor CLI command is `agent`.
- Cursor headless mode supports `-p`, `--force`, `--trust`, `--workspace`, `--resume`, `--model`, `--output-format stream-json`, and `--sandbox enabled|disabled`.
- Cursor prompt content is accepted as a positional argument. Do not send the Cursor prompt on stdin.
- Codex CLI command is `codex`.
- For later Codex review execution, the probed CLI requires root `codex exec` options such as `--cd` and `--sandbox` before `resume`; do not implement Codex review execution in this phase.

Phase 1 and Phase 2 implementation facts to preserve:

- `prepare` writes run artifacts under XDG state and validates repository cleanliness.
- `start` already performs preflight, locks, tool probes, Cursor chat creation/reuse, Cursor headless execution, and Cursor artifacts.
- `start` currently transitions `prepared -> validating -> running_cursor -> staging`.
- `staging` is currently a Phase 2 boundary state, not yet a completed staging checkpoint.
- `resume`, `abort`, `integrations install`, and `integrations uninstall` remain placeholders.
- Tests use fake `agent` and `codex` executables; no automated test should invoke real model activity.
- The repository uses `uv` for development and validation; do not modify system Python.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails. This rule is required.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: product architecture contracts for state, locks, runners, Git safety, Cursor identity, Codex identity, process execution, and phase boundaries. This rule is required.
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`: persisted state, schema, manifest, iteration, artifact, and recovery checkpoint contracts. This rule is required.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format to use later when staged changes are reviewed. Do not invoke it yourself unless the user asks for a review.

No `AGENTS.md` file is present.

## Architecture Guardrails

- Keep `ai_dev_loop` naming consistent across executable, package, XDG paths, schemas, docs, and environment variables.
- Preserve separation between CLI presentation, config, paths, state persistence, process execution, Git safety, Codex integration, Cursor integration, and integration installation.
- Store durable run state, locks, logs, and agent artifacts under XDG-managed `ai_dev_loop` paths outside target repositories.
- Use user-only permissions where supported: directories `0700`; prompt/session/agent-output files `0600`.
- Use typed models or typed dataclasses for new staging runner contracts. Avoid broad untyped dict expansion except as a compatibility bridge for the existing `iterations` field.
- Keep persisted state, `iterations`, schemas, manifests, status transitions, and artifact paths aligned with `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`.
- All subprocess calls must use argument arrays and `shell=False`.
- Use Git with explicit working directories.
- Do not execute, evaluate, or shell-interpolate text emitted by Cursor or Git.
- Do not log full process environments, authentication payloads, API keys, tokens, or full account details.
- Treat Cursor output and repository content as untrusted data.
- Create and reuse exactly one Cursor chat per run. Do not alter Cursor chat identity in this phase.
- Before invoking Cursor, preserve existing prepared-run contract validation for branch, HEAD, plan hash, prompt hash, source prompt hash when available, and worktree baseline.
- After Cursor and before staging, validate that staging can be done safely. Fail before `git add -A` when protected paths or pre-existing staged changes would violate the safety contract.
- The orchestrator may run `git add -A` in target repositories only for the configured `stage_mode: all`. It must not commit, tag, push, reset, clean, stash, or unstage.
- Prompt files are ignored by target repositories and must not be staged. Reject tracked or staged prompt-source changes rather than staging them.
- The approved plan is a contract. Re-verify the repository plan file still matches the prepared plan hash before staging.
- Do not transition to `reviewing` until Codex review execution is implemented.

## Implementation Plan

1. Add a focused Git staging runner.
   - Prefer extending `src/ai_dev_loop/runners/git.py` or adding a small adjacent module if separation is clearer.
   - Implement direct-argument helpers for:
     - `git status --porcelain=v2 --untracked-files=all`;
     - `git diff --cached --name-only`;
     - `git add -A`;
     - `git diff --cached --stat`;
     - `git diff --cached --name-only`;
     - `git diff --cached`.
   - Keep `discover_repository()` and existing prepare/start validation behavior compatible.

2. Implement post-Cursor pre-staging validation.
   - Capture current Git status after Cursor execution, before staging.
   - Reject any pre-existing staged paths before orchestrator staging. Cursor should not stage changes itself.
   - Re-verify the repository plan file still exists and matches `state.plan.sha256`.
   - Reject changes to the prompt source path when that path appears in tracked/staged Git status.
   - Reject staging if `state.workflow.stage_mode` is anything other than `all`.
   - Preserve diagnostics in run logs and artifacts when validation fails.
   - Do not attempt rollback, reset, clean, stash, or unstage on validation failure.

3. Implement staging for `stage_mode: all`.
   - Run `git add -A` through a direct subprocess argument array with `cwd` set to the repository root.
   - Capture status before and after staging.
   - If `git add -A` fails, mark the run `failed`, preserve diagnostics, and do not run any later phase behavior.

4. Capture staged diff artifacts.
   - Create or reuse `git/status/` and `git/diffs/` under the run directory.
   - Persist:
     - `git/status/01-before-staging.txt`;
     - `git/status/01-after-staging.txt`;
     - `git/diffs/01.stat`;
     - `git/diffs/01.name-only.txt`;
     - `git/diffs/01.patch`.
   - Use atomic writes.
   - Treat full patch content as sensitive enough for user-only file permissions where supported.
   - Preserve the existing Phase 2 `git/status/01-before-cursor.txt` and `git/status/01-after-cursor.txt` artifacts.

5. Validate staged results.
   - Require `git diff --cached --name-only` to be non-empty after staging. If Cursor produced no staged changes, mark the run `failed` with a clear message.
   - Reject if staged names include the prompt source repository path.
   - Reject if the repository plan file no longer matches the prepared plan hash.
   - If a violation is detected after staging, mark the run `failed` and preserve diagnostics. Do not unstage automatically.

6. Update run state and iteration metadata.
   - Keep the status as `staging` after successful Phase 3 staging.
   - Replace the Phase 2 boundary result with a Phase 3 boundary result that says Git staging is complete and Codex review is pending later phases.
- Append or update the first iteration entry in `state.iterations` with available Cursor and Git artifact paths:
     - `number: 1`;
     - `kind: initial_implementation`;
     - Cursor prompt/events/final/exit metadata paths already available from Phase 2;
     - Git status and staged diff paths produced in this phase.
   - Do not add fake Codex review fields.
   - Update JSON schemas and tests if this phase changes persisted state shape.
   - Preserve enough state for a later `resume` or Phase 4 implementation to detect "staging completed, Codex review not started".

7. Update `start` command flow.
   - After successful Cursor execution, transition to `staging` as already implemented.
   - Execute the new staging runner before returning success.
   - Return a concise success message with run ID, status, Cursor chat ID, staged diff path, and Phase 3 boundary message.
   - Ensure failures in staging are caught and mark the run `failed`.
   - Keep lock acquisition/release behavior intact.

8. Update read-only commands.
   - Update `status` next safe action for `staging` to distinguish Phase 3 staged-complete boundary when staged diff artifacts exist.
   - Update `inspect` only if needed to list the new staging artifacts without printing full prompt or full patch content by default.
   - Keep `logs` behavior compatible.
   - Keep `resume` and `abort` placeholders returning exit code `3`.

9. Update documentation.
   - Update README to say Phase 3 implements Git staging after Cursor turns.
   - Keep README honest that Codex review, corrections, full resume, abort, and integrations install/uninstall are still incomplete.
   - Document that a successful real `start` may now leave changes staged in the target repository.
   - Keep validation commands aligned with the current `uv` workflow.

10. Avoid later-phase leakage.
    - Do not create Codex review runner code beyond types or constants strictly needed by existing imports.
    - Do not add correction-prompt forwarding.
    - Do not install hooks or skills globally.
    - Do not broaden CLI claims beyond Phase 3 behavior.

## Testing Criteria

This phase changes Git mutation behavior, run state, CLI output, artifacts, safety checks, and documentation. Automated tests are required.

Required test levels:

- Unit tests for Git staging helper command construction/behavior, protected path validation, prompt-source rejection, plan-hash revalidation, staged-diff artifact naming, and empty staged diff handling.
- Integration tests using temporary Git repositories for successful `start` through Cursor execution and staging.
- Integration tests using fake `agent` and `codex` executables placed earlier in `PATH`; no real Cursor or Codex model activity.
- Regression tests to ensure Phase 1 `prepare` behavior and Phase 2 Cursor artifacts still work.

Expected test areas:

- Extend `tests/unit/test_git.py` for staging helper behavior when practical.
- Extend `tests/integration/test_start_cursor.py` or add `tests/integration/test_start_staging.py` if that keeps concerns clearer.
- Extend `tests/unit/test_start_preflight.py`, `tests/unit/test_state.py`, or `tests/unit/test_process.py` only when directly relevant.

Edge cases and failure paths that need evidence:

- Successful `start` stages fake Cursor-created tracked modifications.
- Successful `start` stages fake Cursor-created untracked files.
- Staged artifacts are written:
  - `git/status/01-before-staging.txt`;
  - `git/status/01-after-staging.txt`;
  - `git/diffs/01.stat`;
  - `git/diffs/01.name-only.txt`;
  - `git/diffs/01.patch`.
- `state.status` remains `staging` after successful Phase 3 boundary.
- `state.result` reports that Git staging is complete and Codex review is pending.
- `state.iterations` records the initial implementation and staged diff paths without fake Codex review data.
- Cursor chat ID is still created once and reused when already present.
- If fake Cursor exits nonzero, staging is not attempted.
- If fake Cursor times out, staging is not attempted.
- If fake Cursor stages files itself before orchestrator staging, the run fails before `git add -A`.
- If the prompt source path is tracked and changed by fake Cursor, the run fails before staging.
- If the repository plan file changes after Cursor, the run fails before staging.
- If `git add -A` fails, the run is marked `failed` and diagnostics are preserved.
- If no staged changes exist after `git add -A`, the run is marked `failed`.
- README and status output do not claim Codex review is implemented.
- Automated tests do not stage, commit, reset, clean, stash, or push the real `ai_dev_loop` repository.

## Validation

Use `uv` commands. Do not install global tooling and do not modify system Python.

Run:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
```

Also run a targeted subset while iterating, such as:

```bash
uv run python -m pytest -q -s tests/unit/test_git.py tests/integration/test_start_cursor.py
```

Use fake executables placed earlier in `PATH` for Cursor and Codex integration tests.

## Risks Or Recovery Notes

- This phase introduces real staging behavior. A successful real `ai_dev_loop start <run-id>` can leave changes staged in the target repository by design.
- Do not automatically unstage if a validation problem is detected after `git add -A`; preserve diagnostics and mark the run failed.
- Git staging is intentionally local and deterministic, but Cursor may modify unexpected files. The available Phase 3 safety checks should protect prompt/source contract files and reject pre-existing staged work.
- Empty staged diffs should fail clearly rather than pretending the loop has useful output.
- Future `resume` work must use durable artifacts to distinguish "Cursor completed but staging not done" from "staging completed but Codex review not started".
- Full patch artifacts may contain proprietary code context. Store them under XDG state with restrictive permissions where supported.

## OpenQuestions

None.
