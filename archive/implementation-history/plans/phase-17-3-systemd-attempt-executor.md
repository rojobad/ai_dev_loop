# Phase 17.3 — Systemd attempt executor

## Goal

Add the durable execution boundary that lets a short scheduler tick launch,
observe, reconcile, and terminate one systemd-owned external attempt without a
long-lived Python run worker. This phase proves the mechanism with hermetic test
commands, not Cursor or Codex workflow effects.

## Non-Goals

- Do not invoke real Cursor/Codex or wire any local-loop effect to the backend.
- Do not enable/install a user unit on the developer machine or change target
  repositories.
- Do not implement public abort behavior, usage-limit recovery, or cutover.
- Do not alter the fresh-Codex reviewer sandbox/test-access change introduced
  before this phase; its final policy decision belongs after Phase 17.

## Scope

- Add attempt lifecycle persistence and a narrow `AgentProcessBackend` protocol.
- Implement the production systemd-user transient-unit adapter and a deterministic
  fake adapter for tests.
- Add packaged, parameter-free timer/service templates only as assets; no
  automatic enablement.
- Reconcile the launch-intent crash window using deterministic unit names,
  existing Phase 17.2 capacity ownership, and one worktree lock retained by the
  transient attempt.

## Out of Scope

- `scheduler tick` launching a real Cursor/Codex effect, agent prompt parsing,
  Git/staging, fresh-B identity creation, deletion of old paths, or Windows
  wake-up.

## Required Context

Read the master and Phases 17.1, 17.1.5, 17.1.75, and 17.2; all Cursor rules;
the scheduler v2 domain, tick, fencing, store, and `0002_tick_control.sql`;
`process.py`, `launcher.py`, `abort_control.py`, `locking.py`; the PR-review
supervisor/spawn code; systemd guidance in `README.md`; and the scheduler,
process, launcher, and locking regression tests. The starting scheduler schema
is v2: it already persists `queued`/`authorized`/`admitted` control state,
admission claims, repository reservations, and the single global active-agent
capacity row.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` apply, especially subprocess, abort, privacy, and
state-schema rules. Follow docs governance for service documentation. Do not
invoke the staged-review skill during implementation.

## Architecture Guardrails

- Migrate transactionally from the Phase 17.2 scheduler v2 schema to the next
  version. Preserve all existing run states, events, admission claims,
  reservations, and capacity data; do not reinterpret or reset them.
- Atomically persist `launch_requested`, its stable opaque attempt/unit identity,
  and acquisition of the Phase 17.2 global capacity slot before contacting
  systemd. The holder remains reserved until a verified terminal/reconciled
  outcome durably releases it; a tick never launches an agent without that
  ownership.
- Unit names derive only from safe opaque attempt IDs, never prompts, paths,
  session IDs, or shell text. The unit working directory and artifact paths are
  derived from already validated scheduler bindings, not newly parsed input.
- `observe` returns authoritative unit lifecycle/main exit evidence and bounded
  artifact references. Bare PID polling or a reparented `Popen` child is invalid.
- The process manager owns cgroup timeout/termination; its adapter validates
  unit ownership before reporting liveness or terminating it.
- No SQLite transaction remains open across a systemd call or artifact parsing.
- Build argv arrays only; `shell=True`, dynamic unit snippets, leaked env, raw
  stdout/stderr in status, and unregistered running children are forbidden.
- The transient unit retains its worktree lock for its complete lifetime by
  wrapping the already validated agent argv in a direct `flock --exclusive
  --nonblock <private-lock-path> -- ...` argv. The private lock path derives
  from the resolved worktree key; no shell, interpolation, user-controlled lock
  path, or Python parent-held substitute is allowed.
- The backend must be capable of carrying a future fresh-Codex bootstrap argv
  and later exact-resume argv, but it must not implement either reviewer effect
  or create an identity in this phase.

## Implementation Plan

1. Add and test one transactional scheduler v2-to-v3 migration. Extend the
   typed scheduler state/events/store with attempt identity, launch nonce,
   unit identity, bounded result-artifact references, timeout/cancellation
   classification, completion fence, and capacity-holder linkage. Preserve all
   pre-existing scheduler rows and state semantics.
2. Define `AgentProcessBackend.launch/observe/terminate` and a small attempt
   application service. In the same short database transaction that records
   `launch_requested`, acquire or prove ownership of the global capacity slot;
   then commit before the external call. Persist only verified terminal evidence
   and release the slot in the corresponding fenced transaction. Implement fake
   scenarios for active, success, nonzero, timeout, kill, missing unit, stale
   identity, and unavailable capacity.
3. Implement the systemd-user adapter behind an injectable command boundary.
   Use safe argv calls to `systemd-run` and `systemctl --user`, one stable
   transient-unit ID per attempt, a validated working directory, owner-only
   artifacts, and direct `flock` argv wrapping around the harmless fake-agent
   command. Never generate a shell command or a dynamic unit file.
4. Make restart reconciliation exact and fenced: intent plus an extant owned
   unit adopts one attempt; intent plus a proven absent unit may issue the one
   permitted launch; a finished unit without a verified result becomes
   `uncertain` and holds/reconciles the capacity rather than duplicating work.
   Stale observations, completions, and releases must be rejected.
5. Package parameter-free service/timer assets for one `scheduler tick` every
   30 seconds, with no user paths or secrets. Add rendering and ownership
   validation only. Do not install or enable them; real systemd fake-agent/timer
   acceptance is deferred to Phase 17.7.

## Testing Criteria

- Unit: v2-to-v3 migration preservation/rollback/checksum behavior; all
  lifecycle transitions; fence rejection; safe unit-ID/lock-path/argv shape;
  output path permissions; timeout/cancel classification; capacity ownership and
  release; and the launch crash matrix.
- Integration: an injected fake backend proves a tick exits while one attempt
  stays active and later reconciles exactly one stored completion. Concurrent
  launch requests must either adopt the stable attempt or observe unavailable
  global capacity; they may never start a second unit.
- Asset tests verify the timer is 30 seconds, invokes a one-shot tick, contains
  no shell composition or sensitive values, and cannot be installed/enabled by a
  normal scheduler command.
- Automated tests use only fake `systemd-run`/`systemctl`, fake agent commands,
  and temporary scheduler state. They must not invoke a real user service,
  Cursor, Codex, network service, or enable/install timer.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/unit/test_process.py tests/unit/test_launcher_safety.py tests/integration/test_phase17_3_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

The dangerous gap is external launch accepted before durable acknowledgment or
without the global capacity holder. Stable systemd identity, precommitted
intent/capacity, and a cgroup-held worktree lock are mandatory. Do not claim a
systemd adapter works from mocked command strings alone; Phase 17.7 requires a
separately authorized WSL fake-agent/timer acceptance.

## OpenQuestions

None. Systemd user mode, a 30-second timer, WSL-active-only availability, and
one global attempt capacity are frozen.
