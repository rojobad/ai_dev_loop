# Phase 4 Plan: Codex Review Runner + Structured Event Log

## Goal or Goals

Build the next executable slice of `ai_dev_loop`: after the existing Cursor implementation and Git staging checkpoints complete, resume the exact original Codex session, run the configured target-repository review skill against the staged changes, validate Codex's schema-constrained review result, persist review artifacts, and stop honestly before Cursor correction turns.

This phase should make `ai_dev_loop start <run-id>` advance a newly prepared run through:

1. existing start preflight;
2. existing Cursor chat creation and initial Cursor execution;
3. existing Git staging and staged diff artifact capture;
4. structured orchestrator event logging at `logs/events.jsonl`;
5. Codex review via `codex exec ... resume <exact-session-id>`;
6. schema validation and cross-field validation of the Codex review result;
7. a terminal success state when there are no actionable findings, or a clear `waiting_for_cursor_fix` boundary when Codex returns findings.

## Non-Goals

- Do not implement Cursor correction turns from Codex findings.
- Do not implement the complete bounded stage-review-fix loop.
- Do not implement max-iteration correction behavior beyond recording the first review pass correctly.
- Do not implement full `resume` recovery semantics.
- Do not implement `abort` child-process termination beyond existing process timeout behavior.
- Do not implement global Codex skill installation, SessionStart hook installation, hook trust handling, or integrations install/uninstall.
- Do not modify target repositories outside the already implemented Cursor execution and Git staging behavior.
- Do not rename this repository's local review skill or treat it as the review skill used by target repositories.

## Scope

Implement Phase 4 as defined by `phase-3-findings.md` and the updated master plan:

1. Add structured orchestrator event logging at `logs/events.jsonl`.
2. Implement the Codex review runner in `src/ai_dev_loop/runners/codex.py`.
3. Build a deterministic review wrapper prompt that invokes the configured target-repository review skill explicitly.
4. Run Codex with the probed `codex exec` option order from Phase 0.
5. Store raw Codex JSONL events, stderr where useful, final structured JSON, extracted Markdown review report, and any exact Cursor fix prompt returned by Codex.
6. Update the first iteration entry with real `codex` and `review` sections.
7. Decide the run outcome from schema-validated structured JSON fields, not by parsing Markdown.
8. Update `status`, `logs`, `inspect`, README, schemas, and tests so they describe Phase 4 behavior accurately.

After a successful Codex review:

- If `has_actionable_findings` is false, set status to `completed` when tests passed or were not applicable, or `completed_with_residual_risk` when the structured `tests_status` indicates failed tests, environmental blockage, or residual risk.
- If `has_actionable_findings` is true, store the exact `cursor_fix_prompt`, set status to `waiting_for_cursor_fix`, and stop. The next Cursor correction turn belongs to a later phase.

## Out of Scope

- Do not send any fix prompt to Cursor in this phase.
- Do not create a second Cursor chat.
- Do not invoke `agent -p` after Codex review.
- Do not implement `resume <run-id>` as the mechanism for continuing a Phase 3 `staging` run.
- Do not support old Phase 3 runs already stopped in `staging`; fresh Phase 4 `start` runs should continue from staging into review in one execution path.
- Do not scrape Markdown to determine findings, severity, tests status, or fix prompts.
- Do not fall back to plain `codex exec` without `resume`.
- Do not use `--last`, guessed session IDs, or subagents as reviewers.
- Do not run real Cursor or Codex model activity in automated tests.
- Do not create commits, tags, pushes, resets, cleans, stashes, or unstaging behavior.
- Do not modify global files under `$HOME/.agents` or `$HOME/.codex`.
- Do not delete or alter unrelated user files or metadata artifacts, including `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` if present.
- Do not print full prompts, full staged patches, auth payloads, full process environments, or unredacted secret-like values in default human output.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `phase-3-findings.md`
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
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Treat the master plan as the architecture and safety source of truth. Treat this Phase 4 plan as the scope boundary. If this file and the master plan conflict on safety behavior, stop and report the conflict before making dependent changes.

Phase 0 local CLI facts to preserve:

- Cursor CLI command is `agent`.
- Codex CLI command is `codex`.
- `codex login status` is a safe auth probe.
- `codex exec resume` accepts an explicit session ID and prompt `-` from stdin.
- On the probed Codex CLI, root `codex exec` options such as `--cd` and `--sandbox` must appear before `resume`.
- Resume-specific options such as `--model`, `--json`, `--output-schema`, and `--output-last-message` follow `resume`.
- Do not use `--last`.

Phase 1 through Phase 3 implementation facts to preserve:

