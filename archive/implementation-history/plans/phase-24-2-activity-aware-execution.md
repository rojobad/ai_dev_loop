# Phase 24.2 — Activity-aware runner, systemd budgets and manual hard-timeout continuation

## Goals

Make the accepted 24.1 activity policy execute through real Cursor attempts.
Keep active turns beyond soft, stop idle turns only after soft plus the inactivity
condition, and always stop at hard. An authenticated hard timeout waits for
manual continuation in the same run; no automatic retry is allowed.

## Non-Goals

Inferring useful progress, extending long tools indefinitely, measuring cost,
fixing the user-systemd control bus, or changing Codex/create-chat timeouts.

## Scope

Incremental activity parsing/monotonic policy evaluation, process cleanup,
frozen invocation and completion evidence, systemd execution budgets,
authenticated timeout reason, same-run retry/wait state and safe summaries,
affected docs/rules and mandatory automated tests.

## Out of Scope

No other process-timeout policy changes, transcript copies, Git/CPU heartbeat
watchers, provider/model switching, aggregate spending limits, workstation
services/installations, root YAML or planning/review skill edits. Do not control
live runs or solve the pending Phase 8.9 incident.

## Required Context

Read [the overview](phase-24-cursor-activity-timeouts.md),
[24.1](phase-24-1-timeout-policy-contracts.md), its actual accepted findings,
`AGENTS.md` and CLI/config/state/privacy/operation docs. Verify A-01–A-03 in the
checkout, including loader versions, frozen policy, genuine historical fixtures
and temporary admission guard. If absent, stop dependent implementation and
report evidence; do not copy abandoned WIP as a prerequisite.

Trace `process.py` (`run_process_streaming`, `_capture_bounded_streams`),
`runners/cursor.py` (`execute_prompt`, stream parsing), scheduler
`cursor_attempt_runner.py`, and application `attempt_service.py`,
`systemd_backend.py`, `cursor_evidence.py`, `cursor_workflow_service.py`,
`cursor_timeout_retry.py`, `contracts.py`, `history.py`, plus domain state/events
and authenticated completion envelopes. Read process/incremental capture,
attempt-executor, timeout-retry, correction and abort regressions.

## Cursor Rules And Skills

Follow `AGENTS.md` and the eight `.cursor/rules/` files listed in the overview:
governance, orchestrator, state/schema, loop/recovery, abort, Codex review,
docs/acceptance and global integrations. Read
`.agents/skills/create-cursor-plan/SKILL.md` and respect the separate read-only
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Keep those skills and root YAML unchanged. Update affected timeout-rule
statements to distinguish historical fixed, inactivity and hard behavior.

## Architecture Guardrails

- Follow the overview's exact activity definition, thresholds and retry policy.
  Keep adaptive behavior opt-in and fixed/Codex/create-chat behavior intact.
- Observe stdout while the runner captures it. No second full-transcript reader,
  scheduler polling loop or activity content persisted in public state.
- Use monotonic receipt time. Store only versioned policy/reason and bounded
  scalar diagnostics in authenticated completion metadata; raw events remain
  protected per-attempt artifacts with existing privacy and permissions.
- Transport never decides approval. Retry policy belongs to scheduler services,
  with exact completion/authentication, leases, reservation and cancellation.
- Signal and reap the owned process group using existing identity/control paths;
  never trust an event payload as a PID/command. Unknown termination retains holds.
- Keep runner deadline and systemd budget consistent in this same phase. No
  premature soft/fixed outer envelope for an adaptive Cursor turn.

## Implementation Plan

**T-01 — Incremental bounded activity observer.** Feed complete JSONL records
from the already-multiplexed stdout into a Cursor-specific observer, without
changing ordinary generic process callers. Handle split UTF-8/records and multiple
records per read; retain a bounded partial-record buffer and scalar timestamps,
not another transcript copy. Match the bound chat and supported event forms.
Malformed/oversize/noise events do not reset activity, but remain in ordinary raw
capture. Process complete received activity before making the corresponding idle
decision; check hard deadlines even while stdout continuously remains readable
so draining cannot starve termination. No activity from stderr or connection
retries, and no pending-tool exemption. Record parser limitations safely.

**T-02 — Production timing and termination.** Extend the production streaming
path used by `execute_prompt` with the accepted policy and monotonic start/last
activity. Apply the overview rule at `>=` boundaries, with hard priority, and
initialize inactivity at child launch. Remove the 24.1 capability guard when
the complete runnable path exists. Cover no-output, reasoning/tool activity,
successful early completion, deadlines coincident with exit and inherited open
pipes using bounded existing cleanup. A timed-out turn terminates/reaps the
group, persists one output capture, and produces its reason; avoid duplicated
partial stdout/stderr. Registration failures still clean up the child.

**T-03 — Frozen budget, authenticated reason and outer envelope.** Bind the
activity policy to the exact invocation and launch intent. For activity
`cursor.run_turn` requests, systemd `RuntimeMaxSec`/`TimeoutStartSec` use hard
plus existing finalization grace; create-chat/preflight/Codex/fixed requests use
their existing bounds. Do not derive the budget from a live YAML value.
Introduce a versioned timeout classification (`inactivity_timeout` or
`hard_timeout`, with historical fixed interpretation only where warranted).
Update runner metadata/outcome, writer/readers/semantic validators and affected
schemas together. Preserve coarse timeout envelopes if compatible; scheduler
must authenticate the detailed reason against the exact activity invocation.
Missing/contradictory reason in a new activity timeout cannot trigger auto-retry.
Systemd-only uncertain termination follows existing uncertainty handling, not a
synthetic runner timeout. Safe status/history expose reason and next action,
never reasoning/tool text or full identities.

