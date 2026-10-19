# Phase 25.2 SDK identity, worker and evidence findings

Status: **READY_FOR_REVIEW**. This is implementation evidence, not scheduler
acceptance. It does not authorize an intermediate checkpoint, installation,
provider activity, retry-policy expansion, commit, push, PR or merge.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-25-sdk-replanned`, starting HEAD
`56dba191179334b29c21bf74c1f9c4611510d119`. Baseline `558b799` is an ancestor
(`git merge-base --is-ancestor 558b799 HEAD`, exit 0). HEAD contains accepted
Phase 25.1 and accepted Phases 26-28. Phase 24 was not used. The replanned
overview and this slice both have `OpenQuestions: None`; the accepted Phase 25.1
prerequisite suite passed before dependent work and again against the final
sources.

The implementation adds manifest v4 and frozen definition/entry v3 for named
SDK and mixed-provider sequences, submitted contexts v7/v8, a durable SDK
identity journal, exact SDK agent/native-run state, owned worker dispatch,
frozen in-worker execution deadlines, bounded authenticated SDK output,
process-tree termination gates, normal SDK/Codex review flow and
provider-specific recovery guards. Native success follows cursor-sdk 1.0.37's
`finished` status. Historical Cursor CLI and Codex behavior remains readable
and independently tested.

Every automated execution used fake SDK/Codex providers or fake backends with
temporary repositories and isolated HOME/XDG roots. No real model, live
credential, live scheduler ledger, workstation install, timer, cleanup flow or
external/destructive Git action was invoked. The changing checkout was never
hot-installed or used as a live scheduler driver.

## Contract evidence

| ID | Production callers and behavior | Independent automated evidence | Final result |
| --- | --- | --- | --- |
| W-01 | `resolve_effective_config`, `SubmissionService`, `SequencePrepareService`, `build_sequence_run_context`, `SequenceManifest`/`FrozenSequenceEntry`/`SubmittedRunContext` validation and `SqliteSchedulerStore` freeze complete SDK selection while preserving named presentation metadata, independent implementer/reviewer bindings and historical serializers. Manifest v4 requires complete per-phase SDK model+parameters; v1-v3 reject explicit SDK-only runtime/parameter fields before publication instead of silently converting them to CLI authority. Standalone SDK uses context v7 and sequence leaves v8. Exact SDK IDs use separate validators and do not relax Codex UUIDs. | `test_manifest_v4_requires_names_and_complete_atomic_sdk_selection`, `test_historical_manifest_versions_reject_sdk_only_fields`, `test_named_manifest_v3_keeps_historical_cli_cursor_overrides`, `test_sdk_implementer_binding_has_fresh_shape_and_no_cli_command`, both malformed-ID cases, `test_genuine_historical_fixtures_keep_recorded_bytes_and_parse`, `test_manifest_v3_sdk_fields_fail_before_sequence_publication`, and `test_manifest_v4_freezes_named_mixed_provider_entries`. Historical schema, Phase 20, Phase 27 and Phase 28 selections independently reload old contexts/entries and preserve short names. | Included in current **40/40** Phase 25.2 tests and **60/60** correction-focused tests. The broader **223/223** matrix predates the final narrow F-04 follow-up. |
| W-02 | `AttemptService._cursor_binding` pins attempt/run/dispatch, owner/key, exact prompt digest and agent. `cursor_attempt_runner` calls fsync-backed `append_sdk_journal` barriers before each authority-bearing external effect: create intent before create, persisted agent binding before send can be scheduled, send intent before inference, and native run binding before stream consumption. `CursorWorkflowService` authenticates every binding before state publication. Lost or interrupted results retain the reservation and provider ownership; Phase 25.3 recovery guards prevent automatic redispatch. | Entry callbacks in `test_create_intent_precedes_agent_binding_and_terminal` and `test_send_intent_and_native_id_precede_bounded_terminal_evidence` inspect the durable journal at the external-operation boundary. `test_sdk_persistence_failure_stops_the_next_external_effect` injects each barrier failure and independently counts create/send/stream calls. `test_sdk_lost_identity_result_holds_reservation_and_rejects_recovery` counts exactly one create/send as applicable across extra ticks and proves the active reservation remains held. The normal-flow test proves identity publication before send and exact-agent correction reuse. | Included in current **40/40** and **60/60** selections; prior broader matrix **223/223**. |
| W-03 | `CursorSdkAdapter` performs package/version, bridge health, auth and exact catalog selection inside the owned attempt and redacts the active credential from envelopes, failures and terminal text. `cursor_attempt_runner` enforces frozen `timeout_seconds` across open/preflight/create or open/preflight/send/stream/close independently from systemd's finalization grace. It binds an authenticated empty capture before turn adapter activity, then replaces it with bounded partial/terminal evidence after streaming. `SystemdUserBackend` and `AttemptService` stop and re-observe the complete unit; unproved stop retains capacity/reservation. `IntegrationProcessOutputReadService` strictly authenticates bound capture and returns `not_captured` only for stopped writers without a terminal outcome. | Worker/workflow tests cover early open/preflight/send failures, create/turn deadlines, failed-stream partial finalization, cancellation, bounded capture, credential sentinels, tampering and process-tree policy. Lost-send and early turn failures read both streams through `IntegrationProcessOutputReadService`; reader suites pass **22/22** and resolution units **7/7**. | Phase 25.2 **40 passed**; correction-focused **60 passed**; affected SDK paths **30 passed**; readers **22 + 7 passed**. Prior broader matrix **223 passed**. |
| W-04 | `TickService` uses SDK create/turn effects, existing staging, independent Codex reviewer bootstrap/resume and the same exact SDK agent for correction. The worker accepts only cursor-sdk 1.0.37 terminal `status='finished'` plus non-empty text and rejects other statuses. Shared pre-execution guards authenticate the raw correction, execution envelope and staged patch before SDK send. Named v4 mixed Cursor-SDK/Codex phases preserve the existing checkpoint policy. Dashboard keeps SDK inference under stable `cursor.run_turn`; creation/probes/reconciliation remain excluded. SDK recovery commands retain the Phase 25.3 guard without changing Codex/CLI policy. | `test_sdk_initial_and_correction_reach_acceptance_with_durable_exact_identity` proves both `finished` turns reach staging, the same exact fresh reviewer and acceptance; `test_sdk_rejects_non_finished_terminal_status` preserves rejection. `test_sdk_correction_artifacts_are_authenticated_at_worker_entry_before_send` covers unchanged, missing, tampered and staged-drift cases. Mixed-provider preparation and `test_dashboard_classifies_sdk_effects_by_stable_scheduler_meaning` prove the remaining production paths; accepted Phase 27/28 selections independently preserve distinct roles, named metadata and checkpoint policy. | Included in current **40/40** and **60/60** selections; prior broader matrix **223/223**. |

## Correction review findings

Every reported finding was reproduced against the staged Phase 25.2 work and is
**fixed**. None is disputed or blocked.

| ID | Status | Production correction and caller | Independent evidence |
| --- | --- | --- | --- |
| F-01 | **Fixed** | `cursor_attempt_runner._run_sdk_turn_operation` now classifies only native `finished` plus non-empty terminal text as success. `CursorWorkflowService` therefore advances actual cursor-sdk 1.0.37 results and still blocks unsuccessful statuses. | Normal initial/correction production loop reaches staging, exact reviewer resume and acceptance; worker test rejects former fake `completed` status. |
| F-02 | **Fixed** | `_sdk_operation_deadline` applies the frozen invocation `timeout_seconds` to the complete SDK create/turn operation. Timeout exits with authenticated class `timeout`, keeps an uncertain provider-specific outcome and lets `AttemptService` prove complete owned-unit termination before adoption. The systemd execution-plus-300-second-finalization ceiling remains a separate outer bound. | Synchronized worker deadline tests and real fake worker/bridge/tool process-group timeout/abort tests prove descendant termination, no late adoption, no redispatch and retained reservation. Existing unavailable/incomplete-stop cases prove resources remain held when termination is unproved. No live unit was used. |
| F-03 | **Fixed** | `verify_pre_execution_cursor_guards` performs SDK identity/policy checks and then the shared run-turn prompt, raw-fix/envelope and staged-patch authentication; it does not add a continuous baseline restriction. This is called by the production worker before adapter open/send. | Production `cursor_attempt_runner.main` test accepts unchanged correction artifacts and suppresses send for missing fix, tampered fix and staged drift. |
| F-04 | **Fixed** | The first correction finalized capture only after a native run was bound, so existing empty files from turn open/preflight/send failures had a non-null outcome but no hashes. `_run_sdk_turn_operation` now writes authenticated empty metadata before adapter open and includes those hashes in every early failure outcome; later stream capture atomically replaces it. `stream_availability_reason` remains strict: a bound outcome without hashes is still an integrity error, cancellation without an outcome remains `not_captured`, and confinement/size/hash/identity/tamper checks are unchanged. | Production scheduler tests drive lost send, turn-open failure and turn-preflight failure through ingestion and read both stdout/stderr as authenticated empty captures. Lost send still retains the reservation, performs one send and never redispatches. Partial-stream and cancellation tests remain green; an early-failure stderr mutation is rejected. Affected SDK tests pass **30/30**, current Phase 25.2 **40/40**, reader integrations **22/22** and resolution units **7/7**. |
| F-05 | **Fixed** | `SequenceManifest.validate_commit_messages` requires manifest v4 whenever a Cursor phase explicitly supplies `runtime` or `parameters`. Historical CLI fields and frozen fixtures remain compatible; valid v4 mixed-provider preparation is unchanged. | Parametrized v1-v3 model rejection, v3 CLI compatibility, no-database-publication integration check, historical fixture hashes and v4 mixed preparation all pass. |
| F-06 | **Fixed** | Fsync journal records now have tests at their causal boundaries, not only after completion. Persistence failures stop the next external effect; uncertain create/send/run keeps exact ownership and the provider-specific recovery fence. | Independent create/send/stream counters and operation-entry callbacks prove intent/binding visibility and zero duplicate create/send across repeated ticks; uncertainty cases retain the active repository reservation. |
| F-07 | **Fixed** | `CursorSdkAdapter._terminal_result` replaces the exact active credential before terminal text leaves the adapter. The worker persists only redacted final text, and the reviewer prompt consumes that redacted artifact. | Adapter sentinel test preserves ordinary text; normal two-turn integration proves the sentinel is absent and `[redacted]` is present in every final artifact and both reviewer prompts. |

## Versioned contracts and canonical bindings

| Contract | Version and canonical authority |
| --- | --- |
| Sequence manifest | v4 extends named v3 and requires, per phase, `runtime=sdk-local`, model and canonical parameters, plus independent implementer and reviewer settings. |
| Frozen sequence | definition v3 and entry v3. Each entry freezes SDK binding, workflow, plan/prompt/config digests, independent reviewer binding and either SDK implementer v2 or historical Codex implementer v1. |
| Submitted context | v7 is standalone Cursor SDK; v8 is an SDK-backed sequence leaf and can select Cursor SDK or Codex implementer. Historical v1-v6 require their existing CLI bindings and serialization shapes. |
| SDK selection | `CursorSdkBinding` v1: runtime, SDK 1.0.37, model+canonical parameters, credential reference, setting sources, sandbox and `scheduler-owned-no-shell` tool policy. It contains no machine-private native root. |
| Implementer | v2 is Cursor SDK only and deliberately has no CLI command, Codex reasoning/approval or session. Historical v1 remains byte-compatible for Cursor CLI/Codex. |
| Runtime identity | `CursorSdkRuntimeBinding` v1: conversation owner run ID, derived conversation key, exact agent ID, optional native run ID before confirmed send, identity artifact path/hash and latest journal path/hash. Confirmed turn ingestion validates a mandatory `run-<uuid>` native ID. |
| Invocation evidence | v2 for SDK: runtime/effect, frozen repository identity, SDK selection, owner/key, timeout and exact attempt/run/dispatch; turns add iteration, agent and exact prompt path/digest. |
| Identity journal | v1, append-only and fsync-backed. Create and send causal sequences are authenticated against exact invocation/state bindings before a transition. |
| Scheduler events | `cursor_sdk_agent_created` v1 and `cursor_sdk_turn_completed` v1 preserve identity/evidence digests without changing historical events. |
| SDK capture | metadata v1, `cursor-sdk-envelope-ndjson-v1` events and `utf-8-text-v1` stderr/final. Stream bytes, byte counts, truncation flags and metadata are terminally authenticated. |

## Historical provenance

`tests/fixtures/phase25_historical/provenance.json` records existing fixture
bytes rather than rewriting them with SDK fields:

| Preserved source | SHA-256 |
| --- | --- |
| Phase 21 manifest | `03d2d51f2715ae28280d63913f0d8e1ecbdfc8cf313343dc371d503250a47cd9` |
| Phase 21 SQLite ledger | `43da316de14f2e9cd8fb9cdcbf27c78a36c1e7e20bb8913040377c160edec8cb` |
| Phase 21 Cursor CLI invocation | `6cdc45cbf99fa05844dcfab05970c8e9b7b76ec504dc37c4528a9260b6155b14` |
| Phase 22 state | `79058457f7c7a43030eb2d963e8d998e20aaa118d50a932153c09f19e56ce19e` |
| Phase 22 event | `b849788d9f00e33c079cc5fdb79996a52f2cdc054a3ba34f483ec5fd7ec31ee2` |
| Phase 23 initial recovery | `d5aa0abb86c1fcee4171981684dea5a4d96cd723ff02314d037b7f68a58408a6` |
| Phase 23 sequence lineage | `1d8302595c9491667ab7d879f184aaaffc46f63f9aec6690bfd46d87a51a4b1d` |
| Phase 27 entry hashes | `4dd0199f86f28ba56fb0001444006a42c82e6e7544c2f34e7a5eff7931a55a7a` |
| Phase 27 prepared v2 | `2fd09ba130a13130fe50a0cd74eb927724293696c673ffdece3ed296cf07fe50` |
| Phase 28 prepared named-era v2 | `6cd10c9bcb574eaae79c4a73ce13d1b110072a57370d661f8dba8004abec0780` |

## Exact final validation

All pytest commands below were executed with the prefix
`env -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT TMPDIR=/tmp TMP=/tmp TEMP=/tmp`;
their fixtures used fresh temporary HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME and
XDG_STATE_HOME roots. Removing that inherited test-only witness variable from
subprocess environments did not weaken production reviewer identity or prompt
authentication.

```bash
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/integration/test_phase25_2_sdk_workflow.py
```

Result after the final F-04 follow-up: **40 passed in 37.71s, exit 0**.

The correction-focused selection also included the adapter tests changed for
terminal-result redaction:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/integration/test_phase25_2_sdk_workflow.py
```

