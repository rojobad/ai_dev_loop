# Phase 29.2 token widget findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-29-dashboard-tokens-retry`, HEAD
`6b1f8ccd3a71d003b26bb7e4d6e55411d587f016` (`feat: add shared Dashboard token analytics queries`).
The Phase 29.2 widget, test, doc, and asset changes are uncommitted in that
worktree. No commit, push, pull request, or merge was made.

OpenQuestions: none in the Phase 29.2 plan. Phase 29.1 findings likewise have
none. No new aggregation, price, billing total, inference capture, or backend
token contract was added. The widgets consume the accepted 29.1
`dashboard.token-usage` payload.

## Admission and prerequisite evidence

Phase 29.1 is present at HEAD: public Integration API 1.13,
`capabilities.dashboardTokenUsage`, and `dashboard.token-usage`. Cohort
membership remains started runs. Flows remain the seven-leaf disjoint
partition. Heatmap columns remain the overlapping normalized categories.
`configuredModel: null` stays null. The same model spelling on Cursor and
Codex stays distinct. Unknown stays null; a recorded zero stays 0. An older
producer that rejects the operation returns `INVALID_ARGUMENT` with
`Unsupported dashboard read operation.`

No root YAML, live scheduler state, timer, or integration install was changed.
Tests used fake transports, the existing dashboard fixture server, and an
isolated installed wheel. No model call was used as a test.

## Contract evidence

| ID | Production entry | Independent test evidence | Result |
| --- | --- | --- | --- |
| T292-01 | `TokenAnalytics` owns one cohort `coordinator.observe` at `SUMMARY_REFRESH_MS` (30s) and calls `tokenUsage(filters, null)`. `RunTokenConsumption` observes `SELECTED_RUN_REFRESH_MS` (10s) only while a run is selected. Both go through `CoordinatedDashboardDataSource` on the existing two-slot `RefreshCoordinator`. `LocalFetchDashboardDataSource.tokenUsage` posts `dashboard.token-usage` and omits `runId` for the cohort. `parseTokenUsageData` checks the producer leaf order and the known-total partition. `isUnsupportedTokenUsage` retires the observer for `UNSUPPORTED` or the exact older-producer `INVALID_ARGUMENT`. A retained cohort observation is kept only for the same parsed scope. | `parsers.test.ts` accepts the 255 and mixed partitions and rejects a broken leaf sum and `Infinity`. `local-fetch-data-source.test.ts` omits `runId` for the cohort and sends the exact id for a direct read. `refreshCoordinator.test.ts` shares two slots between the 30s scope and the 10s run. `TokenAnalytics.test.tsx` uses one cohort response, keeps a same-scope refresh, drops a held, failed, or late incompatible scope, and stops recurrence on Pause. `DashboardApp.test.tsx` shares one cohort read and still shows `Runs started: 7` when token usage is unsupported, with no invented known total. | Correction round: **15 files, 92 passed**. |
| T292-02 | `tokenCharts.ts` builds Sankey and Sunburst from disjoint leaves only when `knownTotal > 0`. The heatmap uses `input_total`, `input_uncached`, `cache_read`, `cache_write`, `output_total`, and `reasoning_output`. Null cells are a second series drawn off the measured scale. A measured zero uses the low color and the label `0`. Partial cells keep a visible border. `visualMap` does not use a negative sentinel. `DashboardChart` registers Sankey and Sunburst. | `tokenCharts.test.ts` keeps the 29.1 mixed total at 255, refuses to treat the heatmap column sum as that total, keeps Cursor and Codex `shared` distinct, and separates unknown null from recorded 0. The same file initializes pinned ECharts and checks positive Sankey node heights plus distinct unknown and zero heatmap fills. `charts.test.ts` escapes an injected model in the token-flow tooltip. The browser fixture catalog checks 363 / 140 / 115 before the overlay is served. | Correction round: **15 files, 92 passed**. Browser evidence is in the F-02 row below. |
| T292-03 | Facets are React state on `TokenAnalytics`. A model click selects provider plus configured model and clears role. A role or leaf selects the containing model and role. A heatmap cell selects the row provider and model only. `projectTokenUsage` sums disjoint rows already in memory. Scope replacement clears facets. Run selection is not part of the read key. | `TokenAnalytics.test.tsx` applies `cursor:shared` from the chart event, shows 140 without another read, filters reviewer to the empty known-flow message, and restores 255 with Clear selection. `tokenCharts.test.ts` reconciles a vanished facet and treats a heatmap cell as a model selection. `token-analytics.spec.ts` clicks the real model-colored canvas, then clears with the keyboard, without another `dashboard.token-usage` request. | Correction round: **15 files, 92 passed**, and **15 Playwright tests**. |
| T292-04 | `RunTokenConsumption` renders no metadata and registers no observer when `selectedRun` is null. A selection reads that exact run. The bar uses disjoint leaves grouped by provider, model, and role. Global facets do not filter it. A different run drops the previous observation. Precise values stay in a closed disclosure, with the run id last. | `TokenAnalytics.test.tsx` performs no direct read until selection, keeps duplicate short names distinct, and shows `run-b` only after the disclosure opens. `DashboardApp.test.tsx` adds one direct read on selection and does not refetch the cohort. The browser spec shows direct totals 140 then 115 while the cohort stays 363, and records no `run.initial-prompt`, `run.review`, `run.review-content`, or `run.output`. | Correction round: **15 files, 92 passed**, and **15 Playwright tests**. |
| T292-05 | Shared leaf colors live in `tokenCharts.ts`. Tooltips use `escapeHtml` and body-mounted viewport positioning. `InformationControl` keeps focus return, Escape, and `preventScroll`. Token panels reserve height (Sankey slot and its 480px scroll, heatmap scroll, run panel) and set `overflow-anchor: none`. The same-scope reload keeps the last cohort observation. Observed time uses `formatInstant` in the selected IANA zone. The heatmap stacks under the sunburst below 900px. A selected run uses a fixed chart viewport, so a smaller or zero replacement cannot shrink the panel. The previous run's data is cleared immediately. | `token-analytics.spec.ts` runs in `Pacific/Auckland`, expects `19/10/2026 08:05` for `2026-10-19T12:05:00Z` in `America/La_Paz`, and rejects `20/10/2026 01:05`. It checks wide and 800px layout, internal heatmap scroll, a stable Sankey slot, a body tooltip with no run or attempt id and no injected image, closed disclosures, information Enter/Escape without moving `scrollY`, and held-refresh geometry within 2px. Completion waits for a new `data-token-observed-at`. A 12-model run replaced by a one-model run keeps panel height, chart-viewport height, document height, scroll position, and viewport position through the held response and after that response is applied. | F-05 round: **5 passed in 22.4s** in `token-analytics.spec.ts`, plus the other **10 passed in 53.3s**. |
| T292-06 | Docs: `docs/referencia/dashboard.md`, `docs/referencia/integration-api.md`, `docs/referencia/cli.md`, and `docs/operacion/dashboard-local.md` describe the implemented cohort charts, the separate direct-run read, local facets, and unsupported-as-unavailable. `npm run build` writes `src/ai_dev_loop/dashboard/web_dist`. `scripts/verify-phase26-dashboard.py` expects API minor 13, `dashboardTokenUsage`, a real cohort read, a real direct read, and the asset markers `Token flow` and `Selected run consumption`. The walkthrough is `archive/implementation-history/plans/phase-29-manual-walkthrough.md` with status `MANUAL_PENDING`. | The installed wheel serves those assets and both token scopes without Node. The e2e overlay is only in `dashboard-web/e2e/token_fixture.py`; the installed server uses the real query. | Strict MkDocs and the installed pytest below. |

