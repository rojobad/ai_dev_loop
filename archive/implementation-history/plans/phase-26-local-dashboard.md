# Phase 26 — Local dashboard and reusable query API

Status: operator-requested seven-run planning package, authored 2026-10-14.
Implementation pending. Product scope and per-run model choices come from the
operator's dashboard discussion; technical defaults are explicit in the shared
contract for review. This package does not launch or prepare the sequence.

## Goals

Deliver the approved local dashboard for one scheduler installation, using
scheduler-owned read-only SQLite/artifacts. Expose the same semantic queries
through the local Integration API so a later Hub dashboard for one selected
machine can reuse them and the React/ECharts components.

## Non-Goals

Multi-machine aggregation, scheduler control, user authentication, public/LAN
hosting, token/billing metrics, SDK adoption, activity-aware execution timeouts,
durable historical telemetry, content downloads or exact missing Cursor prompts.

## Scope

Seven ordinary sequential implementation runs, each accepted against actual
predecessor code and focused automated contracts. One final operator walkthrough
in 26.7. Cursor is `grok-4.7-high` in every run. The operator selected
`gpt-6.1-sol / xhigh` for 26.1, 26.2, 26.7 and `/ high` for 26.3–26.6 on
2026-10-14. Each manifest entry freezes both reviewer fields explicitly.

| Subphase | Plan / accepted delivery | Cursor prompt | Reviewer |
| --- | --- | --- | --- |
| 26.1 | [Shared queries and Integration API](phase-26-1-shared-dashboard-queries.md) | [Prompt](prompt_phase-26-1-shared-dashboard-queries.txt) | gpt-6.1-sol / xhigh |
| 26.2 | [Local HTTP read server](phase-26-2-loopback-read-server.md) | [Prompt](prompt_phase-26-2-loopback-read-server.txt) | gpt-6.1-sol / xhigh |
| 26.3 | [Shell, filters and overview charts](phase-26-3-dashboard-shell-and-overview.md) | [Prompt](prompt_phase-26-3-dashboard-shell-and-overview.txt) | gpt-6.1-sol / high |
| 26.4 | [Linked map and timeline](phase-26-4-linked-run-map-and-timeline.md) | [Prompt](prompt_phase-26-4-linked-run-map-and-timeline.txt) | gpt-6.1-sol / high |
| 26.5 | [Attempt modal and explicit content](phase-26-5-attempt-modal-and-content.md) | [Prompt](prompt_phase-26-5-attempt-modal-and-content.txt) | gpt-6.1-sol / high |
| 26.6 | [Activity and visible refresh](phase-26-6-activity-and-visible-refresh.md) | [Prompt](prompt_phase-26-6-activity-and-visible-refresh.txt) | gpt-6.1-sol / high |
| 26.7 | [Installed acceptance and Hub handoff](phase-26-7-installed-dashboard-acceptance.md) | [Prompt](prompt_phase-26-7-installed-dashboard-acceptance.txt) | gpt-6.1-sol / xhigh |

[Sequence manifest](phase-26-sequence.yaml) fixes seven review iterations and
90-minute Cursor/reviewer budgets per run, matching the actual repo's fixed
workflow. This does not use the unimplemented Phase 24 adaptive policy. The first
six entries have existing authorized local checkpoint messages; final 26.7 has
no commit_message. Root ai_dev_loop.yaml remains unchanged.

## Out of Scope

No preparation/submission/start/tick, Git staging/commit/push, real model turns,
user-level installation, timer/service changes, live artifact/ledger reads or
Hub/Bridge implementation is performed by planning. Existing incident documents
are unrelated user work and remain intact. Phase 24/25 plans and execution
policies are not modified or treated as accepted implementation.

## Required Context and baseline

Observed source baseline: main
`90262eed1af7c76c46139855e249de4ea8ec0d5e` (`fix: make Cursor prompts non-interactive`).
Phase 23 production is present. Phase 24/25 are planning-only; phase number 26 is
available and does not imply 24/25 execution. Existing local API is 1.5 with five
read capabilities; scheduler schema is 13, Python minimum 3.11. There is no
frontend/HTTP dashboard at this baseline.

Read [shared contract/architecture](phase-26-dashboard-contract.md),
[source inventory and hashes](phase-26-source-observations.json), and
[frozen visual reference](phase-26-reference/README.md). The reference HTML and
screenshots use synthetic data and are visual guidance only. All seven plans and
prompts are portable repo files; .gitignore includes a narrow exception for these
new prompts, following the previous portable planning deliveries.

The source inspection verifies existing `integration_api/` run/sequence/review/
output/capacity readers and read-only SQLite infrastructure. It does not verify
live runtime or query real operator content. Each run rechecks actual accepted
predecessor code once; a plan/staged patch is not its implementation.

## Cursor Rules And Skills

Use AGENTS.md, all eight existing `.cursor/rules/ai-dev-loop-*.mdc` policies and
the Codex-authored [dashboard rule](../../../.cursor/rules/ai-dev-loop-dashboard-contracts.mdc).
The exact nine paths are listed in every plan. Use the repo-local
`.agents/skills/create-cursor-plan/SKILL.md` and applicable docs acceptance
skill; the scheduler reviewer uses its configured staged-execution skill.
Codex owns planning/governance. Current AGENTS/code supersede obsolete archived
phase/command restrictions. There is no `.cursor/skills/` directory.

