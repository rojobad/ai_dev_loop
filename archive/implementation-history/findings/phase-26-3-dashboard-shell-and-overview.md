# Phase 26.3 dashboard shell and overview findings

Status: **BLOCKED_CONTRACT** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`20672c5ac0f325f6e664c30b66d943a91a1dca8f`. Initial Git status was clean.
Implementation used the accepted Phase 26.1/26.2 and Phase 27.1-27.7 production
baseline: Integration API 1.9, SQLite schema 16, explicit dashboard DTOs,
provider-aware attempts, historical Cursor defaults, current role/retry metadata
and authoritative `implementer_retry` lineage. Phase 24 and Phase 25 were not
used as dependencies. The plan's API 1.8 reference is the pre-26.1 version; the
accepted dashboard-capable source is the compatible 1.9 minor and was not
downgraded.

The implementation replaces the provisional page with a locally bundled React
18/ECharts 6 application. It adds typed same-origin transport/parsers, named-zone
filters, complete summary KPIs and charts, current capacity gauges, accessible
chart companion controls, responsive light/dark presentation, deterministic
frontend tooling and a real production-server browser test. The Python runtime
serves only the fixed package manifest; Node is build/test-only.

No scheduler run was submitted, started, ticked, retried, extended, aborted or
otherwise controlled during implementation. No real Cursor/Codex model turn or
capacity probe was made. No timer, root YAML, review schema, scheduler schema,
installed operator runtime, external Git state, commit, push, PR or merge was
changed. Tests used only temporary owned state, a deterministic SQLite fixture
and a typed fake capacity provider. The frontend has no sensitive-content action
in this slice and introduces no content persistence; the only browser-persisted
value is theme preference.

## Admission and prerequisite evidence

