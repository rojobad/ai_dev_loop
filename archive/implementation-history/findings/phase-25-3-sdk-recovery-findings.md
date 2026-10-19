# Phase 25.3 SDK continuity and recovery findings

Status: **READY_FOR_REVIEW**. This is implementer evidence, not scheduler or
reviewer acceptance. It authorizes no commit, checkpoint, installation, live
provider operation, push, PR or merge. Final changes remain for the scheduler to
stage as one snapshot and for the separate Codex reviewer to inspect under the
preserved staged-only review contract. Only the scheduler may create an accepted
non-final sequence checkpoint.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-25-sdk-replanned`, starting HEAD
`7c1f4171bb986caa4522d4c4604f20ba87c6fe85`. Baseline `558b799` is an ancestor
(`git merge-base --is-ancestor 558b799 HEAD`, exit 0), so this slice starts from
accepted Phases 26-28 and accepted Phase 25 predecessors, not the original Phase
23 baseline. Phase 24 is unexecuted and is not a prerequisite. The replanned
overview, accepted Phase 25.2 plan/findings and preserved POC index were read.
Their prerequisite contracts were present and both the overview and Phase 25.3
plan have `OpenQuestions: None`; no dependent work proceeded through a failed or
unresolved prerequisite.

All automated runs used fake SDK/provider responses, fake Codex responses, fake
process backends or isolated synthetic descendants, and temporary repositories
with test-isolated HOME/XDG state. No real model, live credential, live scheduler
ledger, workstation service/timer, installation, cleanup, external Git action or
destructive Git action was used. This changing checkout was never hot-installed.

## R-01-R-04 contract evidence

| ID | Production callers and implemented contract | Independent production-path tests | Result |
| --- | --- | --- | --- |
| R-01 | `AttemptService._reconcile_existing_attempt`, `CursorWorkflowService._ingest_sdk_turn`, `CursorSdkReconciliationService.request`/`ingest`/`ensure_pending_effects`, `cursor_attempt_runner._run_sdk_reconciliation`, `CursorTimeoutRetryService.retry`, and `TickService.run_once` reconcile the exact native run only after authenticating the attempt and proving the complete owned process tree absent. Exact terminal results are replayed without a send; an exact stale nonterminal run is registered, cancelled and confirmed before the existing timeout/capacity policy may authorize one continuation. A proved-dead reconciliation worker is fenced and its same pending obligation is resumed; it never grants prompt authority. Reconciliation records are worker-specific immutable artifacts, so authenticated historical evidence cannot be mistaken for the interrupted worker's output. Explicit nonzero SDK terminals without a structured quota classification enter authenticated generic-failure reconciliation rather than capacity handling. Unknown or incomplete process/native evidence keeps the reservation and hold. | `test_exact_running_native_run_is_cancelled_and_confirmed_before_retry`, `test_finished_native_run_is_replayed_without_a_second_send`, `test_reconciled_timeout_and_capacity_reenter_existing_policy_once`, `test_failed_worker_without_result_stops_owned_descendants_before_native_replay`, `test_killed_reconciliation_worker_resumes_obligation_without_prompt_redispatch`, `test_killed_abort_reconciliation_preserves_historical_record_and_resumes`, and `test_correction_capacity_timer_preserves_exact_turn_then_uses_decisive_failure`; Phase 25.2 process-tree tests independently cover launcher/runtime death and result-publication interruption. | **Implemented; current 58-test affected matrix passed.** |
| R-02 | The actual `scheduler cursor-retry --check`, `cursor-retry --force`, and Cursor `scheduler implementer-retry --force` callers use authenticated causal reconciliation evidence. `CursorSdkSuccessorService`, `CursorInitialRecoveryService`, `SequenceCursorRecoveryService`, `cursor_budget_carry`, `AttemptService._cursor_binding`, reviewer/correction services and sequence reconciliation preserve the exact conversation owner/key/store, agent, frozen provider settings, reviewer B, prompt/fix evidence, budgets, counters and current sequence ordinal. Durable successor ancestry authentication is separate from same-turn launch eligibility, so accepted successors remain authentic in review/capacity/downstream states without permitting a new dispatch. Source bytes remain immutable; `cursor_retry` and `implementer_retry` ancestry stay distinct; short names copy once and operator renames are not overwritten. Codex recovery remains on its existing branch. | `test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift` drives initial recovery, reviewer findings, correction recovery, repeated successor and acceptance through ticks/CLI; `test_recovered_sdk_sequence_leaf_checkpoints_then_advances_to_codex_provider` drives accepted SDK recovery into a mixed-provider sequence; `test_reconciled_failure_routes_through_cursor_implementer_retry_and_preserves_identity` covers both CLI surfaces, repeated recovery, lineage and copy-once metadata. Phase 23/27 recovery selections independently cover established callers and stale/current leaf rules. | **Implemented; prior complete and current affected matrices passed.** |
| R-03 | `cursor_sdk_journal` and `_run_sdk_turn_operation` durably record the exact-agent run inventory before the sole authorized send. `_list_sdk_runs_for_agent` and the reconciliation worker may adopt only one authenticated post-baseline run from that sender. Missing or conflicting candidates block with no resend and ownership held. A known terminal run is replayed through normal ingestion. Create acceptance before exact agent publication remains unsupported ambiguity: inspection/abort is the safe action, and no timestamp/model/latest-run inference or replacement-agent authority was added. Effective prompt resolution is shared by dispatch, ingestion, reconciliation and repeated recovery; recovery overrides and inherited base prompts apply only to their authorized logical turn. | `test_ambiguous_send_never_speculatively_redispatches`, `test_unique_post_baseline_run_is_adopted_then_cancelled_without_redispatch`, `test_abort_cancels_and_confirms_every_post_baseline_candidate`, `test_finished_native_run_is_replayed_without_a_second_send`, plus the initial/correction tick test's successful journal authentication and later distinct correction envelope. | **Implemented within the supported identity boundary; unsupported ambiguity blocks safely.** |
| R-04 | `SchedulerAbortService`, `AttemptService`, `ensure_aborted_sdk_turn_reconciliation`, `CursorSdkReconciliationService.ensure_pending_effects`, SQLite unresolved-effect queries and the process backends make durable abort win over retry, staging, review and late publication. Active SDK sends stopped during abort now publish an authenticated native reconciliation obligation before their attempt hold can be finalized; the restart/tick path enforces the same boundary. Durable successor ancestry remains authenticated, while current-turn prompt eligibility is derived from the aborted checkpoint so a later correction is not forced back onto the original recovery envelope. Only the specifically authorized reconciliation worker can finish normal attempt processing under the abort fence. Owned retained `failed`/`inactive` as well as `missing` units must be stopped and their whole trees proved absent before journal replay. Replays converge to native confirmation and only then release resources permitted by terminal abort state. | `test_abort_active_sdk_send_persists_native_obligation_until_idempotent_release` uses the real worker/bridge/tool fixture, an unavailable-observation hold, restart ticks and idempotent abort replay; `test_abort_later_successor_correction_uses_current_turn_authority` drives initial failure, `cursor-retry`, reviewer B findings and a synchronized later correction abort; `test_abort_reconciliation_completes_under_fence_without_redispatch` asserts no send/staging/review; `test_abort_holds_reservation_until_native_candidates_are_terminal`, `test_abort_cancels_and_confirms_every_post_baseline_candidate`, Phase 17.6 abort/restart tests and Phase 25.2 late-result/real-descendant tests cover locks, fences and process ownership. | **Implemented; current 58-test affected matrix passed.** |

## Correction findings F-01-F-11

| Finding | Disposition | Production path and validation evidence |
| --- | --- | --- |
| F-01 | **FIXED** | `sdk_successor_prompt_applies`, `authenticated_sdk_successor_launch`, `_base_bytes`, `_cursor_binding` and SDK ingestion now resolve the same effective prompt while scoping inherited/recovered prompt data to the matching turn kind and iteration. The initial/correction tick test authenticates the successful successor journal, later reviewer correction, repeated failure and exact new correction envelope. |
| F-02 | **FIXED** | `authenticate_sdk_successor_authority` now verifies durable ancestry/evidence independently of whether that exact turn is currently dispatchable; launch eligibility remains in `authenticated_sdk_successor_launch`. Budget, reviewer, capacity and downstream callers accept valid advanced successors without weakening frozen identity checks. The repeated initial/correction and mixed-provider sequence acceptance tests exercise these downstream states. |
| F-03 | **FIXED** | `AttemptService._cursor_binding` and reconciliation binding propagate the authenticated inherited `sdk_successor_recovery_key`; the production owner guard is exercised by launching a real follow-on reconciliation worker after a successor fails again. No completed reconciliation row is inserted by the test. |
| F-04 | **FIXED** | `_reconcile_existing_attempt` permits only an authenticated, non-cancelled SDK reconciliation effect to complete under an abort; other aborted-run attempts remain stale cleanup. The abort integration test uses normal ticks and attempt ingestion until native confirmation and reservation release, with zero prompt sends, staging or review. |
| F-05 | **FIXED** | `CursorWorkflowService._capacity_wait_is_correction` authenticates either the ordinary raw correction envelope or a correction successor's exact effective prompt, digest, iteration and recovery key before restoring `WaitingForCursorFixState`. The previous fix and test covered only an ordinary correction, whose `original_prompt_path` equals `latest_correction_envelope_path`; a recovered correction deliberately stores `prompts/cursor-sdk-recovery/effective.txt`, so that raw-path equality missed it and degraded to `CursorReadyState`. The extended initial/correction tick/CLI test now drives a manually recovered correction through structured capacity reconciliation, timer firing, dispatch and successful ingestion. Both invocations remain iteration 2, use the same authenticated effective prompt, carry the successor key, retain reviewer B/budgets, and contain the recovery note once. |
| F-06 | **FIXED** | Lost-envelope recovery now handles authenticated owned `MISSING`, `INACTIVE` and `FAILED` units. Retained units are explicitly terminated and the entire process tree must be proved absent before journal/native reconciliation; unavailable or incomplete evidence still holds. `test_failed_worker_without_result_stops_owned_descendants_before_native_replay` SIGKILLs the real worker with synthetic live bridge/tool descendants and exercises the normal launcher/guard/lock path. Existing Phase 25.2 real launcher/runtime death, persistence-failure and late-publication tests remain green. |
| F-07 | **FIXED** | A parsed nonzero native terminal lacking `failure_kind` becomes `cursor_sdk_terminal_failed`, is reconciled, and enters the existing bounded manual generic-failure policy. It is not treated as quota/capacity evidence. Both initial and correction generic failures are covered by the tick/CLI tests. |
| F-08 | **FIXED** | `decisive_ready_sdk_reconciliation` causally selects the relation preceding the decisive failed attempt/block, checks current sequence authority and rejects conflicting relations instead of requiring exactly one historical ready row. Both check and force callers use it. The correction capacity-then-generic-failure test, repeated-successor test and current mixed-provider leaf test cover historical rows and current sequence leaves. |
| F-09 | **FIXED** | The simulated v12 fixture drops the schema-18 reconciliation table before replaying migrations. Production migration validation was not relaxed. The named v12 regression and the affected schema 18/14/15/16/17 migration/rollback selection passed 7/7. |
| F-10 | **FIXED** | `SchedulerAbortService._reconcile_existing_abort` and `AttemptService._reconcile_aborted_attempt` call `ensure_aborted_sdk_turn_reconciliation` only after the owned process group is stopped. The helper authenticates immutable successor ancestry and its recovery key independently from current-turn eligibility; it uses the aborted checkpoint's prior state, iteration and exact correction envelope unless the original successor turn is still active. It then authenticates the identity artifact and sealed journal and persists one abort-marked native obligation before stale-attempt finalization may release resources. The previous correction covered an active original turn only, where successor ancestry and active prompt authority were identical, so it missed a successor that had advanced through reviewer findings to a later correction. `test_abort_later_successor_correction_uses_current_turn_authority` now drives initial failure, `cursor-retry`, successful implementation, exact reviewer B findings, synchronized correction send, process-tree stop, native abort reconciliation and idempotent release with three sends and no post-abort staging/review. The original unavailable-evidence regression remains green. |
| F-11 | **FIXED** | `cursor_attempt_runner._run_sdk_reconciliation` publishes each record at a source-and-reconciliation-attempt-specific immutable path. `_authenticate_reconciliation_record` accepts that exact worker path (plus the staged legacy path for existing fixtures), while `authenticated_published_reconciliation_result` first checks the interrupted worker's own path. If absent, it separately authenticates the ledger-bound historical record, its ingested worker attempt, dispatch and recovery key, preserves it, and resumes the pending native obligation without treating history as the current result. `AttemptService._reconcile_existing_attempt` still requires whole-tree termination and `CursorSdkReconciliationService._resume_interrupted_reconciliation` grants no prompt authority. The previous correction covered interruption before any record existed, so the shared path was empty. `test_killed_abort_reconciliation_preserves_historical_record_and_resumes` now publishes a blocked native record, abort-reopens it, kills the synchronized next worker, proves the unavailable-evidence hold, preserves the old bytes/digest, and converges through a third reconciliation attempt to release with one prompt send. |

No finding is disputed or blocked. The confirmed fixes preserve exact reviewer B,
distinct implementer/reviewer sessions, conversation owner/store, frozen model
settings, prompt and artifact hashes, bounded recovery policy, reservations,
copy-once short names, budgets, source immutability and current Git authority.

## Durable evidence and safety boundaries

- SQLite schema 18 adds `scheduler_cursor_sdk_reconciliations`; published
  intent/record and successor intent/record JSON schemas authenticate source and
  reconciliation attempts, effect ownership, owner/key/agent, prompt/journal
  digests, inventory/candidates and terminal native outcomes.
- Reconciliation workers use a fresh SDK client with the frozen workspace,
  store, credential reference and exact agent/run. Native observation or cancel
  never authorizes a prompt; a durable automatic-policy transition or explicit
  successor publication is still required.
- Reviewer finding evidence is persisted with `WaitingForCursorFixEnteredEvent`,
  so correction successors retain the exact review-result path/digest rather
  than reconstructing it.
- Current timeout counts/delay, role/session capacity metadata, generic-failure
  policy, staging behavior, reviewer identity and Phase 28 presentation metadata
  remain frozen. Dashboard classification uses stable scheduler meanings.
- Missing exact create identity and zero/multiple post-baseline candidates remain
  explicit safe holds, not incomplete speculative fallbacks.

## Exact executed validation

The current advanced-successor/historical-record correction nodes were run
directly after both production fixes:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_3_sdk_recovery.py::test_abort_later_successor_correction_uses_current_turn_authority tests/integration/test_phase25_3_sdk_recovery.py::test_killed_abort_reconciliation_preserves_historical_record_and_resumes
```

