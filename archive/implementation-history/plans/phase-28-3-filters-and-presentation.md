# Phase 28.3: Filters and presentation

Status: operator-authorized 2026-10-16; implementation pending.
Implementer: Codex gpt-5.6-sol / xhigh; reviewer: gpt-6.1-sol / xhigh.

## Goals

Finish the operator's requested UI: automatic valid filtering, complete cohort
loading, concise panels, readable activity and unclipped tooltips. Deliver an
integrated built/installed artifact suitable for one final human walkthrough.

## Scope

Filter lifecycle; operation-specific runs pagination up to 1000; adaptive batch
loading under existing byte bounds; shared Information controls; ActivityCalendar;
tooltip overlays; affected public schemas/docs and isolated packaging smoke.

## Out of Scope

Raising memory/time/content/security bounds, increasing unrelated operation page
limits, durable caches, new live subscriptions, Dashboard metadata editing,
real installation/timer update or final Git publication/human acceptance.

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

1. Verify accepted 28.1 aliases and 28.2 compact selection/formatting behavior.
   Inspect DashboardApp/time/refreshCoordinator/coordinated-data-source, RunMap,
   ActivityCalendar, DashboardChart, styles, domain filters, dashboard queries,
   IntegrationDashboardReadService, HTTP dispatcher and typed public schemas.
2. C283-1: remove Apply. Project selection (including chart controls)/preset
   applies immediately; typed date/timezone edits debounce 400 ms and only commit
   valid coherent drafts. Keep last valid scope on invalid/incomplete input.
   Cancel obsolete requests, clear retired selections/content, preserve two-read
   concurrency and Pause/hidden-page/no-sensitive-poll rules. Explicit Refresh
   re-observes current valid scope; no feedback request loop or partial drafts.
3. C283-2: allow dashboard.runs limit 1..1000 in shared CLI/API and HTTP validation,
   typed models/schema/parsers; leave projects/run.timeline and all other limits
   unchanged. Preserve fixed requested-limit page semantics and existing
   nextOffset arithmetic. Keep envelopes 240 KiB, 5s/10k/100k limits. Provide a typed
   distinguishable page-envelope-too-large error (or existing structured reason
   if already available), compatible with generic error envelope. Do not parse
   human error message text or retry every INVALID_ARGUMENT.
4. C283-3: request 1000 initially and automatically follow nextOffset to exhaustion;
   if an envelope is too large, retry the same offset at half-size down to 1,
   then continue from each successful returned nextOffset with its actual limit.
   No silent server truncation, no offset jumps/duplicates, no unbounded retry;
   a single oversize row or semantic/data error stays a visible error. Keep
   loaded/total progress visible, a compact Retry continuation on failure and
   all internal pagination machinery. Never present partial data as full.
   Use the coordinator's max2 reads, cancellation, yield to selected-run work.
   Preserve loaded pages during polling and reconcile changed first-page IDs
   without dropping, indefinitely duplicating or skipping old rows. Explicit
   Refresh/scope change safely restarts complete loading. Standard flow requires
   no Load more button. Stable dataset/ordering must cover >1000 runs including
   replacement IDs; growth during paging remains explicitly observed snapshots,
   never a fabricated atomic aggregate.
5. C283-4: add shared accessible Information control per panel/chart header.
   Move explanatory paragraphs, metric definitions, filter semantics and detailed
   observation provenance into it. Keep visible headings, data, controls,
   concise freshness/progress and actionable coverage/error/unknown states.
   Buttons work by click and keyboard with aria-expanded; Escape/close restores
   focus where relevant. No tooltip-only hidden explanations. Move repeated
   explanatory text out of modal heading; keep meaningful reader availability.
6. C283-5: remove ActivityCalendar's permanent daily list; expose equivalent
   keyboard-accessible daily values on demand without clutter. Keep all three
   metrics/selected-zone date formatting. Resolve ECharts tooltip clipping via
   a body-level/app overlay with proper viewport confinement/z-index/cleanup,
   rather than letting long hover content overflow offscreen. Protect escaped
   strings, theme, modal layering and detached chart disposal. Fix all charts,
   including hover over first calendar row near panel top.
