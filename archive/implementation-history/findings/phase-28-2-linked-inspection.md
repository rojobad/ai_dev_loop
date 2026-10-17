# Phase 28.2 linked-inspection findings

Status: **READY_FOR_REVIEW**. This is an implementation/evidence label only; it
does not authorize acceptance, staging, a sequence checkpoint, installation, or
any Git operation.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, starting HEAD
`d6a837f2254594a611d9c252129afc8a2ef4b87a`. The Phase 28.1 checkpoint is that
HEAD, and accepted Phase 26 baseline `67b8edc` was verified as its ancestor
before dependent work. The initial worktree was clean. The active plan and
shared Phase 28 contract have no OpenQuestions.

The implementation replaces the visible all-runs companion wall with one compact
exact-ID selector, keeps the initial/cleared selection empty, and renders only
the selected run's metadata and timeline. A single caption producer numbers all
loaded attempts in the authoritative timeline order and supplies chart lanes,
tooltips, accessible controls, and modal headings. Shared presentation helpers
apply the selected IANA zone, `DD/MM/yyyy` ordering, and the approved duration
convention without changing producer values or metric math. Date drafts use the
same human order, preserve the exclusive upper bound and UTC transport, and
reject DST gaps/folds unless an explicit matching offset disambiguates the fold.

No scheduler, schema, SQLite, API, frozen input, artifact, identity, lineage,
recovery, provider, content-read, or integration behavior changed. Sensitive
content remains action-triggered, bounded, memory-only, abortable, and fenced by
the existing generation/ownership checks. Automated evidence used the isolated
fake-provider fixture server and temporary browser/runtime files only. No live
ledger was opened or migrated; no model, checkout installation, timer,
integration, or workstation configuration was invoked or changed.

## Contract evidence

| ID | Real production caller/path | Independent caller/test evidence | Exact result |
| --- | --- | --- | --- |
| C282-1 | `DashboardChart` emits an exact `runId` -> `runIdFromChartEvent`/the compact selector resolves only a currently loaded item -> `DashboardApp.setSelectedRun` -> `AttemptTimeline` starts a new exact-run generation. Empty selection calls back with `null`; scope changes clear it. There is no visible run-card collection and no `scrollIntoView`. | `RunMap.test.tsx` starts with no selection, exercises 28 runs/two replacement generations, paging, exact chart/control IDs, clearing, escaping, and retained-page polling. `DashboardApp.test.tsx` proves scope retirement. The real-browser linked-inspection scenario clicks two actual ECharts cells, observes exactly one metadata card, unchanged `window.scrollY`, exact replacement lineage/timeline, and no prompt/review/output read. Held-response browser coverage proves an old selection cannot restore stale details. | Required focused Vitest: **7 files / 38 tests passed in 7.30s**. Required Playwright selection: **5 passed in 15.3s**. |
| C282-2 | Existing `SqliteSchedulerStore.list_integration_attempt_rows` orders by `COALESCE(launch_requested_at, created_at)`, then `created_at`, then `attempt_id`; `run.timeline` preserves its page order. `mergeAttempts` retains that ordered page stream, `buildTimelineRows` assigns its one-based per-run ordinal, and `attemptCaption` is reused by ECharts, row controls, selection status, and `AttemptModal`. Component/effect pairs alone determine Cursor implementer, Codex implementer, reviewer, or `Unclassified`; IDs remain exact selection keys and are the last modal metadata fields. | `AttemptTimeline.test.tsx` covers same-iteration Cursor/Codex/reviewer/unclassified entries, page-2 ordinal 26, identical chart/control/modal caption, raw component/effect, incomplete attempts, and ordinal 26 surviving four first-page polls. `AttemptModal.test.tsx` checks the shared heading, IDs-last ordering, and deliberate content ownership. Real-browser paging opens ordinal 26 and observes the identical modal heading. | Included in focused **38/38**, full Vitest **12 files / 57 tests passed in 7.35s**, and Playwright **5/5**. No additive producer field was needed because the accepted API already supplies deterministic ascending pages with a stable tie-breaker. |
| C282-3 | `time.ts` owns selected-zone full/axis instant, calendar-day, duration, and age presentation. `DashboardApp`, `RunMap`, `AttemptTimeline`, `AttemptModal`, `ContentReader`, and `ActivityCalendar` call those helpers for visible scheduler metadata, observations, capacity windows/resets, chart axes/tooltips, daily values, and modal detail. `activityDisplayValue` still divides process seconds by 3600 only for ECharts intensity; transported seconds and all producer calculations are unchanged. Raw content bytes and arbitrary log text are not rewritten. | `time.test.ts` independently expects UTC, America/La_Paz, and DST-zone output; an already-zoned day; and 0/29/60/7260/360060/missing seconds. Activity tests retain exact process-hour chart math while checking human list/tooltip values and non-shifting leap-day text. Timeline/modal tests independently check selected-zone detail and axis/tooltip fields. | Included in focused **38/38** and full Vitest **57/57**; TypeScript typecheck **exit 0**. |
| C282-4 | Filter inputs/presets produce `DD/MM/yyyy HH:mm` drafts -> `commitFilterDraft`/`localDraftToUtc` validate the selected IANA zone -> `DashboardApp` applies canonical UTC `fromUtc`/exclusive `toUtc` to the unchanged data-source contract. Explicit `Z` or `±HH:mm` input must map back to the requested local wall time and zone; ambiguous/unavailable wall times never reach a read call. | `time.test.ts` covers preset conversion, reversed ranges, New York DST gap/fold, two valid fold offsets, and mismatched-offset rejection. `DashboardApp.test.tsx` proves the ambiguous draft adds no summary request, then checks exact `05:30Z`/`07:30Z` transport after explicit offsets. Browser fixtures use the production DD/MM controls. | Included in focused **38/38**, full Vitest **57/57**, and Playwright **5/5**. |

