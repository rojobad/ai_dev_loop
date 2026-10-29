# Phase 30.3 database-only restore acceptance findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rodrigo/project/ai_dev_loop` on Beelink-SER5,
branch `codex/phase-30-sqlite-statistics`, HEAD
`a9c519b09377a29c3e7fc7d20651a56f5f712d3f`.
The Phase 30.3 changes are uncommitted. No commit, push, pull request, or
merge was made. The scheduler owns any authorized intermediate checkpoint.
NucBox was not used. The installed Beelink scheduler driver was not replaced.

OpenQuestions: none. API minor stays 1.13. Schema stays 22.
`USAGE_STATISTICS_SCHEMA_VERSION` stays 21.
`USAGE_BACKFILL_SCHEMA_VERSION` stays 22. Numeric SQLite integrity is separate
from source-content authentication and from operational recovery authority. A
usage row still does not authorize recovery, review, Git, or another prompt.

## Admission and prerequisite evidence

Checked before dependent work:

```bash
git merge-base --is-ancestor 1a97becaff1af8c54f892f42740f040d4cd7478c HEAD
```

Result: **exit 0**. Baseline `1a97becaff1af8c54f892f42740f040d4cd7478c` is an
ancestor of HEAD `a9c519b09377a29c3e7fc7d20651a56f5f712d3f`.

Accepted predecessor contracts, rechecked in code and findings before this
work:

| ID | Where it remains |
| --- | --- |
| S-01–S-04 | `archive/implementation-history/findings/phase-30-1-usage-ingestion.md`. Ingestion still settles facts in SQLite. The public-read sentence in S-04 that returned an unknown total when the artifact root was missing was superseded by B-03: schema 21+ public statistics read facts and do not fall back to artifacts. |
| B-01–B-04 | `archive/implementation-history/findings/phase-30-2-usage-backfill.md`. `scheduler usage-backfill`, SQLite-only usage queries, and Dashboard `token-usage` remain the production callers. |

`archive/implementation-history/plans/phase-30-sqlite-token-statistics.md` and
`phase-30-3-database-only-restore-acceptance.md` list OpenQuestions: None.
Dependent work was not stopped. The plan's historical "SQLite v20 / API 1.13"
note is stale: after 30.1 and 30.2 the ledger is schema 22 and the API minor
is 1.13. This phase did not add a schema or API version.

No live `engine.sqlite3`, production artifact, timer, restic job, retention
policy, cleanup, or real Cursor/Codex model was touched. Tests used temporary
HOME/XDG roots, temporary repositories, and fake providers/backends.

## A-01 — One-file restore of every numeric token view

Production callers exercised on a temporary ledger, then again from only the
backup file in a second process whose artifact root does not exist and whose
`PATH` contains only trap `codex`, `agent`, and `cursor` commands:

- `sqlite3.Connection.backup` after `PRAGMA wal_checkpoint(TRUNCATE)`
- `scheduler usage`, `scheduler list`, `scheduler status`, `scheduler timeline`
- `scheduler sequence usage`, `scheduler sequence status`
- `integration run inspect`, `integration sequence inspect`
- `DashboardHttpDispatcher` operations `dashboard.projects`,
  `dashboard.summary`, `dashboard.runs`, `dashboard.token-usage`, and
  `dashboard.activity`
- `scheduler usage-backfill --check` and `--apply`

The fixture materializes through normal ingestion, then through the real
backfill CLI. Projects are `fixture-alpha` and `fixture-beta`. It includes a
named v3 sequence, a correction run, a known-zero run, a cancelled active
implementer, a Cursor SDK run, a skipped preflight, a standalone Codex
implementer force-retry (`ImplementerRetryService.retry(..., force=True)`),
and a second v3 sequence whose blocked leaf is adopted by that same force
path at generation 2 before the following phase. Models are the literals
`gpt-5.5`, `gpt-5.6-sol`, `grok-4.7`, and `gpt-6.1-sol`.

