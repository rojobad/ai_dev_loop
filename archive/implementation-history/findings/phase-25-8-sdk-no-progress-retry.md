# Phase 25.8 — one automatic SDK resume-no-progress continuation

Date: 2026-10-21
Scope: C-01 through C-06 only

## Outcome

The scheduler classifies one public Cursor SDK terminal status, reserves one
same-agent continuation for that logical turn, and publishes it on a later tick
at least 60 seconds later. A second failure of the same turn stays on explicit
`implementer-retry --force`. No new OpenQuestion.

## Red, then green

The first production-path integration run failed before the delay and sequence
contracts were real:

- Successor publication raised `blocked Cursor SDK run lacks adjacent
  reconciliation authority`. The authorization event had been inserted between
  the reconciliation event and `cursor_turn_blocked`, so the existing decisive
  reconciliation reader no longer saw those two events as adjacent.
- After that event moved to after the block, sequence publication raised
  `source run is not the current blocked sequence leaf`. Holding the sequence
  active preserved the reservation but rejected the existing adoption path,
  which requires the current blocked leaf.
- Letting the sequence block without a reservation exception released the
  repository during the wait. Publication would then have been held for missing
  authority, and another run could have been admitted.
- The status assertion read the top-level JSON object. `scheduler status --output
  json` nests the new fields under `summary`.
- A second submit in the same repository failed at `git commit` because the
  first fake turn had left `tracked.txt` dirty. That case is now its own test.

The authorization event is now appended after the block event. The sequence
still becomes `BlockedSequenceState` on the current leaf, and
`no_progress_retains_repository` skips reservation release while the row is
`reserved` or `publishing`. Publication waits while that sequence is still
active and adopts only once the source is the blocked current leaf.

