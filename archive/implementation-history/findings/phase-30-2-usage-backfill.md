# Phase 30.2 usage backfill and queries findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rodrigo/project/ai_dev_loop` on Beelink-SER5,
branch `codex/phase-30-sqlite-statistics`, HEAD
`ec51b7e899ef7f0ad5f5badad05d35c44fc9e7ac`
(`feat: persist authenticated token statistics in SQLite`).
No commit, push, pull request, or merge was made. The scheduler owns any
authorized intermediate checkpoint. NucBox was not used.

OpenQuestions: none. API minor stays 1.13. Numeric SQLite integrity is
separate from source-content authentication and from operational recovery
authority. A usage row still does not authorize recovery, review, Git, or
another prompt.

## Admission and prerequisite evidence

Checked before dependent work:

```bash
git merge-base --is-ancestor 1a97becaff1af8c54f892f42740f040d4cd7478c HEAD
```

Result: **exit 0**. Baseline `1a97becaff1af8c54f892f42740f040d4cd7478c` is an
ancestor of HEAD. Phase 30.1 is the current HEAD and its findings record
S-01–S-04 as implemented on schema 21. This tree still has
`0021_usage_statistics.sql`, `settle_ingested_attempt_usage`, and
`merge_usage_snapshot`.
`archive/implementation-history/plans/phase-30-2-usage-backfill-and-queries.md`
lists OpenQuestions: None. Dependent work was not stopped.

No installed scheduler driver, live `engine.sqlite3`, production artifact,
timer, backup service, cleanup, or real Cursor/Codex model was touched.
Tests used temporary HOME/XDG roots, temporary repositories, and fake
providers/backends.

## Persisted contract

Migration `0022_usage_backfill.sql` advances `SCHEMA_VERSION` to 22.
`USAGE_STATISTICS_SCHEMA_VERSION` stays 21. The migration creates job tables
only. It does not walk artifacts or infer consumption.

Tables:

- `scheduler_usage_backfill_jobs`
- `scheduler_usage_backfill_items`

Index: `idx_scheduler_usage_backfill_items_status`.

Job rows store scope, bounds, high-water, snapshot count, and status.
Item rows store attempt id, ordinal, revision, status, and diagnostic codes.
They do not store prompts, patches, source text, or session ids. Import origin
on facts stays `ingestion` or `reconciliation`. The job id lives on the job
row.

`schema_supports_usage_statistics` remains user version >= 21 with the facts
table. `schema_supports_usage_backfill` is user version >= 22 with the jobs
table. Readonly open of a version-21 database verifies the v21 tables and does
not create job tables. `scheduler usage` and `usage-backfill --check` do not
migrate. `--apply` opens the normal store and migrates an existing ledger.

## B-01 — Explicit bounded historical import

Production callers:

- `scheduler usage-backfill` in `src/ai_dev_loop/cli.py`
  (`scheduler_usage_backfill_command`)
- `parse_usage_backfill_request` and `run_usage_backfill` in
  `src/ai_dev_loop/scheduler/application/usage_backfill.py`

The command requires exactly one of `--all`, `--run-id`, or `--project-name`,
and exactly one of `--check` or `--apply`. `--resume` implies apply and cannot
combine with `--check`. Bounds are integers from 1 to 100000. Defaults are
batch size 32 and max attempts 100. A missing ledger raises not-found and does
not create the database, its parents, or an artifact directory.

`--check` uses `SqliteSchedulerStore.open_readonly`. It reports readiness from
SQL. It classifies sources only for unmaterialized attempts, and only after
the read transaction is released. `--apply` snapshots eligible terminal
inference attempt ids and revisions, then imports one attempt at a time up to
`max_attempts`. File classification happens outside the write transaction.
Commit rechecks the revision. A changed revision leaves the item pending,
stops the job, and does not advance high-water.

Eligible effects are the existing inference set: Cursor run turn and Codex
review bootstrap/resume plus Codex implementer bootstrap/resume. A terminal
`cursor.reconcile_sdk_run` publication for that logical attempt is imported
onto the same fact, with reconciliation provenance, and its revision is part
of the item revision. It is not a second consumption row. Terminal statuses
are completed, failed, cancelled, and uncertain. Active attempts stay out of
that snapshot. Check counts them as `active_unsettled` and does not record
them as known zero. The default text receipt includes `job_id` and
`job_status`.

