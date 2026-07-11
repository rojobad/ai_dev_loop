# Phase 11 - Recover Failed Runs Through Successor Runs

## Goal

Add a safe, auditable command that recovers eligible terminal `failed` runs without editing them in place, rerunning completed Cursor work, or losing the original failure evidence.

The primary interface is:

```bash
ai_dev_loop recover <failed-run-id>
```

For an eligible failure after Cursor execution and staging, `recover` must create a new successor run that:

- references the failed source run;
- preserves the exact Codex session ID and Cursor chat ID;
- preserves completed iteration state and the staged-patch checkpoint;
- captures missing Phase 10 session runtime metadata for eligible legacy runs;
- starts at an explicit `interrupted` checkpoint whose next safe action is derived from durable artifacts;
- returns a new `recovery_run_id` and a safe `resume_command`;
- leaves the source run, repository contents, and staged index unchanged.

For the motivating incident, recovery must allow the successor to continue directly with Codex review iteration 1 rather than invoking Cursor again.

## Non-Goals

- Do not transition a terminal source run back to a non-terminal status.
- Do not edit, delete, rename, overwrite, or reinterpret the source run's state or artifacts.
- Do not recover `completed`, `completed_with_residual_risk`, `max_iterations_reached`, or `aborted` runs.
- Do not recover failures with ambiguous checkpoints or missing identity artifacts.
- Do not recover failed Cursor execution or failed staging in the first release unless durable evidence proves a no-repeat checkpoint as strictly as the review-retry case. Prefer a focused Codex-review recovery boundary.
- Do not commit, reset, clean, stash, unstage, apply patches, or otherwise rewrite the target repository.
- Do not run Cursor, Codex, or an updater from `recover`.
- Do not add `--continue`, `--start`, or implicit resume behavior in this phase.
- Do not automatically recover the user's real `crypto-sentinel-20260711T010911Z-87d698` run during implementation or validation.
- Do not publish packages, create commits, push branches, or open pull requests.

## Scope

- New `ai_dev_loop recover` CLI command and command module.
- Recoverability analysis based on source state plus durable artifacts.
- Successor-run creation, lineage, copied/recreated artifacts, manifest updates, and checkpoint state.
- Legacy Phase 9 session-runtime capture during recovery.
- Phase 10 effective runtime preservation for already deterministic runs.
- Idempotency and duplicate-successor handling.
- Status, inspect, list, logs, and JSON/text output needed to understand lineage and next actions.
- State models and JSON schemas for recovery lineage.
- Cursor rules governing terminal-run recovery.
- Unit, integration, privacy, filesystem-permission, CLI, and documentation coverage.
- Build, full validation, and installation of the verified `ai_dev_loop` package in WSL.

## Out of Scope

- Target application code in repositories managed by `ai_dev_loop`.
- Recovery by creating commits, temporary branches, stashes, worktrees, or patch application.
- Recovery across a different repository, branch, Git common directory, or HEAD.
- Recovery when the current staged patch differs from the recorded staged patch.
- Recovery when unstaged tracked changes or untracked files are present.
- Recovery of deleted or unavailable Codex sessions or missing Cursor chats.
- Recovery that changes model/reasoning overrides from the source run. A later explicit feature may support recovery with reviewed override changes.
- Updates to Codex Desktop or Cursor Desktop.
- Real model calls, real CLI self-updates, or real user-run recovery during automated validation.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-5-bounded-review-fix-loop-and-resume.md`
- `archive/implementation-history/plans/phase-6-real-abort.md`
- `archive/implementation-history/plans/phase-9-optional-codex-review-model-and-reasoning.md`
- `archive/implementation-history/plans/phase-10-codex-session-runtime-and-cli-compatibility.md`
- `src/ai_dev_loop/cli.py`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/resume_planner.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/run_discovery.py`
- `src/ai_dev_loop/locking.py`
- `src/ai_dev_loop/paths.py`
- `src/ai_dev_loop/commands/start_preflight.py`
- `src/ai_dev_loop/commands/prepare.py`
- `src/ai_dev_loop/commands/status.py`
- `src/ai_dev_loop/commands/inspect.py`
- `src/ai_dev_loop/commands/list_runs.py` or the actual run-list implementation
- `src/ai_dev_loop/review_runtime.py`
- `src/ai_dev_loop/integrations/codex/session_runtime.py`
- `src/ai_dev_loop/runners/codex.py`
- `src/ai_dev_loop/runners/staging.py`
- `src/ai_dev_loop/runners/git.py`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- Existing fake CLI, prepared-run, resume, Phase 10, permission, logging, and privacy fixtures under `tests/`
- User-facing docs for prepare/start/resume/abort, state/artifacts, observability, troubleshooting, CLI reference, configuration, safety/privacy, and phase traceability.

