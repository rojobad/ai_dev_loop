# Phase 17.4 — Cursor, staging, and usage-limit continuation

## Goal

Deliver the first real scheduler vertical slice: after controller A explicitly
starts a submitted run, ticks run preflight, create/reuse the exact Cursor chat,
launch and observe the initial Cursor turn through the systemd attempt backend,
verify its result, normalize staging, and stop at `awaiting_codex_review`.

It also implements safe automatic continuation after a classified Cursor usage
limit: preserve verified partial work and the exact chat/model, then resume on
the first tick after provider `retry-after` or the five-hour fallback.

## Non-Goals

- Do not launch Codex, parse a review result, schedule correction loops, or
  declare a run completed.
- Do not select a fallback Cursor model, run tool updaters, or retry unknown
  Cursor failures automatically.
- Do not enable systemd, delete legacy roots, or change PR-review.

## Scope

- Convert bounded initial-run preflight/probes, Cursor chat creation/turn,
  result ingestion, post-Cursor fingerprint, and `git add -A` normalization into
  scheduler effects.
- Use Phase 17.3 attempts for all agent CLI calls and record one exact chat ID.
- Add `waiting_usage_limit`/equivalent durable timer behavior and continuation
  envelope for initial and later-compatible Cursor-turn context.

## Out of Scope

- Codex invocation, findings, max-review state, public abort command, cutover,
  real systemd/Cursor acceptance, and changes to `ai_dev_loop.yaml`.

## Required Context

Read the master and Phases 17.1–17.3, all Cursor rules,
`workflow_engine.py`, `runners/cursor.py`, `runners/cursor_failure.py`,
`runners/cursor_output.py`, `runners/staging.py`, `runners/git.py`,
`commands/start_preflight.py`, `resume_planner.py`, and current Cursor/staging/
usage-limit test suites.

## Cursor Rules And Skills

Follow every `.cursor/rules/*.mdc`, especially loop/resume, agent identity,
Git/staging, subprocess, privacy, and exact-session rules. Documentation is
evidence-based only; staged review remains a later independent action.

## Architecture Guardrails

- A tick calls no synchronous workflow loop and never waits for Cursor. Chat
  creation and each Cursor turn are distinct, fenced durable effects.
- Preserve current preflight identity/baseline/plan/prompt validation and model
  compatibility behavior, except ticks must block rather than prompt/update.
- Use existing exact positional prompt delivery, stream artifacts, output bounds,
  process timeout, chat persistence, and post-Cursor content fingerprint rules.
- After verified success, always run `git add -A`, capture normalization and
  staged patch artifacts, and require the current staged patch to equal the
  recorded artifact before exposing `awaiting_codex_review`.
- On recognized usage limit only, persist the classifier evidence, binary-safe
  fingerprint, exact original prompt/fix context, and continuation envelope.
  Schedule `retry_due` using provider delay or 18,000 seconds. The retry uses
  the same chat and same frozen model; a repeated limit reschedules, not falls
  back. Any ambiguous/partial/unclassified failure blocks.

## Implementation Plan

1. Extract bounded preflight/probe and Cursor/staging primitives from the old
   workflow behind scheduler effect adapters. Characterize current behavior
   before moving it; do not import the old `while` loop.
2. Add state/effects for preflight, create-chat, launch/observe Cursor, ingest
   result, staging, and `awaiting_codex_review`. Persist every transition before
   scheduling the next action.
3. Connect Cursor effects to the systemd attempt service. Ensure a crash between
   chat output, process exit, parsing, fingerprint, and staging either continues
   from verified evidence or blocks safely without a new chat/turn.
4. Implement usage-limit classification/timer/continuation. Preserve partial
   work and refuse continuation when its fingerprint, branch, HEAD, or prompt
   binding differs. Do not use current legacy successor/mode-switch semantics.
5. Provide redacted scheduler status for active Cursor, usage-limit wait-until,
   staging, awaiting review, and blocked outcomes.

## Testing Criteria

- Integration fake-Cursor traces across separate ticks: submit/start -> active
  turn -> active observation -> verified completion -> stage -> awaiting review.
- Test chat creation crash/reconciliation, no duplicate chat/turn, index mutation
  normalization, staged patch drift, repository/plan/prompt drift, malformed
  result, timeout, and non-interactive tool incompatibility.
- Test classified usage limit with partial work: first retry only after due time,
  same chat/model/envelope, repeated-limit reschedule, and fingerprint mismatch
  block. Test unclassified failure never retries.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/test_cursor_usage_limit.py tests/unit/test_cursor_runner.py tests/integration/test_start_cursor.py tests/integration/test_phase17_4_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

Usage-limit recovery is safe only when classification and partial-work fingerprint
are contemporaneous and verified. A timer is not evidence the provider reset;
the due retry is one controlled continuation attempt, and a repeated limit is
normal durable backoff, not a reason to change models or duplicate work.

## OpenQuestions

None. The five-hour fallback, same-model/same-chat continuation, and conservative
blocking policy are frozen by the master plan.
