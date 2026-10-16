# Phase 28.1: Short names

Status: operator-authorized 2026-10-16; implementation pending.
Implementer: Codex gpt-5.6-sol / xhigh; reviewer: gpt-6.1-sol / xhigh.

## Goals

Add scheduler-owned editable short names and require preparers to choose them
for new runs/sequences/planned leaves, completing legacy values automatically.

## Scope

Typed metadata persistence/migration; new submit flag and manifest v3; local
explicit alias editing commands; preparation/materialization/recovery wiring;
Integration API projections/JSON schemas; Dashboard types/parsers/labels; docs.

## Out of Scope

Changing frozen execution digests or old payload bytes; execution transitions;
Dashboard write endpoints; bulk manual renaming of the operator's current data;
run selection redesign and Phase 28.3 loading/filter behavior. Do not edit agent
policy: narrow new preparation guidance is already authored by Codex.

## Non-Goals

Remote Hub implementation, scheduler execution/recovery redesign, authentication,
new metrics, exact implementer-prompt reconstruction or automatic final acceptance.

## Required Context

Read [Phase 28 contract](phase-28-dashboard-refinements.md) in full, including
compatibility/rollout and all approved product behavior. Accepted baseline is
main `67b8edc` with completed Phase 26 and dual-implementer Phase 27. Recheck the
actual accepted predecessor once before dependent work. Read
`docs/referencia/dashboard.md`, `docs/referencia/integration-api.md`,
`docs/referencia/cli.md`, `docs/operacion/estado-artefactos.md`, and
`docs/operacion/seguridad-privacidad.md`. Archived Phase 26 plans explain original
wiring; Phase 28 explicitly supersedes the UI behavior identified in its contract.

## Cursor Rules And Skills