## Correction findings

- **F-01 — FIXED.** The confirmed production path was an ECharts timeline
  `click` -> `AttemptTimeline.selectAttempt` -> `AttemptModal` mount. The modal
  focused Close, but the opening path had already queued a
  `requestAnimationFrame` that subsequently focused the background attempt row.
  Opening now performs exact-ID selection only, leaving focus owned by the
  modal. `closeAttempt` still queues restoration to that exact attempt control
  with `{ preventScroll: true }`.

  The focused regression triggers the chart event, drains every queued frame,
  and proves focus remains on Close inside the dialog; after closing, it drains
  the restoration frame and proves the ordinal-26 exact row regains focus with
  unchanged `window.scrollY`. The isolated browser harness locates and clicks
  the real rendered Codex interval on the ECharts canvas, waits through two
  browser frames, proves Close remains focused, then proves the exact attempt
  control is focused after close with scroll position unchanged. It also keeps
  the existing no-sensitive-read assertion. Correction validation: affected
  Vitest **2 files / 14 tests passed in 2.94s**; TypeScript **exit 0**; rebuilt
  production assets **655 modules in 5.03s, exit 0**; relevant Playwright **5
  passed in 14.5s**.

## Exact final validation

`npm` is not available on this runner, so the same checked-in package scripts
were executed through their installed CLI entry points using the available
Cursor-owned Node binary. No package installation was performed.

```bash
# dashboard-web
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/typescript/bin/tsc --noEmit

/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/vitest/vitest.mjs --run \
  src/dashboard/RunMap.test.tsx \
  src/dashboard/AttemptTimeline.test.tsx \
  src/dashboard/AttemptModal.test.tsx \
  src/dashboard/time.test.ts \
  src/dashboard/DashboardApp.test.tsx \
  src/dashboard/charts.test.ts \
  src/dashboard/ActivityCalendar.test.tsx
```

Results: typecheck **exit 0**; focused contracts **7 files / 38 tests passed in
7.30s**.

```bash
# dashboard-web
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/vitest/vitest.mjs --run

/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/vite/bin/vite.js build
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  scripts/write-build-manifest.mjs
```

Results: complete Vitest **12 files / 57 tests passed in 7.35s**. Production
build **655 modules in 6.08s, exit 0**; static assets and the build manifest were
regenerated. Vite emitted only its existing advisory for a chunk over 500 kB.

```bash
# dashboard-web, exact-revision Chromium and shared libraries extracted only
# beneath the two listed /tmp roots; nothing was installed system-wide
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase282-playwright \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase282-browser-libs/extracted/usr/lib/x86_64-linux-gnu \
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/@playwright/test/cli.js test \
  e2e/linked-inspection.spec.ts e2e/attempt-content.spec.ts
```

Result: **5 passed in 15.3s, exit 0**, one worker, against the real isolated
fixture HTTP service and production ECharts components. Both owned browser/library
temporary roots were removed after validation (about 688 MiB total); they are not
recoverable or retained as repository evidence.

