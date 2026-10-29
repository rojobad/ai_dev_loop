# Phase 30.1 usage ingestion findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rodrigo/project/ai_dev_loop` on Beelink-SER5,
branch `codex/phase-30-sqlite-statistics`, HEAD
`8e1edb6d1dca0f9e8d2d888523aa6f5d7855535e`
(`docs: plan SQLite token statistics and historical backfill`).
No commit, push, pull request, or merge was made. The scheduler owns any
authorized intermediate checkpoint. NucBox was not used.

OpenQuestions: none. No public reader, Dashboard query, Integration API, price,
billing, or production-history import was switched. API minor stays 13.

## Admission and prerequisite evidence

Checked once before dependent work:

```bash
git merge-base --is-ancestor 1a97becaff1af8c54f892f42740f040d4cd7478c HEAD
```

Result: **exit 0**. Baseline `1a97becaff1af8c54f892f42740f040d4cd7478c` is an
ancestor of HEAD. The pre-change ledger schema was version 20
(`0020_cursor_sdk_no_progress.sql`). Phase 29 token analytics, Phase 25.7
retention, and Phase 25.8 no-progress recovery are present in this tree.
`archive/implementation-history/plans/phase-30-1-sqlite-usage-ingestion.md`
lists OpenQuestions: None. Dependent work was not stopped.

No installed scheduler driver, live `engine.sqlite3`, production artifact,
timer, backup service, cleanup, or real Cursor/Codex model was touched.
Tests used temporary HOME/XDG roots, temporary repositories, and fake
providers/backends.

## Persisted contract

Migration `0021_usage_statistics.sql` advances `SCHEMA_VERSION` to 21. It
creates tables and indexes only. It does not walk artifacts or infer
consumption. Existing snapshot and event hashes are unchanged. Readonly open
of a version-20 database does not migrate.

Tables:

- `scheduler_usage_attempt_facts`
- `scheduler_usage_observations`

Indexes:

- `idx_scheduler_usage_facts_provider_role`
- `idx_scheduler_usage_facts_model`
- `idx_scheduler_usage_facts_run`
- `idx_scheduler_usage_facts_ack`
- `idx_scheduler_usage_observations_attempt`
- `idx_scheduler_usage_observations_provider_role`

Typed models and JSON schemas:

- `UsageFactPayloadV1` / `schemas/scheduler-usage-fact-v1.json`
- `UsageObservationCountersV1` / `schemas/scheduler-usage-observation-v1.json`

Counters are strict non-negative integers, including zero, with optional nulls.
The maximum persisted counter is the signed 64-bit limit `2**63 - 1`. Bool,
string, float, negative, and overflow values are rejected. Native and
normalized payloads are canonical JSON text, not SQL integers. Rows store
provider, role, stage, configured model, coverage, settlement, diagnostic
codes, relative source paths, and SHA-256 digests. They do not store prompts,
patches, free-text errors, session IDs, absolute private paths, or secrets.

Settlement (`settled`, `pending`, `debt`, `conflict`) is separate from
coverage (`complete`, `partial`, `unknown`, `not_applicable`). Pending and
debt rows keep coverage null. A same-key disagreement keeps the previous
counters and records `usage_payload_conflict`. Only monotonic enrichment
(partial to complete, or a null optional counter filled without disagreement)
updates a fact through the normal mutable columns. Those columns do not
include publisher, fence, binding, or import origin. A transient
`sqlite3.Error` while updating statistics is not missing-source evidence: it
reopens one pending `usage_statistics_write_failed` obligation for the logical
inference attempt, records the failed publisher (including
`cursor.reconcile_sdk_run`), keeps known observation rows, and is not
classified as `usage_source_debt`. The next fresh store reconciles that
obligation once and does not send another prompt. A partial Codex observation
load recovers missing published contributions from the authenticated events
JSONL and settles that materialization. Provider coverage stays partial, with
the missing-turn diagnostic, when a completed turn omitted usage.
`usage_source_debt` remains for a published contribution the events source
cannot supply; known counters from the contributions that did load stay.

