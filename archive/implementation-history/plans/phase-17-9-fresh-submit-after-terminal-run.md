# Phase 17.9 — Fresh scheduler submission after a terminal run

## Goal

Correct the scheduler submission boundary exposed by the Parish360 manual acceptance run: an identical `scheduler submit` after a source run has been aborted must never be represented as a new queued run or advertise `scheduler start` without an active reservation. An operator must have an explicit, idempotent way to submit a distinct fresh run while preserving the terminal source run and all of its artifacts.

## Non-Goals

- Do not retry, resume, recover, rewrite, or delete a terminal source run.
- Do not change target-repository YAML, model selection, reviewer-B lifecycle, systemd behavior, timer state, or target Git state.
- Do not make a real Cursor or Codex call in automated tests.
- Do not add a random implicit submission nonce that could make an uncertain command replay create a duplicate run.

## Scope

- Make `scheduler submit` report the existing run's actual durable state and safe next action on an idempotent replay; it must not hard-code `queued` or a start action for terminal states.
- Add an explicit user-owned `--resubmission-id` value for an intentional fresh submission following a terminal run. It is an opaque UUID-form identifier: the caller must reuse the same value to replay that new submission idempotently.
- Hash the explicit resubmission identifier into the submission idempotency identity without persisting or rendering the raw value. The unchanged base submission identity remains the default for existing non-terminal replay behavior.
- Document the safe abort-then-fresh-submit workflow and record concise phase findings with actual validation results.

## Out of Scope

- Ledger/table migrations, mutation of historic idempotency keys, deletion of protected artifacts, and automatic successor creation.
- A convenience mode that silently generates a resubmission identifier.
- Changes to active reservation conflict rules: a distinct resubmission still fails while any non-terminal run owns the same worktree.

## Required Context

Read before editing:

- `AGENTS.md` and all applicable `.cursor/rules/*.mdc`, especially governance, orchestrator, state/schema, abort, and loop/resume contracts;
- `archive/implementation-history/master-plan.md`;
- `archive/implementation-history/plans/phase-17-1-central-ledger-and-submission.md`, `phase-17-6-abort-recovery-and-operations.md`, and `phase-17-8-codex-attempt-bounds-and-systemd-budget.md`;
- `src/ai_dev_loop/scheduler/application/submission.py`, `contracts.py`, `start.py`, `status.py`, and `abort.py`;
- `src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`, `src/ai_dev_loop/commands/scheduler.py`, and `src/ai_dev_loop/cli.py`;
- `tests/integration/test_phase17_1_submit.py`, `tests/unit/scheduler/test_start.py`, and Phase 17.6 abort/restart tests;
- `docs/operacion/prepare-start-resume-abort.md`, `docs/operacion/estado-artefactos.md`, and `docs/operacion/troubleshooting.md`.

The manual acceptance evidence is limited to this safe fact: after a real operator aborted a Parish360 run, the exact same submit inputs returned that terminal run with `reused_existing: true`, emitted a misleading queued/start action, and `scheduler start` correctly refused it because its reservation had been released. Do not copy run IDs, session IDs, private artifacts, prompts, or target code into repository artifacts or documentation.

## Cursor Rules And Skills

- Follow all applicable repository rules and `AGENTS.md`.
- Use fake `agent` and `codex` executables only in automated tests. Do not run real models, systemd acceptance, or mutate the Parish360 run.
- Preserve the strict staged-review boundary: leave the implementation staged for an independent review; do not self-review by changing the staged diff.
- Do not stage, commit, push, reset, clean, stash, unstage, or perform destructive Git operations.

## Architecture Guardrails

- Terminal runs are immutable audit records. A resubmission must create a new run and new protected artifacts; it must not relabel, revive, or alter the terminal source run, its events, or its reservation history.
- Existing submissions with matching non-terminal identity remain idempotent. A repeated command with the same explicit `--resubmission-id` must return the same new run, never a second one.
- The resubmission identifier is caller-owned and intentionally explicit. Do not generate it from current time, an environment variable, random state, or model/session data. Do not write its raw value to ledger state, events, artifacts, status, history, logs, errors, or documentation; use only a one-way hash in the idempotency calculation.
- Reservation admission remains authoritative: a fresh submission may claim a released reservation only, and must fail without side effects if another non-terminal run owns the worktree.
- `SubmitResult` and CLI JSON/text are user-visible contracts. Their state and safe next action must derive from the actual state, including terminal `aborted`, not from an assumed queued state.
- Preserve privacy/redaction, frozen inputs, exact controller identity, fresh-reviewer B semantics, and no target-repository orchestrator state.

## Implementation Plan

1. Extend the submit input boundary with a validated optional `--resubmission-id` UUID and its `SubmitOptions` counterpart. Keep it out of the persisted context and all rendered output.
2. Derive a distinct idempotency key only when that explicit value is supplied, using a one-way hash of it together with the existing canonical submission identity. Preserve base-key behavior for callers that omit the option.
3. Correct idempotent replay reporting. Reused terminal states must return their real state kind and state-appropriate safe action; JSON `status` and text must not say a terminal run is queued/submitted or instruct a start.
4. Permit an explicit resubmission to insert a distinct queued run after the old reservation is safely released. Preserve normal active-worktree conflict behavior and all insert/artifact integrity checks.
5. Update focused operations documentation to explain that an abort is non-destructive and that an operator chooses a new UUID-form `--resubmission-id` for an intentional fresh run, retaining it for any idempotent command replay. Add a concise findings artifact without private operational data.
6. Leave all changes staged for independent review.

## Testing Criteria

- Integration coverage proves a normal identical submit reuses the live queued run exactly as before.
- After aborting a run with no active child, an identical base submission returns the actual `aborted` state with no start action and creates no new run, reservation, or artifact mutation.
- An explicit UUID-form resubmission identifier after that abort creates a distinct queued run with an active reservation and can be authorized by `scheduler start`.
- Repeating that exact explicit resubmission command returns the same fresh run with no duplicate row/artifacts. A different identifier while that new run is non-terminal still fails on the active worktree reservation.
- Invalid identifiers fail before state/artifact writes. CLI JSON/text tests prove no raw identifier, full session ID, prompt, or other sensitive data is exposed and that `status` matches `state_kind`.
- Abort regression tests continue proving the terminal source and its target worktree contents are unchanged.

## Validation

Run focused tests first, then the full suite and quality gates:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase17_1_submit.py \
  tests/unit/scheduler/test_start.py \
  tests/unit/scheduler/test_phase17_6_abort_corrections.py \
  tests/integration/test_phase17_6_restart_reconcile.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Risks Or Recovery Notes

An aborted source remains terminal even when an operator submits a fresh run: the fresh run has its own reservation, artifacts, controller binding, and later fresh reviewer B. No real Parish360 run is mutated in this implementation. Operators must retain an explicit resubmission ID only long enough to repeat an uncertain submit without duplication; it is not a credential.

## OpenQuestions

None. An explicit UUID-form resubmission identity preserves durable idempotence without reinterpreting a terminal run or silently creating duplicate work.
