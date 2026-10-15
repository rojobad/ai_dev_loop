# Phase 27.7 — Integration acceptance and operations

## Goal

Close automated integration coverage and document the exact operator acceptance
procedure for all supported Codex recovery/capacity guarantees.

## Non-Goals

No new recovery feature or real workstation change during automated execution.

## Scope

Cross-feature production smoke, repository-wide final gate, packaging/docs checks,
contract evidence and separately executable operator validation instructions.

## Out of Scope

Live model calls, installation/update/timer/linger changes or real run control by
this phase. These require separate operator authorization after code acceptance.

## Implementation Plan

Prerequisite: accepted C-01..C-13 and R-01..R-09, rechecked against actual code.
- R-10: Add deterministic fake-CLI end-to-end smoke covering standalone and mixed
  sequences: timeout -> retry -> quota wait -> capacity resume -> generic failure
  -> force recovery -> correction -> accepted completion. Include restart/abort,
  exact identities and budgets/ordinal/checkpoint observations. Use real CLI/
  service callers and isolated state, never test-supplied missing production guards.
- R-11: Run final repository-wide pytest once after focused smoke, package build,
  lint/types and strict docs; report failures/skips distinctly and fix confirmed
  affected regressions within scope. Do not infer full success from isolated rerun.
  Keep provenance for inherited versus newly run evidence. Required tests left
  unexecuted mean incomplete automated acceptance, not an optional residual risk.
- R-12: Produce docs/operacion/codex-implementer-recovery.md and archived release
  evidence mapping all contracts to entry points/tests/results. Provide exact
  retry/capacity/abort diagnostics and deferred exclusions. Provide a separate
  operator checklist: clean test repo, explicit model/effort, local package and
  timer refresh per existing guide, controlled real timeout/failure, capacity
  observation when naturally available, restart and final outputs. Never exhaust
  quota intentionally or fabricate capacity. Mark every unexecuted live scenario
  pending; code acceptance does not claim live operational acceptance.

## Testing Criteria

Add tests/integration/test_phase27_7_recovery_acceptance.py using fake CLIs, owned
backend and temporary XDG/Git. Assert independently expected session continuity,
exact prompts, holds, lineage, completed reviews and final staged content. Run
this smoke plus affected phase27_4..27_6 tests, then
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
and uv run python -m build, followed by the static/docs checks in Validation.
No real services/agents/cleanup/install side effects. If environment blocks a
mandatory check, report the concrete blocker rather than relabeling acceptance.

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
