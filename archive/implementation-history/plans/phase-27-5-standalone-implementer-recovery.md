# Phase 27.5 — Standalone failure recovery

## Goal

Recover eligible failed Codex initial/correction turns through one authenticated
standalone successor while preserving the immutable blocked source.

## Non-Goals

No sequence force adoption until 27.6; no recovery from unbound bootstrap failures.

## Scope

Force command eligibility, immutable records/publication, inherited review budget,
exact-session successor continuation, restart/abort and safe diagnostics.

## Out of Scope

Sequence replacement, broad failure auto-retries, provider/model switching.

## Implementation Plan

Prerequisite: accepted R-01..R-03. Verify CLI timeout/capacity continuation first.
- R-04: implementer-retry --force accepts only authenticated, terminated standalone
  generic implementation/correction failures with a bound Codex session. Reject
  sequences, abort/integrity/identity uncertainty and capacity waits mutation-free.
  Freeze failure/source/role/turn evidence in versioned record; replay returns
  existing successor. Preserve source artifacts/snapshot and original config.
- R-05: Publish successor via durable intent and protected artifacts before
  dispatch. Transfer/reacquire reservation through existing authority; check
  partial work at recovery boundary without clean-baseline policing. Initial
  recovery preserves original prompt; correction recovery carries exact raw fix,
  reviewer identity, reviewed patch evidence, reviews completed and extended ceiling.
  Execute deterministic envelope in same implementer session, never another B.
- R-06: Crash between artifacts, record, successor and reservation converges to
  one relation; pending publication cannot execute. Abort fences publication/
  dispatch and preserves work; uncertain termination retains ownership. Repeated
  failure of successor supports another explicitly authorized recovery, with
  bounded authenticated ancestry traversal and no recursive fallback workaround.

## Testing Criteria

Add test_phase27_5_implementer_failure_recovery.py under tests/unit/scheduler.
Drive real --force/publication/tick for initial/correction, exact prompt bytes,
existing B/counters/extensions, duplicate/concurrent commands one successor,
failed successor recovery, all publication crash boundaries and eventual restart
convergence. Test rejected sequence/capacity/aborted/corrupt/unbound inputs with
ledger/artifact nonmutation witnesses. Fake CLIs and temporary repos only.
Run affected phase27_2/27_4 tests and test_phase23_2_initial_recovery.py,
test_phase23_3_budget_ancestry.py, test_phase23_1_cursor_recovery_evidence.py.

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