Result: **2 passed in 13.67s, exit 0**.

After adding explicit reviewer B, conversation owner, review-budget and
copy-once short-name assertions to the advanced-successor case, that exact node
was rerun: **1 passed in 8.74s, exit 0**.

The complete currently affected SDK recovery, abort/restart and worker-launch
set was then rerun:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_2_sdk_workflow.py tests/unit/scheduler/test_phase25_2_sdk_worker.py tests/integration/test_phase17_6_abort_lifecycle.py tests/integration/test_phase17_6_restart_reconcile.py
```

Result: **58 passed in 110.21s (0:01:50), exit 0**.

The following complete focused matrix is retained as the immediately preceding
Phase 25.3 correction evidence. It predates only the two current regressions and
their scoped production fixes; the changed production paths are covered by the
58-test current run above.

It was run exactly as follows:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_3_sdk_recovery.py tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase23_1_cursor_recovery_check.py tests/integration/test_phase23_2_initial_recovery.py tests/integration/test_phase23_3_correction_recovery.py tests/integration/test_phase23_4_sequence_recovery.py tests/integration/test_phase17_6_abort_lifecycle.py tests/integration/test_phase17_6_restart_reconcile.py tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py tests/integration/test_phase27_7_recovery_acceptance.py tests/integration/test_phase28_short_names.py
```

