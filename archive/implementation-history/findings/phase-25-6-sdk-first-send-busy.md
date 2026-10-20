# Phase 25.6 SDK first-send busy findings

Status: **READY_FOR_REVIEW**. This is implementer evidence, not scheduler or
reviewer acceptance. It authorizes no commit, checkpoint, installation, live
provider operation, push, PR or merge. Final changes remain for the scheduler to
stage as one snapshot and for the separate Codex reviewer to inspect under the
preserved staged-only review contract.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop-sdk-busy-fix`,
branch `codex/fix-cursor-sdk-first-send-busy`, HEAD
`16ff58676efa1f7992c617284a330a5cc9a78c88` (the plan baseline; this slice is
uncommitted). The plan's `OpenQuestions` is None. Prerequisites were rechecked
against the installed public package `cursor-sdk` 1.0.37
(`Version: 1.0.37` in the `uv tool` METADATA). Public
`CursorClient.launch_bridge`, `create_agent`, `resume_agent`, `list_runs`,
`get_run`, `Run.cancel`, `Run.events`, `Run.wait`, `Run.observe`,
`Run.conversation` and `Agent.list_messages` exist. `RunSnapshot` still has no
`startedAt` or `turnNumber`. `LocalSendOptions.force` exists and is not used.
`_normal_run_status` maps wire `creating` to public `running`; an unknown wire
status, including raw `queued`, becomes public `error`. No new OpenQuestion
arose. The permanently queued case that surfaces only as public `error` is a
reported limitation, not a reason to cancel without the approved proof.

All automated runs used fake SDK/provider responses, fake Codex responses, fake
process backends and temporary repositories with test-isolated HOME/XDG state.
No real model, live credential, live scheduler ledger, workstation service or
timer, installation, cleanup, external Git action or destructive Git action was
used.

## C-01–C-06 contract evidence

| ID | Production callers and implemented contract | Tests | Result |
| --- | --- | --- | --- |
| C-01 | `cursor_attempt_runner._run_sdk_create_operation` records the public inventory and closes the creation bridge. The next owned turn resumes the same agent. `_dispatch_after_creation_readiness` cancels one exclusive no-work creation occupant, confirms it terminal, then sends the original prompt once. The accepted native run is the post-baseline prompt run. The creation run is not implementation output and does not enter staging or review. | `tests/integration/test_phase25_6_sdk_busy.py::test_cold_first_send_resolves_owned_creation_occupant_once` (queued and public `creating`→`running`). The durable fake lives in `tests/integration/test_phase25_2_sdk_workflow.py`. | **Fixed.** Red before the production path: `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/integration/test_phase25_6_sdk_busy.py --tb=short` → **1 failed in 5.44s**, `cursor_sdk_ambiguous_missing`, `assert 'blocked' == 'completed'`. Green through `TickService` and the fake SDK: the same file later **12 passed in 70.13s**, including one send, one cancel of `run-019abc00-1111-4222-8333-777777777777`, zero conversation calls, and `run_completed`. |
| C-02 | `scheduler/domain/cursor_sdk_readiness.py` `classify_creation_readiness` allows at most 3 observations and 10 seconds (`MAX_CREATION_READINESS_OBSERVATIONS`, `MAX_CREATION_READINESS_SECONDS`) on the injected `_readiness_now` / `_readiness_sleep` clock, inside the existing attempt deadline. `_bound_readiness_call` applies that remainder around each readiness SDK call and restores any sooner operation itimer. The runner checks the injected clock after observation, cancellation and confirmation, and before cancel, the first send and a rejection retry. Eligible statuses are public `queued` and `running` only when the authenticated inventory has one run, `authorized_send_count == 0`, both message counts are 0, and the fresh observation is that same exclusive run. Terminal public statuses (`finished`, `cancelled`, `canceled`, `expired`, `error`, `failed`) send with no cancel. Unknown status, foreign or extra runs, public history, a prior authorized send, non-integer counts, duplicate IDs and a nonprogressing cursor hold. `adapter.list_agent_messages` reads `Agent.list_messages` and does not call `conversation()`. Hold kind `cursor_sdk_creation_readiness_hold` is in `CURSOR_SDK_RESERVATION_HOLD_BLOCK_KINDS`; `CursorWorkflowService` keeps the reservation. It is not `cursor-retry --force` eligible. | `tests/unit/scheduler/test_phase25_6_sdk_busy.py` classification, queued/running cancel-once, terminal `error` without cancel, unknown/nonexclusive hold, public-history hold, duplicate/stuck inventory, plus `test_slow_observation_holds_before_cancellation`, `test_slow_cancellation_holds_before_the_prompt`, `test_slow_confirmation_holds_before_the_prompt`, `test_slow_rejection_does_not_retry_the_prompt` and `test_readiness_budget_refuses_a_call_and_restores_the_outer_deadline`. `tests/unit/test_phase25_1_sdk_adapter.py::test_typed_and_structured_busy_are_distinct_from_message_text`. Integration holds in `test_phase25_6_sdk_busy.py`. | **Implemented.** Missing proof blocks. A public `error` mapped from an unknown wire `queued` is not cancelled. The 10-second limit was not increased. |
| C-03 | `_definitive_send_rejection` is typed `AgentBusyError` or structured `InternalServerError` whose code ends in `agent_busy` (`evidence_class` `typed` or `structured`). Message-only "already has active run", network, server, timeout and invalid-response failures stay `cursor_sdk_send_uncertain` and do not resend. One resend requires that rejection, zero post-baseline prompt runs, the owned blocker terminal, `message_count == 0`, and the next observation ordinal ≤ 3, on the same attempt and original deadline, with a second journal `send_intent`. A unique new post-baseline run is reconciled by the existing path with no second send. Zero or multiple prompt candidates stay held. The creation run is never a candidate and never `native_run_id`. `SendOptions` still sets only `model`. | `test_only_definitive_rejection_can_resend_once`, `test_an_accepted_prompt_is_not_sent_again`, `test_multiple_post_send_runs_stay_held`, `test_a_second_rejection_does_not_send_a_third_time`. Integration: typed and structured busy complete with `send_attempts == 2` and one prompt send; message busy and typed-busy-twice stay `cursor_sdk_ambiguous_missing` with zero sends; `accept-network` plus a unique discovered run completes with `send_attempts == 1` and zero sends. | **Implemented.** The cold incident path is pre-send readiness, not post-error inference. Historical ambiguous recovery is unchanged. |
| C-04 | Create journals are schema 2 (`CREATE_SEQUENCE_V2`: `create_intent`, `agent_bound`, `creation_inventory`, `terminal`), including an empty inventory. An authenticated empty inventory keeps the v1 send sequence. A non-empty inventory uses schema 2 send sequences in `cursor_sdk_journal.py` (`SEND_SUCCESS_SEQUENCES_V2`, `SEND_UNCERTAIN_SEQUENCES_V2`), with contiguous unique dispatch ordinals. Every `_SEALABLE_SEND_PREFIXES_V2` prefix plus `terminal` authenticates. `CursorSdkReconciliationRecordV1` and `record-v1.json` keep their original outcome meanings. A proved-unsent prompt is `CursorSdkReconciliationRecordV2` at `record-v2.json` with event `cursor_sdk_reconciled_v2`; `prompt_unsent` cannot carry a native run or candidates. V1 candidate sets remain post-baseline prompt runs. A sealed v2 journal with no `send_intent` and no post-baseline candidate reconciles `prompt_unsent`; ingest schedules that same turn once. A journal that already contains `send_intent` stays on the existing uncertain/adoption path. An unexpected post-baseline run when no prompt was authorized is `native_unavailable` and is not adopted. `cancellation_requested` may be true with an empty candidate list when abort cancels the creation occupant. Abort is persisted before native cancellation and never becomes `prompt_unsent`. `_confirm_creation_blocker` cancels once, or only observes when the run is already terminal. `CursorSdkWorkerInterrupt` is raised after the journal append for `creation_blocker_cancel_intent`, `creation_blocker_cancel_confirmed`, `send_intent` and `native_run_bound`. The fake backend does not invent a Cursor result envelope when the worker exits without one, so the production lost-envelope sealer runs; it clears the one-shot interrupt env after that traceback. Seal appends schema 2 `terminal` / `cursor_sdk_worker_lost` for a sealable prefix and does not append when the sequence is already complete. A tampered digest or bool counter is rejected. An absent create journal is not eligibility. | `test_every_supported_v2_prefix_seals_for_recovery`, `test_restart_after_interrupt_confirms_without_another_prompt`, `test_abort_cancels_only_the_owned_creation_blocker`, `test_unconfirmed_abort_keeps_the_hold`, `test_v2_journal_rejects_null_bool_and_tampered_bindings`, `test_absent_create_journal_does_not_infer_creation_readiness`. Integration: `test_resolved_pre_send_interruption_sends_one_initial_prompt` and `test_abort_after_pre_send_interruption_sends_nothing` through `TickService`. Phase 25.3 recovery/abort integration tests still drive the real tick and CLI callers. | **Implemented.** A proved-unsent pre-send interruption dispatches the original prompt once. Abort sends none and releases the reservation only after reconciliation is ready. Unconfirmed abort stays `native_unavailable`. |
| C-05 | The same agent and independent reviewer B still complete an initial turn, a correction and a current sequence leaf. `AttemptService._prior_authorized_prompt_evidence` resolves `state.cursor.sdk.latest_journal_path` and `latest_journal_sha256` against the ledger attempt, dispatch and invocation prompt. It passes that path, digest, attempt, dispatch and prompt to the worker. `_prior_prompt_authorized` accepts only that pinned proof: schema 1 with `send_intent`, or schema 2 with `send_intent` plus `readiness_clear` or `creation_blocker_cancel_confirmed`. A self-hash, a directory scan, a create-effect journal, a missing file on a successor, or an orphan journal is not authority. A digest or binding mismatch raises. The immutable creation `authorized_send_count` is not that proof. Creation cancellation does not increment reviews. A successor that carries `cursor/sdk-identity.json` without the source create journal takes the historical send path: `_load_creation_inventory` returns None when that journal file is absent; a present journal must still authenticate; an identity digest mismatch still raises. `AttemptService._cursor_binding` passes `identity_artifact_path` and `identity_artifact_sha256` on the turn. The sequence credential fixture chmods both `config/ai_dev_loop` and `credentials` to `0700`; production path validation is unchanged. | `tests/integration/test_phase25_2_sdk_workflow.py`, `tests/integration/test_phase25_3_sdk_recovery.py`, `tests/integration/test_phase25_5_sdk_acceptance.py`, `test_creation_readiness_does_not_block_the_same_agent_correction`, `test_authorized_prompt_lets_the_same_agent_correct_without_another_cancel`, and `test_sequence_leaf_passes_creation_readiness_before_the_next_phase` under umask `022` and `077` (`AwaitingFinalizationSequenceState`, one checkpoint commit, one prompt send, one creation cancel). | **Proved on fakes.** The first focused run blocked five recovery/acceptance turns because the missing source journal raised before send (`cursor_sdk_preflight_failed`, summary "Cursor SDK in-unit preflight failed before send intent"). After the absent-journal guard, those callers complete. |
| C-06 | CLI, configuration and state-artifact docs describe the 3-observation / 10-second policy, the one eligible cancel, the hold, the single definitive resend, v1 journals keeping their old authority, later turns skipping a resolved gate only with the ledger-pinned journal digest, a sealed never-sent journal dispatching once through reconciliation record v2, and the 10-second budget bounding readiness reads, cancellation, confirmation and the check before a new send. An authorized send keeps the frozen turn deadline. `docs/operacion/incidentes/index.md` adds INC-2026-10-19-001 as open, patch in the working tree, no live acceptance. The incident file has a dated addendum. The original incident body and the RCA were not rewritten. | Strict MkDocs build. Existing v1 journal authentication remains in the phase 25.2/25.3 contract tests. | **Documented.** The incident is not closed. |

No contract is disputed or blocked inside the approved cancellation boundary.
`cursor_sdk_creation_readiness_hold` is the safe action when the no-work proof
is missing.

## Correction turn 2 — F-01–F-04

Manual correction on the same Cursor agent. The staged Phase 25.6 slice was
preserved. These four findings were confirmed against that slice and fixed.
Nothing was staged, unstaged, committed, pushed or rewritten in Git.

| ID | Production path | Red evidence | Green evidence | Closure |
| --- | --- | --- | --- | --- |
| F-01 | `cursor_attempt_runner._prior_prompt_authorized` and the turn branch that used to call `_dispatch_after_creation_readiness` whenever the creation inventory was nonempty. A later turn now skips that gate only after an authenticated prior `send_intent` whose readiness was resolved. | Independent `TickService` repro `/tmp/phase256-review-e7apusea/test_review_regressions.py` with `FAKE_CODEX_REVIEW_SEQUENCE=findings,no_findings`: **2 failed in 15.81s** (this case and F-02). Cold tests used `no_findings`; ordinary corrections had no creation occupant. | `test_authorized_prompt_lets_the_same_agent_correct_without_another_cancel` and `test_creation_readiness_does_not_block_the_same_agent_correction`: one creation cancel, two distinct prompts, second native id `run-019abc00-1111-4222-8333-555555555556`, same reviewer, `reviews_completed == 2`. | **Fixed.** |
| F-02 | `seal_interrupted_sdk_send_journal` / `authenticate_sdk_recovery_journal` now accept every supported v2 prefix plus `terminal`. `_run_sdk_reconciliation_operation` returns `prompt_unsent` when the journal has no `send_intent` and no post-baseline candidate. `CursorSdkReconciliationService.ingest` schedules `cursor.run_turn` for that outcome. `FakeAgentProcessBackend` leaves a missing Cursor envelope missing so `AttemptService` seals it, and drops `AI_DEV_LOOP_SDK_INTERRUPT_AFTER` after `CursorSdkWorkerInterrupt`. The record stays `CursorSdkReconciliationRecordV1` with the new outcome; the published JSON schema matches `model_json_schema()`. | The same repro with `AI_DEV_LOOP_SDK_INTERRUPT_AFTER=creation_blocker_cancel_confirmed` blocked `outcome_evidence_invalid`. Direct unit tests previously required `ambiguous_missing` for cancel-confirmed prefixes. | `test_every_supported_v2_prefix_seals_for_recovery` (all 15 sealable prefixes). `test_restart_after_interrupt_confirms_without_another_prompt`: cancel intent and cancel confirmed are `prompt_unsent`; `send_intent` stays `ambiguous_missing`; `native_run_bound` adopts the prompt run. `test_resolved_pre_send_interruption_sends_one_initial_prompt`: one send, one creation cancel, `completed`. `test_abort_after_pre_send_interruption_sends_nothing`: zero sends, `aborted`, reservation released, reconciliation `ready`, no staging or Codex attempts. | **Fixed.** |
| F-03 | `_bound_readiness_call` and `_readiness_expired` in `cursor_attempt_runner.py`. Observation, `get_run`, `cancel` and both send ordinals are bounded. Expiry before a side effect journals `cursor_sdk_creation_readiness_hold` and does not perform it. The outer operation itimer is restored from elapsed real time. The 3-observation / 10-second limits are unchanged. | A deterministic observation that advanced the injected clock by 12 seconds still reached `adapter.cancel` before this change; only confirmation noticed expiry. | `test_slow_observation_holds_before_cancellation` (zero cancels, zero sends), `test_slow_cancellation_holds_before_the_prompt` and `test_slow_confirmation_holds_before_the_prompt` (one cancel, zero sends), `test_slow_rejection_does_not_retry_the_prompt` (one send), `test_readiness_budget_refuses_a_call_and_restores_the_outer_deadline`. No real sleep. | **Fixed.** |
| F-04 | Test-only. `test_sequence_leaf_passes_creation_readiness_before_the_next_phase` now chmods `config/ai_dev_loop` and `credentials` to `0700` after `mkdir`, and runs under umask `022` and `077`. `paths.read_cursor_credential` / `resolve_cursor_credential_path` were not weakened. | Independent focused gate **1 failed, 121 passed in 245.32s**. The isolated sequence test failed under umask `022` in **10.61s** and passed under `077` in **9.19s**, with `invalid_local_path` before creation. | The same test is parametrized on both umasks and passed inside the integration file (**17 passed in 109.04s**, including the schema equality test) and inside the focused gate below. | **Fixed.** |

`prompt_unsent` was added to the existing reconciliation record, event and
published schema rather than a second record version. Existing outcome meanings
are unchanged. No new OpenQuestion. The public-`error` mapping of an unknown
wire `queued` remains the reported limitation above.

Turn 2 marked F-02 and F-03 fixed. Review 02 showed both closures were
incomplete, and the F-01 proof was not durable authority. That is F-05 below.
The turn-2 paragraph above is the incomplete claim, not the current record.

## Recovery turn 4 — F-01–F-05

Manual recovery on the same Cursor agent after the operator cancelled turn 3
(`run-5d7dbfa1-5a27-4b56-8e2e-7076a25f62d4`). The staged Phase 25.6 slice was
preserved. F-01 and F-04 stay fixed for their original failures. F-02, F-03 and
F-05 were confirmed and fixed. Nothing was staged or committed.

| ID | Why the previous correction was incomplete | Production callers | Closure |
| --- | --- | --- | --- |
| F-01 | The correction no longer re-enters creation readiness, but turn 2 proved that by scanning journals and trusting a fresh hash. The behavioral fix stays. The proof is F-05. | `AttemptService._cursor_binding` now supplies the pinned journal before launch. `test_creation_readiness_does_not_block_the_same_agent_correction` still completes with two distinct prompts, one creation cancel, native id `run-019abc00-1111-4222-8333-555555555556`, the same reviewer and `reviews_completed == 2`. The correction invocation evidence carries `prior_authorized_journal_sha256` equal to the first turn's journal digest. | **Fixed.** Original failure remains closed. |
| F-02 | Crash sealing and the one-send restart worked, but `prompt_unsent` was added to `CursorSdkReconciliationRecordV1`, `record-v1.json` and `CursorSdkReconciledEvent`. The writer emitted schema version 1. The plan requires a new model, schema, path and explicit version dispatch, and forbids changing v1 meanings. | `_run_sdk_reconciliation_operation` writes `CursorSdkReconciliationRecordV2` at `record-v2.json` only for `prompt_unsent`. Every other outcome stays v1 at `record-v1.json`. `CursorSdkReconciliationService` authenticates by `schema_version`, appends `cursor_sdk_reconciled_v2` for the unsent decision, and still schedules `cursor.run_turn`. Abort stays `abort_reconciled` on v1. No ledger migration and no rewrite of historical bytes. | Version split **fixed**. The lost-envelope reader still opened; closed in turn 5. |
| F-03 | The slow observation, cancel and confirmation holds were real, but `send_once` then wrapped `adapter.send` in `_bound_readiness_call`. SDK `Agent.send` waits for the first stream event before returning the run, so the readiness remainder became a second timeout after the prompt was accepted. | `send_once` still checks `_readiness_expired` before journaling `send_intent`. `adapter.send` then runs under the frozen `_sdk_operation_deadline` only. List, get, cancel, confirmation and the pre-send check stay inside the ten-second budget. The budget was not enlarged and the outer timer was not disabled. | **Fixed.** |
| F-04 | No new failure. The sequence credential fixture still chmods both directories to `0700` and is parametrized on umask `022` and `077`. Path validation was not weakened. | `test_sequence_leaf_passes_creation_readiness_before_the_next_phase` remains in the integration file and passed inside the gate below. | **Fixed.** |
| F-05 | `_prior_prompt_authorized` computed `sha256_file` and passed that same value to `authenticate_*`, while binding only owner, key and agent. Any syntactically valid journal in the attempt directory could authorize the next turn. Rewriting `prompt_sha256` left the helper returning true. | `AttemptService._prior_authorized_prompt_evidence` reads `latest_journal_path` / `latest_journal_sha256`, loads that attempt and its `cursor.run_turn` effect, and verifies the invocation. A create-effect journal or a missing successor file supplies no anchor. A digest or binding mismatch raises `ValidationError` before launch. The worker raises on a changed digest, a wrong dispatch, an incomplete anchor or a path that is not the named attempt journal. An orphan file is ignored. | **Fixed.** |

Red evidence for this turn, on the reviewed snapshot before these edits:
`TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=short --confcutdir=/tmp/phase256-review-round02-wbo6w15l -p tests.conftest /tmp/phase256-review-round02-wbo6w15l/test_review_round02.py`
→ **3 failed in 1.07s**. Those three cases are now repository tests with their own expectations: changed digest, authorized send after the readiness budget, and `prompt_unsent` at schema version 2.

Maintained coverage added here:

- `test_changed_prior_journal_digest_rejects_the_correction`
- `test_orphan_journal_and_missing_anchor_do_not_authorize_another_send`
- `test_wrong_prior_binding_rejects_the_source_journal`
- `test_incomplete_prior_anchor_is_rejected`
- `test_authorized_send_keeps_the_frozen_turn_deadline` (readiness budget 0.05s, send accepted then delayed 0.2s, turn deadline 60s, one send, completion signal)
- restart/abort unit and integration assertions that `prompt_unsent` is record v2 / `cursor_sdk_reconciled_v2` and abort stays record v1 / `cursor_sdk_reconciled`
- `test_phase25_3_persisted_models_match_published_json_schemas` includes record v2

No new OpenQuestion. The public-`error` mapping of an unknown wire `queued` remains the reported limitation above.

Review 03 kept F-01, F-03, F-04 and F-05 closed. The F-02 row above closed the v1 mutation. It did not close recovery after the worker published `record-v2.json` and died before returning the result envelope.

## Recovery turn 5 — F-02 lost envelope

Same agent, unstaged correction. The v2 writer, normal ingestion, `cursor_sdk_reconciled_v2`, and historical v1 contracts from turn 4 stay. What that correction missed: `authenticated_published_reconciliation_result` built only `record-v1.json` for the current reconciliation attempt. A valid `record-v2.json` at that exact attempt made the function return `None`. `AttemptService._reconcile_existing_attempt` then synthesized `cursor_sdk_reconciliation_worker_lost` and scheduled another SDK reconciliation. A transient bridge-open failure on that redundant observation blocked `cursor_sdk_native_unavailable` even though the prompt had not been sent and the published proof already existed.

`_current_attempt_published_record` now looks only at the canonical v1 and v2 paths of that source and reconciliation attempt. One file is authenticated through `_authenticate_reconciliation_record`, which keeps path/version correspondence, the source journal digest, and the attempt/dispatch identity. Both files are a conflict and raise. A tampered file fails authentication and raises. Neither file keeps the previous historical prior-attempt fallback, which still returns `None` after authenticating the older v1 record and does not treat it as this worker's result. No other attempt directory is scanned, and a valid current result does not require another native observation. Ownership, termination and abort fences are unchanged. v1 bytes are unchanged. No ledger migration.

Red, on the reviewed snapshot before this edit:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=short \
  --confcutdir=/tmp/phase256-review03-external -p tests.conftest \
  /tmp/phase256-review03-external/test_review03.py
```

