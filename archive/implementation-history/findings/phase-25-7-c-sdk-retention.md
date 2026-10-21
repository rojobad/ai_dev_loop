# Phase 25.7 C — SDK conversation retention

Date: 2026-10-21
Scope: delivery C only (`C-06`, `C-07`)

## Outcome: completed candidate for independent review

Corrections F-01 through F-08 are included. Each is fixed. Details and the
correction commands are in the sections below the original contract.

Deliveries A and B are present in the accepted tree and were not reimplemented.
Fresh SDK submissions still freeze SQLite through the installed public
initializer. Historical v7/v8 contexts and sequence definition/entry v3 stay
JSONL. B's short administrative timeouts and frozen send/stream deadline stay
in place. This leaf does not install a driver, edit `ai_dev_loop.yaml`, or
rewrite frozen sequence inputs. The installed A driver stays fixed.

Q-01 is resolved by the operator decision of 2026-10-20: automatic cleanup
with seven days of retention for definitively finished conversations. That
decision is a versioned ledger policy (`operator_2026-10-20`, retention
exactly 168 hours, automatic cleanup enabled). It is not inferred from
directory age, `mtime`, a SQL `updated_at` column, or a one-time deletion
receipt. A missing policy row fail-closes automatic cleanup. Restoring it
rewrites that same decision; it does not invent a new window.

No new OpenQuestion was found.

## Contract status

### C-06 — explicit preview and apply for one app-owned conversation root: satisfied

- New commands: `scheduler cursor-store cleanup` and `scheduler cursor-store policy`.
  `scheduler cutover cleanup` still deletes only `runs/` and `pr-review-v2/`
  after `delete-legacy-state`.
- Preview and `--dry-run` are read-only, as is `policy` without
  `--automatic`. They open an existing ledger with `open_readonly`. A missing
  ledger or a historical schema does not create directories, migrate, seed the
  policy, or change permissions. They do not delete a root, import
  `cursor_sdk`, launch a bridge, or call a model. Apply requires
  `--confirm delete-cursor-sdk-conversation` plus one `--conversation-key` or
  `--owner-run-id`.
- Eligibility authenticates one owner, backend, and layout. Historical SDK
  runtime v1 with omitted backend/layout authenticates as JSONL layout 1.
  Conflicting owners or backends are `unauthenticated_binding`.
- A conversation is disposable only when every related run is `completed`,
  `completed_with_residual_risk`, or a clean `aborted`, and no related attempt
  is launching, active, or uncertain, and no related reservation, admission
  claim, live effect, checkpoint hold, capacity hold, SDK reconciliation,
  recovery publication, or unadopted sequence replacement remains. Blocked,
  `max_iterations_reached`, and `waiting_codex_capacity` runs stay. Cleanup
  does not abort or rewrite those states. A clean abort whose checkpoint no
  longer carries `cursor.sdk` is grouped from the hash-authenticated
  reconciliation intent, using that intent's `source_state` binding. A
  historical `blocked` ancestor is not continuable when one authenticated
  ready successor edge reaches a definitive member of the same conversation.
- The completion stamp is payload `updated_at` for completed kinds and
  `aborted_at` for `aborted`. A ready SDK reconciliation adds the
  `created_at` of its hash-authenticated `cursor_sdk_reconciled` or
  `cursor_sdk_reconciled_v2` event. `definitively_finished_at` is the latest
  of those stamps. A missing event for a ready reconciliation is
  `missing_completion_evidence`. A damaged payload sets
  `ledger_evidence_unavailable` and blocks disposal of authenticated
  conversations. Unrelated valid Codex work does not.
- Disposal and dispatch share a per-conversation fence. The intent row is
  committed before filesystem removal. The receipt id is allocated with the
  intent and reused on convergence. The tombstone is
  `$XDG_STATE_HOME/ai_dev_loop/cursor-sdk-disposals/<key>.json` (mode `0600`).
  The ledger receipt is the authority.