A usage row is numeric bookkeeping. It does not re-authenticate source
content and it does not authorize recovery, review, Git, or another prompt.

## Contract evidence

| ID | Production entry | Independent test evidence | Result |
| --- | --- | --- | --- |
| S-01 | `SqliteSchedulerStore` migration `_migrate_v20_to_v21`, `schema_supports_usage_statistics`, and `merge_usage_snapshot` in `usage_ingestion.py`. | `tests/unit/scheduler/test_phase30_1_usage_store.py`: a fresh store is version 21 and creates no artifact directory; a fault on `scheduler_usage_attempt_facts` rolls back to version 20 and a later open reaches 21; readonly version 20 does not migrate; null reasoning is accepted; bool, string, float, negative, and overflow counters are rejected; replay stays one row; enrichment fills reasoning and keeps provenance; conflict keeps `output_total` 10 and records `usage_payload_conflict`; an injected `insert_fact` fault rolls back only the new fact. Expected counters are literals, not a call to the normalizer under test. | Included in the correction command: **93 passed**. |
| S-02 | `mark_attempt_ingested` calls `settle_ingested_attempt_usage` after authenticated terminal attempts. `bind_usage_artifacts` is set by `TickService`, `CursorWorkflowService`, `CodexWorkflowService`, `AttemptService`, `SequenceHandoffService`, `CursorTimeoutRetryService`, `ImplementerTimeoutRetryService`, `CursorSdkReconciliationService`, `SchedulerAbortService`, and `consume_no_progress_reobserve`. Non-inference effects, including `cursor.create_chat`, write no fact. `cursor_sdk_preflight_failed` without a native run is `not_applicable`. | `tests/integration/test_phase30_1_usage_ingestion.py` drives production ticks. Codex initial native 100/60/10/4 normalizes to total 110; review 25/5/5 normalizes to total 30; correction 8/0/2/1 normalizes to total 10; second review 12/2/3 normalizes to total 15. Cursor SDK native 4820/139/2560/0/7519/118 normalizes to input total 7380 and total 7519. Models are `gpt-5.5`, `gpt-5.6-sol`, `grok-4.7`, and `gpt-6.1-sol`. Two Codex `turn.completed` events stay one implementer fact. A Cursor correction is a second `cursor.run_turn` fact, stage `correction`, with the same literal 7519 counters and send counter `2`. Quota and generic implementer failures stay coverage `unknown` with zero observations. An active bootstrap cancelled by abort stores `terminal_status=cancelled` and zero observations. A timeout then success keeps the ancestor fact `unknown` and puts total 110 only on the successor. Malformed review usage (`input_tokens: true`) leaves the run completed, reviewer coverage `unknown`, and no reviewer observation. A timed-out implementer fact is `unknown` with no observation. Abort leaves that fact and the attempt count unchanged and the run aborted. Preflight writes one `not_applicable` fact, zero observations, and does not increment the send counter. | Included in **93 passed**. |
| S-03 | `usage_ingestion_barrier` `before_commit` raises `UsageIngestionBarrier` after the usage writes and before the enclosing transaction commits, so those writes roll back with the ingest. `before_ack` commits the rows with `acknowledgement=pending` and skips the post-commit ack. `TickService.run_once` calls `reconcile_unacknowledged_usage`, which retries pending facts, marks a still-pending terminal source as `usage_source_debt` unless the diagnostic is `usage_statistics_write_failed`, and acknowledges classified rows. It schedules no effect and sends no prompt. SDK reconciliation uses `source_attempt_id` as the logical attempt. A failed statistics update reopens that logical fact and records the publisher attempt. A partial Codex binding load reads the authenticated events JSONL before classifying coverage. | A live tick raises `before_commit` after the implementer process has already run. Recreating the store and tick finishes the run with one implementer observation, send counter `1`, one `codex.implementer_bootstrap` attempt, and `reviews_completed` 1. The before-ack fact is acknowledged by a new store and tick without a second implementer send or a second observation id. A direct `mark_attempt_ingested` replay remains only as a rollback check. Deadline replay keeps one cursor observation on `cursor.run_turn`. Injecting `update_fact` during that reconciliation leaves `usage_statistics_write_failed` pending, names the reconcile publisher, retains native total 110, and a fresh tick settles that same observation once without another send. Deleting one of two Codex observation artifacts while keeping the events file recovers totals 10 and 110 on one fact with complete coverage. The same deletion with three completed turns, totals 110 and 10 plus a third turn that omits usage, stores those two contributions once, settles the fact, keeps coverage `partial` and `missing_codex_usage_turn_3`, and does not record `usage_source_debt`. The reviewer fact stays settled and complete at total 30, and `reviews_completed` stays 1. When historical recovery returns nothing, the surviving counter remains and the fact is `debt` with null coverage. Deleting the run artifacts, clearing observation rows, and reconciling a pending fact yields `debt`, null coverage, zero observations, the same attempt count, and `completed`. No-progress finished replay keeps send counter `1` and one cursor observation at 7519. | Earlier evidence is in the 93-passed command. The omitted-usage recovery is in the F-02 follow-up: **3 passed**. |
| S-04 | Ingestion updates only the new usage rows and stats acknowledgement. `TokenUsageReadService` still authenticates artifact files. A missing artifact root returns an unknown public total while the SQLite observations remain. | After a completed Codex run, another tick plus `reconcile_unacknowledged_usage` leaves `state_kind`, `state_payload_sha256`, run version, event count, reservation count, `git status --porcelain`, and `reviews_completed` unchanged. Facts retain `gpt-5.5` / `gpt-5.6-sol` and `complete` coverage. Usage tables do not contain the prompt text, `cursor_fix_prompt`, `/home/`, or `usage_limit_exceeded`. The public reader still reports 140 from artifacts and `None` when the artifact root is missing. The correction crash, acknowledgement restart, and reconciliation write-failure tests also leave implementer send count, review count, and attempt identity unchanged. | Included in **93 passed**. |

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_1_usage_store.py tests/integration/test_phase30_1_usage_ingestion.py tests/unit/scheduler/test_schema.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/scheduler/test_phase25_8_sdk_no_progress.py
```

Result of the pre-correction run: **exit 0, 84 passed in 110.36s**. No tests were skipped.

The correction run of the same command, after the F-01/F-02/F-03 fixes:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_1_usage_store.py tests/integration/test_phase30_1_usage_ingestion.py tests/unit/scheduler/test_schema.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/scheduler/test_phase25_8_sdk_no_progress.py
```

