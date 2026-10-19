# Phase 29: Dashboard token analytics

Status: scope authorized on 2026-10-19; implementation pending. Preparation is
authorized; sequence start, implementation, final commit/push/PR/merge,
installation and timer changes are separate operator actions.

## Delivery

| Run | Deliverable | Implementer | Reviewer |
| --- | --- | --- | --- |
| [29.1](phase-29-1-token-queries.md) | Shared bounded token analytics, model attribution, disjoint flow categories, public API and HTTP projection, including a selected run | Cursor SDK `grok-4.7`, reasoning `high` | `gpt-6.1-sol`, `high` |
| [29.2](phase-29-2-token-widgets.md) | Linked Sankey, Sunburst and heatmap; selected-run consumption widget; packaged assets and joint manual walkthrough | Cursor SDK `grok-4.7`, reasoning `high` | `gpt-6.1-sol`, `high` |

Use [the v4 manifest](phase-29-sequence.yaml). Both phases freeze context `256k`,
fast `false`, SDK `1.0.37` inherited from config v2, a 90-minute implementer
timeout, a 90-minute reviewer timeout and a maximum of seven review iterations.
The SDK selection is a structured model/parameter tuple, not the retired CLI
alias `grok-4.7-high`. Each run has its own Cursor agent and independent Codex
reviewer. This is ordinary product implementation through the SDK, not authority
to add live model smoke tests to automated validation.

## Baseline and scope decisions

Accepted source baseline: main `04f8e52a297bfd895e7a4e1cfa720b8e8d97894c`,
including Phase 25 SDK/token API, Phase 26 Dashboard, Phase 28 refinements and
DUX-01..09. Recheck actual accepted predecessor once before each dependent run.
Current public API is 1.12; its existing token reports group provider/role/stage,
not model. Token widgets are not yet implemented. The installed tool was checked
to offer API 1.12, manifest-v4 preparation and optional `cursor-sdk==1.0.37`.

- Global analysis groups by model, role and token categories, across runs,
  sequences and projects. Project is an optional Inspection scope filter.
- Preserve existing started-run cohort semantics: first owned inference launch
  in `[fromUtc,toUtc)`, exact authoritative project. Include the full observed
  direct consumption of those runs, not only token events within that interval.
  This is **usage of runs started in scope**, not usage incurred during the
  interval. No new token timestamp inference or activity-calendar semantics.
- Global model/role selection links only the three token charts. Selecting a
  run in the existing map updates its consumption widget and Attempt timeline;
  it does not restrict global charts. Model/role facets do not change the map,
  Inspection scope, selected run or existing activity calendar.
- Model means the **configured model from authenticated attempt invocation**.
  Label provenance in information; do not claim actual provider execution model
  or guess a historical missing value. Keep provider part of model identity.
- The run widget shows its direct consumption, never added recovery ancestry.
  Global scope deduplicates run and observation identities; recovery runs count
  as separate actual executions only when they belong to the cohort themselves.
- Flow charts and the run's stacked bar use a disjoint partition supplied by the
  backend. Heatmap uses named normalized counters, which can overlap; its
  columns must not be summed. Unknown category data is not zero.
- No cost, prices, provider billing reconciliation, per-attempt token view,
  Hub/Bridge implementation, new ledger migrations or usage capture changes.

## Execution and acceptance

Both runs execute sequentially after a separate explicit start. Only accepted
29.1 enables 29.2. Existing scheduler checkpointing owns the intermediate commit;
29.2 leaves final work staged and awaiting operator finalization. Use a stable
installed scheduler driver, not an editable installation of the changing source.

Before start, review and commit the planning package on its feature branch so
the target worktree satisfies the existing clean admission policy. Preparation
freezes these plan/prompt bytes and configuration, but neither commits files nor
starts models. If frozen input changes, prepare a new definition; never edit the
protected frozen artifacts or silently start an obsolete definition.

Run focused mandatory checks per subphase. Run the repository-wide suite once
at the separate final integration gate, after both phases, not every correction.
The final phase authors a single manual walkthrough covering all four widgets,
scope/filter/selection behavior, unknown data, information controls, formatting
and scroll. Human acceptance is recorded only after the operator performs it.
Installation and scheduler/timer updates require their separate authorization.
