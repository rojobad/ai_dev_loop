# Phase 28.3 filters and presentation findings

Status: **READY_FOR_REVIEW**. This is an implementation/evidence label only. It
does not authorize acceptance, staging, a sequence checkpoint, installation in
the operator environment, or any Git publication action.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, starting HEAD
`7a3b680` (accepted Phase 28.2 checkpoint). `d6a837f` (Phase 28.1) and accepted
Phase 26 baseline `67b8edca5f1868e3263239694edc3e708c94f243` were verified as
ancestors before dependent work. The initial worktree was clean. The active and
shared Phase 28 plans have no OpenQuestions.

The implementation adds immediate project/preset selection, a 400 ms coherent
typed-filter debounce, a runs-only 1000-page contract, typed fixed-envelope
overflow, automatic adaptive cohort loading, shared Information dialogs,
on-demand daily values, and body-level viewport-confined chart tooltips. API
minor 1.11 and the public schemas/parsers document the additive contract. The
fixed 240 KiB/5-second/10k-run/100k-attempt/content/read-concurrency limits and
the other collection maxima remain unchanged.

All automated data came from temporary SQLite/XDG fixtures and fake providers.
No live ledger or protected artifact was opened or migrated, no model was
invoked, and no checkout/live package, integration, timer, or workstation
configuration was installed or changed. Sensitive prompt/review/output bytes
remain deliberate, bounded, memory-only reads, and filter/run/modal retirement
keeps their existing abort and generation fences.

## Contract evidence