**T-04 — Durable manual hard-timeout wait and ordinary inactivity retries.**
Extend `CursorTimeoutRetryService.record_timeout` and its real ingestion caller.
An authenticated hard timeout records a versioned manual-required decision,
keeps the current conversational checkpoint/reservation and automatic counter,
sets no retry due time and publishes no runnable retry effect/timer. Repeated
ticks/restarts do not launch another attempt. Use the existing timeout-wait
checkpoint rather than terminal `BlockedState`/Phase 23 successor recovery.
`scheduler cursor-retry RUN_ID` authorizes one same-run attempt with exact
chat/prompt/fix/B/budget/policy; repeated/concurrent commands deduplicate through
the existing fenced transition. After a new hard timeout, wait manually again.
Inactivity keeps three automatic retries and 30-minute delays; manual hard retry
neither invents a fourth inactivity retry nor resets consumed automatic budget.
Historical fixed outcomes retain existing behavior. Clear wait/reason metadata
on ordinary successful progression as appropriate.

Changed external-effect boundary: frozen invocation + owned process -> streamed
observation -> terminate/reap -> protected result -> authenticated scheduler
ingestion. Before confirmed termination, no retry and no release of ownership.
Ingestion atomically marks the attempt and creates either an inactivity retry
decision/effect or a hard manual wait. Replay cannot create duplicate effects.
An explicit manual command authorizes one later tick dispatch; it does not
invoke an agent or normalize staging. Abort wins over pending retry and late
results; existing holds remain until actual process/checkpoint reconciliation.
Once a manual or automatic retry succeeds, normal staging and exact-reviewer
review eventually complete the run. Do not accept safety-only indefinite stalls
as proof of complete continuation.

## Testing Criteria

Add `tests/unit/test_cursor_activity_timeout.py`,
`tests/unit/scheduler/test_phase24_2_timeout_evidence.py`,
`tests/unit/scheduler/test_phase24_2_timeout_retry.py` and
`tests/integration/test_phase24_2_activity_timeout.py`; extend process/attempt
regressions. Use injected clocks and actual accelerated fake subprocesses, no
multi-hour sleeps, fake model executables/backends and temporary native XDG/repos.
Synchronization barriers must establish receipt/decision/abort ordering rather
than rely on arbitrary sleep for race promises.

| Contract | Independent acceptance evidence |
| --- | --- |
| T-01 | Split records/UTF-8, multi-event reads, unknown/malformed/oversize records, wrong-chat events and connection/stderr noise do not incorrectly reset time. Supported thinking/assistant/tool events do. Sustained readable output cannot starve hard deadline. |
| T-02 | Production runner survives soft with recent activity and finishes successfully; original fixed cutoff would fail that test. No output cuts at soft when window is already elapsed, or at last activity + window after soft; no idle cut before soft. Hard cuts continuous activity. Silent pending tool cuts under the approved rule. Exact-boundary/exit and group cleanup/partial-output cases pass. |
| T-03 | Real attempt builder/backend argv uses hard + grace for adaptive turns; earlier legacy budget cannot kill them. Invocation/result reason authenticate; missing/tampered policy/reason fails before auto-retry. Historical fixed/create-chat/Codex budgets and envelopes are unchanged. Public output excludes event content. |
| T-04 | Real initial and correction ingestion reaches hard manual wait with no scheduled retry, retained dirty work/reservation, unchanged chat/B/review count/policy. Later ticks and restart remain idle. Real manual retry is idempotent and eventually succeeds through staging/review. Inactivity auto delay/exhaustion and historical fixed retries remain correct; abort and uncertain termination cannot authorize dispatch. |

Tests must drive actual services/CLI/runner, not hand-set a timeout checkpoint or
copy missing authenticated evidence. Existing no-flag timeout continuation and
forced non-timeout recovery must remain distinct.

## Validation

Run mandatory selected contracts and affected regressions, not repository-wide
pytest. Add concrete tests for changed state/projection callers where needed.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/test_cursor_activity_timeout.py tests/unit/scheduler/test_phase24_2_timeout_evidence.py tests/unit/scheduler/test_phase24_2_timeout_retry.py tests/integration/test_phase24_2_activity_timeout.py tests/unit/test_process.py tests/unit/test_process_incremental_capture.py tests/unit/test_cursor_output_and_envelope.py tests/unit/scheduler/test_attempt_executor.py tests/unit/scheduler/test_cursor_workflow_corrections.py tests/unit/scheduler/test_cursor_evidence_corrections.py tests/unit/scheduler/test_phase17_6_abort.py tests/integration/test_cursor_timeout_retry.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Replace 24.1's unsupported-admission test expectation with supported production
execution and rerun affected 24.1 tests. Report T-01–T-04 callers/tests, exact
executed/failed/unexecuted results and blockers in
`archive/implementation-history/findings/phase-24-2-activity-aware-execution-findings.md`.
After corrections rerun failed/affected tests only. 24.3 owns the separate full
pipeline gate; mandatory unfinished behavior/coverage blocks this slice.

## Risks Or Recovery Notes

Hard timeout evidence and scheduler continuation must land atomically in scope:
do not enable adaptive execution with old unconditional automatic retry logic.
A frozen hard threshold is per attempt; manual continuation grants a fresh
attempt, not a higher configured limit. Continuous reasoning may still be an
unproductive loop; hard provides the bound. Preserve existing control-plane
failure semantics and report uncertainty instead of inventing termination proof.

## OpenQuestions

None. The operator approved manual continuation after hard timeout.