Result: **exit 0, 93 passed in 125.78s**. No tests were skipped. No test in that command failed.

An earlier invocation of that command plus three historical downgrade nodes
exited 1: **87 passed, 3 failed**. The failures were
`test_schema_migration_reopens_v12_without_rewriting_lineage`,
`test_schema18_migration_adds_sdk_reconciliation_ledger`, and
`test_schema16_read_fallback_and_v17_migration_preserve_authority`.
Those helpers rewound `user_version` without dropping the new usage tables, so
the forward migration saw `table scheduler_usage_attempt_facts already exists`.
The downgrade helpers now drop both usage tables. The same fix was applied to
the version-14, version-15, version-18 inspection, and presentation-migration
fault helpers.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase23_4_sequence_recovery.py::test_schema_migration_reopens_v12_without_rewriting_lineage tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_adds_sdk_reconciliation_ledger tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_fault_rolls_back_table_and_audit_row tests/integration/test_phase28_short_names.py::test_schema16_read_fallback_and_v17_migration_preserve_authority tests/integration/test_phase28_short_names.py::test_schema17_migration_fault_rolls_back_all_presentation_tables tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py::test_v14_migration_adds_relation_without_rewriting_existing_runs tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py::test_v15_migration_adds_sequence_replacement_without_rewriting_runs tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py::test_cli_inspection_does_not_create_or_migrate_a_ledger --tb=line
```

Result: **exit 0, 8 passed in 3.03s**.

```bash
.venv/bin/ruff check src/ai_dev_loop/scheduler/application/usage_ingestion.py src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py tests/integration/test_phase30_1_usage_ingestion.py tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py
.venv/bin/ruff format --check src/ai_dev_loop/scheduler/application/usage_ingestion.py src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py tests/integration/test_phase30_1_usage_ingestion.py tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py
```

Result: **exit 0**. All checks passed. `ruff format --check` reported 4 files
already formatted. The pre-correction format check had reported 10 files
already formatted for the earlier edit set.

```bash
.venv/bin/python -m mypy src/ai_dev_loop
```

Result: **exit 0**, no issues found in 227 source files.

```bash
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase30-1-correction-site
```

Result: **exit 0**, documentation built in 0.61 seconds. The Material theme
printed its existing MkDocs 2.0 warning. Strict mode did not fail. The
pre-correction strict build used `/tmp/ai-dev-loop-phase30-1-site` and
finished in 0.65 seconds, also exit 0.

```bash
git diff --check
```

Result: **exit 0**.

## Unfinished mandatory items

S-01 through S-04 are implemented and covered by the tests above. F-01 and
F-03 stay as recorded by the 93-passed command. F-02 needed a second
correction: closing the published-artifact gap required `historical_complete`,
so a recovered pair of contributions became `usage_source_debt` when another
completed turn omitted usage. Materialization recovery is now independent of
provider completeness. No mandatory phase-30.1 ID is left unfinished.

F-02 follow-up, production path `settle_ingested_attempt_usage` in
`usage_ingestion.py`:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase30_1_usage_ingestion.py::test_f02_recovered_bindings_stay_partial_when_a_turn_omits_usage tests/integration/test_phase30_1_usage_ingestion.py::test_f02_partial_codex_observation_recovers_from_authenticated_events tests/integration/test_phase30_1_usage_ingestion.py::test_f02_partial_source_loss_keeps_surviving_counters_as_debt
```

