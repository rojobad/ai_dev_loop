# Phase 2 Plan: Start Preflight + Cursor Runner

## Goal or Goals

Build the next executable slice of `ai_dev_loop`: start-run preflight, run/repository locking, local CLI validation, Cursor chat creation, Cursor headless execution, and durable Cursor artifacts/checkpoints.

This phase should make `ai_dev_loop start <run-id>` able to advance a prepared run through the initial Cursor implementation turn, then stop honestly at the next unimplemented boundary before Git staging and Codex review.

## Non-Goals

- Do not implement the full stage-review-fix loop.
- Do not implement Codex review execution.
- Do not implement Git staging after Cursor turns.
- Do not implement Cursor correction turns from Codex findings.
- Do not implement full `resume` recovery semantics.
- Do not implement `abort` child-process termination beyond any low-level process helpers needed for timeouts.
- Do not implement global Codex skill installation, SessionStart hook installation, hook trust handling, or integrations install/uninstall.

## Scope

Implement Phase 2 as defined by `phase-1-findings.md`:

1. Implement `start` preflight using the prepared run state.
2. Add run and repository locks before any mutation.
3. Verify prepared hashes, branch, HEAD, repository path, plan snapshot, prompt source, and worktree baseline before invoking agents.
4. Verify local executables for `git`, `agent`, and `codex`.
5. Probe Cursor model availability and authentication non-destructively.
6. Probe Codex authentication non-destructively.
7. Create and persist one Cursor chat ID with `agent create-chat`.
8. Implement Cursor headless execution with the Phase 0 command shape, process-group handling, raw JSONL capture, final result extraction, timeout state, and durable checkpoints.
9. Capture pre/post Cursor Git status for the future staging phase without staging changes in this phase.

After a successful Cursor turn, transition the run to `staging` and print a clear Phase 2 boundary message: Cursor execution is complete, but Git staging, Codex review, corrections, and completion are not implemented yet.

## Out of Scope

- Do not call `git add`, `git commit`, `git push`, `git tag`, `git reset`, `git clean`, or `git stash`.
- Do not add or remove global Codex files under `$HOME/.agents` or `$HOME/.codex`.
- Do not create or modify target-repository `ai_dev_loop.yaml` files except in tests/fixtures.
- Do not write orchestrator state into target repositories.
- Do not run real Cursor or Codex model activity in tests.
- Do not delete or alter unrelated user files or metadata artifacts, including `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` if present.
- Do not claim in README or CLI output that the complete automated loop works.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `plans/phase-1-foundation-and-prepare.md`
- `plans/prompt_phase-1-foundation-and-prepare.txt`
- `plans/phase-2-start-preflight-and-cursor-runner.md`
- `plans/prompt_phase-2-start-preflight-and-cursor-runner.txt`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Treat the master plan as the architecture and safety source of truth. Treat this Phase 2 plan as the scope boundary. If this file and the master plan conflict on safety behavior, stop and report the conflict.

Phase 0 local CLI facts to apply:

- Cursor CLI command is `agent`.
- `agent create-chat` exists and returns a plain UUID on stdout.
- `agent status --format json` exists and is a safe auth probe.
- `agent models` exists and listed `composer-2.5-fast`.
- Cursor headless mode supports `-p`, `--force`, `--trust`, `--workspace`, `--resume`, `--model`, `--output-format stream-json`, and `--sandbox enabled|disabled`.
- Cursor prompt content is accepted as a positional argument. Do not send the Cursor prompt on stdin.
- Codex CLI command is `codex`.
- `codex login status` is a safe auth probe.
- For later Codex review execution, the probed CLI requires root `codex exec` options such as `--cd` and `--sandbox` before `resume`; do not implement Codex review execution in this phase.

Phase 1 implementation facts to apply:

- `prepare` is implemented and writes run artifacts under XDG state.
- `start`, `resume`, `abort`, `integrations install`, and `integrations uninstall` are placeholders.
- `src/ai_dev_loop/runners/cursor.py` is a placeholder.
- `src/ai_dev_loop/process.py` is a basic `subprocess.run` helper and needs process-group/streaming support for Cursor.
- `src/ai_dev_loop/locking.py` has a basic `fcntl` lock wrapper and needs integration with run/repository locks.
- The repository uses `uv` for development and validation; do not modify system Python.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails. This rule is required.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: product architecture contracts for state, locks, runners, Git safety, Cursor identity, Codex identity, process execution, and phase boundaries. This rule is required.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format to use later when staged changes are reviewed. Do not invoke it yourself unless the user asks for a review.

No `AGENTS.md` file is present.

## Architecture Guardrails