Motivating incident evidence:

- Source run: `crypto-sentinel-20260711T010911Z-87d698`.
- Source status: `failed` during Codex review iteration 1.
- Cursor iteration 1 completed and its chat ID remains available.
- Staging completed and the current staged patch matches `git/diffs/01.patch` exactly.
- No unstaged tracked changes or untracked files were present when recovery was analyzed.
- The source is a legacy Phase 9 run with unset session runtime fields.
- Safe session extraction resolves the exact Desktop-originated session to `gpt-5.6-sol` with reasoning `high`.
- The failed review artifacts show an old WSL Codex CLI attempting a cross-model resume and failing during pre-sampling compaction.
- Current `resume` correctly rejects `failed` because terminal states remain terminal.

This real run is evidence only. Cursor must use sanitized fixtures and must not recover or mutate it.

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

Update the relevant Cursor rules in the same change so future work preserves successor-run recovery semantics.

## Architecture Guardrails

- Terminal source runs remain terminal and immutable. Recovery creates a distinct successor run; it never changes `failed -> interrupted` on the source.
- Recovery must preserve both agent identities exactly: source `codex.session_id` and source `cursor.chat_id`. Never use `--last`, fuzzy session lookup, a new Codex session, or a replacement Cursor chat.
- Recovery must be checkpoint-driven, not `last_error` string parsing. Use state plus validated durable artifacts to determine recoverability and next action.
- A source run is eligible only when the next action can be proven without rerunning a completed agent turn.
- The target repository and index are read-only to `recover`. Do not invoke Git commands that mutate state.
- Before successor creation, require the same repository root, Git common directory, Git directory, branch, and initial HEAD recorded by the source run.
- Require the current staged patch to match the source iteration's recorded patch byte-for-byte through the existing canonical validation helper.
- Reject any unstaged tracked changes or untracked files.
- Validate source plan and prompt snapshots/hashes using existing contracts. Do not silently adopt changed target-repository plan or prompt content.
- Reuse existing validated helpers for Cursor completion, staging completion, review-result availability, session runtime extraction, and staged-patch equality. Do not implement parallel ad hoc detection.
- For Phase 10 sources with non-null effective model/reasoning and provenance, preserve the frozen runtime exactly. Do not reread the session and silently alter it.
- For eligible legacy Phase 9 sources with missing runtime metadata, use the safe exact-session runtime reader and record that recovery performed the migration. Never use WSL `config.toml` defaults.
- If session runtime extraction is missing, ambiguous, malformed, or unsafe, reject recovery before creating the successor.
- Copy sensitive artifacts using atomic writes/copies and user-only permissions where supported. Never hard-link sensitive mutable artifacts across runs.
- Artifact paths inside successor state and manifests remain relative to the successor run directory. Never store absolute source-run or rollout paths.
- Lineage must use run IDs and safe classifications, not filesystem paths or copied error payloads.
- The source failure evidence remains in the source run. The successor should reference it by source run ID and safe relative artifact classifications rather than copying raw failed stderr/events unless a dedicated recovery-history artifact is needed.
- `recover --dry-run` must be strictly read-only: no directories, locks that persist, state writes, updates, Git changes, or agent execution.
- `recover` itself never launches updaters. The returned `resume_command` may include user-selected flags only if they were explicitly supplied to a future design; in this phase return a neutral `ai_dev_loop resume <successor-id>` and document optional `--update-tools` usage.
- Core recovery logic must not depend on Typer, TTY prompts, or human-formatted output.
- A partially failed successor creation must not leave a discoverable runnable run. Use a temporary directory plus atomic final rename, or an equivalent transaction pattern.
- Automated tests must use temporary repositories, synthetic sensitive sentinels, sanitized session rollouts, and fake CLIs. Never access the real user run.

