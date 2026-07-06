# Phase 5 Plan: Bounded Review/Fix Loop + Real Resume

## Goal or Goals

Build the first complete automated local loop for `ai_dev_loop`.

After Phase 5, `ai_dev_loop start <run-id>` must advance a fresh prepared run through the full bounded sequence:

1. prepare-validated start preflight;
2. existing Cursor chat creation or reuse;
3. Cursor initial implementation;
4. Git staging and staged diff capture;
5. Codex review by resuming the exact original Codex session;
6. when Codex returns actionable findings and the review limit has not been reached, send Codex's exact `cursor_fix_prompt` to the same Cursor chat;
7. stage again;
8. review again with the same original Codex session;
9. stop only when Codex reports no actionable findings, residual-risk completion applies, or `workflow.max_review_iterations` is reached.

Phase 5 must also make `ai_dev_loop resume <run-id>` real. It must continue runs from durable checkpoints without changing Cursor chat identity or Codex session identity. In particular, it must continue Phase 4-style runs that are already in `waiting_for_cursor_fix` by sending the stored fix prompt to the same Cursor chat and continuing the bounded loop.

## Non-Goals

- Do not implement `abort <run-id>` child-process termination.
- Do not implement global Codex skill installation, SessionStart hook installation, hook trust handling, or integrations install/uninstall.
- Do not implement destructive cleanup, rollback, reset, clean, stash, commit, tag, push, or unstaging behavior.
- Do not implement migration support for arbitrary corrupted or hand-edited run state beyond explicit safe validation and clear failure messages.
- Do not add real Cursor or Codex model calls to automated tests.
- Do not expand the product beyond local WSL/Linux orchestration.

## Scope

Implement Phase 5 as defined by:

- `plan-2-build-ai-dev-loop-orchestrator.md`;
- `phase-0-findings.md`;
- `phase-1-findings.md`;
- `phase-2-findings.md`;
- `phase-3-findings.md`;
- `phase-4-findings.md`;
- this plan.

This phase owns:

1. Refactoring the current single-iteration `start` flow into reusable workflow steps.
2. Running Cursor correction turns from persisted `prompts/fixes/NN.txt`.
3. Reusing the existing `state.cursor.chat_id` for every Cursor turn.
4. Reusing the exact `state.codex.session_id` for every Codex review.
5. Supporting multiple iteration numbers and artifact sets:
   - `cursor/iterations/01`, `02`, `03`, ...
   - `git/status/02-before-cursor.txt`, `git/diffs/02.patch`, ...
   - `codex/events/02.jsonl`, `codex/reviews/02.json`, ...
   - `prompts/fixes/01.txt`, `02.txt`, ...
6. Updating iteration metadata without overwriting completed earlier iterations.
7. Updating Git staging safety so correction iterations may begin with staged changes from the previous orchestrator iteration, while still rejecting user or agent self-staging outside the recorded run state.
8. Implementing `resume <run-id>` for clear durable checkpoints.
9. Adding a durable Cursor governance rule for bounded loop and resume contracts so later phases preserve the same semantics.
10. Updating `status`, `logs`, `inspect`, README, schemas if needed, and tests to describe Phase 5 behavior accurately.

## Out of Scope

- Do not send any prompt that was not prepared by the user/Codex or returned by the resumed Codex review result.
- Do not synthesize, summarize, rewrite, translate, or "improve" `cursor_fix_prompt`.
- Do not create a second Cursor chat for the same run.
- Do not use a new Codex session, `--last`, guessed session IDs, or subagents as reviewers.
- Do not parse Markdown to decide loop outcome.
- Do not store copied Codex transcript contents.
- Do not validate target-repository review skills by inspecting this repository's `.agents/skills` directory.
- Do not make `resume` silently retry ambiguous partial agent turns where durable artifacts cannot prove the next safe action.
- Do not remove or modify unrelated files or metadata artifacts, including `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` if present.
- Do not print full prompts, fix prompts, staged patches, review Markdown, raw JSONL, full session IDs, auth payloads, full process environments, or secret-like values in default CLI output.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `phase-3-findings.md`
- `phase-4-findings.md`
- `plans/phase-1-foundation-and-prepare.md`
- `plans/prompt_phase-1-foundation-and-prepare.txt`
- `plans/phase-2-start-preflight-and-cursor-runner.md`
- `plans/prompt_phase-2-start-preflight-and-cursor-runner.txt`
- `plans/phase-3-git-staging-runner.md`
- `plans/prompt_phase-3-git-staging-runner.txt`
- `plans/phase-4-codex-review-runner.md`
- `plans/prompt_phase-4-codex-review-runner.txt`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Important current implementation facts:

- `src/ai_dev_loop/commands/start.py` currently implements one linear initial implementation plus one review.
- `src/ai_dev_loop/runners/staging.py` currently records only `state.iterations[0]` and rejects all pre-existing staged paths.
- `src/ai_dev_loop/runners/codex.py` currently updates only `state.iterations[0]`.
- `src/ai_dev_loop/commands/start_preflight.py` currently only allows `start` from `prepared`; `resume` is still a placeholder.
- `src/ai_dev_loop/commands/status.py`, `inspect.py`, and `logs.py` currently have Phase 4 assumptions around one primary iteration.
- `state.py` already has statuses for `waiting_for_cursor_fix`, `max_iterations_reached`, and `interrupted`, but the transition helpers need Phase 5 behavior.
- Tests use fake `agent` and `codex` executables in `tests/conftest.py`; extend those fakes rather than invoking real model activity.

Phase 0 local CLI facts to preserve:

- Cursor CLI command is `agent`.
- Codex CLI command is `codex`.
- Cursor prompts are passed as a single positional argument to `agent -p`.
- `agent create-chat` returns the chat ID.
- `agent models` and `agent status --format json` are safe probes.
- Codex review command shape must keep root `codex exec` options such as `--cd` and `--sandbox` before `resume`.
- Resume-specific Codex options such as `--model`, `--json`, `--output-schema`, and `--output-last-message` follow `resume`.
- Do not use `--last`.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails. This rule is required.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: state, locks, runners, Git safety, Cursor identity, Codex identity, process execution, and phase-boundary contracts. This rule is required.
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`: persisted state, schema, manifest, iteration, artifact, and recovery checkpoint contracts. This rule is required.
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`: Codex review execution, structured review result, review prompt, event logging, privacy, and review artifact contracts. This rule is required.
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`: bounded loop semantics, correction prompt ownership, multi-iteration artifacts, correction staging safety, resume idempotency, and multi-iteration privacy/testing contracts. This rule is required and must be kept aligned with this phase.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format for reviewing staged changes in this `ai_dev_loop` repository. It is not the target-repository review skill invoked by automated runs.

No `AGENTS.md` file is present.

Review-skill distinction to preserve:

- Automated target-repository reviews must use the exact `state.codex.review_skill` from the prepared run.
- Invoke that skill as `$<review-skill>` in the resumed Codex prompt.
- Do not inspect this repository's `.agents/skills` to validate or choose a target review skill.

## Architecture Guardrails

- `ai_dev_loop` is a deterministic local orchestrator. It transports exact prompts and schema-validated decisions; it does not replace Codex or Cursor reasoning.
- Store run state, locks, logs, prompts, staged patches, and agent artifacts under XDG-managed `ai_dev_loop` paths outside target repositories.
- Use user-only permissions where supported: directories `0700`; prompt/session/agent-output/review/patch files `0600`.
- All subprocess calls must use argument arrays with `shell=False`.
- Apply configured timeouts and terminate full process groups.
- Treat all agent output as untrusted data.
- Do not execute text returned by agents.
- Never log full process environments, tokens, API keys, auth payloads, full prompts, full patches, or raw review content in default user-facing output.
- Create at most one Cursor chat per run. Persist and reuse `state.cursor.chat_id`.
- Every Codex review must resume the exact `state.codex.session_id`.
- The same resumed Codex session must author every `cursor_fix_prompt`.
- The orchestrator may validate, persist, and forward a fix prompt. It must not author, rewrite, summarize, or reinterpret it.
- The structured Codex JSON result is the source of truth for loop decisions. Do not scrape Markdown.
- Staged diffs for each iteration should represent the full current staged change set at that point, not necessarily an incremental delta.
- Do not commit, tag, push, reset, clean, stash, or unstage.
- Fail safely on ambiguous checkpoints, unexpected worktree drift, unexpected staged changes, missing fix prompts, missing chat IDs, changed branch/HEAD, changed plan/prompt contracts, or invalid structured Codex output.

## Implementation Plan

1. Add or update the Phase 5 loop/resume governance rule.
   - Ensure `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc` exists.
   - Keep it focused on bounded loop semantics, Cursor/Codex identity, fix prompt ownership, multi-iteration artifacts, correction staging safety, resume idempotency, privacy, and tests.
   - Keep this plan, README, and implementation aligned with that rule.

2. Refactor the workflow into reusable steps.
   - Keep CLI presentation in `cli.py`; move orchestration helpers into focused modules or private functions.
   - Avoid a large copy-paste second path for `resume`.
   - Suggested shape:
     - `start_run(run_id)` validates `prepared`, acquires locks, and calls a shared continuation engine.
     - `resume_run(run_id)` acquires locks and calls the same continuation engine with resume mode.
     - Shared helpers execute one Cursor turn, one staging pass, and one Codex review pass.
   - Preserve existing `StartResult` rendering or replace it with a shared result object that works for both `start` and `resume`.

3. Define iteration numbering and prompt ownership.
   - Iteration `01` is the initial Cursor implementation using `state.prompt.snapshot_path`.
   - Iteration `02` is the first Cursor correction using `prompts/fixes/01.txt`.
   - Iteration `NN` for `NN > 1` uses the fix prompt produced by review `NN-1`: `prompts/fixes/{NN-1:02d}.txt`.
   - Review `NN` writes `codex/reviews/{NN:02d}.json`, `codex/reviews/{NN:02d}.md`, `codex/events/{NN:02d}.jsonl`, and, when findings exist, `prompts/fixes/{NN:02d}.txt`.
   - Set iteration `kind` to `initial_implementation` for `01` and `cursor_correction` for correction iterations.

4. Update state transitions and helpers.
   - Add a helper for `reviewing -> max_iterations_reached`.
   - Keep `waiting_for_cursor_fix -> running_cursor` for correction turns.
   - Keep `interrupted -> validating` for explicit resume, then continue only when artifacts make the next action clear.
   - Add or update status transition tests.
   - Keep terminal states terminal.

5. Make iteration metadata multi-iteration safe.
   - Replace `state.iterations[0]` updates with lookup/update by iteration number.
   - Do not overwrite completed prior iterations.
   - Each iteration entry should keep stable keys:
     - `number`;
     - `kind`;
     - `started_at`;
     - `completed_at`;
     - `cursor`;
     - `git`;
     - `codex`;
     - `review`.
   - Store relative artifact paths only.
   - Update `src/ai_dev_loop/schemas/run-state-v1.json` if a stricter iteration schema is introduced. If leaving `iterations` structurally open, still add tests for expected persisted shapes.

6. Generalize Cursor execution for initial and correction turns.
   - Reuse `execute_prompt()` and the stored chat ID.
   - For iteration `01`, prompt path is `state.prompt.snapshot_path`.
   - For correction iterations, read the exact stored `prompts/fixes/{previous}.txt`.
   - Reject missing or empty correction prompt files.
   - Write artifacts under `cursor/iterations/{NN}/`.
   - Metadata must redact the prompt argument.
   - Preserve raw Cursor JSONL and stderr as sensitive artifacts.
   - Do not create a new chat when `state.cursor.chat_id` already exists.

7. Make Git staging safe for correction iterations.
   - Initial iteration should preserve the existing Phase 3 safety behavior: reject pre-existing staged paths before the first orchestrator staging pass.
   - Correction iterations may begin with staged changes from the previous orchestrator iteration.
   - Before running a correction Cursor turn, verify the current staged patch still matches the previous iteration's recorded staged patch artifact. If it differs, fail safely because the user or another process changed the index.
   - Before running a correction Cursor turn, verify there are no unstaged tracked changes and no untracked files outside the recorded staged state. Use direct Git commands or well-tested helpers; do not rely on ad hoc string checks.
   - During correction staging, allow the previous orchestrator-staged paths, then run `git add -A` to update the index with Cursor's correction.
   - After staging, capture the full cumulative staged diff artifacts for the current iteration:
     - `git/status/{NN}-before-staging.txt`;
     - `git/status/{NN}-after-staging.txt`;
     - `git/diffs/{NN}.stat`;
     - `git/diffs/{NN}.name-only.txt`;
     - `git/diffs/{NN}.patch`.
   - Continue rejecting staged prompt-source paths and changed repository plan hashes.
   - Continue failing on empty staged diffs.
   - Do not unstage anything.

8. Generalize Codex review for multiple iterations.
   - Reuse the existing `run_codex_review()` command shape and prompt discipline.
   - Update iteration review metadata by iteration number, not index `0`.
   - For each review, include the latest Cursor final response from that same iteration.
   - Continue including the original Cursor prompt content directly in the review prompt.
   - If useful, include current iteration artifacts and prior review report paths for auditability, but do not require Codex to read XDG files.
   - Preserve the exact session ID and never use `--last`.
   - Preserve privacy behavior in errors and logs.

9. Implement the bounded loop in `start`.
   - `start` must still require `prepared` at entry.
   - On review result with no actionable findings:
     - `passed` or `not_applicable` -> `completed`;
     - `failed`, `blocked_environment`, or `skipped_findings_present` without findings -> `completed_with_residual_risk`.
   - On review result with actionable findings:
     - if `current_review_iteration >= max_review_iterations`, set `max_iterations_reached`, preserve the latest review and latest fix prompt, leave changes staged, and do not call Cursor again;
     - otherwise set `waiting_for_cursor_fix` as a durable checkpoint, save state, then transition to `running_cursor` and execute the next correction turn.
   - Append structured events for loop start, each Cursor turn, each staging pass, each review, fix prompt availability, max-iteration exhaustion, completion, interruption, and failures.
   - Return an honest final result message with run ID, final status, iteration count, latest review path, staged diff path, and manual follow-up summary when max iterations are reached.

10. Implement real `resume <run-id>`.
   - Wire `src/ai_dev_loop/cli.py` to call a real resume command instead of the placeholder.
   - `resume` must acquire the same run and repository locks before mutating state or the target repository.
   - `resume` must use the stored Cursor chat ID and Codex session ID.
   - `resume` must not re-run completed agent turns when durable artifacts prove they completed.
   - Support at least these clear checkpoints:
     - `prepared`: behave like `start` after validating the prepared contract;
     - `waiting_for_cursor_fix`: continue with the stored fix prompt for the next correction iteration;
     - `staging`: if staging artifacts for the current iteration are complete, continue to review; otherwise run staging only if the Cursor turn completed successfully and current repository state is safe;
     - `reviewing`: if a valid review result already exists, process the outcome; otherwise rerun the review for that iteration using the exact original Codex session;
     - `interrupted`: inspect artifacts and continue only when the next safe action is clear. For a timed-out Cursor or Codex turn with no completed result, explicit `resume` may retry that same turn using the same Cursor chat or Codex session, but it must preserve prior partial artifacts instead of silently overwriting them.
   - Refuse terminal states (`completed`, `completed_with_residual_risk`, `max_iterations_reached`, `failed`, `aborted`) with clear messages unless a safe no-op status display is more appropriate.
   - If a checkpoint is ambiguous, fail safely with a diagnostic and do not mutate the repository.

11. Preserve partial-attempt artifacts on retry.
    - If `resume` retries a Cursor or Codex turn after timeout/interruption and existing artifact files are present, do not overwrite them without preserving evidence.
    - Use a deterministic attempt subdirectory or suffix such as:
      - `cursor/iterations/02/attempts/20260706T.../events.jsonl`; or
      - `cursor/iterations/02/events.attempt-YYYYMMDDTHHMMSSZ.jsonl`.
    - Keep the canonical successful artifact path as the path recorded in `state.iterations`.
    - Document whichever approach is implemented and test it.

12. Update read-only commands.
    - `status` should report:
      - current review iteration and max;
      - final statuses;
      - `waiting_for_cursor_fix` as resumable with `ai_dev_loop resume <run-id>`;
      - `max_iterations_reached` with manual follow-up instructions;
      - `interrupted` as resumable when a safe checkpoint exists.
    - `inspect` should render all iteration summaries, not only the first.
    - `logs --component codex` should summarize all Codex review iterations, not only the first.
    - `logs --component cursor` should remain privacy-aware; if changed, avoid dumping full prompts by default.

13. Update documentation.
    - README must describe Phase 5 behavior as the current behavior:
      - full bounded review/fix loop;
      - same Cursor chat;
      - same Codex session;
      - max iteration behavior;
      - final staged changes;
      - real `resume`;
      - still-unimplemented `abort` and integrations install/uninstall.
    - Update any Phase 4 boundary wording that says correction execution is not implemented.
    - Keep installation and validation commands aligned with `uv`.

## Testing Criteria

Add or update automated tests. Use fake `agent` and `codex` executables only.

Unit tests:

- Status transitions:
  - `reviewing -> max_iterations_reached`;
  - `waiting_for_cursor_fix -> running_cursor`;
  - `interrupted -> validating`;
  - terminal states remain terminal.
- Iteration helpers:
  - append/update by iteration number;
  - do not overwrite iteration `01` when writing iteration `02`;
  - correction iteration prompt path points to the previous review's fix prompt.
- Cursor runner integration helpers:
  - same chat ID is used for initial and correction turns;
  - metadata redacts correction prompts.
- Git staging helpers:
  - first iteration rejects pre-existing staged changes;
  - correction iteration allows exactly the previously recorded orchestrator-staged patch;
  - correction iteration rejects changed staged patch before Cursor correction;
  - correction iteration rejects unstaged/untracked work before Cursor correction;
  - prompt source and plan hash protections still apply.
- Codex runner:
  - review metadata updates the requested iteration number;
  - review prompt includes the current iteration's latest Cursor final response;
  - command shape still resumes the exact session and never uses `--last`.
- Resume planner:
  - terminal statuses are refused or no-op as designed;
  - ambiguous checkpoints fail safely.

Integration tests:

- Fresh `start` with no findings completes after one review.
- Fresh `start` with review sequence `findings -> no_findings`:
  - creates one Cursor chat;
  - runs Cursor twice with `--resume <same-chat-id>`;
  - runs Codex twice with the exact same session ID;
  - writes `prompts/fixes/01.txt`;
  - writes iteration `01` and `02` artifacts;
  - ends `completed`;
  - leaves final changes staged.
- Fresh `start` with findings through `max_review_iterations`:
  - stops at `max_iterations_reached`;
  - does not send another Cursor prompt after the final review;
  - preserves the latest fix prompt and latest review artifacts.
- `resume` from a Phase 4-style `waiting_for_cursor_fix` state:
  - sends the stored fix prompt to the same Cursor chat;
  - stages iteration `02`;
  - reviews iteration `02`;
  - completes when Codex returns no findings.
- `resume` from `staging` with completed staging artifacts continues to review without rerunning Cursor.
- `resume` from `reviewing` with a valid review JSON processes the outcome without rerunning Codex.
- `resume` after Codex timeout reruns review only when no valid review result exists.
- Existing Cursor chat is reused after a recoverable interruption.
- Same Codex session ID is used for every review.
- Same Cursor chat ID is used for every implementation and fix.
- `logs/events.jsonl` does not contain full prompts or fix prompts.
- `logs --component codex` redacts all review Markdown and fix prompts across multiple iterations.
- `status` and `inspect` report multiple iterations and Phase 5 final statuses.
- Existing Phase 4 tests are updated so they assert Phase 5 loop behavior rather than the old `waiting_for_cursor_fix` boundary where appropriate.

Fake CLI guidance:

- Extend the fake `codex` in `tests/conftest.py` to support review sequences such as `FAKE_CODEX_REVIEW_SEQUENCE=findings,no_findings` and `findings,findings,findings`.
- Extend the fake `agent` if needed to make correction turns produce a distinct tracked change, and log enough data to assert that all turns use the same `--resume` chat ID.
- Do not call real `agent` or `codex` in tests.

## Validation

Run these commands before handing off for review:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
uv run ai_dev_loop doctor
uv run ai_dev_loop integrations status
uv run ai_dev_loop config validate --repo tests/fixtures/sample_repo
```

If the full suite is slow, run targeted tests during development, but the final validation must include the full suite above.

## Risks Or Recovery Notes

- The highest-risk change is allowing correction iterations to start with staged changes. Only allow staged changes that match the previous orchestrator-recorded staged patch; reject anything else.
- `resume` must be conservative. It is better to fail with a precise diagnostic than to repeat a completed agent turn or stage unrelated user work.
- Existing Phase 4 tests encode a one-review boundary. Update them intentionally; do not preserve obsolete assertions that say correction execution is unimplemented.
- Multi-iteration state is now part of the recovery contract. Keep state, artifacts, docs, and tests aligned.
- If retrying after interruption requires preserving partial artifacts, implement the preservation before enabling retry.
- Any user-facing error that includes raw Cursor or Codex output risks leaking proprietary content. Point to sensitive artifacts instead.

## OpenQuestions

None.