→ **2 failed, 1 passed in 8.82s**. The passing test is the application prior-anchor digest check. The two failures are the published v2 reader and the envelope-loss plus transient-outage scheduler path.

Green, same external file against this correction: **3 passed in 11.13s**.

Maintained coverage, without the reviewer's diagnostic path monkeypatch:

- `test_published_v2_record_is_recovered_when_its_envelope_is_lost`
- `test_published_v2_conflicts_with_a_same_attempt_v1_record`
- `test_tampered_published_v2_record_is_not_recovered`
- `test_historical_v1_record_is_not_the_current_attempt_result`
- `test_lost_published_v2_envelope_does_not_start_another_reconciliation` (interrupt at `creation_blocker_cancel_confirmed`, drop only the v2 result envelope, arm a bridge-open failure for a redundant reconciliation; one reconciliation, one send, `completed`)
- `test_killed_abort_reconciliation_preserves_historical_record_and_resumes` still passes (**1 passed in 6.33s**)

Focused checks this turn:

```text
uv run python -m pytest -q \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
# 157 passed in 262.70s
uv run python -m ruff check \
  src/ai_dev_loop/scheduler/application/cursor_sdk_reconciliation.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_6_sdk_busy.py
# All checks passed
uv run python -m ruff format --check <the same three paths>
# passed after formatting the unit test
uv run python -m mypy src
# Success: no issues found in 216 source files
uv run python -m mkdocs build --strict
# exit 0; built in 0.76 seconds
```

