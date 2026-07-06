# Phase 4 Findings

This file summarizes the Phase 4 implementation and review cycle. It is intended as a handoff artifact so Phase 5 can proceed without depending on prior chat history.

## Scope

Phase 4 implemented Codex review after the existing Cursor execution and Git staging flow.

The implemented slice advances a fresh prepared run through:

- existing start preflight;
- run and repository locking;
- local Git/Cursor/Codex CLI probes;
- Cursor chat creation or reuse;
- one initial headless Cursor execution;
- post-Cursor Git staging with `git add -A` for `stage_mode: all`;
- structured orchestrator event logging at `logs/events.jsonl`;
- Codex review by resuming the exact prepared Codex session;
- schema and cross-field validation of the Codex review result;
- durable Codex review artifacts;
- a terminal no-finding state, residual-risk state, or `waiting_for_cursor_fix` boundary.

It still does not implement the complete automated loop. In particular, Phase 4 does not implement Cursor correction turns from the stored fix prompt, full `resume` recovery semantics, `abort` child-process termination, bounded multi-iteration review/fix behavior, global Codex skill installation, SessionStart hook installation, or integrations install/uninstall.

After a successful Phase 4 `start`, the target repository may contain Cursor-modified files and those changes remain staged. Phase 4 does not commit, push, tag, reset, clean, stash, or unstage anything.

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

As in earlier phases, use `-s` for pytest in this Codex desktop environment if capture-related tempfile issues appear before collection.

The implementing agent reported final full validation passing with `120` tests, plus ruff, mypy, build, and `ai_dev_loop --help`.

The final staged Codex review pass also ran:

```bash
git diff --cached --check
uv run python -m ruff check src/ai_dev_loop/runners/codex.py src/ai_dev_loop/process.py src/ai_dev_loop/commands/logs.py tests/unit/test_codex_runner.py tests/unit/test_process.py tests/unit/test_logs.py tests/integration/test_start_codex_review.py
uv run python -m pytest -q -s tests/unit/test_process.py tests/unit/test_logs.py tests/unit/test_codex_runner.py tests/integration/test_start_codex_review.py
```

The targeted review validation passed with `33 passed`.

## Delivered Files And Structure

Phase 4 added or materially updated:

- `plans/phase-4-codex-review-runner.md`: Phase 4 implementation plan.
- `plans/prompt_phase-4-codex-review-runner.txt`: concise Cursor handoff prompt.
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`: Codex review execution, schema, privacy, artifact, prompt, and phase-boundary contracts.
- `src/ai_dev_loop/event_log.py`: append-only structured orchestrator event logging.
- `src/ai_dev_loop/review_result.py`: typed Codex review result model, cross-field validation, and status mapping.
- `src/ai_dev_loop/runners/codex.py`: real Codex review runner, review prompt builder, command construction, artifact persistence, and result messages.
- `src/ai_dev_loop/process.py`: stdin-capable process execution with timeout coverage for the full stdin write/read cycle.
- `src/ai_dev_loop/commands/start.py`: integrates review after staging and records Phase 4 outcomes.
- `src/ai_dev_loop/commands/start_preflight.py`: adds `reviewing`, completion, residual-risk, and waiting-for-fix transition helpers.
- `src/ai_dev_loop/commands/prepare.py`: creates the initial `logs/events.jsonl` event.
- `src/ai_dev_loop/commands/status.py`: updates next-action text for Phase 4 statuses.
- `src/ai_dev_loop/commands/inspect.py`: reports iteration review summaries and Codex report paths.
- `src/ai_dev_loop/commands/logs.py`: renders Codex logs as summaries and artifact metadata with sensitive content redacted.
- `README.md`: documents actual Phase 4 behavior and remaining non-goals.
- `tests/conftest.py`: extends the fake `codex` executable for review execution scenarios.
- `tests/integration/test_start_codex_review.py`: integration coverage for successful review, findings, residual risk, invalid JSON, nonzero exit, timeout, privacy, and staged-change persistence.
- `tests/unit/test_codex_runner.py`: unit coverage for command shape, prompt content, review-result validation, and safe failure messaging.
- `tests/unit/test_event_log.py`: structured event log JSONL and redaction coverage.
- `tests/unit/test_logs.py`: Codex log rendering and redaction coverage.
- `tests/unit/test_process.py`: stdin, timeout, and non-duplicated timeout-output coverage.
- Existing Cursor and staging integration tests were updated because a successful `start` now continues past the Phase 3 `staging` boundary into Codex review.

The master plan was also updated to clarify that this repository's local `.agents/skills/review-staged-changes` is for reviewing `ai_dev_loop` implementation work only. Automated target-repository runs must use the exact `codex.review_skill` value from the prepared target repository configuration, commonly `review-staged-cursor-execution`, and must not validate that skill by inspecting this repository's `.agents/skills` directory.

## CLI Surface

Implemented by the end of Phase 4:

- `ai_dev_loop prepare`
- `ai_dev_loop start <run-id>`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop logs <run-id> --component codex`
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
4. Appends `start_requested` to `logs/events.jsonl`.
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
12. Executes Cursor headlessly using the prepared prompt snapshot as one positional argument.
13. Captures Cursor stdout/stderr and parsed metadata.
14. Captures Git status after Cursor execution.
15. On successful Cursor execution, transitions to `staging`.
16. Runs the Phase 3 staging runner:
   - validates `stage_mode: all`;
   - rejects pre-existing staged changes;
   - re-validates the repository plan hash;
   - rejects tracked prompt-source changes;
   - runs `git add -A`;
   - captures staged diff artifacts;
   - validates non-empty staged paths;
   - rejects staged prompt-source paths;
   - re-validates the repository plan hash again.