After those corrections, on the pre-correction tree:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase27_3_implementer_sequences.py
```

229 passed in 236.13s. An earlier run of the same command, before annotation-only
mypy fixes, was 229 passed in 244.45s.

`uv run python -m mypy src` first reported 7 errors (literal category assignment,
an untyped pending-result helper, a successor row union, and status keyword
unpacking). After annotations it reported `Success: no issues found in 225 source files`.

`uv run mkdocs build --strict` exited 0. It printed the existing Material for
MkDocs warning and the existing unlisted incident pages.

`uv run python -m ruff check .` exits 1 on pre-existing
`dashboard-web/e2e/fixture_server.py` import order (`I001`). That file is
unchanged in this phase. Ruff check of the phase sources and tests passed.

`uv run python -m ruff format --check .` exits 1 because pre-existing
`dashboard-web/e2e/token_fixture.py` would be reformatted. That file is
unchanged. The phase files are formatted.

## Contract status

### C-01 — exact public status only: satisfied

Category `cursor_sdk_resume_no_progress` comes from the public SDK `status`
event whose message, after an outer strip, is exactly `Agent turn stopped after
repeated resume attempts made no progress`, with `status` `ERROR` and the event
agent and run equal to the owned stream. The final native status must be
`error`. The worker writes `cursor/sdk-no-progress/<attempt>/witness-v1.json`
only for one such line. Assistant text, tool text, a prefixed message, a
mismatched identity, truncation, and a second match do not write a witness.
Stream exceptions stay uncertain and are not classified from exception text.
The witness stores the message digest, not the message.

### C-02 — truthful continuation diagnosis: satisfied

`continuation_ready` without this witness says a safe continuation was
established and that explicit `implementer-retry --force` is required. A
reserved continuation names the one-use allowance, the UTC due time, and tells
the operator to tick after that time rather than force another successor. An
exhausted turn says the automatic allowance is already used. Outcomes that are
not `continuation_ready` keep the existing "did not establish one safe
continuation" sentence. Status and history add category, action, consumed count,
and `due_at` only when this authority exists. They do not print the SDK message,
full native or session IDs, or a network cause. Historical summaries omit the
four fields.

### C-03 — one delayed continuation per logical turn: satisfied

Authorization is inserted in the reconciliation transaction with
`consumed_count` 1, status `reserved`, and `due_at` 60 seconds later. The
logical-turn key is the unwrapped base prompt, turn kind, conversation, review
count, and fix/review binding. It does not include the run, the attempt, or the
recovery-note wrapper hash. Replay uses `ON CONFLICT DO NOTHING`. A tick at or
after `due_at` schedules one owned reconciliation attempt on the blocked source.
That worker reads the native run. `finished` with recoverable text is ingested
through the existing terminal-result path and does not send another prompt. A
standalone replay returns the same run to staging. A sequence replay adopts a
successor at the same ordinal, then uses the ordinary checkpoint or final staged
handoff. Any other confirmed `error` observation publishes one successor through
`CursorSdkSuccessorService.force`. A tick before that due time does not. A
second exact failure finds the consumed row and does not reserve another
automatic continuation. Owned-process termination, exact native reconciliation,
and the same owner, key, and agent remain the existing successor predicates.

### C-04 — standalone and current sequence leaf: satisfied

Standalone initial turns and a current v4 sequence leaf use the same successor
publication. The sequence blocks on the source leaf, keeps that ordinal, and
keeps the repository reservation until adoption. The due tick adopts the
successor at ordinal 1. The successor's own no-progress failure blocks that same
ordinal and does not start the next phase. Before the due time, manual
`implementer-retry --force` reports `publication=pending` and
`agent_execution_ready=no` and does not insert a successor. Two publishers
synchronized on a barrier before `force` share one successor. A crash after the
`publishing` claim leaves no successor; the next call publishes one. Abort
cancels `reserved` or `publishing`, and a `published` row only while the
successor has not started and the sequence has not adopted it. A published
continuation that has already executed or been handed off is not an abort
target. Consumed count is not refunded, and a later due tick does not dispatch
a cancelled continuation. A cancelled successor relation may remain, with the
ready prompt and record fields cleared so the versioned intent still validates.
Retention preview, apply, and maintenance treat an open row as
`sdk_no_progress_pending` and ineligible. The recovery prompt remains the
existing fixed note plus the exact base instructions.

### C-05 — versioned persistence, historical readers unchanged: satisfied

Schema 20 adds `scheduler_cursor_sdk_no_progress`. Store methods no-op when the
table is absent, so a historical ledger stays manual. Witness and authorization
bodies are schema version 1 and match their published JSON schemas. Reconciliation
v1/v2 records, successor canonical records, and frozen submitted contexts are
not given new fields. `CursorInitialRecoveryResult` omits
`automatic_continuation` and `automatic_due_at` unless the continuation is
automatic, so existing recovery JSON stays stable. `ImplementerRetryResult` is
unchanged. A boolean consumed count is rejected. Timeout, capacity, reviewer-retry,
and other SDK failures do not create or reset this row. Downgrade fixtures drop
the new table before remigrating older schemas. Assertions that meant the current
schema version now expect 20. The Phase 25.7 retention schema version stays 19.

### C-06 — tests and the approved wording: satisfied

New coverage is `tests/unit/scheduler/test_phase25_8_sdk_no_progress.py` and
`tests/integration/test_phase25_8_sdk_no_progress.py`. The integration cases go
through submit, start, and tick with the fake SDK. The delay uses a controlled
UTC clock. The duplicate publisher uses a `threading.Barrier`, not sleep. The
fake SDK emits the exact status only when `FAKE_CURSOR_SDK_NO_PROGRESS=1`; the
previous terminal-status default is unchanged. Docs, `AGENTS.md`, and the loop
rule describe only this 60-second, one-use exception. Other generic failures stay
manual.

## Changed callers

- `cursor_attempt_runner` writes and returns the witness on the terminal `error` path.
- `cursor_sdk_reconciliation` asks `diagnose_continuation_ready` for a
  `continuation_ready` block, then records the decision after the block event.
- `tick` calls `publish_due_no_progress_continuations` after SDK reconciliation
  effects and before the per-run loop.
- `cursor_sdk_successor.force` returns the pending automatic result before the
  due time, rechecks the successor relation inside the write transaction, and
  cancels a pending successor when abort is requested before dispatch.
- `abort` cancels a reserved or publishing row in the abort transaction.
- `sequence_reconcile` still blocks a reconciled SDK failure and, while the
  continuation is unpublished, does not release that run's repository reservation.
- `cursor_sdk_retention` adds `sdk_no_progress_pending` for open rows in the lineage.
- `status`, `history`, `safe_actions`, and the scheduler CLI render the safe fields.
- `implementer_timeout_retry` maps a pending automatic result onto the existing
  result's `available_at` and source run id.
- `sqlite_store` migrates to schema 20 and reads and writes the new table.

## Historical compatibility

Records without the witness or without schema 20 stay on the manual path. They
are not scanned or retried from old transcripts. Canonical hashes of frozen
inputs, reconciliation records, and successor records are unchanged. A new
correction boundary, a timeout, or a capacity wait does not refresh this
allowance. Abort does not refund a consumed count.

## Correction F-01 through F-07

Date: 2026-10-21. The probes in `/tmp/phase258_review_probes.py` reproduced each
defect on the pre-correction tree. The first rerun of the mandatory command
after the fresh-read change was `21 failed, 222 passed in 298.20s`: the witness
schema had illegal trailing commas, the duplicate publisher raised
`BrokenBarrierError` because the native read swapped `cursor_sdk` before
`before_force`, and three Phase 25.7 deadline tests saw the leaked fake
`Client` (`AttributeError: type object 'Client' has no attribute 'connect'`).

The native read now holds one lock, prepends the test `PYTHONPATH` only for
that call, and restores every `cursor_sdk` module. The witness file was
rewritten from `CursorSdkNoProgressWitnessV1.model_json_schema()` as strict
JSON. On the corrected tree:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase27_3_implementer_sequences.py
```