Independent totals, not recomputed by the normalizer:

| Surface | Expected |
| --- | --- |
| Codex implementer initial | input 100, cache read 60, output 10, reasoning 4, total 110. Total is input + output. Cache and reasoning are not added again. |
| Codex correction | total 10 |
| First review | total 30 |
| Second review | total 15 |
| Correction run direct and recovery | 165, two completed reviews |
| Original sequence of two accepted phases | 280, exactly the two phase runs |
| Standalone recovery source direct | 140 |
| Standalone recovery successor direct | 34 |
| Standalone successor recovery lineage | 174, source and successor once each |
| Adopted phase source direct | 64 |
| Adopted successor direct | 28 |
| Following phase direct | 20 |
| Replacement sequence | 112, exactly the source, adopted successor, and following phase |
| Known-zero run, before source deletion | known 0, coverage `complete` |
| Recovery source and replacement source | coverage `partial`: the failed correction stays unknown and the earlier known turns stay in the total |
| Cohort Dashboard source | coverage `partial`, known total 8280 |
| Cursor SDK implementer | input total 7380, cache read 2560, cache write 0, output 139, reasoning 118, total 7519 |
| SDK run with one review | 7549 |
| Skipped preflight | coverage `not_applicable`, known total null |
| Alpha project cohort | 731 |
| Beta project cohort | 7549 |
| Both projects | 8280 |
| Window `2020-01-01Z`–`2020-01-02Z` | known total null and no rows |
| Dashboard submissions / review rounds | 12 / 11 in both `UTC` and `America/Santiago` |
| Observed process hours | 0 in both zones, because the fixture clocks do not advance between launch and completion |

Disjoint Dashboard flows for the correction run are input other 78, cache
read 67, output other 7, reasoning 5, and output unsplit 8. Those leaves add
to 165. Cache and reasoning are not added again on top of that sum. The SDK
direct view is input other 4840, cache read 2565, cache write 0, output other
21, reasoning 118, and output unsplit 5, which adds to 7549. The Cursor
implementer leaf is input other 4820, cache read 2560, cache write 0, output
other 21, and reasoning 118.

A live WAL commit renames the correction short name from `Alpha correction` to
`Alpha WAL rename` without checkpointing. A copy of only the main file still
reads `Alpha correction`. `Connection.backup` reads `Alpha WAL rename`. The
restored one-file copy matches the live snapshot, including that short name.

On the restored process, statistic reads patch
`authenticate_process_attempt` and
`ReadOnlyProtectedArtifactStore.read_verified_bytes` to fail if called.
`opener_calls` is 0. The provider witness file stays empty. The database
SHA-256 and `user_version` 22 are unchanged by the reads.

## A-02 — Non-token statistics, missing text, and difficult rows

The same snapshot compares project run counts (`fixture-alpha` 10,
`fixture-beta` 2), short names, run states (`completed`, `aborted`,
`blocked`), sequence state `awaiting_finalization`, review-round counts,
process-hour totals, UTC and `America/Santiago` activity, timelines, and the
cohort window. Query-time `observedAt` is excluded. File-availability fields
that are allowed to differ, and are asserted on the restored copy, are:

- phase checkpoint availability and commit SHA prefix (absent without the
  checkpoint file)
- completion-report SHA prefix (null)
- SDK wait stage, elapsed seconds, and initial-response flag (`unknown`)
- review prompt bytes

`aggregate_counts.checkpointed` is not one of those file fields. It counts
distinct runs that have a validated `sequence_checkpoint_committed` event.
The original sequence stays 1 on both the live ledger and the one-file
restore. Planned and materialized stay 2. The checkpoint file itself stays
unavailable on the restore.

`integration run review-content --kind prompt` on the restored copy returns
`available: false`, `reason: not_recorded`, and empty `contentBase64`. It does
not call a provider. On that same restored process,
`integration run initial-prompt` exits 5 with `ok: false`,
`error.code: DATA_INTEGRITY`, and `data: null`. It does not reconstruct the
frozen prompt. After the artifacts are present again, replacing the correction
run's review invocation file with `{` makes `review-content` exit 5 with
`DATA_INTEGRITY` and empty data. `subprocess.run` is refused during that read,
and `scheduler usage` still returns 165.

