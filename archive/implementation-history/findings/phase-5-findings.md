# Phase 5 Findings

This file summarizes the Phase 5 implementation and review cycle. It is intended as a handoff artifact so later work can proceed without depending on prior chat history.

## Scope

Phase 5 implemented the first complete bounded local Cursor/Codex development loop and made `resume <run-id>` real.

The implemented slice advances a prepared run through:

- start preflight and local CLI probes;
- one Cursor chat creation for the run;
- Cursor initial implementation;
- Git staging and cumulative staged diff capture;
- Codex review by resuming the exact prepared Codex session;
- optional Cursor correction turns using the exact Codex-authored fix prompts from `prompts/fixes/NN.txt`;
- additional staging and reviews;
- termination only when Codex reports no actionable findings, residual-risk completion applies, or `workflow.max_review_iterations` is reached.

Phase 5 also supports durable `resume` from clear checkpoints without changing Cursor chat identity or Codex session identity.

It still does not implement:

- `abort <run-id>` child-process termination;
- global Codex skill installation;
- SessionStart hook installation;
- `integrations install` or `integrations uninstall`;
- commits, pushes, resets, cleanups, rollbacks, stashes, or unstaging.

After a successful Phase 5 `start` or `resume`, target-repository changes remain staged. If the review loop reaches the configured iteration limit with actionable findings still present, the run stops in `max_iterations_reached` with the latest review artifacts and latest fix prompt preserved.

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

The implementing agent reported final full validation passing with:

```text
156 passed
ruff clean
mypy clean
```

The final staged Codex review pass also ran:

```bash
git diff --cached --check
```

No whitespace errors were reported.

## Delivered Files And Structure

Phase 5 added or materially updated:

- `plans/phase-5-bounded-review-fix-loop-and-resume.md`: Phase 5 implementation plan.
- `plans/prompt_phase-5-bounded-review-fix-loop-and-resume.txt`: concise Cursor handoff prompt.
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`: bounded loop and resume governance rule.
- `src/ai_dev_loop/workflow_engine.py`: shared engine for `start` and `resume`.
- `src/ai_dev_loop/resume_planner.py`: checkpoint planning, artifact completion probes, staged-patch review validation, and artifact preservation before retries.
- `src/ai_dev_loop/iterations.py`: multi-iteration lookup, upsert, labels, prompt-path, and staged-patch metadata helpers.
- `src/ai_dev_loop/commands/resume.py`: real resume command wrapper.
- `src/ai_dev_loop/commands/start.py`: thin wrapper over the shared workflow engine.
- `src/ai_dev_loop/commands/start_preflight.py`: resume validation and Phase 5 transition helpers.
- `src/ai_dev_loop/commands/status.py`: multi-iteration status and next-action text.
- `src/ai_dev_loop/commands/logs.py`: multi-review Codex log summaries and artifact-path rendering with sensitive content redacted.
- `src/ai_dev_loop/cli.py`: real `resume` CLI wiring.
- `src/ai_dev_loop/runners/staging.py`: correction-staging safety and iteration-numbered Git artifacts.
- `src/ai_dev_loop/runners/codex.py`: iteration-numbered review artifacts, review metadata updates by iteration number, and fix prompt persistence.
- `src/ai_dev_loop/runners/git.py`: staged-patch validation, correction preflight, and dirty/untracked helpers.
- `src/ai_dev_loop/state.py`: updated transition graph for Phase 5 statuses.
- `README.md`: documents actual Phase 5 behavior and remaining non-goals.
- `tests/integration/test_phase5_loop_resume.py`: integration coverage for full-loop and resume behavior.
- `tests/unit/test_iterations_and_resume.py`: iteration and resume planner coverage.
- `tests/unit/test_staging_correction.py`: correction-staging safety coverage.
- Existing Phase 2, 3, and 4 tests were updated because `start` now runs the complete bounded loop rather than stopping at a single intermediate phase boundary.

## CLI Surface

Implemented by the end of Phase 5:

- `ai_dev_loop prepare`
- `ai_dev_loop start <run-id>`
- `ai_dev_loop resume <run-id>`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop logs <run-id> --component cursor`
- `ai_dev_loop logs <run-id> --component codex`
- `ai_dev_loop inspect <run-id>`
- `ai_dev_loop doctor`
- `ai_dev_loop integrations status`

