# Phase 26.4 linked run map and timeline findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`a41f4e3f7d89513b333bc16aea24a18c90e6dd0b`. Initial Git status was clean.
Implementation used the accepted Phase 26.1-26.3 and Phase 27.1-27.7 production
baseline: Integration API 1.9, SQLite schema 16, provider-aware attempts,
historical Cursor defaults, current pending-role/retry metadata and
authoritative `implementer_retry` lineage. Phase 24 and Phase 25 were not used as
dependencies. No OpenQuestion remained unresolved.

The implementation adds an explicitly paged sequence/run map and a linked,
explicitly paged attempt Gantt. Selection is always an opaque `runId` or
`attemptId`; authoritative sequence bindings keep replacement generations
distinct. Cursor implementer, Codex implementer and Codex reviewer processes
have separate classifications. Missing launch and missing end evidence remain
markers rather than fabricated durations. The operator-authorized C264-4 change
adds exact raw unknown-state aggregates to the summary producer, public DTO and
schema, validates their arithmetic, and renders them as React text or escaped
tooltip HTML.

No scheduler run was submitted, started, ticked, retried, extended, aborted or
otherwise controlled. No real Cursor/Codex turn or account-capacity probe ran.
No timer, root YAML, review schema, scheduler schema, installed operator runtime,
external Git state, commit, push, PR or merge changed. Tests used only temporary
owned state, a deterministic SQLite fixture and the existing fake capacity
provider. No prompt, review content or process output endpoint was called by the
new production UI or browser contract.

## Correction review disposition

- **F-01 — fixed.** `RunMap.runMapOption` now assigns every run a distinct
  integer position within its authoritative sequence/standalone group. The
  chart reserves at least 44 pixels per position and scrolls horizontally, so
  the custom cell's 0.72-unit width (with a 16-pixel minimum) cannot overlap a
  neighbor and no generation is clamped or aliased. `RunMap.test.tsx` asserts
  unique positions 1 through 30 for 30 replacement generations. The production
  server fixture persists authoritative `SequenceRunBinding` and
  `SequenceRunAttempt` lineage for generations 1 and 2; Playwright clicks both
  real canvas cells and proves exact selection of `older-map-run` / generation
  1 / `planned_run` and `current-map-run` / generation 2 /
  `implementer_retry`. Companion rows and mocked chart events are not used for
  those assertions.
- **F-03 — fixed.** The HTTP data source maps an admitted-server HTTP 503 to
  typed `SERVICE_UNAVAILABLE`. `AttemptTimeline` applies only to selected-run
  metadata/timeline reads a bounded five-delay retry sequence (100, 200, 400,
  800 and 1600 ms), using the existing per-owner abort signal. A later
  selection/reset cancels the delay and any next request; generation and exact
  returned-run-ID fences remain mandatory, and exhaustion renders an explicit
  saturation error. The deterministic browser fixture holds the old run's
  `inspect_run` and `list_timeline` inside executing service jobs, occupying
  both production-server slots. It switches runs while those slots are held,
  observes both exact new-run operations retry, releases the jobs and proves
  only `current-map-run` metadata/timeline renders. Each canceled old-run
  operation is requested exactly once. Admission limits and read-only behavior
  are unchanged; this is bounded recovery, not Phase 26.6 polling.

Phase 26.3 F-02 remains closed: exact raw unknown states still appear beneath
Other and its producer/DTO/schema/parser arithmetic contract is unchanged.

## Admission and prerequisite evidence

The accepted prerequisite was checked once before dependent implementation.
Source and history inspection confirmed accepted Phase 26.1 commit `161e6eb`,
26.2 `20672c5`, 26.3 `6906762`, the carry-over documentation commit `a41f4e3`,
API 1.9 and schema 16. The stable installed scheduler driver was the repository
virtual environment; the changing editable source was not used to operate live
scheduler work.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase264-uv-cache TMPDIR=/tmp TMP=/tmp TEMP=/tmp \
  uv run python -m pytest -q \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py \
  tests/integration/test_phase27_3_inspection.py