247 passed in 327.48s.

`uv run python -m mypy src` reported `Success: no issues found in 225 source files`.
`uv run mkdocs build --strict` exited 0, with the existing Material warning and
the existing unlisted incident pages.
`uv run python -m ruff check .` still exits 1 only on unchanged
`dashboard-web/e2e/fixture_server.py` `I001`.
`uv run python -m ruff format --check .` still exits 1 only on unchanged
`dashboard-web/e2e/token_fixture.py`. Those two Dashboard files were not edited.

Phase 25.3, 25.7, and 27.3 results in that command are the existing manual
recovery, deadline, and sequence regressions. They are not evidence for this
automatic policy. The automatic-policy evidence is the Phase 25.8 tests named
under each finding.

### F-01 — fresh native evidence before publish and send: reopened

The first correction observed the native run inside the scheduler process and
stopped at "the source is no longer blocked" plus send count. That missed
correction staging, which accepts only `CursorReadyState`, and sequence
checkpoint admission, which requires an active current leaf. See the round-2
section.

### F-02 — full capture digest: reopened

The first correction authenticated the events digest before `force`. The
dispatch binding then carried only `no_progress_guard_native_run_id`, so damage
after publication and before send was outside that assertion. See the round-2
section.

### F-03 — contradictory terminal sequence: fixed

The worker records public terminal status names and writes the witness only
when that sequence is exactly `("ERROR",)`, the capture is not truncated, and
there is one exact status hit. `RUNNING` is not a terminal name, so
running-then-error still qualifies. Error followed by finished does not.
`test_contradictory_terminal_status_does_not_write_a_witness` asserts no
witness for error-then-finished and a witness for running-then-error. That
evidence stays on the generic manual path.

### F-04 — authorization and due time in the successor transaction: fixed

`CursorSdkSuccessorService.force` still consults the pending result before the
transaction. Inside `begin_immediate`, after the schema-3 relation check, it
calls `pending_result_for_connection` on the same connection. A reserved row
whose due time is still ahead returns that pending result and does not insert.
`test_manual_force_during_authorization_reuses_the_pending_continuation` drives
`ImplementerRetryService.retry(force=True)`, pauses after the pre-transaction
lookup, lets a tick reserve the allowance, then resumes. The result is
unchanged, `available_at` is set, and there is no successor.

### F-05 — abort the pending continuation after reservation transfer: reopened

The first abort tests cancelled inside publication hooks and never parsed the
persisted ready intent. A cancelled payload still carried
`effective_prompt_path`, `effective_prompt_sha256`, and `record_sha256`, which
`CursorSdkSuccessorIntentV1` rejects. See the round-2 section. The hook-boundary
tests remain.

### F-06 — resume an existing matching successor: fixed

`_publish_one` claims `publishing`, runs the `after_claim` hook, then
`_resume_existing_publication` before `_revalidate` and before the
source-ownership and current-leaf checks. One schema-3 pending/ready relation
for the same failed attempt is resumed through `force`, and the authorization
is marked `published`. Consumed count stays 1.
`test_crash_after_force_publishes_the_existing_successor` raises at
`after_force`, clears the hook, and ticks again. The same successor is
published once. `test_abort_and_crash_and_duplicate_publish_share_one_continuation`
still shares one successor across two publishers.