Result: **exit 0, 3 passed in 6.26s**. No test in that command failed or skipped.

```bash
.venv/bin/ruff check src/ai_dev_loop/scheduler/application/usage_ingestion.py tests/integration/test_phase30_1_usage_ingestion.py
.venv/bin/ruff format --check src/ai_dev_loop/scheduler/application/usage_ingestion.py tests/integration/test_phase30_1_usage_ingestion.py
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase30-1-f02-site
git diff --check
```

Result: **exit 0**. Ruff reported both files already formatted. Strict MkDocs
built in 0.61 seconds with the existing Material theme warning. `git diff
--check` was clean. The 93-passed command and the repository-wide suite were
not rerun for this follow-up.

## Unexecuted checks

The repository-wide suite, its wrappers, and any split of that suite into
batches were not run. That includes final acceptance. The separate pipeline
gate remains **pending**.

Also not run, because this checkpoint does not authorize them: real provider
or model calls, production backfill, live ledger or artifact migration,
runtime install of the Beelink scheduler driver, NucBox work, timer or linger
changes, backup-service edits, cleanup, and external or destructive Git
actions. No Node/npm or frontend build was run; no Dashboard DTO or frontend
source changed. Public token readers remain artifact-backed until Phase 30.2.

The long Phase 27.5 and Phase 27.6 scenario tests that only assert
`SCHEMA_VERSION` at the end of a recovery flow were not rerun. Their current
schema constant is 21, and the dedicated migration nodes above passed.

## Remaining operator actions

Phase 30.2 owns the historical query switch and backfill. Phase 30.3 owns
restore acceptance. This checkpoint does not migrate production history and
does not change the installed Beelink driver. Numeric SQLite integrity remains
separate from source-content authentication and from operational recovery
authority.
