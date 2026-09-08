# Phase 17.2 — Tick control, reservations, and controller observability

## Goal

Make the central ledger schedulable without external agents: controller A can
explicitly authorize a submitted run, query it by exact controller session and
repository, and a bounded one-shot tick can claim/reconcile synthetic work,
enforce one global active slot and reject a duplicate worktree reservation.

## Non-Goals

- Do not launch or observe Cursor/Codex, systemd units, probes, or staging.
- Do not couple synthetic agent effects to `workflow_engine`, or implement a
  process backend. The sole Git scope is the injected, one-time admission
  preflight defined below.
- Do not delete legacy roots or remove the legacy CLI surface.
- Do not auto-start a submitted run; `scheduler start` remains explicit.

## Scope

- Add durable `start` authorization, global tick-leader claim, run/effect claim,
  due-timer primitives, repository reservation, and a no-wait `scheduler tick`.
- On the first authorized tick, perform one real Git worktree admission
  preflight. It is the durable gate that must complete before a later phase can
  launch its first Cursor attempt:
  verify the resolved root equals the submitted repository target; capture branch,
  HEAD, and porcelain status as a protected admission artifact
  (`git/admission-status.txt`); enforce frozen `require_clean_worktree` only there;
  append a durable admission event/checkpoint. Do not add recurring baseline
  equality checks after admission merely because agents changed the worktree.
  `TickService` receives this capability through a narrow Git-admission port;
  it must not invoke a subprocess directly. Production uses a bounded real-Git
  adapter and automated tests use a deterministic fake.
- Extend the existing `ai_dev_loop controller status` central read path, rather
  than adding a new `scheduler controller-status` command. It queries the
  central ledger by exact controller session ID and canonical repository root;
  it retains the existing legacy read path until cutover.
- Prove agent/timer/claim behavior with synthetic in-memory effects only. The
  one-shot Git admission is separately exercised through the fake admission port.

## Out of Scope

- Agent attempt schema/backend changes beyond fields already added in Phase 17.1.
- Any target-worktree mutation, process launch, systemd package template, abort,
  or public cutover/removal.

## Required Context

Read the Phase 17 master and Phases 17.1, 17.1.5, and 17.1.75 plans/results, all Cursor rules,
`locking.py`, `commands/controller.py`, `run_discovery.py`,
`pr_review_v2/application/engine.py`, `workers/effect_worker.py`,
`workers/supervisor.py`, `runners/git.py`, `commands/start_preflight.py`,
`scheduler/domain/admission_contract.py`, `scheduler/infrastructure/repository_target.py`,
and controller/locking/scheduler test suites.

## Cursor Rules And Skills

Follow all `.cursor/rules/*.mdc` and `AGENTS.md`; the scheduler must preserve
controller A identity, the frozen fresh-B model/reasoning binding, and the rule
that no B exists until the first review. The docs governance skill applies to
doc changes; staged review is deferred.

## Architecture Guardrails

- `scheduler start` commits authorization before a tick can claim work. It never
  launches a process itself. Its public form is
  `ai_dev_loop scheduler start <run-id> --controller-session-id <exact-A>`;
  the identity must equal the one frozen in the run or it appends no event.
- A tick has a global lease/owner generation, processes a finite eligible snapshot
  in stable order, then exits. It may not sleep, poll, or retain locks after exit.
- A status query is read-only. `controller status` combines central-ledger and
  legacy candidates during the transition, matches controller session plus
  canonical repository root, and applies an optional `run_id` only after that
  exact match. Zero/multiple candidates never select by timestamp.
- Submission owns the single active repository reservation. `start` verifies
  that reservation belongs to its run and never creates a second one. One global
  active-agent slot is a singleton durable capacity record with value `1`;
  Phase 17.2 exercises fenced synthetic acquisition/release and Phase 17.3 is
  the first phase allowed to bind it to an actual attempt. Neither constraint is
  a process-local `flock` handle or evidence that an OS lock survives an agent.
- CAS and claim fencing prevent stale ticks from applying an event after another
  tick, future abort, or later phase has superseded it.
- The first authorized tick may use the Git-admission port only while holding a
  valid per-run claim. It writes the protected artifact before appending the
  admission event/checkpoint; a failed, mismatched, or dirty admission blocks
  the run with a redacted safe action. Later ticks never re-run Git admission
  merely to police ordinary agent worktree evolution.