17. Records the first iteration with Cursor and Git metadata.
18. Transitions to `reviewing`.
19. Sets `state.workflow.current_review_iteration = 1`.
20. Runs Codex review using the exact prepared Codex session ID.
21. Validates the structured review result.
22. Updates the first iteration with real `codex` and `review` sections.
23. Chooses the final Phase 4 outcome from structured JSON fields:
   - `completed` for no actionable findings with tests passed or not applicable;
   - `completed_with_residual_risk` for no actionable findings when tests failed, were blocked, or findings were skipped/present according to the structured status;
   - `waiting_for_cursor_fix` when actionable findings exist.

The CLI still prints the Codex TUI warning before running `start_run()`:

```text
Important: exit the active Codex TUI before continuing with start.
```

## Codex Review Runner Details

The review runner lives in `src/ai_dev_loop/runners/codex.py`.

The command shape is:

```text
codex exec
--cd
<repo-root>
--sandbox
<codex-sandbox>
resume
--model
<review-model>
--json
--output-schema
<codex-review-result-v1.json>
--output-last-message
<codex/reviews/01.json>
<session-id>
-
```

Important details:

- root `codex exec` options such as `--cd` and `--sandbox` are before `resume`;
- resume-specific options such as `--model`, `--json`, `--output-schema`, and `--output-last-message` are after `resume`;
- `--last` is never used;
- the review prompt is supplied on stdin;
- subprocess calls use argument arrays and `shell=False`;
- `cwd` is the target repository root;
- stdout is captured as raw Codex JSONL events;
- stderr is captured separately;
- the output-last-message file is the structured review JSON source of truth;
- raw Codex stdout/stderr remain in sensitive artifacts, not default user-facing errors.

The deterministic review wrapper prompt:

- invokes the configured target-repository review skill exactly as `$<state.codex.review_skill>`;
- reviews the current staged changes only;
- references both the repository plan path and the plan snapshot path;
- includes the original Cursor prompt content directly;
- includes the latest Cursor final response directly when available;
- explicitly states when the latest Cursor final response could not be extracted;
- includes audit artifact paths for plan, prompt, Cursor output, and staged diffs;
- tells Codex to follow the configured review skill's read-only discipline;
- requires a final response conforming to `codex-review-result-v1.json`;
- requires a complete English `cursor_fix_prompt` when actionable findings exist;
- requires `cursor_fix_prompt: null` when there are no actionable findings.

The orchestrator does not generate, rewrite, summarize, or reinterpret the `cursor_fix_prompt`. It only validates and stores the exact prompt returned by the resumed Codex session.

## Review Result Contract

`src/ai_dev_loop/review_result.py` defines `CodexReviewResult`.