F-02 is **fixed** for the lost-envelope consumer. F-01, F-03, F-04 and F-05 stay fixed. No new OpenQuestion. Nothing was staged or committed. The repository-wide suite, package build, install and live SDK smoke remain unexecuted. INC-2026-10-19-001 stays open.

## Limitations

- A wait cannot clear an indefinitely queued native run. If the exclusive
  no-work proof is unavailable, the run stays held with the repository
  reservation.
- SDK 1.0.37 maps an unknown wire status, including raw `queued`, to public
  `error`. Public `error` is terminal and is not cancelled. The observed
  incident's public status was `running`, which this slice does cancel when the
  rest of the proof holds. A creation occupant that appears only as public
  `error` is not automatically fixed.
- Empty text, a missing timestamp, `duration_ms == 0` and null usage do not
  prove that no work ran. `GetRunConversation` is not a readiness gate.
- Historical agents without an authenticated creation inventory, including a
  successor whose identity artifact points at a create journal that is not in
  that run root, are not inferred eligible. Already ambiguous runs and the
  aborted sequence `ai-dev-loop-seq-97f6aab2f8da` stay immutable.
- `SendOptions.local.force` is not set. An uncertain prompt is not resent.
- Fake proof is not live acceptance. INC-2026-10-19-001 stays open.