## Implementation Plan

### 1. Define Recovery Lineage And Result Models

Add an optional typed recovery section to `RunState`, for example:

```text
RecoveryState
  source_run_id
  source_status                 # failed
  source_iteration
  recovered_checkpoint          # reviewing | process_review
  source_staged_patch_sha256
  created_at
  runtime_migration             # none | phase9_session_capture
  reason_code                   # stable allowlisted classification
```

Requirements:

- Keep `RunState.run_id` unique and independent from `source_run_id`.
- Add only safe, audit-relevant fields.
- Do not copy raw `last_error`, stderr, event payloads, prompts, patches, or full session IDs into lineage.
- Keep `recovery` optional so historical states remain readable.
- Update `run-state-v1.json`, Pydantic models, serialization, schema/model-alignment tests, status/inspect/list rendering, and docs together.
- Use stable reason codes such as `codex_review_failed`, `codex_review_result_invalid`, or `codex_review_processing_failed`; derive them from checkpoint/artifact evidence and structured events where available, not arbitrary error substrings.

Define typed command results such as:

```text
RecoveryAnalysis
  eligible
  source_run_id
  source_status
  checkpoint
  iteration
  blockers[]
  warnings[]
  staged_patch_sha256
  session_runtime_action
  existing_successor_run_id?

RecoveryResult
  source_run_id
  recovery_run_id
  checkpoint
  iteration
  cursor_chat_id_short
  session_model
  session_reasoning_effort
  resume_command
  reused_existing_successor
```

Human output must shorten IDs where appropriate; JSON output may expose run IDs needed for automation but must follow existing session-ID privacy rules.

### 2. Implement Pure Recoverability Analysis

Create a focused module, for example `recovery_planner.py`, that analyzes a source run without mutation.

Eligibility baseline:

- Source status is exactly `failed`.
- Source run can be parsed under the supported schema/compatibility strategy.
- No active child process is registered and no live/ambiguous process metadata remains.
- Repository identity, branch, Git directories, and HEAD match.
- Plan and prompt contracts remain valid.
- Cursor chat ID exists in state and `cursor/chat.json`, and both agree.
- The active/latest iteration is unambiguous.
- Cursor turn metadata proves exit code 0 and not timed out.
- Staging artifacts prove completion.
- Current staged patch matches the recorded patch.
- No unstaged tracked changes or untracked files exist.
- The next action is either:
  - `review`: no valid structured review result exists; or
  - `process_review`: a valid structured review result exists but outcome processing did not complete.

For the first release, reject when the next safe action would be Cursor execution or staging. Report a stable blocker explaining that Phase 11 supports post-staging recovery only.

Do not classify eligibility by checking for the motivating error text. A different Codex nonzero exit at the same proven checkpoint should be recoverable under the same safety rules.

Analysis must return all safe blockers in deterministic order without leaking sensitive content.

### 3. Resolve Runtime For The Successor

Add a recovery-specific runtime resolver:

- Phase 10 source:
  - require effective `review_model` and `review_reasoning_effort` plus valid provenance;
  - preserve them exactly;
  - preserve captured `session_model` and `session_reasoning_effort` exactly;
  - copy or recreate `codex/session-runtime.json` from allowlisted source fields only;
  - reject inconsistent Phase 10 runtime state rather than recapturing and changing it.
- Legacy Phase 9 source:
  - detect with `is_legacy_phase9_codex_state`;
  - call `read_codex_session_runtime` with the exact source session ID;
  - set successor session and effective review model/reasoning to the captured values;
  - set provenance to `session`;
  - record `runtime_migration: phase9_session_capture`;
  - write a fresh safe `codex/session-runtime.json` artifact with the same privacy contract as Phase 10 prepare.

Explicit legacy values, if present, must retain explicit precedence independently. Do not collapse model and reasoning provenance into one decision.

If model/reasoning cannot be resolved safely, analysis is ineligible and no successor is created.