The structured result fields are:

- `has_actionable_findings: bool`
- `findings_count: int >= 0`
- `highest_severity: "P0" | "P1" | "P2" | "P3" | null`
- `review_markdown: non-empty string`
- `cursor_fix_prompt: string | null`
- `tests_status: "passed" | "failed" | "skipped_findings_present" | "blocked_environment" | "not_applicable"`
- `summary: non-empty string`

Cross-field rules enforced in Python:

- findings require `findings_count > 0`;
- findings require non-null `highest_severity`;
- findings require non-empty `cursor_fix_prompt`;
- no findings require `findings_count == 0`;
- no findings require `highest_severity is null`;
- no findings require `cursor_fix_prompt is null`;
- Markdown is required for the human report but is not parsed as the machine decision source.

Status mapping:

- actionable findings -> `waiting_for_cursor_fix`;
- no findings plus `tests_status` in `failed`, `blocked_environment`, or `skipped_findings_present` -> `completed_with_residual_risk`;
- no findings plus `passed` or `not_applicable` -> `completed`.

Invalid JSON, missing output-last-message, schema validation errors, and cross-field validation errors mark the run `failed` while preserving raw Codex artifacts where available.

## Structured Event Logging

Phase 4 adds `logs/events.jsonl` under each run directory.

`prepare` writes the initial `prepare_completed` event. `start` appends events for major checkpoints including start request, validation, preflight, probes, Cursor chat/execution, staging, review start, review completion, and failures.

Each event includes:

- `schema_version`;
- UTC timestamp;
- level;
- component;
- event name;
- run ID;
- status when known;
- iteration when relevant;
- artifact path when relevant;
- redacted detail fields.

Event details redact prompt-like fields, patch-like fields, stdin payloads, cursor fix prompts, long string values, and secret-like text via the existing redaction helper.

The older human-readable `logs/ai_dev_loop.log` remains in place.

## Run Artifacts