- `prepare` writes run artifacts under XDG state and validates repository cleanliness.
- `start` already performs preflight, locks, tool probes, Cursor chat creation/reuse, Cursor headless execution, Git staging, and staged diff artifact capture.
- After Phase 3, `start` leaves status as `staging` and writes a Phase 3 boundary result.
- `src/ai_dev_loop/runners/codex.py` is still a placeholder.
- `state.py` already defines `reviewing`, `waiting_for_cursor_fix`, `completed`, `completed_with_residual_risk`, and `max_iterations_reached` statuses.
- The repository uses `uv` for development and validation; do not modify system Python.
- Tests use fake `agent` and `codex` executables; no automated test should invoke real model activity.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails. This rule is required.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: product architecture contracts for state, locks, runners, Git safety, Cursor identity, Codex identity, process execution, and phase boundaries. This rule is required.
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`: persisted state, schema, manifest, iteration, artifact, and recovery checkpoint contracts. This rule is required.
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`: Codex review execution, structured review result, review prompt, event logging, privacy, and Phase 4 boundary contracts. This rule is required.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format for reviewing staged changes in this `ai_dev_loop` repository. It is not the target-repository review skill that `ai_dev_loop` invokes during automated review runs.

No `AGENTS.md` file is present.

Important review-skill distinction:

- This repository may contain `.agents/skills/review-staged-changes` for reviewing implementation work on `ai_dev_loop` itself.
- Target repositories that use `ai_dev_loop` are expected to configure their own compatible review skill, commonly `review-staged-cursor-execution`, in their `ai_dev_loop.yaml`.
- The Codex review runner must use the exact `state.codex.review_skill` from the prepared run and include `$<review-skill>` in the review wrapper prompt.
- Do not validate target-repository review skill names by looking at this repository's `.agents/skills` directory.

## Architecture Guardrails

- Keep `ai_dev_loop` naming consistent across executable, package, XDG paths, schemas, docs, and environment variables.
- Preserve separation between CLI presentation, config, paths, state persistence, process execution, Git safety, Cursor integration, Codex integration, and integration installation.
- Store durable run state, locks, logs, and agent artifacts under XDG-managed `ai_dev_loop` paths outside target repositories.
- Use user-only permissions where supported: directories `0700`; prompt/session/agent-output/review files `0600`.
- Use typed models or typed dataclasses for Codex review runner inputs, outputs, and validation.
- Keep persisted state, `iterations`, schemas, manifests, status transitions, and artifact paths aligned with `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`.
- All subprocess calls must use argument arrays and `shell=False`.
- Supply the Codex review instruction on stdin. Do not pass it through shell interpolation.
- Use explicit working directories.
- Treat Codex stdout, stderr, and structured output files as untrusted data.
- Preserve raw Codex JSONL events even when parsing or validation fails.
- Do not execute, evaluate, or shell-interpolate text emitted by Codex.
- Do not log full process environments, authentication payloads, API keys, tokens, or full account details.
- Every review must resume the exact original Codex session ID from `state.codex.session_id`.
- Never use `codex exec` without `resume` for final review.
- Never use `--last`, guessed session IDs, or subagents as the final reviewer.
- The resumed Codex session must author any `cursor_fix_prompt`; the orchestrator only validates, stores, and later phases will forward it.
- The orchestrator must decide outcome from schema-validated structured JSON fields, not Markdown parsing.
- Include the original Cursor prompt content and latest Cursor final response content directly in the review instruction payload, but do not print those contents in default CLI/log output.
- Include artifact paths in the review instruction for auditability, but do not require the resumed Codex process to read files from the XDG state directory.
- Continue to stage with the existing Phase 3 runner before review. Do not add commits, pushes, resets, cleans, stashes, or unstaging behavior.
- Keep CLI output honest: after findings, report that Codex review is complete and a Cursor correction prompt is stored, but correction execution is not implemented yet.

## Implementation Plan

1. Add structured orchestrator event logging.
   - Add a small event-log helper, preferably near state/persistence code or in a focused module.
   - Write append-only JSONL to `logs/events.jsonl`.
   - Each event must include at least:
     - `schema_version`;
     - UTC timestamp;
     - level;
     - component;
     - event name;
     - run ID;
     - status when known;
     - iteration when known;
     - artifact path when relevant;
     - redacted detail fields.
   - Do not include full prompts, full patches, authentication payloads, full process environments, or unredacted secret-like values in event details.
   - Keep the existing human-readable `logs/ai_dev_loop.log`.
   - Ensure `prepare` creates or appends the first structured event so new runs contain `logs/events.jsonl`.
   - Add events for major `start`, Cursor, staging, Codex review, outcome, and failure checkpoints.

2. Extend process execution for Codex stdin when needed.
   - Reuse existing process-group handling and timeout behavior.
   - Add support for passing review prompt text to stdin without shell interpolation.
   - Capture stdout and stderr separately.
   - Support writing raw stdout JSONL to `codex/events/01.jsonl`.
   - Apply `state.workflow.codex_timeout_minutes`.
   - Terminate the full child process group on timeout.
   - Redact sensitive data in user-facing messages and structured event details.

