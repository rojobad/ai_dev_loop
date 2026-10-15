# Phase 26.6: Activity calendar and visible refresh lifecycle

Status: operator-requested implementation scope; implementation pending.
Sequence position: 6/7. Implementer: Codex gpt-5.6-sol / xhigh (90 minutes).
Reviewer: gpt-6.1-sol / high, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Finish the three-mode activity calendar and coherent bounded automatic refresh, Pause/Resume and last-success observation presentation for the full dashboard.

## Non-Goals

Hub browser WSS/shared multi-tab polling, durable offline history/cache, token usage or model execution.

## Scope

Calendar metric selection, named-zone event dates, visible refresh coordinator, per-key cadence/ownership, scope changes and failure presentation.

## Out of Scope

Metric recomputation in React, query algorithms already accepted in 26.1, content polling, new storage/services and scheduler/driver policies.

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

Actual accepted 26.1 activity CLI/service, 26.3 data source/filter parsing,
26.4 selection/paging and 26.5 reader retirement. Recheck their production APIs
and exact generation/cancellation contracts before integrating refresh. Existing
Hub Phase 15 is product context only: this local repo uses HTTP polling and does
not import/implement its Django/Channels coordinator or persistent cache.


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

1. Replace activity placeholder with ECharts calendar heatmap plus accessible
   daily values and selector submissions/review-rounds/process-hours. Render
   backend daily values; convert process seconds to hours for display only.
   Provider type never substitutes for semantic role: implementation/corrections
   contribute process time but never reviewer rounds.
   Explain event-date activity vs started cohort and coverage exclusions, including
   resolved failures/parallel time, so zero is not invented for missing evidence.
2. Make project/range/zone/metric part of canonical query identity; metric change
   retires old activity request but need not reload unrelated widgets. Dates are
   named-zone calendar days, not the browser/host timezone. Year boundaries,
   leap days and empty/partial data have honest labelled states.
3. Implement one client refresh coordinator with <=2 active reads, deduplicated
   keys, no overlap/catch-up, request timeouts and cancellation. Target summary/map/
   activity 30 s, selected run inspect/timeline 10 s, capacity 60 s. Query only
   currently visible widgets/pages; appended historic pages do not become permanent
   polling keys. Dataset Refresh resets map pages to first page. Do not poll
   sequence history, content/review bodies or guessed future resources.
4. Pause/hidden/route leave retires queued/owned refresh work; explicit Refresh
   still works while paused. Resume schedules fresh reads without catch-up burst.
   Last widget removal leaves no recurring workload. A slow/failing key retries
   at a later normal cadence, not a hidden immediate loop.
5. Retain only memory-owned last successful observation with its original dates
   and explicit refresh error/age; after server restart re-read, without fake
   persistent cache or reconstructing in-flight success. Query generation fences
   HTTP races across metric/filter/run changes. A new filtered map that excludes
   selected run closes timeline/modal and clears sensitive buffers.
6. Add deterministic clock/visibility/request-count and real browser/server cases;
   document implemented refresh policy and record
   `archive/implementation-history/findings/phase-26-6-activity-and-visible-refresh.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C266-1 | Activity query -> metric selector -> calendar | Known submitted/round/process-second series including Cursor/Codex implementation and separate Codex review, pre-range-start activity, retry, midnight and metric/project/range change. | Daily displayed counts/hours match hand-written expected values and matching canonical filter; prior metric reply cannot replace it. Catches deriving activity from map rows or dividing hours twice. |
| C266-2 | Visibility/Pause/clock -> refresh coordinator -> actual HTTP counts | Visible widgets, held read longer than cadence, saturation, hidden tab, Pause/manual Refresh/Resume and unmount. | <=2 reads, no identical overlap/catch-up; capacity cadence distinct; zero recurring reads after last observer; manual Refresh while paused works; sensitive count stays zero. Catches unconditional timers and recursive refresh. |
| C266-3 | Failure/restart/filtered dataset -> last-success UI + selection | Drop server/read, recover it, insert concurrent run, remove selected run from applied cohort and release old replies. | Original last-success dates and error shown; current filtered selection/data only; content cleared and fresh read after recovery. Catches reclassifying stale observations as fresh or stale attempt resurrection. |

## Validation

Add `dashboard-web/src/dashboard/ActivityCalendar.test.tsx`,
`refreshCoordinator.test.ts`, `dashboard-web/e2e/refresh-lifecycle.spec.ts`.
From dashboard-web run:

```bash
npm run typecheck
npm test -- --run src/dashboard/ActivityCalendar.test.tsx src/dashboard/refreshCoordinator.test.ts
npm run build
npx playwright test e2e/refresh-lifecycle.spec.ts
```

Also run the affected scope/content retirement tests, root scoped Python query
regressions if interfaces change, `uv run mkdocs build --strict` and
`git diff --check`. Use deterministic scheduler/fake-clock synchronization for
cadence claims; a full-minute sleep or animation screenshot is insufficient.
Browser visibility tests exercise actual page/client handlers with controlled
synthetic responses, not a test-only timer supplying missing production policy.

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

No promise of atomic cross-widget snapshot or history surviving server/browser
restart. Display each observation honestly. Pausing dashboard observation does
not pause scheduler execution. Old selected historical pages remain explicitly
observed until Refresh/load-more; they are not silently rewritten as current.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