```

Result: **exit 0, 29 passed in 43.92s**. This selection retains the accepted
execution-role, reviewer-only content, read-only server, lineage and current
provider/retry contracts. A preceding environment-information command tried the
unavailable bare `python` executable and failed before any test or repository
action; `uv run python` then reported API 1.9/schema 16. The prerequisite test
selection itself was not repeated.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact executed result |
| --- | --- | --- | --- |
| C264-1 | `DashboardApp` applied scope -> `LocalFetchDashboardDataSource.runs` -> `parseRunsData` -> `RunMap`; grouping consumes `sequenceBinding.sequenceId`, ordinal, generation, attempt kind and source run ID, never names or `currentRunId`. `runMapOption` assigns a unique full-step position per authoritative group and `RunMap` provides a width-scaled scroll owner. | `RunMap.test.tsx` supplies 28 matching runs, exercises the 25/28 page boundary and independently asserts 30 replacement generations receive coordinates 1 through 30. `linked-inspection.spec.ts` persists actual sequence replacement lineage and clicks both real ECharts canvas cells, proving each exact run ID/generation/kind without companion-row or mocked-event substitution. | Correction frontend suite: **3 files / 14 tests passed in 2.23s Vitest / 3.22s wall, exit 0**. Final linked browser: **2 passed in 7.0s Playwright / 7.91s wall, exit 0**. |
| C264-2 | Run map typed selection -> `DashboardApp.selectedRun` -> `AttemptTimeline` -> exact `run.inspect` and `run.timeline`; `LocalFetchDashboardDataSource.request` types HTTP 503 saturation and `readCurrentSelection` performs bounded cancellable current-selection recovery. Independent metadata/timeline AbortControllers plus generation and returned-run-ID checks retire old ownership; filter/revision changes clear selection. | Unit tests prove retry-then-success, cancellation of an old selection during its retry delay with no next old-run call, explicit bounded exhaustion, old-signal abortion, late-reply fencing, independent metadata/page ownership and returned-run-ID rejection. The browser holds both older-run reads inside executing production service jobs, switches selection, proves both current-run operations retry after 503, releases the jobs, and asserts only the current exact run loads while each old operation was requested once. `DashboardApp.test.tsx` still proves an excluding applied filter clears selection. | Included in correction **14/14**, affected Phase 26.3 **4 files / 14 tests in 3.79s Vitest / 4.57s wall**, and linked browser **2/2**, all exit 0. |
| C264-3 | `LocalFetchDashboardDataSource.runTimeline` -> `parseAttemptTimelineData` -> `attemptRole`/`buildTimelineRows` -> custom ECharts Gantt and exact attempt companion rows; `run.inspect` supplies provider, pending role/turn/capacity and retry metadata | `AttemptTimeline.test.tsx` independently enumerates overlapping Cursor implementer, Codex implementer and Codex reviewer intervals, a gap, launched/no-end and created/not-launched markers, unclassified ownership and exact timestamps/IDs. It pages from offset 0 to 25 and selects the same typed retry ID through row and chart. The real fixture exposes 27 attempts so page two contains a reviewer retry and active Codex correction; the browser asserts both role labels and the honest no-end marker. | Correction **14/14** and linked browser **2/2**, exit 0. Codex implementer processes contribute their observed start/duration/timeline rows but are never classified as Codex reviewer attempts or routed to reviewer-content calls. |
| C264-4 | `DashboardQueries.summary._other_states` -> `DashboardSummaryData`/`DashboardProjectSummary` -> JSON schema -> `parseSummaryData` -> `SummaryPanels`, `projectStateOption` and escaped tooltip formatter | `test_queries.py` passes two raw future states, including a malicious string, through the real query producer, public model and packaged schema; it asserts sorted exact labels/counts, sum equality and model rejection of an inconsistent sum. The application port is used because constructing a currently unsupported future `SchedulerState` in SQLite would correctly trip the existing integrity validator; that guard was not weakened. Parser tests independently reject inconsistent arithmetic; app/chart tests assert exact literal text and escaped tooltip output. | Dashboard Python selection: **42 passed in 3.32s pytest / 3.89s wall, exit 0**. Affected frontend: **14/14 passed in 3.79s Vitest / 4.57s wall, exit 0**. F-02 remains closed without paging `dashboard.runs`, private SQLite/artifact access or guessed labels. |

## Implemented boundaries

- `RunMap` fetches only explicit offset pages of 25, displays loaded versus
  authoritative total and per-page observation times, deduplicates by exact
  `runId`, and never treats the future planned sequence context as a run.
  Every loaded run owns a distinct full-step cell within its authoritative
  group; the plot expands and scrolls rather than compressing or clamping
  replacement generations.
  RFC3339 echo ownership compares instants semantically so equivalent `Z` and
  `.000Z` encodings match while project and IANA zone remain exact.
- The initial map waits for catalog and capacity settlement. This sequences the
  three supporting initial reads behind the summary/catalog pair and respects
  the local server's bounded two-job admission instead of adding a third
  unconditional service read. Later Apply/Refresh uses only summary plus map.
- `AttemptTimeline` uses one lane per attempt and actual launch/completion only.
  Created-only and launched-without-end attempts are explicit markers. Page EOF
  is labelled as an observation boundary, not completion. Raw component, effect,
  status, iteration and phase-attempt remain visible.
- Metadata and timeline have separate request controllers under the same
  selected-run generation. Timeline page replacement cannot cancel a still
  valid metadata read; changing run cancels both. The chart is re-owned and
  disposed per selected run, and chart selection restores the corresponding
  companion-row focus. Transient selected-run saturation has bounded,
  signal-cancellable recovery; unrelated errors and exhausted recovery remain
  explicit.
- `otherStates` is an additive summary field. The producer emits unique sorted
  raw labels and counts; the strict DTO and frontend parser both require their
  sum to equal `stateCounts.other`. The overview does not scan run pages.
- The frontend data source remains the only HTTP-aware layer. SQLite and
  artifacts remain authoritative, read-only and outside browser code. Content
  stays outside this slice and has no durable client cache.

## Exact focused validation

Commands used the owned Node 24.22.0 and Playwright/browser-library directories
created for accepted Phase 26.3, the owned uv cache and temporary resources.

| Command | Result |
| --- | --- |
| `npm run typecheck` | **exit 0, 3.77s** |
| `npm test -- --run src/dashboard/RunMap.test.tsx src/dashboard/AttemptTimeline.test.tsx src/dashboard/local-fetch-data-source.test.ts` | **exit 0, 3 files / 14 tests passed, 2.23s Vitest / 3.22s wall** |
| `npm test -- --run src/dashboard/parsers.test.ts src/dashboard/DashboardApp.test.tsx src/dashboard/charts.test.ts src/dashboard/DashboardChart.test.tsx` | **exit 0, 4 files / 14 tests passed, 3.79s Vitest / 4.57s wall** |
| `npm run build` | **exit 0, 650 modules, 6.70s Vite / 7.30s wall**; packaged HTML 0.45 kB (0.27 gzip), CSS 6.15 kB (1.89 gzip), JS 806.72 kB (270.65 gzip); the existing Vite >500 kB advisory remains non-fatal |
| `npx playwright test e2e/linked-inspection.spec.ts` | **exit 0, 2 passed in 7.0s / 7.91s wall**; includes real canvas replacement clicks and production-worker saturation ownership |
| `npx playwright test e2e/overview.spec.ts` | **exit 0, 1 passed in 5.1s / 6.08s wall** |
| `uv run python -m pytest -q tests/unit/dashboard` | **exit 0, 42 passed in 3.32s / 3.89s wall** |
| `uv run mypy src` | **exit 0, 202 source files, 1.25s** |
| focused `uv run ruff format --check ...` / `uv run ruff check ...` | **exit 0, 4 files already formatted in 0.11s / all checks passed in 0.10s** |
| `uv run mkdocs build --strict --site-dir <owned-temp>` | **exit 0, built in 0.93s / 1.38s wall**; only the repository's existing informational Material/MkDocs 2 notice and unnav-listed incident pages were reported |
| `git diff --check` | **exit 0, 0.11s combined final status/diff audit** |

Development attempts are not hidden: the first typecheck found the ECharts
custom-render `api.size` union and was corrected; the first focused Vitest run
had **2 failed / 3 passed** because two test queries were ambiguous and the
production behavior was then asserted through exact accessible names. A chained
Python command used the frontend cwd and exited **4** without collecting tests.
The first focused C264-4 Python run had **1 failed / 4 passed** because the
negative test converted strict tuple fields to lists; the corrected typed input
then passed **5/5**. The first linked browser run exited 1 in **11.93s** and found
the equivalent `.000Z`/`Z` filter-echo defect; the next exited 1 in **6.87s** on
an ambiguous late Project locator; an over-constrained exact-label retry timed
out and exited 1 in **35.55s**. The scoped filter locator and semantic instant
comparison produced the final pass. The first Ruff format check exited 1 for
four changed Python files; formatting was applied and the final check passed.
Correction attempts are also not hidden: the first correction Vitest selection
had **3 failed / 10 passed** because fake timers prevented two asynchronous
queries from settling and leaked into the following test; synchronous
post-advance assertions fixed the test ownership, and the complete 14-test
selection passed. Three linked-browser attempts exited 1 while successively
exposing an off-screen canvas coordinate, the sequence-row category coordinate,
and an ambiguous lineage assertion; scrolling the real canvas into view, using
the correct category center and scoping the lineage locator produced the final
2/2 pass. A four-command timing wrapper attempt exited 127 before starting any
frontend command because `npm` was not on that wrapper's PATH; the same four
commands then ran with the owned Node path and passed as reported above. All
failed affected checks were rerun after correction.

## Preserved surfaces and unfinished work

The protected plan/docs/reviewer skills and root YAML are unchanged. Final
SHA-256 audit:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

The separate read-only review skill was neither invoked as reviewer B nor edited
by this implementer.

No Phase 26.4 contract or mandatory focused test is unfinished. Repository-wide
pytest, isolated package/wheel acceptance, real account capacity observation,
operator walkthrough and human visual acceptance were deliberately **not
executed**; the approved plan assigns the combined integration/package/human
gate once to Phase 26.7. Phase 26.5 and 26.6 content/activity scope was not
implemented. Green checks do not authorize human acceptance, final commit or
deployment.