3. Implement a typed Codex review result model.
   - Add a Pydantic model or equivalent typed validator for `codex-review-result-v1`.
   - Keep `src/ai_dev_loop/schemas/codex-review-result-v1.json` aligned with the typed model.
   - Validate schema shape and application cross-field rules:
     - findings require `findings_count > 0`;
     - no findings require `findings_count == 0`;
     - findings require non-null `highest_severity`;
     - findings require non-empty `cursor_fix_prompt`;
     - no findings require `cursor_fix_prompt is null`;
     - `review_markdown` must be non-empty.
   - Optionally validate broad Markdown compatibility without using Markdown as the decision source.

4. Build the deterministic review wrapper prompt.
   - State that this is an automated review turn for the existing approved plan.
   - Invoke the configured target-repository review skill exactly as `$<state.codex.review_skill>`.
   - Tell Codex to review the current staged changes only.
   - Reference both the repository plan path and the plan snapshot path.
   - Include the original Cursor prompt content directly.
   - Include the latest Cursor final response content directly when extracted.
   - Explicitly state when the latest Cursor final response could not be extracted.
   - Include artifact paths for auditability:
     - plan snapshot;
     - prompt snapshot;
     - latest Cursor event/final/metadata paths;
     - staged diff/stat/name-only paths.
   - Tell Codex to follow the review skill's read-only rule.
   - Tell Codex to run tests only according to the review skill.
   - Require the final response to conform to `codex-review-result-v1.json`.
   - Require `cursor_fix_prompt` to be a complete English prompt when findings exist.
   - Require `cursor_fix_prompt` to be `null` when there are no findings.
   - Do not summarize, rewrite, or reinterpret findings in the orchestrator.

5. Implement `src/ai_dev_loop/runners/codex.py`.
   - Replace the placeholder with a real runner.
   - Construct the Phase 0 validated command shape:

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

   - Put root `codex exec` options before `resume`.
   - Put resume-specific options after `resume`.
   - Use the exact session ID from `state.codex.session_id`.
   - Use `cwd` as the target repository root.
   - Do not use `--last`.
   - Store command metadata with prompt content redacted.

6. Persist Codex review artifacts.
   - Create `codex/events/` and `codex/reviews/` under the run directory.
   - Persist:
     - `codex/events/01.jsonl`;
     - `codex/events/01.stderr.txt` when stderr exists or as a deterministic artifact if simpler;
     - `codex/reviews/01.json`;
     - `codex/reviews/01.md`;
     - `codex/reviews/01.metadata.json`;
     - `prompts/fixes/01.txt` when findings exist.
   - Treat all Codex event/review/fix-prompt artifacts as sensitive.
   - Preserve raw Codex JSONL and final-message output even when structured validation fails.
   - Do not persist copied Codex transcript contents.

7. Integrate review into `start`.
   - After successful Phase 3 staging, transition `staging -> reviewing`.
   - Run Codex review before returning success.
   - Increment or set `state.workflow.current_review_iteration` to `1` for the first review pass.
   - On Codex timeout, mark `interrupted` with diagnostics.
   - On Codex nonzero exit, invalid structured JSON, missing result file, or cross-field validation failure, mark `failed` with diagnostics.
   - On findings:
     - store the fix prompt exactly;
     - update the iteration with review summary fields;
     - set status to `waiting_for_cursor_fix`;
     - set a result message that correction execution is pending a later phase.
   - On no findings:
     - update the iteration with review summary fields;
     - set status to `completed` or `completed_with_residual_risk` according to `tests_status`;
     - leave final changes staged.
   - Do not run any later Cursor correction turn.

8. Update iteration metadata.
   - Extend the existing first iteration entry with real `codex` and `review` sections after Codex review completes.
   - Use relative run-directory paths.
   - Suggested `codex` keys:
     - `events_path`;
     - `stderr_path`;
     - `result_path`;
     - `report_path`;
     - `metadata_path`;
     - `fix_prompt_path` when findings exist;
     - `exit_code`.
   - Suggested `review` keys:
     - `has_actionable_findings`;
     - `findings_count`;
     - `highest_severity`;
     - `tests_status`;
     - `summary`.
   - Do not add fake correction-turn data.
   - Update JSON schemas and tests if persisted state shape changes.

9. Update read-only commands and docs.
   - Update `status` next safe action for:
     - `reviewing`;
     - `waiting_for_cursor_fix`;
     - `completed`;
     - `completed_with_residual_risk`;
     - `failed` after Codex review failures.
   - Update `logs --component codex` to render Codex artifacts usefully without printing full prompts by default.
   - Update `inspect` to list Codex review artifact paths and optionally render the final review report.
   - Update README to describe Phase 4 behavior honestly:
     - Codex review exists;
     - correction execution and full loop are still pending later phases;
     - successful no-finding runs end with staged changes and review artifacts;
     - finding runs stop with a stored correction prompt.

