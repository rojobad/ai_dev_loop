# Phase 29.1: Shared token analytics queries

Status: operator-authorized scope 2026-10-19; implementation pending.
Implementer: Cursor SDK `grok-4.7` / reasoning `high`;
reviewer: `gpt-6.1-sol` / `high`.

## Goals

Deliver one bounded semantic token analytics service for the three global
Dashboard charts and a selected run, reusable through public Integration API and
loopback HTTP. Preserve authenticated evidence, unknowns and additive API
compatibility. Provide all calculations in the producer, not in chart options.

## Non-Goals

No charts in this run, token prices, new inference capture, billing-equivalent
totals, inferred actual model, event-time analytics or remote Hub implementation.

## Scope

Read-side domain/application token analytics; a narrow reusable authenticated
usage collection port; dashboard repository/service; typed public DTOs, JSON
schemas, capability/version and Integration CLI/HTTP wiring; focused tests and
current data-contract documentation. New query modules are preferable to adding
chart concerns to the existing token capture/report service.

## Out of Scope

Scheduler transitions, writes/migrations/backfills, usage observation v1 format,
provider normalization v1, SDK dispatch/recovery/capture, native provider calls,
root `ai_dev_loop.yaml`, timer/integrations/installation, credentials, frontend
runtime/assets, governance/skills and Hub repository. Preserve existing
`run.inspect` / `sequence.inspect` token reports and their schemas/meaning.

## Required Context

Read [the Phase 29 overview](phase-29-dashboard-token-analytics.md), `AGENTS.md`,
`docs/referencia/dashboard.md`, `docs/referencia/dashboard-ux.md`,
`docs/referencia/integration-api.md`, `docs/referencia/cli.md`,
`docs/operacion/estado-artefactos.md`, `docs/operacion/seguridad-privacidad.md`.

Verify baseline `04f8e52` once: `scheduler/domain/token_usage.py`,
`scheduler/application/token_usage_query.py` and `token_usage_capture.py` already
authenticate/deduplicate observations, support partial/unknown/historical usage
and expose direct versus recovery reports. Their breakdown has no model field.
Inspect authenticated invocation fields in `scheduler/application/attempt_service.py`:
`cursor_model`, Codex implementer `model`, reviewer `review_model`. Do not use
today's YAML as a historical model fallback.

Inspect `dashboard/{domain,application,adapters}`, especially `filters.py`,
`metrics.py`, `ports.py`, `queries.py`, `sqlite_read.py`, `http.py`;
`integration_api/{dashboard_models,dashboard_service,models,info,version,schemas}.py`,
`commands/integration.py`, existing dashboard schemas, and the Phase 25 usage and
Phase 26 query/server tests. Resolve absent prerequisites before dependent work.

## Cursor Rules And Skills