- Removal accepts only the owned real directory
  `cursor-sdk/<validated-key>`. Symlinks, path escape, and a different uid are
  refused without a receipt. Tombstone publication rejects a symlinked
  disposals directory, temporary file, or destination and creates the
  temporary with `O_EXCL|O_NOFOLLOW`. An already absent eligible root records
  `already_absent` and does not touch siblings. A crash after the intent
  converges to the same receipt id. A stale fence generation does not replace
  a receipt another worker already stored. Each tombstone publication uses
  its own temporary file. A later maintenance pass republishes a committed
  receipt whose tombstone is missing, without deleting again.
- Dispatch checks the intent or receipt before opening native state. A disposed
  conversation raises `conversation_disposed` and the worker records
  `cursor_sdk_preflight_failed` with `timed_out=false`, not an uncertain hold.
- Ledger, artifacts, credentials, repositories, operator backups, Codex state,
  and unrelated conversation roots stay. There is no global CLI scan and no
  native-blob parser.

### C-07 — automatic seven-day retention through scheduler maintenance: satisfied

- Schema 19 seeds `scheduler_cursor_sdk_retention_policy` with automatic
  cleanup enabled, `retention_hours=168`, `decision_source=operator_2026-10-20`,
  and `decision_at=2026-10-20T00:00:00.000000Z`.
- `scheduler cursor-store policy` shows that row. `--automatic enabled|disabled`
  changes only `automatic_cleanup_enabled` and `updated_at`. Manual preview and
  apply stay available while automatic cleanup is disabled. Manual apply does
  not wait 168 hours.
- Automatic eligibility starts at `definitively_finished_at + 168 hours` UTC.
  One microsecond before that instant is not eligible. The exact instant is
  eligible, and deletion happens on the next maintenance pass, not earlier.
  Later related work moves the clock to the latest durable completion. Missing
  completion evidence blocks deletion regardless of file age.
- `scheduler tick` runs that pass through the same eligibility, fence, intent,
  deletion, and receipt path after the tick that acquired the global lease has
  released it. The batch limit is 8. A repeated tick at the same clock reuses
  the receipt and does not fill the batch with already deleted conversations.
  A tick that does not acquire the lease does not maintain. Cutover's early
  return does not maintain.
- No new systemd unit, linger change, Dashboard read, or workstation service
  is part of this path. The existing scheduler tick remains the only caller.

## Historical compatibility

- Schema 19 is additive. Fresh databases migrate through v18 into v19. A
  read-only historical v18 database still verifies the v18 table and index
  sets. Schema-rewind fixtures drop the three v19 tables before replaying
  older migrations.
- Cutover confirmation, targets, and tests are unchanged.
- Historical JSONL bindings remain readable and, once every related run is
  definitive and old enough, use the same disposal path with `store_backend`
  `jsonl`. Canonical context hashes are not rewritten.
- SDK dispatch, recovery, and deadline behavior from A and B stay on their
  existing callers. The new guard is the pre-open disposal check.

## Correction F-01 through F-05

Each finding was confirmed in production code and fixed. None is disputed or
blocked. The pre-correction 307-pass run below does not cover these fixes.

### F-01 — fixed

`scheduler cursor-store cleanup` preview and `scheduler cursor-store policy`
without `--automatic` no longer construct a writable store.
`src/ai_dev_loop/cli.py` uses `_cursor_store_reader`, which returns no store
when `engine.sqlite3` is missing or a symlink and otherwise calls
`SqliteSchedulerStore.open_readonly`. Apply and `--automatic` stay on the
writable constructor. Historical ledgers skip the retention policy and
disposal tables when `sqlite_master` does not contain them
(`_policy_from_connection`, `_assess_group`).

Coverage: `test_cli_inspection_does_not_create_or_migrate_a_ledger` asserts a
missing parent stays absent, then a downgraded schema-18 ledger keeps
`user_version`, tables, and modes, then a current ledger preview does not add
a disposal row or remove the root.

### F-02 — fixed