10. Extend fake Codex tests.
    - Update the fake `codex` executable in `tests/conftest.py` to simulate:
      - `codex login status`;
      - `codex exec --cd <repo> --sandbox <sandbox> resume ... <session-id> -`.
    - Record received args and stdin prompt for assertions.
    - Write fake output-last-message JSON according to `codex-review-result-v1.json`.
    - Emit fake JSONL events to stdout.
    - Simulate no findings, findings, invalid JSON, nonzero exit, and timeout.

## Testing Criteria

This phase changes process execution, Codex integration, structured validation, persisted state, event logging, CLI output, and docs. Automated tests are required.

Required test levels:

- Unit tests for Codex command construction and option order.
- Unit tests for review wrapper prompt construction.
- Unit tests for Codex review result cross-field validation.
- Unit tests for structured event log JSONL writing and redaction.
- Unit tests for process stdin support and timeout behavior if process helpers change.
- Integration tests using temporary Git repositories and fake `agent`/`codex` executables.
- Regression tests proving Phase 1 through Phase 3 behavior still works.

Expected test areas:

- Add `tests/unit/test_codex_runner.py`.
- Add or extend `tests/unit/test_state.py` for structured event logging and any state transition changes.
- Add or extend `tests/unit/test_process.py` for stdin-capable streaming execution.
- Add `tests/integration/test_start_codex_review.py`.
- Extend `tests/conftest.py` fake `codex` behavior.
- Update `tests/integration/test_start_staging.py` expectations because a successful `start` should no longer stop at the Phase 3 boundary; either adapt the tests to exercise internal staging helpers or make their boundary assertions Phase 4-aware.

Edge cases and failure paths that need evidence:

- Successful no-finding review marks the run `completed`.
- No-finding review with `tests_status: blocked_environment` marks `completed_with_residual_risk`.
- Review with findings marks `waiting_for_cursor_fix` and stores `prompts/fixes/01.txt`.
- `state.workflow.current_review_iteration` becomes `1` after the first review pass.
- First iteration includes real `codex` and `review` sections after review.
- Codex command resumes the exact prepared session ID.
- Codex command uses the probed option order with `--cd` and `--sandbox` before `resume`.
- Codex command does not include `--last`.
- Review prompt includes `$review-staged-cursor-execution` or whatever exact configured skill name is in state.
- Review prompt includes the original Cursor prompt content.
- Review prompt includes the latest Cursor final response content when available.
- Review prompt explicitly states when the latest Cursor final response is unavailable.
- Review decisions come from structured JSON fields, not Markdown text.
- Findings require a non-empty `cursor_fix_prompt`.
- No findings require `cursor_fix_prompt: null`.
- Invalid structured JSON marks the run `failed` and preserves raw Codex artifacts.
- Codex nonzero exit marks the run `failed`.
- Codex timeout marks the run `interrupted`.
- `logs/events.jsonl` exists for prepared runs and receives structured events during start/review.
- Event log detail fields do not contain full prompts, full staged patches, auth payloads, or obvious unredacted secrets.
- `logs --component codex` finds Codex artifacts after review.
- README and CLI output do not claim Cursor corrections or the complete loop are implemented.
- Automated tests do not invoke real Cursor or Codex model activity.

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

Also run focused tests while iterating:

```bash
uv run python -m pytest -q -s tests/unit/test_codex_runner.py
uv run python -m pytest -q -s tests/integration/test_start_codex_review.py
```

If pytest capture fails in this Codex desktop environment, keep using `-s` as documented in prior phase findings.

## Risks Or Recovery Notes

- Codex CLI option order is version-sensitive. Use the Phase 0 probed order and keep tests that would fail if `--cd` or `--sandbox` are placed after `resume`.
- `codex exec --output-last-message` may produce final structured content differently across versions. Preserve raw JSONL and the output-last-message file so failures are diagnosable.
- The review prompt intentionally includes the full original Cursor prompt and latest Cursor final response for Codex context. Do not duplicate those full contents in logs or default CLI output.
- `waiting_for_cursor_fix` is a phase boundary in Phase 4. It must not imply that a correction turn has run.
- Existing Phase 3 runs that already stopped in `staging` are not automatically continued by this phase. Full recovery belongs to a later `resume` phase.
- Structured event logging is now part of the audit and recovery contract. Add it carefully without breaking the existing human log.
- This repository's local `.agents/skills/review-staged-changes` is for reviewing changes to `ai_dev_loop`; target repositories provide their own configured review skill.

## OpenQuestions

None.