Independent counters from fake providers, not recomputed by the code under
test: Codex implementer native 100/60/10/4 is total 110; review 25/5/5 is
total 30; together the direct total is 140. Cursor native
4820/139/2560/0/7519/118 is input total 7380 and total 7519, including known
cache write 0. One review of 30 makes the SDK direct total 7549. Models are
`gpt-5.5`, `gpt-5.6-sol`, `grok-4.7`, and `gpt-6.1-sol`.

Tests:

- `tests/unit/scheduler/test_phase30_2_usage_backfill.py` for flag conflicts,
  missing ledger, empty ready/`not_applicable`, schema 21 check with unchanged
  bytes, unknown run/project, and the CLI `--check` JSON path
- `tests/integration/test_phase30_2_usage_backfill.py` for Codex roles,
  project scope, active status, max-attempts resume, malformed `{}` envelope,
  and corrupt trailing-byte envelope

## B-02 — Replay, resume, conflicts, and readiness

Same module. `UsageBackfillBarrier("after_fact_before_cursor")` is a test hook
on the store after the fact transaction commits and before the item/high-water
transaction. Resume of that job replays the committed fact and finishes the
snapshot. Observation count stays 2.

A second apply is a new job and goes through `merge_usage_snapshot`. The
ingestion race calls `settle_ingested_attempt_usage` from `before_commit`
before the backfill write. The unique observation count stays 2 and the direct
total stays 140.

Same-key conflict rewrites one published observation and its authenticated
binding, then applies again. Stored normalized totals stay at the imported
values, the public total stays 140, and `conflicting` stays visible, so the
scope is not statistically ready. Restoring a malformed or corrupt envelope
after an unknown import enriches back to 140. Deleting the artifact tree after
import does not erase the original ledger's 140. A copy whose facts were
deleted first imports unknown coverage, reports `usage_source_missing`, and
can be statistically ready while `provider_coverage` is not complete.

Materialization is complete when no eligible terminal attempt is unmaterialized
and no fact is pending. Unmaterialized terminal attempts stay in
`unmaterialized` and also enter the provider denominator as unknown, so an
entirely unimported scope is `unknown` rather than `not_applicable`. An empty
scope stays `not_applicable` and ready. A partial import is `partial` and not
ready. Statistical readiness also requires no active attempt, no pending fact,
no unresolved debt, and no conflicting fact. Settled unknown provider coverage
does not by itself block readiness. Debt with null coverage is not counted as
that settled unknown; it increases `active_unsettled` and `remaining`.
Surviving observation ids on a debt fact still contribute their counters to
the public total. Lost source is reported and is not new materialization debt
after a successful import. Completing a job does not delete facts. Backfill is
not called from tick, Dashboard, or abort.

## B-03 — SQLite-only run and sequence queries

Production caller: `TokenUsageReadService._collect` in
`src/ai_dev_loop/scheduler/application/token_usage_query.py`. Schema 21+ uses
`_collect_from_facts`. Older schemas keep `_collect_from_artifacts` and are not
migrated by the read. The fact path validates payload and observation digests and loads only
`UsageFactPayloadV1.observation_ids`, in that order. Each required row must
exist and belong to that fact's run, logical attempt, provider, role, and
stage. Its publisher must be the fact publisher, the logical attempt, or a
terminal reconciliation publisher for that attempt. A missing or mismatched
required row raises `SchedulerEngineError` kind `CORRUPTION` with
`usage statistics payload is invalid`. Rows that are not in `observation_ids`
do not enter totals. There is no file fallback.

`scheduler usage` and Integration `run inspect` / `sequence inspect` use that
service. Direct and recovery-lineage totals for the SDK run stay 7549 with the
artifact tree removed and with usage-source openers instrumented to fail.
The updated Phase 25.4 sequence inspect, after the artifact root is removed,
keeps known total 170 and partial coverage. The schema 11 historical fixture
still reads artifacts and does not migrate. Unmaterialized terminal attempts
report `backfill_required` and an unknown total.

Tests: `tests/integration/test_phase30_2_database_statistics.py`,
`tests/integration/test_phase25_4_token_usage.py`, and
`tests/unit/integration_api/test_phase25_4_historical_usage.py`.