### F-07 — automatic-policy acceptance: reopened

The first correction stopped the timeout and capacity cases when the wait was
scheduled. It did not execute those continuations, challenge the same logical
turn again, extend the review ceiling, or exercise the remaining crash and
retention-apply boundaries. See the round-2 section. The successful automatic
initial, correction, and sequence cases from that correction remain.

## Correction round 2 — F-01, F-02, F-05, F-07, F-08, F-09

Date: 2026-10-22. Probes in `/tmp/phase258_round2_review.py` were converted into
`tests/integration/test_phase25_8_sdk_no_progress.py`. F-03, F-04, and F-06 stay
as recorded above. The in-process `_observe_native_gate` and its
`sys.modules` SDK swap were removed.

The mandatory command on this tree:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase27_3_implementer_sequences.py
```

256 passed in 396.39s (exit 0). That process imported the production modules
before the final annotation edit: a duplicate `ReconciliationRecord` alias was
removed, the refused dispatch dict was annotated, `fetchone` was cast, and a
blocked state that reaches the ordinary or timeout cursor-binding branch now
returns `attempt_state_changed` without calling `_cursor_binding`. The reobserve
effect does not use those branches. After that edit,
`uv run python -m mypy src` reported `Success: no issues found in 225 source files`.
The 256-test command was not repeated after the annotation edit.

`uv run mkdocs build --strict` exited 0 earlier in this correction, before the
annotation edit. Docs were not changed after that build. It printed the existing
Material warning and the existing unlisted incident pages.

After the annotation edit, `uv run python -m ruff check .` exits 1 only on
unchanged `dashboard-web/e2e/fixture_server.py` `I001`.
`uv run python -m ruff format --check .` exits 1 only on unchanged
`dashboard-web/e2e/token_fixture.py` (1 file would be reformatted, 430 already
formatted). Those two Dashboard files were not edited.

Phase 25.3, 25.7, and 27.3 results in the 256-test command are inherited manual
recovery, deadline, and sequence regressions. They are not evidence for this
automatic policy. The automatic-policy evidence is the Phase 25.8 tests named
under each finding.

### F-01 — finished replay completes the supported ingestion: fixed

`_publish_one` does not call the SDK. A due authorization with no completed
reobserve inserts effect `cursor-sdk-no-progress-reobserve`
(`RECONCILE_CURSOR_SDK_RUN`, payload `no_progress_reobserve: true`) and returns.
`tick` appends `no_progress_reobserve_run_ids`. `AttemptService` launches,
observes, and completes `BlockedState` only for that effect.
`cursor_sdk_reconciliation.ingest` calls `consume_no_progress_reobserve` and
does not rewrite the historical reconciliation row. A standalone
`terminal_recovered` result installs `CursorReadyState`, the replay fingerprint,
`CursorSdkTurnCompletedEvent`, and normalize-staging, then cancels the
authorization. A sequence result commits that ingest, then
`finish_no_progress_terminal_replay` adopts through `force`, copies the
fingerprint by its aggregate (the file digest is not the aggregate hash),
drops the successor run-turn, and inserts normalize-staging. Consumed count
stays 1. Reviewer, allowance, and ordinal stay on the existing recovery record.

`test_changed_native_evidence_does_not_send_again` finished case reaches
`completed` on the same run with send count 1 and authorization `cancelled`.
`test_finished_correction_replay_reaches_completion` reaches `completed` on the
same correction run with send count 2. `test_finished_sequence_replay_advances_without_another_prompt`
adopts ordinal 2 and records `accepted SDK no-progress checkpoint` with send
count 1.

The previous assertions used `kind != "blocked"` and send count. They never
required a correction to leave `waiting_for_cursor_fix` or a sequence leaf to
pass `enter_checkpoint_pending`.

### F-02 — dispatch rechecks the frozen capture: fixed

`no_progress_dispatch_authority` returns the witness events path and
`events_sha256` with the native run id, or `no_progress_evidence_refused`.
`AttemptService._cursor_binding` attaches that evidence on a successor prompt.
`cursor_attempt_runner._no_progress_send_guard` re-hashes the source events file
before `get_run`. A missing, changed, or conflicting digest returns
`cursor_sdk_native_run_uncertain` and does not send. Consumed count stays 1.
`_resume_existing_publication` uses the same published row, so a resumed
successor carries the same authority.

`test_changed_capture_blocks_automatic_publication` still refuses before
publication. `test_postpublication_capture_damage_blocks_send` crashes after
`force`, resumes publication, deletes every events line except the matching
status, and asserts the worker does not send, status stays `published`, and
consumed count stays 1.

The previous check ran only inside `_revalidate` before `force`. The interval
after the successor existed and before send was not authenticated.

### F-05 — cancelled ready intent validates: fixed

`_fence_no_progress_successor` sets schema-3 status `cancelled` and clears
`effective_prompt_path`, `effective_prompt_sha256`, and `record_sha256`.
`CursorSdkSuccessorIntentV1` is unchanged. The historical record file stays on
disk. Ledger record path and hash are cleared. Termination and reservation
release stay on the existing abort path.

`test_abort_ready_unstarted_successor_validates_cancelled_intent` parses the
persisted payload with `CursorSdkSuccessorIntentV1.model_validate_json`, checks
the three ready fields are null, keeps the historical record bytes, reads
status and history, and shows later ticks do not dispatch. Send count stays 1.

The previous abort tests aborted inside publication hooks and never loaded the
cancelled ready intent through the production model.

### F-07 — remaining automatic-policy cases: fixed

Kept from the previous correction, and still passing in the 256-test run:
`test_successful_automatic_initial_continuation_reaches_review`,
`test_automatic_correction_keeps_reviewer_base_note_and_ceiling`,
`test_manual_descendant_executes_without_refreshing_the_allowance`,
`test_new_correction_receives_a_fresh_allowance`,
`test_successful_sequence_continuation_checkpoints_the_non_final_phase_once`,
`test_final_sequence_phase_stays_staged_without_a_commit`, and
`test_exact_no_progress_waits_then_continues_once_and_holds_the_second`.

Added or extended in this correction:

- `test_timeout_descendant_does_not_refresh_the_allowance` records one timeout
  retry, continues after the 30-minute delay, then hits no-progress again.
  Send count goes to 3. One row stays consumed 1, and the block says the
  allowance is already used.
- `test_capacity_descendant_does_not_refresh_the_allowance` reaches
  `waiting_usage_limit`, continues when the wait ends, then hits no-progress
  again. Send count goes to 3. Consumed count stays 1.
- `test_extended_review_ceiling_is_frozen_on_the_continuation` exhausts a
  submitted ceiling of 1, extends the effective ceiling to 2, and freezes both
  numbers on the correction successor.
- `test_dispatch_crash_keeps_one_owned_continuation` leaves one owned
  nonterminal attempt across a dispatch crash, then one later send. The second
  no-progress failure does not add a row.
- `test_pending_retention_apply_and_maintenance_keep_the_conversation` refuses
  cleanup apply and maintenance deletion while the row is `reserved`, with
  reason `sdk_no_progress_pending`.

The previous timeout and capacity tests stopped when the wait was scheduled.
They did not execute the continuation or challenge the same logical turn.

### F-08 — completed ancestry is not an open abort hold: fixed

`source_has_open_no_progress` is true for `reserved` and `publishing`, and for
`published` only while `_published_continuation_unstarted` (no successor
attempt, successor still `cursor_ready` or `waiting_for_cursor_fix`, and the
sequence current run is not the successor). `obsolete_no_progress_source` is
the published complement. `abort_run` raises
`automatic continuation already left this source; abort the current run` on
both the outer read and the immediate transaction, before mutation.
`cancel_no_progress_for_abort` returns without rewriting a relation that is no
longer open. This is separate from clearing ready fields on a genuinely pending
cancellation.

`test_completed_successor_ancestry_survives_old_source_abort` completes the
successor, then abort of the old source raises and the CLI abort is non-zero.
The source stays blocked. Recovery authority, payload and record hashes, and
retention preview stay unchanged.

### F-09 — observation runs in the owned reconciliation worker: fixed

`_observe_native_gate`, `_fresh_native_gate`, and the scheduler-process
`sys.modules` SDK swap are gone. Fresh observation and replay use the existing
`_run_sdk_reconciliation` worker under `_sdk_operation_deadline`. A nonterminal
reobserve does not cancel the native run. `running`, unavailable, and
finished-without-text become `native_unavailable` and hold the authorization.
Finished with text becomes `terminal_recovered`. Confirmed `error` stays
`continuation_ready` and is what a later tick publishes.

`test_owned_reobserve_stays_nonterminal_until_aborted` keeps one owned
nonterminal reobserve attempt across ticks, then abort terminates that attempt
id. Send count stays 1 and consumed count stays 1. Phase 25.7 deadline tests in
the same command still pass without a leaked fake client.

## Correction round 3 — F-01, F-05, F-07, F-08, F-09

Date: 2026-10-22. Probes in `/tmp/phase258_round3_review.py` were converted into
`tests/integration/test_phase25_8_sdk_no_progress.py`. F-02, F-03, F-04, and
F-06 stay as recorded above. The round-2 "fixed" claims for F-01, F-05, F-07,
F-08, and F-09 did not cover the intervals below.

The mandatory command on this tree first reported `3 failed, 260 passed in
432.34s`. Those three failures were Phase 25.6 pending-envelope readers: the
new reobserve branch called `get_effect_by_dispatch_id` before the pending
check, and their fake store rejects that lookup. The pending path now looks up
the effect only when the reconciliation row is already `ready`. After that:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py::test_published_v2_record_is_recovered_when_its_envelope_is_lost \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py::test_published_v2_conflicts_with_a_same_attempt_v1_record \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py::test_tampered_published_v2_record_is_not_recovered \
  tests/integration/test_phase25_8_sdk_no_progress.py::test_lost_reobserve_before_publication_retries_the_same_allowance \
  tests/integration/test_phase25_8_sdk_no_progress.py::test_lost_reobserve_after_record_publication_does_not_reread
```