Result after the final F-04 follow-up: **60 passed in 38.63s, exit 0**.

```bash
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/integration/test_phase17_4_cursor_workflow.py \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/unit/scheduler/test_attempt_executor.py \
  tests/integration/test_phase20_1_sequence_prepare.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/unit/scheduler/test_phase27_1_implementer_inputs.py \
  tests/unit/scheduler/test_phase27_2_codex_implementer.py \
  tests/integration/test_phase27_3_implementer_sequences.py \
  tests/integration/test_phase28_short_names.py \
  tests/unit/dashboard/test_metrics.py \
  tests/integration/test_phase26_1_dashboard_queries.py
```

Result before the final narrow F-04 follow-up: **223 passed in 233.43s, exit
0**. This broader matrix was not rerun after the worker-only evidence binding
change; the current 40/40 Phase 25.2 and affected-reader selections below are
the final-source evidence for that change.

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_phase25_1_sdk_config.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/integration/test_phase25_1_sdk_packaging.py
```

Result: **46 passed in 43.48s, exit 0**. This is the accepted Phase 25.1
prerequisite selection against the final Phase 25.2 sources.

```bash
.venv/bin/python -m pytest -q \
  tests/integration/test_phase21_5_process_output.py \
  tests/integration/test_phase26_5_attempt_content.py
