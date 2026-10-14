# Phase 27.2 — Codex implementation and capacity continuation

## Goal

Deliver executable standalone Codex implementation, review and correction with
mandatory waiting_codex_capacity continuation; retain Cursor behavior.

## Non-Goals

No manual/automatic timeout-recovery parity, provider switching or sequence v2 yet.

## Scope

Provider adapter, fenced attempt dispatch/ingestion, role-bound identities,
capacity state/reducers/services, staging/review envelope and abort/restart behavior.

## Out of Scope

Sequence v2 authoring (27.3), blanket schema/path renames, new review-result schema,
project YAML, skills, dashboard, real timer/installation/model operations in tests.

## Implementation Plan

Prerequisite: accepted 27.1 configuration, bindings and provider-aware admission.
Verify its C-01..C-04 tests; missing capability blocks dependent work.

- C-05: Implement Codex execution adapter using shared process/attempt infrastructure,
  explicit frozen model/effort, workspace-write and noninteractive policy; use
  sanitized WSL Codex environment as current reviewer does. Write each attempt's
  JSONL/stderr/final/metadata privately before ingestion. Parse actual JSONL types,
  authenticate thread identity even on failure, bind once then resume exact ID.
  Route durable role through exact owned attempt backend, including systemd/fake.
- C-06: Successful implementation alone normalizes staging and builds the existing
  review target. Bootstrap independent B at first review; findings forward exact
  cursor_fix_prompt to implementer session. Preserve review budget/extension
  semantics. Generalize wrapper/provider labels without changing v1 result schema.
- C-07: Extend waiting_codex_capacity with versioned role-bound initial/correction
  continuation and authenticated evidence per shared contract. Reuse the actual
  CodexCapacityProbePort/app-server mechanism, classification and state transitions.
  Historical role-absent waits retain reviewer semantics. AVAILABLE schedules one
  retry for pending role; EXHAUSTED/UNAVAILABLE retains wait. Repeated quota waits
  again. No completed review/turn or staging/review effect from quota output.
- C-08: Persist wait intent, prompt hash, session binding, partial output and attempt
  termination before releasing a launch claim. Restart reconstructs pending turn
  from ledger/artifacts. Leases/CAS/wait generation deduplicate simultaneous ticks
  and late result ingestion. Unknown termination or uncertain bootstrap retains
  hold and blocks; resolved evidence converges to continuation. First-bootstrap
  no-session retry is limited to the shared contract's authenticated no-work case.
- C-09: Abort while running/waiting prevents retry and stops only owned attempt;
  preserve partial edits/artifacts. Non-quota error or timeout becomes classified
  blocked state. Guard unsupported Cursor/review recovery commands before mutation.
  review retry must not reinterpret implementer wait as reviewer wait. Existing
  reviewer capacity path keeps identity, patch, retry and budget semantics.

## Testing Criteria

Add test_phase27_2_codex_implementer.py and test_phase27_2_implementer_capacity.py
under tests/unit/scheduler, plus adapter tests. Exercise real submit/start/tick/
ingest/staging/review/correction callers with fake Codex/Cursor and temporary Git.
Mandatory scenarios: success with two distinct identities; unchanged correction
bytes; initial and correction quota after partial edits; identity captured on failed
first turn; proven no-session quota bootstrap; uncertain/conflicting identity blocked;
AVAILABLE resumes exact implementer, never reviewer; EXHAUSTED/UNAVAILABLE no launch;
repeated quota; scheduler restart during wait; deterministic competing ticks/replay
one dispatch; abort before/after probe/dispatch; uncertain termination holds; timeout
and generic failure; unsupported recovery commands mutation-free; no premature
staging/review/phase advance and no review-budget charge. Reuse regressions
 test_phase19_codex_capacity.py, test_phase20_5_reliable_codex_limit_detection.py,
 test_phase17_5_codex_corrections.py, test_phase17_6_abort.py and affected Cursor
workflow/retry tests. Check exact argv, absence of --last, artifacts permissions,
hashes and safe summary redaction. Use independent expected identities/effects.

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