5 passed in 10.04s. The 263-test command was not repeated after that lookup move.

`uv run python -m mypy src` reported `Success: no issues found in 225 source files`.
`uv run mkdocs build --strict` exited 0, with the existing Material warning and
the existing unlisted incident pages.
`uv run python -m ruff check .` exits 1 only on unchanged
`dashboard-web/e2e/fixture_server.py` `I001`.
`uv run python -m ruff format --check .` exits 1 only on unchanged
`dashboard-web/e2e/token_fixture.py` (1 file would be reformatted, 430 already
formatted).

Phase 25.3, 25.6, 25.7, and 27.3 results are inherited regressions. They are
not evidence for this automatic policy except the three Phase 25.6 cases above,
which prove the pending-envelope check was not relaxed.

### F-01 — replay successor stays non-dispatchable until install: fixed

`_finish_sequence_replay` still adopts through `force`, then
`_fence_replay_dispatch` cancels the successor `cursor.run_turn` before copying
recovered output. A copy or install failure leaves the authorization
`publishing` and does not mark it `held`, so the next tick resumes the same
successor. `no_progress_dispatch_authority` is not required to guard that
interval because the run-turn effect is already cancelled.

`test_replay_artifact_fault_restarts_without_another_prompt` raises `OSError`
on the fingerprint copy, proves the run-turn is not pending and the
authorization stays `publishing`, then a new tick service installs the same
successor and checkpoints with send count 1.
`test_replay_install_fault_restarts_without_another_prompt` makes
`_install_replay_turn` fail once and then restarts to the same checkpoint
without another prompt.

