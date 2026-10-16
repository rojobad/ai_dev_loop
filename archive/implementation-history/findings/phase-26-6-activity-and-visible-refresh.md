# Phase 26.6 activity and visible refresh findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, final commit or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`d43d241`. Initial Git status was clean. Accepted Phase 26.1-26.5 production and
the Phase 27.1-27.7 provider-aware baseline were used: Integration API 1.9,
SQLite schema 16, historical missing implementer provider as Cursor, current
pending-role/retry metadata and authoritative `implementer_retry` lineage.
Phase 24 and Phase 25 were not dependencies. No OpenQuestion remained
unresolved.

The implementation replaces the activity placeholder with a typed ECharts
calendar plus accessible daily values for submissions, review rounds and
process hours. It renders backend-owned event-date days, converts process
seconds to hours exactly once for display, and labels named-zone, partial,
coverage and event-date-versus-started-cohort semantics. Cursor and Codex
implementation/correction attempts contribute execution duration but never
review rounds; only the accepted Codex review effects do.

One memory-only `RefreshCoordinator` now owns automatic and deliberate dashboard
reads with a global maximum of two active jobs, canonical keys, bounded timeout,
no identical-key overlap, cancellation and generation ownership. Summary, the
visible first map page and activity use 30 seconds; selected-run inspect and the
visible first timeline page use 10 seconds; current capacity uses 60 seconds.
Historic appended pages never become recurring keys. Pause, hidden visibility,
selection/scope retirement and unmount cancel owned recurring work. Explicit
scope Refresh remains available while paused, while Resume starts one fresh read
per visible key without replaying missed intervals. Slow or failed reads retry
only at their later normal cadence.

Widgets retain only their last successful observation in React memory, with the
original source/receive dates, visible age and refresh error. A new
filter/metric/run generation cannot restore an old reply. Applying a filtered
scope clears the selected run, timeline, modal and held sensitive reader; late
content cannot resurrect it. Prompt/review bodies and stdout/stderr remain
deliberate, paginated calls routed through the same bounded queue and are never
automatic polling keys.

No scheduler transition, migration, reservation, recovery, Git action, real
provider turn, timer, root YAML, review/result schema, operator installation or
live ledger/artifact state changed. SQLite and protected artifacts remain
authoritative and read-only. Automated browser cases used a production loopback
server, temporary owned SQLite/artifact state and fake providers; trace,
screenshot and video recording remained disabled.

## Admission and prerequisite evidence

The accepted prerequisite was checked once before dependent implementation.
History and source inspection confirmed accepted commits `161e6eb` (26.1),
`20672c5` (26.2), `6906762` (26.3), `2899147` (26.4) and `d43d241` (26.5), API
1.9/schema 16, the typed activity producer/HTTP operation, Phase 26.3 filter and
transport DTOs, Phase 26.4 generation/cancellation boundaries, Phase 26.5
content retirement, and Phase 27's exact component/effect role rules. The source
checkout was not used as a live scheduler driver.

The following supplemental admission command was executed once as required:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase266-admission-uv-cache \
  TMPDIR=/tmp TMP=/tmp TEMP=/tmp \
  uv run python -m pytest -q \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py \
  tests/integration/test_phase27_3_inspection.py
