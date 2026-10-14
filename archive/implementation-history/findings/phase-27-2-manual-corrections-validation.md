# Phase 27.2 — manual correction validation

Date: 2026-10-14. Worktree: `/home/rojobad/Projects/ai_dev_loop-phase27-execution`.
Branch: `codex/phase-27-dual-implementer`; HEAD remains `062f43e`.

This records implementation-side correction evidence for the supplied independent
review. It does not record independent acceptance or change the blocked scheduler
run/sequence. Existing staged Phase 27.2 work and the original partial implementation
were preserved. Scope remains C-05 through C-09.

## Finding closure

### F-01 — fixed (C-07, C-08)

Production: `scheduler/codex_implementer_attempt_runner.py::_execute`,
`scheduler/application/codex_workflow_service.py::_ingest_codex_implementer`, and
`scheduler/domain/reducer.py::apply_implementer_usage_capacity_detected`, under
`src/ai_dev_loop/`.

The adapter retains failure-code/evidence classification in protected outcome and
metadata artifacts. It publishes role/turn/session/prompt continuation intent for
eligible resolved nonzero failures before publishing the completion envelope.
The workflow uses the existing `CodexCapacityProbePort` and app-server implementation:
an EXHAUSTED post-failure observation enters an implementer capacity wait with
`post_failure_capacity_probe` provenance; AVAILABLE or UNAVAILABLE blocks the
non-quota failure. Timeout, signal, incomplete output, and unsafe identity cannot
gain post-failure retry authority. Successful implementation clears its inferred
failure provenance before the independent reviewer starts.

Tests in `tests/unit/scheduler/test_phase27_2_implementer_capacity.py`:

- `test_post_failure_exhausted_probe_waits_and_resumes_exact_pending_turn`: initial
  and correction submit/start/tick paths, real fake app-server probe witness,
  authenticated intent/hash, restart, exact implementer resume, and review counters.
- `test_post_failure_probe_without_exhaustion_blocks_without_retry_authority`:
  initial/correction crossed with AVAILABLE/UNAVAILABLE; no quota continuation,
  no extra staging/review, ordinary resolved-failure reservation release.
- `test_provider_message_quota_preserves_evidence_classification` and
  `test_unsafe_failure_never_gains_post_failure_probe_authority`.
- `test_late_post_failure_probe_cannot_reingest_a_consumed_attempt`: delayed
  EXHAUSTED/AVAILABLE results cannot mutate a newer continuation.

### F-02 — fixed (C-08, C-09)

Production: `scheduler/application/codex_workflow_service.py::_block_implementer`
and `_append_block`, `scheduler/domain/codex_contract.py` hold classifications,
`scheduler/domain/state.py::is_codex_implementer_reservation_hold`,
`scheduler/application/abort.py::SchedulerAbortService.abort_run`,
`scheduler/domain/reducer.py::apply_run_aborted`, and
`scheduler/application/safe_actions.py`, under `src/ai_dev_loop/`.

Unsafe implementer identity/evidence blocks retain the existing active repository
reservation. Restart does not release it, and another submission cannot acquire it.
Explicit abort is narrowly permitted for a held unsafe Codex implementer block;
it uses the existing durable cancellation, owned-attempt reconciliation, and resource
cleanup. Other terminal blocks retain their abort conflict policy. No identity is
guessed and no replacement bootstrap is authorized.

`test_uncertain_bootstrap_with_possible_work_retains_reservation_across_restart`
covers conflicting identities and quota without identity after partial edits,
restart, competing submission rejection, projected safe action, explicit abort,
and preservation of edits. Ordinary failure/abort regressions also pass.

### F-03 — fixed (C-05, C-06)

Production: `src/ai_dev_loop/scheduler/codex_implementer_attempt_runner.py::_execute`
decodes authenticated prompt bytes directly, without universal newline translation.
The existing bounded stdin writer encodes those UTF-8 characters unchanged.

`test_adapter_forwards_authenticated_prompt_bytes_without_newline_translation`
in `test_phase27_2_codex_implementer_adapter.py` uses a separate fake executable
reading `sys.stdin.buffer`, for initial, correction, and capacity-continuation
arguments. `test_production_prompt_byte_witness_for_initial_correction_and_capacity_resume`
in the capacity suite exercises complete submit/start/tick/review/correction callers
and restart with an independent binary stdin witness. CRLF, mixed newlines,
Unicode, and trailing newlines must equal the protected execution envelope bytes
and digest; the reviewer-authored correction remains exact. Existing LF tests pass.

### F-04 — fixed (C-08, C-09)

Production callers exercised: `TickService.run_once`,
`CodexWorkflowService._maybe_probe_codex_capacity` and `_ingest_codex_implementer`,
`AttemptService._maybe_launch_codex_implementer_attempt`, and
`SchedulerAbortService.abort_run`. The existing lease/claim/abort mechanisms handle
the authorization/dispatch races. The tests additionally exposed a stale
post-failure result that could re-ingest an already consumed attempt; implementer
wait, success, and block transitions now reject that consumed attempt transactionally.

Tests in `test_phase27_2_implementer_capacity.py`:

- `test_competing_ticks_around_available_probe_launch_one_exact_continuation` and
  `test_competing_tick_owners_at_continuation_claim_dispatch_only_once`: Events
  synchronize distinct tick owners and generations; one exact role/session/prompt/
  frozen configuration launches, and stale authorization/claim loses.
- `test_late_post_failure_probe_cannot_reingest_a_consumed_attempt`: independently
  reproduced two production failures before the guard (**2 failed in 6.24s**),
  then verified the fix (**2 passed, 33 deselected in 7.93s**).
- `test_abort_during_implementer_wait_cancels_retry_and_preserves_partial_work`,
  `test_abort_during_available_probe_rejects_late_authorization`,
  `test_abort_after_probe_before_dispatch_cancels_continuation_effect`, and
  `test_abort_after_continuation_dispatch_holds_until_termination_and_rejects_late_result`:
  before/during/after probe and before/after dispatch, retained unresolved hold,
  exact owned termination, unchanged review counters, and one stale-result rejection.
- `test_uncertain_attempt_ownership_holds_then_converges_without_duplicate_work`
  and `test_uncertain_completion_evidence_converges_to_capacity_wait_after_resolution`:
  hold while evidence is uncertain, then restart/reconcile into the same implementer
  wait/continuation without duplicate bootstrap or premature staging/review.

## Contract-to-production mapping

| Contract | Production route | Verification |
| --- | --- | --- |
| C-05 | submit/start/tick → `AttemptService._maybe_launch_codex_implementer_attempt` → authenticated implementer runner | argv/configuration, distinct identity, private artifacts, binary stdin adapter tests |
| C-06 | implementer completion → `CursorWorkflowService._run_staging` → independent reviewer → `_apply_review_decision` → implementer resume → `_complete_accepted_review` | complete standalone tests and correction/continuation binary witnesses |
| C-07 | `_ingest_codex_implementer` → typed/provider or post-failure evidence → capacity reducer → `_maybe_probe_codex_capacity` → pending-role effect | initial/correction quota, repeated quota, probe outcomes, provenance, exact resume |
| C-08 | authenticated wait intent/envelope → durable snapshot/attempt → lease/claim/CAS reconciliation | restart, reservation hold, synchronized owners, stale-result rejection, resolved uncertainty |
| C-09 | existing durable abort/owned-attempt cleanup; narrow held-implementer block abort; retry guards | probe/dispatch abort races, timeout/failure exclusions, unsupported retries, Cursor/reviewer regressions |

## Exact validation

Final required contract/prerequisite/reviewer/recovery selection:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase27_1_implementer_inputs.py \
  tests/unit/scheduler/test_phase27_2_codex_implementer.py \
  tests/unit/scheduler/test_phase27_2_codex_implementer_adapter.py \
  tests/unit/scheduler/test_phase27_2_implementer_capacity.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py \
  tests/unit/scheduler/test_phase17_5_codex_corrections.py \
  tests/unit/scheduler/test_phase17_6_abort.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase23_2_initial_recovery.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py
```

**261 passed in 303.84s**. Includes 43 current Phase 27.2 tests. An earlier
iteration of this selection reported 257 passed and two new test-harness failures:
the assertion queried the un-ingested-only attempt selector after ingestion.
It now reads the exact terminated attempt bound to the continuation; the final
selection above has no failures.

Additional affected Cursor/ledger/tick/abort/attempt regression selection:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_cursor_workflow_corrections.py \
  tests/unit/scheduler/test_cursor_retry_after.py \
  tests/unit/scheduler/test_v4_migration.py \
  tests/unit/scheduler/test_v5_migration.py \
  tests/unit/scheduler/test_v6_migration.py \
  tests/unit/scheduler/test_sqlite_store.py \
  tests/unit/scheduler/test_tick.py \
  tests/unit/scheduler/test_tick_regressions.py \
  tests/unit/scheduler/test_phase17_6_abort_corrections.py \
  tests/unit/scheduler/test_phase20_8_sequence_blocked_abort.py \
  tests/unit/scheduler/test_phase20_9_multi_run_abort.py \
  tests/unit/scheduler/test_attempt_executor.py
```

**124 passed in 74.82s**. This selection ran before the final implementer-only
consumed-attempt guard; its affected historical paths were unchanged by that guard.
The two selections cover **385 distinct passing tests**, not a repository-wide run.

Required checks:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict --site-dir /tmp/phase27-2-corrections-review-site
git diff --check
git diff --cached --check
```

Formatting: **344 files already formatted**. Ruff: **passed**. Mypy:
**passed, 181 source files**. Strict documentation build and whitespace checks:
**passed**; the documentation build only printed the informational upstream
Material/MkDocs notice.

## Remaining work and boundaries

No mandatory C-05..C-09 implementation or finding closure remains open in this
correction. Independent review/acceptance and the separate repository-wide
pipeline/integration gate remain pending. Phase 27.3 remains deferred.

Tests used fake CLIs/backends, temporary Git repositories, and isolated XDG roots.
No real model, scheduler-owned session, live ledger/protected run artifact,
blocked run/sequence, package/timer installation, target YAML, or planning/review
skill was controlled or modified. No commit was created in the execution worktree.
Changes are left staged for independent review under the user's staging authority.
