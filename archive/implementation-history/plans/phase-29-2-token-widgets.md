# Phase 29.2: Token charts and selected-run consumption

Status: operator-authorized scope 2026-10-19; dependent on accepted 29.1.
Implementer: Cursor SDK `grok-4.7` / reasoning `high`;
reviewer: `gpt-6.1-sol` / `high`.

## Goals

Deliver three linked global token views (Sankey, Sunburst, heatmap) plus a compact
selected-run consumption widget, using accepted 29.1 data and current Dashboard
UX governance. Finish with packaged frontend assets and one joint manual test.

## Non-Goals

No new usage math/capture, costs, provider billing, per-attempt analytics, temporal
token calendar, remote Hub UI, redesign of existing widgets or final acceptance
by an automated reviewer.

## Scope

DashboardDataSource and parsers/types, coordinated token reads, linked chart
selection, ECharts modules/options, selected-run consumption, loading/empty/error/
unsupported states, responsive layout, accessible equivalents, focused browser
tests, packaged assets, current docs and combined manual walkthrough.

## Out of Scope

Backend/domain/API redesign or replacing 29.1 (report prerequisite defects
instead), SQL/artifact reads in the browser, scheduler execution/persistence,
root YAML, governance/skills, timer/integrations/installation, real credentials,
new CDN/dependencies, chart IDs as labels, real model smoke tests, final Git
publication or Hub repository changes.

## Required Context

Read [Phase 29 overview](phase-29-dashboard-token-analytics.md) and accepted
[29.1 plan](phase-29-1-token-queries.md), implementation, schemas and findings.
Verify its actual `dashboard.token-usage` query, configured model attribution,
capability, category/flow coverage and exact-run scope before dependent work.
An archived plan is not evidence of implemented prerequisite behavior.

Read `AGENTS.md`, `docs/referencia/{dashboard,dashboard-ux,integration-api}.md`,
`docs/operacion/dashboard-local.md` and security guidance. Inspect real
`dashboard-web/src/dashboard/{types,parsers,local-fetch-data-source,
coordinated-data-source,refreshCoordinator,DashboardApp,DashboardChart,
InformationControl,RunMap,AttemptTimeline,ActivityCalendar,charts,time}` modules,
`src/styles.css`, frontend scripts/package lock, browser fixture server, current
chart/refresh/scroll tests and the frontend build-manifest/package checks.

## Cursor Rules And Skills

