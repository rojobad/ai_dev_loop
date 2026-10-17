# Phase 28.2: Linked inspection

Status: operator-authorized 2026-10-16; implementation pending.
Implementer: Codex gpt-5.6-sol / xhigh; reviewer: gpt-6.1-sol / high.

## Goals

Make map-to-run-to-attempt inspection compact, stable and readable, without page
jumps or walls of run cards; apply consistent human date/duration presentation.

## Scope

RunMap, AttemptTimeline, AttemptModal, shared presentation helpers, all Dashboard
displayed timestamps/durations and focused browser/formatting regression tests.

## Out of Scope

New alias migrations/commands, API page-size expansion, auto-apply, information
panel redesign, activity-list removal and tooltips clipping (owned by 28.3).
Minimal additive attempt ordering metadata is allowed only if needed for stable
labels; no scheduler execution/recovery change.

## Non-Goals

Remote Hub implementation, scheduler execution/recovery redesign, authentication,
new metrics, exact implementer-prompt reconstruction or automatic final acceptance.

## Required Context

Read [Phase 28 contract](phase-28-dashboard-refinements.md) in full, including
compatibility/rollout and all approved product behavior. Accepted baseline is
main `67b8edc` with completed Phase 26 and dual-implementer Phase 27. Recheck the
actual accepted predecessor once before dependent work. Read
`docs/referencia/dashboard.md`, `docs/referencia/integration-api.md`,
`docs/referencia/cli.md`, `docs/operacion/estado-artefactos.md`, and
`docs/operacion/seguridad-privacidad.md`. Archived Phase 26 plans explain original
wiring; Phase 28 explicitly supersedes the UI behavior identified in its contract.

## Cursor Rules And Skills

