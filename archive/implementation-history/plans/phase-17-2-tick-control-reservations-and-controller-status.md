# Phase 17.2 — Tick control, reservations, and controller observability

## Goal

Make the central ledger schedulable without external agents: controller A can
explicitly authorize a submitted run, query it by exact controller session and
repository, and a bounded one-shot tick can claim/reconcile synthetic work,
enforce one global active slot and reject a duplicate worktree reservation.

## Non-Goals

- Do not launch or observe Cursor/Codex, systemd units, Git, probes, or staging.
- Do not delete legacy roots or remove the legacy CLI surface.
- Do not auto-start a submitted run; `scheduler start` remains explicit.

## Scope

- Add durable `start` authorization, global tick-leader claim, run/effect claim,
  due-timer primitives, repository reservation, and a no-wait `scheduler tick`.
- Perform one real Git worktree admission preflight before the first Cursor launch:
  verify the resolved root equals the submitted repository target; capture branch,
  HEAD, and porcelain status as a protected admission artifact
  (`git/admission-status.txt`); enforce frozen `require_clean_worktree` only there;
  append a durable admission event/checkpoint. Do not add recurring baseline
  equality checks after admission merely because agents changed the worktree.
- Add `scheduler controller-status`/equivalent central read path keyed by exact
  controller session ID and repository; update the existing controller read path
  only if needed to route new scheduler runs unambiguously.
- Prove the tick with synthetic/in-memory effects only.

## Out of Scope

- Agent attempt schema/backend changes beyond fields already added in Phase 17.1.
- Any target-worktree mutation, process launch, systemd package template, abort,
  or public cutover/removal.

## Required Context

Read the Phase 17 master and Phases 17.1, 17.1.5, and 17.1.75 plans/results, all Cursor rules,
`locking.py`, `commands/controller.py`, `run_discovery.py`,
`pr_review_v2/application/engine.py`, `workers/effect_worker.py`,
`workers/supervisor.py`, and controller/locking test suites.

## Cursor Rules And Skills

Follow all `.cursor/rules/*.mdc` and `AGENTS.md`; the scheduler must preserve
controller A identity, the frozen fresh-B model/reasoning binding, and the rule
that no B exists until the first review. The docs governance skill applies to
doc changes; staged review is deferred.

## Architecture Guardrails

- `scheduler start` commits authorization before a tick can claim work. It never
  launches a process itself.
- A tick has a global lease/owner generation, processes a finite eligible snapshot
  in stable order, then exits. It may not sleep, poll, or retain locks after exit.
- A status query is read-only. It must match controller session plus resolved
  repository; zero/multiple matches never select by timestamp.
- One active repository reservation and one global active-agent slot are durable
  constraints, not process-local `flock` handles. This phase does not yet claim
  that an OS lock survives agent execution.
- CAS and claim fencing prevent stale ticks from applying an event after another
  tick, future abort, or later phase has superseded it.
- This phase carries the immutable reviewer binding only. It must not create,
  probe, resume, or report a B identity.

## Implementation Plan

1. Add typed start, lease, claim, timer, reservation, and read-status contracts
   to the scheduler domain/store. Ensure `queued -> authorized` is valid only
   for the recorded controller identity.
2. Implement a `TickService.run_once()` with injected clock/ID factory. It
   acquires/releases the leader lease, fires due synthetic timers, visits each
   eligible run once, and returns an aggregated redacted receipt.
3. Add synthetic test effects/state transitions rather than prematurely coupling
   to `workflow_engine`. Verify claim, completion fence, stale claimant, and
   future-due behavior entirely within central state.
4. Implement controller query by exact `(controller_session_id, repository_root)`
   and optional run ID. Render queue/authorized state, last event, safe next
   action, and no live worker claim.
5. Enforce the initial one-worktree reservation at submit/start and implement the
   one-shot worktree admission preflight before first Cursor launch. Enforce the
   one-global-agent-slot policy as a durable capacity value, even though Phase
   17.3 is the first phase to consume it.

## Testing Criteria

- Unit: leader contention/expiry/fencing, effect claim race, stale completion,
  due timer, stable ordering, no-op empty tick, and reservation conflict.
- Integration: submit -> start -> manual tick synthetic trace; two concurrent
  ticks; controller A query works during/after ticks; wrong controller/repo and
  ambiguous query are read-only failures.
- Assert no subprocess/Git call and no `sleep` invocation in the production tick
  path through injected fakes.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration/test_phase17_1_*.py tests/integration/test_phase17_2_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

This phase proves scheduler ownership, not process ownership. Do not use the
global capacity/reservation as evidence that a real child has been launched or
can be killed. A stale tick must leave an auditable rejected/stale event rather
than retrying its mutation.

## OpenQuestions

None. A/B `submit` + explicit `start`, one global slot, one worktree, and
controller-A read access are frozen decisions.
