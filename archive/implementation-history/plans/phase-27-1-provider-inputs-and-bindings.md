# Phase 27.1 — Provider inputs and durable bindings

## Goal

Introduce a minimal role-aware implementer configuration and durable bindings
without changing existing Cursor execution or advertising executable Codex
implementation before Phase 27.2.

## Non-Goals

No Codex implementation subprocess or capacity runtime yet; no full provider abstraction.

## Scope

Config/CLI submission, versioned submitted contexts and effective input hashes,
provider-selected admission/probes, historical readers and honest capability gating.

## Out of Scope

Sequence manifest v2 execution (27.3), capacity dispatch (27.2), recovery expansion,
project YAML, planning/review skills, dashboard and workstation operations.

## Implementation Plan

- C-01: Add the shared contract's config/CLI forms. Freeze resolved provider,
  command/model/effort/timeout independently from reviewer at submit. Include
  selection in idempotency/input hashes. Same payload replays; changed provider
  cannot accidentally reuse a Cursor run. Canonical new persisted representation
  has explicit provider and validated settings; preserve old versions byte-for-byte.
- C-02: Version typed bindings/context/checkpoint schemas only where needed;
  historical absent metadata means Cursor. Keep reviewer binding separate. Define
  optional identity before execution with durable purpose, not guessed values.
  Old serialized schemas remain readable without migration on inspection.
- C-03: Make admission/probes provider aware through existing preflight port.
  Codex implementation must not require Cursor binary/auth/model availability.
  Cursor still checks Cursor plus reviewer. Validate frozen exact model/effort
  using existing Codex compatibility evidence; capability unavailable/ambiguous
  fails honestly, never substitutes another model. Phase-local dispatch guard
  blocks Codex execution as not implemented until 27.2 wires it.
- C-04: Preserve all existing Cursor flags, default resolution, reviewer bootstrap,
  clean-baseline admission and idempotent submit. Update CLI/docs/governance to
  describe only the input slice and execution guard; do not claim usable Codex loop.

## Testing Criteria

Add tests/unit/scheduler/test_phase27_1_implementer_inputs.py and config/CLI/schema
coverage: valid Cursor defaults/Codex explicit fields; omissions vs null; conflicting
flags; reviewer independence; frozen/hash replay; malformed providers/efforts/timeouts;
old genuine snapshots remain readable. Exercise scheduler submit/preflight production
callers with fake CLIs and no Cursor executable for Codex selection. Assert no agent
subprocess before start and no Codex implementation dispatch under the phase guard.
Run focused new tests plus tests/unit/test_config.py (if present), existing submission,
preflight and schema-readonly historical tests found in the checkout.

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