- Keep `ai_dev_loop` naming consistent across executable, package, XDG paths, schemas, docs, and environment variables.
- Preserve separation between CLI presentation, config, paths, state persistence, process execution, Git safety, Cursor integration, Codex integration, and integration installation.
- Store durable run state and locks under XDG-managed `ai_dev_loop` paths outside target repositories.
- Use user-only permissions where supported: directories `0700`; prompt/session/agent-output files `0600`.
- Use typed models or typed dataclasses for new state and runner contracts. Do not expand untyped dict usage unless it is only a compatibility bridge around existing `iterations`.
- All subprocess calls must use argument arrays and `shell=False`.
- Do not log full process environments, authentication payloads, API keys, tokens, or full account details from `agent status --format json`.
- Treat Cursor output as untrusted data. Parse JSONL only as data; never execute emitted text.
- Create exactly one Cursor chat per run. If `cursor.chat_id` already exists in state, reuse it rather than calling `agent create-chat` again.
- Persist the Cursor chat ID immediately after creation in both `state.json` and `cursor/chat.json` using atomic writes.
- Before invoking Cursor, verify that branch, HEAD, plan hash, prompt hash, source prompt hash when available, and worktree baseline still match the prepared contract.
- Do not use `--resume --last` for any agent.
- Do not run Codex review execution in this phase.
- Do not stage files in this phase. Capture Git status snapshots only.
- Keep CLI output honest: after successful Cursor execution, report that the run has reached the Phase 2 boundary and that staging/review are still pending later phases.

## Implementation Plan

1. Add run resolution helpers needed by `start`.
   - Reuse `run_discovery.py` where possible.
   - Load `state.json`, `manifest.json`, snapshots, and paths from the run directory.
   - Return clear `AiDevLoopError` messages when a run ID is unknown or ambiguous.

2. Implement start preflight in a dedicated command/service module.
   - Validate the run is in `prepared` status.
   - Verify repository root exists and is still a Git repository.
   - Re-run Git discovery and verify branch and HEAD match prepared state.
   - Verify `plan/plan.md` hash matches `state.plan.sha256`.
   - Verify the repository plan path still exists and matches the prepared plan hash.
   - Verify `prompts/cursor-initial.txt` hash matches `state.prompt.sha256`.
   - If the prompt source file still exists, verify it matches the prepared prompt hash.
   - Verify current `git status --porcelain=v2 --untracked-files=all` matches the prepared baseline before invoking Cursor.
   - Validate configured timeouts are positive.
   - Validate the Codex session ID is present.

3. Integrate run and repository locks.
   - Use one lock file under the run directory for the run lock.
   - Use one repository-worktree lock under XDG state, keyed by a stable hash of the repository root path.
   - Store lock metadata: PID, run ID, repository path, and start timestamp.
   - Do not place lock files inside the target repository.
   - If a lock is held by a live process, fail with a clear message.
   - If stale-lock detection is implemented, only remove stale metadata when the process is definitely not live.
   - Ensure read-only `status` and `inspect` remain usable while locks are held.

4. Harden process execution for Cursor.
   - Add a process helper based on `subprocess.Popen`, `start_new_session=True`, and process-group termination on timeout.
   - Capture stdout and stderr separately.
   - Support writing raw stdout/stderr to artifact files while returning parsed metadata.
   - Apply explicit working directories and timeouts.
   - Redact known secret patterns in user-facing errors/logs.
   - Keep the existing simple `run_process` helper working for short probes and Git commands unless replacing it cleanly.

5. Add local CLI probes.
   - Verify configured `git`, Cursor, and Codex commands exist.
   - Probe Cursor auth using `agent status --format json`; record only sanitized success/failure metadata.
   - Probe Cursor models using `agent models`; require an exact match for `state.cursor.model`.
   - Probe Codex auth using `codex login status`; record only sanitized success/failure metadata.
   - Fail before Cursor chat creation if any probe fails.

6. Implement the Cursor runner in `src/ai_dev_loop/runners/cursor.py`.
   - Add a `create_chat` function that runs `[cursor_command, "create-chat"]`, validates a non-empty UUID-like chat ID, and returns it.
   - Add an `execute_prompt` function that builds the Phase 0 command shape:

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

   - Include `--force`, `--trust`, and `--sandbox` according to stored config.
   - Pass the exact prompt snapshot as one positional argument.
   - Do not use shell interpolation.
   - Do not assume stdin prompt support.
   - Parse enough `stream-json` output to identify result/final text and explicit error events, while preserving raw JSONL even if parsing fails.

7. Persist Cursor artifacts and checkpoints.
   - Create `cursor/chat.json` after chat creation.
   - For the initial Cursor turn, create `cursor/iterations/01/`.
   - Persist raw stdout JSONL as `cursor/iterations/01/events.jsonl`.
   - Persist stderr as `cursor/iterations/01/stderr.txt`.
   - Persist final result text, when detected, as `cursor/iterations/01/final.txt`.
   - Persist metadata as `cursor/iterations/01/metadata.json`, including args with prompt content redacted, exit code, elapsed time, timeout flag, and parse status.
   - Capture `git/status/01-before-cursor.txt` before Cursor execution and `git/status/01-after-cursor.txt` after Cursor execution.
   - Append a concise line to `logs/ai_dev_loop.log` for each major checkpoint.