The accepted prerequisite was checked once before dependent implementation.
Source inspection established API 1.9, schema 16, the accepted Phase 26.1 query
and Phase 26.2 server surfaces, all provider-aware Phase 27 execution effects,
reviewer-only review ownership/content, and current role/retry/lineage fields.
The first command using uv's default home cache stopped before collection with
**exit 2** because that cache was read-only; no prerequisite test executed. The
same selection was then run once with an owned cache:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase263-uv-cache uv run pytest -q tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/integration/test_phase27_3_inspection.py
```

Result: **exit 0, 29 passed in 42.44s**. Dependent work began only after this
pass. The selection independently retains Codex implementer execution/start/time
ownership, reviewer-only round/content classification, provider/role metadata,
historical Cursor fallback and accepted read-only HTTP query behavior. No
prerequisite was missing or incompatible and the plan has no unresolved
OpenQuestion.

The host had no Node executable. Tooling was repaired only in owned temporary
paths: official Node **v24.22.0** was unpacked below
`/tmp/ai-dev-loop-phase263-node`, and its archive SHA-256
`cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008`
matched the downloaded official `SHASUMS256.txt`. npm used
`/tmp/ai-dev-loop-phase263-npm-cache`; Playwright used
`/tmp/ai-dev-loop-phase263-playwright`. Missing browser shared libraries were
downloaded as Ubuntu packages and extracted, not installed, below
`/tmp/ai-dev-loop-phase263-browser-libs`.

## Correction disposition

- **F-01 — FIXED.** The finding was confirmed: the original tests exercised only
  one workload root total, the standalone `escapeHtml` helper and unmount
  disposal, so the prior evidence claims exceeded the assertions. The corrected
  tests now invoke production `projectStateOption` and independently assert all
  seven state series for two projects; invoke `workloadOption` and assert exact
  project, sequence and Standalone leaf names/counts; invoke both production HTML
  tooltip formatters with malicious projected labels; directly assert
  `projectFromChartEvent`; prove keyboard and chart actions each apply the same
  global project filter; drive actual ResizeObserver callbacks; and prove the
  observer/chart instance replaced by a theme change is disconnected/disposed
  before the current instance is resized and later retired on unmount. The tests
  confirmed the production implementations; no production source or built asset
  changed. Final correction evidence is **6 files / 17 tests passed**, TypeScript
  passed, and the real-server browser smoke passed **1/1**.
- **F-02 — BLOCKED.** The accepted public `DashboardSummaryData` producer in
  `integration_api/dashboard_models.py` and its schemas exposes only the integer
  `projects[].stateCounts.other`; it does not expose the raw state strings or
  per-raw-state counts required to make exact unknown values visible under Other.
  Existing `dashboard.runs` does expose each page item's raw `state`, but obtaining
  a complete exact set would require silently paging the full started cohort.
  That would broaden this overview slice into the Phase 26.4 map consumer and
  violate the shared refresh rule against silently scanning every historical
  page. Private SQLite/artifact inspection, guessed labels and partial-page labels
  are not authoritative alternatives. Dependent raw-state display work therefore
  stopped. An external scope decision is required: either (a) authorize an
  additive aggregate such as `otherStates[{state,count}]` in the Phase 26.1
  summary producer/public DTO/schema and then consume/test it here, or (b)
  explicitly amend/defer the requirement so Phase 26.3 promises only the
  authoritative aggregate Other count and raw states remain in the explicitly
  paged Phase 26.4 run map. No scheduler control was invoked.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact result |
| --- | --- | --- | --- |
| C263-1 | package `web_dist/index.html` -> `main.tsx` -> `DashboardApp` -> `DashboardDataSource` -> `LocalFetchDashboardDataSource` -> same-origin `POST /api/dashboard/read`; `time.ts` owns named-zone draft conversion and Apply/Refresh; `DashboardApp` owns catalog paging, AbortController and generation retirement | `DashboardApp.test.tsx` resolves two summary calls in reverse order and proves the retired reply cannot restore the old scope; an invalid reversed range issues no read and retains the prior observation. `time.test.ts` independently covers default/presets, DST nonexistent/ambiguous rejection, explicit-offset resolution and range bounds. `local-fetch-data-source.test.ts` asserts exact same-origin operations/envelopes, received time and safe failures. `overview.spec.ts` launches the real production server over a temporary fixture ledger and observes an independently expected three-run UTC cohort after editing/applying the project/range controls. | Frontend unit selection: **6 files / 17 tests passed in 2.30s**. Real browser: **1 passed in 2.7s**. Affected Python producer/server selection: **65 passed in 38.57s**. |
| C263-2 | accepted `dashboard.summary` DTO -> `parseSummaryData` consistency checks -> `SummaryPanels` -> four KPIs, `projectStateOption`, `workloadOption` and global `selectProject` | `DashboardApp.test.tsx` independently asserts 7 started, 2 completed, 1 residual-risk and visible aggregate Other count, then proves keyboard and chart activation each apply `project-b`. `charts.test.ts` invokes production projections and asserts every expected state series, exact sequence/Standalone leaves and counts, both actual tooltip formatter boundaries with malicious labels, and production event mapping. Parser tests reject internally inconsistent project/workload/KPI totals rather than charting partial page-derived values. The real-server fixture includes accepted Cursor and Codex-implementer attempts, while the rendered cohort count remains run-based and no reviewer-content call exists. | Aggregate/count/chart behavior is **17/17 frontend, 1/1 browser and 65/65 Python pass**. Exact raw unknown state labels are **BLOCKED** because the accepted summary DTO supplies only `other`; C263-2 is not fully complete pending the F-02 scope decision. |
| C263-3 | `DashboardApp` starts `DashboardDataSource.capacity` once after initial catalog settlement -> existing `machine.codex-capacity` service -> `CapacityPanel`/`capacityOption`; the call has no project/time parameters or summary refresh dependency | `DashboardApp.test.tsx` asserts both returned windows, exact 64/36 and 27/73 remaining/used values, explicit unavailable reset, one capacity call after filter selection and summary Refresh, and unavailable status without synthetic zero. `local-fetch-data-source.test.ts` asserts the capacity request has `{}` parameters. `overview.spec.ts` uses a typed fake service through the real dispatcher/server and observes both windows without a model/account process. | Included in final frontend **17/17 pass** and browser **1/1 pass** results; no real capacity probe was executed. |
| C263-4 | Vite fixed-output build -> package-owned manifest `/`, `/assets/dashboard.js`, `/assets/dashboard.css`; modular ECharts registrations -> `DashboardChart` ResizeObserver/disposal; React text plus escaped HTML-mode tooltips; responsive CSS and theme-only localStorage | `DashboardChart.test.tsx` drives the production observer callbacks, verifies the current instance resizes, then proves theme replacement disconnects/disposes the retired ownership and unmount retires the replacement. `charts.test.ts` invokes both production tooltip formatters with malicious projected project/sequence labels and asserts exact escaping. `test_server_policy.py` fetches the real built JS/CSS with exact MIME/policy behavior. `overview.spec.ts` proves zero external requests, literal malicious content without image/script effects, theme switching and no 390px horizontal overflow. | Corrected frontend assertions: **17/17 pass**. Build remains **exit 0**, 648 modules, HTML 0.45 kB (0.27 gzip), CSS 4.56 kB (1.57 gzip), JS 762.17 kB (257.28 gzip), 4.51s Vite / 4.98s wall. Corrected browser: **1/1 pass**. No production source changed, so packaged assets were not regenerated. |

## Implemented boundaries

- `dashboard-web/package.json` pins React/DOM 18.3.1, ECharts 6.1.0,
  echarts-for-react 3.0.6, Vite 6.4.4, TypeScript 5.9.3, Vitest 5.0.5 and
  Playwright 1.64.1. `package-lock.json` freezes the resolved graph. A final
  `npm ci` audited 157 packages with zero known vulnerabilities.
- The build has fixed same-origin JS/CSS names, no source map and no CDN/font/
  network runtime dependency. The static server's closed manifest explicitly
  exposes only those files; each is below the accepted 10 MiB static-asset bound.
  The sdist source allowlist now includes `dashboard-web`; final isolated wheel
  verification remains assigned to 26.7.
- Components depend only on the typed `DashboardDataSource`. The fetch adapter
  alone knows the fixed relative API path and projects the outer observation into
  `{source: local, observedAt, receivedAt, data}`. It stores no response and knows
  no SQLite, artifact, Hub, auth or localhost configuration.
- Project catalog pages use limit 100 until authoritative `hasMore=false`.
  Summary scope changes clear prior-scope observations, abort old work and use a
  generation check; same-scope Refresh may retain the labelled last successful
  observation on error. Invalid/ambiguous/nonexistent local drafts remain
  uncommitted. Presets use named-zone local calendar days and `[from,to)`.
- Summary parsers require complete project/workload/state/KPI arithmetic. The
  accepted summary DTO makes unknown additive states visible only as the explicit
  aggregate Other count. It does not supply their raw values, so the UI does not
  invent them; exact raw visibility remains the F-02 blocked contract rather than
  a completed behavior. Treemap grouping uses public sequence binding fields and
  never parses IDs.
- Capacity is a separately observed current value, starts only after the initial
  catalog read has released one of the server's two service-job slots, and is not
  reissued by project/range changes. Every returned window, reset, duration,
  used/remaining percentage and observation time is displayed; missing values
  say unavailable.
- ECharts uses only bar, treemap, gauge, grid, legend, tooltip and Canvas modules.
  The wrapper owns resize observation and instance disposal. Companion buttons
  provide keyboard-equivalent project selection. Source strings are React text or
  escaped before ECharts HTML tooltips; malicious fixture data never becomes DOM.

## Exact focused validation

The mandatory frontend commands used `dashboard-web` as cwd and the owned Node,
npm and browser caches.

```bash
env PATH=/tmp/ai-dev-loop-phase263-node/node-v24.22.0-linux-x64/bin:$PATH npm_config_cache=/tmp/ai-dev-loop-phase263-npm-cache npm ci
```

Result: **exit 0**, 156 packages added / 157 audited / 0 vulnerabilities,
**6.69s wall**. npm reported an informational allow-scripts warning for esbuild;
the subsequent clean build executed successfully.

```bash
env PATH=/tmp/ai-dev-loop-phase263-node/node-v24.22.0-linux-x64/bin:$PATH npm_config_cache=/tmp/ai-dev-loop-phase263-npm-cache npm run typecheck
```

Correction-final result: **exit 0**, TypeScript no-emit check passed,
**3.26s wall**.

```bash
env PATH=/tmp/ai-dev-loop-phase263-node/node-v24.22.0-linux-x64/bin:$PATH npm_config_cache=/tmp/ai-dev-loop-phase263-npm-cache npm test -- --run
```

Correction-final result: **exit 0, 6 files / 17 tests passed in 2.30s**
(**2.89s wall**).

```bash
env PATH=/tmp/ai-dev-loop-phase263-node/node-v24.22.0-linux-x64/bin:$PATH npm_config_cache=/tmp/ai-dev-loop-phase263-npm-cache npm run build
```

Result: **exit 0, 648 modules transformed, built in 4.51s** (**4.98s wall**).
Output: `index.html` 0.45 kB / 0.27 kB gzip,
`assets/dashboard.css` 4.56 kB / 1.57 kB gzip and
`assets/dashboard.js` 762.17 kB / 257.28 kB gzip. Vite's >500 kB chunk notice is
informational; the single local overview bundle remains far below the server's
10 MiB per-asset bound. Python serves these exact committed resources through
`DEFAULT_STATIC_MANIFEST`, not a Vite development server.

```bash
env PATH=/tmp/ai-dev-loop-phase263-node/node-v24.22.0-linux-x64/bin:$PATH npm_config_cache=/tmp/ai-dev-loop-phase263-npm-cache PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase263-playwright LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase263-browser-libs/root/usr/lib/x86_64-linux-gnu npx playwright test e2e/overview.spec.ts
```

Correction-final result: **exit 0, 1 passed in 2.7s** (**3.59s wall**). The test starts the real
`DashboardServer`/dispatcher with an owned SQLite ledger and fake capacity
service, serves the production bundle and cleans its state root and process.

Affected Python producer/static/server tests:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase263-uv-cache uv run pytest -q tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py
```