```bash
# repository root; site output was an owned temporary /tmp directory
.venv/bin/mkdocs build --strict --site-dir <owned-temp>
git diff --check
```

Results: strict MkDocs **exit 0, built in 0.79s**; only the existing Material
notice and unnav-listed incident-page notices were emitted. `git diff --check`
**exit 0**.

### F-01 correction validation

```bash
# dashboard-web
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/vitest/vitest.mjs --run \
  src/dashboard/AttemptTimeline.test.tsx \
  src/dashboard/AttemptModal.test.tsx

/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/typescript/bin/tsc --noEmit

/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/vite/bin/vite.js build
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  scripts/write-build-manifest.mjs
```

Results: affected timeline/modal tests **2 files / 14 tests passed in 2.94s**;
typecheck **exit 0**; production build **655 modules in 5.03s, exit 0**, followed
by a regenerated build manifest. Vite emitted only its existing chunk-size
advisory.

```bash
# dashboard-web; browser and libraries were downloaded/extracted only below /tmp
PLAYWRIGHT_BROWSERS_PATH=/tmp/ai-dev-loop-phase282-f01-playwright \
LD_LIBRARY_PATH=/tmp/ai-dev-loop-phase282-f01-browser-libs/extracted/usr/lib/x86_64-linux-gnu \
/home/rojobad/.cursor-server/bin/2d29876d567da1607532b23bbf2cd5ddbca496f0/node \
  ./node_modules/@playwright/test/cli.js test \
  e2e/linked-inspection.spec.ts e2e/attempt-content.spec.ts
```

Result: **5 passed in 14.5s, exit 0**, including the real ECharts timeline
interval focus path and the deliberate content/cancellation scenarios. The two
owned F-01 browser/library temporary roots were then removed (about 688 MiB);
they were never system-installed or retained as repository evidence.

## Failed checks encountered and corrected

- The first post-edit focused run reported **4 failed, 33 passed**. Expectations
  still used the retired ID-only modal/header text and one local-zone expected
  hour was incorrect. The tests were corrected to the approved shared captions
  and actual IANA conversion; the final focused run is **38/38** after adding the
  DST transport and poll-stable ordinal cases.
- The first browser attempt reported **5 failed before application startup**
  because the exact Playwright Chromium revision was absent. After downloading
  that revision under the dedicated `/tmp` root, startup still reported **5
  failed** because this image lacks Chromium's NSS/audio shared libraries. The
  required Ubuntu packages were downloaded and extracted under a second `/tmp`
  root without installing them; the final browser run is **5/5**.
- The first strict-doc invocation could not write `uv`'s default cache on this
  sandbox. A cleanup command also masked that subprocess status, so it was not
  counted as evidence. The repository's existing `.venv/bin/mkdocs` was rerun
  directly with strict failure handling and passed in **0.79s**.
- During F-01 validation, the first browser invocation reported **5 failures
  before application startup** because this fresh temporary Chromium download
  again required NSS/audio libraries. They were downloaded and extracted only
  below the owned `/tmp` root. The next run passed the three content scenarios
  and refresh scenario but the new real-chart scenario missed the interval: a
  geometric center click was outside ECharts' plotted rectangle. The harness
  now locates the actual rendered Codex interval pixels and clicks their center;
  the complete rerun passed **5/5**.

## Preserved boundaries and unfinished checks

- No Python producer/schema/timeline code changed, so the plan's conditional
  affected-Python test selection was not triggered. The repository-wide pytest
  suite is **not run**; the shared contract assigns it to the separate final
  integration/pipeline gate, and the frontend results do not imply it.
- Only the mandatory linked-inspection and attempt-content Playwright files were
  run. Isolated wheel/sdist installation, installed-server smoke, the remaining
  package/browser gate, and the Phase 28 manual walkthrough are **not run**;
  Phase 28.3/final human acceptance own those checks.
- No unresolved automated C282 contract is known. No live provider capacity,
  authentication, billing/quota, scheduler execution, timer, WSL, integration,
  operator database, or protected artifact behavior was tested.
- `ai_dev_loop.yaml`, AGENTS/rules/skills, approved plans, scheduler authority,
  and frozen fixtures are unchanged. Documentation was updated from current
  production callers and the executed evidence; no archived instruction was
  used to restore retired commands or behavior.
- The native sequence's existing staged snapshot was preserved; correction edits
  are left for the orchestrator's final `git add -A` normalization. This
  implementation made no commit, push, PR, merge, or destructive Git-history
  action.