Result: **220 passed in 976.50s (0:16:16), exit 0**. This complete focused
matrix was executed after the preceding F-05/F-10/F-11 correction round. Ruff
formatting afterward was mechanical. A final audit in that round then tightened
the restart-side abort call to require an explicit owned stop for retained
inactive/failed units; the complete directly affected 56-test set was rerun
after that guard tightening as recorded below.

The directly affected SDK recovery, abort/restart and worker-launch set was
also run after the final restart-path fix:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_2_sdk_workflow.py tests/unit/scheduler/test_phase25_2_sdk_worker.py tests/integration/test_phase17_6_abort_lifecycle.py tests/integration/test_phase17_6_restart_reconcile.py
```

Result: **56 passed in 98.39s (0:01:38), exit 0**.

After mechanical Ruff formatting, the exact F-05/F-10/F-11 production-path
regressions were rerun:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_3_sdk_recovery.py::test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift tests/integration/test_phase25_3_sdk_recovery.py::test_abort_active_sdk_send_persists_native_obligation_until_idempotent_release tests/integration/test_phase25_3_sdk_recovery.py::test_killed_reconciliation_worker_resumes_obligation_without_prompt_redispatch
```

Result: **3 passed in 20.35s, exit 0**.

Phase 25.2's complete integration workflow was rerun against the corrected fake
public SDK and completed production sources:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_2_sdk_workflow.py -x
```

Result: **13 passed in 37.97s, exit 0**.

The directly affected correction-event and v12 migration files were rerun:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase18_corrections.py tests/unit/scheduler/test_phase23_4_sequence_recovery.py
```