The previous replay tests drove the happy path. They never failed the copy
after `force` had already inserted an executable run-turn, so a `held`
authorization with no send guard was outside the assertion.

### F-05 — aborted unstarted successor is a definitive lineage: fixed

`_fence_no_progress_successor` still writes the validated cancelled intent and
clears the three ready fields. When the successor has no nonterminal attempt,
`_abort_unstarted_successor` appends the abort request and `run_aborted` events
and compare-and-swaps `cursor_ready` or `waiting_for_cursor_fix` to
`AbortedState`, then the existing reservation release runs. Retention rules
were not weakened.

`test_aborted_unstarted_successor_becomes_retention_eligible` aborts the source,
checks the successor is `aborted`, later ticks do not send, and after 169 hours
every conversation in the cleanup preview is manually eligible.
`test_abort_ready_unstarted_successor_validates_cancelled_intent` still parses
the cancelled intent.

The previous correction only made the cancelled payload validate. It left the
successor `cursor_ready`, so retention stayed ineligible for `continuable_state`.

### F-07 — crash and restart evidence: fixed

The timeout, capacity, extended-ceiling, and pending-retention tests from round
2 still pass. `test_dispatch_crash_keeps_one_owned_continuation` was replaced:
`test_dispatch_crash_restarts_the_same_continuation` raises during an active
owned observation, constructs a new tick service on the same process backend,
and continues that same run-turn. Send count goes from 1 to 2, consumed count
stays 1, the successor and reservation stay, and no second automatic row appears.
`test_relation_crash_restarts_onto_the_same_successor` raises at `before_force`
and a new tick service publishes one successor without sending. The artifact
and install restarts are the F-01 tests. The post-adoption abort is the F-08
test.

