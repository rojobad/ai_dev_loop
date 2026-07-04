# Phase 3 Findings

This file summarizes the Phase 3 implementation and review cycle. It is intended as a handoff artifact so Phase 4 can proceed without depending on prior chat history.

## Scope

Phase 3 implemented Git staging after a successful Cursor turn.

The implemented slice advances a prepared run through:

- existing start preflight;
- run and repository locking;
- local Git/Cursor/Codex CLI probes;
- Cursor chat creation or reuse;
- one initial headless Cursor execution;
- post-Cursor staging safety checks;
- `git add -A` for `stage_mode: all`;
- staged diff artifact capture;
- durable first-iteration metadata;
- a clear Phase 3 boundary state before Codex review.

It did not implement the complete automated loop. In particular, Phase 3 did not implement Codex review execution, Cursor correction turns, full `resume` recovery semantics, `abort` child-process termination, or global Codex integration install/uninstall.

After a successful Phase 3 `start`, the target repository may contain Cursor-modified files and those changes may be staged by the orchestrator.

## Environment And Tooling

- Workspace: `/home/rojobad/Projects/ai_dev_loop`
- Primary runtime workflow: `uv`
- Package runtime requirement: Python `>=3.11`
- Tests use fake `agent` and `codex` executables placed earlier in `PATH`.
- Automated tests do not invoke real Cursor or Codex model activity.
- The user still prefers not to modify or replace system Python.

Recommended validation commands remain:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
```

During Phase 3, the implementing agent reported all requested validation passing, ending with `88` passing tests after review fixes.

The Codex review pass also ran:

```bash
git diff --cached --check
uv run python -m pytest -q -s tests/integration/test_start_staging.py
```

The targeted staging suite passed with `14 passed`.

## Delivered Files And Structure

Phase 3 added or materially updated:

- `plans/phase-3-git-staging-runner.md`: Phase 3 implementation plan.
- `plans/prompt_phase-3-git-staging-runner.txt`: concise Cursor handoff prompt.
- `src/ai_dev_loop/runners/staging.py`: Git staging runner, artifact paths, staging completion detection, and iteration metadata recording.
- `src/ai_dev_loop/runners/git.py`: Git helpers for status, cached diff capture, `git add -A`, staged path parsing, plan hash validation, prompt-source safety, and stage-mode validation.
- `src/ai_dev_loop/commands/start.py`: integrates staging after successful Cursor execution and reports the Phase 3 boundary.
- `src/ai_dev_loop/commands/status.py`: distinguishes incomplete `staging` from Phase 3 staging-complete state.
- `tests/conftest.py`: extends the fake `agent` with `FAKE_AGENT_MODIFY_MODE`.
- `tests/integration/test_start_staging.py`: integration coverage for staging success and failure paths.
- `tests/integration/test_start_cursor.py`: updates start expectations for Phase 3 artifacts and boundary text.
- `tests/unit/test_git.py`: unit coverage for new Git staging helpers.
- `README.md`: documents actual Phase 3 behavior without claiming the full loop works.

The master plan was also updated after Phase 3 to clarify structured logging requirements such as `logs/events.jsonl`. At the time of this handoff, the implemented code still uses the existing human-readable `logs/ai_dev_loop.log` plus per-agent raw artifacts; a full structured orchestrator event stream should be treated as future work unless already implemented in a later commit.

## CLI Surface

Implemented by the end of Phase 3:

- `ai_dev_loop prepare`
- `ai_dev_loop start <run-id>`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop inspect <run-id>`
- `ai_dev_loop doctor`
- `ai_dev_loop integrations status`

Still placeholders returning exit code `3`:

- `ai_dev_loop resume <run-id>`
- `ai_dev_loop abort <run-id>`
- `ai_dev_loop integrations install`
- `ai_dev_loop integrations uninstall`

## Start Command Behavior

`ai_dev_loop start <run-id>` now:

1. Loads the prepared run from XDG state.
2. Acquires both the run lock and repository worktree lock.
3. Validates the run is still in `prepared` status.
4. Transitions to `validating`.
5. Re-validates the prepared contract:
   - repository root exists;
   - Git branch is unchanged;
   - Git HEAD is unchanged;
   - plan snapshot hash matches;
   - repository plan file hash matches;
   - prompt snapshot hash matches;
   - prompt source hash matches when the source still exists;
   - worktree porcelain status still matches the prepared baseline;
   - timeouts are positive;
   - Codex session ID is present.
