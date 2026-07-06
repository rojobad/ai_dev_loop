# Phase 6 Plan: Real Abort

## Goal or Goals

Implement a real `ai_dev_loop abort <run-id>` command.

After Phase 6, a user must be able to request cancellation of an active local run without losing auditability or corrupting the target repository. `abort` must:

1. detect whether the run has an active Cursor or Codex child process;
2. request termination of the complete child process group;
3. persist enough abort metadata for the running workflow to stop cleanly;
4. mark the run `aborted`;
5. preserve repository contents and staged changes;
6. preserve all existing run artifacts and logs;
7. leave completed, failed, max-iteration, and residual-risk runs untouched except for clear no-op/refusal output.

This phase is intentionally small and sharp. It should complete the process-control surface that Phase 5 left pending, while leaving global Codex integrations for Phase 7.

## Non-Goals

- Do not implement global Codex skill installation.
- Do not implement the SessionStart hook.
- Do not implement hook trust handling.
- Do not implement `integrations install`.
- Do not implement `integrations uninstall`.
- Do not repair arbitrary corrupted or manually edited run state beyond clear validation errors.
- Do not add destructive cleanup, rollback, reset, clean, stash, commit, tag, push, or unstaging behavior.
- Do not invoke real Cursor or Codex model activity in automated tests.
- Do not change the completed bounded loop semantics from Phase 5 except where needed to observe abort requests safely.

## Scope

Implement Phase 6 as defined by:

- `plan-2-build-ai-dev-loop-orchestrator.md`;
- `phase-0-findings.md`;
- `phase-1-findings.md`;
- `phase-2-findings.md`;
- `phase-3-findings.md`;
- `phase-4-findings.md`;
- `phase-5-findings.md`;
- `plans/phase-5-bounded-review-fix-loop-and-resume.md`;
- this plan.

This phase owns:

1. A real `ai_dev_loop abort <run-id>` command wired through the CLI.
2. Durable active-child-process metadata for Cursor and Codex subprocesses.
3. A durable abort-request marker that can be written even while the main workflow holds run/repository locks.
4. Workflow checks that stop at safe points when an abort is requested.
5. Process-group termination from the abort command.
6. State transition to `aborted` without overwriting user work, staged changes, or agent artifacts.
7. Status, logs, inspect, README, and tests updates needed to describe real abort behavior.

## Out of Scope

- Do not use `abort` as a recovery or cleanup command for failed or completed runs.
- Do not remove locks by deleting lock files.
- Do not reset, clean, stash, unstage, or otherwise rewrite Git state after aborting.
- Do not delete partial Cursor or Codex artifacts.
- Do not kill processes unless they are tied to the selected run by durable active-process metadata.
- Do not add a broad process supervisor or daemon.
- Do not change Cursor chat identity, Codex session identity, prompt ownership, or review-result decision rules.
- Do not modify or remove unrelated files or metadata artifacts, including `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` if present.
- Do not print full prompts, fix prompts, staged patches, review Markdown, raw JSONL, full session IDs, auth payloads, full process environments, or secret-like values in default CLI output.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `phase-3-findings.md`
- `phase-4-findings.md`
- `phase-5-findings.md`
- `plans/phase-5-bounded-review-fix-loop-and-resume.md`
- `plans/prompt_phase-5-bounded-review-fix-loop-and-resume.txt`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Important current implementation facts:

- Phase 5 already implemented the bounded Cursor/Codex review-fix loop.
- Phase 5 already implemented real `resume <run-id>`.
- `src/ai_dev_loop/commands/placeholders.py` still backs the placeholder `abort`.
- `src/ai_dev_loop/commands/integrations.py` still has placeholder install/uninstall behavior and must not be expanded in this phase.
- `src/ai_dev_loop/process.py` already starts streaming subprocesses in new process groups and terminates those groups on timeout.
- `src/ai_dev_loop/workflow_engine.py` owns `start_run()`, `resume_run()`, and the shared continuation loop.
- `src/ai_dev_loop/locking.py` uses advisory run and repository locks. A separate `abort` command cannot assume it can acquire the run lock while `start` or `resume` is active.
- `RunStatus.ABORTED` and transitions into `aborted` already exist for active/resumable statuses.
- Tests use fake `agent` and fake `codex` executables in `tests/conftest.py`; extend those fakes rather than invoking real model activity.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: state, locks, runners, Git safety, process execution, and phase-boundary contracts.
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`: persisted state, schema, manifest, artifact, and recovery checkpoint contracts.
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`: Codex review execution, structured review result, event logging, privacy, and artifact contracts.
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`: bounded loop, correction prompt ownership, multi-iteration artifacts, correction staging safety, resume idempotency, privacy, and tests.
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`: abort requests, active process metadata, process-group termination, state preservation, stale-metadata safety, privacy, and tests. This rule is required.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format for reviewing staged changes in this `ai_dev_loop` repository.

No `AGENTS.md` file is present.

## Architecture Guardrails

- `ai_dev_loop` is a deterministic local orchestrator. It transports exact prompts and schema-validated decisions; it does not replace Codex or Cursor reasoning.
- Store run state, locks, logs, process metadata, abort requests, prompts, staged patches, and agent artifacts under XDG-managed `ai_dev_loop` paths outside target repositories.
- Use user-only permissions where supported: directories `0700`; prompt/session/agent-output/review/patch/process-control files `0600`.
- All subprocess calls must use argument arrays with `shell=False`.
- Treat all agent output as untrusted data.
- Do not execute text returned by agents.
- Never log full process environments, tokens, API keys, auth payloads, full prompts, full patches, or raw review content in default user-facing output.
- Create at most one Cursor chat per run. Persist and reuse `state.cursor.chat_id`.
- Every Codex review must resume the exact `state.codex.session_id`.
- The same resumed Codex session must author every `cursor_fix_prompt`.
- The structured Codex JSON result remains the source of truth for loop decisions.
- Do not commit, tag, push, reset, clean, stash, or unstage.
- Fail safely on ambiguous process-control state rather than killing unrelated processes.

## Design Requirements

### Abort Request Marker

Add a durable abort request file inside the run directory. A suggested path is:

```text
locks/abort-request.json
```

or another clearly named run-relative control path. The file should contain at least:

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "requested_at": "<UTC iso timestamp>",
  "requested_by_pid": 12345,
  "reason": "user_requested_abort"
}
```

Requirements:

- Write it atomically.
- Treat it as sensitive/control metadata and apply `0600` where supported.
- Do not remove it until there is a clear reason. Leaving it in place for auditability is acceptable.
- The running workflow must check for it before starting a new Cursor turn, staging pass, Codex review, or review processing step.
- The running workflow must also check it after child process completion and before converting killed child results into generic failures.

### Active Process Metadata

Persist active child process metadata as soon as a Cursor or Codex streaming subprocess starts. A suggested path is:

```text
locks/active-process.json
```

The metadata should include at least:

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "component": "cursor",
  "iteration": 1,
  "pid": 12346,
  "pgid": 12346,
  "parent_pid": 12345,
  "started_at": "<UTC iso timestamp>",
  "cwd": "/absolute/target/repo",
  "argv_redacted": ["agent", "-p", "...", "<redacted-prompt>"]
}
```

Requirements:

- Write it atomically and with sensitive permissions.
- Include enough information for `abort` to decide whether it can safely signal the process group.
- Redact prompt content and other sensitive argv values.
- Clear or mark the active-process metadata when the child completes normally, fails, times out, or is aborted.
- Do not store full process environments.
- Do not log raw prompts or full command arguments in human output.

### Process Runner Changes

Update `run_process_streaming()` or wrap it with a higher-level registration helper so Cursor and Codex executions can register their active child process.

Requirements:

- Keep `shell=False`.
- Keep `start_new_session=True`.
- Capture `pid` and `pgid` after `Popen`.
- Continue terminating the whole process group on timeout.
- Ensure metadata cleanup happens in `finally`.
- Preserve stdout/stderr capture behavior and sensitive file permissions.
- Avoid breaking existing tests that do not need process registration.

### Abort Command Semantics

Implement `ai_dev_loop abort <run-id>`.

The command should:

1. Load the run by ID.
2. Refuse terminal states where abort is not meaningful:
   - `completed`;
   - `completed_with_residual_risk`;
   - `max_iterations_reached`;
   - `failed`;
   - `aborted`.
3. Write the abort request marker.
4. Inspect active-process metadata.
5. If a live active child process is registered, signal the recorded process group:
   - send `SIGTERM`;
   - wait a short bounded grace period;
   - send `SIGKILL` if still alive.
6. If no live active child process is registered:
   - if no active workflow lock is held, acquire the normal mutation locks and mark the run `aborted`;
   - if a workflow lock is held, leave the abort request marker and report that the active workflow should stop at the next safe checkpoint.
7. Append human and structured logs.
8. Return concise output with the final/requested abort status and any active process signal result.

Do not delete lock files, do not release locks owned by another process manually, and do not mutate Git state.

### Workflow Integration

Update the shared workflow engine so active `start` and `resume` commands honor abort requests.

Requirements:

- Check the abort request marker before dispatching each planned workflow action.
- Check again after each action.
- If abort is requested before a child process starts, transition to `aborted`, save state, append logs/events, and return a normal workflow result with status `aborted`.
- If a Cursor or Codex child was killed due to abort, mark `aborted` instead of `failed` or `interrupted`.
- Do not run staging after an abort request has been observed.
- Do not run Codex review after an abort request has been observed.
- Do not send another Cursor correction after an abort request has been observed.
- Preserve whatever repository contents and staged changes exist at the point of abort.
- Do not overwrite partial artifacts from the interrupted child.

### State And Logging

Add helper functions rather than scattering ad hoc JSON reads/writes.

Suggested module:

```text
src/ai_dev_loop/abort_control.py
```

or another focused name. It can own:

- abort request path;
- active process path;
- request writing;
- request reading;
- active process registration;
- active process clearing/marking;
- process-group signaling helpers;
- stale metadata validation helpers.

Add a transition helper such as `mark_aborted(state, message)` if one does not already exist.

Structured events should include, where relevant:

- `abort_requested`;
- `active_process_registered`;
- `active_process_cleared`;
- `active_process_signaled`;
- `workflow_aborted`;
- `abort_no_active_process`;
- `abort_refused_terminal_state`;
- `abort_stale_process_metadata`.

Structured event details must be redacted and must not include prompts or full environments.

### Safety Around Stale Metadata

Fail safely when process metadata is ambiguous.

At minimum:

- Validate `run_id` matches the selected run.
- Validate `pid` and `pgid` are positive integers.
- Check whether the process is alive before signaling.
- Prefer signaling only when active metadata is associated with a live workflow parent or otherwise clearly current.
- If metadata is stale, do not kill arbitrary process groups. Report the stale metadata and mark or clear it only when safe.

The exact stale-process policy may be conservative. It is better to leave a manual diagnostic than to kill an unrelated process.

## Implementation Plan

1. Keep the Phase 6 abort governance rule aligned with the implementation.
   - Treat `.cursor/rules/ai-dev-loop-abort-contracts.mdc` as required.
   - If implementation decisions refine abort semantics, update this plan and the rule together.
   - Do not weaken the non-destructive abort, process-signaling safety, privacy, or testing contracts.

2. Add abort-control primitives.
   - Create a focused module for abort request and active process metadata.
   - Define typed dataclasses or Pydantic models for `AbortRequest` and `ActiveProcess`.
   - Add atomic read/write helpers using existing persistence patterns.
   - Add process liveness and process-group signaling helpers.