`_assert_tombstone_publishable` and `_ensure_tombstone` in
`src/ai_dev_loop/scheduler/application/cursor_sdk_retention.py` require a real
owned state root and disposals directory, reject symlinks at the directory,
`.<key>.json.tmp`, and `<key>.json`, and open the temporary with
`O_CREAT|O_EXCL|O_NOFOLLOW`. Manual apply and automatic maintenance share that
publisher. Refusal happens before the conversation root is removed.

Coverage: `test_tombstone_publication_rejects_symlink_escapes` keeps the
external file bytes and mode for the directory, temporary-file, and
destination symlink cases.

### F-03 — fixed

After the intent hook, apply rechecks the conversation fence generation. A
lost generation republishes the stored `receipt_payload` when the row is
already `deleted`, or returns `fence_busy` without abandoning the other
worker's intent. `_finalize_receipt` updates only
`status='intent' AND receipt_id=? AND fence_generation=?` and treats
`rowcount != 1` as adoption of the stored receipt.

Coverage: `test_stale_cleanup_generation_cannot_replace_the_receipt` lets the
second worker run at `now + 31s`. The tombstone bytes stay equal to the ledger
payload, and a later resume keeps that receipt id.

### F-04 — fixed

`_bind_reconciled_conversations` reads
`scheduler_cursor_sdk_reconciliations` when the current checkpoint has no
`cursor.sdk`. It authenticates `intent_payload` against
`intent_payload_sha256`, parses `CursorSdkReconciliationIntentV1`, and takes
the store backend only from `source_state.cursor.sdk` when that object's
owner and key match the intent. It does not read native blobs or directory
names. Pending or blocked reconciliation, a live attempt, and a ready row
without an authentic event still refuse deletion.

Coverage: `test_killed_abort_reconciliation_preserves_historical_record_and_resumes`
now checks preview, manual eligibility, and that maintenance one microsecond
before completion plus 168 hours leaves the root.
`test_phase25_7_blocked_abort_manual_disposal_removes_one_root` then deletes
that one root and leaves the aborted ledger row.
`test_reconciled_abort_uses_authenticated_completion_not_abort_time` covers
the pending and missing-event refusals.

### F-05 — fixed

`_reconciliation_completion_stamps` uses the hash-checked reconciled event's
`created_at`. `definitively_finished_at` is the maximum of the state stamps
and those event stamps. It does not use `mtime`, `scheduler_runs.updated_at`,
or `scheduler_cursor_sdk_reconciliations.updated_at`. A ready reconciliation
without that event blocks deletion even when `aborted_at` is old.

Coverage: `test_phase25_7_abort_retention_starts_after_native_reconciliation`
drives the production active-abort path with abort at
`2027-01-01T01:00:00Z` and reconciliation at `2027-01-01T02:00:00Z`.
Maintenance at abort plus 168 hours and one microsecond before completion
plus 168 hours deletes nothing. The exact completion boundary deletes once,
and a repeated pass keeps the same receipt and tombstone. The unit test also
withholds deletion when the event is absent.

## Correction F-06 through F-08

F-01 through F-05 stay in place. These three findings were confirmed and
fixed. None is disputed or blocked.

### F-06 — fixed

F-04 grouped a run whose current checkpoint no longer has `cursor.sdk` when a
hash-authenticated reconciliation intent still names that conversation. A
successful SDK recovery leaves the source `blocked` and publishes a successor.
The production chain in
`test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift`
is blocked, then blocked, then completed, with ready recovery rows and no live
attempt or reservation. After F-04 those immutable ancestors joined the one
shared conversation, and `_assess_group` rejected each solely because its
historical kind was still `blocked`.

`_superseded_blocked_runs` and `_ready_successor_edge` in
`src/ai_dev_loop/scheduler/application/cursor_sdk_retention.py` now accept a
blocked ancestor only when one ready `scheduler_cursor_initial_recoveries`
row authenticates (`intent_payload` hash, `CursorSdkSuccessorIntentV1`,
matching owner and key, and `record_sha256`) and the successor chain reaches
a definitive member of the same conversation. The blocked payload is not
rewritten. A blocked run with no such edge, a corrupt edge, a waiting
descendant, or a live attempt remains ineligible. Pending recovery,
uncertain attempts, and missing completion stamps still block deletion.

