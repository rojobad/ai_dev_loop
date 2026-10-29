# Phase 30 — Durable SQLite token statistics and historical backfill

## Goals

A consistent backup containing **only `engine.sqlite3`** must preserve every
already-recorded statistic: projects/runs/sequences, short names, states, rounds,
observed times and token totals/categories/coverage by provider, configured model,
role and stage, with direct/recovery/phase/cohort scopes. Preserve unknown values
as unknown. External text, patches, transcripts and runtime stores are deliberately
outside this statistical guarantee; new tables must not contain them.

The operator requests planning and preparation on Beelink, not starting execution.
Prepare a named three-phase sequence; do not start it, install changing code or
apply a production backfill during this planning delivery. Chosen agents are
Cursor `grok-4.7`, context `256k`, effort `high`, fast false; reviewer
`gpt-6.1-sol` / `xhigh`, independently frozen per phase. Current 90-minute role
budgets and seven-review ceiling remain unchanged.

## Non-Goals

A database-only backup is not a resumable agent runtime or a complete text audit.
No billing/USD, invented missing counters, backup infrastructure, artifact deletion,
retention changes, provider changes, UI redesign or deployment automation.

## Scope

| Slice | Plan | Observable delivery |
| --- | --- | --- |
| 30.1 | [SQLite facts and ingestion](phase-30-1-sqlite-usage-ingestion.md) | Versioned compact facts and authenticated durable ingestion for new attempts, replay/recovery/abort included. |
| 30.2 | [Backfill and independent queries](phase-30-2-usage-backfill-and-queries.md) | Explicit bounded resumable backfill plus SQLite-only CLI/API/Dashboard statistics. |
| 30.3 | [Database-only restore acceptance](phase-30-3-database-only-restore-acceptance.md) | Isolated backup/restore equivalence, affected installed-path gates and operator runbook. |

Prompts: [30.1](prompt_phase-30-1-sqlite-usage-ingestion.txt),
[30.2](prompt_phase-30-2-usage-backfill-and-queries.txt),
[30.3](prompt_phase-30-3-database-only-restore-acceptance.txt).
Manifest: [phase-30-sequence.yaml](phase-30-sequence.yaml).

## Out of Scope

No production source implementation in this planning turn. Implementation must
not modify live ledger/artifacts, provision credentials, run the new backfill on
operator history, delete any source files/stores, change restic/timers, alter root
YAML or planning/review skills, or publish Git changes. Normal scheduler staging
and authorized non-final checkpoints retain their existing boundaries.

## Required Context

This work is authored/prepared/executed on **Beelink-SER5** only, checkout
`/home/rodrigo/project/ai_dev_loop`. Baseline is `1a97becaff1af8c54f892f42740f040d4cd7478c`. Phase 29 token analytics,
Phase 25.7 retention and Phase 25.8 no-progress recovery are implemented. Inspect
accepted source/tests, not older Phase 25 assumptions. SQLite is currently v20;
API is 1.13. Recheck version allocation before editing.

Read [the Phase 30 overview](phase-30-sqlite-token-statistics.md). Preserve the
independent installed driver and frozen source config throughout this sequence.
Never install editable/changing source into that driver. Temporary Linux homes,
SQLite and fake providers are required for tests. Do not execute live backfill,
provider inference, runtime installation, cleanup, timer/backup-service changes,
push/PR/merge or unrelated Git actions as implementation side effects.


Evidence inspected on Beelink: clean main baseline, 171 retained scheduler runs,
no usage/token tables; `token_usage_capture.py` writes protected observations and
`TokenUsageReadService._collect` authenticates files on every query. Phase 29
`SqliteDirectUsageReader` calls the same collector. Missing artifacts currently
turn known totals into unknown, including model attribution and category views.

Read `token_usage_capture.py`, `token_usage_query.py`, `domain/token_usage.py`,
`attempt_service.py`, Cursor/Codex workflow services and workers, SDK
reconciliation/successor/no-progress services, SQLite migrations/schema readers,
recovery/phase lineage, Integration run/sequence projections, and
`dashboard/application/token_usage.py`, `dashboard/domain/token_analytics.py`,
`dashboard/adapters/token_usage_read.py`, `dashboard/adapters/sqlite_read.py`.
Use accepted Phase 25.4/29 fixtures/tests and current CLI reference/help.

## Cursor Rules And Skills
- `AGENTS.md`.
- `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-docs-acceptance-contracts.mdc`,
  `ai-dev-loop-global-integrations-contracts.mdc`, `ai-dev-loop-dashboard-contracts.mdc`,
  `ai-dev-loop-dashboard-refinements-contracts.mdc` (all paths under `.cursor/rules/`).
- `.agents/skills/create-cursor-plan/SKILL.md` and
  `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`.