| ID | Real production caller/path | Independent caller/test evidence | Exact result |
| --- | --- | --- | --- |
| C283-1 | `DashboardApp` project/chart controls call `selectProject` immediately; presets pass through `presetDraft` and `commitFilterDraft`; typed date/time-zone state commits through one 400 ms effect only after a coherent valid draft. Scope/revision keys retire summary, map, activity, selected-run and content ownership through `RefreshCoordinator`; Refresh re-observes the last valid current scope, including while paused. | `DashboardApp.test.tsx` uses fake timers and held old observations to prove 399 ms has no read, 400 ms has one read, a project chosen at 399 ms survives the typed commit, presets have no delayed duplicate, invalid drafts retain the previous view/message, immediate project selection clears exact run ownership, and old replies cannot restore it. `refreshCoordinator.test.ts` proves max-two active reads plus Pause/hidden/retire cancellation. Real browser coverage observes no valid UTC request at 300 ms, automatic commit after 400 ms, immediate project selection, no Apply button, and no request for an invalid draft. Existing refresh/sensitive-content browser scenarios remain in the full suite. | Complete Vitest: **13 files / 71 tests passed in 6.59s**. Complete Playwright: **12 passed in 48.9s**. TypeScript typecheck: **exit 0**. |
| C283-2 | `DashboardPage.parse_runs` owns the sole 1..1000 exception; CLI `dashboard runs`, `IntegrationDashboardReadService.runs`, HTTP dispatch, `DashboardRunsPageData`, JSON Schema `runsPage`, and the browser runs parser use it. Projects/timeline continue through the 1..100 path. `_bounded` retains 240 KiB and maps only an oversized run page to `PAGE_ENVELOPE_TOO_LARGE`; HTTP maps it to 413 and the generic API envelope remains additive-compatible. | `test_phase28_dashboard_paging.py` uses a real fixture ledger for service/CLI/HTTP acceptance at 1000 and rejection of 1001/zero/bool, independently rejects projects/timeline 101, validates Pydantic/JSON Schema, and uses a bounded fake repository whose real encoded response exceeds 240 KiB to prove typed service error and HTTP 413. Parser/data-source tests accept runs 1000, reject 1001, retain other max100, and propagate the typed code without reading message text. | Focused Python contract/regression selection: **86 passed in 40.81s**; API-version/capacity regressions: **19 passed in 18.98s**. Frontend parser/data-source coverage is included in **71/71**. API minor is **1.11**; envelope cap assertion remains exactly **245,760 bytes**. |
| C283-3 | `RunMap.readAdaptiveRunsPage` begins at 1000, halves only typed page overflow at the same offset down to 1, verifies returned offset/limit, and continues from the producer's exact `nextOffset` using its returned limit. `requestRefreshContinuation` keeps every page inside the coordinator's two-slot, timeout, owner, Pause/visibility and generation lifecycle. Any successful same-scope row establishes preserved history, even before a chain completes. Each rebuilding chain merges fresh rows by exact ID over that visible history; only terminal success replaces the cohort with the chain's exact ID set and reconciles obsolete IDs away. Held, failed or cancelled continuation work therefore retains every previously successful row, and bounded Retry remains generation-owned. Load more is absent. | `RunMap.test.tsx` loads **1005 unique IDs** with variable thresholds and asserts the exact sequence `1000,500,250 @ offset 0; 250 @ 250; 250,125 @ 500; 125 @ 625/750/875/1000`. One correction regression covers the complete-cohort 3-to-1 contraction and 1-to-3 expansion. The added regression starts with no complete snapshot: offsets 0 and 1 succeed, offset 2 fails, the scheduled automatic poll refreshes offset 0 then holds/fails offset 1, and the UI remains `Loaded 2 of 3` with fresh A, cached B and one option per exact ID. Retry completes all three fresh rows. Existing tests separately exercise replacement generations, large cohorts, late cancellation/fencing, incompatible scope clearing and the two-read coordinator bound. The real fixture loads **1036 unique IDs** and observes `(0,1000)->413`, `(0,500)->200`, `(500,500)->200`, `(1000,500)->200`. | Included in Vitest **71/71** and complete Playwright **12/12**. The real browser reports `Loaded 1036 of 1036 started runs`, summary total 1036, and 1037 selector options including the empty option. |
| C283-4 | Shared `InformationControl` portals an `aria-modal` dialog to `body`, makes other body children inert for the dialog lifetime, owns `aria-expanded`, focuses Close, contains forward/reverse Tab, handles Escape/backdrop/Close, restores the trigger without scrolling, and removes listeners, inertness and its portal on unmount. Inspection scope, both overview charts, capacity, run map, attempt timeline and activity calendar use it for instructions/definitions/provenance; headings, progress, freshness, coverage, warnings and actionable errors remain visible. | `InformationControl.test.tsx` independently proves initially closed state, keyboard open, dialog labelling, background inertness, Close focus, forward Tab, wraparound Tab, Shift+Tab, Escape, inert cleanup and trigger restoration. Real-browser coverage enumerates all seven controls and proves forward/reverse containment, root inertness, programmatic background focus rejection, Escape cleanup and trigger restoration. | Included in Vitest **71/71** and Playwright **12/12**. |
| C283-5 | `ActivityCalendar` keeps the ECharts calendar/metric selector and renders the equivalent daily list only inside native `<details>`. `DashboardChart.withViewportTooltip` appends every HTML tooltip to `body`, disables chart-dimension confinement, and supplies a position callback that translates chart-local points through the chart's current viewport rectangle, clamps all edges to `window.innerWidth/innerHeight`, and returns the chart-local coordinate ECharts requires. The viewport max width, z-index 900, escaping, theme behavior and disposal remain intact. | Calendar/chart unit tests cover all metrics, selected-zone day labels, human process durations, closed daily disclosure, immutable tooltip wrapping, scrolled-chart coordinate math and disposal. The real ECharts scenario scans actual pixels and bounds-checks the first calendar row plus the far-right cell of the **45k+ pixel horizontally scrollable run map** at **1095x884** and **390x844**, in both light and dark themes. Every actual body-level tooltip remains inside the browser viewport above panel/modal layers. | Included in Vitest **71/71** and Playwright **12/12**. |
| C283-6 | Maintained CLI/API/Dashboard/operator docs now describe API 1.11, operation-specific limits, automatic/adaptive paging, current-scope refresh, Information controls and tooltip/daily presentation. Vite regenerated same-origin `web_dist` plus the source/inventory manifest. `verify-phase26-dashboard.py` builds wheel/sdist, installs the wheel into an owned temporary venv, starts the installed loopback server against an isolated named fixture, probes API/bounds/assets, runs the installed browser journey, compares authority hashes, and removes its owned resources. | Strict docs, build guard, static asset markers, named DTO/lineage, 1000/100 bounds, deliberate sensitive reads and real installed browser behavior are checked through the maintained harness; the focused service/HTTP test owns the induced 413 check. The final manual walkthrough contains the current `integration info`, `dashboard serve`, manifest-v3 prepare and metadata rename command surfaces and remains explicitly operator-pending. | Corrected Vite bundle: **657 modules in 4.71s, exit 0** (only the existing >500 kB advisory). `python -m build`: wheel and sdist built successfully, **exit 0**. Corrected installed acceptance: `PASS packaging`, `PASS api runs=27 reviews=3 timeline=26`, `PASS browser installed offline journey=1`, `PASS safety authority=unchanged runtime=loopback/no-node`, `READY_FOR_REVIEW elapsed=30.77s`; Python **3.11.15**. Strict MkDocs: **exit 0 in 0.84s**. |

