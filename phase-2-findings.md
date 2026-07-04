# Phase 2 Findings

This file summarizes the Phase 2 implementation and review cycle. It is intended as a handoff artifact so Phase 3 can proceed without depending on prior chat history.

## Scope

Phase 2 implemented the first real `start` slice for `ai_dev_loop`.

The implemented slice advances a prepared run through:

- start preflight;
- run and repository locking;
- local Git/Cursor/Codex CLI probes;
- Cursor chat creation or reuse;
- one initial headless Cursor execution;
- durable Cursor and Git status artifacts;
- a clear Phase 2 boundary state.

It did not implement the complete automated loop. In particular, Phase 2 did not implement Git staging after Cursor turns, Codex review execution, Cursor correction turns, full `resume` recovery semantics, `abort` child-process termination, or global Codex integration install/uninstall.

After a successful Phase 2 `start`, the target repository may contain Cursor-modified files, but nothing is staged automatically.

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

During Phase 2, the implementing agent reported all requested validation passing, ending with `64` passing tests after review fixes.

## Delivered Files And Structure

Phase 2 added or materially updated:

- `plans/phase-2-start-preflight-and-cursor-runner.md`: Phase 2 implementation plan.
- `plans/prompt_phase-2-start-preflight-and-cursor-runner.txt`: concise Cursor handoff prompt.
- `src/ai_dev_loop/commands/start.py`: `start` workflow implementation.
- `src/ai_dev_loop/commands/start_preflight.py`: prepared-run contract validation and state transition helpers.
- `src/ai_dev_loop/runners/cursor.py`: Cursor chat creation, headless command construction, stream-json parsing, and prompt execution.
- `src/ai_dev_loop/runners/probes.py`: local CLI, auth, and Cursor model probes.
- `src/ai_dev_loop/process.py`: process-group subprocess execution with timeout handling and sensitive capture files.
- `src/ai_dev_loop/locking.py`: run and repository worktree locks with metadata.
- `src/ai_dev_loop/paths.py`: repository lock path derivation.
- `src/ai_dev_loop/commands/status.py`: status output for Cursor chat, Phase 2 boundary, and next safe action.
- `src/ai_dev_loop/cli.py`: wires `ai_dev_loop start <run-id>`.
- `README.md`: documents actual Phase 2 behavior without claiming the full loop works.

New tests were added under:

- `tests/integration/test_start_cursor.py`
- `tests/unit/test_cursor_runner.py`
- `tests/unit/test_locking.py`
- `tests/unit/test_process.py`
- `tests/unit/test_start_preflight.py`

Existing test fixtures in `tests/conftest.py` were extended with fake `agent` and `codex` CLIs plus a reusable `prepared_run` fixture.

## CLI Surface

Implemented in Phase 2:

- `ai_dev_loop start <run-id>`

Still placeholders returning exit code `3`:

- `ai_dev_loop resume <run-id>`
- `ai_dev_loop abort <run-id>`
- `ai_dev_loop integrations install`
- `ai_dev_loop integrations uninstall`

Existing Phase 1 commands remain available:

- `ai_dev_loop prepare`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop inspect <run-id>`
- `ai_dev_loop doctor`
- `ai_dev_loop integrations status`

## Start Command Behavior

`ai_dev_loop start <run-id>` now:

1. Loads the prepared run from XDG state.
2. Acquires both the run lock and repository worktree lock.
3. Writes the first `start requested` log entry only after locks are acquired.
4. Validates the run is still in `prepared` status.
5. Transitions to `validating`.
6. Re-validates the prepared contract:
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
7. Probes local tools:
   - `git` executable;
   - Cursor executable;
   - Codex executable;
   - Cursor auth via `agent status --format json`;
   - Cursor model availability via `agent models`;
   - Codex auth via `codex login status`.
8. Creates a Cursor chat with `agent create-chat`, unless `state.cursor.chat_id` already exists.
9. Persists the Cursor chat ID immediately in:
   - `state.json`;
   - `cursor/chat.json`.
10. Transitions to `running_cursor`.
11. Captures Git status before Cursor execution.
12. Executes Cursor headlessly using the prepared prompt snapshot as a single positional argument.
13. Captures Cursor stdout/stderr and parsed metadata.
14. Captures Git status after Cursor execution.
15. On success, transitions to `staging`.
16. Writes a Phase 2 boundary result:

```text
Cursor execution is complete. Git staging, Codex review, corrections, and completion are not implemented yet.
```

The CLI prints the Codex TUI warning before running `start_run()`:

```text
Important: exit the active Codex TUI before continuing with start.
```

## Cursor Execution Details

Cursor command construction follows the Phase 0 findings:

```text
agent
-p
--force
--trust
--workspace
<repo-root>
--resume
<cursor-chat-id>
--model
<cursor-model>
--output-format
stream-json
--sandbox
disabled
<exact-prompt-content>
```

The implementation respects stored config for:

- `cursor.command`
- `cursor.model`
- `cursor.output_format`
- `cursor.force`
- `cursor.trust_workspace`
- `cursor.sandbox`

The prompt is passed as one subprocess argument. It is not sent through stdin and is not shell-interpolated.

Cursor output parsing is intentionally modest. It attempts to extract:

- final/result text;
- error/failure messages;
- parse success/failure.

Raw stdout JSONL is preserved even if parsing fails.

## Run Artifacts

For the first Cursor turn, Phase 2 writes:

```text
cursor/chat.json
cursor/iterations/01/events.jsonl
cursor/iterations/01/stderr.txt
cursor/iterations/01/final.txt
cursor/iterations/01/metadata.json
git/status/01-before-cursor.txt
git/status/01-after-cursor.txt
logs/ai_dev_loop.log
```

Sensitive artifacts are written with user-only permissions where supported:

- `cursor/chat.json`
- `cursor/iterations/01/events.jsonl`
- `cursor/iterations/01/stderr.txt`
- `cursor/iterations/01/final.txt`
- `cursor/iterations/01/metadata.json`
- `state.json`
- `logs/ai_dev_loop.log`

Important implementation detail: sensitive stdout/stderr capture files are created with restrictive permissions before `Popen` starts, using `os.open(..., 0o600)`, so they are not temporarily world-readable during a long Cursor run.

## Locking

Phase 2 uses:

- one run lock under the run directory: `locks/run.lock`;
- one repository worktree lock under XDG state: `repository-locks/<sha256(repo-root)>.lock`.

Lock metadata includes:

- PID;
- run ID;
- repository path;
- start timestamp.

Lock files are outside target repositories.

If repository lock acquisition fails after acquiring the run lock, the run lock is released before the error is re-raised.

`status` remains read-only and usable while locks are held. `inspect` was not materially changed in this phase, but the locking design does not require it to acquire mutation locks.

## State Transitions

Successful Phase 2 path:

```text
prepared -> validating -> running_cursor -> staging
```

Preflight/probe/chat/setup failures mark the run as:

```text
failed
```

Cursor timeout marks the run as:

```text
interrupted
```

Cursor nonzero exit marks the run as:

```text
failed
```

The `staging` status is a Phase 2 boundary state only. Git staging is not implemented yet.

## Review Findings Fixed During Phase 2

The following review findings were confirmed and fixed during the Phase 2 review cycle:

- Cursor run artifacts were initially written without sensitive permissions.
  - Fixed by writing `metadata.json` and `final.txt` with `sensitive=True`.
  - Fixed by making `run_process_streaming()` support sensitive capture files.

- Cursor stdout/stderr capture files were initially chmodded only after process completion.
  - Fixed by creating sensitive capture files with `os.open(..., 0o600)` before `Popen`.
  - Post-write chmod remains as defense in depth.

- Missing executable probe setup failures could leave runs stuck in `validating`.
  - Fixed by returning failed `ProbeResult`s for missing executables and wrapping the full probe block in the failure handler.

- Run lock was not released if repository lock acquisition failed.
  - Fixed by releasing the run lock when repository lock acquisition raises.

- The Codex TUI warning printed only after Cursor execution.
  - Fixed by printing the warning before calling `start_run()`.

- `start_run()` wrote the initial log entry before acquiring locks.
  - Fixed by moving the log write inside the `RunLocks` context.

- Exceptions from Cursor launch or artifact capture could leave a run stuck in `running_cursor`.
  - Fixed by wrapping the Cursor launch/capture block and marking the run `failed` on expected launch/capture exceptions.

## Tests

At the end of Phase 2, the implementing agent reported `64` passing tests.

Coverage added or extended for Phase 2 includes:

- unknown run IDs;
- non-`prepared` run rejection;
- successful `start` path and artifact creation;
- Cursor chat creation and persistence;
- existing Cursor chat reuse;
- branch drift rejection;
- Cursor auth failure;
- missing Cursor model;
- missing Cursor executable marking the run `failed`;
- Cursor nonzero exit;
- Cursor timeout marking the run `interrupted`;
- Cursor launch exception marking the run `failed`;
- run lock contention;
- run immutability when lock acquisition fails;
- repository lock failure releasing the run lock;
- status readability while locked;
- Phase 2 boundary CLI output;
- Codex TUI warning ordering;
- Cursor artifact `0600` permissions;
- sensitive capture file permissions while the child process is still running;
- Cursor command argument construction;
- prompt redaction in metadata args;
- Cursor stream-json parsing;
- lock metadata parsing and process-alive checks;
- start preflight hash and worktree drift checks.

## Validation Results

The implementing agent reported these passing:

```text
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s   # 64 passed
uv run python -m build
uv run ai_dev_loop --help
```

During the Codex review passes, `git diff --cached --check` was run repeatedly and passed.

## Current Repository Notes

The Phase 2 files are staged at the time this handoff was written, except this handoff file itself may need to be staged separately if the user wants it included with the Phase 2 commit.

Repo-local governance files currently include:

```text
.cursor/rules/ai-dev-loop-governance.mdc
.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc
.agents/skills/create-cursor-plan/SKILL.md
.agents/skills/review-staged-changes/SKILL.md
```

No `AGENTS.md` file is present.

The earlier Windows metadata artifact `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` should still be treated as unrelated and not modified unless explicitly requested.

## Phase 3 Handoff Notes

Recommended next direction:

1. Implement Git staging after successful Cursor turns.
2. Capture staged diff artifacts:
   - `git diff --cached --stat`;
   - `git diff --cached --name-only`;
   - full staged patch.
3. Keep `stage_mode: all` constrained by the baseline safety policy.
4. Do not stage prompt files or unrelated pre-existing work.
5. Add durable iteration metadata to `state.iterations` or a typed iteration model.
6. Implement the Codex review runner only after staging checkpoints are reliable.
7. Preserve the exact original Codex session ID for review.
8. Use the probed Codex CLI option order from Phase 0:

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
- Do not run model activity in tests.
- Do not store authentication tokens or full process environments.
- Keep all run state outside the target repository.
- Preserve enough artifacts for manual recovery.
- Reuse the same Cursor chat ID for all later turns in the run.
- Resume the exact original Codex session for every future review.

## Open Issues

None known from the final Codex review pass for Phase 2.