- Preserve `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; reviewer B
  follows its separate contract. No `.cursor/skills/` directory exists.

Read `docs/referencia/dashboard-ux.md` and `docs/referencia/dashboard.md`.
Affected DUX-06/09: bounded coherent read-only scopes and shared data queries.
Preserve DUX-01–08 presentation, labels, model filtering, dates and layout. No UI
redesign or new controls are needed. All governance applies to either implementer.

## Architecture Guardrails

**SQLite is the durable statistics authority after verified import/capture.**
Retain protected artifacts for detailed audit and execution/recovery under their
current policies, but numeric queries must no longer reopen them to validate
persisted facts. Record that validation occurred at ingestion/backfill, not a
claim that original external bytes were rechecked at read time. This is the
operator-approved adjustment to Phase 25's artifact-dependent metrics contract.
Do not weaken artifact authentication for review decisions or explicit content.

Persist compact per-attempt attribution/completeness facts and per-contribution
observations, not only totals or a single cached report. Keep schema/normalization
versions, native+normalized counters, stable observation IDs, logical attempt,
publisher/dispatch identity, configured model (nullable), stage/role/provider,
terminal/capture state, known/partial/unknown/not-applicable status, bounded safe
diagnostic codes, verified-at timestamp, import origin and source relative
paths/digests. Full session IDs, prompts, free-text errors, raw provider messages,
keys, absolute private paths and copied artifacts do not belong in these tables.

Reuse current deduplication identity and normalization: one Cursor native run
per logical inference attempt; SDK reconciliation publishers are not a second
consumption. Codex contributions use completed-turn ordinal and role. Do not sum
cache/reasoning subsets twice. Unknown optional categories stay null. Preserve
strict integer semantics, including zero; never coerce bool/string/fraction or
truncate overflow. Use canonical typed JSON for compact native/normalized payloads
if needed to preserve integer range; aggregation must not silently overflow SQL
or change existing public magnitude/error limits.

Store attribution and missingness even when no usable counters exist, so the
inference denominator and model-filter options survive artifact loss. Model
comes from authenticated invocation at import/capture, never current YAML. Use
validated ledger effect semantics for unattributed historical attempts and
expose uncertainty; no guessed roles or fabricated models. Preflight/catalog,
bridge health and reconciliation-only operations are not new inference.

New tables are additive (next free migration, expected 21); maintain pre-30
read-only schema compatibility without hidden migration or backfill. Legacy
artifact reading remains an explicit import/old-schema compatibility mechanism,
not an unconditional prerequisite of new-schema statistics queries. On a migrated
but unmaterialized history show `backfill_required` diagnostics and honest unknown
coverage, not silent automatic file scans, zero or disappearing runs.

Ingestion validates evidence through existing trust boundaries **before**
persisting facts, independently of operational success/failure. Importing metrics
cannot approve work, stage, create a prompt, retry, transfer reservations or
alter budgets. DB-only statistics do not authorize resuming old agent sessions.
Use typed store/repository ports and normal transactions; no worker-owned
unfenced second SQLite writer or replacement snapshot authority.

## Implementation Plan

Execute 30.1 -> 30.2 -> 30.3. Only two non-final scheduler checkpoints may commit
locally after acceptance; final changes remain staged. Plans/manifest are authored
and frozen in Beelink; NucBox and its ledger/runtime remain untouched.

Historical migration has two completeness axes: **materialization** (which
eligible attempts are represented in SQLite) and **provider coverage** (how much
consumption had usable counters). A fully materialized history may correctly
remain partial/unknown. The readiness report must distinguish missing import work,
active attempts, unavailable sources and conflicting/corrupt evidence. No procedure
may say 'safe to delete' based only on a successful schema migration or job cursor.
This phase implements no deletion command.

## Testing Criteria

Acceptance uses real scheduler/CLI/application queries with fake transports,
independent expected counters and deterministic crash/race barriers. Use SQLite
backup API to restore one file into a fresh HOME with no artifacts, provider
binaries, credentials, stores or target repositories. All statistical fields
must remain equal except observation timestamps. Explicit text reads may report
not-recorded and runtime recovery is outside that guarantee.

Recheck project/run/state/round/time statistics already in SQLite alongside new
tokens, model scopes, current short names and sequence/recovery scopes. Test
unknown/partial/null/zero, malformed/conflicting evidence, multiple Codex turns,
SDK replay and imported failures/aborts. No live history is copied into checked-in
fixtures, and production backup services remain untouched.

## Validation

Use explicit test paths/cases only. **DO NOT RUN THE REPOSITORY-WIDE SUITE**, its
wrappers or an equivalent full-suite run split into batches. This operator
constraint applies to implementation, corrections and review, including 30.3.
The global pipeline gate remains separately pending, without waiving any mandatory
phase coverage. After a fix rerun failed/affected tests; broaden only for a
specific remaining risk. Record executed, failed, skipped and unexecuted checks.

Run Ruff lint/format on changed maintained files, `python -m mypy src/ai_dev_loop`,
strict MkDocs for changed docs, and `git diff --check` with the available isolated
`.venv/bin/python`/uv tooling. No new tests solely mirroring documentation wording.
Existing Node/npm is absent; no new frontend behavior is authorized. If DTO or
frontend source must change, preserve existing build provenance and record any
unavailable focused frontend checks rather than install tools or weaken tests.

Each slice names focused commands and acceptance IDs; findings map IDs to actual
callers/tests. Full pytest stays a later separately authorized gate. Plan readiness
and sequence preparation are not SDK implementation, live backfill or restore
acceptance. Deployment then production import/readiness verification and the
operator's backup policy switch are later operator actions, after final acceptance.

## Risks Or Recovery Notes

Import historical data **before** discarding artifact backups. Missing/deleted
sources cannot recreate counters; record the loss honestly and allow a later
explicit retry if valid sources are restored. Same-key conflicts must preserve
previous known facts and report the conflict, never silently replace or add them.
A digest detects accidental inconsistency, not protection against an attacker
rewriting SQLite and its digests. Retain native semantic/version provenance.
Old runtimes cannot safely read a new-schema backup: restore with a compatible
package. Installation does not itself materialize historical statistics.

## OpenQuestions

None for the specified design. Failed source/runtime prerequisites are reported
before dependent work. Any proposal to delete artifacts, copy text into SQLite,
change retention, switch providers or run a live backfill needs separate scope.