7. C283-6: update public docs, affected Phase26 fixture expectations and built
   assets/manifest through standard build. Build wheel/sdist and smoke installed
   artifact in isolated UV environment with temporary XDG and owned free port:
   GET root/assets, named historical/new DTO fixture reads and API limit behavior;
   no live scheduler or real account ledger migration. Update final manual
   walkthrough with actual new commands. Record final evidence and pipeline gate
   ownership, leaving real workstation install and manual approval pending.

## Testing Criteria

| ID | Production path/scenario | Required independent outcome |
| --- | --- | --- |
| C283-1 | Filter UI -> coordinator -> real reads, with fake timers/held old response | Immediate project/preset, one commit after 400 ms valid typing, no request for invalid draft, Apply absent; old replies cannot restore data/reader, Pause/hidden/Refresh semantics intact, <= 2 active reads. |
| C283-2 | Real shared service/CLI and HTTP against fixture ledger | runs accepts 1000 rejects 1001/bool/zero; other collections still 100; schemas/parser agree; >240KiB raises typed size reason; old generic errors remain valid, no increase in budgets. |
| C283-3 | Map data source over >1000 exact fixture IDs with long aliases and induced oversize pages | Automatic full ID set with no manual Load more; requested initial 1000, halving retries same offset, correct continuation even variable batches; terminal errors visible, no infinite retry. Cancel/filter switch retires complete old chain. Polling retains historic loaded IDs; summary total never counted from partial pages. |
| C283-4 | Every panel/chart in real browser | Info initially closed, instructional wall absent, explanations available by keyboard, close/focus works; meaningful warnings remain visible and aren't hidden as documentation. |
| C283-5 | Real ECharts calendar first-row hover at 1095x884 plus narrow screen, light/dark | Actual tooltip DOM bounding box visible above panel and within viewport, no clipping; daily wall absent and on-demand accessible equivalent correct; process durations use human formatter. |
| C283-6 | Build -> wheel/sdist -> installed isolated HTTP server | Shipped assets match new source/build manifest, names/new filters work without Node runtime; read-only startup and other bounds preserved. Final manual instructions describe actual CLI; no real install/model/timer side effects. |

## Validation

Add backend tests in `tests/integration/test_phase28_dashboard_paging.py` and
unit service/HTTP/schema coverage. Extend RunMap/DashboardApp/ActivityCalendar,
coordinator/parser/chart tests and add info/tooltip coverage. Add
`dashboard-web/e2e/phase28-refinements.spec.ts` using real isolated backend and
chart events; generate >1000 runs through fixture setup, no liveledger reads.

```bash
uv run python -m pytest -q tests/unit/dashboard tests/integration/test_phase28_dashboard_paging.py tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
# dashboard-web
npm run typecheck
npm test -- --run
npm run build
npx playwright test
# repository root
uv run python -m build
```

Run the new isolated installed smoke and affected existing installed acceptance
checks using scripts/verify-phase26-dashboard.py or equivalent targeted harness.
All browser commands above use Node24 and temporary fixtures/fake providers, not
real account data. Do not require a full repository pytest per correction. One
repository-wide pytest is assigned to the final integration/pipeline gate after
three accepted runs; report pending if not executed, or exact failed/unexecuted
results if run. Do not add a CI workflow just for this plan. Findings:
`archive/implementation-history/findings/phase-28-3-filters-and-presentation.md`.
Map each C283 contract to real producer/caller/test result. Failed required
focused/browser/package checks block completion. Final manual acceptance is
separate and must not be claimed from automation.

## Risks Or Recovery Notes

1000 summary rows often exceed240KiB: adaptive batches are required behavior,
not an optional fallback that leaves the operator pressing Load more. Max1000
is a request bound, not a promise all such responses fit. Reuse semantic queries
and existing offsets; never solve overflow by weakening bounds. Distinguish
scope-too-large errors from page-too-large errors. Cancellation retires all
adaptive retries and info/tooltip overlays on disposal. Preserve exact IDs and
bounded native scheduler behavior through all refinements.

## OpenQuestions

None.
