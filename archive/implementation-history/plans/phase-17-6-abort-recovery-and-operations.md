# Phase 17.6 — Abort, recovery hardening, and operations

## Goal

Harden the complete scheduler loop for operational use while WSL is running:
durable abort, explicit safe recovery/blocking states, controller and operator
observability, attempt/repository reconciliation, and factual timer operations
documentation.

## Non-Goals

- Do not delete legacy XDG state or remove public legacy/PR-review code yet.
- Do not auto-retry an unclassified/ambiguous process outcome, switch models, or
  perform tool updates.
- Do not install/enable a real systemd user service in automated tests.

## Scope

- Implement central scheduler abort and terminal fencing across active systemd
  attempts and between ticks.
- Complete status/history/list/controller read models and redacted operations
  diagnostics.
- Reconcile stale reservations, attempts, claims, timers, and capacity after
  restarts; preserve the special verified usage-limit retry behavior.
- Package/validate timer operational interfaces, still without enabling them.

## Out of Scope

- Cutover cleanup, deletion, command removal, PR-review workflows, Windows wake,
  real model use, target Git destructive operations, and config/skill changes.

## Required Context

Read the master and Phases 17.1–17.5, all Cursor rules, `abort_control.py`,
`commands/abort.py`, `locking.py`, `launcher.py`, PR-review claim/lease/control
implementations, `docs/operacion/{observabilidad,seguridad-privacidad,troubleshooting}.md`,
and abort/process/privacy regression suites.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` apply. Prioritize abort contracts, late-result fences,
privacy, and docs governance. The staged-review skill remains a post-implementation
review activity.

## Architecture Guardrails

- Abort is durable first: append cancellation/invalidate claims, then terminate
  only the exact owned systemd unit/cgroup after validated identity. Preserve all
  target/staged files and diagnostic artifacts.
- A late agent result after cancellation, lease loss, supersession, or terminal
  state is stale evidence only; it cannot advance the run.
- Reconciliation distinguishes active, proven completed, missing-before-launch,
  timeout, cancelled, and ambiguous states. It must never guess success from a
  log file or retry a possibly mutating incomplete turn.
- Controller A queries are read-only and session/repository bound. Status/history
  never expose prompt/patch/review content, full session IDs, raw unit/PID data,
  environments, or command argv.
- The 30-second timer is eventual progress only; no code claims WSL wake-up.

## Implementation Plan

1. Add central `scheduler abort` and controller/operator status/history/list
   commands. Render exact safe actions: wait-until, usage-limit retry time,
   inspect blocked artifact class, or no action.
2. Connect cancellation to systemd backend stop semantics and attempt/result
   fences. Cover abort before launch, active Cursor/Codex, exited-before-ingest,
   stale metadata, and repeated abort.
3. Implement startup/tick reconciliation for expired leader/effect claims,
   reservations, capacity, pending attempts, missing units, verified completion,
   and uncertain outcomes. Release capacity/reservation only at proved safe
   boundaries.
4. Add bounded, redacted event history and operational logs. Preserve all raw
   sensitive evidence solely in protected artifacts.
5. Document manual `systemctl --user` inspection, timer status, status queries,
   retry/blocked/abort behavior, and WSL-active-only limitation. Do not state
   that manual systemd acceptance has already happened.

## Testing Criteria

- Unit: cancellation/state transitions, stale fences, claim/lease expiry,
  safe-action projection, redaction, and capacity/reservation release.
- Fake-backend integration: abort all lifecycle positions; process identity
  mismatch refuses signaling; restart after claimed/active/finished attempt;
  late result rejected; controller query works while Cursor/Codex is active.
- Privacy regression: inject prompts, patches, session IDs, stdout/stderr, and
  fake unit IDs into artifacts and prove human/JSON output remains redacted.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/test_process.py tests/unit/test_locking.py tests/integration/test_launch_worker.py tests/integration/test_abort.py tests/integration/test_phase17_6_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

## Risks Or Recovery Notes

Do not mistake a timer or central DB for permission to replay external work.
Uncertainty is an intentional stop condition. The final cutover is prohibited
until all abort/recovery and privacy traces pass with fake systemd ownership.

## OpenQuestions

None. The master fixes conservative recovery, systemd ownership, controller
observability, and WSL-active-only behavior.
