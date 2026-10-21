# Phase 25.7 B — SDK deadlines and wait evidence

Date: 2026-10-20
Scope: delivery B only (`C-04`, `C-05`)

## Correction: F-01 fixed, F-02 fixed

### F-01 — stopped-wait diagnostics: fixed

`inspect_sdk_wait` treated `(send_authorized, interrupted)` as an open wait.
Later clocks kept `waiting=true` and increased elapsed time. An interrupted
prefix without a native handle now projects stage `interrupted`,
`waiting=false`, and elapsed time frozen from authorization to the seal.
If that gap is not established, elapsed time is `unknown`. The projection does
not invent `native_handle_bound` or `terminal_observed`. A bound handle still
freezes elapsed time at the binding. An open `send_authorized` prefix still
grows with the inspection clock.

Ordinary send, readiness, and stream failure returns in
`cursor_attempt_runner` seal that progress when a file exists and copy the
safe fields into the outcome. `main` writes that line to the attempt result
stderr. The authenticated child stderr is not modified. Reconciliation,
reservation, and retry policy are unchanged. Restart and abort callers already
seal the same progress file; those prefixes now project as closed waits.

### F-02 — production-boundary deadline coverage: fixed

The public-client check now calls `CursorSdkAdapter.list_runs`, `resume_agent`,
`send`, and `stream` through the pinned `Client` and an `httpx` mock transport.
Administrative `ListRuns` keeps the short cap. Resume unary timeouts decrease
with the remaining budget. One `Send` waits past that short cap and still
completes inside the frozen deadline, with the recorded stream timeout on the
deadline view. `ObserveRun` uses that same stream view. A spent budget raises
`CursorSdkOperationBudgetExhausted` and performs no further RPC. The worker
turn drives the same adapter: one accepted send, short inventory timeout, and
an empty child stderr.

Descendant termination is no longer demonstrated by a pid the test registers
itself. The regression uses `SystemdUserBackend.launch`, which sets
`KillMode=control-group`, then `terminate` and `observe`. Bridge and tool
descendants tracked by that control group are gone, and
`unit_observation_stopped` is true, before the test treats reconciliation as
allowed. The in-worker descendant registry is not used as that proof.

## Outcome: completed candidate for independent review

Delivery A is present in the accepted tree and remains compatible. Fresh SDK
submissions still freeze SQLite through the installed public initializer, and
historical v7/v8 contexts and sequence definition/entry v3 stay JSONL. This
leaf does not change that store, install code, or any frozen sequence input.

B separates short administrative RPC timeouts from resume/send/stream waiting
and records safe stage evidence. A lost send still converges through the
existing authenticated native reconciliation. Delivery C is not implemented.

No new OpenQuestion was found.

## Contract status

### C-04 — administrative timeouts stay short; the frozen deadline owns the send: satisfied

- Ping, authentication, inventory, get, and cancel stay on the base client.
  `Client.connect(..., timeout=60)` and `launch_bridge(client_timeout=60)` still
  set both the unary and stream caps on that client to 60 seconds.
- Resume, send, and stream recheck `operation_budget_remaining()` before the
  RPC. The remaining frozen attempt deadline is applied with public
  `Client.with_options`: stream waiting raises only `stream_timeout`, and
  resume raises only `unary_timeout`. The base client is not rewritten.
- A remaining budget at or below zero raises `CursorSdkOperationBudgetExhausted`
  before `with_options` and before the RPC. That exception is a `BaseException`,
  like `CursorSdkOperationTimeout`, so an SDK `except Exception` cannot swallow
  it. The worker treats either exception as exit 124.
- The monotonic deadline is bound once in `_sdk_operation_deadline` and is not
  reset for resume, send, or stream. The outer `SIGALRM` / `ITIMER_REAL`
  deadline and owned process-tree termination stay in place. HTTP inactivity is
  only a ceiling equal to the remaining budget; it is not the stall detector.
- Phase 25.6 creation readiness still uses its own ten-second remainder and
  restores the outer itimer afterward. An authorized send is not wrapped in
  that readiness timer.