8. Update run state atomically.
   - Transition `prepared -> validating` at start of preflight.
   - Transition `validating -> running_cursor` immediately before invoking Cursor.
   - Persist `cursor.chat_id` immediately after chat creation.
   - On successful Cursor execution, transition `running_cursor -> staging` and record a Phase 2 boundary result/last message that staging and Codex review are pending later phases.
   - On Cursor failure, set `failed` with diagnostics.
   - On Cursor timeout, set `interrupted` or `failed` consistently with existing status semantics and record timeout details.
   - Preserve enough state for a later `resume` implementation to reuse the same Cursor chat ID and artifacts.

9. Wire `ai_dev_loop start <run-id>`.
   - Replace the placeholder with Phase 2 behavior.
   - Print a warning before mutation that the original interactive Codex TUI must be closed.
   - Print concise human output by default.
   - Add `--output json` only if it fits the existing CLI pattern without broad refactoring; otherwise leave JSON output for a later observability phase.
   - Return non-zero on preflight/probe/Cursor failure.
   - On successful Phase 2 boundary, return success but clearly state that the complete automated loop is not implemented yet.

10. Keep later commands honest.
   - Keep `resume` and `abort` placeholders returning exit code `3`.
   - Update status/inspect rendering only as needed to show the Cursor chat ID, phase boundary status, latest error, and next safe action.
   - Update README to describe Phase 2 accurately without claiming the full loop works.

## Testing Criteria

This phase changes CLI behavior, persistence, locking, subprocess execution, Cursor integration, Git validation, and recovery checkpoints. Automated tests are required.

Required test levels:

- Unit tests for preflight validators, hash checks, state transitions, lock path derivation, Cursor JSONL parsing, command argument construction, and sensitive metadata redaction.
- Integration tests using temporary repositories for prepared-run state, branch/HEAD drift, plan/prompt drift, worktree baseline drift, lock contention, and run artifact persistence.
- Integration tests using fake `agent` and `codex` executables placed earlier in `PATH` for auth/model probes, chat creation, Cursor execution, nonzero exits, and timeout handling.
- Regression tests for Phase 1 behavior so `prepare`, `config validate`, read-only inspection, schema availability, and packaging are not broken.

Expected test areas:

- Extend existing `tests/unit/test_state.py`, `tests/unit/test_git.py`, and `tests/integration/test_prepare.py` only where that keeps concerns clear.
- Add focused test files such as `tests/unit/test_cursor_runner.py`, `tests/unit/test_locking.py`, `tests/unit/test_start_preflight.py`, and `tests/integration/test_start_cursor.py` when clearer.
- Use fixture helpers for fake CLIs and temporary XDG directories so tests do not touch real user state, real Cursor, or real Codex.

Edge cases and failure paths that need evidence:

- Unknown run ID.
- Non-`prepared` state.
- Changed branch or HEAD.
- Changed plan snapshot or repository plan file.
- Changed prompt snapshot or existing prompt source file.
- Unexpected worktree change against the prepared baseline.
- Missing executable.
- Cursor auth failure.
- Cursor model missing.
- Codex auth failure.
- Existing `cursor.chat_id` reused without creating another chat.
- Cursor output parse failure still preserves raw JSONL.
- Cursor timeout terminates the process group and records timeout state.

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

Add or update tests for:

- `start` rejects unknown run IDs.
- `start` rejects non-`prepared` states unless the behavior is explicitly supported.
- preflight rejects changed branch.
- preflight rejects changed HEAD.
- preflight rejects changed plan snapshot.
- preflight rejects changed repository plan path.
- preflight rejects changed prompt snapshot.
- preflight rejects changed prompt source when it still exists.
- preflight rejects unexpected worktree changes compared with baseline.
- run lock prevents concurrent mutation.
- repository lock prevents concurrent mutation by another run.
- status/inspect remain read-only while locks are held.
- missing Cursor/Codex executables fail before mutation.
- Cursor auth/model probe failures fail before chat creation.
- `agent create-chat` success persists the chat ID immediately.
- if `cursor.chat_id` exists, chat creation is not repeated.
- fake Cursor execution receives prompt as one positional argument.
- fake Cursor execution writes JSONL, stderr, final text, metadata, and Git status artifacts.
- Cursor nonzero exit records failure without deleting artifacts.
- Cursor timeout terminates the process group and records timeout state.
- no tests invoke real Cursor or Codex model activity.

Use fake executables placed earlier in `PATH` for Cursor and Codex integration tests.

## Risks Or Recovery Notes

- Phase 2 introduces code that can invoke a real local Cursor agent. Automated tests must use fakes, and documentation/CLI output must be clear that the complete loop is still incomplete.
- A successful real `start` in this phase may leave repository files modified but not staged because staging is out of scope. The command must report this Phase 2 boundary clearly.
- If chat creation succeeds and Cursor execution fails, the chat ID must remain persisted so later `resume` work can reuse the same chat.
- Timeout handling must terminate the full child process group, not just the parent process.
- Cursor JSONL format may change. Preserve raw output and treat parsing failures as diagnostics rather than data loss.
- Do not log auth payloads, full environments, or prompt content in process metadata.

## OpenQuestions

None.