The previous dispatch test advanced `active_ticks` on the same tick service. It
never raised and never constructed a restarted service.

### F-08 — adoption counts while the authorization is still publishing: fixed

`_continuation_departed` reads schema-3 recovery rows, not
`authorization.successor_run_id`. A successor attempt, or a sequence
`current_run_id` equal to that successor, makes the source obsolete in
`reserved`, `publishing`, and `published`. `abort_run` still raises
`automatic continuation already left this source; abort the current run` before
mutation. Pre-adoption abort tests still cancel.

`test_post_adoption_source_abort_keeps_the_current_leaf` aborts inside
`publish_fault_hook('after_force')` while the authorization is still
`publishing` and the sequence leaf has already moved. The abort is refused, the
source stays blocked, the ready relation stays, and send count stays 1.

The previous cutoff treated every `publishing` row as open and only inspected
`published` rows, so it missed the window after adoption and before the
authorization copy.

### F-09 — a lost reobserve retires against the ready row: fixed

`authenticated_published_reconciliation_result` keeps the pending-row path
unchanged. A `ready` row is accepted only when the effect payload is
`no_progress_reobserve` for that recovery key. It authenticates the historical
record, refuses a result that moved onto the historical path, and returns this
attempt's published record when one exists. `_resume_interrupted_reconciliation`
uses the same role check, verifies the historical file hash, marks the lost
attempt ingested, and calls `schedule_lost_reobserve_replacement` only when
this attempt published nothing. The ready row and consumed count stay. Normal
pending reconciliation does not take this branch.

`test_lost_reobserve_before_publication_retries_the_same_allowance` proves one
missing worker is ingested as failed, a second owned read publishes one
successor, send count stays 1, and the historical result hash is unchanged.
`test_lost_reobserve_after_record_publication_does_not_reread` interrupts after
`reconciliation_record` is written and before the envelope. One reobserve
attempt is ingested, one successor is published, and there is no second read
or prompt. The worker interrupt is the existing
`AI_DEV_LOOP_SDK_INTERRUPT_AFTER` hook.

The previous worker migration ran the read in the owned worker but left
lost-envelope authentication and interrupted retirement requiring `pending`
authority. A ready historical row therefore kept the attempt uncertain and the
authorization `publishing`.

## Correction round 4 — F-10, F-11

Date: 2026-10-22. Probes in `/tmp/phase258_round4_review.py` were converted into
`tests/integration/test_phase25_8_sdk_no_progress.py`. F-01 through F-09 stay as
recorded above. This turn did not capture a pre-fix pytest log; the defects are
the selectors named below, and the green runs are the evidence after the fix.

Focused new tests, before the full command:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/integration/test_phase25_8_sdk_no_progress.py::test_abort_during_replay_install_cancels_the_current_leaf \
  tests/integration/test_phase25_8_sdk_no_progress.py::test_later_logical_turn_reads_its_own_native_run