## Test-clock note

`test_exact_source_sdk_config_runs_initial_correction_and_read_only_views`
freezes tick time at `2026-10-18T12:00Z`, so the first execution launch is that
instant, while `submit_run` records wall-clock `submitted_at`. The dashboard
cohort uses the launch instant. After `2026-10-19T12:00Z`, a window of
`submitted_at ± 1 day` excluded that launch and the assertion saw an empty
cohort on a completed run. The test now queries
`2026-10-18T12:00Z ± 1 day`. Dashboard production code was not changed.

## Exact executed validation

Red, before the production readiness path:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase25_6_sdk_busy.py --tb=short
# 1 failed in 5.44s
# AssertionError: cursor_sdk_ambiguous_missing; assert 'blocked' == 'completed'
```

After the absent-journal guard, the previously failing recovery and acceptance
callers:

```bash
uv run python -m pytest -q \
  tests/integration/test_phase25_3_sdk_recovery.py::test_failed_initial_and_correction_recover_through_ticks_and_cli_without_prompt_drift \
  tests/integration/test_phase25_3_sdk_recovery.py::test_correction_capacity_timer_preserves_exact_turn_then_uses_decisive_failure \
  tests/integration/test_phase25_3_sdk_recovery.py::test_abort_later_successor_correction_uses_current_turn_authority \
  tests/integration/test_phase25_3_sdk_recovery.py::test_recovered_sdk_sequence_leaf_checkpoints_then_advances_to_codex_provider \
  tests/integration/test_phase25_5_sdk_acceptance.py::test_sdk_blocked_review_successor_keeps_store_and_can_send_later_correction \
  tests/integration/test_phase25_5_sdk_acceptance.py::test_exact_source_sdk_config_runs_initial_correction_and_read_only_views
