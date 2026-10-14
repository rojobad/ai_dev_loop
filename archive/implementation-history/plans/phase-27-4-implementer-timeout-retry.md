# Phase 27.4 — Timeout continuation

## Goal

Complete same-run Codex timeout continuation and a provider-neutral retry entry point.

## Non-Goals

No generic blocked-failure successor recovery yet.

## Scope

Timeout ingestion, durable retry metadata/timers, exact-session continuation,
CLI authorization, capacity/abort coordination and honest projections/docs.

## Out of Scope

Force recovery (27.5/27.6), new sequence adoption, real workstation validation.

## Implementation Plan

- R-01: Add implementer-retry no-flag production CLI path, delegating Cursor to
  its existing timeout service. Reject unsupported --force until later phase.
  Persist authorization before dispatch, with generation/fence deduplication;
  tick owns the subprocess. Preserve cursor-retry compatibility.
- R-02: On authenticated Codex timeout with confirmed termination and bound
  session, persist same-run retry intent and exact logical turn/prompt binding.
  Apply three automatic retries at 30-minute intervals; manual request advances
  wait or retries after exhaustion without fabricating another review. Continue
  same session/config/partial work; no fresh admission or staging/review before
  successful completion. Timeout after a capacity continuation shares turn budget.
- R-03: Reject identity/integrity/termination uncertainty and aborted state;
  retain required holds. Capacity exhaustion during retry enters existing role
  wait, then continues exact turn. Restart/manual-vs-auto tick races dispatch one
  attempt; late results do not revive cancelled or superseded work. Resolved
  termination evidence allows supported next action without permanent orphan hold.

## Testing Criteria

Add tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py: exercise real
CLI authorization, ingest/tick, fake adapter termination, clock advancement and
restart. Independent assertions cover retry count/spacing/reset, manual advance,
exhaustion, exact argv/session/prompt, initial/correction, capacity roundtrip,
abort and deterministic manual/automatic race one dispatch. Corrupt/stale evidence
and uncertain identity never launch; verify resolution convergence. Regression
selection: phase27_2 implementer/capacity tests, test_attempt_executor.py,
Cursor timeout tests discovered in checkout and test_phase19_codex_capacity.py.

## Required Context

Read phase-27-recovery-contract.md, phase-27-dual-implementer-contract.md,
AGENTS.md, docs/referencia/cli.md, docs/referencia/configuracion.md,
docs/referencia/integration-api.md, docs/operacion/estado-artefactos.md and
docs/operacion/seguridad-privacidad.md. Phase 27.1/27.2/27.3 are accepted commits
062f43e/128b5cb/8693cf9 on codex/phase-27-dual-implementer; recheck production
prerequisites and tests, not merely commit messages. Global pipeline validation
was pending at planning time. Phase 24/25/26 are not prerequisites.

Relevant production: scheduler/application/cursor_timeout_retry.py,
cursor_initial_recovery.py, cursor_correction_recovery.py,
sequence_cursor_recovery.py, cursor_recovery_evidence.py,
codex_workflow_service.py, attempt_service.py,
sequence_reconcile.py, sequence_materializer.py, sequence_abort.py;
scheduler/codex_implementer_attempt_runner.py; domain/state.py, events.py,
reducer.py, cursor_recovery_v2.py, cursor_sequence_replacement.py;
infrastructure/sqlite_store.py and protected_artifacts.py; cli.py;
integration_api/info.py, models.py, run_projection.py and sequence_projection.py.
Use real existing module names discovered in checkout, not speculative files.

## Cursor Rules And Skills

The execution agent is Codex; repository Cursor governance remains applicable.
Follow AGENTS.md and .cursor/rules/ai-dev-loop-governance.mdc,
ai-dev-loop-orchestrator-contracts.mdc, ai-dev-loop-state-and-schema-contracts.mdc,
ai-dev-loop-loop-and-resume-contracts.mdc, ai-dev-loop-abort-contracts.mdc,
ai-dev-loop-codex-review-contracts.mdc and ai-dev-loop-docs-acceptance-contracts.mdc.
Apply global-integrations contracts when changing integration surfaces. No
.cursor/skills directory exists. Read .agents/skills/create-cursor-plan/SKILL.md
and ai-dev-loop-docs-acceptance-governance/SKILL.md for reporting/docs evidence.
Reviewer follows review-staged-ai-dev-loop-execution separately; implementation
must not invoke or modify the review skill.

## Architecture Guardrails

Ledger/protected artifacts and authenticated role-specific bindings are authority.
Domain reducers own transitions; services own fenced transactions; adapters own
CLI execution/parse; presentation never infers eligibility from prose. Keep
models/schemas/migrations/readers/projections aligned and old snapshots readable.
No state in target repos, continuous baseline gates, secrets/full environments,
agent-generated shell commands, destructive Git or external Git operations.
Preserve reservations during uncertainty and converge after evidence resolves.
Reuse existing attempt/publication/CAS infrastructure, with provider-specific
evidence adapters where necessary; no bulk historical Cursor rename.
Frozen models and efforts never change during retry. Retain reviewer staged-only
discipline and independent identity. Only the explicitly started sequence may
create existing authorized non-final checkpoints; agents do not commit themselves.
Do not edit project ai_dev_loop.yaml, planning/review skills or integration/hook
installation code. Scope-local runtime rules/docs updates are permitted.

## Validation

Add mandatory automated coverage and run the concrete phase selection below with
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q <selected paths>.
Run uv run python -m ruff format --check ., uv run python -m ruff check .,
uv run python -m mypy src; changed docs require uv run mkdocs build --strict.
Report exact commands/results and map each contract ID to production callers and
independent expected test outcomes. Failed/affected tests are rerun after fixes;
broaden only for a concrete remaining risk. Mandatory missing behavior/coverage
is incomplete scope. Full suite/build belong to the final 27.7 integration gate,
not every review. Do not claim live validation from fake tests.

## Risks Or Recovery Notes

Use fake CLIs/process backends, deterministic clocks/synchronization, temporary
Git and XDG roots. Genuine historical fixtures remain unchanged. Test failures
at filesystem publication versus ledger commit and prove both safe uncertainty
and eventual convergence. Do not manually patch the ledger or release locks to
force success. Live scheduler/package/timer operations are forbidden in automated
validation. Current installed scheduler does not hot-load staged implementation.

## OpenQuestions

None. The user approved the shared retry command and timeout policy on 2026-10-14.
