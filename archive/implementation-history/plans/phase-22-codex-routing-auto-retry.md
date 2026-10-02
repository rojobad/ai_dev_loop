# Phase 22 — Bounded automatic retry for Codex workspace routing failures

## Goals

Recognize the observed Codex terminal error `workspace routing discovery timed out`
and automatically retry the same review through scheduler ticks, at five-minute
intervals, for at most twelve automatic retries per review iteration. Preserve
the authenticated reviewer, frozen configuration, reviewed artifacts, and durable
audit trail. Expose why and when another attempt will occur.

The user approved this narrow automatic-retry policy on 2026-10-02. It supersedes
historical manual-only instructions only for this classified same-run failure.

## Non-Goals

- Diagnose or repair OpenAI networking, authentication, routing, or quota services.
- Retry all invalid outcomes, network warnings, process timeouts, or model errors.
- Guarantee completion within an hour: twelve five-minute delays exclude attempt
  duration, capacity contention, and time without scheduler ticks.
- Create a second reviewer or automatically recover a blocked run via a successor.

## Scope

Classification at the existing Codex runner/evidence boundary; typed durable retry
policy state and events; tick scheduling; capacity-observation diagnostics; safe
inspection output; compatibility; automated tests; current operation documentation
and applicable Cursor rules. Use existing states and dispatch/reconciliation paths
where possible, without a new daemon, timer, public command, or YAML setting.

## Out of Scope

Do not modify `ai_dev_loop.yaml`, planning/review skills, frozen submitted context,
Codex binaries/config/auth files, integrations/hooks/session bridges, real runs or
their artifacts, actual workstation systemd timers, WSL lingering, GitHub/remotes,
Cursor retry policy, blocked-source recovery, or sequence checkpoint semantics.
Do not stage, commit, push, or execute this plan as part of planning. Implementation
does not authorize live retries, deployment, service installation, or tool updates.

## Required Context

Read `AGENTS.md`, the Cursor rules listed below, and relevant current docs:
`docs/referencia/cli.md`, `docs/referencia/integration-api.md`,
`docs/operacion/estado-artefactos.md`, and `docs/operacion/timer-systemd-wsl.md`.
Historical Phase 19, 20.5, and 20.8 plans/findings explain existing capacity and
manual retry behavior; current code and tests control prerequisite verification.

Established prerequisites to recheck before dependent implementation:

- `scheduler/application/codex_workflow_service.py`: authenticated outcome ingestion,
  operational failure routing, post-failure capacity probe, review retry wait,
  capacity wait, and review scheduling.
- `scheduler/codex_attempt_runner.py`, `application/codex_evidence.py`, and
  `application/codex_argv.py`: per-attempt output, integrity, bootstrap identity,
  and exact-session resume. Bootstrap can bind B even when no review is produced.
- `application/review_retry.py`: process-free manual same-run authorization,
  authenticated checkpoint verification and per-failure-generation deduplication.
- `application/tick.py`, `tick_fencing.py`, `attempt_service.py`,
  `infrastructure/sqlite_store.py`: lease/fences, effect dispatch, reservations,
  global agent capacity, and `waiting_codex_review_retry` already tick eligible.
- `domain/state.py`, `events.py`, `reducer.py`, applicable schemas and migrations:
  typed state transitions and completed-review accounting.
- Unit tests for Phase 19 capacity, Phase 20.5 detection, Phase 20.8 retries and
  concurrency; integration tests for attempt ticks, Phase 20.8 retry, and Phase
  21.6 capacity. Existing fake backends, fake CLIs, injected clocks and isolated XDG.

Audit evidence, summarized without identities or raw transcripts: both
`ai-dev-loop-hub-60fb1596fafe` and `ai-dev-loop-hub-ce364712d4d9` exited with code 1
after about 233 seconds, emitted terminal `turn.failed` with the exact routing
message, produced no review result, and entered manual retry wait. Later probes
reported 51% remaining in the five-hour window. This does not establish quota at
failure time. Tests must use sanitized terminal-event fixtures, never live data.

## Cursor Rules And Skills