Follow `AGENTS.md` and `.cursor/rules/ai-dev-loop-governance.mdc`,
`ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-dashboard-contracts.mdc`, `ai-dev-loop-dashboard-refinements-contracts.mdc`
and `ai-dev-loop-docs-acceptance-contracts.mdc` in that rules directory.
Read applicable current portions of
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`; no `.cursor/skills`
exists. Independent reviewer uses
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementation does
not invoke/edit it. DUX-01..09 apply to all new panels and their integration.

## Architecture Guardrails

Components depend on DashboardDataSource. Both loopback HTTP and future adapters
use the shared typed contract; no host URL, SQLite, process or artifact knowledge
in components. Consume backend flow leaves and category meanings; do not renormalize
providers, estimate unknown amounts or total overlapping counters in JavaScript.
No per-run fan-out to calculate global totals from loaded map pages.

Preserve max two coordinated reads, Pause/visibility/generation fencing and
memory-only observations. Token summaries are nonsensitive metrics but never
trigger prompt/review/stdout/stderr fetching. IDs remain exact internal keys;
source strings are untrusted and escaped. Dates/ages/durations reuse selected-IANA
helpers. Model names are configured labels with provider-qualified identity,
not asserted actual models. No model call or real state in automated tests.

Use current pinned ECharts 6.1.0 and existing wrapper. Node 24 is build/test-only;
ship package assets and coherent source manifest. No SDK/model catalog calls,
root config changes, editable self-install, workstation/timer operations or Git
publication. Scheduler normalizes staging; final phase leaves work staged.

## Implementation Plan

1. **T292-01 — Typed data and one observation owner.** Add explicit token DTOs,
   defensive parsers and DashboardDataSource reads for accepted 29.1. An absent
   optional `dashboardTokenUsage` capability is unsupported, not zero and not a
   fatal Dashboard error; handle the existing HTTP error shape when no capability
   discovery is available. Preserve all existing widgets on an older producer.
   Create one scope-level token observation shared by all three global charts,
   refreshed through the existing coordinator at summary cadence (30 seconds),
   with canonical scope identity and Refresh/Pause/hidden-tab behavior. No timer
   per chart. A separate exact-run observation uses selected-run cadence (10s),
   scope+run identity and the same two-slot coordinator. No selected run means
   no run-token request. Do not duplicate an existing owner/read unnecessarily.
   Scope/run replacement cancels ownership and fences held old replies.
2. **T292-02 — Three global charts.** Register Sankey and Sunburst ECharts
   modules alongside the existing heatmap in DashboardChart. Render distinct
   panels with useful readable sizing, normal titles, compact known totals/
   coverage and the shared circled italic information icon. Suggested placement:
   a full-width Sankey, then Sunburst and heatmap side by side on wide screens,
   stacked on narrow screens. Prevent excess model rows from crushing labels;
   heatmap may use a bounded internal scroll region. No permanent cards/tables.

   - **Sankey:** configured model -> role -> backend disjoint token leaf, widths
     from known flow amounts. Distinguish provider+model and path-qualified
     internal nodes even if displayed labels repeat. No positive width invented
     for unknown or zero values. Preserve model/role provenance in click data.
   - **Sunburst:** model outer hierarchy containing role then disjoint token
     leaf; rings represent model -> role -> type, center can show known total.
     Totals and colors for leaf types agree with Sankey. Choose readable labels,
     tooltips and drill/selection behavior without reordering dimension meaning.
   - **Heatmap:** rows configured model (provider disambiguation), columns
     input_total, input_uncached, cache_read, cache_write, output_total,
     reasoning_output. Values use backend normalized category aggregates;
     columns overlap and are not a summable partition. Explicit absent/unknown
     cells differ from observed zero; partial values have visible coverage cues.
     A zero-only scale remains valid. Do not color missing values as measured0.

   All three derive from the same observation/filtered rows; do not mix query
   generations. Information explains configured provenance, full direct usage
   of started runs, partial lower bounds, disjoint/unsplit leaves and overlapping
   heatmap columns. Known zero, no runs, unknown-only, partial, unsupported and
   error states are distinct and visible, with retry on actual failures.
3. **T292-03 — Linked token facets.** Keep one in-memory model/provider and role
   selection for the three global charts, with compact accessible selects/chips
   and Clear selection. Clicking model/role nodes or sectors selects those exact
   typed dimensions; clicking a heatmap cell selects its row's provider+model,
   without treating its overlapping counter as a flow-leaf filter. Leaf clicks
   can select their containing model/role, not invent a counter conversion.
   Apply facets locally to the one aggregated response, recomputing displayed
   aggregates only by summing already disjoint rows/leaves, retaining coverage.
   No network request per click, automatic run selection, map filter or Inspection
   scope mutation. Clear facets on scope replacement; reconcile a vanished key
   after refresh. Run selection never narrows the global token charts.
4. **T292-04 — Selected-run consumption.** Add one chart-first run-token panel
   linked to the exact map selection and Attempt timeline. Initially show a
   concise select-a-run state with no metadata card or API call. On selection,
   show authoritative short name, compact known total/coverage and a horizontal
   stacked bar per configured model+role, with segments from backend disjoint
   leaves. No ancestry included, no global token facet filtering. Another run
   replaces the data and ignores late replies; scope-invalid selection clears.
   Optional precise values/native counters go in a closed disclosure, human
   metadata first and any IDs last. Explain direct-only semantics via information.
   Unknown/partial consumption remains visible; no attempt-specific drilldown.
5. **T292-05 — Stable accessible UX.** Preserve readable labels, shared token
   colors/units, exact numeric values in tooltips, optional compact tick notation
   and no technical IDs in chart tooltips. Escape model/role/native text through
   shared tooltip helpers; body-mounted viewport-confined overlays stay below
   modals and are cleaned up. Keyboard/touch alternatives invoke identical typed
   selection; information uses existing dialog focus/Escape/preventScroll.
   Reserve reasonable panel geometry during initial loads, refresh, facets and
   selected-run changes; no scrolling/focus jump or disappearing height that
   moves the chart outside the viewport. Retain 500px map internal scroll,
   automatic valid Inspection scope and existing activity geometry protections.
6. **T292-06 — Delivery and joint acceptance.** Update current Dashboard/API
   operational docs only for implemented behavior. Build static assets and
   regenerate/verify the checked-in frontend source manifest. Add
   `archive/implementation-history/plans/phase-29-manual-walkthrough.md` with one
   operator session for all four widgets; authoring the procedure does not mark
   it accepted. Record exact production/test evidence and pending gates.

## Testing Criteria

| Contract | Required observable evidence |
| --- | --- |
| T292-01 | Actual DataSource parser/HTTP/coordinator through rendered Dashboard: one global response supplies all charts, no map-page sums/fan-out, max2 active, 30s/10s cadence, Pause/hidden stops recurrence, valid scope auto-applies, held old scope/run responses cannot restore old rows, unsupported leaves existing Dashboard working. |
| T292-02 | Independent known Cursor/Codex fixture arithmetic from 29.1: Sankey/Sunburst totals equal knownTotalTokens (cache/reasoning counted once), hierarchy dimensions correct, mixed null/partial/zero different, heatmap overlapping columns not summed, unknown model retained, same display name/providers not conflated, no NaN/negative/infinite series. |
| T292-03 | Real ECharts clicks plus keyboard controls produce same model/role facets across all three charts with reset; exact provider-qualified identity, no token network fan-out, global scope/map/run selection unchanged; invalidated facet retires on refresh. |
| T292-04 | No selection = no run read; actual map clicks select exact runs with duplicate aliases and populate timeline + own token panel only, A->B held reply fence, direct versus ancestry totals distinct, global charts unchanged, unknown/missing data honest, no sensitive content reads. |
| T292-05 | Real browser at wide/narrow viewport, many models and bottom-of-page: hover not clipped/no IDs or unsafe HTML; information keyboard/Escape/focus; all disclosures closed initially. Hold real HTTP token response and compare scrollY, panel bounding box/document height before/during/after model/scope/run changes; graphs remain visible without automatic jumps. Time provenance uses selected IANA, e.g. UTC timestamp2026-10-19T12:05Z ->19/10/2026 08:05 in America/La_Paz regardless browser zone. |
| T292-06 | Build output/source manifest coherent, isolated installed wheel serves new assets and real typed fixture endpoint without Node, both token query scopes work, no real scheduler/timer/install side effects, manual walkthrough authored and explicitly pending human acceptance. |

Extend `parsers.test.ts`, `local-fetch-data-source.test.ts`, `DashboardApp.test.tsx`,
`charts.test.ts`, `DashboardChart.test.tsx`, `refreshCoordinator.test.ts` and add
focused token component/options tests. Add `e2e/token-analytics.spec.ts` with the
real fixture server and deterministic held-response synchronization; an option
mock or direct React setter does not prove ECharts click/scroll wiring. Extend
isolated installed acceptance fixtures/tests where assets/allowlist changed.

## Validation

Use repo Node24/tooling; do not skip browser checks because a global npm is
absent. Install only isolated development/test tooling when needed, not the
changing scheduler into the workstation tool environment.

```bash
# From dashboard-web:
npm run typecheck
npm test -- --run
npm run build
npx playwright test e2e/token-analytics.spec.ts e2e/linked-inspection.spec.ts e2e/phase28-refinements.spec.ts e2e/refresh-lifecycle.spec.ts
# From repository root:
uv run --no-sync python -m pytest -q tests/integration/test_phase26_7_installed_dashboard.py tests/integration/test_phase29_1_token_queries.py tests/integration/test_phase25_4_dashboard_compatibility.py
uv run --no-sync python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase29-2-docs
git diff --check
```

The frontend suite is scoped to this changed Dashboard, not the repository-wide
Python gate; after corrections rerun failed/affected tests. Run any new installed
test plus manifest validation used by the existing installed acceptance. Report
T292-01..06 and exact commands/results in
`archive/implementation-history/findings/phase-29-2-token-widgets.md`.
Full repository suite is a separate once-per-sequence integration gate before
operator finalization, not a required repeated reviewer turn. Do not claim it or
human acceptance unless actually performed. Unfinished mandatory chart/data/
browser/package behavior remains incomplete scope.

The joint manual procedure must cover: unfiltered/project scope and auto-apply;
linked model/role clicks and accessible reset; three equal flow totals and
overlapping heatmap interpretation; exact-run detail independent of global
facets; partial/unknown/zero/unsupported; collapsed details/information; hover
escape and theme; selected-IANA dates/durations; mobile/wide layout; bottom-page
scroll during delayed reads; Pause/Refresh and no sensitive auto-load. Include
the supported local serve command, but no implicit workstation installation.

## Risks Or Recovery Notes

Large hierarchies can crowd labels; use adequate chart space and bounded internal
scroll rather than shrunk text or cards. Keep selection keys independent from
display names and body tooltip lifecycle safe. Old API support must not turn
absence into zero consumption. If 29.1 cannot support the approved producer
contract, record the exact blocker; do not bypass it with browser artifact reads,
unknown-counter assumptions or a private duplicate aggregation.

## OpenQuestions

None.
