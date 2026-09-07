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

## Scope

- Add attempt lifecycle persistence and a narrow `AgentProcessBackend` protocol.
- Implement the production systemd-user transient-unit adapter and a deterministic
  fake adapter for tests.
- Add packaged, parameter-free timer/service templates only as assets; no
  automatic enablement.
- Reconcile the launch-intent crash window using deterministic unit names.

## Out of Scope

- `scheduler tick` launching a real Cursor/Codex effect, agent prompt parsing,
  Git/staging, fresh-B identity creation, deletion of old paths, or Windows
  wake-up.

## Required Context

Read the master and Phases 17.1, 17.1.5, 17.1.75, and 17.2, all Cursor rules, `process.py`,
`launcher.py`, `abort_control.py`, `locking.py`, the PR-review supervisor/spawn
code, systemd guidance in `README.md`, and process/launcher regression tests.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` apply, especially subprocess, abort, privacy, and
state-schema rules. Follow docs governance for service documentation. Do not
invoke the staged-review skill during implementation.

## Architecture Guardrails

- Persist `launch_requested` before contacting systemd. Unit names derive only
  from safe opaque attempt IDs, never prompts, paths, session IDs, or shell text.
- `observe` returns authoritative unit lifecycle/main exit evidence and bounded
  artifact references. Bare PID polling or a reparented `Popen` child is invalid.
- The process manager owns cgroup timeout/termination; its adapter validates
  unit ownership before reporting liveness or terminating it.
- No SQLite transaction remains open across a systemd call or artifact parsing.
- Build argv arrays only; `shell=True`, dynamic unit snippets, leaked env, raw
  stdout/stderr in status, and unregistered running children are forbidden.
- The future agent unit must retain a worktree lock for its lifetime; this phase
  defines/tests the mechanism with a harmless fake command.
- The backend must be capable of carrying a future fresh-Codex bootstrap argv
  and later exact-resume argv, but it must not implement either reviewer effect
  or create an identity in this phase.

## Implementation Plan

1. Extend central schema/domain with typed attempt states, launch nonce, unit
   identity, result artifact refs, timeout/cancellation classification, and
   completion fence. Migrate from Phase 17.1 schema transactionally.
2. Define `AgentProcessBackend.launch/observe/terminate`; inject it into a small
   attempt application service. Implement fake backend scenarios for active,
   success, nonzero, timeout, kill, missing unit, and stale identity.
3. Implement the systemd adapter using safe argv calls to `systemd-run`/
   `systemctl --user` or an equally testable user-manager interface. Use a stable
   transient-unit ID per attempt and owner-only stdout/stderr/completion paths.
4. Make restart reconciliation exact: intent + extant unit adopts one attempt;
   intent + absent unit may launch once; a finished unit without verified result
   becomes uncertain, never duplicated.
5. Package service/timer assets for `scheduler tick` at 30 seconds, with no user
   paths/secrets. Add rendering/ownership validation only; enabling belongs to
   Phase 17.7 acceptance.

## Testing Criteria

- Unit: all lifecycle transitions, fence rejection, unit-ID safety, argv shape,
  output path permissions, timeout/cancel classification, and launch crash matrix.
- Integration: injected fake backend proves a tick exits while an attempt stays
  active and later reconciles exactly one stored completion; concurrent launch
  requests reuse the stable job.
- Asset tests verify the timer is 30 seconds, invokes a one-shot tick, contains
  no shell composition or sensitive values, and cannot be installed/enabled by a
  normal scheduler command.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/unit/test_process.py tests/unit/test_launcher_safety.py tests/integration/test_phase17_3_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

The dangerous gap is external launch accepted before durable acknowledgment.
Stable systemd identity plus precommitted intent is mandatory. Do not claim a
systemd adapter works from mocked command strings alone; Phase 17.7 requires a
separately authorized WSL fake-agent acceptance.

## OpenQuestions

None. Systemd user mode, a 30-second timer, WSL-active-only availability, and
one global attempt capacity are frozen.
