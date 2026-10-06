# Phase 23.1 — Read-only Cursor recovery eligibility and evidence

## Goals

Provide a production read-only diagnostic for manual Cursor recovery:
`ai_dev_loop scheduler cursor-retry RUN_ID --check --output json`. Authenticate
the failed turn and explain supported evidence or blockers before any successor
implementation exists. This first run makes recovery analysis independently
reviewable; it creates no new recovery authority.

## Non-Goals

- Recover a run, reserve a worktree, resume a chat or publish a successor.
- Classify quota from prose or infer renewed capacity.
- Guarantee that an inspected source remains eligible after concurrent change.

## Scope

Read-only CLI routing, typed inspection/evidence DTOs, authenticated causal
analysis of initial/correction failures, safe output, unit/integration regression
tests and current documentation. Distinguish evidence sufficiency from whether
the currently implemented recovery scope supports mutation.

## Out of Scope

No `--force` implementation, ledger migration, new failure writes, successor or
sequence replacement model, dispatch changes, automatic retries, input mutation,
Git mutation, installed services, live runs, hub code or provider calls in tests.
Do not edit `ai_dev_loop.yaml`, planning/review skills or the stopped worktree.

## Required Context

Read [the replanning overview](phase-23-replanned-overview.md) and `AGENTS.md`.
Start from accepted `main` at
`b516abdc7aa6582e17ad88a6c4ee94c3896499a9`; inspect the actual checkout before work.
The aborted combined Phase 23 patch is reference material, not a prerequisite.

Existing production prerequisites, relative to `src/ai_dev_loop/`:

- `cli.py`, `scheduler/application/cursor_timeout_retry.py`: existing timeout
  command and binding checks; leave its no-flag path intact.
- `scheduler/application/cursor_workflow_service.py`, `cursor_evidence.py` and
  `attempt_service.py`: invocation/outcome authentication, original/correction
  envelopes and failure ingestion.
- `scheduler/domain/events.py`, `state.py`, `reducer.py`: ordered completion
  events include attempt/dispatch IDs. `CursorTurnBlockedEvent` and the blocked
  snapshot do not supply the full failed workflow checkpoint or failed attempt
  ID. Reconstruction must prove causality from authenticated ordered evidence.
- `scheduler/infrastructure/sqlite_store.py`: `open_readonly`, `begin_read`,
  validated snapshots/events and fenced attempt/effect records. Inspection must
  neither bootstrap nor migrate a database or create its parent directories.
- `scheduler/application/codex_evidence.py`, `review_budget.py`: authenticated B,
  completed-review evidence and ordered extension folding for correction analysis.
- `scheduler/application/review_recovery.py`: useful evidence helpers; its
  completed-stage/reviewer eligibility is not eligibility for Cursor failure.

Read `docs/referencia/cli.md`, `docs/operacion/estado-artefactos.md`,
`prepare-start-resume-abort.md`, `troubleshooting.md` and existing timeout and
correction evidence tests. No future recovery writer is required in this phase.

## Cursor Rules And Skills