- On a deadline exception, the worker SIGKILLs only process groups registered
  for that attempt (`start_new_session` children). It does not kill the
  worker's own process group or widen cancellation.

### C-05 — safe stage evidence and existing lost-send reconciliation: satisfied

- New persisted form: `cursor/sdk-attempts/<attempt-id>/wait-progress.jsonl`,
  schema 1. Stages are `send_authorized`, `native_handle_bound`, and
  `terminal_observed`. Sealers may append `interrupted` only after a sealable
  prefix: empty, `send_authorized`, or `send_authorized` then
  `native_handle_bound`. A missing file is already complete. A symlink or
  corrupt file fails closed. A completed or already interrupted file is an
  idempotent success and does not invent a later stage.
- Journal v1 and v2 are unchanged. A failed progress seal is diagnostic and
  does not veto journal recovery.
- `scheduler timeline` projects `sdk_wait_stage`,
  `sdk_wait_waiting_for_initial_response`, and `sdk_wait_elapsed_seconds`.
  Missing or unusable evidence is `unknown`. Timeline JSON stays schema 1 with
  those additive fields defaulting to `unknown`. A timeline service constructed
  without an artifact root does not read the filesystem.
- Elapsed time is frozen at native-handle binding (`bind_at - authorized_at`).
  An open wait uses the inspection clock. Interrupted evidence keeps the last
  real stage. Extra keys such as text, prompt, native status, and CPU are not
  rendered. Nested extra values fail closed.
- The attempt result stderr carries one safe `sdk_wait` line on success and on
  a deadline failure. The authenticated SDK child stderr stream is not modified,
  so its existing digest and empty-stream contract stay intact.
- A native `running` label, CPU, or missing text is not reported as a stall or
  as model progress.
- Lost-send convergence reuses the existing policy: one attributable candidate
  is adopted and cancelled with no second send; a finished native run is
  replayed once; zero or multiple candidates, and unusable evidence, hold the
  reservation. Retry ceilings, cancellation eligibility, and reviewer budgets
  are unchanged.

## Historical compatibility

- SDK journals v1 and v2, their sealable prefixes, and their readers are
  unchanged.
- Wait progress is a new schema-1 artifact. Historical attempts have no file;
  seal and inspection treat that absence as complete / `unknown`.
- `TimelineEntry` gains three optional strings that default to `unknown`.
  Existing constructors and schema version 1 remain valid.

## Red, then green

The first focused gate failed in four tests (`4 failed, 196 passed in 318.78s`):

- `test_failed_health_closes_bridge_and_adapter_uses_no_sdk_private_surface`
  rejected the method name `_transport_view` because the adapter source must
  not contain the private SDK token `_transport`. The method is now
  `_deadline_client`.
- `test_sdk_initial_and_correction_reach_acceptance_with_durable_exact_identity`
  and the send-uncertain / stream-failure cases of
  `test_sdk_lost_identity_result_holds_reservation_and_rejects_recovery`
  failed because stage lines were appended to the authenticated child stderr
  after, or instead of, its sealed digest. Those lines now stay in
  `wait-progress.jsonl` and the attempt result stderr.

The four previously failing tests, together with the new deadline file, then
passed: `17 passed in 20.05s`.

## Changed callers

- `CursorSdkAdapter.resume_agent`, `send`, and `stream` take a public
  `with_options` view from `_deadline_client`. After resume, the returned agent
  is pointed back at the base client. Send swaps `agent.client` only for the
  call that primes the stream, then restores it.
- `cursor_attempt_runner._sdk_operation_deadline` binds the deadline once.
  Its descendant registry terminates only pids production code has registered;
  no production caller registers one. `main` maps both deadline exceptions to
  exit 124 and a safe result-stderr line. Descendant coverage uses
  `SystemdUserBackend.launch` (`KillMode=control-group`), then `terminate` and
  `observe`, and treats reconciliation as allowed only after
  `unit_observation_stopped`.
