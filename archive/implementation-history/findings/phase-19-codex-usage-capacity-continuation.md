# Phase 19 Findings — Durable Codex Usage-Capacity Continuation

## Implemented contract

- Strict `usage_limit_exceeded` classification from recognized Codex JSONL error
  wrappers only (`response_schema.events_text_indicates_usage_limit_exceeded` and
  `runners.codex_failure`).
- Usage-limit recovery eligibility is fail-closed: timeout, truncation, bootstrap
  uncertainty, generic failure markers, and invalid outcomes block recovery even
  when quota JSONL is present (`is_codex_usage_limit_recovery_eligible`).
- Bounded `codex app-server --stdio` probe (`account/rateLimits/read`) behind
  `CodexCapacityProbePort` with typed `available` / `exhausted` / `unavailable`
  results; handshake uses `initialized` (not `notifications/initialized`);
  exchange parsing accepts Codex's headerless wire format (omitted `jsonrpc`
  header) while rejecting wrong versions; rejects malformed/duplicate/error/
  mismatched responses; validates every limit record before deciding availability
  (invalid records yield unavailability regardless of map order); bounded JSON
  line/nesting parsing and numeric validation reject oversized or out-of-range
  `usedPercent` values; legacy `rateLimits` is a single record while
  `rateLimitsByLimitId` is a map; null primary/secondary windows are absent; no
  persisted account telemetry.
- Production default probe is always `CodexAppServerCapacityProbe`; tests inject
  fakes explicitly and integration coverage exercises the concrete adapter against
  the fake executable.
- Shared DrvFS-safe subprocess environment policy
  (`scheduler.application.codex_subprocess_env`) applied to Codex review attempts
  and capacity probes.
- Durable scheduler state `waiting_codex_capacity`, events
  `codex_usage_capacity_detected` and `codex_capacity_available`, reducer
  transitions, SQLite tick eligibility, safe next action, and history redaction.
- `CodexWorkflowService` binds reviewer B before waiting, preserves reservation,
  probes once per tick while exhausted (no ledger growth), resumes through the
  existing fenced `codex.resume_review` effect path, and blocks on probe protocol
  failure.

## Test evidence

Focused and regression suites (fake `codex` / `agent` only):

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_response_schema.py \
  tests/unit/test_process.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_tick.py \
  tests/unit/scheduler/test_phase17_5_codex_corrections.py \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler tests/integration/test_phase17_5_*.py
```

Result (2026-09-12 correction turn 2): **118 passed** focused suite; **268 passed**
broader scheduler regression suite.

Static checks: `ruff format --check`, `ruff check`, `mypy src`, and
`git diff --check` clean after headerless-protocol, full-record validation, and
bounded parsing fixes.

## Real-account validation

Not performed. Phase 19 validation uses hermetic fake CLIs and injected probe
fixtures only, per plan guardrails.

## Residual risks

- `account/rateLimits/read` is an experimental app-server capability; protocol
  drift or CLI absence must remain a visible `codex_capacity_probe_unavailable`
  block rather than a hidden retry loop.
- Positive capacity is point-in-time; a concurrent client may consume quota before
  resume succeeds, returning the run to `waiting_codex_capacity` on the next
  confirmed `usage_limit_exceeded`.
- Continuity depends on the exact reviewer B session ID; manual account changes are
  supported only when the configured WSL CLI can still resume that session.
