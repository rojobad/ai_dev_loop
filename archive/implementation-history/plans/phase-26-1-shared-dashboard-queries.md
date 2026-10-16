# Phase 26.1: Shared dashboard queries and Integration API

Status: operator-requested implementation scope; implementation pending.
Sequence position: 1/7. Implementer: Codex gpt-5.6-sol / xhigh (90 minutes).
Reviewer: gpt-6.1-sol / xhigh, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Implement the complete semantic query layer for one local installation and expose the four dashboard operations through the public JSON Integration API, before any HTTP/UI depends on them.

## Non-Goals

Rendering charts, serving HTTP, remote Hub/Bridge integration, token/billing telemetry, Phase 24 timeouts or Phase 25 SDK adoption.

## Scope

Pure filter/time/metric rules, batched read port/SQLite adapter, typed public DTOs and schemas, dashboard projects/summary/runs/activity CLI, additive capability/version and focused regression coverage.

## Out of Scope

HTTP/frontend/runtime packaging, scheduler writers/migrations, runner behavior, root YAML, user XDG/ledger/artifacts and existing Phase 24/25 planning.

## Required Context

Refreshed planning baseline is `main` at `466b8e09724430cebddbdeca6f1961b2d6243531`, observed 2026-10-15:
Phase 27.1–27.7 dual implementer, role-aware inspection and recovery code is merged.
Phases 24/25 remain planning-only and are not dependencies. SQLite schema is 16;
local API is 1.8 before 26.1 adds dashboard capability in compatible minor 1.9.
Python >=3.11 and `uv` remain the runtime/tooling. The selected implementer is
Codex `gpt-5.6-sol / xhigh`, independently bound from reviewer B. Manifest v2
freezes provider/model/effort/90-minute timeout per phase. Existing Phase 27
findings retain their historical full-suite/build verification limits; this
phase owns its changed contracts and 26.7 integration gate, not a replay of 27.
Recheck relevant accepted code once at admission. A future predecessor plan or
staged patch is not evidence of accepted runtime. If a producer changed, stop
only dependent work and report its exact incompatible/missing contract.

Read [Phase 26 overview](phase-26-local-dashboard.md),
[shared dashboard contract](phase-26-dashboard-contract.md),
[source inventory](phase-26-source-observations.json),
[visual reference](phase-26-reference/README.md), `AGENTS.md`,
`docs/referencia/integration-api.md`, `docs/referencia/cli.md`,
`docs/operacion/estado-artefactos.md` and `docs/operacion/seguridad-privacidad.md`.
The referenced new paths below are implementation outputs, not current prerequisites.

No Phase 26 predecessor. Existing evidence: `integration_api/run_service.py`,
`run_projection.py`, `sequence_service.py`, `sequence_projection.py`,
`review_service.py`, `process_output_service.py`, `models.py`, `schemas.py`,
`commands/integration.py`; `scheduler/domain/effects.py`, `cursor_contract.py`,
`codex_contract.py`; `scheduler/infrastructure/sqlite_store.py` read-only store
and validated snapshot/lineage loaders. Reuse `tests/unit/integration_api/`,
`tests/unit/scheduler/test_schema_readonly_historical.py` and genuine Phase 21
historical fixtures. Never use the unexecuted token/SDK/timeouts plans as source.


Provider-aware prerequisite: read `scheduler/domain/codex_contract.py`,
`tests/integration/test_phase27_3_inspection.py` and the accepted Phase 27
source/inspection DTOs. Execution includes `cursor.run_turn`,
`codex.implementer_bootstrap`, `codex.implementer_resume`,
`codex.bootstrap_review` and `codex.resume_review`. Only the final two are
review rounds. `component=codex_implementer` identifies implementation, while
`component=codex` plus the review effect allowlist identifies reviewer attempts.
Preserve implementerProvider, pending role/turn/capacity and retry metadata;
historical absent provider stays Cursor. An `implementer_retry` replacement is
authoritative lineage, never a reason to parse run IDs. Existing public process
output supports both providers; there is no exact per-attempt implementer-prompt
read contract to invent from private files.