```

Result after the final F-04 follow-up: **22 passed in 75.95s, exit 0**.

```bash
.venv/bin/python -m pytest -q \
  tests/unit/integration_api/test_process_output_resolution.py
```

Result after the final F-04 follow-up: **7 passed in 0.34s, exit 0**.

```bash
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/integration/test_phase25_2_sdk_workflow.py
```

Result after the final F-04 follow-up: **30 passed in 39.27s, exit 0**.

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
git diff --cached --check
```

Results: Ruff format **399 files already formatted**; Ruff check **All checks
passed**; MyPy **Success: no issues found in 208 source files**; strict MkDocs
**exit 0** with the existing Material/MkDocs 2 notice and
existing unnav-listed incident-page notices; unstaged and staged diff checks
both **exit 0**.

Additional final-path regression evidence:

```bash
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_2_sdk_workflow.py::test_sdk_completed_result_waits_for_proved_process_tree_termination \
  tests/integration/test_phase25_2_sdk_workflow.py::test_sdk_initial_and_correction_reach_acceptance_with_durable_exact_identity \
  tests/integration/test_phase17_6_abort_lifecycle.py \
  tests/unit/scheduler/test_attempt_executor.py
```

Result after the correction implementation: **46 passed in 20.52s, exit 0**.

## Resolved development failures