`scheduler sequence status` now uses `ReadOnlyProtectedArtifactStore`. A
missing run or sequence root leaves the checkpoint and completion-report
fields empty and does not create `artifacts/`.

After the settled restore, the test deletes the zero-run artifact tree and its
usage rows, then `usage-backfill --run-id --apply` imports
`usage_source_missing` with a null configured model and unknown coverage. It
deletes only the cancelled run's facts, so that terminal attempt stays visible
as `backfill_required`. It retargets one correction observation and applies
backfill again: `conflicting >= 1` and the correction total stays 165.
`--all --check` is not statistically ready. The second one-file restore still
shows those rows and the same known totals (731 / 7549 / 8280). The first
restore is statistically ready with `unmaterialized == 0`.

Manual Cursor SDK cleanup of the completed SDK conversation deletes that
conversation directory and leaves the run's usage-fact count and attempt count
unchanged. Phase 25.8's exact message
`Agent turn stopped after repeated resume attempts made no progress` is
unchanged in `src/ai_dev_loop/scheduler/domain/cursor_sdk_no_progress.py`.

## A-03 — Installed base package

`python -m build --outdir <temp>` writes the wheel and sdist with hatchling.
Both METADATA records `Requires-Python: >=3.11` and list `cursor-sdk` only as
an extra. `python3.11` is not installed on this machine (`command -v python3.11`
prints nothing). The test does not download it. It creates a fresh venv with
the available CPython 3.12.3 and `uv pip install` of the base wheel, without
the `cursor-sdk` extra.

That venv's interpreter, with no checkout on `PYTHONPATH`, no provider
commands, and no API key, reads the restored SQLite file. During that read
the test-owned target repository, artifact root, and Cursor SDK store path
are renamed away, and `CODEX_HOME` / `OPENAI_API_KEY` are absent from the
child environment. The implementation checkout stays in place and is not the
import origin:

- import origin is under the venv and not under `src/`
- `import cursor_sdk` raises `ImportError`
- schema user version is 22
- `scheduler usage` known total is the literal 140 and coverage is `complete`
- Dashboard `token-usage` `knownTotalTokens` is 140
- configured models are `gpt-5.5` and `gpt-5.6-sol`
- the statistic openers are not called
- the database digest is unchanged
- review-content for the missing prompt is `available: false`,
  `reason: not_recorded`, and empty content

No Dashboard frontend, DTO, or DUX scope changed. DUX-06 automatic windows and
DUX-09 on-demand text reads stay as documented. Node/npm and a browser pass
were not run because there is no frontend change. Existing Node/npm is absent.

## A-04 — Runbook

Maintained procedure, not executed on the live ledger:

`docs/operacion/respaldo-estadisticas-sqlite.md`

Linked from `mkdocs.yml`, `docs/operacion/estado-artefactos.md`,
`docs/operacion/observabilidad.md`, `docs/operacion/seguridad-privacidad.md`,
`docs/referencia/cli.md`, `docs/referencia/dashboard.md`, and
`docs/referencia/integration-api.md`.

The six operator steps are: install the accepted package in an authorized
window; take a consistent initial backup that still keeps artifacts; run
`usage-backfill --check`, then bounded `--apply` / `--resume`; compare
project, run, sequence, and Dashboard totals plus readiness; back up only the
SQLite file with the backup API and verify a restore that has no external
roots; record unknowns and conflicts and keep the original evidence until they
are resolved. The text states that model/role/stage metrics are
database-authoritative after import, while prompts, patches, reviews, and
session recovery are not in a database-only copy. It does not promise a fixed
size or compression ratio, and it does not change restic, prune, or cleanup.

## Contract map