## Architecture Guardrails

The shared service calculates semantic DTOs; both Integration CLI and local HTTP
call it in process. SQLite queries live inside ai_dev_loop, use bound params and
validated state/lineage, and never recreate scheduler transitions. New public
queries are bounded, major-compatible and capability-gated. API minor advances
additively from the observed 1.5 baseline, with no dependency on Phase 25 tokens.

Frontend React 18/TypeScript/Vite + ECharts 6/echarts-for-react 3 depends on
DashboardDataSource. A later Hub adapter can change transport while reusing
components/calculations. Python loopback HTTP has no login; packaged assets run
without Node/CDN at runtime. Fixed Host/Origin and explicit ID-based content
reads preserve local read scope. Runtime UI language is English; current product
MkDocs documentation remains Spanish.

The started cohort uses first `cursor.run_turn`/Codex-review launch request;
chat creation/preflight/probes are excluded. Project and phase lineage come from
fields, never slug heuristics. Review retries within run+iteration count once.
Parallel recorded process time is summed, clipped and split by named local day;
missing/active intervals are explicit. Activity dates and started-cohort filters
are distinct. Current capacity is independent of the historical range.

The map includes all matching stored run IDs, including replacements and
standalone runs, through explicit pages; header totals come from aggregate reads.
Partial evidence/paging/error is visible. Exact attempt selection loads metadata;
prompt/stdout/stderr require deliberate buttons and verified existing readers.
No raw content becomes summaries/logs/browser storage. Exact Cursor correction
prompt linkage is not invented. UI state and last-success observations stay in
memory; only theme persists. Refresh is bounded and visibility/Pause aware.

## Implementation Plan and sequence boundaries

1. 26.1 delivers all shared query semantics, public schemas and capability.
2. 26.2 introduces the real loopback command and typed HTTP/content adapter.
3. 26.3 introduces frontend build/test tools and overview/filter widgets.
4. 26.4 completes authoritative map/run/timeline selection and paging.
5. 26.5 completes modal and bounded explicit content inspection.
6. 26.6 adds activity and coherent visible polling/Pause/Refresh.
7. 26.7 closes packaged installation, the once-only integration gate, factual
   docs, manual acceptance site and exact future Hub handoff.

Each run owns its contracts/tests and honest documentation for that slice;
missing mandatory predecessor functionality stops dependent work. It cannot be
accepted as an unexplained residual risk merely to advance the sequence.

### Launch condition and self-hosted execution

The operator asked to launch after Hub Phase 15 finishes. Close its actual final
subphase/walkthrough before separately preparing/starting Phase 26. This package
adds no watcher/automation and starts nothing. Use a stable installed ai_dev_loop
scheduler driver while implementing its source, not an editable checkout runtime.
The source account context owns preparation; disposable test XDG is only for tests.

Existing unrelated incident modifications and these planning files mean the
checkout is not currently clean. Before an authorized launch, preserve/finalize
operator-owned changes through the normal agreed Git workflow; do not discard or
silently include incidents in an implementation patch. No worktree or commit is
created by this planning delivery.

## Testing Criteria

Each plan carries stable C261-* through C267-* IDs connecting real production
callers, independent expected values and required tests. Fake providers and
isolated ledger/artifacts support known values; actual local HTTP/browser paths
prove data reaches widgets and selected content. Existing historical fixtures
prove compatibility separately from synthetic new states.

One final manual walkthrough covers filters -> overview -> replacement map run
-> timeline attempt -> metadata modal -> explicit prompt/stdout/stderr, all activity
metrics, current capacity, Pause/Resume and hidden/error/recovery behavior. Fixture
acceptance and optional operator-source read observation are labelled separately.
No real implementation/reviewer model turns are required for acceptance.

## Validation

Use existing uv-based Python/MkDocs commands and future frontend commands only
when their owning run introduces and executes them. Each run reports commands,
exit/count/elapsed results and required contract coverage in its findings file.
26.3 validates the new Node/test/build interface. 26.7 validates built wheel/
sdist/assets in an isolated installation outside the checkout with no Node or
external browser network at runtime, and owns the single repository-wide pytest
gate. No full historical acceptance-script nesting or full-suite correction loop.

Planning validation is separately recorded in
[Phase 26 planning validation](../findings/phase-26-planning-validation.md).
It checks models/files/links/hashes/manifest/diff, not future implementation.

## Risks Or Recovery Notes

The largest new slice is 26.1's bounded validated aggregation. Preserve explicit
source coverage rather than invent historical telemetry. Source size/time bounds
may require narrowing a requested range, with a clear error and no false totals.
A higher accepted API/schema at execution needs an admission compatibility check.

Read-only rollback is ordinary code review/reversion; no dashboard migration or
state cleanup exists. An installed fixture site has explicit owned cleanup, and
uncertain ownership preserves recovery markers. Human acceptance remains pending
until the operator performs the final walkthrough. Green reviews, Git checkpoint
commits and scheduler awaiting_finalization are distinct facts.

## OpenQuestions

None for the requested product split and reviewer choices. Technical defaults are
fully stated in the shared contract; the operator can review this concrete package
before launch. Missing/incompatible accepted prerequisites or mandatory tools
found during execution are reported as new dependent-work blockers.