The browser fixture overlay runs only after the real `token_usage()` call
succeeds, and only for `map-project` or all projects when the cohort is
non-empty. `NOT_FOUND`, empty cohorts, and filter validation stay on the real
service. A catalog import check printed `363 140 115 12`.

## Validation

System `npm` is absent. Frontend commands used Node v24.11.1 from
`/tmp/ai-dev-loop-dashboard-polish-tools/node-v24.11.1-linux-x64/bin`.
Browser launches used `PLAYWRIGHT_BROWSERS_PATH` and `LD_LIBRARY_PATH` from
that same tooling directory. Python commands set
`UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache`.

```bash
# dashboard-web, PATH prefixed with the Node 24.11.1 bin
npm run typecheck
```

Result: **exit 0**.

```bash
npm test -- --run
```

Result: **exit 0, 15 files, 87 passed, 8.47s**.

```bash
npm run build
```

Result: **exit 0**. Vite wrote `index.html` 0.45 kB, `dashboard.css` 11.65 kB,
and `dashboard.js` 968.01 kB, then `scripts/write-build-manifest.mjs` exited 0.
The manifest `schemaVersion` is 1 and lists `TokenAnalytics.tsx`,
`RunTokenConsumption.tsx`, and `tokenCharts.ts`. The later installed accept
runs `npm run build` again and validates that manifest before packaging.

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-dashboard-polish-tools/browsers \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-dashboard-polish-tools/libs/root/usr/lib/x86_64-linux-gnu \
npx playwright test e2e/token-analytics.spec.ts e2e/linked-inspection.spec.ts \
  e2e/phase28-refinements.spec.ts e2e/refresh-lifecycle.spec.ts