Still placeholders returning exit code `3`:

- `ai_dev_loop abort <run-id>`
- `ai_dev_loop integrations install`
- `ai_dev_loop integrations uninstall`

Both `start` and `resume` still print the Codex TUI warning before running:

```text
Important: exit the active Codex TUI before continuing with start.
```

The warning text is shared even for `resume`, so do not treat the word `start` there as a semantic difference.

## Shared Workflow Engine

`src/ai_dev_loop/workflow_engine.py` is the center of Phase 5.

It exposes:

- `start_run(run_id)`;
- `resume_run(run_id)`;
- `WorkflowResult`;
- `render_workflow_output(result)`.

`start_run()`:

1. Loads the prepared run.
2. Acquires the run and repository locks.
3. Requires `prepared` status.
4. Transitions to `validating`.
5. Runs start preflight checks.
6. Runs local Cursor/Codex probes.
7. Enters the shared continuation loop.

`resume_run()`:

1. Loads the run.
2. Acquires the same locks.
3. Validates the current status is resumable.
4. Restores `interrupted` runs to the real checkpoint derived from artifacts.
5. Runs start-style validation when resuming from `prepared`.
6. Runs resume preflight checks for non-prepared checkpoints.
7. Handles legacy `waiting_for_cursor_fix` at the iteration limit by marking `max_iterations_reached` without sending another Cursor turn.
8. Transitions `waiting_for_cursor_fix` to `running_cursor` when a correction turn is still allowed.
9. Enters the shared continuation loop.

The continuation loop asks `resume_planner.plan_next_action()` for the next safe action and dispatches exactly one of:

- Cursor turn;
- staging pass;
- Codex review pass;
- processing an already-existing review result.

Loop decisions are based only on schema-validated Codex JSON. The orchestrator does not parse Markdown to decide outcomes.

## Iteration Numbering And Artifacts

Iteration numbering is stable and 1-based:

- `01`: initial Cursor implementation using `state.prompt.snapshot_path`.
- `02`: first Cursor correction using `prompts/fixes/01.txt`.
- `NN` where `NN > 1`: correction using the fix prompt produced by review `NN-1`, stored at `prompts/fixes/{NN-1:02d}.txt`.

Review `NN` writes:

- `codex/events/{NN}.jsonl`
- `codex/events/{NN}.stderr.txt`
- `codex/reviews/{NN}.json`
- `codex/reviews/{NN}.md`
- `codex/reviews/{NN}.metadata.json`
- `prompts/fixes/{NN}.txt` when actionable findings exist

Cursor iteration `NN` writes:

- `cursor/iterations/{NN}/events.jsonl`
- `cursor/iterations/{NN}/stderr.txt`
- `cursor/iterations/{NN}/metadata.json`
- `cursor/iterations/{NN}/final.txt` when a final message was parsed

Git staging for iteration `NN` writes:

- `git/status/{NN}-before-cursor.txt`
- `git/status/{NN}-after-cursor.txt`
- `git/status/{NN}-before-staging.txt`
- `git/status/{NN}-after-staging.txt`
- `git/diffs/{NN}.stat`
- `git/diffs/{NN}.name-only.txt`
- `git/diffs/{NN}.patch`

Staged diff artifacts are cumulative snapshots of the current staged patch at that iteration. They are not necessarily incremental deltas.

## State And Metadata

`state.iterations` is now multi-iteration aware.

`src/ai_dev_loop/iterations.py` provides:

- `iteration_label(number)`;
- `find_iteration(state, number)`;
- `upsert_iteration(state, entry)`;
- `max_iteration_number(state)`;
- `iteration_kind(number)`;
- `fix_prompt_path(review_number)`;
- `cursor_prompt_path(state, iteration_number)`;
- `read_cursor_prompt(state, run_directory, iteration_number)`;
- `iteration_staged_patch_rel_path(state, iteration_number)`;
- `previous_iteration_git_patch_path(state, iteration_number)`.

Important behavior:

