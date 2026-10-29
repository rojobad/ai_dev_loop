# Phase 30.2 — Resumable historical backfill and SQLite-only statistics

## Goals

Import every recoverable historical token observation and its coverage into
SQLite. CLI/API/Dashboard token statistics then operate without external artifacts.
Expose import readiness separately from consumption coverage and preserve old
read-only compatibility.

## Non-Goals

Reconstructing never-recorded counters, rewriting old execution inputs, live
history import during implementation, deletion, deployment or visual redesign.

## Scope

Explicit bounded backfill CLI/service/jobs, trusted import from artifact-backed
sources, persisted missingness/provenance/conflicts, SQLite-only public statistics
queries including Phase 29 cohorts/model filters and read-only readiness reporting.

## Out of Scope

Live ledger/artifacts, provider runtimes/credential access, old snapshot/hash edits,
root YAML, model/session policy, retention, backup service changes and skills.

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


Verify accepted S-01–S-04. Read current `TokenUsageReadService._collect`,
`_load_published_observations`, `_historical_codex_observations`,
`_run_ids_with_ancestors`, `_sequence_run_ids`, configured-model extraction,
`DirectUsageEvidence`, per-run/sequence inspect projections and Phase 29
`SqliteDirectUsageReader`, token analytics domain/query and HTTP bounds. Preserve
cohort membership (first execution launch) and all direct lifetime consumption
for each member, not submission-time or only attempts inside the time range.

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

The SQLite path must retain unknown/unattributed attempts in denominators and
model options without opening files. Preserve valid current wire fields and
coverage semantics; additive readiness/provenance metadata follows API/schema/
capability conventions. Do not fabricate model provenance or mark current file
integrity as checked when only imported facts were validated. Numeric reads use
typed DB validation and provenance, not `authenticate_process_attempt` over files.
Content/recovery readers continue authenticating original evidence independently.

## Implementation Plan

**B-01 — Explicit bounded historical import.** Implement proposed command
`scheduler usage-backfill` with exactly one explicit scope `--all`, `--run-id ID`
or `--project-name NAME`, and explicit mode `--check` (read only) or `--apply`.
Support bounded `--batch-size`, `--max-attempts`, `--resume JOB_ID`, `--output
text|json`; validate positive integers/conflicting scope/mode/nulls and require
an existing ledger. These are planned flags, not current installed commands.
Dry-run cannot create/migrate tables, create dirs/artifacts or write progress.
Apply uses normal schema migration/store and new SQLite job/progress tables;
never pack raw evidence into job records. Snapshot eligible attempt IDs/revisions
at job creation; process deterministically in short per-attempt/batch transactions,
releasing SQLite before expensive file reads. Revalidate revision/digest at
commit to handle a simultaneous terminal publication or live ingestion.

Reuse the authenticated extractor to verify captured observations, source hashes,
invocation/outcome and per-role Codex JSONL (including pre-25 captures when valid).
Import source configured model and normalization version alongside all categories,
counts/capture status and deterministic safe diagnostics. Cursor CLI with no
counters remains unknown. Missing/tampered/malformed/contradictory sources are
classified and cannot create trusted totals. Do not silently accept source paths
or reports unrelated to the ledger attempt. Every eligible terminal inference
attempt gets a fact, even if it is unknown/unattributed. Active/unsettled attempts
are reported pending and revisited by a later job, not frozen as known zero.

**B-02 — Replay, resume, conflicts and readiness.** Store job scope/high-water,
per-attempt progress, counts and status in SQLite. Resume after interruption
never duplicates observations and uses the same scope, refusing incompatible
options. A second apply is idempotent; different jobs/normal ingestion races use
the same uniqueness/enrichment contract. Re-import restored valid sources may
upgrade unknown/partial only with compatible verified evidence; conflicts retain
existing known facts and remain visible. Source snapshots/events/artifacts, agent
state and workflow authority stay byte/hash unchanged (new stats rows/progress and
normal schema migration are the only writes). No concurrent full-ledger write lock
or automatic backfill inside Dashboard/tick/read commands.