Result: **exit 0, 65 passed in 38.57s** (**39.16s wall**).

Python lint/type checks:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase263-uv-cache uv run python -m ruff check src/ai_dev_loop/dashboard/adapters/http.py tests/unit/dashboard/test_server_policy.py dashboard-web/e2e/fixture_server.py
UV_CACHE_DIR=/tmp/ai-dev-loop-phase263-uv-cache uv run python -m mypy src
```

Results: Ruff **exit 0**, `All checks passed!`, **0.23s wall**; MyPy **exit 0**,
no issues in **202 source files**, **0.29s wall**.

Documentation and whitespace checks:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase263-uv-cache uv run mkdocs build --strict
git diff --check
```

Results: strict docs **exit 0**, built in 0.61s (**0.89s wall**); diff check
**exit 0**. MkDocs printed the upstream Material informational banner and the
existing incident-pages-outside-nav list; neither is a strict-build failure.

The measured final focused command set consumed approximately **61.98s wall**,
plus the separate one-time 42.44s prerequisite pytest. This is below the
subphase's 40-minute focused-validation ceiling.

### Corrected development failures

- The first prerequisite invocation used uv's read-only default home cache and
  exited 2 before pytest collection. The owned-cache invocation above is the one
  executed prerequisite verification.
- Initial trial frontend pins produced npm audit advisories in ECharts 6.0,
  Playwright 1.55 and Vitest 3 worker dependencies. They were replaced with the
  compatible exact pins above; the final clean install reports zero
  vulnerabilities.