```

Result: **exit 0, 12 passed in 59.8s**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache \
PHASE26_NODE_BIN=/tmp/ai-dev-loop-dashboard-polish-tools/node-v24.11.1-linux-x64/bin \
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-dashboard-polish-tools/browsers \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-dashboard-polish-tools/libs/root/usr/lib/x86_64-linux-gnu \
uv run --no-sync python -m pytest -q \
  tests/integration/test_phase26_7_installed_dashboard.py \
  tests/integration/test_phase29_1_token_queries.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
```

First result, before the installed-spec correction: **exit 1, 2 failed, 14
passed in 233.84s**. Both failures were
`test_c267_1_and_c267_3_installed_acceptance_real_server_browser_and_package`
on Python 3.11 and Python 3.13. The API, direct token query, and asset checks
had already passed. The installed Playwright journey timed out at 60s waiting
for the source run's attempt button.

After the correction below, the same command: **exit 0, 16 passed in 143.41s**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m mkdocs build \
  --strict --site-dir /tmp/ai-dev-loop-phase29-2-docs
```

Result: **exit 0**, documentation built in 0.88s. The Material theme printed
its existing MkDocs 2.0 warning. Strict mode did not fail.

```bash
git diff --check
```

Result: **exit 0**. Untracked widget, fixture, and walkthrough files have no
trailing whitespace or conflict markers.

The repository-wide suite was not run.

## Correction

The installed journey opens attempt details for the replacement run, then
selects the source run and clicks an attempt row. `AttemptTimeline` remounts
that disclosure closed because the element key is the selected run id. The
source-run button was therefore absent from the accessibility tree. Production
timeline behavior is unchanged.

`dashboard-web/e2e/installed-acceptance.spec.ts` now clicks
`.attempt-details-disclosure > summary` after selecting the source run, then
continues the existing stdout and active-output assertions. A browser-only
harness run after that change reported `PASS browser installed offline
journey=1` and `READY_FOR_REVIEW elapsed=34.94s cases=browser`.

## Correction round F-01..F-06

The counts above this section that say 87 unit tests or 12 Playwright tests
are the pre-correction implementation run. This round supersedes them. Backend
aggregation, schemas, and scheduler orchestration were not changed.

| ID | Result | Production path and evidence |
| --- | --- | --- |
| F-01 | Fixed | `TokenAnalytics` keeps the last cohort observation and its facets only when `sameTokenScope` matches the applied filters. An incompatible scope clears both immediately and shows the loading or error state. Chart slots keep their reserved height. `TokenAnalytics.test.tsx` covers a same-scope hold, a failed refresh, and a late scope response that must not reappear. |
| F-02 | Fixed | `tokenHeatmapOption` puts numeric cells on series `measured` and null cells on series `unmeasured`. The measured `visualMap` uses `seriesIndex: 0`. The unmeasured series has its own hidden constant map, so unknown cells paint as `#d9d3c5` and stay off the measured scale. Null `exact` remains null; a measured 0 stays 0 and uses `#e7eef6`. `tokenCharts.test.ts` initializes pinned ECharts, checks both SVG fills, and selects the unknown cell as the unrecorded model. The browser spec clicks the unknown heatmap color and gets `cursor:`, then clicks a measured-zero color and gets a different Cursor row with the role cleared. |
| F-03 | Fixed | Each Sankey and Sunburst model tooltip uses `modelSummary` over every role bucket for that provider and model, including an unknown-only role. `tokenCharts.test.ts` checks a model used by both roles (280, complete) and an implementer plus an unknown role (140, partial) against the filtered total. |
| F-04 | Fixed | `tokenSankeyHeight` sizes the chart from the busiest column at 18px per node and an 8px gap, with a 480px floor. `.token-sankey-scroll` keeps the outer slot at 480px and scrolls internally. `tokenCharts.test.ts` initializes pinned ECharts at 960px and 640px and requires every node `dy` to be positive. The browser spec keeps the Sankey slot height within 2px at 1280px and 800px, with the scrollport at most 480px. |
| F-05 | Fixed | The first correction reserved panel height only while loading or failing, so a successful smaller chart dropped it. `RunTokenConsumption` now uses a fixed `.run-token-frame` and `.run-token-chart-scroll` for idle, loading, ready, error, and zero states. Evidence is in **Correction round F-05**. |
| F-06 | Fixed | The held same-scope browser check waits for a new `data-token-observed-at` before the final geometry assertion. The visible 363 total stays on screen during that hold and is not the completion signal. The spec clicks the real Sunburst model, Sunburst role, unknown heatmap cell, and measured-zero heatmap cell, then compares those selections with the accessible model and role controls. The click sequence adds no `dashboard.token-usage` request and does not change the project, time range, or selected run. A clock-driven test checks the 30s cohort and 10s direct cadence, then Pause and a hidden tab for 60s. A held project change drops 363 immediately. After release, a retry if the replacement read was saturated applies the other project and 363 does not return. |

