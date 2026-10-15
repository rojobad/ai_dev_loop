# Phase 26.3: Dashboard shell and overview charts

Status: operator-requested implementation scope; implementation pending.
Sequence position: 3/7. Implementer: Codex gpt-5.6-sol / xhigh (90 minutes).
Reviewer: gpt-6.1-sol / high, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Replace the provisional page with a real packaged-source React dashboard showing shared filters, KPIs, project/state bars, workload treemap and current capacity.

## Non-Goals

Sequence/timeline interaction, sensitive modal, activity calendar or automatic polling; those have dedicated successors.

## Scope

React/TypeScript/Vite frontend, ECharts imports/wrapper, locked build/test tools, DashboardDataSource adapter, source-aware filter state, overview widgets and accessibility.

## Out of Scope

Data metric algorithms already owned by 26.1, new HTTP operations, Hub auth/URLs, CDN runtime assets, browser API-response persistence and global multi-machine UI.

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

Actual accepted 26.1/26.2 public results, fixed local HTTP command and packaged
provisional page. Read `phase-26-reference/README.md` and images as synthetic
visual guidance, not data. No frontend exists on planning baseline; this run must
introduce its build/test tools and prove their commands. The Hub's intended
React 18/ECharts 6/echarts-for-react 3 stack supplies compatibility intent, not an
installed local dependency or code permission to alter that separate repo.


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

1. Create `dashboard-web/` with explicit pinned React 18, TypeScript, Vite,
   ECharts ^6 and echarts-for-react ^3 dependencies and lockfile; record the
   selected supported Node version. Add typecheck/test/build scripts and Vitest,
   DOM testing and Playwright/browser test tooling needed by following plans.
   Node is build/test-only. Bundle chart/font/assets locally, no CDN imports.
2. Define camelCase DTO parsers and DashboardDataSource interface; local fetch
   adapter owns POST transport/error/receivedAt semantics. React receives typed
   observations and components never construct localhost/SQLite/Hub URLs.
   Keep fixture mocks in tests only. Wire built assets into 26.2 static manifest;
   source build emits web_dist, and sdist/build inputs are explicitly included.
   Final standalone wheel verification is 26.7, not claimed from a dev server.
3. Implement project catalog paging, default/custom/preset ranges and named-zone
   conversion per shared contract. Invalid/ambiguous draft values stay uncommitted.
   Apply/Refresh owns canonical filter generation; old response/page/selection
   cannot enter a new scope. Include coverage, source/observation times, useful
   empty/error states and explicit map/activity placeholders for later slices.
4. Render four KPIs, horizontal stacked project states and project/sequence-or-
   standalone treemap from complete summary counts. Exact raw unknown states stay
   visible under Other. Project selection via chart updates the global filter.
   Capacity requests existing service and displays every returned window with
   percentage/reset/observed time; unknown values are unavailable, not zero.
   Capacity is visibly independent of project/time and no other read probes Codex.
5. Add light/dark theme, responsive layout and chart lifetime/ResizeObserver
   disposal. All labels/tooltips use safe text or escaped rich text; provider/role labels and optional metadata
   remain compatible with historical omitted fields and unknown additive strings; keyboard
   buttons/companion rows expose equivalent selection. Persist only theme.
6. Add real API-backed page/browser smoke with deterministic fixture ledger;
   update factual development/build notes and record
   `archive/implementation-history/findings/phase-26-3-dashboard-shell-and-overview.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C263-1 | Server package assets -> React -> API data source | Real local server plus fixture ledger, project/range edits, invalid draft and reversed-order replies. | Rendered header/counts match independent expected ledger values; prior filter replies never replace applied scope. Catches static illustrative data or URL logic embedded in charts. |
| C263-2 | Summary DTO -> KPI/bar/treemap -> global selection | Several projects, independent/sequence runs, risk/other states and clicked project. | Correct count/area/state labels; chart and keyboard action produce the same project filter. Catches computing totals from visible map pages or treating residual risk as ordinary completed. |
| C263-3 | Existing capacity service -> gauges | Two numeric windows plus missing/unavailable/reset fields; change project/date. | Current source timestamp/limits remain accurate and never imply historical quota; other queries issue zero capacity probes. Catches fake percentages or filter-dependent capacity. |
| C263-4 | Build + chart lifecycle + escaped data -> served browser | Offline bundled assets, dark/light resize/unmount, malicious source label and narrow viewport. | No external network/CDN requests, disposable chart instances/listeners, readable focus controls and literal labels without script/HTML effects. Catches unsafe tooltip interpolation and development-only assets. |

## Validation

Add frontend tests under `dashboard-web/src/dashboard/` and a scoped real-server
browser smoke under `dashboard-web/e2e/overview.spec.ts`. In this run define and
actually execute the new commands (the npm lines use `dashboard-web` as cwd):

```bash
npm ci
npm run typecheck
npm test -- --run
npm run build
npx playwright test e2e/overview.spec.ts
```

Browser dependencies must be installed in an owned test cache or explicitly
reported missing; browser smoke is a required gate, not optional residual risk.
Also run affected Python static-asset/server tests, `uv run python -m mypy src`,
`uv run mkdocs build --strict` and `git diff --check` at repo root. Document exact
build output and how Python serves it. 26.7 performs final installed wheel acceptance.

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

Missing assets/tooling is an execution prerequisite to fix in this owning run.
Keep native scheduler driver independent of new frontend builds. Build outputs,
node_modules and browser downloads belong to ignored/owned locations. Source
strings are data, including ECharts tooltips; do not insert source-generated HTML.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