| ID | Evidence |
| --- | --- |
| S-01–S-04 | Predecessor findings plus the passing Phase 30.2 and Phase 29 regression files. Public reads follow B-03, not the pre-backfill artifact fallback. |
| B-01–B-04 | Predecessor findings. This phase calls the same backfill, usage, inspect, and Dashboard readers. |
| A-01 | `tests/integration/test_phase30_3_database_only_restore.py` and `tests/support/phase30_3_restore_snapshot.py`. Partial coverage, disjoint flow leaves, and the one-file totals are independent literals. |
| A-02 | Same test: counts, names, states, rounds, process hours, zones, timelines, cohort window, `not_recorded` review text, `DATA_INTEGRITY` for the missing prompt and for a present invalid review file, unknown/pending/conflict rows, unchanged digest. |
| A-03 | `tests/integration/test_phase30_3_installed_statistics.py`. Phase 25.8 file passed without edits. SDK cleanup inside the restore test keeps facts and attempts. |
| A-04 | `docs/operacion/respaldo-estadisticas-sqlite.md` and the links above. |

## Correction F-01, F-02, and F-03

F-01: **fixed**. `SequenceStatusService._validated_checkpointed_count` counts
distinct runs whose `sequence_checkpoint_committed` payload matches
`event_payload_sha256` and `SequenceCheckpointCommittedEvent`, and whose
`run_id` matches the event row. Digest or binding failures raise
`SchedulerEngineError` validation and are not counted. Checkpoint file
prefixes and completion-report prefixes stay file-based, and status still
uses `ReadOnlyProtectedArtifactStore` without creating an artifact root.
`integration_api.sequence_projection` now publishes
`status_result.aggregate_counts.checkpointed` instead of counting
`phase.checkpoint.available`. The restore proof no longer drops `checkpointed`
from snapshot equality and expects 1 for the original sequence on the
restored process.

F-02: **fixed**. The same-run timeout fixture is gone. The restore proof now
drives `ImplementerRetryService.retry(run_id, force=True)` with the fake Codex
provider. The standalone successor's direct total is 34 and its recovery
lineage is 174, including the 140 source once. The replacement sequence's
public `scheduler sequence usage` and `integration sequence inspect` totals
are 112, with run ids exactly the blocked source, the generation-2
`implementer_retry` successor, and the following phase. Unrelated runs,
including the standalone successor, are absent. `scheduler_codex_implementer_recoveries`
has both ready rows, and the original sequence's generations stay 1. No
usage-query production change was required: `_run_ids_with_ancestors` and
`_sequence_run_ids` already implement that membership.

F-03: **fixed**. Restored reads in both proofs rename the test-owned recorded
repository roots, the artifact root, and the Cursor SDK store directory, then
restore them afterward. The child environment drops `CODEX_HOME`,
`OPENAI_API_KEY`, and the other `CODEX_` / `OPENAI_` / `CURSOR_` /
`ANTHROPIC_` / `AZURE_OPENAI_` / `FAKE_` variables after planting sentinels.
The snapshot and installed reader fail if any named path still exists or a
forbidden variable is set. The package under test stays importable. The
implementation checkout and live homes are not renamed. Provider witnesses
stay empty, database digests and schema 22 stay unchanged, and review-content
stays `not_recorded` with empty content.

## Correction F-04

F-04: **fixed**. `SequenceStatusService._validated_checkpointed_count` no longer
reads every `sequence_checkpoint_committed` row. Membership comes from
`_checkpoint_member_run_ids`: when lineage rows exist,
`load_sequence_run_lineage` supplies every attempt run, including an adopted
`implementer_retry` generation; a prepared sequence with no rows contributes
none; a historical sequence without lineage rows uses
`project_historical_lineage_from_state`. A lineage validation failure becomes
`SchedulerEngineError` corruption. The event query is `event_kind =
sequence_checkpoint_committed AND run_id IN (those members)`. Each selected
row still fails closed on payload digest, `SequenceCheckpointCommittedEvent`,
run binding, and sequence binding. The count is the number of distinct bound
run ids. Checkpoint result files and completion-report prefixes stay
file-based, and status still uses `ReadOnlyProtectedArtifactStore`.