## B-04 — Phase 29 Dashboard statistics

Production callers:

- `SqliteDirectUsageReader.collect` in
  `src/ai_dev_loop/dashboard/adapters/token_usage_read.py`
- `default_dashboard_read_service().token_usage`
- `DashboardHttpDispatcher` operation `dashboard.token-usage`

They use the same fact collector. No Dashboard UI, DTO, or frontend file
changed. DUX presentation is unchanged. Cohort membership stays first execution
launch plus direct lifetime consumption. A 2020 window over the SDK run returns
`knownTotalTokens: null` and no rows. The launch window returns cursor
`grok-4.7` at 7519, review `gpt-6.1-sol` at 30, cache read 2560, reasoning
output 118, and known cache write 0. After artifact removal those figures stay.
A corrupt fact becomes Dashboard `DATA_INTEGRITY`
(`Scheduler dashboard data failed integrity validation.`) and Integration
`DATA_INTEGRITY` (`Scheduler run state is inconsistent.`). The CLI prints
`usage statistics payload is invalid`.

The Phase 29 tests that used to treat a tampered observation file or a removed
invocation model as a public invalidation now keep the imported total 7519 and
model `gpt-5.5`. Unit aggregation of contradictory totals still does not go
through the reader.

## Validation

Mandated focused command, after the behavioral edits:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_database_statistics.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/dashboard/test_token_usage.py tests/integration/test_phase29_1_token_queries.py
```

Implementation result, before this correction: **exit 0, 69 passed in 96.12s**.
None failed or skipped. Ruff format then wrapped
`tests/integration/test_phase30_2_usage_backfill.py` only. That affected file
was rerun:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase30_2_usage_backfill.py --tb=line
```

Result: **exit 0, 1 passed in 2.91s**.

Schema assertions and rewind helpers that drop usage tables also drop the two
new job tables. Affected nodes, not part of the mandated command:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase23_4_sequence_recovery.py::test_schema_migration_reopens_v12_without_rewriting_lineage tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_adds_sdk_reconciliation_ledger tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_fault_rolls_back_table_and_audit_row tests/integration/test_phase28_short_names.py::test_schema16_read_fallback_and_v17_migration_preserve_authority tests/integration/test_phase28_short_names.py::test_schema17_migration_fault_rolls_back_all_presentation_tables tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py::test_v14_migration_adds_relation_without_rewriting_existing_runs tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py::test_v15_migration_adds_sequence_replacement_without_rewriting_runs tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py::test_cli_inspection_does_not_create_or_migrate_a_ledger tests/unit/scheduler/test_phase30_1_usage_store.py tests/unit/scheduler/test_phase20_1_reviewer_retry.py::TestSchemaMigration::test_v6_migration_adds_review_retry_tables tests/unit/scheduler/test_phase20_8_sequence_review_retry.py::TestPhase208Schema::test_v10_migration_adds_sequence_execution_replacement_table tests/unit/scheduler/test_phase23_2_initial_recovery.py::test_schema_migration_adds_initial_recovery_relation --tb=line
```

Result: **exit 0, 18 passed in 3.81s**. None failed or skipped.

```bash
.venv/bin/ruff check src/ai_dev_loop/scheduler/application/usage_backfill.py src/ai_dev_loop/scheduler/application/token_usage_query.py src/ai_dev_loop/cli.py tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_database_statistics.py
.venv/bin/ruff format --check src/ai_dev_loop/scheduler/application/usage_backfill.py src/ai_dev_loop/scheduler/application/token_usage_query.py src/ai_dev_loop/cli.py tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_database_statistics.py
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-mkdocs-phase30-2
git diff --check
```

Results: Ruff check and format check **exit 0**. Mypy **exit 0**, 228 source
files. Strict MkDocs **exit 0**, built in 0.62 seconds to
`/tmp/ai-dev-loop-mkdocs-phase30-2`. The Material theme printed its existing
MkDocs 2.0 warning. Strict mode did not fail. `git diff --check` **exit 0**.

Correction rerun of the same mandated command, after F-01 through F-06:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_database_statistics.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/dashboard/test_token_usage.py tests/integration/test_phase29_1_token_queries.py
```

Result: **exit 0, 72 passed in 105.82s**. None failed or skipped.