# 2 failed, 5 passed in 58.86s
# both failures were the dashboard cohort window above; the five recovery callers passed
```

The dashboard window correction plus the absent-journal unit test:

```bash
uv run python -m pytest -q \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py::test_absent_create_journal_does_not_infer_creation_readiness \
  tests/integration/test_phase25_5_sdk_acceptance.py::test_exact_source_sdk_config_runs_initial_correction_and_read_only_views
# 3 passed in 14.13s
```

Focused gate:

```bash
uv run python -m pytest -q \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
# 122 passed in 246.52s
```

That gate was the pre-correction snapshot. The independent review of the staged
tree then returned **1 failed, 121 passed in 245.32s** (F-04) and the external
repro returned **2 failed in 15.81s** (F-01 and F-02).

Correction turn 2, after the four fixes:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py::test_phase25_3_persisted_models_match_published_json_schemas \
  tests/integration/test_phase25_6_sdk_busy.py
# 17 passed in 109.04s

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
# 147 passed in 327.74s
```

```bash
uv run python -m ruff check \
  src/ai_dev_loop/scheduler/cursor_attempt_runner.py \
  src/ai_dev_loop/scheduler/application/cursor_sdk_journal.py \
  src/ai_dev_loop/scheduler/application/cursor_sdk_reconciliation.py \
  src/ai_dev_loop/scheduler/application/fake_attempt_backend.py \
  src/ai_dev_loop/scheduler/domain/cursor_sdk_recovery.py \
  src/ai_dev_loop/scheduler/domain/events.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_2_sdk_workflow.py
# All checks passed!
uv run python -m ruff format --check <the same 9 paths>
# 9 files already formatted
uv run python -m mypy src
# Success: no issues found in 216 source files
uv run python -m mkdocs build --strict
# exit 0; built in 1.16 seconds
```