## Cursor Rules And Skills

Follow `AGENTS.md` and these repo-local rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`

Read `.agents/skills/create-cursor-plan/SKILL.md`; for documentation/evidence,
read `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, applying its
current source/privacy requirements rather than historical Phase 8 restrictions.
The separate final reviewer follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementation does
not invoke or modify it. There is no `.cursor/skills/` directory. Codex authored
the new dashboard rule/shared contract; the implementer does not change those policies,
planning/review skills or reviewer/correction/result schemas to pass acceptance.

## Architecture Guardrails

One local installation owns the data. SQLite reads use the read-only store and
validated snapshots/lineage; the new SQL read adapter belongs inside ai_dev_loop.
No scheduler transition, migration, reservation, repair, Git, model turn, timer
or real workstation installation is a dashboard side effect. Initial execution
means the first owned execution launch request, excluding chat creation/probes;
project and lineage come from authoritative fields, not names. Keep public DTOs
separate from internal models and ECharts options. Enforce the shared contract's
query/page/envelope/time bounds; missing evidence and partial pages are explicit.

The local server has no login and binds only 127.0.0.1 with typed same-origin read
routes. Sensitive content uses existing confined, verified, paginated readers,
loads only through deliberate UI actions, remains memory-only and is not logged.
UI components use DashboardDataSource; transports/SQLite never own chart logic.
Requests and content retire on scope/selection changes; old responses cannot
restore data. Preserve existing major compatibility and unsupported historical
semantics. Capacity is current, independently dated, and never time-filtered.

Keep root ai_dev_loop.yaml, Phase 24/25 plans, scheduler execution/recovery,
installed providers/drivers, user integrations and live source ledger/artifacts
unchanged. A stable installed scheduler driver must run this self-hosted sequence;
using an editable changing checkout as the live implementation driver is outside
scope. Explicit sequence start authorizes only existing non-final checkpoint
commits; agents do not create their own commits, push or final acceptance.

The shared contract fixes transport parameters/DTOs, metric ownership, lifecycle
and explicit safety bounds; complete this plan's owned slice without broadening
adjacent work. Read the exact public/shared types rather than reproducing rules
from display strings. Any incompatible prerequisite is a dependent-work blocker.

## Implementation Plan

1. Create `dashboard/domain/filters.py`, `metrics.py`, `application/ports.py`,
   `queries.py` and `adapters/sqlite_read.py`. Build batched validated observation
   records and perform no N+1 subprocess inspection. Authoritative SQL/JSON
   prefilters must be backed by validated snapshots and lineage or a shared
   batched equivalent preserving the existing hash/state invariants. Document the
   reviewed mapping of current state variants; new unknown values remain Other.
2. Implement all four query results exactly as the shared contract: catalog,
   started-cohort summary/workload, joined map pages and event-date activity.
   Replacements have distinct run IDs and source-recorded binding/generation.
   Summary totals cover the query cohort, independently of map pagination.
3. Include owned Cursor and Codex implementer execution effects as well as
   reviewer effects in start/process-time; review rounds use reviewer effects only.
   Preserve implementerProvider and new lineage kind; public compatibility tests
   include historical omitted provider, current Codex provider and role/retry DTOs.
   Use explicit execution effect allowlist, first launch, one review round per
   run+iteration, clip/split process intervals by named-zone day and retain
   coverage exclusions. SQL timestamps are normalized instants, not lexical
   comparisons of different RFC3339 representations. Current capacity/content
   are not part of these aggregate queries. Canceled attempts contribute hours
   only after existing ingested/reconciled termination evidence confirms resolution.
4. Add explicit models, packaged schemas/registry and compatible minor/capability
   as specified. Add the singular `integration dashboard` group to existing JSON
   presentation. Preserve plural integrations and every existing command/envelope.
   Old info payload without dashboard means false; explicit null/coerced flag fails.
