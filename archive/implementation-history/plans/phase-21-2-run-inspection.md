# Phase 21.2 — Run Inspection and Frozen Inputs

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.1`.

## Goals

Make all central-scheduler runs discoverable and inspectable through the public
CLI, including standalone/sequence bindings, attempts, history, and exact frozen
plan/initial prompt. Supply stable attempt keys for later review/output readers.

## Non-Goals

Do not inspect review bodies or process streams, implement sequence navigation,
or change scheduler lifecycle. Do not import retired pre-scheduler run storage.

## Scope

Run queries/projections, bounded collection/byte readers, public response
schemas, read-only artifact helpers, CLI wiring, tests, and updates to
`docs/referencia/integration-api.md`, `docs/referencia/cli.md`, and
`docs/operacion/seguridad-privacidad.md` for explicit sensitive inspection.

## Out of Scope

- Remote monorepo, Bridge runtime/registration/WSS, web/backend/database/cache,
  remote operation execution, deployment, and machine/user management.
- `ai_dev_loop.yaml`, `.agents/skills/`, `.cursor/rules/`, installed global skills,
  hooks/session bridges, real systemd/lingering setup, package reinstallation.
- Scheduler reducers, Git checkpoint authority, recovery policy, automatic
  retries, finalization/commit/push/PR behavior, and retired legacy run import.
  Narrow integration query helpers and explicitly named evidence/probe changes
  below are allowed; do not refactor adjacent runtime behavior.

## Required Context

Planning baseline inspected: commit `d1d8960` (2026-09-18), after Phase 20.9 and
the stricter planning/review guidance. Read current source/tests again once at
admission; do not require this exact HEAD or add continuous baseline gates.
Read `docs/referencia/cli.md`, `docs/operacion/seguridad-privacidad.md`,
`docs/operacion/estado-artefactos.md`, `docs/operacion/observabilidad.md`, and
`pyproject.toml`. Current code/tests establish prerequisites; archived plans
alone do not. The source discussion is consolidated in
`archive/implementation-history/plans/phase-21-local-integration-overview.md`;
this execution plan is self-contained and requires no Downloads file.

Prerequisite: independently accepted Phase 21.1 implementation and its 1.0
fixtures. It is planned, not present at the planning baseline. Before dependent
edits verify `integration info`, error envelopes, schema loading, and tests;
if missing, stop dependent work instead of rebuilding that phase here.

- `scheduler/application/status.py`, `history.py`, `timeline.py`, `contracts.py`,
  `safe_actions.py`: validated states, redacted history, observed timestamps.
- `scheduler/infrastructure/sqlite_store.py`: `list_runs`, `get_attempt_by_id`,
  `list_attempt_timeline_rows` already select stable attempt IDs internally.
- `scheduler/application/submission.py`: `plan/plan.md`,
  `prompts/cursor-initial.txt`, and frozen PlanPromptBinding hashes.
- `scheduler/infrastructure/paths.py` pure roots versus ensure_* paths;
  `protected_artifacts.py` mutating read entry points.
- `tests/unit/scheduler/test_phase18_timeline_and_provenance.py`,
  `test_status_readonly.py`, `test_schema_readonly_historical.py`, and
  `test_protected_artifacts.py` establish reusable behavior.

## Cursor Rules And Skills

Read `AGENTS.md` and these repository-local Cursor rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`

There is no `.cursor/skills/` directory. Existing rules are sufficient; do not
create a new governance framework for this feature. This plan follows
`.agents/skills/create-cursor-plan/SKILL.md`; independent review follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. Read their contract
coverage and correction-reporting requirements, but do not modify those skills
or invoke the handoff/controller workflow as part of implementation.

`AGENTS.md`, the approved phase, and current scheduler code/tests govern the
supported flow. Legacy rule references to Phase 4/10 session setup, continuous
baseline gates, or Phase 8-only documentation restrictions do not authorize
restoring retired workflows. Preserve current fresh reviewer B bootstrap and
exact subsequent resume. New inspection commands intentionally expose selected
sensitive artifacts; existing default status/history/log privacy stays intact.

## Architecture Guardrails

- The local scheduler ledger, frozen definitions, validated lineage, and protected
  artifacts remain authoritative. Integration DTOs are public projections, never
  aliases for internal Pydantic models or database rows.
- Add the singular `integration` CLI namespace. Preserve the existing plural
  `integrations` installation namespace and all existing scheduler CLI output.
  Put presentation under `commands/integration.py`, public DTOs/application
  readers under a focused `integration_api/` package, and storage queries/path
  handling behind the existing infrastructure boundary. No HTTP server or daemon.