- Iteration entries are looked up by `number`, not by list index.
- `upsert_iteration()` merges nested `cursor`, `git`, `codex`, and `review` sections without wiping previously recorded fields.
- Earlier completed iterations must not be overwritten by later correction turns.
- Artifact paths stored in state are repository-run-relative paths, not absolute paths.
- Correction iterations use `kind: cursor_correction`; iteration `01` uses `kind: initial_implementation`.

## Cursor Behavior

Cursor chat identity is single-run scoped.

Important contracts:

- `agent create-chat` may create a chat only for a genuinely fresh run with no workflow progress.
- Checkpointed runs with progress but no `state.cursor.chat_id` fail safely.
- Every Cursor turn reuses `state.cursor.chat_id`.
- Cursor prompts are passed through `execute_prompt()` as before.
- Iteration `01` uses the prepared prompt snapshot exactly.
- Correction iterations read the exact persisted fix prompt from `prompts/fixes/NN.txt`.
- The orchestrator must not synthesize, summarize, translate, rewrite, or improve Codex's `cursor_fix_prompt`.
- Missing or empty Cursor prompts now fail the run with persisted `FAILED` status and `last_error`.

Before retrying a partial Cursor turn, existing partial artifacts are copied to `*.attempt-<timestamp>.*` names instead of being silently overwritten.

## Codex Review Behavior

Codex session identity remains exactly the session captured at `prepare`.

Important contracts:

- Every review uses `codex exec ... resume ... <state.codex.session_id> -`.
- Root `codex exec` options such as `--cd` and `--sandbox` remain before `resume`.
- Resume-specific options such as `--model`, `--json`, `--output-schema`, and `--output-last-message` remain after `resume`.
- `--last` is never used.
- Reviews invoke the exact configured target-repository skill as `$<state.codex.review_skill>`.
- This repository's `.agents/skills/review-staged-changes` is only for reviewing `ai_dev_loop` implementation work. Automated target-repository reviews must not validate or choose skills by inspecting this repository's `.agents/skills`.
- `run_codex_review()` updates iteration metadata by iteration number.
- Review result JSON is loaded and validated through the existing Phase 4 review-result model.
- When actionable findings exist, `cursor_fix_prompt` is persisted to `prompts/fixes/{NN}.txt`.

Before retrying a partial Codex review, existing events, stderr, and review JSON artifacts are preserved as `*.attempt-<timestamp>.*`.

## Git Safety

Phase 5 intentionally keeps target repository changes staged and never commits, pushes, resets, cleans, stashes, or unstages.

Initial staging keeps the Phase 3 behavior:

- reject pre-existing staged changes;
- run `git add -A`;
- reject empty staged diffs;
- reject staging the prompt source;
- validate the repository plan hash before and after staging.

Correction turns add stricter safety:

- before Cursor correction, the current staged patch must exactly match the previous iteration's recorded staged patch artifact;
- before Cursor correction, unstaged tracked changes are rejected;
- before Cursor correction, untracked files are rejected;
- before correction staging, the current staged patch must still exactly match the previous iteration's recorded staged patch artifact;
- correction staging allows the previous orchestrator-staged patch to remain in the index, then runs `git add -A` to update the cumulative staged patch;
- an emptied index is rejected because it no longer matches the previous recorded staged patch.

Review and review-processing also validate safety:

- before running Codex review, the staged patch must match the current iteration's recorded staged patch artifact;
- before processing an already-existing review JSON during `resume`, the staged patch must still match the current iteration's recorded staged patch artifact;
- both paths reject extra unstaged tracked changes and untracked files.

The relevant helpers live in `src/ai_dev_loop/runners/git.py`:

- `git_status_porcelain()`;
- `paths_with_unstaged_changes()`;
- `paths_with_untracked()`;
- `validate_staged_patch_matches_artifact()`;
- `validate_correction_pre_cursor()`.

## Resume Planning

`src/ai_dev_loop/resume_planner.py` owns the checkpoint decision logic.

It models next actions as:

- `CURSOR`;
- `STAGING`;
- `REVIEW`;
- `PROCESS_REVIEW`.

Supported resumable statuses:

- `prepared`;
- `waiting_for_cursor_fix`;
- `running_cursor`;
- `staging`;
- `reviewing`;
- `interrupted`.