The MkDocs 2.0 Material warning is pre-existing. Incident pages stay out of
nav, as the other incident pages do. The RCA's absolute links were left as-is
and did not fail the strict build.

Recovery turn 4, after F-02, F-03 and F-05:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
# 152 passed in 310.64s
```

```bash
uv run python -m ruff check \
  src/ai_dev_loop/scheduler/application/attempt_service.py \
  src/ai_dev_loop/scheduler/application/cursor_sdk_reconciliation.py \
  src/ai_dev_loop/scheduler/application/review_recovery.py \
  src/ai_dev_loop/scheduler/cursor_attempt_runner.py \
  src/ai_dev_loop/scheduler/domain/cursor_contract.py \
  src/ai_dev_loop/scheduler/domain/cursor_sdk_recovery.py \
  src/ai_dev_loop/scheduler/domain/events.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_6_sdk_busy.py
# All checks passed!
uv run python -m ruff format --check <the same paths>
# 10 files already formatted
uv run python -m mypy src
# Success: no issues found in 216 source files
uv run python -m mkdocs build --strict
# exit 0; built in 1.21 seconds
```

An earlier pass of the integration file (**16 passed in 99.68s**) and of the
unit busy file plus the schema equality test (**62 passed in 12.63s**) ran
before `CursorSdkReconciliationRecordV2` was split from the v1 subclass. The
152-pass gate above is the result after that split.

## Unexecuted gates

Not run, and not claimed:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Package build, install, and any real SDK smoke were not run. No live model,
timer change, integration install, or scheduler run against a real ledger was
performed. Fake completion does not close INC-2026-10-19-001.

## Changed files

Production:

- `src/ai_dev_loop/integrations/cursor_sdk/adapter.py`
- `src/ai_dev_loop/integrations/cursor_sdk/port.py`
- `src/ai_dev_loop/scheduler/application/attempt_service.py`
- `src/ai_dev_loop/scheduler/application/cursor_sdk_journal.py`
- `src/ai_dev_loop/scheduler/application/cursor_sdk_reconciliation.py`
- `src/ai_dev_loop/scheduler/application/cursor_workflow_service.py`
- `src/ai_dev_loop/scheduler/application/fake_attempt_backend.py`
- `src/ai_dev_loop/scheduler/application/review_recovery.py`
- `src/ai_dev_loop/scheduler/cursor_attempt_runner.py`
- `src/ai_dev_loop/scheduler/domain/cursor_contract.py`
- `src/ai_dev_loop/scheduler/domain/cursor_sdk_readiness.py`
- `src/ai_dev_loop/scheduler/domain/cursor_sdk_recovery.py`
- `src/ai_dev_loop/scheduler/domain/events.py`
- `src/ai_dev_loop/scheduler/domain/state.py`
- `src/ai_dev_loop/schemas/scheduler-cursor-sdk-reconciliation-record-v1.json`
- `src/ai_dev_loop/schemas/scheduler-cursor-sdk-reconciliation-record-v2.json`

Tests:

- `tests/integration/test_phase25_2_sdk_workflow.py`
- `tests/integration/test_phase25_5_sdk_acceptance.py`
- `tests/integration/test_phase25_6_sdk_busy.py`
- `tests/unit/scheduler/test_phase25_2_sdk_worker.py`
- `tests/unit/scheduler/test_phase25_3_sdk_recovery.py`
- `tests/unit/scheduler/test_phase25_6_sdk_busy.py`
- `tests/unit/test_phase25_1_sdk_adapter.py`

Docs:

- `docs/operacion/estado-artefactos.md`
- `docs/operacion/incidentes/index.md`
- `docs/operacion/incidentes/2026-10-19-cursor-sdk-busy-first-send-ambiguous-missing.md` (addendum only)
- `docs/referencia/cli.md`
- `docs/referencia/configuracion.md`

This findings file:

- `archive/implementation-history/findings/phase-25-6-sdk-first-send-busy.md`