Production callers: `SequenceStatusService.get_status` (`scheduler sequence
status`) and `IntegrationSequenceReadService.inspect_sequence` (`integration
sequence inspect`), which calls `get_status` and publishes
`status_result.aggregate_counts.checkpointed`. Integration maps that
validation error to `INVALID_ARGUMENT`.

`tests/unit/scheduler/test_phase20_4_sequence_status.py::test_f04_checkpoint_count_ignores_other_sequences_and_keeps_replacements`
puts the checkpoint on a blocked generation-1 source while the active leaf is
an unresolved generation-2 successor, then inserts a malformed checkpoint
event on a non-member run. Status and inspect both report `checkpointed == 1`,
and the successor itself has no checkpoint event. Rebinding that member event
to another sequence id, and replacing its payload with invalid JSON, each make
status raise validation and inspect raise `INVALID_ARGUMENT`.
`tests/integration/test_phase30_3_database_only_restore.py` now requires the
replacement sequence's restored `checkpointed` to stay 1, the same distinct
count as the original sequence's restored checkpoint. Counting did not move
back to artifact files.

## Validation

The pre-correction focused command (67 passed, 227.76s) did not include these
fixes. The correction command adds
`tests/integration/test_phase21_3_sequence_inspection.py` because integration
inspect used to recompute `checkpointed` from checkpoint files:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/integration/test_phase30_2_database_statistics.py \
  tests/integration/test_phase29_1_token_queries.py \
  tests/unit/dashboard/test_token_usage.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py \
  tests/integration/test_phase21_3_sequence_inspection.py
```

Result: **80 passed**, 0 failed, 0 skipped, exit 0, 266.57s.

An earlier correction run of only
`tests/integration/test_phase30_3_database_only_restore.py` failed in 12.73s
because a one-phase replacement manifest is rejected. The replacement is now
two phases. The same file then passed alone in 37.46s, and the command above
includes it again.

Correction checks that also passed:

```bash
.venv/bin/python -m ruff check \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  src/ai_dev_loop/integration_api/sequence_projection.py \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/support/phase30_3_restore_snapshot.py
.venv/bin/python -m ruff format --check \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  src/ai_dev_loop/integration_api/sequence_projection.py \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/support/phase30_3_restore_snapshot.py
.venv/bin/python -m mypy src/ai_dev_loop
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m mkdocs build --strict --site-dir /tmp/phase303-mkdocs
git diff --check
```

`ruff check` and `ruff format --check` exited 0. `mypy` reported no issues in
228 source files. MkDocs strict build wrote `/tmp/phase303-mkdocs` and exited
0. The theme printed its existing MkDocs 2.0 warning. `git diff --check`
exited 0.

The partial-coverage, disjoint-flow, missing-prompt, and invalid-review-file
assertions were added after that correction. The restore file alone was rerun
first:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/integration/test_phase30_3_database_only_restore.py --tb=short
```

Result: **1 passed**, 0 failed, 0 skipped, exit 0, 37.37s.

The same focused selection, including the checkpoint-count regressions, was
then rerun:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/integration/test_phase30_2_database_statistics.py \
  tests/integration/test_phase29_1_token_queries.py \
  tests/unit/dashboard/test_token_usage.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py \
  tests/integration/test_phase21_3_sequence_inspection.py
```

Result: **80 passed**, 0 failed, 0 skipped, exit 0, 263.27s.

```bash
.venv/bin/python -m ruff check \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/support/phase30_3_restore_snapshot.py \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  src/ai_dev_loop/integration_api/sequence_projection.py \
  src/ai_dev_loop/integration_api/review_evidence_auth.py \
  src/ai_dev_loop/integration_api/review_reader.py \
  src/ai_dev_loop/integration_api/review_service.py