Correction validation, with the same Node 24.11.1, Playwright browser, and
library paths recorded above:

```bash
npm run typecheck
```

Result: **exit 0**.

```bash
npm test -- --run
```

Result: **exit 0, 15 files, 92 passed**.

```bash
npm run build
```

Result: **exit 0**. Vite wrote `index.html` 0.45 kB, `dashboard.css` 11.75 kB,
and `dashboard.js` 969.61 kB, then `scripts/write-build-manifest.mjs` exited 0.

```bash
npx playwright test e2e/token-analytics.spec.ts e2e/linked-inspection.spec.ts \
  e2e/phase28-refinements.spec.ts e2e/refresh-lifecycle.spec.ts
```

Result: **exit 0, 15 passed in 1.2m**.

```bash
uv run --no-sync python -m pytest -q \
  tests/integration/test_phase26_7_installed_dashboard.py \
  tests/integration/test_phase29_1_token_queries.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
```

Result: **exit 0, 16 passed in 122.02s**. This rerun packages the corrected
assets. The installed accept rebuilds the frontend and validates the manifest.

```bash
git diff --check
```

Result: **exit 0**.

The strict MkDocs build was not rerun. This correction did not change
documentation. The previous strict build remains the docs evidence. The
repository-wide suite was not run.

The F-05 row in the table above records only that first correction. A later
probe showed it was incomplete. The next section is the F-05 result.

## Correction round F-05

The previous correction measured the ready panel and applied that height as
`minHeight` only while `status` was `loading` or `error`. On a successful
replacement `holdGeometry` became false and the inline reservation was removed.
The browser fixture then used two 12-model runs, totals 120 and 240.
`runConsumptionHeight` depends on the bucket count, not the token total, so
both charts had the same height and the success path never shrank. A later
probe selected the 12-model run and then a one-model run: the panel fell from
673.22px during the hold to the 440px CSS floor after success. The panel's
document position stayed put, and document height and `scrollY` each fell by
about 233px.

`RunTokenConsumption` now keeps every selected-run state, including idle, in
`.run-token-frame`. The meta row is 9rem, `.run-token-chart-scroll` is 320px,
and the closed disclosure row is at least 3rem. A 12-bar chart scrolls inside
the viewport. A one-bar chart and an unknown or zero placeholder use the same
slot. The section no longer copies the retired run's height, and an
incompatible selection still clears that run before the response arrives.
Generation fencing, `sameRun`, and unsupported retirement are unchanged.

`TokenAnalytics.test.tsx` keeps the scroll slot across a held replacement, a
late previous-run reply, and a failed read, then applies a zero-total
replacement and checks that 140 does not return. `token-analytics.spec.ts`
loads a 12-model direct run (120) and a one-model replacement (20). It records
panel height, chart-viewport height, document height, `scrollY`, panel document
position, and viewport position before the request, during the hold, and after
the response is applied.

```bash
npm run typecheck
```

Result: **exit 0**.

```bash
npm test -- --run
```

Result: **exit 0, 15 files, 93 passed**.

```bash
npm run build
```

Result: **exit 0**. Vite wrote `index.html` 0.45 kB, `dashboard.css` 12.07 kB,
and `dashboard.js` 969.64 kB, then `scripts/write-build-manifest.mjs` exited 0.
The packaged assets contain `run-token-chart-scroll` and do not contain
`holdGeometry`.

```bash
npx playwright test e2e/token-analytics.spec.ts
npx playwright test e2e/linked-inspection.spec.ts \
  e2e/phase28-refinements.spec.ts e2e/refresh-lifecycle.spec.ts
```

Result: **exit 0, 5 passed in 22.4s**, then **exit 0, 10 passed in 53.3s**.
Together these are the same 15 browser tests as the previous round.

```bash
uv run --no-sync python -m pytest -q \
  tests/integration/test_phase26_7_installed_dashboard.py \
  tests/integration/test_phase29_1_token_queries.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
```

Result: **exit 0, 16 passed in 128.50s**. This rerun packages the corrected
assets. The installed accept rebuilds the frontend and validates the manifest.

```bash
git diff --check
```

Result: **exit 0**.

The strict MkDocs build was not rerun. This correction did not change
documentation. The previous strict build remains the docs evidence. The
repository-wide suite was not run.

## Unfinished mandatory items

The joint manual walkthrough is authored and **MANUAL_PENDING**. It is not
human acceptance. The repository-wide suite remains the separate sequence gate
and was not run. No commit, push, pull request, or merge was made. No timer,
integration install, or live scheduler control was performed.