Follow `AGENTS.md` and these repository-local rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc` for integration
  output/privacy and the prohibition on installation side effects.

No `.cursor/skills/` exists. The relevant repository review skill is
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; preserve its review
contract and do not edit it. Correct applicable rules to describe the approved
narrow automatic exception, without loosening manual blocked-source recovery.

## Architecture Guardrails

- Ledger and protected artifacts remain authority. Classification consumes bounded,
  authenticated attempt evidence, not logs copied from a different session or an
  agent's review prose. Transport parsing stays outside pure domain reducers.
- Require exact bound B and confirmed termination before retry. Integrity, abort,
  ambiguous termination, missing identity, or conflicting evidence take precedence.
  Keep repository reservation during waits/uncertainty, release global active-agent
  capacity after confirmed termination, and converge via existing reconciliation.
- Keep frozen executable/model/reasoning, sandbox policy, iteration, Cursor chat,
  staged checkpoint and deterministic review context. Operational attempt retries
  do not increment completed reviews or repeat Cursor implementation/staging.
- Use transactional transitions, lease revalidation and existing effect fences;
  no sleeps inside ticks, shell commands, direct JSON edits, or autonomous Git work.
- Default outputs contain allowlisted diagnostic codes, counters, times and artifact
  references, never raw errors/transcripts, credentials or full session IDs.

## Implementation Plan

### Mandatory contracts

| ID | Production behavior and acceptance outcome |
| --- | --- |
| C-01 | Runner/evidence parser classifies an unsuccessful completed attempt whose terminal structured `turn.failed.error.message` is exactly `workspace routing discovery timed out` as `codex_workspace_routing_timeout`. Leading/trailing whitespace may be trimmed; do not substring-match arbitrary output. Reconnecting progress alone, stderr model/plugin warnings, generic timeout, valid review, auth errors and quota errors never authorize this policy. Preserve integrity/identity/structured quota precedence. |
| C-02 | Workflow durably records eligibility, failure generation, failed attempt, UTC next-attempt time and automatic retry count. First automatic retry is due 300 seconds after failure ingestion; a subsequent eligible failure schedules 300 seconds after its ingestion. Maximum is twelve automatic authorizations per review iteration, separate from completed-review counters and manual retries. |
| C-03 | Real `TickService.run_once()` authorizes at most one retry for the current failure generation at/after its due time. Before then it launches none. Replayed ticks, restart, expired leases, capacity contention and manual/automatic races create no duplicate intent or attempt and consume the automatic count only once per durable automatic authorization. Dispatch uses ordinary fenced effect/attempt machinery. |
| C-04 | Same-run retry preserves exact authenticated B, iteration, frozen executable/model/effort, patch and review context; supports failures after initial bootstrap has bound B. No identity/termination/integrity proof means no automatic retry. Abort and non-current/aborted sequence leaves prevent authorization/launch. A successful authenticated review returns to the ordinary review/fix/sequence flow. |
| C-05 | Post-failure probe stores safe typed status and reason alongside classified failure. `exhausted` uses existing capacity wait; `unavailable`, including timeout, does not veto the eligible routing retry. `available` permits it. No fabricated quota diagnosis. Preserve existing capacity-resumption behavior and do not add a second parallel retry path. |
| C-06 | After twelve automatic authorizations, another routing failure stays in manual retry wait with an explicit exhausted-policy reason and existing manual command. Manual retry can advance an earlier wait, shares generation deduplication and does not replenish automatic allowance. Reset count only after a valid completed review begins the next review iteration; never via restart, reclassification or capacity wait. |
| C-07 | Status/history and relevant existing integration projections show automatic eligibility, attempts used/limit, due time, failure code and probe status/reason, or manual next action. Keep schemas/projections/privacy aligned without unrelated API redesign. Historical snapshots/events remain readable; historical waits receive no implicit automatic authorization or evidence backfill. |

### Work sequence

1. Recheck prerequisites and locate all production readers/writers of changed
   state/outcome/event shapes. Add an isolated classifier and sanitized fixtures
   for the observed terminal event, negatives and precedence (C-01).
2. Add typed retry metadata/events and persistence through existing state reducers
   and store. Freeze this policy's constants as 300 seconds and 12; no public
   configuration surface. Keep it a runtime retry policy, not a rewrite of frozen
   submission inputs. Record only newly ingested eligible failures (C-02, C-05).
3. Wire due-time reconciliation into Codex workflow under the real tick caller.
   Share the manual retry checkpoint-verification and authorization primitive
   rather than invoke CLI recursively or blindly call operator retry. Carry
   source (`manual`/`automatic`) in a typed durable event, recheck current generation,
   attempt termination, sequence/abort and lease inside commit. Persist authorization,
   count increment and transition atomically; use existing effect deduplication
   afterward. Finish resume/ingest/success paths, not just the timer helper (C-03/04).
4. Preserve capacity wait and manual override, enforce exhaustion, and clear only
   completed-iteration scheduling metadata while retaining audit evidence (C-05/06).
5. Update safe actions, status/history/integration projections, typed schemas,
   documentation and applicable rules; complete compatibility tests (C-07).
6. Run validation and report every contract with production path and test evidence.

### Persistence, replay and rollout contract

Canonical new metadata: explicitly typed eligibility boolean, nonnegative integer
count (reject bool/string/fraction), policy interval/limit or version identifying
the fixed policy, nullable UTC due timestamp, classified diagnostic and nullable
typed probe status/reason. Use repository datetime serialization conventions.
Validate cross-field consistency: automatic due time requires eligible current
generation, bound reviewer and remaining allowance. Choose field names mechanically
to fit existing models; no opaque unvalidated dictionary of retry data.

Historical missing policy fields default to inactive/zero/null; null means no
schedule, never immediate retry. Historical diagnostic fields may be absent/null.
New canonical writers emit explicit values. Keep genuine pre-change fixtures to
prove model/store readers and inspection accept old state without migration on
read-only commands. Add a normal schema migration only if storage structure needs
one; never rewrite protected historic outcomes, infer old eligibility or edit the
live ledger. Existing manual waits stay manual until a future explicitly authorized
attempt yields a newly classified failure under this implementation.

Transition: terminated authenticated failed attempt -> durable eligible wait ->
due-time authorization transaction -> awaiting review -> existing fenced dispatch
-> exact-session resume -> authenticated result ingestion. Crash before transaction
leaves due wait; crash after commit preserves one consumed authorization and resumes
ordinary pending-effect reconciliation. Uncertain dispatch retains ownership and
does not create another retry; after outcome resolves, normal ingestion converges.
Abort cancels future work through existing abort reconciliation, without deleting
evidence, modifying Git or releasing reservations while owned work remains uncertain.
Capacity-only transitions cannot reset the allowance or authorize duplicate work.

## Testing Criteria

Automated unit, integration and regression tests are mandatory. Add focused
`tests/unit/scheduler/test_phase22_codex_routing_auto_retry.py` and
`tests/integration/test_phase22_codex_routing_auto_retry.py`, plus parser, schema,
inspection and existing retry test updates where appropriate. Fake Codex/Cursor,
probe ports and process backends only; temporary repos/XDG and injected UTC clock.

- C-01: sanitized exact terminal fixture; progress-only, unrelated stderr, quoted
  text inside tool/review content, malformed/truncated evidence, quota/auth error,
  valid result, missing/conflicting identity and integrity failures are negatives.
- C-02/03: fail through production runner/ingestion, tick at 299 seconds (none),
  300 seconds (one), repeat same tick (none); restart service/store and assert
  due time/count persist. Progress twelve automatic retries and verify no thirteenth.
  The original regression test must fail on baseline because tick never resumes
  the eligible manual-wait state, not because a test adds the transition itself.
- C-03/04: deterministic barriers for manual-versus-tick and expired-lease races;
  crash after authorization before dispatch, busy global capacity, uncertain
  launch, abort before due and before launch. Assert one intent/count/attempt,
  reservation retention and eventual convergence after uncertainty resolves.
- C-04: authenticated failed bootstrap with binding resumes exact B (no second
  bootstrap); normal later review; standalone and active sequence final/non-final
  phase. Verify no Cursor reexecution, completed review inflation, unintended
  checkpoint/phase advance or altered frozen values. Verify stale/aborted leaves.
- C-05/06: available/exhausted/unavailable-timeout probes, capacity recovery without
  count reset, exhausted allowance and manual override before/after due. Successful
  completed review and new iteration have independent allowance; invalid results
  do not reset it. Preserve existing non-routing manual failure behavior.
- C-07: historical fixture reads cause no automatic scheduling; invalid field
  forms/cross-field combinations rejected; status/history/integration privacy and
  truthful automatic/manual safe next actions. Probe reason survives restart.

## Validation

Use the repository Python 3.11+ virtual environment; do not use system Python.
Run and report exact commands/results, including failures/unexecuted checks:

```bash
.venv/bin/python -m pytest tests/unit/scheduler tests/integration/test_phase22_codex_routing_auto_retry.py tests/integration/test_phase20_8_sequence_review_retry.py tests/integration/test_phase17_3_attempt_tick.py tests/integration/test_phase21_6_capacity.py
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Include schema/inspection tests in full suite. Do not claim completion from test
counts: map C-01..C-07 to implementation, actual production caller and independent
assertions. Mandatory unwired behavior or missing tests means incomplete scope.
No live model calls, scheduler run starts/ticks/retries, timer changes or integration
installation in validation; tests use isolated fake environments only.

## Risks Or Recovery Notes

Repeated attempts can consume quota; fixed delay and limit bound this policy.
The interval is a minimum due-time delay, not a precise workstation timer. A
capacity timeout means unknown; record it instead of misdiagnosing exhaustion.
Exact classification is intentionally narrow and may miss future changed CLI
messages; keep those failures manual rather than broaden matching speculatively.
Existing waits do not auto-resume on upgrade. Pending authorized effects remain
durable, so rollout/removal must not pretend to cancel them by deleting metadata.
No real-run repair is part of Cursor execution. Preserve historical manual retry
and blocked recovery semantics outside this explicit exception.

## OpenQuestions

None.
