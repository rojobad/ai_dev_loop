# Phase 21.3 — Sequence Inspection and Multi-Run Lineage

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.2`.

## Goals

Expose complete sequence supervision: prepared phases, all materialized runs,
current/accepted lineage, checkpoints, residual risks, frozen inputs and the
existing final report. Represent the already-implemented Phase 20.9 lifecycle.

## Non-Goals

Do not add start/abort/retry/relaunch/finalize operations, modify run/sequence
reducers, or create a second authority for phase completion.

## Scope

Sequence listing/detail/phase-run collections and read-only report/input
projections; public schemas/fixtures; integration CLI and tests; updates to
integration API/CLI and state-artifact product documentation.

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

Prerequisites: independently accepted Phase 21.1–21.2 (planned at this baseline),
plus existing Phase 20.9. Verify earlier integration commands and fixtures once
before dependent work. Stop on absence/conflict; do not substitute an old plan.

- `scheduler/domain/sequence.py`, `sequence_run_lineage.py` and
  `infrastructure/sequence_run_lineage_store.py`: full phase attempts with
  generation, source_run_id, current/accepted run IDs, and materialization times.
- `scheduler/application/sequence_status.py`, `sequence_lineage_projection.py`,
  `sequence_report.py`: authoritative projection logic; existing shortened
  fields alone are insufficient as public navigation keys.
- `scheduler/application/sequence_prepare.py`/`sequence_materializer.py`:
  frozen phase bindings and the distinction planned ID versus scheduler row.
- `tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py`,
  `test_phase20_4_sequence_status.py`, and
  `tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py` prove
  actual multi-run phases, current/accepted runs and finalization.

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

1. Add the following public commands with the foundation conventions:

   ```text
   integration sequences list [--offset N --limit N]
   integration sequence inspect SEQUENCE_ID
   integration sequence phase-runs SEQUENCE_ID --ordinal N [--offset N --limit N]
   integration sequence phase-plan SEQUENCE_ID --ordinal N [--offset N --limit N]
   integration sequence phase-prompt SEQUENCE_ID --ordinal N [--offset N --limit N]
   integration sequence report SEQUENCE_ID [--offset N --limit N]
   ```

   phase-plan/phase-prompt/report offsets and limits are bytes; phase-runs/list
   use collection units. The typed operation fixes the meaning; no raw paths.
2. List sequences from the authoritative sequence table including prepared
   sequences without runs, ordered preparedAt descending, sequenceId ascending,
   bounded before projection. Missing database is []; existing historical DB
   without sequence support returns UNSUPPORTED, never migrates.
3. Inspect emits sequenceId/name/projectName/repositoryRoot, state,
   preparedAt/startedAt/updatedAt/finalizedAt, currentPhaseOrdinal/currentRunId,
   currentRunState, aggregateCounts, residualRisk/residualRiskOrdinals,
   blockReason, safeNextAction, report availability, and phases ordered ordinal.
   SafeNextAction retains the shared kind/runId/sequenceId/waitUntil fields;
   sequenceId identifies this sequence, runId is null when no current run exists.
   Phase entries include ordinal/name, initialPlannedRunId, materialized flag,
   currentRunId/acceptedRunId nullable, cancelled, acceptedOutcome, residualRisk,
   checkpoint summary and frozen input availability. Reuse authoritative status
   semantics; do not invent a conflicting per-phase lifecycle enum.
4. Each phase has a `runs` array from day one, with `runsHasMore` and
   `runsNextOffset`. Inline at most 100 runs per phase (existing maximum 32
   phases bounds this overview); `phase-runs` provides the full paged history.
   Materialized run elements include full runId, generation, sourceRunId,
   attemptKind, materializedAt/resolvedAt, current state and terminalOutcome.
   Use generation ascending; counts cover the entire validated lineage, not
   the page. A planned run ID with no scheduler row produces `runs:[]`, no
   materialized run, and null current/accepted IDs. Retry successors already
   exist today and must be visible, including superseded runs and more than
   one replacement generation. Never infer binding from naming or timestamps.
5. Use authoritative full lineage readers inside ai_dev_loop, reusing domain
   validation and historical projection from state where that is the existing
   supported behavior. Don't expand shortened existing CLI summaries by string
   matching. Corrupt lineage is DATA_INTEGRITY, not an invented singleton.
   A cancelled unmaterialized entry still retains its frozen inputs.
6. Expose frozen phase plan AND initial prompt before and after materialization
   through the Phase 21.2 pure verified reader. Sequence phase inputs refer to
   the prepared definition; run inputs refer to that run's actual binding.
   Never replace one by the other to conceal a mismatch.
7. Report reads only the already-published `reports/completion-v1.json`, with
   schema/identity validation and existing integrity evidence where available.
   Do not call report builders/publishers or authenticate via mutating roots
   just to render. Before completion return an unavailable chunk with
   reason `not_yet_produced`; awaiting_finalization with missing publication
   returns `publication_pending` (existing scheduler reconciliation owns repair).
   Malformed or mismatched published data is DATA_INTEGRITY. Serve the validated
   stored bytes with a computed content digest; label integrity as
   `schema_validated` unless an authoritative expected digest is actually bound.
   Do not present a computed hash as independent authentication. Historical
   report versions supported by current code remain readable without rewrite.
8. Checkpoint timestamps/commit references come from stored facts; no Git read
   to fill gaps. Preserve awaiting_finalization and manual final action exactly;
   report retrieval never finalizes or creates a commit. Set API 1.2,
   `sequences:true`; retain earlier capabilities and `v1_0`/`v1_1` fixtures.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | Sequence list/inspect CLI → validated storage/status | Prepared, active, blocked, aborted and awaiting_finalization are discoverable |
| C-02 | Inspect/phase-runs → authenticated lineage projection | Zero/one/multiple runs, full source/current/accepted IDs, bounded complete navigation |
| C-03 | Phase-plan/phase-prompt → frozen definition readers | Exact inputs available before materialization and after cancellation |
| C-04 | Report CLI → existing report reader | Full stored report or explicit publication absence; never publication/repair |
| C-05 | All sequence summaries | Correct counts, residual risk and checkpoints with no new lifecycle/Git effects |
| C-06 | Historical/protocol compatibility | Existing pre-lineage data is honestly projected; API additions preserve old consumers |

## Testing Criteria

Add `tests/unit/integration_api/test_sequence_projection.py` and
`tests/integration/test_phase21_3_sequence_inspection.py`.

| Contract | Level and production-path acceptance |
|---|---|
| C-01 | CLI over real prepare/start/test scheduler services: prepared sequence appears without scheduler runs; each existing sequence terminal/wait state projects correctly |
| C-02 | Reuse Phase 20.9 lifecycle fixture to create real retry successors (including a successor of a successor); assert literal ordered run IDs/current/accepted leaf; query page boundaries |
| C-03 | Change/delete original repository inputs after prepare; phase bytes/hashes stay frozen; verify prepared and cancelled entries |
| C-04 | Before publication, after publication, interrupted publication, and corrupt report; fail spies on builders/publishers/ensure helpers; read leaves missing report missing |
| C-05 | Residual risk intermediate/final phase and checkpoint facts from existing lifecycle; index/HEAD/reservations/ledger unchanged by CLI reads |
| C-06 | Actual pre-lineage fixtures from earlier migrations/state representations; pinned API fixtures and unsupported pre-sequence schema |

Include 101+ lineage elements at query/projection test level to prove page
completeness without running 101 model attempts. The multi-run integration
scenario must use the real supported retry workflow, not only injected DTOs.
Use deterministic publication fault hooks already provided by sequence_report;
tests must not publish a report on behalf of the command under test.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api tests/integration/test_phase21_3_sequence_inspection.py tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py tests/unit/scheduler/test_phase20_4_sequence_status.py tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py
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

No new sequence persistence/migration or recovery relation: Phase 20.7–20.9
already own these. Sequence state and each run state are different resources.
Existing status/report helpers that create paths cannot be reused unmodified
for integration inspection. Incomplete publication is a truthful observation,
not permission for a reader to repair state or discard residual risk.

## OpenQuestions

None.