### 4. Create The Successor Run Transactionally

Add a command module such as `commands/recover.py` and a lower-level service that creates the successor only after successful analysis.

Successor creation requirements:

1. Acquire source-run and repository mutation locks in a documented deterministic order compatible with existing start/resume/abort locking.
2. Re-run recoverability checks under lock to prevent time-of-check/time-of-use drift.
3. Generate a normal unique run ID under the same project.
4. Build the successor in a temporary sibling directory with sensitive permissions.
5. Copy required immutable snapshots and completed-checkpoint artifacts atomically.
6. Write new state, manifest, lineage, runtime artifact, and logs.
7. Atomically rename the temporary directory into the discoverable run path.
8. Leave the source run and repository untouched throughout.

Copy only artifacts required for deterministic continuation and audit:

- plan snapshot and metadata;
- cursor initial prompt snapshot;
- source/effective config snapshots;
- Cursor chat metadata;
- completed Cursor iteration directories through the recovered iteration;
- Git status and staged-diff artifacts through the recovered iteration;
- persisted fix prompts and earlier valid review artifacts needed when recovering a later correction iteration;
- completed iteration state entries;
- safe session runtime artifact;
- any other manifest-listed artifact proven necessary by resume validation.

Do not copy the failed Codex attempt into the active successor review path. Preserve it in the immutable source run. If the successor needs an audit pointer, store only source run ID, iteration, safe reason code, and documented source artifact relative names without raw content.

The successor state should use:

- `status: interrupted`;
- the same repository identity and initial HEAD;
- the same plan/prompt paths and hashes;
- the same Cursor chat ID;
- the same Codex session ID;
- resolved/frozen runtime fields;
- copied iteration metadata;
- the same current review iteration;
- optional `last_error: null` and a safe recovery result message;
- `recovery` lineage populated.

`resume` must derive `reviewing` or review processing from copied durable artifacts. Do not hardcode a transition that bypasses `resume_planner` validation.

### 5. Add Idempotency And Successor Discovery

Repeated `recover` invocations must not silently create multiple active successors for the same source checkpoint and staged patch.

Under project/repository locks:

- Search runs in the same project for `recovery.source_run_id`, source iteration, checkpoint, and staged-patch hash.
- If a matching non-terminal successor exists and its repository/staged checkpoint remains valid, return it with `reused_existing_successor: true`.
- If a matching completed successor exists, report it and refuse to create another by default.
- If a prior successor is terminal `failed`, require the user to recover that successor explicitly so lineage remains a clear chain; do not skip generations.
- Ignore unrelated or malformed run directories conservatively, but report ambiguity if multiple matching successors exist.

Do not mutate the source run to record child IDs; successor lineage is the source of truth.

### 6. Add CLI Surface And Dry Run

Add:

```bash
ai_dev_loop recover RUN_ID [--dry-run] [--output text|json]
```

Behavior:

- `--dry-run` performs analysis only and reports eligibility, checkpoint, blockers, warnings, and whether runtime migration would be needed.
- Without `--dry-run`, create or reuse the successor and print the exact next command.
- Do not prompt.
- Do not invoke `resume` automatically.
- Do not invoke tool updates.
- If the source is ineligible, exit nonzero with concise blockers and no source/repository mutation.

Example successful text output:

```text
Source run: crypto-sentinel-...
Recovery run: crypto-sentinel-...
Recovered checkpoint: Codex review, iteration 1
Cursor chat: 1c9d071f...
Session runtime: gpt-5.6-sol / high
Runtime migration: phase9_session_capture
Repository and staged patch: verified
Next command: ai_dev_loop resume <recovery-run-id> --update-tools
```

The output may recommend `--update-tools` when compatibility analysis indicates it, but must not claim an update exists. Prefer returning a neutral `resume_command` plus an optional `recommended_resume_command` if needed for structured output.

Add `recover --help` and CLI-reference coverage.

### 7. Integrate Lineage With Status, Inspect, List, Logs, And Resume

Update observability:

- `status` shows that a run is a recovery successor, shortened source run ID, recovered checkpoint, and next safe action.
- `inspect` shows full run lineage IDs where existing inspect privacy policy permits run IDs, runtime migration, source iteration, checkpoint, and safe artifact names.
- `list` marks recovery successors without exposing session IDs.
- orchestrator logs/events record recovery analysis, successor creation, reuse, and refusal using safe reason codes.
- the source run remains byte-for-byte unchanged, including its logs and timestamps.

Update resume integration only as needed so an `interrupted` successor created by `recover` follows existing checkpoint restoration and compatibility/update behavior.

Do not weaken terminal rejection in ordinary `resume`; `resume <failed-source-id>` must still fail.

On successor `resume`:

- tool compatibility and optional `--update-tools` behavior remain Phase 10 responsibilities;
- completed Cursor/staging work must not rerun;
- partial/new Codex review artifacts are written only inside the successor;
- findings continue through the inherited Cursor chat;
- max-iteration counting remains unchanged.

### 8. Update State Machine, Rules, Schemas, And Documentation

Do not add `FAILED -> INTERRUPTED` to `ALLOWED_STATUS_TRANSITIONS`. The successor is created directly as a new `interrupted` run with lineage; source transition semantics remain unchanged.

Update relevant Cursor rules to state:

- terminal runs remain immutable;
- explicit `recover` creates a successor;
- successor recovery is checkpoint- and artifact-driven;
- Git/index mutation is forbidden;
- identity and runtime preservation are mandatory;
- ordinary `resume` still rejects failed source runs.

Update user-facing documentation, including at minimum:

- README command/workflow summary;
- quick guide;
- prepare/start/resume/abort operations page, renamed or extended to include recover;
- state and artifacts;
- observability;
- troubleshooting;
- security and privacy;
- CLI reference;
- phase traceability;
- any recovery/manual-cleanup guidance that currently says only “prepare a new run.”

Documentation must include:

- `recover --dry-run` before actual recovery;
- eligibility matrix;
- immutable source/successor model;
- exact Git safety requirements;
- Phase 9 runtime migration behavior;
- Phase 10 frozen-runtime behavior;
- interaction with `resume --update-tools`;
- refusal examples and manual fallback;
- statement that `recover` never updates tools or invokes agents;
- statement that update flags belong to the returned `resume` command;
- example based on sanitized IDs, not the user's real run.

### 9. Automated Tests

Unit tests:

- Recovery lineage model/schema alignment and historical state compatibility.
- Pure eligibility analysis for review and process-review checkpoints.
- Stable blocker ordering.
- Phase 10 runtime preservation.
- Legacy Phase 9 exact-session runtime migration.
- Missing/ambiguous session runtime rejection.
- Source and successor artifact selection.
- Idempotent successor discovery.
- Text and JSON rendering without sensitive leakage.
- `--dry-run` produces no filesystem changes.

Integration tests with fake CLIs and temporary repositories:

- Recover a failed Codex review after completed Cursor and staging.
- Source run directory hash/tree remains unchanged before and after recovery.
- Repository `git status --porcelain=v1` and staged patch remain unchanged.
- Successor uses a new run ID and correct lineage.
- Successor `resume` goes directly to Codex review and never invokes Cursor again.
- Findings from recovered review resume the same Cursor chat for correction.
- No-findings recovered review completes normally.
- Valid existing review result resumes at processing without re-running Codex.
- Legacy Phase 9 source captures session model/reasoning and successor review argv passes both explicitly.
- Phase 10 source retains its frozen explicit/session-derived runtime even if rollout/YAML changed.
- Tool incompatibility is handled by successor `resume`; `recover` never invokes updater binaries.
- Repeated recovery returns the existing matching active successor.
- A failed successor must itself be selected for another recovery; recovering the grandparent does not skip it.
- Reject source statuses other than `failed`.
- Reject missing/mismatched chat ID.
- Reject incomplete Cursor turn.
- Reject incomplete staging.
- Reject staged-patch drift, empty index versus non-empty artifact, unstaged tracked changes, and untracked files.
- Reject branch, HEAD, Git directory, plan, or prompt drift.
- Reject active/ambiguous child-process metadata.
- Transaction failure before final rename leaves no discoverable runnable successor.
- Concurrent recover attempts produce one reusable successor or a deterministic lock error, never two active successors.