## Corrected findings during implementation

- The first complete browser run exposed stale selectors for the now-hidden daily
  disclosure and retired explanatory text. Assertions were moved to the visible
  activity total and exact selected-run metadata; no behavior check was removed.
- The same run showed that the first automatic-paging implementation discarded
  successful map pages before a failed same-scope Refresh completed. `RunMap` now
  preserves compatible successful rows until a replacement chain reaches
  terminal success; dedicated complete- and partial-cohort unit regressions and
  the existing real-browser scenarios pass.
- A code review found intentional continuation cancellation could be displayed
  as an error and a slow continuation could race its next first-page poll.
  Cancellation is now treated as expected partial progress and the coordinator
  defers the next poll until owned continuation work finishes. New coordinator
  coverage passes.
- The final filter audit found that an older typed-date timer could fire just
  after an immediate project selection and restore its captured project. The
  debounced commit now updates only typed date/time-zone fields while preserving
  the latest applied project. A 399/400 ms fake-timer regression passes.
- Two virtual-clock browser checks could advance at the exact boundary before
  the fixture worker/promise-finally had released or scheduled work. Each now
  drains that explicitly bounded fixture boundary before advancing virtual time;
  the production cadence was not weakened. The final complete browser run is
  **12/12**.
- Initial Ruff reported only two import-order findings in the new Python fixture
  and integration test. Ruff's deterministic import fix was applied; the final
  repository Ruff check passes.

## Codex correction findings

| Finding | Disposition | Production correction | Exact caller/test evidence |
| --- | --- | --- | --- |
| F-01 / C283-5 | **Fixed** | `dashboard-web/src/dashboard/DashboardChart.tsx` now uses the live chart rectangle and browser viewport in a custom tooltip position callback. It clamps body-appended HTML overlays after horizontal/vertical scrolling instead of applying ECharts' chart-width `confine`; escaping, max width, z-index 900, themes and disposal are unchanged. | `DashboardChart.test.tsx` proves the scrolled 1388+-pixel chart-coordinate translation at a 390-pixel viewport and the wrapped option. `phase28-refinements.spec.ts` scans real canvas pixels and bounds-checks calendar-first-row and horizontally scrolled far-right map tooltips at 1095x884 and 390x844 in both themes. Complete browser suite remains **12 passed in 48.9s**. |
| F-02 / C283-3 | **Fixed** | The previous correction used `hasCompleteSnapshot`, so its preservation branch and regression began from a terminally complete cohort; successful rows from the initial incomplete chain were still replaceable. `dashboard-web/src/dashboard/RunMap.tsx` now records any successful same-scope row as preservation history. A rebuilding chain merges fresh exact IDs over that history, and only terminal success reconciles obsolete IDs away. Retry, adaptive limits, generation/cancellation ownership and the two-read bound are unchanged. | The new automatic-poll regression begins with no completed chain, succeeds at offsets 0/1 and fails at 2, then holds/fails offset 1 after the scheduled next offset-0 poll. It proves `Loaded 2 of 3`, fresh-metadata precedence, exact-ID deduplication and successful three-page Retry. The complete-cohort contraction/expansion, **1005-ID variable-envelope**, replacement-generation and cancellation tests remain green. Focused RunMap: **1 file / 12 tests in 4.24s**; affected RunMap/coordinator/filter/parser/data-source selection: **5 files / 37 tests in 4.67s**; complete frontend: **13 files / 71 tests in 6.59s**. |
| F-03 / C283-4 | **Fixed** | `dashboard-web/src/dashboard/InformationControl.tsx` makes all background body children inert while open and contains Tab/Shift+Tab within the portal dialog, with cleanup restoring prior inert state. Escape, Close, backdrop dismissal, initial Close focus, trigger restoration and unmount cleanup remain. | `InformationControl.test.tsx` proves forward and reverse wrapping, background inertness, Escape, cleanup and restored trigger focus. The real browser proves `#root` is inert, Tab/Shift+Tab remain on the only focusable control, and a programmatic background-focus attempt cannot leave the dialog. Included in frontend **71/71** and browser **12/12**. |

## Exact validation commands and results

```bash
# dashboard-web; remaining F-02 correction and affected callers
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npm test -- --run src/dashboard/RunMap.test.tsx
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npm test -- --run \
  src/dashboard/RunMap.test.tsx \
  src/dashboard/refreshCoordinator.test.ts \
  src/dashboard/DashboardApp.test.tsx \
  src/dashboard/parsers.test.ts \
  src/dashboard/local-fetch-data-source.test.ts
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npm run typecheck
```