```bash
.venv/bin/ruff check src/ai_dev_loop/scheduler/application/usage_backfill.py src/ai_dev_loop/scheduler/application/token_usage_query.py tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py
.venv/bin/ruff format --check src/ai_dev_loop/scheduler/application/usage_backfill.py src/ai_dev_loop/scheduler/application/token_usage_query.py tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-mkdocs-phase30-2
git diff --check
```

Results: Ruff check and format check **exit 0**. Mypy **exit 0**, 228 source
files. Strict MkDocs **exit 0**, built in 0.62 seconds. The Material theme
printed its existing MkDocs 2.0 warning. Strict mode did not fail.
`git diff --check` **exit 0**.

Second correction, after sharing observation validation with readiness and
importing ledgers written by commit `1a97becaff1af8c54f892f42740f040d4cd7478c`:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_2_usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py --tb=line
```

Result: **exit 0, 11 passed in 17.03s**. None failed or skipped. This is the
affected backfill set. The earlier 72-test command was not repeated.

```bash
.venv/bin/ruff check src/ai_dev_loop/scheduler/application/usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/support/phase30_2_baseline_writer_export.py
.venv/bin/ruff format --check src/ai_dev_loop/scheduler/application/usage_backfill.py tests/integration/test_phase30_2_usage_backfill.py tests/support/phase30_2_baseline_writer_export.py
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-mkdocs-phase30-2
git diff --check
```

Results: Ruff check and format check **exit 0**. Mypy **exit 0**, 228 source
files. Strict MkDocs **exit 0**, built in 0.61 seconds. The Material theme
printed its existing MkDocs 2.0 warning. Strict mode did not fail.
`git diff --check` **exit 0**.

## Correction F-01 through F-06

F-01 fixed. Production path:
`usage_backfill._classify` / `_fold_reconcile_publications`. A fake SDK
deadline with no streamed usage and terminal reconciliation usage of 110 is
imported onto the logical `cursor.run_turn`, with `import_origin`
`reconciliation` and the reconcile attempt as publisher. Clearing facts and
applying backfill restores 110. Resume and a second apply keep one cursor
observation. After the artifact tree is removed, both the schema-22 import and
the later schema-20 CLI import still return 110, and source openers are
forbidden. Test:
`test_f01_reconcile_publication_imports_onto_the_logical_attempt`.

F-02 fixed. The first correction validated observation bindings only inside
`TokenUsageReadService._collect_from_facts`. Readiness is a separate reader:
`usage_backfill._readiness` parsed `fact_payload` and its digest in
`_validated_fact`, then counted coverage from that payload. It never opened
`scheduler_usage_observations`. A deleted required child therefore left
`coverage=complete`, so CLI `usage-backfill --check` reported
`statistically_ready=true`, `provider_coverage=complete`, `remaining=0`, and
no diagnostics while `scheduler usage` raised `CORRUPTION`.

The shared validator is `token_usage_query._validated_observations`.
`_readiness` calls it for every non-pending, applicable fact before counting
that fact as materialized. The call stays inside the existing read
transaction. Check does not repair rows and does not fall back to artifacts.
A Codex implementer turn with native 100/0/10 and 40/0/10 is 160, plus review
25/5/5, direct total 190. An unreferenced observation row with total 99999
does not change 190. Deleting one required child, mismatching its publisher,
and replacing its digest each make CLI `usage-backfill --check` exit non-zero
with `usage statistics payload is invalid` and leave the database bytes
unchanged. The same deletion and publisher mismatch still fail `scheduler
usage`, Integration inspect, Dashboard `token_usage`, and the in-process
`DashboardHttpDispatcher`. After the artifact tree is moved aside and source
openers are forbidden, check stays ready with complete coverage and
`remaining=0`. The socket-dependent HTTP deadline test was not changed.
Test: `test_f02_f04_f05_f06_bindings_debt_receipt_and_historical_import`.

F-03 fixed. Production path: `usage_backfill._readiness`. Empty ledger stays
`not_applicable` and ready. The accepted schema-11 fixture
`tests/fixtures/phase21_4_pre_21_4_historical/engine.sqlite3`, sha256
`43da316de14f2e9cd8fb9cdcbf27c78a36c1e7e20bb8913040377c160edec8cb`, has two
unmaterialized terminal inference attempts. CLI `--check` reports
`provider_coverage=unknown`, `unmaterialized=2`, `remaining=2`, and does not
migrate. A one-attempt import of a two-attempt Codex run is `partial` and not
ready, with materialization debt still reported as `unmaterialized`. Tests:
`test_f03_schema11_unmaterialized_history_is_unknown_and_check_does_not_migrate`
and the partial-import assertion in
`test_b01_b02_backfill_preserves_counters_across_resume_conflict_and_source_loss`.

F-04 fixed. Production path: `usage_backfill._readiness`. A committed
implementer fact with `settlement_status=debt`, null coverage, and only the
110 observation id stays distinct from settled unknown. Readiness reports
`unknown=0`, `provider_coverage=complete` for the settled review, `remaining`
above zero, and `statistically_ready=false`. The public total is 140, so the
unreferenced 50-token row is excluded and the survivor remains. Settled
unknown on the source-loss copy stays ready with `remaining=0`. The debt
payload is the production fact model and canonical digest of an ingested run,
with settlement rewritten to the accepted debt shape.

F-05 fixed. Production path: `render_backfill_report`. Default text includes
`job_id` and `job_status`. The Codex and Cursor tests invoke the CLI text
apply with `--batch-size 1 --max-attempts 1`, parse that id, and resume it.
The resume completes the same job and does not insert another job row.

F-06 fixed. Dropping `scheduler_usage_*` and setting `PRAGMA user_version = 20`
on a ledger written by the current attempt writer only proves that those
current artifacts can be reimported after a version rewind. It does not prove
that the accepted schema-20 writer produced the attempts, envelopes, or usage
observations. That rewind remains in
`test_f02_f04_f05_f06_bindings_debt_receipt_and_historical_import` as the
current-writer text apply/resume path. It is not the baseline evidence.

The baseline evidence is
`test_f06_baseline_writer_fixtures_survive_current_import`. It exports
`1a97becaff1af8c54f892f42740f040d4cd7478c` with read-only `git archive` into
temporary storage, runs the fake-provider generator under
`PYTHONPATH=<archive>/src`, and does not switch the checkout or install that
tree into the live driver. The exported ledgers are schema 20 and have no
usage-fact tables. Provenance records that writer commit. Current CLI
`--check` reports `provider_coverage=unknown`, `unmaterialized=2`,
`remaining=2`, and does not migrate. Text `--apply` with `--batch-size 1
--max-attempts 1`, then `--resume` of that exact job, imports the independent
counters: Codex implementer 160 plus reviewer 30 is direct 190, with
implementer `cache_read` known total 0; Cursor implementer 7519 plus reviewer
30 is direct 7549, with `cache_read` 2560 and `cache_write` 0. Public
`scheduler_usage` and Integration inspect return those totals. Authority-table
and artifact-tree digests are unchanged, and the export bytes stay unchanged.
Deleting the implementer's bound source, and separately replacing one
`result.json` with `{}`, does not import 190 or 7549. After the Cursor
artifact tree is removed, the public total remains 7549 without opening
sources.

## Unfinished mandatory items

B-01 through B-04 are implemented and covered by the tests above. Correction
findings F-01 through F-06 are fixed in the production paths named in the
correction section. No mandatory phase-30.2 ID is left unfinished. F-06 is
demonstrated by the schema-20 export of
`1a97becaff1af8c54f892f42740f040d4cd7478c`, not by rewinding a current-writer
ledger.

## Unexecuted checks

The repository-wide suite, its wrappers, and any split of that suite into
batches were not run. That includes final acceptance. The separate pipeline
gate remains **pending**.

Also not run, because this checkpoint does not authorize them: real provider
or model calls, production backfill, live ledger or artifact migration,
runtime install of the Beelink scheduler driver, NucBox work, timer or linger
changes, backup-service edits, cleanup, and external or destructive Git
actions. No Node/npm or frontend build was run; no Dashboard DTO or frontend
source changed.

## Remaining operator actions

Phase 30.3 owns restore acceptance. This checkpoint does not import production
history and does not change the installed Beelink driver. An operator who
wants historical rows on a schema 21 or 22 ledger runs
`scheduler usage-backfill` explicitly. Check does not migrate. Apply migrates
and imports only the requested batch. Numeric facts verified at import time
are not continuing source-file authentication and do not authorize recovery,
review, or Git.