- Integration reads use `SqliteSchedulerStore.open_readonly` and read
  transactions; they never bootstrap/migrate a database, repair state, tick,
  acquire a scheduler write reservation, or execute Git. Read-only means no
  application writes/chmod/mkdir in ledger or artifact paths; ordinary OS access
  metadata and SQLite-managed read coordination are not a new product gate.
- Existing `ProtectedArtifactStore.run_root`, `sequence_root`, and readers that
  call them may create/chmod directories. Do not use those mutating entry points
  from integration reads. Reuse safe lexical path and hash validation through
  pure read helpers; resolve only registered IDs and allowlisted artifact kinds,
  never arbitrary client paths or shell strings.
- Full run/sequence/attempt IDs are public resource keys. They are not full agent
  session IDs. Do not add surrogate ID storage. Summaries exclude prompts, review
  bodies, output, raw argv, environments, credentials, and provider prose.
  Explicit content reads return the captured artifact; do not log their bodies,
  reconstruct missing history, or promise secret removal from arbitrary content.
- No new roles, local tokens, remote authentication, caches, telemetry database,
  continuous baseline checks, arbitrary file browser, or multi-account registry.
  The local process runs under the operator's OS user; remote authentication is
  the future Bridge/control-plane responsibility.
- Same API major is compatible, different major requires UPDATE_REQUIRED at the
  consumer and no further requests. Minor changes are additive. Unknown fields
  are ignored by consumers; capabilities only enable additive features. Never
  add per-minor adapters or treat minor differences as incompatibility.
- Preserve repository reservations, identities, abort/retry semantics, and the
  final staged boundary. Phase 21 authorizes no additional Git effects. Never
  execute agent output. Do not stage/commit/push or run real scheduler controls
  during this implementation; an independently authorized outer scheduler owns
  any staging/checkpoints in its existing workflow.
- Use fake CLIs, temporary native-Linux XDG roots, disposable repositories, and
  deterministic clocks/synchronization. No real models, provider accounts,
  workstation timer/lingering, credentials, live installation, or network in tests.
  Temporary isolated wheel installs for package validation are allowed.
- Each additive phase updates current-version info/schema test expectations,
  while retaining previous producer fixtures and consumer compatibility tests
  unchanged. Historical fixture preservation does not freeze the live version
  or capability values asserted by current CLI tests.

## Implementation Plan

1. Add these production commands (all retain Phase 21.1 JSON/error conventions):

   ```text
   integration runs list [--kind all|standalone|sequence] [--offset N --limit N]
   integration run inspect RUN_ID
   integration run attempts RUN_ID [--offset N --limit N]
   integration run timeline RUN_ID [--offset N --limit N]
   integration run history RUN_ID [--offset N --limit N]
   integration run plan RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
   integration run initial-prompt RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
   ```

2. Runs list defaults to all, ordered by submittedAt descending then runId
   ascending; use bounded DB selection with kind filter before LIMIT. Reuse
   validated projection logic; no CLI-to-CLI subprocess. Unknown state labels
   remain labels, not inferred lifecycle behavior. Missing DB returns an empty
   list; an unknown/missing exact run returns NOT_FOUND.
3. `inspect` emits runId, projectName, repositoryRoot (target worktree location,
   not managed artifact roots), state, submittedAt, updatedAt, review budget
   (`completed,max,submittedMax`), nullable `sequenceBinding` with full
   sequenceId/ordinal/phaseCount, residualRisk, blockReason, cursorWaitUntil,
   safeNextAction, and attemptCount. Unknown risk is null, not false.
   SafeNextAction has kind, runId, nullable sequenceId and nullable waitUntil
   from already-projected state. The current internal
   `command` string is not transported or parsed as shell instructions; do not
   invent a public recovery permission from that display text.
   Do not embed unbounded attempt arrays or read artifact bodies in summaries.
4. Attempts and timeline retain full `attemptId`, component, effectKind,
   iteration, phaseAttempt, status, createdAt, launchRequestedAt, completedAt,
   observedDurationSeconds. Join effect metadata through the existing dispatch
   relation; no client dispatch IDs/argv/PIDs. Order by coalesced launch/creation,
   creation, attemptId ascending. Derive phaseAttempt across the complete
   iteration/component partition before pagination, preserving current timeline
   grouping semantics. Running durations remain null until completed; do not
   fabricate a start time. History separately projects ledger event sequence,
   kind, timestamp, and existing safe detail in ascending sequence order; raw
   payloads are never returned. This distinguishes process duration from state
   transitions/capacity waits. Existing scheduler timeline stays unchanged.