.venv/bin/python -m ruff format --check \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase30_3_installed_statistics.py \
  tests/support/phase30_3_restore_snapshot.py \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  src/ai_dev_loop/integration_api/sequence_projection.py \
  src/ai_dev_loop/integration_api/review_evidence_auth.py \
  src/ai_dev_loop/integration_api/review_reader.py \
  src/ai_dev_loop/integration_api/review_service.py
.venv/bin/python -m mypy src/ai_dev_loop
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m mkdocs build --strict --site-dir /tmp/phase303-mkdocs
git diff --check
command -v python3.11
```

Ruff check passed and reported 8 files already formatted. `mypy` reported no
issues in 228 source files. Strict MkDocs exited 0 and wrote
`/tmp/phase303-mkdocs`; the theme printed the same MkDocs 2.0 warning. `git
diff --check` exited 0. `command -v python3.11` printed nothing. Host for this
rerun: Beelink-SER5. HEAD remained
`a9c519b09377a29c3e7fc7d20651a56f5f712d3f`.

No command in the F-01–F-03 correction was blocked. The installed-package proof's
`python -m build` and `uv pip install` completed inside the passing pytest
command. Tests were not weakened for network or socket limits. Unrelated Ruff
failures outside these files were left unchanged.

Failed during development, then fixed and rerun as part of the passing files:

- sequence state was read as `.kind` on `ActiveSequenceState`
- the second SDK credential directory already existed
- the Dashboard window was longer than 366 days
- checkpoint and SDK-wait file fields differed across the restore
- process hours are 0 under the frozen fixture clocks
- `scheduler sequence status` created `artifacts/` on a read
- the first installed probe imported `cursor_sdk` from the project venv
- the correction's first replacement sequence had one phase; sequence
  manifests require two

F-04 rerun, after restricting checkpoint selection to validated lineage
membership. The first fixture disagreed with
`load_and_validate_sequence_lineage` (`lineage current_run_id disagrees with
materialized entry`) and was rewritten so the sequence state adopts the
successor while the checkpoint stays on the replaced source. The focused
command then passed:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase20_4_sequence_status.py \
  tests/integration/test_phase21_3_sequence_inspection.py \
  tests/integration/test_phase30_3_database_only_restore.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py --tb=line
```

Result: **18 passed**, 0 failed, 0 skipped, exit 0, 69.05s. The new unit test
alone had already passed in 0.96s after that fixture rewrite.

```bash
.venv/bin/python -m ruff check \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  tests/unit/scheduler/test_phase20_4_sequence_status.py \
  tests/integration/test_phase30_3_database_only_restore.py
.venv/bin/python -m ruff format --check \
  src/ai_dev_loop/scheduler/application/sequence_status.py \
  tests/unit/scheduler/test_phase20_4_sequence_status.py \
  tests/integration/test_phase30_3_database_only_restore.py
.venv/bin/python -m mypy src/ai_dev_loop
```

Ruff check passed and reported those 3 files already formatted. `mypy`
reported no issues in 228 source files. No F-04 command was blocked. The
repository-wide suite stayed unrun.

## Unfinished mandatory work

None of A-01–A-04. No OpenQuestion is unresolved.

## Unexecuted checks and later operator actions

The repository-wide suite, its wrappers, and any split of that suite into
batches were not run. That gate stays pending.

Not run, and not authorized by this phase:

- installing the accepted package into the live scheduler environment
- `usage-backfill` against the production ledger
- a backup or restore of the live `engine.sqlite3`
- timer, restic, prune, retention, or cleanup changes
- real Cursor or Codex model calls
- a Node or browser pass; no frontend file changed and npm is not being
  installed for this phase
- a Python 3.11 virtualenv; `command -v python3.11` still prints nothing and that interpreter was not downloaded

Those install, import, and backup steps remain operator actions for an
authorized maintenance window. The runbook documents them. Verifying a
database-only restore does not grant execution, Git, or deletion authority.