- `_note_sdk_wait` appends schema-1 progress at send authorization, native
  binding, and terminal observation. It does not write the child stderr.
- `_sdk_uncertainty_payload` seals interrupted progress for a turn deadline.
  `AttemptService` does the same when a lost turn seals its journal.
  `ensure_aborted_sdk_turn_reconciliation` seals the aborted turn's progress
  after the journal seal and does not change that function's boolean result.
- `SchedulerTimelineService.get_timeline` and `default_timeline_service` project
  the artifact. `render_scheduler_timeline_output` prints the three fields.
- Reconciliation callers are unchanged. The new tests drive
  `_run_sdk_reconciliation_operation` and assert `send_calls == 0`.

Production and documentation files:

- `src/ai_dev_loop/integrations/cursor_sdk/adapter.py`
- `src/ai_dev_loop/scheduler/application/cursor_sdk_progress.py` (new)
- `src/ai_dev_loop/scheduler/cursor_attempt_runner.py`
- `src/ai_dev_loop/scheduler/application/{attempt_service.py,contracts.py,cursor_sdk_reconciliation.py,timeline.py}`
- `src/ai_dev_loop/scheduler/domain/cursor_contract.py`
- `src/ai_dev_loop/commands/scheduler.py`
- `docs/referencia/cli.md`
- `docs/operacion/{estado-artefactos.md,observabilidad.md}`
- `tests/unit/scheduler/test_phase25_7_sdk_deadlines.py` (new)

## Validation

Final focused gate, after the child-stderr correction. This command does not
pass `--extra cursor-sdk`; the public-client test imported the already
installed `cursor_sdk` and passed inside this run. It was not skipped.

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/test_phase25_1_sdk_adapter.py tests/unit/test_paths.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
200 passed in 320.69s (0:05:20)
```

Static and documentation checks, run in parallel with that gate and finished
successfully:

```text
uv run python -m ruff check .
All checks passed!

uv run python -m ruff format --check .
417 files already formatted

uv run python -m mypy src
Success: no issues found in 217 source files

uv run python -m mkdocs build --strict
Documentation built in 0.67 seconds
```

The strict docs build also printed the pre-existing Material-for-MkDocs warning
and the existing unlisted incident pages. It exited 0.

Correction rerun for F-01 and F-02. The focused gate includes the timeline
regressions and ran before the final format of the deadline test file:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/test_phase25_1_sdk_adapter.py tests/unit/test_paths.py \
  tests/unit/scheduler/test_phase18_timeline_and_provenance.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py
210 passed in 313.74s (0:05:13)
```

After `ruff format` of `tests/unit/scheduler/test_phase25_7_sdk_deadlines.py`:

```text
uv run python -m ruff check \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  src/ai_dev_loop/scheduler/cursor_attempt_runner.py \
  src/ai_dev_loop/scheduler/application/cursor_sdk_progress.py
All checks passed!

uv run python -m ruff format --check .
417 files already formatted

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q --tb=line \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/unit/scheduler/test_phase18_timeline_and_provenance.py
22 passed in 8.07s

uv run python -m mypy src
Success: no issues found in 217 source files

uv run python -m mkdocs build --strict
Documentation built in 0.75 seconds
```

`ruff check .` had already passed on the pre-format tree. The format pass
changed only the deadline test file. The strict docs build again printed the
pre-existing Material warning and unlisted incident pages, then exited 0.

## Remaining gates and boundaries

- The repository-wide pytest suite and the outer packaging/integration pipeline
  were not run. They remain the separate pipeline gate.
- A's pinned-SDK SQLite store gate was not repeated. B does not change the
  initializer, the store binding, or that test.
- Real-model smoke, live acceptance, operator installation, live ledger or
  artifact mutation, scheduler/controller/recovery actions, services, timers,
  and retention cleanup were not executed.
- Delivery C (automatic seven-day retention, C-06/C-07) was not implemented.
- No `ai_dev_loop.yaml` change, staging, commit, push, or Git-history mutation
  was performed.