Privacy and permission tests:

- Source transcript, prompts, patches, stderr, review Markdown, fix prompts, full session IDs, auth data, and full environments do not appear in default recover output, status, logs, events, lineage, or error messages.
- Copied sensitive successor artifacts use user-only permissions where supported.
- No absolute source-run or rollout paths appear in successor state/manifest/runtime artifacts.

Use fake `agent` and `codex` binaries and sanitized rollout fixtures. Do not execute real model calls, real self-updaters, or the real motivating run.

### 10. Validation And WSL Installation

Run the complete required suite:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q -s
uv run python -m build
uv run mkdocs build --strict
uv run ai_dev_loop --help
uv run ai_dev_loop recover --help
uv run ai_dev_loop status --help
uv run ai_dev_loop inspect --help
uv run ai_dev_loop resume --help
uv run ai_dev_loop doctor
```

Add a sanitized end-to-end acceptance scenario that:

1. Creates a fixture source run.
2. Completes fake Cursor and staging.
3. Forces fake Codex review failure.
4. Runs `recover --dry-run` and proves no mutation.
5. Runs `recover` and obtains a successor.
6. Runs successor `resume` with fake tools.
7. Proves Cursor was not rerun and the review loop completed.

After all checks pass, install the verified package into WSL:

```bash
uv tool install --force .
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop recover --help
```

Do not run `recover` against any real user run as part of installation verification. The user must explicitly authorize real recovery in a separate step after reviewing the implementation.

## Testing Criteria

Acceptance requires automated evidence that:

- Failed source runs remain terminal and byte-for-byte immutable.
- Eligible post-staging failures produce a distinct successor with auditable lineage.
- Recovery never mutates Git state or reruns completed Cursor/staging work.
- Exact Cursor chat and Codex session identities are preserved.
- Current staged patch must match the source artifact before and during creation.
- Phase 10 runtime stays frozen; eligible Phase 9 runtime is safely captured and explicitly passed on successor review.
- Successor resume derives the correct review/process-review checkpoint from artifacts.
- Duplicate recovery is idempotent.
- Ineligible or ambiguous recovery fails before successor creation.
- Dry run is genuinely read-only.
- Privacy and sensitive-permission contracts hold.
- Full static analysis, tests, build, strict docs, fake E2E, and installed CLI smoke checks pass.

## Validation

Cursor's final response must report:

- Files changed, grouped by recovery planner/service, CLI/workflow, state/schema, tests, rules, and docs.
- Exact recoverability matrix.
- Source immutability and successor lineage design.
- Which artifacts are copied and which remain only in the source.
- Phase 9 versus Phase 10 runtime behavior.
- Idempotency behavior.
- Dry-run guarantees.
- Commands executed and pass/fail results.
- Fake E2E evidence that Cursor was not rerun.
- Installed WSL `ai_dev_loop` path/version and `recover --help` result.
- Confirmation that no real user run, real updater, or real model process was invoked.

## Risks Or Recovery Notes

- Copying too few artifacts can make successor resume ambiguous; copying too many can duplicate sensitive or failed content unnecessarily. Route artifact selection through explicit checkpoint requirements and test every copied path.
- The source target repository may change between analysis and creation. Revalidate under locks immediately before writing the successor.
- Existing lock acquisition order must be preserved to avoid deadlocks with start/resume/abort. Document and test the chosen order.
- A legacy Phase 9 session can have changed since the source run was prepared because runtime was not frozen then. Record recovery-time capture explicitly; do not claim it was the historical runtime if evidence cannot prove that.
- A Codex failure can leave partial events but no structured result. Keep those artifacts only in the source; successor review starts a clean attempt while lineage preserves auditability.
- A valid structured review result may exist despite a later processing failure. Resume processing it only after revalidating the current staged patch.
- Multiple recovery generations must form an explicit chain. Do not silently leap from an old source to a later checkpoint.
- Package version remains `0.1.0` unless the repository's release/versioning policy explicitly requires a bump; installed behavior must be verified independently of version-string change.
- If installation fails, preserve the prior WSL executable and report the failure; do not delete tool directories manually.

## OpenQuestions

None.
