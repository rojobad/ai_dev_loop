# Phase 20.1.1 Findings — Retryable Codex Reviewer Failures

## Summary

Phase 20.1.1 adds `waiting_codex_review_retry`, post-failure capacity probing for
operational Codex review failures, `scheduler review retry <run-id>`, immutable-source
recovery for eligible historical `blocked` runs, attempt-unique Codex review artifact
paths, and systemd observation reconciliation for the owned attempt-runner timeout
exit code `124`.

Correction pass (2026-09-13): historical recovery now derives operational eligibility
from authenticated outcome evidence (not block-code alone), separates bootstrap
reviewer identity from failed resume-attempt authentication, authenticates frozen
config/runtime and Cursor final-response bytes via a bounded copy manifest, and adds
regressions for digest mismatch, tampered artifacts, oversized files, symlink paths,
and bootstrap→capacity-wait→resume blocked recovery.

Second correction pass (2026-09-13): fails closed when a digest-bound Codex review
result artifact disappears; authenticates frozen `context.codex.binding_artifact_path`
in the recovery manifest instead of optional unchecked copy; adds regressions for
missing/tampered reviewer input and removed bound review results.

Third correction pass (2026-09-13): removes unauthenticated legacy Cursor final-response
recovery. Pre-phase outcomes without envelope-authenticated `final_response_path` and
`final_response_sha256` bindings cannot recover; consistent replacement of unbound
events/final artifacts is rejected without successor creation or source mutation.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_attempt_executor.py::test_systemctl_show_parsing_matrix \
  tests/unit/test_response_schema.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_tick.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/integration/test_phase20_1_reviewer_retry.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Validation Results

- **334** scheduler unit tests passed (`tests/unit/scheduler`).
- Phase 20.1.1 focused unit, correction, and integration tests passed (**100** in the
  focused batch; **334** including full scheduler suite).
- `mypy src`, `ruff format --check .`, and `ruff check .` passed after third correction.
- SQLite schema version is **6** (`0006_review_retry.sql`).
- Owned attempt-runner timeout reconciliation: systemd `Result=exit-code` with
  `ExecMainCode=1` and `ExecMainStatus=124` maps to `TerminationClass.TIMEOUT`.
- No real blocked production run, timer, or global installation state was touched.

## Behavior Notes

- Structured `usage_limit_exceeded` still routes to `waiting_codex_capacity`.
- Integrity failures hard-block before capacity probes and before historical recovery.
- Historical recovery rejects integrity block kinds and re-validates operational
  eligibility from authenticated failed-attempt outcome evidence even when the legacy
  block code is `codex_review_outcome_invalid`.
- Bootstrap reviewer identity is authenticated from the original bootstrap attempt
  events digest recorded in the binding artifact; failed resume attempts must reference
  the same reviewer session ID.
- Recovery copies only manifest-authenticated artifacts, including frozen plan/prompt/
  config bindings, frozen fresh-reviewer input (`context.codex.binding_*`), Cursor
  final response bytes bound in the authenticated cursor outcome, and bootstrap
  events.
- `scheduler review retry` replays existing recovery successors and authorized retry
  generations before re-running eligibility analysis.

## Correction Notes

- Cursor attempt stdout/metadata records `final_response_path` and
  `final_response_sha256` for durable final-response authentication during recovery.
- Pre-phase blocked runs without those envelope-authenticated outcome bindings fail
  closed; events/final artifacts alone are not sufficient review-input evidence.
- Digest-bound Codex review result artifacts must exist as safe regular files; missing
  bound artifacts fail integrity checks before recovery or retry successor creation.
- `context.codex.binding_artifact_path`/`binding_sha256` are manifest-authenticated;
  recovery no longer performs unchecked optional copy of fresh-reviewer input.
- `get_codex_bootstrap_attempt()` resolves bootstrap evidence separately from the
  latest failed attempt.
- Recovery copy uses `read_verified_bytes` against a bounded manifest rather than raw
  file reads.

## Unresolved Compatibility Limitations

- Historical blocked runs whose authenticated cursor outcome lacks
  `final_response_path` and `final_response_sha256` cannot recover through Phase
  20.1.1 review recovery. The pre-phase artifact format did not record an
  independently authenticated digest for Cursor events or final text; deriving trust
  from present files would allow successor review-input substitution. Those sources
  remain inspect-only unless a future phase adds a durable authenticated binding at
  capture time.

## Residual Risks

- Capacity probe evidence is point-in-time, not causal proof.
- Historical recovery depends on durable ledger events and protected artifacts; missing
  evidence fails closed.
