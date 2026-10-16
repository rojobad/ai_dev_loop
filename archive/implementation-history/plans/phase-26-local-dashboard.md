# Phase 26 — Local dashboard and reusable query API

Status: operator-requested seven-run planning package, authored 2026-10-14,
refreshed and replacement launch authorized 2026-10-15.
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
in 26.7. Implementer is Codex `gpt-5.6-sol / xhigh` in every run, explicitly
selected on 2026-10-15. The operator selected
`gpt-6.1-sol / xhigh` for 26.1, 26.2, 26.7 and `/ high` for 26.3–26.6 on
2026-10-14. Each manifest entry freezes both reviewer fields explicitly.

| Subphase | Plan / accepted delivery | Implementation prompt | Reviewer |
| --- | --- | --- | --- |
| 26.1 | [Shared queries and Integration API](phase-26-1-shared-dashboard-queries.md) | [Prompt](prompt_phase-26-1-shared-dashboard-queries.txt) | gpt-6.1-sol / xhigh |
| 26.2 | [Local HTTP read server](phase-26-2-loopback-read-server.md) | [Prompt](prompt_phase-26-2-loopback-read-server.txt) | gpt-6.1-sol / xhigh |
| 26.3 | [Shell, filters and overview charts](phase-26-3-dashboard-shell-and-overview.md) | [Prompt](prompt_phase-26-3-dashboard-shell-and-overview.txt) | gpt-6.1-sol / high |
| 26.4 | [Linked map and timeline](phase-26-4-linked-run-map-and-timeline.md) | [Prompt](prompt_phase-26-4-linked-run-map-and-timeline.txt) | gpt-6.1-sol / high |
| 26.5 | [Attempt modal and explicit content](phase-26-5-attempt-modal-and-content.md) | [Prompt](prompt_phase-26-5-attempt-modal-and-content.txt) | gpt-6.1-sol / high |
| 26.6 | [Activity and visible refresh](phase-26-6-activity-and-visible-refresh.md) | [Prompt](prompt_phase-26-6-activity-and-visible-refresh.txt) | gpt-6.1-sol / high |
| 26.7 | [Installed acceptance and Hub handoff](phase-26-7-installed-dashboard-acceptance.md) | [Prompt](prompt_phase-26-7-installed-dashboard-acceptance.txt) | gpt-6.1-sol / xhigh |

[Sequence manifest](phase-26-sequence.yaml) fixes seven review iterations and
90-minute implementer/reviewer budgets per run. Native manifest v2 freezes
the Codex implementer separately; conflicting Cursor phase overrides are omitted. This does not use the unimplemented Phase 24 adaptive policy. The first
six entries have existing authorized local checkpoint messages; final 26.7 has
no commit_message. Root ai_dev_loop.yaml remains unchanged.

## Out of Scope

No preparation/submission/start/tick, Git staging/commit/push, real model turns,
user-level installation, timer/service changes, live artifact/ledger reads or
Hub/Bridge implementation is performed by planning. Existing incident documents
are unrelated user work and remain intact. Phase 24/25 plans and execution
policies are not modified or treated as accepted implementation.

## Required Context and baseline

Refreshed source baseline: main
`466b8e09724430cebddbdeca6f1961b2d6243531` (merged Phase 27.1–27.7 dual
implementer/recovery code). Phase 24/25 are planning-only; Phase 26 keeps its
original product number. Existing local API is 1.8 with eight read capabilities;
scheduler schema is 16, Python minimum 3.11. There is no
frontend/HTTP dashboard at this baseline.

Read [shared contract/architecture](phase-26-dashboard-contract.md),
[source inventory and hashes](phase-26-source-observations.json),
[original source observation](phase-26-source-observations-2026-10-14.json), and
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
additively to 1.9 from the observed 1.8 baseline, with no dependency on Phase 25 tokens.

Frontend React 18/TypeScript/Vite + ECharts 6/echarts-for-react 3 depends on
DashboardDataSource. A later Hub adapter can change transport while reusing
components/calculations. Python loopback HTTP has no login; packaged assets run
without Node/CDN at runtime. Fixed Host/Origin and explicit ID-based content
reads preserve local read scope. Runtime UI language is English; current product
MkDocs documentation remains Spanish.

The started cohort uses first Cursor implementation, Codex implementation or
Codex-review execution launch request;
chat creation/preflight/probes are excluded. Project and phase lineage come from
fields, never slug heuristics. Review retries within run+iteration count once; Codex implementation/correction
attempts do not count as reviewer rounds. Preserve provider/role/retry metadata
and authoritative implementer_retry phase lineage.
Parallel recorded process time is summed, clipped and split by named local day;
missing/active intervals are explicit. Activity dates and started-cohort filters
are distinct. Current capacity is independent of the historical range.

The map includes all matching stored run IDs, including replacements and
standalone runs, through explicit pages; header totals come from aggregate reads.
Partial evidence/paging/error is visible. Exact attempt selection loads metadata;
prompt/stdout/stderr require deliberate buttons and verified existing readers.
No raw content becomes summaries/logs/browser storage. Exact per-attempt implementer prompt
linkage is not invented for either provider; run initial prompt is contextual. UI state and last-success observations stay in
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

The original scheduling preference was after Hub Phase 15. The operator's
2026-10-15 instruction explicitly authorizes aborting/replacing the old sequence,
updating these plans, committing pending work, creating `codex/phase-26-local-dashboard`
and starting this replacement there. The previous sequence
`ai-dev-loop-seq-c062d0252688` was observed aborted with zero materialized runs and
all seven future entries cancelled; it cannot run again. No Hub runtime/code or
its separate manual acceptance is changed by this authorization.

Use the stable installed ai_dev_loop driver (verified API 1.8/schema 16), not an
editable changing checkout. Preserve source-user context for preparation/start;
disposable XDG is only for validation. Planning/review skills and root YAML stay
unchanged. The operator authorized committing pending incident documents as part
of leaving this repo clean; preserve their contents in a separate documentation
commit before creating/starting the Phase 26 branch. No push/PR/public deployment
is part of this request. Source preparation/start must happen after the clean
branch/committed-plan state exists, so first admission freezes the intended branch.

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
The [Codex relaunch validation](../findings/phase-26-codex-relaunch-validation.md)
records the 2026-10-15 manifest/provider refresh and focused producer checks.

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