- This phase carries the immutable reviewer binding only. It must not create,
  probe, resume, or report a B identity.

## Implementation Plan

1. Evolve the scheduler domain from its queued-only v1 state into a strict union
   with at least `queued`, `authorized`, and an admitted/blocking form. Define
   typed authorization, admission, lease, claim, timer, reservation, capacity,
   and read-status events/contracts. `queued -> authorized` accepts only the
   recorded controller A identity; duplicate start by that A is idempotent and
   a mismatched identity changes nothing.
2. Add an ordered, transactional scheduler schema migration for the new durable
   lease/claim/capacity and state requirements. It must preserve v3 queued rows,
   their events, artifacts, and submit-created reservations exactly; reject
   checksum drift or unsupported future schemas. Seed exactly one durable global
   capacity record with value `1`; do not hand-edit or convert historic rows.
3. Implement `scheduler start <run-id> --controller-session-id <exact-A>` as
   the only authorization transition. It verifies the submit-created reservation
   belongs to the selected run, appends the authorization event/checkpoint, and
   returns a redacted next action. It launches neither a process nor Git.
4. Implement a `TickService.run_once()` with injected clock, ID factory, store,
   synthetic-effect executor, and Git-admission port. It acquires/releases the
   leader lease, captures a stable eligible snapshot, visits each run once, fires
   due synthetic timers, and returns an aggregated redacted receipt. It holds no
   database transaction or claim across a port call.
5. For an authorized run without an admission checkpoint, claim it and invoke
   the Git-admission port once. The bounded production adapter verifies the
   resolved root, records branch/HEAD/porcelain status only in
   `git/admission-status.txt`, checks the frozen clean-worktree policy, and
   returns typed evidence. Persist/hash the artifact, then atomically append the
   admission event and transition. Mismatch, dirty state when required, failed
   artifact verification, or a stale claim must block or be rejected without a
   retrying Git poll.
6. Add synthetic effects/state transitions rather than prematurely coupling to
   `workflow_engine`. Verify effect and capacity claim, completion fence, stale
   claimant, release, and future-due behavior entirely within central state.
   No Cursor/Codex/systemd command, process launch, or staging operation belongs
   in this phase.
7. Extend the existing `controller status` through a central-ledger read adapter.
   It combines transition-period legacy and scheduler candidates by exact
   `(controller_session_id, repository_root)` and optional `run_id`; zero or
   multiple candidates return a redacted read-only safe action. Render
   queue/authorized/admitted-or-blocked state, last event, capacity/claim facts,
   and safe next action without exposing a reviewer B or raw admission evidence.
8. Keep the submit-created worktree reservation authoritative throughout all
   new transitions. Test reservation conflicts at submit and ownership checks at
   start; do not defer reservation creation to start or add an OS-lock claim.

## Testing Criteria

- Unit/domain/store: authorization identity/idempotency, ordered migration from
  a populated v1 ledger with a v3 queued run, leader contention/expiry/fencing,
  effect and capacity claim races, stale completion/release, due timer, stable
  ordering, no-op empty tick, and submit-time reservation conflict.
- Unit/admission: use a deterministic fake Git-admission port to prove root
  mismatch, clean and dirty status under both frozen policies, protected artifact
  hash/write ordering, one-shot admission, and stale-claim refusal. Assert that
  `TickService` has no direct subprocess or `sleep` call; only the production
  adapter may invoke the bounded Git command.
- Integration: submit -> authorized start -> manual tick with fake admission
  plus synthetic trace; two concurrent ticks; controller A query works during
  and after ticks; wrong controller/repository, ambiguous central/legacy lookup,
  and optional wrong `run_id` are read-only failures.
- Assert that no test invokes real Cursor, Codex, systemd, network, or target
  worktree mutation. Fake Git admission is the only test boundary for this
  phase's production Git behavior.

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
than retrying its mutation. The initial admission guards only the worktree
before its first future Cursor launch; it is not a continuing Git-baseline
watcher. Preserve the legacy controller read behavior during the transition and
make a combined lookup ambiguous rather than guessing a scheduler or legacy run.

## OpenQuestions

None. A/B `submit` + explicit `start`, one global slot, one worktree, and
controller-A read access are frozen decisions.