5. Read plan and initial prompt using the run's frozen binding, expected full
   hash, existing artifact bounds, and pure path readers. Never fall back to a
   repository file, current worktree, other run, or legacy storage. Verify the
   complete immutable artifact by bounded streaming before returning its byte
   slice; no full-size response or whole-file allocation is required. Return
   Phase 21.1 chunk metadata plus runId, artifactKind, sourceRepositoryPath and
   verified sha256. At EOF return an empty available chunk, nextOffset:null;
   offset beyond available size is INVALID_ARGUMENT.
6. Missing bound frozen plan/prompt, mismatched hash, or invalid registered
   path is DATA_INTEGRITY, not historical optional absence. OS permission/I/O
   failure is IO_ERROR. Reject traversal/symlink escapes from the registered
   root using existing safety semantics without chmod or repair. A reader must
   not call ensure_* through a helper. Do not claim protection against every
   hostile same-user filesystem race; cover normal supported path confinement.
7. Preserve supported old scheduler DB reads without migration. If optional
   attempt/history tables truly did not exist in a supported schema, return
   UNSUPPORTED for that operation; never fabricate empty evidence or require a
   current table on an older schema. Existing old run summaries still work.
8. Set API 1.1 and `runs:true` only when all commands above work. Other
   capabilities stay false. Freeze `v1_1` examples; keep `v1_0` fixtures intact.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | Run list/inspect CLI → validated read service | Standalone/sequence runs, full binding, filters and empty store behave correctly |
| C-02 | Attempts/timeline/history CLI → bounded ledger queries | Stable attempt IDs/ordinals, actual timestamps, safe event history and pagination |
| C-03 | Plan/initial-prompt CLI → frozen binding + pure verified reader | Exact historical bytes even after repository files change |
| C-04 | Every read and failure path | No scheduler/artifact application writes, no secret bodies in summaries/errors |
| C-05 | Historical DB and public 1.0 consumer path | No migrations or latest-table assumptions; additive 1.1 compatibility |

## Testing Criteria

Add `tests/unit/integration_api/test_run_projection.py`,
`test_artifact_reader.py`, and `tests/integration/test_phase21_2_run_inspection.py`.

| Contract | Level and acceptance scenario |
|---|---|
| C-01 | CLI/integration on submitted standalone and sequence-bound runs; literal IDs, budget and expected filter ordering; unknown run and empty XDG |
| C-02 | Multiple attempts in one iteration/component, another iteration, waits, active/failed/completed attempts; page boundary does not renumber attempts; no raw event body in history |
| C-03 | Submit through real submission service, modify/delete source plan/prompt, reconstruct original bytes through CLI chunks; hash mismatch, missing file and wrong-root symlink fail safely |
| C-04 | Inspect with scheduler locks held; snapshot ledger rows/version, artifact tree and permissions before/after success/error; sentinel sensitive text absent in inspect/list/history/stderr |
| C-05 | Reuse actual prior migrations/fixtures from historical read tests, not relabeled modern DBs; unchanged old API fixtures parsed by the pinned consumer |

Include limit/offset bounds, empty artifact at reader level, EOF, non-ASCII
crossing chunk boundaries, and missing optional timestamps. Test the production
root CLI; a direct reader unit test alone does not establish command wiring.
For large-page behavior assert bounded query/projection work, not timing thresholds.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api tests/integration/test_phase21_1_integration_contract.py tests/integration/test_phase21_2_run_inspection.py tests/unit/scheduler/test_status_readonly.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase18_timeline_and_provenance.py
```

## Validation

Run the phase-specific tests above, then the repository checks once the final
implementation is ready. Use the documented `uv` workflow, not Docker or system
Python changes. Report each exact command and exit/result as executed, failed,
or unexecuted. Do not claim a full rerun from a focused correction rerun.

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

Completion report: for every contract ID, list production entry point/callers,
implementation files, acceptance test names, and actual validation results.
Mandatory missing behavior or coverage is incomplete scope, not residual risk.
On corrections, retain reviewer finding IDs and report fixed/disputed/blocked
with evidence; explain the invariant missed by an earlier incomplete fix.
Do not impose unrelated hardening or exhaustive unsupported Git cases as new
acceptance requirements. Do not stage generated build/site output.

## Risks Or Recovery Notes

No new persisted state or migration. Reads may see a later snapshot on a later
request; offset pagination is intentionally simple. Missing mandatory frozen
evidence is different from an optional artifact never recorded by old code.
Reuse safe-action/risk logic rather than duplicating a state machine. Public
queries must not invoke reconciliation to make data appear consistent.

## OpenQuestions

None.