Result: **14 passed in 2.65s, exit 0**.

The explicit affected migration/rollback selection was run:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase23_4_sequence_recovery.py::test_schema_migration_reopens_v12_without_rewriting_lineage tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_adds_sdk_reconciliation_ledger tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_schema18_migration_fault_rolls_back_table_and_audit_row tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py::test_v14_migration_adds_relation_without_rewriting_existing_runs tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py::test_v15_migration_adds_sequence_replacement_without_rewriting_runs tests/integration/test_phase28_short_names.py::test_schema16_read_fallback_and_v17_migration_preserve_authority tests/integration/test_phase28_short_names.py::test_schema17_migration_fault_rolls_back_all_presentation_tables
```

Result: **7 passed in 4.53s, exit 0**.

Quality and documentation commands:

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m compileall -q src/ai_dev_loop tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_3_sdk_recovery.py
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Results: Ruff format **405 files already formatted**; Ruff check **All checks
passed**; MyPy **Success: no issues found in 211 source files**; compileall
**exit 0**; strict MkDocs **exit 0** with only the existing Material 2.0 and
unlisted-page informational notices; full working-tree diff check **exit 0**.

## Failed and superseded validation

Failures below were diagnostic runs against intermediate correction states. No
failed result is presented as passing; the two current failed nodes were rerun
directly and in the current 58-test affected matrix.

| Executed scope | Exact result | Confirmed cause and disposition |
| --- | --- | --- |
| Abort of a later correction on an advanced SDK successor | **1 failed in 7.56s** | The owned process tree terminated, but no native obligation was created because immutable initial-successor authority was incorrectly required to match the later iteration-2 correction prompt. F-10 now separates ancestry authentication from current-turn checkpoint eligibility; the exact node passed in the 2-node and 58-test reruns. |
| Killed abort-reconciliation worker with a prior published record | **1 failed in 38.08s** | The shared record path still held the prior worker's authenticated record, which conflicted with the interrupted worker and left the relation pending indefinitely. F-11 now publishes worker-specific immutable records and authenticates historical evidence separately; the exact node passed in the 2-node and 58-test reruns. |
| First historical-record fixture attempts | Combined run **2 failed in 8.42s**; then **1 failed in 5.27s**, **1 failed in 5.71s**, and **1 failed in 26.67s** | These diagnostics successively corrected an overlong test short name, selected a genuine published native record instead of a worker-level failure, and synchronized on a known native observation rather than an ambiguous-missing inventory. None is counted as production validation; the completed reproduction above is the evidence for F-11. |
| Recovered correction capacity continuation | **1 failed in 13.61s** | The timer recognized only an ordinary raw correction envelope and restored `CursorReadyState`, so the next invocation selected `prompts/cursor-initial.txt` instead of the authenticated recovery effective prompt. F-05 adds durable successor-turn authentication; the extended node passed in the final matrix and post-format rerun. |
| Abort during an active SDK send before any reconciliation relation existed | **1 failed in 4.06s** | Production abort returned `termination_pending=False` and released ownership without persisting the native obligation. F-10 now authenticates and persists the obligation after process-tree proof in both direct abort and restart/tick paths; the regression passed in the final matrix and post-format rerun. |
| First killed-reconciliation-worker regression draft | **1 failed in 6.31s** | The initial diagnostic fixture constructed `ObserveResult` incorrectly before reaching the production assertion. The fixture was corrected; code inspection confirmed the unhandled reconciliation effect, and the synchronized production-path regression then demonstrated the unavailable-evidence hold, same-relation reschedule and eventual convergence for F-11. |
| First post-correction full focused-matrix invocation | **uncompleted; no terminal result** | The invocation was interrupted during the correction turn, so it is not counted as validation. The identical command was rerun to completion with **220 passed in 976.50s**. |
| Four-file initial correction selection | **9 passed, 1 failed in 5.10s** | Decisive-reconciliation lookup assumed `BlockedState.cursor`; fixed to use authenticated causal event/source evidence. |
| Repeated initial/correction recovery node, across development reruns | Failed for fake public-status compatibility, frozen-clock ordering, correction attribute selection, latest-journal selection, later-turn authority, missing persisted reviewer evidence and one test digest | Each confirmed production/fake defect was corrected; final node **1 passed in 11.38s**. |
| Correction capacity node, across development reruns | Two failures from invalid test CLI use and direct result-column lookup | Test now exercises the supported CLI/service path; final node **1 passed in 11.07s**. |
| Abort reconciliation node, across development reruns | Failures from an outdated expectation, absent abort-state source checkpoint lookup and test result-column lookup | Source authority lookup and assertions corrected; final node **1 passed in 5.12s**. |
| Real worker/descendant selection | Initially **1 passed in 5.37s**; broader run then **1 failed, 12 passed in 28.30s** because the test raced the synthetic tool marker | Deterministic readiness barrier added; the final matrix passed. |
| Mixed-provider sequence node, across development reruns | Failures from premature sequence reconciliation, a test attribute mismatch and expected lineage kind | Generic SDK failure now blocks the current leaf through the normal recovery path; final node **1 passed in 9.59s**. |
| Broader six-file compatibility selection | **1 failed, 21 passed in 55.62s**; narrowed reproduction **1 failed, 6 passed in 23.67s** | The enhanced fake exposed a terminal public status to an old stream-failure fixture; public fake semantics were scoped to preserve the intended unsupported-evidence case. Phase 25.2 then passed 13/13. |
| Pre-F-05/F-10/F-11 prescribed matrix | **218 passed in 963.67s** | Superseded by the expanded final **220/220** run. The earlier pre-F-01-F-09 matrix was **213 passed in 920.17s** and is likewise superseded. |
| Archived no-model POC replay | Failed before collection with `ModuleNotFoundError: httpx` | No dependency was installed. Preserved README/report/manifest and recorded 27/27 offline evidence remain prerequisite history, not fresh validation. |

## Unexecuted work and safe next actions

- Repository-wide pytest is deliberately **unexecuted**; Phase 25.5 owns that
  integration gate. The focused results do not imply repository-wide coverage.
- The prior 15-file complete focused matrix was not rerun wholesale after the
  two current scoped fixes. Its last completed result remains 220/220; every
  changed production path and both new regressions are included in the current
  58/58 affected run.
- Real provider/model calls, live credentials/state, native-store repair, live
  user-systemd/timer operations, cleanup, installation and external/destructive
  Git operations are **unexecuted by design**.
- `git diff --cached --check` was executed successfully on the existing
  pre-normalization staged subset, but is **unexecuted on the final correction
  snapshot** because the orchestrator has not yet performed its promised
  `git add -A`. `git diff --check` validated the complete working-tree patch.
  No attempt was made to bypass the environment's read-only `.git` mount.
- No mandatory R-01-R-04 or F-01-F-11 implementation work is known unfinished.
  Unsupported missing create identity and ambiguous candidate sets remain the
  contract's intentional held outcomes; widening authority requires a new plan.
- The safe next action is scheduler-owned `git add -A`, followed by staged-only
  review by the independent Codex reviewer. If accepted, only the scheduler may
  create the intermediate checkpoint. If rejected, corrections remain with this
  implementer identity and the affected focused tests must be rerun.
- `.agents/skills/create-cursor-plan`,
  `.agents/skills/ai-dev-loop-docs-acceptance-governance` and
  `.agents/skills/review-staged-ai-dev-loop-execution` are unchanged. No
  `.cursor/skills/` directory was created.
