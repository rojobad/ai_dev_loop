# Phase 16.8 Gate B Cursor Chat-Creation Boundary Correction

## Goals

- Make `agent create-chat` a bounded, abort-controllable and auditable Cursor
  boundary instead of an unbounded pre-Cursor call.
- Preserve one-chat-per-run identity: if creation times out or is ambiguous
  before a chat ID is durably persisted, fail closed and never issue another
  `create-chat` for that run.
- Surface a privacy-safe durable carrier failure to `pr-review-v2` rather than
  leave a worker permanently claimed in `validating`.

The defining incident completed probes, then remained `validating` with zero
iterations and no recorded chat while an `agent … create-chat` child lived for
over twenty minutes. A direct standalone `agent create-chat` call completed
promptly. This plan fixes the unbounded process boundary; it does not diagnose
or modify Cursor's internal service implementation.

## Non-Goals

- Do not change Cursor capacity, socket/agent configuration, model selection,
  global environment, public config, model catalogs or retry limits.
- Do not retry or recover a chat-creation timeout automatically: a remote chat
  might have been created even though its ID was never received.
- Do not implement the separate carrier usage-limit recovery, change later
  local-fix/Codex/GitHub/Git semantics, complete Gate B, or repair a live run.
- Do not infer an error from a generic child exit or alter legacy PR-review
  behavior except by preserving shared Cursor chat-create safety.

## Scope

Expected production seams:

- `src/ai_dev_loop/runners/cursor.py`
- `src/ai_dev_loop/workflow_engine.py`
- shared process/abort helpers only if the existing streaming process contract
  needs a narrow correction.
- `src/ai_dev_loop/pr_review_v2/infrastructure/local_fix_adapter.py` only to
  map a terminal carrier failure before durable chat persistence into an
  explicit safe v2 block.
- current process/start/abort/v2 carrier control privacy tests and honest docs.

## Out of Scope

- Live Gate B state, PRs, branches, checkouts, XDG state/artifacts/SQLite,
  GitHub/Cursor/Codex/models/network/credentials/socket access.
- Manual process signaling or edits to state, artifacts, prompts, sessions,
  chats, Git history or GitHub content.
- Commit, push, install, start, launch, resume, abort, reset, clean, stash,
  checkout, rebase, merge or other Git-state change.

## Required Context

Read before editing:

1. This plan and `phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`.
2. `archive/implementation-history/findings/phase-16-7-to-16-8-handoff.md`.
3. `runners/cursor.py`, `process.py`, `abort_control.py`, `workflow_engine.py`,
   start preflight, state and resume planner modules.
4. `pr_review_v2_carrier.py`, v2 local adapter/executor/control/abort code,
   and current Phase 16.8 carrier/control tests.
5. `tests/unit/test_process.py`, `tests/integration/test_start_cursor.py`,
   local restart/carrier helpers and A/B barrier tests.
6. Current CLI, troubleshooting and observability docs.
7. Every applicable `.cursor/rules/*.mdc` file.

Use only the sanitized incident facts above. Do not inspect the actual run or
raw live carrier artifacts.

## Cursor Rules And Skills

Read and obey all mandatory repository rules:

- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`

No repository `AGENTS.md` or `.cursor/skills/` exists at planning time. If one
appears, obey it before editing. Do not invoke the staged-review skill.

## Architecture Guardrails

1. **One chat identity.** A chat becomes authoritative only after parsing,
   validation and durable state plus `cursor/chat.json` persistence. Timeout,
   kill, malformed output or abort beforehand is ambiguous: never retry,
   infer an ID or use `--last`.
2. **Bounded registered child.** Use explicit repo cwd, the existing workflow
   Cursor timeout, process-group terminate/reap, owner-only output capture and
   `ActiveProcessRegistration` before the child can run uncontrolled. Failed
   registration must terminate/reap the group.
3. **Abort wins.** Check abort before launch and after child return. Abort
   during creation persists first, signals only the registered group, creates
   no replacement chat state and ends `aborted`, never generic `failed`.
4. **Honest state.** Do not set `running_cursor`, create an iteration or write
   a chat before success. A creation failure must durably fail the carrier with
   a fixed safe diagnostic; do not strand `validating` or add `FAILED ->
   INTERRUPTED`.
5. **Privacy.** Raw stdout/stderr, chat ID, paths and provider details stay in
   owner-only artifacts. Default errors/events/logs/status have fixed/redacted
   summaries only.
6. **v2 fail-closed.** A carrier terminal failure before a durable chat maps to
   an inspectable, nonretryable parent block, not a timer retry, successor,
   local-fix success or remote mutation.
7. **Shared compatibility.** Preserve normal legacy start/resume, persisted
   chat replay, baseline and review identity checks.

## Implementation Plan

### 1. Create a controlled chat-create runner

- Replace unbounded `run_process([cursor_command, "create-chat"])` with a
  typed/structured operation built on the existing streaming process primitive
  (or a narrowly equivalent helper).
- Pass explicit cwd, state-derived Cursor timeout, run directory, run ID and a
  pre-Cursor iteration identity. Use argv arrays and redacted argv metadata.
- Persist bounded stdout/stderr and minimal safe metadata at deterministic
  sensitive artifact paths. Keep chat IDs/raw child text out of default output.
- Distinguish valid success, timeout, nonzero exit, invalid/empty ID,
  registration failure and abort. Terminate/reap all non-success process groups.

### 2. Make workflow transition and persistence safe

- Invoke the controlled operation after current preflight/abort checks and
  before `running_cursor`/iteration work.
- On success, retain immediate durable chat-ID/state and `cursor/chat.json`
  persistence. Resume reuses it exactly and never creates another chat once
  either checkpoint proves progress.
- On timeout/nonzero/malformed/registration error, store only a constant safe
  category, a terminal failed source and protected diagnostics. Do not leave a
  resumable chat identity, create staging/review effects or permit ordinary
  resume/recover to manufacture a replacement chat.
- If abort races with a returned valid ID, abort wins: do not persist the ID and
  finish via existing abort finalization.

### 3. Integrate v2 carrier conservatively

- `FilesystemLocalCarrierRuntime` must inherit this behavior through the shared
  local-review workflow without environment-specific code.
- Convert a carrier terminal failure before durable chat at `LocalFixAdapter`
  into a fixed safe adapter failure so `LocalEffectExecutor` creates the
  existing inspectable non-retryable parent block. Do not leak raw output or
  persist a v2 local-fix result.
- Prove v2 control/abort can signal a blocking create-chat child while it is
  registered, and cannot treat its carrier as terminal accepted/replayable.

### 4. Documentation

- Update current docs only if needed: chat-create timeout is fail-closed and
  needs inspection/new preparation, not blind resume. Do not mention internal
  socket paths or promise a Cursor-side remedy.

## Testing Criteria

Use fake `agent`/`codex`, isolated native-XDG state and temporary repositories.
Never call real models, live state, credentials or network. Do not add public
configuration fault controls.

Prove with unit and real local-process integration tests:

1. Normal creation uses explicit cwd/timeout, creates exactly one durable chat
   and preserves normal start/replay behavior.
2. A blocking fake child is registered, timed out, terminated and reaped; no
   process or active metadata remains live afterward.
3. Timeout/nonzero/malformed output yields no chat file/state ID, iteration,
   staging/review/Git mutation; it leaves a terminal safe failure and only
   protected diagnostics.
4. Abort during blocking creation persists first, signals only the child group,
   leaves repository contents unchanged and reaches `aborted`, not `failed`.
5. Valid output racing with abort does not become a persisted resumable chat.
6. A v2 real filesystem carrier blocking here creates one safe parent block,
   no retry timer/local-fix success/GitHub write/second carrier or chat.
7. Reopen/restart cannot rerun create-chat on the failed source; terminal
   accepted-carrier replay still never invokes agents.
8. Registration failure, unsafe artifact path/mode, stale active metadata,
   legacy start/resume and Phase 16.1 A/B barrier remain fail-closed/green.

## Validation

Run focused tests, then at minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/test_process.py tests/integration/test_start_cursor.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/pr_review_v2 tests/integration/test_phase16_8_local_restart.py tests/integration/test_phase16_8_control_matrix.py tests/integration/test_phase16_8_privacy.py tests/integration/test_phase16_1_ab_regression_barrier.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
```

If unrelated global formatting drift remains, do not fix it only for a pass;
report it separately and format every changed file.

## Risks Or Recovery Notes

- This correction cannot safely rescue the current carrier that hung before
  persisting a chat ID. After staged review, commit/push and CLI installation,
  a fresh Gate B run is the safe path; never edit the current run state.
- A direct successful `agent create-chat` probe shows only that one interactive
  invocation works. It does not prove detached-worker environment equivalence.
- Gate B remains controller/operator work after review. Cursor must not touch
  live acceptance state.

## OpenQuestions

None.