Terminal statuses are refused:

- `completed`;
- `completed_with_residual_risk`;
- `max_iterations_reached`;
- `failed`;
- `aborted`.

Artifact probes:

- `cursor_turn_complete()` requires `cursor/iterations/NN/metadata.json` with `exit_code == 0` and `timed_out` false.
- `staging_complete_for_iteration()` checks current iteration staging metadata or `git/diffs/NN.patch`.
- `review_result_available()` requires a valid schema-validated `codex/reviews/NN.json`.

Important checkpoint behavior:

- `prepared` resume behaves like `start` after validating the prepared contract.
- `waiting_for_cursor_fix` sends the stored fix prompt only if the max review iteration limit has not already been reached.
- `running_cursor` retries the same Cursor turn when durable metadata does not prove completion.
- `staging` skips completed staging when artifacts prove completion; otherwise it stages only after Cursor completion is proven.
- `reviewing` processes an existing valid review result if one exists; otherwise it reruns the review.
- `interrupted` is restored to `running_cursor`, `staging`, or `reviewing` based on artifacts. It is not blindly restored to `validating`.
- Partial retry artifacts are preserved before rerunning Cursor or Codex.

## Terminal Outcomes

Phase 5 recognizes:

- `completed`: no actionable findings and tests passed or were not applicable.
- `completed_with_residual_risk`: no actionable findings, but tests failed, were blocked, or the structured review result indicates skipped findings present.
- `max_iterations_reached`: actionable findings still exist at the configured review iteration limit.
- `failed`: validation, preflight, runner, artifact, prompt, dirty-worktree, invalid-review, or other unsafe failures.
- `interrupted`: Cursor or Codex timeout, with artifacts available for explicit resume.

`waiting_for_cursor_fix` remains a durable checkpoint, but the shared loop normally transitions through it immediately to `running_cursor` when another correction turn is allowed. It can still exist for legacy Phase 4 runs or intentionally checkpointed tests.

## Review Fixes Made During Phase 5

Several staged-review findings were valid and fixed. These are important because future simplification may accidentally reintroduce them.

### P1: `interrupted` resume invalid transitions

Initial code blindly resumed `interrupted` through `validating`. That could violate state transitions and obscure the real safe checkpoint.

Fix:

- added artifact-derived checkpoint restoration;
- `interrupted` now restores to `running_cursor`, `staging`, or `reviewing`;
- transitions were extended accordingly;
- tests cover resume after Cursor completion, after staging completion, and with an existing review JSON.

### P1: New Cursor chat on checkpointed runs

Initial resume could create a new Cursor chat when checkpointed progress existed but no chat ID was present.

Fix:

- chat creation is allowed only for fresh runs with no workflow progress;
- checkpointed runs require an already persisted chat ID;
- missing chat IDs on checkpointed runs fail safely;
- tests cover missing chat ID rejection.

### P1: Review without staged-patch validation

Initial review resume could run or process Codex review without verifying the current index still matched the recorded staged patch.

Fix:

- `validate_recorded_staged_patch_for_review()` now runs before both `REVIEW` and `PROCESS_REVIEW`;
- staged index drift fails and persists `FAILED` with `last_error`;
- tests cover staged index drift before review processing.

### P2: `waiting_for_cursor_fix` ignored max iterations

Initial resume from `waiting_for_cursor_fix` could send another Cursor correction even when `current_review_iteration >= max_review_iterations`.

Fix:

- planner refuses another correction at the limit;
- `resume_run()` marks legacy waiting checkpoints at the limit as `max_iterations_reached`;
- no extra Cursor turn is sent;
- transition `waiting_for_cursor_fix -> max_iterations_reached` was added;
- tests cover the no-extra-Cursor-call behavior.

### P2: Correction staging allowed emptied index

Initial correction staging accepted an empty current staged patch because it only compared when a current patch existed.

Fix:

- correction pre-staging now requires the current staged patch to equal the recorded previous patch unconditionally;
- empty index is rejected;
- tests cover emptied-index rejection.

### P2: Chat creation failures stuck in `validating`

Initial `_require_cursor_chat()` failures were not persisted by `_continue_workflow()`.