Results: RunMap **1 file / 12 tests passed in 4.24s**; affected selection
**5 files / 37 tests passed in 4.67s**; typecheck **exit 0**.

```bash
# dashboard-web; correction-focused unit/type gate
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npm test -- --run \
  src/dashboard/DashboardChart.test.tsx \
  src/dashboard/RunMap.test.tsx \
  src/dashboard/InformationControl.test.tsx
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npm run typecheck
```

Result: **3 files / 14 tests passed in 4.01s**, then typecheck **exit 0**.

```bash
# dashboard-web; correction-focused real Chromium contract
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase263-playwright \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase263-browser-libs/root/usr/lib/x86_64-linux-gnu \
npx playwright test e2e/phase28-refinements.spec.ts
```

Result: **1 passed in 18.5s, exit 0**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run python -m pytest -q \
  tests/integration/test_phase28_dashboard_paging.py \
  tests/unit/dashboard \
  tests/unit/integration_api/test_contract.py \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py \
  tests/integration/test_phase26_7_installed_dashboard.py::test_c267_3_docs_and_handoff_match_public_contract
```

Result: **86 passed in 40.81s, exit 0**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run python -m pytest -q \
  tests/integration/test_phase21_1_integration_contract.py \
  tests/integration/test_phase21_6_capacity.py
```

Result: affected API-version/capacity regressions **19 passed in 18.98s, exit
0**.

```bash
# dashboard-web; Node v24.11.1 from an owned /tmp runtime
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH npm test -- --run
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH npm run typecheck
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH npm run build
```

Results: complete Vitest **13 files / 71 tests passed in 6.59s**; typecheck
**exit 0**; production build **657 modules in 4.71s, exit 0**, followed by a
regenerated manifest. Vite emitted only its existing chunk-size advisory.

The first relevant-browser selection (`phase28-refinements.spec.ts` plus
`refresh-lifecycle.spec.ts`) finished **5 passed / 1 failed** because the
unchanged cadence case's initial fixture capacity read returned `An internal
error occurred.` The exact cadence case was immediately rerun in a fresh
isolated fixture with `--grep "actual HTTP reads obey queue"` and passed
**1/1 in 6.5s**. No production or test change was made in response. The final
complete browser command below independently includes and passes that case.

```bash
# dashboard-web; exact Chromium and shared libraries are under owned /tmp roots
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase263-playwright \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase263-browser-libs/root/usr/lib/x86_64-linux-gnu \
PATH=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin:$PATH \
npx playwright test
```

Result: complete real-browser suite **12 passed in 48.9s, exit 0**, one worker.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run python -m ruff check .
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run python -m mypy src
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run mkdocs build --strict \
  --site-dir /tmp/ai-dev-loop-phase283-mkdocs-site
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache uv run python -m build \
  --outdir /tmp/ai-dev-loop-phase283-build-final-Q63KA0
```

Results: Ruff **exit 0**; MyPy **204 source files, exit 0**; strict MkDocs
**exit 0** with only the existing Material notice and unnav-listed incident-page
notices; wheel and sdist build **exit 0**.

```bash
PHASE26_NODE_BIN=/tmp/ai-dev-loop-phase283-node/node-v24.11.1-linux-x64/bin \
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase263-playwright \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase263-browser-libs/root/usr/lib/x86_64-linux-gnu \
UV_CACHE_DIR=/tmp/ai-dev-loop-phase283-uv-cache \
.venv/bin/python scripts/verify-phase26-dashboard.py accept \
  --python .venv/bin/python \
  --case packaging --case api --case browser --case safety
```

Result: all four cases passed, final `READY_FOR_REVIEW`, **30.77s**, installed
Python **3.11.15**.

## Preserved boundaries and unfinished checks

- The repository-wide pytest suite is **not run**. The shared Phase 28 contract
  assigns that single gate to the final integration/pipeline stage; focused and
  browser results do not imply repository-wide validation.
- The final Phase 28 operator walkthrough and real workstation/package update
  are **pending**. Automation is not manual acceptance.
- No live provider capacity/authentication/billing, model invocation, scheduler
  execution/recovery, timer, WSL, integration, operator ledger, or protected
  artifact behavior was exercised.
- The root configuration, rules, skills, frozen sequence inputs, scheduler
  identities/lineage and native sequence authority were not changed. No commit,
  push, PR, merge, staging, destructive Git action, or live cleanup was performed;
  the native sequence owns staging and checkpoints.