Follow `AGENTS.md` and `.cursor/rules/ai-dev-loop-{governance,orchestrator-contracts,
state-and-schema-contracts,loop-and-resume-contracts,abort-contracts,
codex-review-contracts,docs-acceptance-contracts,global-integrations-contracts,
dashboard-contracts,dashboard-refinements-contracts}.mdc` (brace notation lists
individual existing files, not a shell command). Read
`.agents/skills/create-cursor-plan/SKILL.md` and applicable documentation guidance
in `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, using current
AGENTS.md rather than retired command instructions. There is no .cursor/skills.
Independent reviewer follows `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`;
implementation does not invoke or modify review policy.

## Architecture Guardrails

Keep domain/application logic independent from CLI, SQLite, HTTP and ECharts.
Scheduler metadata owns labels; browser never derives identity, state, project,
provider or lineage from them. Dashboard remains read-only and loopback-only,
with typed additive projections and no arbitrary SQL/path/command route. Existing
content reads remain explicit, confined, memory-only and generation-fenced;
no content preload or auto-refresh. Preserve frozen artifacts/hashes, exact role
identities, accepted historical data, bounded read concurrency, Pause and hidden
page behavior. Labels/source strings are escaped, not trusted HTML.

Use a stable installed driver for this sequence. Never install this checkout,
change real SQLite, touch real artifacts/integrations/timer, launch real model
calls as tests, or alter root ai_dev_loop.yaml. Native sequence checkpointing
owns non-final commits; implementer makes no ad hoc commits/push/PR/merge.

## Implementation Plan

1. Inspect sequence.py, submission.py, sequence_prepare/materializer and native
   replacement/recovery paths; sqlite_store, migrations, read-only snapshots,
   integration_api run/sequence/dashboard models/projections and schema exports.
2. C281-1: add a typed presentation metadata store outside immutable payloads,
   with explicit max 35 trimmed-code-point validation and run/sequence/planned
   leaf ownership. Add the next registered migration after schema 16; backfill all
   existing records from the Phase 28 contract. Do not edit historic migrations.
   Read-only Dashboard startup must never initiate migration. Define a legacy
   read fallback for schema 16/missing optional metadata and a clear unsupported
   schema outcome where needed; expose nonempty short names through projections.
3. C281-2: introduce manifest v3 preserving v2 implementer selection, requiring
   short_name on sequence and each phase. Add --short-name to fresh standalone
   scheduler submit. Fail missing/null/blank/overlength new labels before durable
   work or resource publication. Preserve exact historical replay. New label
   metadata is written atomically with scheduler ownership/entry creation;
   do not change old identity derivation or rewrite accepted frozen inputs.
4. C281-3: store planned leaf aliases at prepare, materialize them with exact
   planned_run_id, and propagate a source alias to a new retry/replacement run
   only once. Replay/restart must not overwrite a rename. Add explicit local
   commands `scheduler run set-short-name <run-id> --short-name ...` and
   `scheduler sequence set-short-name <sequence-id> --short-name ...`; implement
   only metadata changes, including prepared/terminal records, with typed errors
   for missing IDs. No Dashboard mutation or execution effect. Renaming a planned
   but unmaterialized leaf can remain deferred; its initial name is required.
5. C281-4: propagate shortName and sequenceShortName as appropriate through
   shared Integration API inspect/list/dashboard DTOs and schemas, producer and
   DashboardDataSource parsers. Existing payload consumers retain compatibility.
   Map axes and run labels prefer short names; full original names and IDs remain
   in details/tooltips. Handle duplicate names using IDs internally. Unknown
   legacy labels fall back through the approved metadata projection, not frontend
   name parsing. Decide API minor/capability bump from existing policy and update
   all info/schema/tests/docs consistently.
6. C281-5: document migration/new flags/v3/examples/edit commands and the
   historical replay boundary. Verify the conditional preparation guidance in
   AGENTS.md/planning skill matches the implemented CLI. Keep this sequence's
   pre-existing v2 manifest untouched. Record findings and final manual checks.

## Testing Criteria

Automated unit/integration/schema tests are mandatory for this changed boundary.

| ID | Production path/scenario | Required independent outcome |
| --- | --- | --- |
| C281-1 | Migration from genuine schema 16 runs/sequences/planned leaves, then reopen/replay | All names populated with specified truncation; IDs, frozen bytes/hashes, state, reservations and artifact digests unchanged; edited names survive rerun. Test multibyte/35-code-point input, null/blank/control/36-character rejection. |
| C281-2 | Real CLI submit and sequence prepare v3 through persistence; historical v1/v2 fixture replay | New missing names rejected without resource creation; valid v3 freezes independent roles and preserves leaf names; fresh legacy input gets upgrade error, accepted legacy replay/materialization works. No real model call. |
| C281-3 | Prepare -> start/materialize -> existing retry successor/restart and alias edit commands | Correct aliases linked to exact IDs, no copying wrong leaf, replay does not clobber manual rename; edits do not reserve, authorize, dispatch or change immutable hashes. Missing target/invalid name leaves prior metadata intact. |
| C281-4 | Shared read-only queries -> CLI/HTTP JSON DTO/schema -> Dashboard parsers/chart labels | Names match real store, historical fallback is nonempty, duplicate names select distinct exact IDs, schema 16 read causes no migration/write. Malicious alias rendered as literal text. |
| C281-5 | Supported CLI/help/docs examples | New commands/examples match implementation; old frozen fixtures remain truthful; final manual governance review explicitly pending. |

## Validation

Add `tests/unit/scheduler/test_phase28_short_names.py` and
`tests/integration/test_phase28_short_names.py` (or equivalent organized files).
Run these plus affected migration/submit/sequence-prepare/materialization/recovery
and Integration API schema/dashboard tests; use existing Phase17.1, Phase20.1,
Phase27.3 fixtures, not ad hoc rewritten historical payloads. Update parsers,
RunMap and DTO fixtures; run corresponding Vitest tests and typecheck.

```bash
uv run python -m pytest -q tests/unit/scheduler/test_phase28_short_names.py tests/integration/test_phase28_short_names.py
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
# from dashboard-web:
npm run typecheck
npm test -- --run src/dashboard/parsers.test.ts src/dashboard/RunMap.test.tsx
```

Commands are expected validation, not authored results. Use isolated temporary
ledgers/repos/XDG and fake CLIs. Record actual changed-regression test paths and
exact executed/failed/unrun checks in
`archive/implementation-history/findings/phase-28-1-short-names.md`.
Mandatory historical materialization, migration and new-entry tests cannot be
relabelled residual risk. Full repository suite belongs to final integration.

## Risks Or Recovery Notes

The installed driver remains schema 16 during this self-hosted sequence: tests
migrate only fixtures. The live DB is not upgraded until a separately authorized
installation. Prepared phase28 v2 must remain valid after later upgrade. If
metadata transactions fail, scheduler/alias records roll back together; replay
completes exactly once without replacing later edits. Abort retains metadata and
normal execution evidence; aliases grant no execution authority. Do not add
continuous baseline checks or speculative scheduler lifecycle changes.

## OpenQuestions

None.
