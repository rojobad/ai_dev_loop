# Phase 26.4: Linked sequence/run map and attempt timeline

Status: operator-requested implementation scope; implementation pending.
Sequence position: 4/7. Cursor: grok-4.7-high.
Reviewer: gpt-6.1-sol / high, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Implement the central drill-down: a run in the filtered map selects exactly that run and loads its authoritative paginated attempt timeline.

## Non-Goals

Sensitive content readers, calendar activity or new scheduler/start/phase transition logic.

## Scope

Map pages and sequence/standalone grouping, replacement identity, linked selected-run metadata, Gantt bars and accessible attempt selection ready for the next modal slice.

## Out of Scope

Aggregate query semantics, backend transport/framework changes, prompt/output requests and future Hub multi-machine aggregation.

## Required Context

Planning baseline is `main` at `90262eed1af7c76c46139855e249de4ea8ec0d5e`, observed 2026-10-14:
Phase 23 production plus planning-only Phases 24/25 and the non-interactive prompt
fix. Neither 24 nor 25 is a dependency. At this planning baseline, SQLite schema
is 13 and local API is 1.5, before 26.1 adds dashboard capability/additive minor.
Python >=3.11 and `uv` are the current runtime/tooling.
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

Actual accepted 26.3 UI/data source/filter-generation/build/test implementation
plus 26.1 joined dashboard.runs pages. Existing `run.inspect`, `run.timeline`,
`sequence.inspect` and `sequence.phase-runs` public services provide state and
exact lineage/timeline IDs. Inspect accepted parsers and existing phase 21
attempt paging tests before reuse; source test fixture setup may write its own
temporary ledger, but the production page reads it only through accepted HTTP.

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
the new dashboard rule/shared contract; Cursor does not change those policies,
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

1. Replace map/timeline placeholders with paginated data-source calls. Map scope
   is all matching started runs on this machine, including standalone and every
   replacement run. Show returned totalCount independently of loaded rows,
   explicit Load more and dataset observation/coverage. Reset on Apply/Refresh.
2. Group sequence rows/cells by authoritative sequenceId and phase ordinal/name;
   retain one selectable entity per runId/generation, including initial and later
   attempts in the same phase. No current-run-only collapse, name-derived lineage
   or synthesized phase state. Zero-run/planned/out-of-range context is labelled
   nonselectable if shown and contributes no filtered count. Use shared API data
   for phase metadata; any sequence detail is independently timestamped.
3. Selecting a chart cell or keyboard companion row owns exact runId and page
   generation. Load selected run inspect/timeline through DashboardDataSource;
   no initial guessed run selection and no content preload. Switching/resetting
   clears attempt selection, aborts obsolete work and fences late replies.
4. Build custom Gantt series using observed launch/completion; created-but-not-
   launched attempts have explicit non-duration representation. Preserve gaps,
   ongoing/unknown end, component, iteration, phaseAttempt and raw status;
   retain non-execution attempts with honest labels. No sum/stack that hides
   parallel processes or treats waiting as execution time.
5. Use typed attempt IDs in chart data, companion rows, labels and selection
   callback for the next modal. Keep timeline paging visible, deduplicate rows,
   never infer completion from EOF/page exhaustion. Restore focus/scroll and
   dispose chart instances on selection changes.
6. Add production-path selection/page/race browser cases and record
   `archive/implementation-history/findings/phase-26-4-linked-run-map-and-timeline.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C264-1 | Joined runs page + authoritative lineage -> map | Sequence with two replacement runs in one phase, independent run, queued/out-of-range runs, zero materialized phase and >25 matches. | Exact selected IDs/counts, distinct replacement cells/rows and explicit page coverage; future planned context has no selected run. Catches currentRunId-only or name-derived maps. |
| C264-2 | Map/keyboard click -> data source -> selected inspect/timeline | Select an older replacement, then another run while first reply is held; change filters to exclude selected run. | Requests/responses target exact clicked IDs; late first result cannot reappear, excluded selection clears, no content endpoint invoked. Catches selected phase opening the latest run instead. |
| C264-3 | Public timeline pages -> Gantt + attempt selection | Cursor/Codex retry attempts, active/missing launch, parallel intervals, gap and second page. | Positions/IDs/timestamps match an independent expected interval list; unknown durations are honest; same IDs selectable through chart/keyboard. Catches fabricated end times and page-as-complete rendering. |

## Validation

Add `dashboard-web/src/dashboard/RunMap.test.tsx`, `AttemptTimeline.test.tsx`
and `dashboard-web/e2e/linked-inspection.spec.ts`; existing 26.3 scripts are now
accepted commands. From dashboard-web run:

```bash
npm run typecheck
npm test -- --run src/dashboard/RunMap.test.tsx src/dashboard/AttemptTimeline.test.tsx
npm run build
npx playwright test e2e/linked-inspection.spec.ts
```

Run affected filter/data-source regressions as needed, plus root
`uv run mkdocs build --strict` and `git diff --check` for changed docs.
Browser cases use a real local server with isolated known ledger/lineage and
fake providers; deterministic response holds prove selection fences. A screenshot
of synthetic bars alone is not evidence of correct run/attempt ownership.

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

Offset pages may change with concurrent scheduler inserts; reset and source
observation labels prevent a false atomic-history promise. Lost selected IDs
produce a recoverable unavailable/empty view, without guessed successor routing.
All runtime state remains browser memory; closing the page changes no scheduler work.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