Coverage: `test_phase25_7_completed_recovery_lineage_follows_latest_completion`
runs that production flow, requires manual eligibility, refuses maintenance
one microsecond before the latest authenticated completion plus 168 hours,
deletes at that instant, and keeps the receipt on a repeated pass.
`test_blocked_ancestor_without_successor_stays_ineligible` and
`test_authenticated_successor_chain_uses_the_completed_stamp` cover a bare
blocked ancestor, a corrupt edge, a waiting descendant, and an uncertain
attempt.

### F-07 — fixed

`_ensure_tombstone` no longer unlinks the shared `.<key>.json.tmp` path. Each
publication creates `.<key>.<random>.<pid>.json.tmp` with
`O_CREAT|O_EXCL|O_NOFOLLOW` and replaces only that file onto the destination.
A symlink at the old predictable temporary path, the disposals directory, or
the destination is still rejected. Overlapping apply and receipt replay can
both publish the same authenticated payload. Neither removes the other's
temporary file.

The first correction only synchronized publication after `cursor-sdk-disposals/`
already existed. `_ensure_tombstone` still used an unchecked `exists` then
`os.mkdir`. Two first-time publishers could both observe the directory absent;
the second created it and published, and the first then raised `FileExistsError`.
`_ensure_disposals_directory` catches that loss, then accepts the path only
when it is a real directory owned by this user. A symlink or a non-directory
is rejected and not followed. The mode is set to `0700` only when this caller
attempted creation.

Coverage: `test_concurrent_tombstone_publication_keeps_one_receipt` pauses the
first publisher inside `os.write`, after the directory exists, lets the second
apply finish, then resumes the first. Both complete, and the tombstone bytes
equal the ledger payload. `test_overlapping_first_publication_directory_creation`
starts with the directory absent, pauses the first caller inside `os.mkdir`
after the receipt is committed, lets the second create the directory and
publish, then resumes the first. The first returns `deleted`, the tombstone
matches the stored receipt, and the directory is a real non-symlink.

### F-08 — fixed

Automatic maintenance treated `already_disposed` as ineligible, so a receipt
committed before a failed `_ensure_tombstone` never gained its tombstone.
`_repair_unpublished_tombstones` republishes the authenticated stored
payload for a bounded number of missing or divergent tombstones after the
deletion batch. It does not delete again, does not allocate a new receipt,
and does not consume the deletion batch. An unauthenticated payload is
skipped. Symlinked destinations are not followed.

Coverage: `test_tick_repairs_tombstone_without_starving_deletion` injects the
failure through `TickService.run_once`, checks that the root is gone and the
receipt is `deleted`, restores publication, and checks later ticks write the
stored payload. A second ledger with `batch_limit=1` still deletes the
remaining eligible conversation in the same pass that repairs the tombstone.

## Validation

F-07 directory-creation correction. The 69-pass run below predates
`_ensure_disposals_directory`. This run includes that helper, the new
first-publication race, and the earlier F-01 through F-08 coverage:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/integration/test_phase25_3_sdk_recovery.py
70 passed in 117.91s (0:01:57)
```

`/tmp/test_phase257c_review03_publication.py::test_overlapping_first_publication_directory_creation`
passed with the repository race tests in `4 passed in 2.97s`.

Static and documentation checks after the directory-creation fix:

```text
uv run python -m ruff check .
All checks passed!

uv run python -m ruff format --check .
420 files already formatted

uv run python -m mypy src
Success: no issues found in 219 source files

uv run python -m mkdocs build --strict
Documentation built in 0.94 seconds
```

The strict docs build printed the pre-existing Material-for-MkDocs warning and
the existing unlisted incident pages. It exited 0.

Earlier F-06 through F-08 gate, before the first-publication directory race
was covered:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/integration/test_phase25_3_sdk_recovery.py
69 passed in 115.26s (0:01:55)
```