Follow `AGENTS.md` and `.cursor/rules/ai-dev-loop-{governance,orchestrator-contracts,
state-and-schema-contracts,loop-and-resume-contracts,abort-contracts,
codex-review-contracts,docs-acceptance-contracts,global-integrations-contracts,
dashboard-contracts,dashboard-refinements-contracts}.mdc` (brace notation lists
individual existing files, not a shell command). Read
`.agents/skills/create-cursor-plan/SKILL.md` and applicable documentation guidance
in `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, using current
AGENTS.md rather than retired command instructions. There is no .cursor/skills.
Independent reviewer follows `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`;
implementation does not invoke or modify review policy.

## Architecture Guardrails

Keep domain/application logic independent from CLI, SQLite, HTTP and ECharts.
Scheduler metadata owns labels; browser never derives identity, state, project,
provider or lineage from them. Dashboard remains read-only and loopback-only,
with typed additive projections and no arbitrary SQL/path/command route. Existing
content reads remain explicit, confined, memory-only and generation-fenced;
no content preload or auto-refresh. Preserve frozen artifacts/hashes, exact role
identities, accepted historical data, bounded read concurrency, Pause and hidden
page behavior. Labels/source strings are escaped, not trusted HTML.

Use a stable installed driver for this sequence. Never install this checkout,
change real SQLite, touch real artifacts/integrations/timer, launch real model
calls as tests, or alter root ai_dev_loop.yaml. Native sequence checkpointing
owns non-final commits; implementer makes no ad hoc commits/push/PR/merge.

## Implementation Plan

1. Verify accepted 28.1 naming producers/consumer parsers and existing exact-ID
   selection/cancellation boundaries. Inspect RunMap, AttemptTimeline,
   AttemptModal, DashboardApp, ActivityCalendar, charts.ts, time.ts and styles.
2. C282-1: remove all-runs visible companion cards/group lists. Initially show
   no run detail card; after exact cell selection render only its single card,
   preserving that exact run ID and replacement generation. Another click
   replaces it and its timeline. Do not select a first run automatically, render
   all sequence leaves, focus a distant card or call scrollIntoView. A compact
   accessible run selector or keyboard chart navigation must offer the same IDs.
   Keep selected styling and layout stable. Clear scope-invalid selections.
3. C282-2: create one attempt-caption producer reused by chart lanes/tooltip,
   accessible controls and modal heading. Number every attempt in authoritative
   chronological timeline order, including non-execution attempts, with stable
   tie-breaker. Example `2 - Iteration 1 - Reviewer`. Preserve raw effect/component
   secondary, Cursor/Codex distinction, and Unclassified for unsupported effects.
   Paging/refresh must not restart ordinals or infer missing launches. Retain
   exact runId+attemptId selection and content readers. Move opaque IDs to last
   metadata fields, with optional accessible copy actions.
4. C282-3: add shared selected-IANA-zone instant/day/duration formatters. Apply
   them across metadata, observation lines, capacity reset/window durations,
   run/timeline tooltips/axes and activity tooltips/values. Day bucket dates must
   not shift across timezones; exact raw API math remains unchanged. Use the
   Phase 28 contract minute rounding and <1m convention. Raw content bytes and
   arbitrary log timestamps are not rewritten. Time-axis tick labels may use a
   compact contextual date/time if space requires; tooltips/details use full
   DD/MM/yyyy HH:mm. Age uses the same human duration convention.
5. C282-4: ensure date filter controls visibly support DD/MM/yyyy HH:mm with an
   explicit selected IANA zone. Keep UTC transport ISO and established exclusive
   upper bound. Preserve explicit-offset disambiguation for DST folds and reject
   nonexistent/ambiguous local instants rather than guessing; update parser/help
   and presets coherently. Coordinate the draft type with 28.3, not auto-apply.
6. Record production-path browser evidence and findings; update affected docs.

## Testing Criteria

| ID | Production path/scenario | Required independent outcome |
| --- | --- | --- |
| C282-1 | Real map chart click / keyboard selection with dozens of sequence runs | Initially zero run cards; after click exactly one, another click replaces it; document scroll position unchanged; timeline exact IDs; no content reads. Held old replies after reselection/filter change cannot restore stale details. |
| C282-2 | Real timeline pages with same-iteration retries and Cursor/Codex roles | Distinct ordered captions across pages, chart/control/modal identical, stable after poll; attempts retain exact IDs and raw role metadata. Non-execution never mislabeled implementer/reviewer. Modal IDs are last. |
| C282-3 | Shared formatter -> every timestamp/duration UI surface | Fixed independently expected UTC/La_Paz/DST-zone times; daily buckets don't shift; 0/29/60/7260 seconds -> 00 h 00 m/<1 m/00 h 01 m/02 h 01 m, missing unavailable, >99h supported. Axis not browser-local zone. Exact process hours math unchanged. |
| C282-4 | Human date input/preset -> commitFilterDraft -> transport | DD/MM/year order is explicit, selected IANA conversion correct, to exclusive, invalid/fold/gap needs explicit correction/offset and never submits guessed UTC. |

## Validation

Extend existing `RunMap.test.tsx`, `AttemptTimeline.test.tsx`,
`AttemptModal.test.tsx`, `time.test.ts`, `DashboardApp.test.tsx`, charts and
activity tests. Add shared formatting tests if a new module is used. Exercise
production charts in `e2e/linked-inspection.spec.ts` and
`e2e/attempt-content.spec.ts` using real isolated fixture server; do not replace
chart click with a direct state setter. Deterministic held responses prove fences.

```bash
# dashboard-web
npm run typecheck
npm test -- --run src/dashboard/RunMap.test.tsx src/dashboard/AttemptTimeline.test.tsx src/dashboard/AttemptModal.test.tsx src/dashboard/time.test.ts src/dashboard/DashboardApp.test.tsx src/dashboard/charts.test.ts src/dashboard/ActivityCalendar.test.tsx
npm run build
npx playwright test e2e/linked-inspection.spec.ts e2e/attempt-content.spec.ts
```

Also run any new formatter tests and affected Python schema/timeline tests only
if that producer changes; run git diff --check and strict MkDocs for changed docs.
Report exact commands/results and C282 mapping in
`archive/implementation-history/findings/phase-28-2-linked-inspection.md`.
Use temporary fixtures/fake providers, no real model/ledger/install/timer actions.

## Risks Or Recovery Notes

Visible companions currently carry keyboard selection; removing their wall must
retain equal keyboard functionality. No focus restoration may reintroduce page
jumps. Ordinals must reflect backend order, not phaseAttempt/iteration alone.
Keep unknown data honest. If 28.1 metadata contract is absent, report the exact
prerequisite rather than recreating a second store or guessing labels.

## OpenQuestions

None.