```

The tool exposed only a continuing stream of passing progress dots through its
30-second yield and then lost the process/session handle. A later process check
confirmed that pytest was no longer running, but no terminal exit code, count or
elapsed result was recoverable. It is therefore **not reported as a passing test
run** and was not repeated, preserving the once-only admission instruction.
Accepted commit/source evidence was conclusive, and the final mandatory browser
suite independently exercised the real activity/HTTP producer-consumer path.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact executed result |
| --- | --- | --- | --- |
| C266-1 | Accepted `DashboardQueryService.activity` and the read-only SQLite adapter -> allowlisted `dashboard.activity` HTTP operation -> `LocalFetchDashboardDataSource.activity` -> strict `parseActivityData`/typed DTO -> `ActivityCalendar` and ECharts calendar. Project/range/zone/metric/revision form the canonical generation key. | `ActivityCalendar.test.tsx` independently checks leap-day/named-zone boundaries, old-metric fencing, accessible daily values, partial absence and one 5,400-second-to-1.5-hour conversion. Parser/transport tests preserve seconds and exact metric/unit/filter parameters. `refresh-lifecycle.spec.ts` uses the real loopback service and a provider-aware SQLite fixture: 28 submissions, one reviewer round and 4.917 displayed process hours across Cursor/Codex implementer attempts plus a separate Codex review; no sensitive call occurs. | Mandatory unit selection: **2 files / 7 tests passed, 1.17s Vitest / 1.92s elapsed, exit 0**. Real browser acceptance (all C266 contracts): **3 passed in 10.2s Playwright / 11.18s elapsed, exit 0**. Affected parser/transport assertions are included in the **6 files / 29 tests** result below. |
| C266-2 | `DashboardApp` creates the single `RefreshCoordinator`; `DashboardApp`, `RunMap`, `ActivityCalendar` and `AttemptTimeline` register visible recurring keys; page/content/catalog calls use the same coordinator queue, including `CoordinatedDashboardDataSource`. The coordinator owns two slots, timeout, start-to-start cadence, Pause/Resume, visibility, cancellation and normal-cadence failure retry. | `refreshCoordinator.test.ts` uses an independent deterministic runtime to hold jobs and assert at most two active, distinct 30/60-second cadence, no overlap/catch-up, delayed failure retry, hidden/Pause/unmount retirement, explicit paused refresh and bounded deliberate timeout. Browser C266-2 intercepts actual HTTP, holds both slots beyond cadence, advances the real client clock through Pause/manual Refresh/Resume/hidden/visible/unmount, asserts request counts and maximum two, and proves zero sensitive polling. The updated F-03 browser regression verifies saturation retries the exact current selection only at the 10-second normal cadence. | Coordinator assertions are included in the **7/7** mandatory unit result. Browser C266-2 is included in the final **3/3** result. Final linked-inspection rerun: **2 passed in 7.6s / 8.74s elapsed, exit 0**. |
| C266-3 | Widget observation state retains the last successful `SourceObservation`; refresh errors do not rewrite its dates. Scope/metric/run generations unregister old owners and abort queued/active jobs. `DashboardApp` clears selection on a new applied dataset; `AttemptTimeline` closes a no-longer-owned attempt and the coordinated `ContentReader` aborts and clears memory on unmount. | The activity unit test forces a successful observation followed by restart failure and asserts the original last success, age, value and visible error. Browser C266-3 drops/restarts the real activity read, verifies original dates/error then recovery, writes one concurrent run to the owned SQLite fixture and observes 28 -> 29, opens a real selected Codex-implementer output read, holds it, applies an excluding filter, then proves timeline/modal/content retirement and no late sentinel resurrection. | Unit assertion is included in the mandatory **7/7** result. Browser C266-3 is included in the final **3/3** result. Existing sensitive retirement browser cases also passed in the affected run described below. |

## Exact focused validation

Commands used the owned Node 24.22.0, Playwright browser/library locations,
owned uv caches and temporary local state.

| Command | Exact result |
| --- | --- |
| `npm run typecheck` | **exit 0, 3.49s elapsed** |
| `npm test -- --run src/dashboard/ActivityCalendar.test.tsx src/dashboard/refreshCoordinator.test.ts` | **exit 0, 2 files / 7 tests passed, 1.17s Vitest / 1.92s elapsed** |
| affected `AttemptModal`, `AttemptTimeline`, `DashboardApp`, `RunMap`, local-fetch and parser Vitest selection | **exit 0, 6 files / 29 tests passed, 2.68s Vitest / 3.32s elapsed** |
| `npm run build` | **exit 0, 655 modules, 4.67s Vite / 5.29s elapsed**; packaged HTML 0.45 kB, CSS 8.58 kB and JS 892.45 kB; the existing >500 kB advisory remains non-fatal |
| `npx playwright test e2e/refresh-lifecycle.spec.ts` | **exit 0, 3 passed in 10.2s / 11.18s elapsed** |
| affected `attempt-content.spec.ts`, `linked-inspection.spec.ts`, `overview.spec.ts` | First combined run: **5 passed / 1 failed in 25.6s / 26.74s elapsed, exit 1** because F-03 still expected the superseded immediate saturation loop. After aligning that regression with the required deterministic 10-second cadence, `linked-inspection.spec.ts`: **exit 0, 2 passed in 7.6s / 8.74s elapsed**. The other five affected cases had passed and were not changed. |
| `uv run ruff format --check dashboard-web/e2e/mutate_fixture.py` | **exit 0, 1 file already formatted, 0.45s elapsed** |
| `uv run ruff check dashboard-web/e2e/mutate_fixture.py` | **exit 0, all checks passed, 0.47s elapsed** |
| `uv run mkdocs build --strict --site-dir <owned-temp>/site` | **exit 0, built in 0.66s / 1.01s elapsed**; only the existing Material notice and unlisted incident pages were reported |
| `git diff --check` | **exit 0, 0.02s elapsed** |

Development attempts are not hidden:

- Two early typechecks exited **2** in **2.04s** and **3.01s** for, respectively,
  a stray calendar registration token and not-yet-updated coordinator props in
  existing tests. After correction, intermediate typechecks passed in **3.04s**,
  **2.98s** and **3.81s**. The final typecheck above also passed.
- The first mandatory unit run had **3 failed / 4 passed** in **6.51s Vitest /
  7.18s elapsed, exit 1**: two deterministic-runtime tests needed complete
  promise flushing and one fake-timer assertion used `waitFor`. After correcting
  the test synchronization, the same selection passed **7/7** in **1.24s /
  1.92s elapsed**, followed by the final result above.
- The first affected component selection had **4 failed / 28 passed** in **3.49s
  Vitest / 4.23s elapsed, exit 1**. Three assertions still required the retired
  immediate selected-run saturation loop and one overview assertion raced the
  queued first read. Normal-cadence expectations and queued-read synchronization
  corrected them; an intermediate **7 files / 32 tests** then passed in **2.66s /
  3.30s elapsed** before the final affected selection above.
- An initial production build passed (**655 modules, 4.50s Vite / 5.05s
  elapsed**) before the final packaged build.
- The first new browser attempt had **3 failed in 23.38s elapsed** from test-only
  selectors and incomplete queued-read synchronization. A second attempt showed
  C266-1 passing and C266-2 failing before the 30-second tool handle was lost; no
  terminal summary is claimed. The corrected isolated C266-2 case passed **1/1
  in 5.9s / 6.77s elapsed**.
- A later complete new-browser attempt had **2 passed / 1 failed in 36.8s /
  37.78s elapsed, exit 1** because the deliberately held modal backdrop correctly
  intercepted a pointer click on the underlying Apply control. The test now
  dispatches the real Apply handler to model an external scope change while the
  old response remains held; the final **3/3** suite passed.
- One parallel typecheck/unit wrapper omitted the owned Node path; both commands
  exited **127 in 0.00s** without executing their checks. The identical final
  commands were rerun with the owned Node path and passed.

## Correction review F-01 through F-04

All four reviewer findings were confirmed and are **FIXED**. No finding was
disputed or blocked.

| Finding | Confirmed production path and fix | Independent correction assertion | Exact correction result |
| --- | --- | --- | --- |
| F-01 | `ActivityCalendar` previously replaced state with a loading-only value whenever `datasetKey` changed. It now retains an observation only when its typed metric and actual filter echo still match. `RunMap` now compares a normalized actual-scope key separately from revision ownership: same-scope Refresh retains only the successful first page and its dates, resets appended pages, and shows that first page if the replacement read fails; an actual scope change still clears it. A failed scope with no successful observation is labelled unavailable, not empty; a successful empty last observation remains an honest empty result. | The new activity unit case resolves a dated success, changes only revision, rejects the replacement and proves the value/source/receive dates remain; a project change clears that incompatible success. Map unit coverage independently clears an old-scope row and rejects the new scope, proving it is unavailable rather than empty. Browser F-01 loads 28 rows, appends page 25, pauses, forces real activity/map network failure during explicit Refresh, and proves original activity dates plus the 25-row first-page success remain while the appended page is reset and automatic refresh stays paused. | Component correction selection: **4 files / 23 tests passed, 1.72s Vitest / 2.39s elapsed, exit 0**. Browser F-01 is included in the final **11/11** browser sweep below. |
| F-02 | `DashboardApp` no longer gates `RunMap` mounting on `capacity.loading`. Catalog settlement remains the admission condition, while capacity and map independently use the shared coordinator. A cancelled initial capacity read may honestly remain pending during Pause but cannot suppress the map or an explicit paused Refresh. | Browser F-02 holds the first real `machine.codex-capacity` HTTP request, pauses (cancelling coordinator ownership), applies the map scope and explicitly refreshes it. It asserts an additional `dashboard.runs` request, 25/28 visible rows and paused status without invoking Resume. | Included in the final **11 passed in 29.3s Playwright / 30.29s elapsed, exit 0**. |
| F-03 | `RunMap` now aborts `pageController` both when the dataset effect starts and when that dataset owner cleans up. The controller ref is retired after settlement; the existing generation and filter-echo fences remain. | The deterministic `RunMap.test.tsx` regression holds offset 25, verifies one active coordinator owner, rerenders a new revision, asserts the old signal is aborted and active ownership returns to zero, then releases the old success and proves its row cannot enter the current dataset. | Included in the **4 files / 23 tests** correction selection and the final **6 files / 33 affected tests** result. |
| F-04 | `RunMap` and `AttemptTimeline` now store one observation record per explicit page offset. Each first-page refresh replaces only offset 0 metadata; retained appended pages keep their original dates. This bounds recurring metadata without converting appended offsets into polling keys. | New map and timeline tests load offset 25, perform four first-page refreshes, and independently assert call offsets `[0, 25, 0, 0, 0, 0]`, retained historic rows, the original offset-25 source/receive dates, only the newest offset-0 dates and no original offset-0 log entry. | Included in the **4 files / 23 tests** correction selection and final **6 files / 33 affected tests** result. |

### Correction validation

These commands validate the complete corrected snapshot. The packaged build was
regenerated before browser execution.

| Command | Exact result |
| --- | --- |
| `npm run typecheck` | **exit 0, 3.19s elapsed** |
| correction-focused `ActivityCalendar`, `RunMap`, `AttemptTimeline`, `refreshCoordinator` Vitest selection | **exit 0, 4 files / 23 tests passed, 1.72s Vitest / 2.39s elapsed** |
| mandatory `npm test -- --run src/dashboard/ActivityCalendar.test.tsx src/dashboard/refreshCoordinator.test.ts` | **exit 0, 2 files / 8 tests passed, 2.02s Vitest / 2.92s elapsed** |
| affected `AttemptModal`, `AttemptTimeline`, `DashboardApp`, `RunMap`, local-fetch and parser Vitest selection | **exit 0, 6 files / 33 tests passed, 3.29s Vitest / 4.10s elapsed** |
| `npm run build` | **exit 0, 655 modules, 4.32s Vite / 4.86s elapsed**; packaged HTML 0.45 kB, CSS 8.58 kB and JS 893.57 kB; the existing >500 kB advisory remains non-fatal |
| final combined `refresh-lifecycle`, `attempt-content`, `linked-inspection`, `overview` Playwright sweep | **exit 0, 11 passed in 29.3s / 30.29s elapsed** |
| `uv run mkdocs build --strict --site-dir <owned-temp>/site` | **exit 0, built in 0.68s / 1.33s elapsed**; only the existing Material notice and unlisted incident pages were reported |
| `git diff HEAD --check` | **exit 0, 0.01s elapsed** |

One final affected Vitest attempt completed **6 files / 33 assertions passed**
but exited **1 with one unhandled error in 3.40s Vitest / 4.31s elapsed**: a
deferred chart-focus callback reached JSDOM after its test and called the browser
`scrollIntoView` API that JSDOM does not implement. The map/timeline focus helpers
now capability-check that optional browser API; the exact affected selection
then passed **33/33** as reported above. Earlier implementation attempts and
their failures remain recorded above rather than being rewritten by this correction.
The corrected contracts map to C266-2's independent lifecycle/ownership boundary
and C266-3's last-success and retirement boundary; C266-1 metric/role semantics
were unchanged and reran through the mandatory real producer/browser case.

## Preserved surfaces and unfinished work

The documentation governance skill kept the lifecycle description aligned with
the production coordinator and excluded sensitive bodies from evidence. The
planning/documentation/reviewer skills, dashboard governance rule and root YAML
are unchanged. Final SHA-256 audit:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`:
  `aa4e3ae23ad92ba31ea13b6d2d2fbc24baeac97d9163dcd975d2ebe7bcfae853`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

The separate read-only review skill was read as an implementation boundary but
was neither invoked as reviewer B nor modified. No Phase 26.6 contract or
mandatory focused check is unfinished. The repository-wide pytest gate,
isolated wheel/sdist installation acceptance, live operator-source observation,
final operator walkthrough and human visual acceptance were deliberately **not
executed**; the approved plan assigns those combined gates once to Phase 26.7.
Green checks do not authorize human acceptance, a final commit or deployment.