`/tmp/test_phase257c_review02_contracts.py` is outside the repository. Its
concurrent-publication and tombstone-repair tests passed. Its lineage test
still asserts the pre-fix reason `continuable_state` and therefore fails
once that ancestor is superseded; the repository test above is the durable
check and expects an empty reason list.

Static checks for that earlier gate. They do not cover the directory-creation
race:

```text
uv run python -m ruff check .
All checks passed!

uv run python -m ruff format --check .
420 files already formatted

uv run python -m mypy src
Success: no issues found in 219 source files

uv run python -m mkdocs build --strict
Documentation built in 0.84 seconds
```

The strict docs build printed the pre-existing Material-for-MkDocs warning and
the existing unlisted incident pages. It exited 0.

The 307-pass command below is the pre-correction delivery gate. It is not
evidence for F-01 through F-08.

Correction gate, after the final production and test edits:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/integration/test_phase25_3_sdk_recovery.py
38 passed in 94.07s (0:01:34)
```

That run includes the CLI inspection tests, tombstone and stale-generation
regressions, reconciled-abort age tests, the production blocked-abort preview
and manual disposal tests, and the delayed native-reconciliation age test.

An earlier same-session run of the SDK worker, unit recovery, dispatch
uncertainty, lifecycle, and integration recovery files passed before the
final assertion split. Production code did not change in that split:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/integration/test_phase25_3_sdk_recovery.py
63 passed in 94.60s (0:01:34)
```

The review harness `/tmp/test_phase257c_review_contracts.py` is outside the
repository. After the fixes it passed: 6 tests, included in `9 passed in
27.60s` together with the three production retention tests. Repository
coverage above is the durable regression set.

Static and documentation checks for this correction:

```text
uv run python -m ruff check .
All checks passed!

uv run python -m ruff format --check .
420 files already formatted

uv run python -m mypy src
Success: no issues found in 219 source files

uv run python -m mkdocs build --strict
Documentation built in 0.62 seconds
```

The strict docs build printed the pre-existing Material-for-MkDocs warning and
the existing unlisted incident pages. It exited 0.

Pre-correction focused gate, including the lifecycle file, the plan's SDK
regressions, cutover, and the schema-rewind suites affected by schema 19:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/test_phase25_1_sdk_adapter.py tests/unit/test_paths.py \
  tests/unit/scheduler/test_phase17_7_cutover.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase23_2_initial_recovery.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py \
  tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase28_short_names.py
307 passed in 535.83s (0:08:55)
```

`tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py` covers preview purity,
one-root apply, protected siblings, shared successors, capacity, max
iterations, uncertain abort, checkpoint handoff, pending recovery, symlink and
uid refusal, missing roots, both fence winners, crash convergence, unrelated
Codex work, the 168-hour boundary against `mtime` and the SQL column, later
related work, missing and damaged evidence, disabled and missing policy,
repeated ticks, the batch of 8, historical JSONL, worker preflight
classification, and CLI preview/apply/policy. Homes, ledgers, and clocks are
temporary. No model or live store is used.

Pre-correction static and documentation checks. They do not cover F-01
through F-05; the correction checks are the block above:

```text
uv run python -m ruff check .
All checks passed!

uv run python -m ruff format --check .
420 files already formatted

uv run python -m mypy src
Success: no issues found in 219 source files

uv run python -m mkdocs build --strict
Documentation built in 0.88 seconds
```

The strict docs build printed the pre-existing Material-for-MkDocs warning and
the existing unlisted incident pages. It exited 0.

## Remaining gates and boundaries

- The repository-wide pytest suite and the outer packaging/integration pipeline
  were not run. They remain the separate pipeline gate.
- Wheel build, operator installation, real-model smoke, live ledger mutation,
  and live conversation deletion were not executed.
- No timer was installed, enabled, or disabled. Linger was not changed.
- No `ai_dev_loop.yaml` change, staging, commit, push, or checkpoint commit
  was performed. The final leaf does not create a checkpoint; the outer
  scheduler owns staging and review.
