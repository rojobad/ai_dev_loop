# Phase 27.3 — Sequences and release acceptance

## Goal

Make both implementers selectable for new sequences and complete a focused,
documented release that can use Codex to implement later repository work.

## Non-Goals

No complete Codex recovery parity or deferred dashboard/timeouts phases.

## Scope

Versioned sequence manifest/frozen entries/materialization, provider-neutral
checkpoint handoff, capacity routing across sequence leaf, status/content inspection,
honest capabilities and operator examples for standalone and sequence use.

## Out of Scope

Project YAML, planning/review skills, broader recovery workflows, timer/linger,
production package installation, real model acceptance calls, external Git actions.

## Implementation Plan

Prerequisite: accepted 27.1 bindings and 27.2 standalone success/correction/capacity
contracts. Verify actual accepted production code and tests before sequence wiring.

- C-10: Add manifest v2 per shared contract; retain v1 Cursor default and frozen
  historical sequences. Freeze independent implementer fields/hashes per ordinal
  and materialize exactly that provider into each submitted leaf. Configure YAML
  optional implementer defaults with phase override precedence, keeping explicit
  reviewer fields. Reject null/conflicting provider fields. Prepare has no agents
  or Git effects; changing source config later does not change prepared entries.
- C-11: Mixed Cursor/Codex phases follow existing accepted non-final checkpoint
  commit/handoff and final-staged semantics. Each phase owns separate implementer
  and reviewer sessions. Initial/correction Codex capacity wait keeps current leaf,
  ordinal, reservation, prompts and partial edits; no checkpoint or successor until
  accepted review. Capacity AVAILABLE continues same leaf/turn. Restart and abort
  through actual sequence tick service preserve ownership and cancellation.
- C-12: CLI/status/history/timeline and integration projections expose provider,
  pending role/turn and actionable safe wait summary. Update schemas/API version
  only as required by existing compatibility policy. Route explicit protected
  output reads to provider-specific authenticated artifacts, preserving bounds,
  redaction and historical availability. Honest capability flags/default help must
  not advertise unsupported Codex implementer manual retries/recovery. Regression
  test old dashboard/integration callers against historical inputs without reads
  mutating the ledger. Do not implement Phase 26 dashboard.
- C-13: Update implemented AGENTS/rules and docs/config/CLI/operation examples.
  Include exact standalone and manifest v2 example selecting gpt-5.6-sol/xhigh
  implementation and gpt-6.1-sol/high reviewer, pending-role capacity wait behavior,
  process uncertainty and deferred recovery. Document that package installation
  after validation is explicit operator work; existing installed scheduler does
  not hot-load staged source changes. Maintain no review-skill/result changes.

## Testing Criteria

Add test_phase27_3_implementer_sequences.py and integration projection/content
contract tests. Fake complete Cursor->Codex and Codex->Cursor sequences through
prepare/start/tick/accept/checkpoint/materialize/final stages. Assert exact frozen
model/effort, distinct sessions, two non-final authorized checkpoints in a three
phase scenario and final staged output. Force initial and correction Codex quota
on current leaf; restart scheduler, report wait role, resume exact session on
AVAILABLE and eventually finish handoff. Abort during wait/uncertain process must
prevent next phase. Test v1 genuine frozen sequence fixtures and v2 schema/model
accepted/rejected inputs, omission/null, idempotent prepare and source config drift.
Run new tests plus affected test_phase20_1_sequence_prepare.py,
test_phase20_2_sequence_start.py, test_phase20_4_sequence_lifecycle.py,
test_phase20_4_sequence_abort.py, test_phase20_4_sequence_corrections.py,
test_phase19_codex_capacity.py and focused tests/unit/integration_api selection.
Use a documented fake-CLI smoke through real production standalone and sequence
entry points; no live model or workstation install. Release report lists all
C-01..C-13 evidence and separate pending full-suite/build/operator install gate.

## Required Context

