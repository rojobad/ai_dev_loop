# Phase 20.5 Findings — Reliable Codex Limit Detection and Capacity-Probe Recovery

## Implemented Decision Table

| Evidence | Prior capacity lineage | Probe result | Next checkpoint |
| --- | --- | --- | --- |
| Structured `usage_limit_exceeded` in authenticated API envelope | ignored | not required | `waiting_codex_capacity` (`structured_error`) |
| Provider-message marker in terminal `error` / `turn.failed` wrapper | none | not required | `waiting_codex_capacity` (`provider_message_limit`) |
| Provider-message marker | prior inferred/message/probe wait only (`structured_error` alone does not qualify) | `EXHAUSTED` | `waiting_codex_capacity` (`post_failure_capacity_probe`) |
| Provider-message marker | prior inferred/message/probe wait only | `AVAILABLE` or `UNAVAILABLE` | `waiting_codex_review_retry` |
| Generic operational reviewer failure | any | `EXHAUSTED` | `waiting_codex_capacity` (`post_failure_capacity_probe`) |
| Generic operational reviewer failure | any | `AVAILABLE` or `UNAVAILABLE` | `waiting_codex_review_retry` |
| Capacity wait tick (automatic probe) | n/a | `AVAILABLE` | `awaiting_codex_review` via `codex_capacity_available` |
| Capacity wait manual authorization | n/a | not probed | `awaiting_codex_review` via `codex_capacity_retry_authorized` (idempotent per `capacity_wait_generation`) |
| Capacity wait tick with unavailable probe | n/a | `UNAVAILABLE` | remain in `waiting_codex_capacity` |

Manual `scheduler review retry` from `waiting_codex_capacity` never emits
`codex_capacity_available` unless an automatic tick probe observed `AVAILABLE`.

## Probe Transport and Cleanup

- Incremental stdout/stderr capture and JSON-line buffering are bounded; overflow
  terminates promptly and returns typed `OUTPUT_LIMIT` (before protocol/timeout).
- The exchange state machine fails closed in every phase: malformed JSON, invalid
  JSON-RPC headers, unexpected terminal responses, and limits before outbound
  completion all yield `PROTOCOL_ERROR`.
- Limits responses are accepted only after all `initialized` and
  `account/rateLimits/read` bytes have been written (`outbound_complete`).
- Process-group cleanup records the spawn PGID deterministically from
  `start_new_session=True` (`pid == pgid`), signals the group even when the
  leader has already exited, performs a bounded final PGID existence check with
  SIGKILL when needed (independent of the probe timeout deadline), closes all
  pipes, and reaps the direct child on every path. After the bounded TERM/KILL
  sequence, a final group and direct-child liveness check must succeed; otherwise
  `_InteractiveProbeResult.cleanup_failed` is set and the probe returns typed
  `UNAVAILABLE` with `CLEANUP_FAILURE` (never `AVAILABLE` or `EXHAUSTED`).
- JSON-RPC presence validation is fail-closed: accept `jsonrpc` only when absent
  or exactly `"2.0"`; explicit `null` is rejected. Notifications require the `id`
  member to be absent; explicit `null` is not a notification. Correlated terminal
  responses must be response-only (no `method`/`params`), and IDs must match the
  expected integer exactly (boolean `true` does not satisfy `id: 1`).
- Capacity parsing is exhaustion-first: any valid non-empty reached marker or any
  valid window at or above 100% wins even when the same record has a malformed
  reached marker or a sibling record is a non-object. Non-object or otherwise
  invalid siblings are preserved as evidence during the scan; only after finding
  no exhaustion does any invalid entry force `UNAVAILABLE`, so a valid below-100
  record plus a malformed sibling never returns `AVAILABLE`. A present but
  malformed/null `rateLimitsByLimitId` does not fall back to legacy `rateLimits`.
- `CLEANUP_FAILURE` takes precedence over other transport failure reasons when
  cleanup verification fails, while the public probe result remains `UNAVAILABLE`.
- Premature EOF is classified immediately when the child has exited and both
  streams are at EOF, without waiting for the full probe timeout.

## Test Evidence

Focused Phase 20.5 + retry/capacity unit slice:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py
```

Result: **137 passed**.

Phase 20.1–20.4 integration regressions:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase20_1_reviewer_retry.py \
  tests/integration/test_phase20_2_sequence_start.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/integration/test_phase20_4_sequence_end_to_end.py
```

Result: **13 passed**.

Full unit scheduler regression:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler
```

Result: **541 passed**, 2 skipped.

Static validation (correction turn):

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run python -m build
uv run mkdocs build --strict
```

All succeeded.

## Unperformed Real-Account Validation

No real `codex app-server --stdio` probe or live account reset was executed during
implementation or automated tests. Hermetic fakes, injected probes, and temporary
XDG homes were used exclusively.

## Residual Risks

- Message-only classification is intentionally permissive; safety relies on the
  read-only reviewer, unchanged Git state, and manual retry after capacity returns.
- The App Server protocol remains experimental; probe uncertainty stays typed as
  `UNAVAILABLE` rather than inferred `AVAILABLE`.
- Concurrent capacity consumption between probe and resume can still return a run
  to `waiting_codex_capacity` without consuming a completed review iteration.