`--check` reports current materialization/readiness: eligible terminal attempts,
persisted known/partial/unknown/unattributed, unmaterialized, active/unsettled,
missing/corrupt/conflicting sources and remaining work. It may inspect sources
within explicit bounded import scope, but does not change normal readonly stats.
For already materialized facts, readiness must remain computable without the
original source files. Inspect sources only for unmaterialized/import candidates;
absence after successful import is not new materialization debt.
Job cursor completion is not permission to delete data. Provider coverage and
materialization are separate; a successful import can still preserve unknowns.
Stable terminal scope with no unmaterialized/unsettled/conflicting items can be
statistically ready even when its original evidence contained unknown usage.
Report unrecoverable lost-source coverage explicitly; do not claim full tokens.

**B-03 — Independent run/sequence queries.** Route new-schema `scheduler usage`,
`sequence usage`, Integration `tokenUsage`, and their direct/recovery scopes to
SQLite facts. Ledger lineage/phase-attempt membership remains authority; do not
reopen recovery records or checkpoints merely to authenticate numeric membership.
Test Cursor/Codex/reviewer successors and adopted sequence generations, no double
counting/shared ancestors and no unrelated unadopted successor. For migrated old
attempts not imported, expose backfill-required plus honest unknown without file
fallback. Preserve old-schema readonly behavior without migrations; those legacy
ledgers do not yet promise database-only token recovery until explicit upgrade/import.
Known facts remain identical with sources absent, inaccessible or subsequently
modified; an explicit fresh import detects disagreements separately. A corrupt
SQLite payload fails safe with typed diagnostics, not silent totals or fallback
that conceals DB corruption.

**B-04 — Phase 29 and all statistical read surfaces.** Switch the shared direct
collector behind `SqliteDirectUsageReader` and all cohort/selected-run/model views.
Retain exact counts/categories/flows/provider-model-role-stage groupings, native
semantics, unknown model, strict magnitude bounds, date/project/cohort semantics,
selected run validation, limits/deadline/cancellation and no page-derived totals.
Run/sequence inspect must return numeric summaries when optional text/checkpoint
files are absent; keep content fields honestly unavailable and do not weaken
execution/recovery admission. No new UI or automatic polling/import is required.
Apply DUX-06/09 and keep all remaining DUX presentation decisions unchanged.

Boundary: explicit scope/check -> source validation -> typed candidate -> short
revision-checked idempotent fact/progress transaction -> restartable receipt.
SQLite-only query then uses that durable record without runtime/files; missing
import work remains visible. Abort of an import stops batches without undoing
committed facts and never controls agent runs. No artifact deletion is implemented.

## Testing Criteria

Add `tests/unit/scheduler/test_phase30_2_usage_backfill.py`,
`tests/integration/test_phase30_2_usage_backfill.py`,
`tests/integration/test_phase30_2_database_statistics.py`. Generate genuine
pre-30 fixtures through an accepted baseline writer under temp HOME/fake providers,
record baseline provenance/digests and do not relabel synthetic counters as genuine
provider/account history. Reuse existing accepted fixtures; no live transcripts.

| ID | Acceptance through production callers |
| --- | --- |
| B-01 | Actual CLI check has zero writes/migration; apply imports both Codex roles and Cursor counters plus known-zero/missing/malformed/corrupt/active cases without inference. |
| B-02 | Crash after fact commit/before cursor, resume, second job and ingestion race converge once; restored compatible evidence enriches unknown; conflicts preserve known facts. Readiness distinguishes import debt and provider coverage. |
| B-03 | Run/direct/recovery/sequence/inspect totals stay equal after losing all external sources; corrupt DB fails safe and old schemas stay readonly. |
| B-04 | Real shared Dashboard/HTTP/API query retains independent cohort/model/category/flow totals, filters/bounds and unknown counts after artifact removal; no source file opener is invoked. |

Use deterministic barriers for concurrency and independent expected totals.
Instrument source access to fail in the SQLite-only path; do not mock away the
production read adapter or supply its missing import logic in a fixture.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_database_statistics.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/dashboard/test_token_usage.py tests/integration/test_phase29_1_token_queries.py
```

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

Record B-01–B-04, CLI/help/schema changes, resume/conflict semantics, source-provenance
and exact commands in `archive/implementation-history/findings/phase-30-2-usage-backfill.md`.

## Risks Or Recovery Notes

Known facts are statistics verified at import time, not continuing source-file
verification. This does not make missing execution evidence usable for resume or
review. Do not delete historical backups before import/readiness/restore acceptance.
Never hide conflicting observations or present unimported history as not-applicable.

## OpenQuestions

None. Failed prerequisite/source formats must be classified, not guessed around.