Fix:

- added `_require_cursor_chat_or_fail()`;
- chat setup `ValidationError` now marks the run `FAILED`, records `last_error`, appends an event, and raises `AiDevLoopError`;
- tests cover invalid chat ID output from `agent create-chat`.

### P2: Correction pre-Cursor safety failures not persisted

Initial `validate_correction_pre_cursor()` ran before the handled block in `_run_cursor_turn()`.

Fix:

- correction preflight is now wrapped and persisted as `FAILED` with event `correction_preflight_failed`;
- tests cover staged-index drift before correction resume.

### P2: Missing or empty fix prompts left `resume` stuck in `running_cursor`

Initial `read_cursor_prompt()` errors happened after `resume_run()` transitioned from `waiting_for_cursor_fix` to `running_cursor`, but before a handled failure block.

Fix:

- prompt path and prompt read are now wrapped in `_run_cursor_turn()`;
- missing or empty prompts mark the run `FAILED` with `last_error` and event `cursor_prompt_failed`;
- tests cover missing and empty fix prompt files.

### P2: Review resume accepted unrelated worktree drift

Initial review preflight only validated the staged patch. Extra unstaged tracked changes or untracked files could coexist with the recorded staged patch and slip into review resume.

Fix:

- `validate_recorded_staged_patch_for_review()` now also rejects unstaged tracked changes and untracked files;
- tests cover both dirty tracked worktree drift and untracked-file drift before review processing.

## Tests

The final suite reported `156` passing tests.

New or heavily updated coverage includes:

- bounded `start` loop with no findings;
- findings followed by successful correction;
- findings through the max iteration limit;
- reuse of the same Cursor chat across turns;
- reuse of the same Codex session across reviews;
- forwarding exact fix prompts from `prompts/fixes/NN.txt`;
- real `resume` from `waiting_for_cursor_fix`;
- real `resume` from `staging`;
- real `resume` from `reviewing`;
- `interrupted` checkpoint restoration;
- terminal-state resume refusal;
- max-iteration waiting checkpoint handling;
- missing checkpoint chat ID rejection;
- staged-index drift rejection before review processing;
- unstaged and untracked drift rejection before review processing;
- correction preflight failure persistence;
- missing and empty fix prompt failure persistence;
- correction staging safety;
- iteration helper upsert/lookup behavior;
- status/log rendering for multiple iterations.

Fakes added or extended in `tests/conftest.py` include:

- `FAKE_CODEX_REVIEW_SEQUENCE`;
- correction modify modes;
- review counters;
- multi-review artifact behavior.

## Privacy And Output

Phase 5 preserves the privacy discipline from earlier phases:

- default CLI output does not print full prompts;
- default CLI output does not print fix prompts;
- default CLI output does not print staged patches;
- default CLI output does not print raw Codex JSONL;
- Codex log rendering summarizes review JSON and redacts `review_markdown` and `cursor_fix_prompt`;
- metadata args redact sensitive prompt/session values where appropriate;
- sensitive artifacts remain under XDG state paths with restrictive permissions where supported.

## Important Invariants For Future Agents

- Do not create a second Cursor chat for an existing run.
- Do not swap or guess the Codex session ID.
- Do not use `--last` for Codex review.
- Do not interpret review Markdown as the loop decision source.
- Do not edit or improve Codex fix prompts before sending them to Cursor.
- Do not rerun completed agent turns when artifacts prove completion.
- Do not silently overwrite partial Cursor or Codex artifacts.
- Do not relax staged-patch equality checks around correction or review resume.
- Do not allow unrelated unstaged or untracked work during correction or review resume.
- Do not convert cumulative staged patches into incremental-only patches without revisiting safety logic and tests.
- Do not add commits, pushes, resets, cleans, stashes, or unstaging as hidden side effects.
- Keep this repository's review skill separate from target-repository `state.codex.review_skill`.

## Known Pending Work

The next phases still need to decide or implement:

- real `abort <run-id>` with child-process/process-group termination;
- global Codex skill installation;
- SessionStart hook installation and trust handling;
- `integrations install`;
- `integrations uninstall`;
- any broader migration story for corrupted or manually edited run state.