Follow `AGENTS.md` and `.cursor/rules/ai-dev-loop-governance.mdc`,
`ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-dashboard-contracts.mdc`, `ai-dev-loop-dashboard-refinements-contracts.mdc`
and `ai-dev-loop-docs-acceptance-contracts.mdc` in that same rules directory.
Read `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for applicable
current documentation requirements; AGENTS/current CLI supersede retired examples.
There is no `.cursor/skills` directory. Reviewer follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementer does not
invoke or edit review/governance skills. Apply DUX-04, DUX-06..09 at data boundaries.

## Architecture Guardrails

Domain calculations import no SQLite, HTTP, CLI or chart framework. An application
service coordinates typed ports for validated run-cohort membership and
authenticated usage evidence. Reuse/refactor the existing collector rather than
creating an alternate unverified artifact/JSONL reader or public private-path API.
Resolve cohort and usage against a consistent read boundary where possible;
immutable evidence bindings are checked and each result has its own observation
time, not a cross-widget atomic-snapshot claim.

Reads do not tick, reserve, repair, migrate, rename, backfill, run providers,
read credentials, call capacity probes or persist caches. Protected source reads
are confined, integrity checked and bounded. Output includes no prompts, raw
process content, sessions, paths, environments, credentials or observation IDs.
Source evidence is authoritative even when counters are unavailable.

This phase may add public read DTOs/schemas and query entry points only. No
changes to existing persisted observation bytes/hashes or normalization rules.
Stable installed scheduler executes this self-hosted sequence. No source install,
real ledger/timer mutation or live model calls as tests. Scheduler owns staging
and non-final checkpoint; no ad hoc commit/push/PR/merge by implementation.

## Implementation Plan

1. **T291-01 — Cohort and direct scope.** Add a semantic token query with the
   existing canonical Dashboard filters. Reuse the exact summary/map started-run
   membership (`first_execution_launches`), not submission dates or loaded map
   pages. Include the entire observed direct usage for each unique cohort run.
   Optional exact `runId` restricts this to one run in that same cohort/project;
   out-of-cohort/nonexistent selection fails with typed NOT_FOUND, no data from
   another run. Omitted/null runId means cohort; empty/non-string is invalid.
   Current short name for a selected run is authoritative presentation metadata.
   Recovery ancestry is never automatically expanded or summed with direct.
2. **T291-02 — Authenticated model attribution.** Refactor the read collector
   through a narrow typed port, retaining role/stage classification and unique
   observation identity (including SDK reconciliation publishers and historical
   Codex evidence). Attach configured model only from authenticated invocation
   for that exact inference attempt. Group by `(provider, configuredModel, role,
   stage)`; configuredModel is null if unrecorded. Provider+model together form
   a dimension key, not model text alone. Unknown models remain in totals and an
   explicit Unknown model group. Invalid evidence contributes existing diagnostic
   and unknown coverage semantics; do not fabricate counters/model/role for it.
   Creation, preflight-only failure, reconciliation, probes and non-inference
   effects are not independent token consumers. Distinct real inference retries
   do count; their repeated publication does not.
3. **T291-03 — Counters, coverage and disjoint flows.** Retain normalized and
   provider-native category aggregates with known count, observation count and
   coverage. Expose attempt complete/partial/unknown counts, diagnostics, selected
   run count and nullable known total. Empty eligible scope is not_applicable;
   eligible attempts with no evidence are unknown with null known total;
   recorded zero remains zero. Partial known totals are lower bounds, not
   estimates. Missing category values remain null, not zero.

   Compute the following disjoint leaves **per authenticated observation before
   grouping**, and aggregate their totals/coverage separately from overlapping
   category counters:

   | Provider/evidence | Input leaves | Output leaves |
   | --- | --- | --- |
   | Cursor v1 | input_other = input_uncached; cache_read; cache_write | output_other = output_total - reasoning_output and reasoning_output, when reasoning known; otherwise output_unsplit = output_total |
   | Codex with cached input known | input_other = input_total - cache_read; cache_read | same output rule |
   | Codex with cached input unknown | input_unsplit = input_total | same output rule |

   Codex input_other can include cache writes: its optional cache_write counter
   is not a separately additive leaf. Explain that distinction in the contract.
   Do not turn missing split evidence into zero reasoning/cache. Unsplit is a
   known amount with unknown subtype, not wholly unknown consumption. For every
   valid observation, sum(leaves) == normalized.total_tokens; group/scope flow
   sums equal known totals. Unknown attempts add coverage counts, not invented
   flow widths. Reject contradictory arithmetic through existing integrity
   semantics. Preserve native counters for the heatmap/details; never add
   input_total+cache+output_total+reasoning as one total. Return integers;
   guard unsupported numeric magnitude instead of rounding past JS safe integers.
4. **T291-04 — One typed read contract.** Add `dashboard.token-usage` to the
   fixed HTTP read allowlist with canonical scope parameters and optional runId.
   Add `integration dashboard token-usage` with matching explicit date/project/
   zone and optional `--run-id` arguments. Both invoke the same application
   query, not subprocess CLI/SQL or one run.inspect per run. A response contains
   canonical filters, observedAt, scope (`cohort`/`direct`), nullable selected-run
   metadata, cohort/source coverage, known totals, rows by the four dimensions,
   normalized/native category coverage and disjoint flow leaves. Cohort output
   is aggregated, never a run/attempt inventory. Model provenance is explicitly
   `configured`; null models remain null on the wire.

   Define strict Python DTOs and aligned versioned JSON schemas, register them
   with the existing schema registry and contract tests. Add optional capability
   `dashboardTokenUsage` (absent means unsupported) and advance API minor 1.12 to
   1.13, preserving major 1 and old clients. Existing `tokenUsage` continues to
   describe existing run/sequence reports. Unknown request parameters and bad
   values fail before storage access; no arbitrary path/SQL/command surface.
5. **T291-05 — Bounded complete computation.** Preserve 366-day range, 10,000
   relevant-run, 100,000 contributing-attempt, 5-second query and 240-KiB response
   bounds. The query deadline includes authenticated artifact collection and
   aggregation, with cancellation/deadline checks between bounded reads, not
   just SQL progress. Retain protected-reader per-artifact size limits; avoid
   unbounded historical scans or materializing a full large report per run.
   Bound attempts before loading evidence. Scope/deadline/envelope overflow
   fails with a useful typed narrow-scope error, never sampled/silently truncated
   aggregates. Ordinary missing historical evidence remains explicit unknown
   coverage. Preserve server admission/concurrency/origin/security bounds.
6. Document the new operation, configured model meaning, lifetime-of-cohort
   semantics, direct-only run report, overlapping versus disjoint categories,
   coverage, bounds and capability in current Dashboard/API/CLI references.
   State accurately that frontend charts arrive in 29.2. Record findings.

## Testing Criteria

| Contract | Production callers and independent expected outcomes |
| --- | --- |
| T291-01 | Shared query, Integration CLI and loopback HTTP: exact `[from,to)` first launch and project match map/summary, submitted-before-start edge, runs started earlier excluded, in-scope run's later attempts included, no selection uses all projects, duplicate map pages irrelevant, exact run restricted, out-of-scope NOT_FOUND and no recovery double count. |
| T291-02 | Real protected fixture/authentication paths: Cursor SDK + historical CLI evidence + Codex implementer/reviewer use invocation-specific configured models; absent model Unknown; same spelling across providers distinct; initial/correction/retries; SDK reconciliation replay counted once; tampered/missing evidence never guessed or exposed. |
| T291-03 | Cursor input100/cache-read20/cache-write5/output15/reasoning3 => total140, leaves100/20/5/12/3. Codex input100/cached20/output15/reasoning3 => total115, leaves80/20/12/3 (not135). Codex missing cache/reasoning => input_unsplit100/output_unsplit15. Mixed known/unknown observations split individually, not from partial aggregate subtraction. Native cache_write remains visible but never adds a second Codex leaf. Recorded zero differs from unknown/null and no-inference; partial counts/totals agree with existing reports. |
| T291-04 | Public schema/model validation, real CLI dispatch and HTTP dispatcher produce equivalent semantic rows/totals/errors, explicit capability, major-compatible old consumers, null/absent/empty forms and rejected unknown parameters; no body/session/path/credential leakage. |
| T291-05 | Isolated DB/artifacts remain byte/row unchanged, including supported historical schema reads; deterministic fake monotonic/cancellation during evidence loop; independently exceed run/attempt/size bounds; no partial successful total, no outbound provider/probe, no real state roots. |

Add focused unit tests under `tests/unit/dashboard/` (suggest
`test_token_usage.py`), public DTO/schema tests and integration
`tests/integration/test_phase29_1_token_queries.py` using temporary ledger,
protected artifacts and fake SDK/Codex evidence. Reuse actual fixture producers
and authentication assertions from Phase 25; a fake aggregate alone cannot prove
dedup/authentication. Fixed arithmetic expectations must not call the function
under test to compute expected values.

## Validation

Run at minimum, adjusting only new test filenames when the chosen module naming
differs; document the exact substitutions and production coverage:

```bash
uv run --no-sync python -m pytest -q tests/unit/dashboard tests/integration/test_phase29_1_token_queries.py tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/integration/test_phase25_4_token_usage.py tests/integration/test_phase25_4_dashboard_compatibility.py
uv run --no-sync python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase29-1-docs
git diff --check
```

Also run affected API schema/version/CLI compatibility tests discovered from
changed callers. After corrections rerun failed/affected checks, not the whole
repository. Full-suite integration gate belongs after 29.2. Findings at
`archive/implementation-history/findings/phase-29-1-token-queries.md` must map
T291-01..05 to production files, tests and exact executed/failed/unexecuted
commands. Missing mandatory behavior or evidence is unfinished scope.

## Risks Or Recovery Notes

Current usage collection was designed for run/sequence reports; do not bypass
integrity or deadlines to scale it globally. Missing evidence/model is expected
historically, not license to query a live provider or today’s config. Keep model
provenance honest. A schema/API mismatch or overflow is a typed read failure;
do not migrate/repair records or change execution outcome to make a chart work.
If accepted prerequisite semantics conflict, report evidence before dependent
work; preserve the existing reports and protected data.

## OpenQuestions

None.