Follow `AGENTS.md` and these files under `.cursor/rules/`:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`,
`ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc`, and
`ai-dev-loop-global-integrations-contracts.mdc`.
There is no `.cursor/skills/`. The authoring skill is
`.agents/skills/create-cursor-plan/SKILL.md`; the separate reviewer follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. Do not edit either.

## Architecture Guardrails

- Separate CLI presentation from pure validation/DTOs and ledger/artifact adapters.
- Use the ledger and protected artifacts. Do not query legacy state, provider
  transcripts, guessed sessions or the latest file by mtime.
- Prove the unique decisive failed turn via authenticated journal sequence,
  completion event, matching dispatch/unit/nonce, before-block checkpoint and
  outcome. Timestamp ordering or a generic latest-attempt helper is insufficient.
- If retained historical evidence cannot establish unique causality, report
  insufficient evidence. Do not backfill a failure link or invent certainty.
- No single fixed event page may silently omit needed history. Paginate existing
  read APIs or return an explicit incomplete-evidence blocker.
- Protect raw prompts, fixes, reviews, full agent IDs, output and credentials.
  Read-only checks must not acquire/release reservations or probe agent CLIs.

## Implementation Plan

**E-01 — Read-only entry point and bounded receipt.** Add `--check` routing to
`scheduler cursor-retry`; no flag continues using the existing timeout service.
Inspection uses read-only ledger opening. Define a v1 typed JSON receipt and
schema with required `schema_version`, `run_id`, `evidence_status`
(`authenticated`, `insufficient`, `ineligible`, `corrupt`), `recovery_supported`,
`turn_kind` (`initial`, `correction` or null), `reason_code` and bounded
`safe_summary`. Optional information uses explicitly required nullable
`sequence_id` and `ordinal` fields; never put unknown facts in synthetic values.
Canonical versions/ordinals are strict JSON integers; reject booleans/numeric
strings. Follow existing CLI not-found/error handling; a successfully inspected
ineligible source returns a receipt, not a fabricated process failure.
In this phase `recovery_supported=false`: mutation is not implemented. Help and
Spanish docs explain that authenticated evidence does not itself authorize retry.

**E-02 — Authenticate the decisive initial turn.** Implement a reusable analyzer
that accepts a validated blocked source and read ports. Resolve its exact
pre-failure admitted checkpoint, chat binding, initial turn/iteration and failed
invocation prompt. Authenticate contemporaneous nonzero terminal outcome and
the matching completion/ingestion transition into `cursor_failure`, independent
of timestamps. Verify repository identity and frozen input/artifact bindings
at existing evidence boundaries; expected dirty partial work is allowed. Reject
active/uncertain processes, abort/checkpoint holds, chat-creation or preflight
failures, wrong block kind, successful/timed-out outcomes governed by other
policies, missing/corrupt evidence and ownership conflicts. The analyzer produces
an internal typed evidence bundle, not externally supplied authorization.

**E-03 — Correction and sequence inspection without adoption.** For a correction,
resolve the exact failed execution envelope, raw B-authored fix, reviewer binding,
latest staged/review evidence, logical iteration, completed-review count and
effective ceiling using authenticated journal and existing helpers. If B should
exist but cannot be proved, mark insufficient/corrupt. Inspect sequence binding
and current leaf by native sequence IDs, ordinal and entry hash. This phase can
report evidence about either turn type and a sequence source; it cannot recover
them. Historical review-recovery ancestry uses existing authenticated relations;
unknown/future Cursor recovery relations remain explicitly unsupported.

**E-04 — Honest boundaries and future reuse.** Keep analyzer inputs/results
independent of writable transactions; later phases must call it again at their
authorization boundary and authenticate relation-specific evidence. Document
reason codes and current limits; preserve no-flag output and timeout behavior.
Do not build dormant successor creation or change failure ingestion in this run.

## Testing Criteria

Mandatory unit, schema and real-CLI integration tests, suggested files
`tests/unit/scheduler/test_phase23_1_cursor_recovery_evidence.py`,
`tests/integration/test_phase23_1_cursor_recovery_check.py`, and genuinely captured
baseline evidence under `tests/fixtures/phase23_1_historical/`.

| Contract | Independent observable acceptance |
| --- | --- |
| E-01 | Real CLI `--check` succeeds on an inspected source and refuses unknown/missing input appropriately. Compare ledger state/events/effects/reservations and private artifact inventory before/after repeated checks: unchanged, no migration and no child process. Existing no-flag tests pass. Validate required/null/numeric output forms and redaction. |
| E-02 | Use actual fake-agent ingestion to create an initial blocked failure. Check resolves its exact completion/dispatch/invocation and prompt. Include two failures with equal/reversed timestamps and an unrelated later attempt: correct unique cause or explicit insufficient evidence; never wrong eligibility. Genuine historical ambiguous/incomplete cases reject without changes. |
| E-03 | Actual correction with bound fake B proves exact fix/envelope, completed reviews and extension ceiling; corruption/missing prior B is rejected. Sequence check proves native ordinal/current leaf without adoption. Reject stale leaf, abort, uncertain process and conflicting ownership. |
| E-04 | Authenticated evidence still reports mutation unsupported. No successor/effect or hidden recovery write exists. Paginated history test puts required proof beyond the first page and proves complete analysis or explicit incomplete-history rejection. |

Use fake agent/Codex and isolated native-WSL HOME/XDG/repos with injected time.
Capture fixtures from the baseline writers with their commit/provenance, not from
new analyzer-generated JSON. Never have the test create the missing causal link.

## Validation

```bash
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_1*.py tests/integration/test_phase23_1*.py
.venv/bin/python -m pytest tests/integration/test_cursor_timeout_retry.py tests/unit/scheduler/test_cursor_evidence_corrections.py tests/unit/scheduler/test_review_budget_extend.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Use isolated Python 3.11+ `.venv`, with `uv` for setup; do not replace system
Python. Verify local CLI help. Map E-01–E-04 to production callers/tests and exact
executed, failed and unexecuted commands. Mandatory missing coverage is incomplete
scope. No live run inspection/mutation is required for automated validation.

## Risks Or Recovery Notes

The baseline blocked event's limited fields make causal reconstruction the
principal risk. Honest insufficient-evidence results are required for ambiguous
history; the canonical simple initial-failure and correction fixtures must still
produce authenticated evidence. If neither can be proven from accepted contracts,
report that prerequisite conflict before designing dependent mutation.
Planning does not authorize executing this plan. Existing automatic policies
continue; this CLI path provides no timer, reservation or external side effect.

## OpenQuestions

None within this scope. Report a newly discovered causal-evidence conflict and
stop dependent work rather than inventing a historical link.