The following failures occurred during implementation and are not omitted from
the evidence:

- The separate reviewer correctly reopened F-04 after the first correction:
  open/preflight/send failures returned a terminal outcome while the empty SDK
  capture files had no bound hashes. This was confirmed in the production
  worker and strict reader paths. The final fix binds the empty capture before
  adapter activity; no reader exception was relaxed.

- The first correction unit selection reported **1 failed, 46 passed** because
  the new production-entry correction fixture used a run ID different from its
  invocation evidence. The fixture was aligned; the worker module then passed
  **17/17**, and that correction-focused selection passed **58/58**. The final
  F-04 follow-up selection is **60/60**.
- The first normal-flow correction run failed before SDK send because the fake
  SDK inspected an obsolete test journal directory. The test now inspects the
  production `cursor/sdk-attempts` path; its immediate rerun passed **1/1 in
  6.13s** and the final module selection passed.
- A correction integration `-x` run reported **1 failed, 3 passed in 12.49s**:
  the stream-failure fixture tried to edit an optional file before yielding its
  first envelope, producing an empty rather than partial capture. The fake
  edit now handles an absent file; the full integration module passed **11/11
  in 29.05s**, and the later first-correction Phase 25.2 selection passed
  **38/38**. The final F-04 follow-up selection is **40/40**. An
  earlier full-module process handle produced only partial progress output and
  is treated as inconclusive, not as validation evidence.