3. Register active Cursor and Codex subprocesses.
   - Extend `run_process_streaming()` with an optional registration object, or wrap calls in `runners/cursor.py` and `runners/codex.py`.
   - Persist active metadata immediately after `Popen` starts.
   - Clear or mark active metadata in `finally`.
   - Redact prompt arguments.
   - Keep existing timeout behavior intact.

4. Add workflow abort checks.
   - Add a helper such as `_abort_if_requested(run_directory, state)`.
   - Check before the continuation loop dispatches each action.
   - Check after Cursor, staging, review, and review processing.
   - When requested, transition to `aborted`, save state, append logs/events, and return a `WorkflowResult`.
   - Ensure child termination caused by abort is not reported as a generic Cursor/Codex failure.

5. Implement the abort command.
   - Add `src/ai_dev_loop/commands/abort.py`.
   - Wire `ai_dev_loop abort <run-id>` in `src/ai_dev_loop/cli.py`.
   - Remove the placeholder path for abort.
   - Provide concise text output.
   - Add `--output json` only if it fits the existing CLI output conventions for mutation commands; otherwise keep text-only and document that choice.
   - Use clear exit codes through existing `AiDevLoopError` patterns.

6. Update read-only command output where needed.
   - `status` should show whether an abort request exists and whether an active process is registered.
   - `inspect` should list abort/control artifact paths without printing sensitive process details by default.
   - `logs` should render abort-related events in the existing style.

7. Update documentation.
   - README should describe:
     - when to use `abort`;
     - what `abort` does and does not do;
     - that staged changes and working tree contents are preserved;
     - how to inspect/resume-manually after an abort;
     - that integrations remain pending for the next phase.
   - Update any stale "abort is a placeholder" text.

8. Add tests.
   - Unit-test abort request read/write and permissions where practical.
   - Unit-test active process metadata redaction and stale validation.
   - Unit-test process-group signaling helpers with safe fake processes.
   - Integration-test aborting a running fake Cursor execution.
   - Integration-test aborting a running fake Codex review.
   - Test abort requested while no child is active marks a resumable non-terminal run `aborted`.
   - Test terminal-state abort refusal/no-op behavior.
   - Test workflow does not continue to staging/review/correction after abort is requested.
   - Test staged and unstaged repository changes are preserved.
   - Test status/logs/inspect include abort information without sensitive leakage.

9. Validate.
   - Run focused tests first.
   - Then run the full validation suite:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
```

Use `-s` for pytest in this Codex desktop environment if capture-related tempfile issues appear before collection.

## Acceptance Criteria

- `ai_dev_loop abort <run-id>` no longer returns the placeholder exit code.
- `abort` can request termination of an active Cursor or Codex child process group.
- Active child process metadata is persisted while the child is running and cleared or marked when done.
- The workflow observes abort requests and stops at safe points.
- A killed child caused by a user abort results in run status `aborted`, not a misleading generic failure.
- Repository contents and staged changes are preserved.
- No commits, tags, pushes, resets, cleans, stashes, or unstaging are introduced.
- Abort logs and structured events are written.
- `status`, `logs`, and `inspect` expose useful abort diagnostics without leaking sensitive data.
- Terminal states are not mutated by abort except for clear no-op/refusal output.
- Automated tests use fake CLIs and do not invoke real model activity.
- Full validation passes.

## Phase 7 Boundary

Phase 7 should implement the global Codex integrations that remain pending:

- global handoff skill under `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`;
- SessionStart hook script;
- safe merge into `~/.codex/hooks.json`;
- trust instructions;
- `integrations install`;
- `integrations uninstall`;
- improved `integrations status`;
- tests using a temporary `HOME`.

Do not start that work in Phase 6.

## Open Questions

None. Use a conservative stale-process policy: if metadata is not clearly tied to the selected active run, do not signal the process group.