For the first Cursor turn, Phase 3 staging, and Phase 4 review, a successful run writes:

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
codex/events/01.jsonl
codex/events/01.stderr.txt
codex/reviews/01.json
codex/reviews/01.md
codex/reviews/01.metadata.json
prompts/fixes/01.txt        # only when actionable findings exist
logs/ai_dev_loop.log
logs/events.jsonl
```

Codex event streams, stderr, review JSON, review Markdown, metadata, and fix prompts are treated as sensitive artifacts.

`codex/reviews/01.metadata.json` records command metadata with the stdin prompt redacted. The session ID is still stored there as sensitive metadata, while `logs --component codex` shortens it in default output.

## Iteration Metadata

The first iteration entry in `state.iterations` now records:

- `number: 1`;
- `kind: initial_implementation`;
- Cursor prompt/events/stderr/metadata/final paths;
- Cursor exit code;
- Git status and staged diff paths;
- Codex event/stderr/result/report/metadata paths;
- Codex exit code;
- optional Codex fix prompt path;
- review summary fields:
  - `has_actionable_findings`;
  - `findings_count`;
  - `highest_severity`;
  - `tests_status`;
  - `summary`.

No fake correction-turn data is recorded in Phase 4.

## State Transitions

Successful no-finding path:

```text
prepared -> validating -> running_cursor -> staging -> reviewing -> completed
```

Successful no-finding path with residual risk:

```text
prepared -> validating -> running_cursor -> staging -> reviewing -> completed_with_residual_risk
```

Successful review with actionable findings:

```text
prepared -> validating -> running_cursor -> staging -> reviewing -> waiting_for_cursor_fix
```

Failure behavior:

- preflight/probe/chat/setup failures mark the run `failed`;
- Cursor timeout marks the run `interrupted`;
- Cursor nonzero exit marks the run `failed`;
- staging validation and artifact failures mark the run `failed`;
- Codex timeout marks the run `interrupted`;
- Codex nonzero exit marks the run `failed`;
- missing/invalid structured Codex JSON marks the run `failed`;
- Codex artifact write failures mark the run `failed`.

No automatic rollback or unstaging is performed after staging or review failures.

## Read-Only Commands

`status` now reports Phase 4-aware next actions:

- `reviewing`: wait for `start` to finish or inspect logs if stuck;
- `waiting_for_cursor_fix`: inspect `prompts/fixes/` and `codex/reviews/`; correction execution is not implemented yet;
- `completed`: no actionable findings; changes remain staged;
- `completed_with_residual_risk`: inspect review artifacts and repository state before committing;
- `failed`: inspect `last_error`, `codex/`, and `cursor` artifacts.

`inspect` now includes iteration summaries and Codex report paths when review metadata exists.

`logs --component codex` intentionally does not print raw Codex artifacts by default. It renders:

- state review summary;
- Codex artifact paths and sizes;
- JSONL line counts with content redacted;
- review JSON structured fields with `review_markdown` and `cursor_fix_prompt` redacted;
- metadata with shortened session IDs, including session-like strings in args;
- Markdown and text artifact sizes with content redacted.

This privacy behavior was added after review because raw review content and prompts can include proprietary code, findings, and complete correction instructions.

## Review Findings Fixed During Phase 4

The following review findings were confirmed and fixed during the Phase 4 review cycle:

- Codex stdin timeout could hang outside the configured timeout.
  - Cause: `run_process_streaming` previously wrote stdin in a way that did not reliably put the whole stdin write/read cycle under `communicate(..., timeout=...)`, and `Popen` did not set `stdin=subprocess.PIPE` when stdin text was provided.
  - Risk: Codex review could hang or fail to receive the review prompt, especially with shebang-based fake or real CLIs and large prompt payloads.
  - Fix: `run_process_streaming` now sets `stdin=subprocess.PIPE` when stdin is provided and uses one `proc.communicate(input=stdin_text, timeout=timeout)` call so timeout covers prompt delivery and output collection.
  - Regression test added: child sleeps without reading stdin, large prompt, short timeout.

- `logs --component codex` leaked sensitive Codex artifacts.
  - Cause: Codex component logs originally rendered artifact trees verbatim.
  - Risk: `review_markdown`, `cursor_fix_prompt`, raw JSONL, stderr, full session IDs, and proprietary findings could appear in default CLI output.
  - Fix: Codex log rendering now shows summaries, artifact paths, sizes, counts, and redacted structured fields only.
  - Regression tests added for review JSON redaction, Markdown/text redaction, JSONL redaction, and shortened session IDs.

- Timeout handling duplicated partial process output.
  - Cause: on `TimeoutExpired`, `run_process_streaming` appended `exc.stdout`/`exc.stderr` and then appended output again from the final `proc.communicate()` after termination.
  - Risk: timeout diagnostics and captured artifacts could contain duplicated partial output such as `before\nbefore\n`.
  - Fix: timeout handling now terminates the process group and uses only the final `communicate()` result.
  - Regression test added: process prints once, sleeps, times out, and captured stdout remains exactly one copy.

- Codex nonzero/timeout errors leaked raw process output through user-facing errors.
  - Cause: `run_codex_review` embedded raw `process.stderr`/`process.stdout` in `AiDevLoopError` on nonzero Codex exit.
  - Risk: raw Codex output could flow into `state.last_error`, `logs/ai_dev_loop.log`, and CLI output.
  - Fix: user-facing errors now point to artifact paths only:

    ```text
    Codex review failed with exit code N; inspect codex/events/01.jsonl and codex/events/01.stderr.txt
    Codex review timed out; inspect codex/events/01.jsonl and codex/events/01.stderr.txt
    ```

  - Raw stdout/stderr remain only in the sensitive artifact files.
  - Regression tests added at unit and integration levels.

The final Codex review pass after these fixes reported no actionable findings.

## Tests

At the end of Phase 4, the implementing agent reported `120` passing tests.

Coverage added or extended for Phase 4 includes:

- successful `start` completes after a no-finding Codex review;
- successful `start` leaves target repository changes staged;
- findings review marks `waiting_for_cursor_fix`;
- findings review writes `prompts/fixes/01.txt`;
- no-finding review with blocked environment marks `completed_with_residual_risk`;
- invalid Codex review JSON marks the run `failed`;
- Codex nonzero exit marks the run `failed` with a safe artifact-path error;
- Codex timeout marks the run `interrupted`;
- first iteration includes real `codex` and `review` sections after review;
- current review iteration becomes `1`;
- Codex command resumes the exact prepared session ID;
- Codex command uses `--cd` and `--sandbox` before `resume`;
- Codex command does not use `--last`;
- review prompt includes the configured review skill, original Cursor prompt, and latest Cursor response when available;
- review prompt states when the latest Cursor final response is unavailable;
- structured JSON, not Markdown, drives review decisions;
- cross-field review validation rejects inconsistent findings/no-findings payloads;
- `logs/events.jsonl` exists for prepared runs;
- event logs avoid full prompt leakage;
- `logs --component codex` redacts review reports and fix prompts;
- process stdin support works;
- process timeout covers stdin delivery;
- timeout output is not duplicated.

## Validation Results

The implementing agent reported these passing:

```text
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s   # 120 passed
uv run python -m build
uv run ai_dev_loop --help
```

During Codex review, targeted validation after the final fixes passed:

```text
git diff --cached --check
uv run python -m ruff check src/ai_dev_loop/runners/codex.py src/ai_dev_loop/process.py src/ai_dev_loop/commands/logs.py tests/unit/test_codex_runner.py tests/unit/test_process.py tests/unit/test_logs.py tests/integration/test_start_codex_review.py
uv run python -m pytest -q -s tests/unit/test_process.py tests/unit/test_logs.py tests/unit/test_codex_runner.py tests/integration/test_start_codex_review.py
```

The targeted pytest command passed:

```text
33 passed in 135.77s
```

## Current Repository Notes

At the time this handoff was created, the Phase 4 implementation was staged, and this handoff file itself had just been created separately.

Important staged files include:

```text
.cursor/rules/ai-dev-loop-codex-review-contracts.mdc
README.md
plan-2-build-ai-dev-loop-orchestrator.md
plans/phase-4-codex-review-runner.md
plans/prompt_phase-4-codex-review-runner.txt
src/ai_dev_loop/event_log.py
src/ai_dev_loop/review_result.py
src/ai_dev_loop/runners/codex.py
src/ai_dev_loop/process.py
src/ai_dev_loop/commands/start.py
src/ai_dev_loop/commands/logs.py
tests/integration/test_start_codex_review.py
tests/unit/test_codex_runner.py
tests/unit/test_event_log.py
tests/unit/test_logs.py
tests/unit/test_process.py
```

Repo-local governance files now include:

```text
.cursor/rules/ai-dev-loop-governance.mdc
.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc
.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc
.cursor/rules/ai-dev-loop-codex-review-contracts.mdc
.agents/skills/create-cursor-plan/SKILL.md
.agents/skills/review-staged-changes/SKILL.md
```

No `AGENTS.md` file is present.

The earlier Windows metadata artifact `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` should still be treated as unrelated and not modified unless explicitly requested.

## Phase 5 Handoff Notes

Recommended next direction:

1. Implement Cursor correction turns from `prompts/fixes/NN.txt`.
2. Reuse the same Cursor chat ID for correction turns.
3. Preserve the exact original Codex session ID for every later review.
4. Run the same stage-review-fix cycle within `workflow.max_review_iterations`.
5. Keep status transitions and iteration metadata honest for each correction pass.
6. Do not synthesize or rewrite Codex-authored fix prompts in the orchestrator.
7. Preserve all existing privacy behavior for Codex artifacts, event logs, and user-facing errors.
8. Decide outcomes from structured Codex JSON, never by scraping Markdown.
9. Keep tests on fake `agent` and fake `codex`; do not invoke real model activity.
10. Continue avoiding commits, pushes, resets, cleans, stashes, and unstaging behavior unless a later approved phase explicitly adds one of those operations.

Likely Phase 5 will need to define:

- how `waiting_for_cursor_fix` resumes into `running_cursor`;
- how correction prompts are passed to the existing Cursor chat;
- how correction iteration directories are numbered;
- how Git staging behaves between correction turns when staged changes already exist;
- how to record multiple Codex reviews and multiple Cursor turns in `state.iterations`;
- how max-iteration exhaustion maps to `max_iterations_reached`;
- what `resume <run-id>` owns versus what `start <run-id>` owns.

## Open Issues

None known from the final Codex review pass for Phase 4.