- The first Vitest development run exited 1 because the include pattern also
  collected Playwright code, the ResizeObserver mock was not constructible, and
  DOM cleanup was implicit. Test discovery was limited to `src/**/*.test`, the
  mock was made constructible and explicit cleanup was added. Corrected affected
  runs passed 13 tests, then 15 after the final unavailable/invariant coverage.
- The first F-01 correction typecheck exited 2 in **3.13s wall** because two test
  helper casts and generic mock types were too broad. The simultaneously run
  focused assertions already passed **3 files / 9 tests in 2.26s** (**2.87s
  wall**). The test-only types were narrowed without production changes; the
  correction-final full typecheck and 17-test suite both pass as recorded above.
- The first Playwright launch exited 1 before running an assertion because the
  owned Chromium lacked `libnspr4`; required libraries were downloaded/extracted
  into the owned cache. The next one-test run failed because three concurrent
  initial reads exceeded the production server's two-job admission boundary;
  capacity now begins after catalog settlement. The next one-test run exposed
  horizontal overflow from the malicious long label at 390px; companion controls
  were bounded. The corrected final browser command passes 1/1.

Every failed affected behavior was rerun after correction. Development-attempt
wall timings were not captured before the final timed gate; no failed mandatory
check is represented as passing or hidden by a narrower final selection.

## Preserved surfaces and unfinished work

The requested protected inputs retain their prior SHA-256 values:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

One mandatory C263-2 behavior is unfinished: exact raw unknown state values
cannot be displayed under Other from the accepted summary producer. F-02 records
the missing field, the unsuitable paged alternative and the external decision
needed; this implementation therefore remains **BLOCKED_CONTRACT** even though
all implemented focused checks pass. The repository-wide pytest suite, final
sdist/wheel build and isolated installed-wheel browser acceptance, real account
capacity observation, operator walkthrough and human visual acceptance were
deliberately **not executed**. The approved plan assigns those combined
integration/package/human gates once to Phase 26.7. Phase 26.4-26.6 still own
run/sequence map and timeline interaction, deliberate sensitive-content
inspection and activity/refresh behavior. Green focused checks do not authorize
human acceptance, final commit or deployment.