- An early affected-path selection reported **106 passed, 5 failed**; caller
  assumptions about new binding/context versions were corrected. A follow-up
  reported **33 passed, 1 failed** because the accepted Phase 25.1 temporary
  submit guard test still expected rejection; it was converted to assert the
  implemented v7 freeze boundary.
- The first plan matrix reported **192 passed, 1 failed in 199.09s**. A Phase
  20.3 test was producing a fresh manifest v1, contrary to accepted Phase 28
  admission; the fresh fixture now uses named v3 while historical v1 replay
  tests remain unchanged.
- Initial new-schema checks had **3 failures**: two were test-side unresolved
  local JSON references and one exposed v4 serialization of CLI null fields.
  Tests now use a local schema registry and the v4 serializer omits CLI fields;
  the focused schema rerun passed **2/2**.
- A pre-final mandatory matrix passed **201 tests in 214.02s** before the later
  process-tree and capture-format strengthening. One later matrix reached 71%
  with no failure but its terminal output was lost when the repeated user turn
  invalidated the process handle; it is treated as inconclusive and replaced by
  the then-final pre-correction **203/203** result. The correction-expanded
  first-correction matrix was **223/223**; the final F-04 follow-up used the
  focused selections recorded above.
- One prerequisite rerun reported **44 passed, 1 failed in 26.30s** because a
  test joined the logical run ID directly to the hashed protected-artifact
  root. It now resolves through `ProtectedArtifactStore.run_root`; the final
  pre-correction prerequisite result was **45/45** and the expanded correction
  result is **46/46**.
- The first repository formatter gate reported two accepted test files needing
  formatting; mechanical Ruff formatting was applied. Final format is clean.
- Intermediate MyPy runs exposed 18 new optional-binding/type-narrowing errors,
  then one obsolete `type: ignore`; all were corrected. Final MyPy is clean.
- The new termination test first reported **1 failed, 6 passed** and then **1
  failed, 43 passed** because abort reconciliation treated a previously exited
  launcher as proof that descendant termination was resolved. Abort now issues
  an explicit owned-unit stop and requires a stopped observation for every
  pending attempt. The final targeted regression is **46/46** and the final
  first-correction plan matrix was **223/223**.

## Unexecuted work and safe next actions

- Repository-wide pytest is deliberately **unexecuted**; Phase 25.5 owns that
  gate. The focused results do not imply repository-wide validation.
- The **223/223** documented broader matrix was not rerun after the final narrow
  F-04 worker evidence change. Current final-source validation is the **40/40**
  Phase 25.2 selection, **60/60** correction selection, **30/30** affected SDK
  selection and **22/22 + 7/7** output-reader selections above.
- Real Cursor/Codex model activity, live credentials, live user-systemd units,
  installed-scheduler behavior, timer changes and workstation installation are
  **unexecuted by design**. Process ownership was tested with fake backends and
  child-tree models, not a live workstation unit.
- SDK recovery/reconciliation remains deliberately guarded. Phase 25.3 must
  replace the runtime-specific guard with approved convergent recovery; this is
  planned future work, not unfinished W-01-W-04 work. No second agent, guessed
  identity or fallback provider is authorized for uncertain create/send/run.
- No mandatory Phase 25.2 item is known unfinished. The safe next action is
  scheduler-owned staging normalization followed by the separate Codex reviewer
  using the preserved staged-only review skill. Only that scheduler/reviewer
  flow may accept an intermediate sequence checkpoint. Final changes must remain
  staged; no commit or external Git action is authorized here.
- `create-cursor-plan`, `ai-dev-loop-docs-acceptance-governance` and
  `review-staged-ai-dev-loop-execution` are unchanged. No `.cursor/skills/`
  directory was created. The documentation-governance skill required the
  privacy, no-live-operation, authoritative-evidence and safe-next-action
  statements recorded above.