```

4 passed in 30.43s.

The mandatory command then reported `267 passed in 499.16s`. That includes the
four new cases and the existing F-01–F-09 regressions. Phase 25.3, 25.6, 25.7,
and 27.3 results in that command are inherited regressions, not new evidence
for this automatic policy.

`uv run python -m mypy src` reported `Success: no issues found in 225 source files`
after the line-wrap format.
`uv run mkdocs build --strict` exited 0, with the existing Material warning and
the existing unlisted incident pages.
`uv run python -m ruff check .` exits 1 only on unchanged
`dashboard-web/e2e/fixture_server.py` `I001`.
`uv run python -m ruff format --check .` exits 1 only on unchanged
`dashboard-web/e2e/token_fixture.py` (1 file would be reformatted, 430 already
formatted).

### F-11 — a later logical turn uses its own native read: fixed

`get_cursor_sdk_no_progress_for_source` now prefers `publishing`, then
`reserved`, then `published`, then `held`, then the newest row. A cancelled
historical allowance no longer hides the current turn. Callers of that selector
are `pending_result_for_connection` (the path
`ImplementerRetryService.retry(force=True)` uses through
`CursorInitialRecoveryService.force` and `CursorSdkSuccessorService.force`),
`source_has_open_no_progress`, `obsolete_no_progress_source`,
`cancel_no_progress_for_abort`, `no_progress_retains_repository`,
`no_progress_projection` (`scheduler status`), `no_progress_guard_native_run_id`,
`no_progress_dispatch_authority`, and `_transition_authorization`. Replay ingest
in `cursor_sdk_reconciliation.py` also loads the authorization through that
selector. Exact attempt binding is
`get_cursor_sdk_no_progress_for_source_attempt`.

Reobserve busy checks, latest-attempt reads, scheduling, replay finish, and
lost-worker replacement filter `no_progress_reobserve` effects by the current
authorization's reconciliation `recovery_key`. A new read uses effect id
`cursor-sdk-no-progress-reobserve-<source_attempt_id>`. A recovered record whose
`source_attempt_id` is not the current turn is not replayed or treated as the
fresh observation. A single historical row still selects that row. Consumed
count stays 1 on each row, and the cancelled row's payload and witness hashes
are not rewritten.

`test_later_logical_turn_reads_its_own_native_run` replays the first failure
onto the same run, lets review request a correction, and blocks that correction
on an authenticated no-progress failure. Two authorization rows remain.
`status` reports the fresh `due_at` and consumed `1`. Manual
`ImplementerRetryService.retry(force=True)` before that due time returns
`changed=False` and creates no successor. At the fresh due time the owned read
carries the correction's recovery key, two reobserve attempts exist, one
successor is published, and the send count stays 2. The first reconciliation
result hash and the first authorization payload hash stay unchanged.

Earlier coverage replayed one turn or published a successor for a different run.
It never left a cancelled allowance and a new reserved allowance on the same
source, so the oldest-row lookup and the reused reobserve effect id were
outside the assertions.

### F-10 — abort of the adopted replay leaf stops install: fixed

`abort_run` calls `cancel_adopted_replay_for_aborted_successor` after
`cancel_no_progress_for_abort`. Sequence abort reaches the same function because
`SequenceAbortService` aborts the current run. The lookup is the schema-3
recovery row for the aborted successor, matched to
`source_attempt_id = failed_attempt_id`, including while the authorization's
`successor_run_id` is still null. Only that authorization becomes `cancelled`.
The ready recovery status, intent hash, and record hash are not rewritten, and
`_fence_no_progress_successor` is not used on this path. Consumed count stays 1.
`_finish_sequence_replay` sees an aborted successor and cancels the install
instead of copying again. `obsolete_no_progress_source` still rejects abort of
the old source after handoff when the current row is `cancelled`.

`test_abort_during_replay_install_cancels_the_current_leaf` copies the
fingerprint once with `OSError` after adoption, then aborts either the
successor run or its sequence. A new tick service runs 12 ticks. The successor
stays aborted, the authorization is `cancelled`, the send count stays 1, no
`cursor.run_turn` or no-progress reobserve effect is pending, claimed, or in
`retry_wait`, and abort of the old source still raises
`automatic continuation already left this source; abort the current run`. After
169 hours every cleanup conversation is manually eligible and none reports
`sdk_no_progress_pending`. Retention rules were not weakened.

Earlier replay-fault tests restarted a successful install. They aborted the
source, or aborted an unstarted successor before adoption, so they never left a
`publishing` predecessor after the current leaf had been aborted during the
interval before `successor_run_id` was copied.

## Unexecuted gates

Repository-wide pytest, wheel and isolated install, live model acceptance, live
ledger or native-store cleanup, integration installation, and workstation timer
or service changes were not run. Tests used temporary homes, repositories, and
ledgers, fake SDK and CLI executables, and a controlled clock. The two unchanged
Dashboard Ruff findings were left as they are.