Read phase-27-dual-implementer-contract.md, AGENTS.md, docs/referencia/cli.md,
docs/referencia/configuracion.md, docs/referencia/integration-api.md,
docs/operacion/estado-artefactos.md and docs/operacion/seguridad-privacidad.md.
Verify prerequisites against accepted production code before dependent changes.
Phase 24/25/26 are not prerequisites. Their plans are unimplemented history.

Current evidence: config.py; cli.py; runners/cursor.py, runners/codex.py,
fresh_codex_reviewer.py; scheduler/domain/state.py, events.py, reducer.py,
effects.py, sequence.py; scheduler/application/submission.py,
scheduler_preflight.py, cursor_workflow_service.py, codex_workflow_service.py,
codex_capacity_probe.py; scheduler/cursor_attempt_runner.py,
codex_attempt_runner.py; scheduler/infrastructure/sqlite_store.py and
protected_artifacts.py; integration_api/run_projection.py and sequence_projection.py.
Existing waiting_codex_capacity requires a bound reviewer and staged patch;
it cannot currently represent an implementation wait. Reuse its capacity probe,
evidence classification, leases and transactional machinery, not these
reviewer-only preconditions.

## Cursor Rules And Skills

Follow AGENTS.md and .cursor/rules/ai-dev-loop-governance.mdc,
ai-dev-loop-orchestrator-contracts.mdc, ai-dev-loop-state-and-schema-contracts.mdc,
ai-dev-loop-loop-and-resume-contracts.mdc, ai-dev-loop-abort-contracts.mdc,
ai-dev-loop-codex-review-contracts.mdc and ai-dev-loop-docs-acceptance-contracts.mdc.
Apply ai-dev-loop-global-integrations-contracts.mdc only if changing integration
surfaces. No .cursor/skills directory exists. Read
.agents/skills/create-cursor-plan/SKILL.md for reporting conventions and
.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md for docs evidence.
The independent reviewer uses review-staged-ai-dev-loop-execution; do not invoke
that skill as the implementer or modify planning/review skills.
The approved shared contract supersedes Cursor-only wording only for the new
Codex implementation path. Update affected AGENTS.md/rules to describe the
implemented slice; preserve all underlying identity, review and Git boundaries.

## Architecture Guardrails

Ledger, protected artifacts, exact identities, fenced effects and repository
reservation remain authoritative. CLI presents; domain validates/transitions;
application coordinates; provider adapters execute/parse. Do not introduce a
general provider framework, continuous baseline policing or Git-status emulation.
No destructive Git, external Git, cloud workers, timer/install changes or real
model calls in tests. Keep reviewer B independent and staged-only; workspace-write
capability does not authorize reviewer edits. Preserve existing authorized staging
and sequence checkpoint authority, with no implementation-agent commits.
Do not modify ai_dev_loop.yaml or planning/review skills: use CLI/manifest overrides.
Only phase-local operational docs and governance wording are in scope.

## Validation

Run mandatory contract tests and affected production-path regressions, using
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q with the concrete
selection below. Add meaningful tests for every new contract; existing tests
alone are insufficient. Then run uv run python -m ruff format --check .,
uv run python -m ruff check . and uv run python -m mypy src. Run
uv run mkdocs build --strict for changed docs. Report exact commands/results,
contract IDs, production entry points, tests, blockers and unfinished coverage.
After correction, rerun failed/affected checks only unless a concrete risk
requires broader coverage. Repository-wide pytest/build is a separate final
pipeline/integration gate after all three phases, owned by the operator or
pipeline; do not require it in every implementation/review round.

## Risks Or Recovery Notes

Use fake CLIs, fake process backends, deterministic capacity observations/clocks,
temporary repositories and XDG roots. Keep genuine existing historical fixtures
unchanged. Crash/replay/abort must retain ownership during uncertain effects and
converge once evidence resolves. Missing identity is never permission to guess
or replace a session. Unexpected prerequisites or safety conflicts block only
dependent work and must be reported with evidence.

## OpenQuestions

None. Public behavior and deferred features are decided in the shared contract.