6. Probes local tools:
   - `git` executable;
   - Cursor executable;
   - Codex executable;
   - Cursor auth via `agent status --format json`;
   - Cursor model availability via `agent models`;
   - Codex auth via `codex login status`.
7. Creates a Cursor chat with `agent create-chat`, unless `state.cursor.chat_id` already exists.
8. Persists the Cursor chat ID immediately in:
   - `state.json`;
   - `cursor/chat.json`.
9. Transitions to `running_cursor`.
10. Captures Git status before Cursor execution.
11. Executes Cursor headlessly using the prepared prompt snapshot as one positional argument.
12. Captures Cursor stdout/stderr and parsed metadata.
13. Captures Git status after Cursor execution.
14. On successful Cursor execution, transitions to `staging`.
15. Runs Phase 3 staging:
   - validates `stage_mode: all`;
   - rejects pre-existing staged changes;
   - re-validates the repository plan hash;
   - rejects tracked prompt-source changes;
   - runs `git add -A`;
   - captures staged diff artifacts;
   - validates non-empty staged paths;
   - rejects staged prompt-source paths;
   - re-validates the repository plan hash again.
16. Records the first iteration in `state.iterations`.
17. Leaves status as `staging` and writes the Phase 3 boundary result:

```text
Git staging is complete. Codex review, corrections, and completion are not implemented yet.
```

The CLI still prints the Codex TUI warning before running `start_run()`:

```text
Important: exit the active Codex TUI before continuing with start.
```

## Git Staging Details

The staging runner lives in `src/ai_dev_loop/runners/staging.py`.

Key behavior:

- only `workflow.stage_mode: all` is supported;
- `git add -A` is the only staging operation;
- no commits, tags, pushes, resets, cleans, stashes, or unstaging operations are performed;
- Cursor self-staging is rejected before orchestrator staging;
- tracked prompt-source modifications are rejected before staging;
- prompt-source paths in the staged diff are rejected after staging;
- the repository plan file must still match the prepared plan hash before and after staging;
- empty staged diffs fail clearly instead of producing a false successful boundary;
- full patch artifacts are written with sensitive permissions where supported.

The new staged diff artifacts are:

```text
git/status/01-before-staging.txt
git/status/01-after-staging.txt
git/diffs/01.stat
git/diffs/01.name-only.txt
git/diffs/01.patch
```

`git/diffs/01.patch` is treated as sensitive because it may contain proprietary code.

## Run Artifacts

For the first Cursor turn plus Phase 3 staging, a successful run writes:

```text
cursor/chat.json
cursor/iterations/01/events.jsonl
cursor/iterations/01/stderr.txt
cursor/iterations/01/final.txt
cursor/iterations/01/metadata.json
git/status/01-before-cursor.txt
git/status/01-after-cursor.txt
git/status/01-before-staging.txt
git/status/01-after-staging.txt
git/diffs/01.stat
git/diffs/01.name-only.txt
git/diffs/01.patch
logs/ai_dev_loop.log
```

The first iteration entry in `state.iterations` records:

- `number: 1`;
- `kind: initial_implementation`;
- Cursor prompt/events/stderr/metadata/final paths;
- Cursor exit code;
- Git status and staged diff paths.

It intentionally does not record fake Codex review fields.

## State Transitions

Successful Phase 3 path:

```text
prepared -> validating -> running_cursor -> staging
```

`staging` is now the Phase 3 boundary state meaning Cursor execution and Git staging completed, while Codex review has not started.

Failure behavior:

- preflight/probe/chat/setup failures mark the run `failed`;
- Cursor timeout marks the run `interrupted`;
- Cursor nonzero exit marks the run `failed`;
- staging validation failures mark the run `failed` and preserve diagnostics;
- staging Git helper failures mark the run `failed`;
- staging artifact write failures mark the run `failed`.

No automatic rollback or unstaging is performed after staging failures.

## Review Findings Fixed During Phase 3

The following review finding was confirmed and fixed during the Phase 3 review cycle:

- Staging failures after `begin_staging()` could leave a run stuck in `staging`.
  - Cause: `start_run()` persisted `status=staging`, then only handled `ValidationError` from `run_git_staging()`.
  - Risk: `AiDevLoopError` from Git helpers or `OSError` from artifact writes could escape without `_fail_run()`, leaving no `last_error`.
  - Fix: the staging block now catches `ValidationError`, `AiDevLoopError`, and `OSError`; it marks the run `failed` with diagnostics before re-raising.
  - Regression tests added:
    - Git command failure via monkeypatched `git_status_porcelain`;
    - artifact write failure via monkeypatched `atomic_write_text`;
    - existing `git add -A` failure coverage retained.

The final Codex review pass reported no actionable findings.

## Tests

At the end of Phase 3, the implementing agent reported `88` passing tests.

Coverage added or extended for Phase 3 includes:

- successful `start` stages tracked modifications;
- successful `start` stages untracked files;
- staged artifacts are written;
- staged patch uses sensitive permissions where supported;
- state remains `staging` after the Phase 3 boundary;
- state result reports that Git staging is complete and Codex review is pending;
- first iteration records Cursor and Git paths without fake Codex data;
- Cursor chat ID is still created once and reused when already present;
- Cursor failure prevents staging;
- Cursor timeout prevents staging;
- Cursor self-staging is rejected;
- tracked prompt-source modification is rejected;
- repository plan modification after Cursor is rejected;
- `git add -A` failure marks the run failed;
- Git command failure during staging marks the run failed;
- staging artifact write failure marks the run failed;
- no staged changes after `git add -A` marks the run failed;
- status output reports the Phase 3 boundary correctly;
- Git helper functions parse staged/worktree paths and validate staging safety.

## Validation Results

The implementing agent reported these passing:

```text
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s   # 88 passed
uv run python -m build
uv run ai_dev_loop --help
```

During Codex review:

```text
git diff --cached --check        # passed
uv run python -m pytest -q -s tests/integration/test_start_staging.py
```

The targeted staging suite passed:

```text
14 passed in 120.68s
```

## Current Repository Notes

At the time this handoff was created, `main` was clean and ahead of `origin/main` by five commits. Recent commits include:

- Phase 3 plan and prompt creation.
- Phase 3 implementation.
- A later plan/documentation/logging-structure update that mentions `logs/events.jsonl`.

Repo-local governance files remain:

```text
.cursor/rules/ai-dev-loop-governance.mdc
.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc
.agents/skills/create-cursor-plan/SKILL.md
.agents/skills/review-staged-changes/SKILL.md
```

No `AGENTS.md` file is present.

The earlier Windows metadata artifact `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` should still be treated as unrelated and not modified unless explicitly requested.

This handoff file itself may need to be staged separately if the user wants it included with the Phase 3 history.

## Phase 4 Handoff Notes

Recommended next direction:

1. Implement the Codex review runner after reliable staging.
2. Use the staged diff artifacts and current repository staged changes as the review input.
3. Resume the exact original Codex session ID from `state.codex.session_id`.
4. Invoke the configured review skill explicitly in the review wrapper prompt.
5. Use schema-constrained Codex output with `codex-review-result-v1.json`.
6. Store raw Codex JSONL events, final structured JSON, Markdown review report, and the exact Cursor correction prompt.
7. Keep the orchestrator from generating or rewriting the correction prompt itself.
8. If findings exist, stop at a clear boundary or implement the next Cursor correction turn only in the phase that explicitly owns that behavior.
9. Preserve the same Cursor chat ID for future correction turns.
10. Consider the master plan's structured event log requirement before adding more recovery behavior.

Use the probed Codex CLI option order from Phase 0:

```text
codex exec
--cd
<repo-root>
--sandbox
workspace-write
resume
--model
<review-model>
--json
--output-schema
<codex-review-result-v1.json>
--output-last-message
<iteration-result.json>
<session-id>
-
```

Carry forward these non-negotiable constraints:

- Do not create commits, tags, pushes, stashes, resets, or cleans.
- Do not use `shell=True`.
- Do not execute text returned by agents.
- Do not run real model activity in automated tests.
- Do not store authentication tokens or full process environments.
- Keep all run state outside the target repository.
- Preserve enough artifacts for manual recovery.
- Reuse the same Cursor chat ID for all later turns in the run.
- Resume the exact original Codex session for every future review.

## Open Issues

None known from the final Codex review pass for Phase 3.
