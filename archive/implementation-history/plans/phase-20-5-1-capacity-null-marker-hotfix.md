# Phase 20.5.1 — Capacity Null-Marker Hotfix

## Goals

- Correct the accepted Phase 20.5 Codex capacity parser so a present
  `rateLimitReachedType: null` is treated as a valid “not reached” marker.
- Restore the already-implemented timer-driven transition from
  `waiting_codex_capacity` to the ordinary same-reviewer retry path when all
  authenticated capacity windows are valid and available.
- Preserve fail-closed behavior for malformed, incomplete, mixed, or
  unavailable App Server observations.
- Record that the Phase 20.6 recovery and Phase 20.6.5 rollover efforts were
  abandoned without integration because their Git/recovery authority retained
  P1 defects after repeated review.

## Non-Goals

- Do not implement fresh-review recovery, authenticated rollover, terminal-run
  supersession, managed recovery worktrees, integration CAS, or new Git
  authority.
- Do not change manual retry semantics, reviewer identity binding, review
  budgets, sequence lifecycle, or capacity-wait persistence.
- Do not accept or import code from either abandoned Phase 20.6 worktree.

## Scope

- Narrow parser correction in
  `src/ai_dev_loop/scheduler/application/codex_capacity_probe.py`.
- Focused unit and scheduler regression tests in the existing Phase 20.5 test
  suite for valid null markers and timer-driven automatic resume.
- One archival findings/decision record stating that Phase 20.6 and 20.6.5
  were not shipped and naming their retained P1 risks.

## Out of Scope

- `ai_dev_loop.yaml`, package-owned skills, hooks, integrations, migrations,
  schemas, database state, CLI commands, remotes, PRs, timer installation, and
  any source under the abandoned recovery/rollover prototypes.
- Real Codex account or App Server calls in automated tests.
- Cleanup, deletion, reset, or mutation of the abandoned runs and worktrees.

## Required Context

- `AGENTS.md`, all `.cursor/rules/*.mdc`, the accepted Phase 20.5 plan and
  findings, and `tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py`.
- Actual valid WSL App Server shape observed by the operator: a limit record
  with numeric `primary`/`secondary.usedPercent` values and
  `rateLimitReachedType: null` when the limit is not reached.
- Phase 20.5 already probes `waiting_codex_capacity` on scheduler ticks and
  preserves the same reviewer when capacity becomes available. This phase must
  fix only the parser condition preventing that existing path.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local `.cursor/rules/*.mdc` rule,
  especially orchestrator, state/schema, review, loop/resume, abort, and global
  integration contracts.
- Do not modify package-owned skills or invoke real agents in tests.
- Leave the intended result staged for the independent Codex review.

## Architecture Guardrails

- `_reached_marker_status` returns exhausted only for a nonempty string, valid
  not-reached for explicit `null`, and malformed for unsupported types or an
  empty/whitespace string.
- Missing `rateLimitReachedType` retains its existing valid-absent behavior.
- A record is available only when all present relevant evidence is valid and at
  least one valid window proves availability; any malformed sibling record or
  window keeps the overall observation unavailable unless another valid record
  conclusively proves exhaustion under the existing contract.
- Do not weaken timeout, protocol, output-bound, process-failure, cleanup, JSON
  shape, numeric finiteness, or mixed-record validation.
- Automatic resume must use the existing capacity-available event and ordinary
  same-reviewer retry flow. Do not add a new retry mechanism or reset timer.
- No implementation commit or installation before independent acceptance and
  explicit operator authorization.

## Implementation Plan

1. Update the reached-marker parser with the smallest typed change that treats
   explicit `None` as valid not-reached while preserving malformed handling.
2. Replace the obsolete null-is-malformed expectation and add table-driven
   coverage for missing, null, empty, whitespace, nonempty, boolean, numeric,
   list, and object marker values with available and exhausted windows.
3. Add or tighten a scheduler-level regression proving repeated timer ticks
   automatically leave `waiting_codex_capacity` and schedule the same reviewer
   when the probe returns the real null-marker/available-window shape.
4. Add an archival decision record for the abandoned Phase 20.6/20.6.5 scope;
   state clearly that no recovery/rollover implementation was accepted,
   committed, installed, or merged.
5. Run focused and full validation, update the Phase 20.5.1 findings artifact,
   stage the intended changes, and do not commit or install.

## Testing Criteria

- Unit tests for every marker type and window combination named above.
- Regression tests for multiple records, malformed siblings, exhaustion
  precedence, and explicit null with valid available windows.
- Scheduler integration/unit test with fake capacity probe and fake Codex
  runner proving timer-driven automatic resume keeps the bound reviewer and
  does not require `scheduler review retry`.
- Negative tests proving unavailable/exhausted observations remain waiting and
  never launch a review attempt.
- All tests use fakes and native `/tmp`; no network, credentials, or real model
  calls.

## Validation

- `git diff --cached --check`
- `uv run python -m ruff format --check .`
- `uv run python -m ruff check .`
- `uv run python -m mypy src`
- `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py`
- `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q`
- `uv run mkdocs build --strict`
- `uv build`

## Risks Or Recovery Notes

- The change affects automatic launch authorization after a provider-capacity
  observation. Fail closed on every unrecognized shape and retain focused
  regression evidence.
- If the provider changes its payload again, the scheduler should remain in
  `waiting_codex_capacity`; operators retain the explicit manual retry path.
- Abandoned worktrees and scheduler artifacts remain immutable audit evidence
  and are intentionally not cleaned up in this phase.

## OpenQuestions

None.