5. Enforce range, row/time/envelope bounds; check schema support before dependent
   SQL, use bounded progress cancellation and release transaction before output.
   Empty absent ledger is read-only; oversized scope fails without partial totals.
6. Add independent fixture-backed contract tests through the real CLI/service;
   update existing Integration API reference/CLI pages only for implemented commands.
   Record findings at `archive/implementation-history/findings/phase-26-1-shared-dashboard-queries.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C261-1 | Validated local ledger -> query service -> summary/map CLI | Project/range and execution-attempt fixture with chat creation before execution, pre-range submit, queued-only run, unknown state, two replacements and >25 matching runs. | Hand-authored cohort IDs/state counts and authoritative lineage; totals remain correct past the first page. Catches counting chat creation/submissions or parsing IDs as start/project. |
| C261-2 | Execution attempts -> pure metric rules -> activity CLI | Two reviews in one iteration, intervening Codex implementer initial/correction turns, one later iteration, a separate implementer_retry successor run, parallel processes, missing/active/canceled-unresolved intervals, range boundaries and midnight/DST crossing. | Explicit expected daily rounds/seconds, e.g. two parallel one-hour intervals =7200 seconds; no cross-run dedup or duration fabricated. Catches retry double counts and start-cohort filtering of event activity. |
| C261-3 | Integration CLI -> envelope/schema -> compatible consumer | Four real commands with strict params, historical info fixtures missing flag, additive fields and mismatched major. | Validate real serialized JSON against packaged schemas and concrete expected values; old APIs remain parseable. Catches advertised capability without functioning handlers/schema drift. |
| C261-4 | Read-only adapter -> bounded SQLite snapshot -> caller/error | Missing/old supported/unsupported/corrupt ledger, candidate/envelope limits and deterministic progress-budget expiry while a separate writer commits. | No created paths/migrations/state writes; read snapshot is coherent, connection retires, next write succeeds, scope failure exposes no partial success/driver text. Catches read-side bootstrap or leaked long transaction. |

## Validation

Add `tests/unit/dashboard/test_metrics.py`, `test_queries.py` and
`tests/integration/test_phase26_1_dashboard_queries.py`. Run:

```bash
uv run python -m pytest -q tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_run_projection.py tests/unit/scheduler/test_schema_readonly_historical.py
uv run python -m ruff check src/ai_dev_loop/dashboard src/ai_dev_loop/integration_api src/ai_dev_loop/commands/integration.py tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Validate new CLI syntax through CliRunner/subprocess fake-backed contract tests,
including `--help`/invalid arguments and exactly one JSON document; do not probe
live data as proof of metric correctness. Known fixtures are synthetic except
explicitly labelled genuine historical compatibility inputs.

These are planned implementation checks, not results of plan authoring. Use
isolated temporary XDG/homes/repos and fake providers; tests must not launch real
Cursor/Codex model turns, tick/control the live scheduler or change installed
services. Report exact commands, exit codes, elapsed time, executed/failed/unrun
checks and every contract ID -> production caller -> independent assertion ->
result in the owning findings file. Mandatory missing behavior/tests remain
incomplete scope. After correction rerun failed/affected checks; expand only for
a concrete remaining risk. Full repository pytest belongs once to 26.7's
integration gate, not to every run/review. Target <=40 minutes focused validation
per subphase, keeping setup/cleanup owned and bounded. A failed required check
blocks dependent work; informational READY_FOR_REVIEW/BLOCKED_CONTRACT labels
are not new scheduler states.

## Risks Or Recovery Notes

No persistent schema change is authorized. Revert code ordinarily before
installation; read-side failures preserve the ledger/artifacts. A known historical
missing timestamp is partial coverage; a broken required new query/schema is
unfinished mandatory work. A newer unsupported ledger/API requires a concrete
compatibility decision, not an unvalidated SQL fallback.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
