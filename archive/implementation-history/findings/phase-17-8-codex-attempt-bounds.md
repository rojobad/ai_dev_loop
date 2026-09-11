# Phase 17.8 findings: Codex attempt bounds and systemd runtime budget

## Diagnosis (safe summary)

Manual smoke on a production target showed a scheduler Codex worker exiting after
about 77 seconds with exit code 124 and `timed_out: true`, while the frozen Codex
review budget was 90 minutes. The root cause was output capture treating a JSONL
events stream just above the prior 256 KiB bound as a synthetic timeout. Separately,
systemd units received a generic one-hour runtime cap instead of the frozen workflow
budget.

The preexisting blocked run on that target was **not** modified, retried, resumed,
aborted, or recovered during this phase.

## Changed contracts

- `StreamingProcessResult` distinguishes `timed_out` from `stdout_truncated` /
  `stderr_truncated` with bounded captured-byte counts.
- Default bounded capture still terminates on limit (non-timeout exit 2). Scheduler
  Codex reviews opt into `drain_after_limit` so the child can finish while retaining
  at most `MAX_CODEX_EVENTS_ARTIFACT_BYTES` (8 MiB) of JSONL.
- Codex stdout capture limit aligns with `MAX_CODEX_EVENTS_ARTIFACT_BYTES`; stderr
  retains its separate bound.
- Truncation without a valid schema review blocks with `codex_review_output_truncated`;
  bootstrap uncertainty is classified before timeout or truncation; a genuine timeout
  blocks with `codex_review_timeout` when reviewer identity is unambiguous, even when
  truncation diagnostics are also present.
- Production Codex attempt timeouts derive only from frozen `codex_timeout_minutes`
  in authenticated invocation evidence; no process-environment test overrides exist
  in production code.
- `LaunchRequest.execution_timeout_seconds` propagates frozen `cursor_timeout_minutes`
  or `codex_timeout_minutes` from submitted state. Systemd sets explicit
  `TimeoutStartSec` and `RuntimeMaxSec` to that budget plus bounded finalization
  grace, validates `stop_grace_seconds` before emitting `TimeoutStopSec`, and rejects
  non-positive stop-grace values.

## Validation results

Recorded after the second review-correction pass (env-override removal and ingest
classification ordering).

Focused tests (`TMPDIR=/tmp TMP=/tmp TEMP=/tmp`):

```text
86 passed in 99.53s
```

Full suite:

```text
567 passed, 1 skipped in 178.59s
```

Quality gates (all passed):

- `uv run python -m ruff format --check .`
- `uv run python -m ruff check .`
- `uv run python -m mypy src`
- `uv run python -m build`
- `uv run mkdocs build --strict`
- `git diff --check`

## Real-model / systemd acceptance

Not performed in this phase. Post-merge operator smoke should use a disposable
target repository, a fresh reviewer B, and inspect only safe status/history
summaries.

## Residual risk

- Behavior fixes apply to future attempts only; historically blocked runs require
  an explicit operator decision (non-destructive abort + fresh submit).
- Draining after the artifact cap trades full trace retention for process liveness;
  valid schema output and exact reviewer identity remain mandatory.
- Control-plane changes to process lifecycle and systemd argv generation require
  human review before commit.
