# Phase 17 Pre-cutover Acceptance Findings

Date: 2026-09-09

## Summary

Phase 17.6.5 repaired the committed formatting defect in
`src/ai_dev_loop/scheduler/application/status.py`, corrected three full-suite
test regressions, ran the full automated validation set with fake agents only,
and produced this consolidated pre-cutover evidence record for Phases 17.1–17.6.

**Cutover status: ELIGIBLE (automated gate).** The full validation command set
passed on the current working tree. Phase 17.7 may begin implementation planning
and code work only after independent human acceptance review for control-plane
and destructive changes. This artifact is still not authority to delete user
state or enable a production systemd timer.

This artifact is an audit record only. It is not scheduler state, not a manifest,
not test output authority, and not permission to delete user state or enable a
timer without the separate human-acceptance step required by the Phase 17.7
plan.

## Baseline Revision

Parent commit on branch `rba/phase_17`:

```text
8361d961270371f9a66a9003a2c591b41a595f60
```

Validation was executed on the working tree that includes uncommitted Phase 17.6.5
remediation: the formatter repair in `status.py`, test fixes in
`tests/unit/test_probes.py` and
`tests/integration/test_phase16_4_acceptance_matrix.py`, this findings artifact,
and Phase 17.7 gate wording updates.

## Formatter Remediation

`ruff format --diff` on the designated module showed one mechanical change:
collapse a multi-line call to a single line for
`aborted_pending_resource_cleanup_safe_next_action(state.run_id)`.

Applied change in `src/ai_dev_loop/scheduler/application/status.py` only. The diff
contains no semantic source change.

## Test Remediation

Three full-suite failures blocked the initial validation gate. Corrected with
test-only changes:

1. `tests/unit/test_probes.py` — added `timed_out = False` to both fake
   `run_process` result objects so they match the current process-result contract.
2. `tests/integration/test_phase16_4_acceptance_matrix.py` — after reply
   adjudication, assert `EXECUTE_EFFECT`, complete queued `post_thread_reply`
   effects with `ThreadReplyConfirmedOutcome`, then assert `WAIT_FOR_USER` once
   no deferred replies remain.

No probe, reducer, or scheduler behavior was changed.

## Commands Run And Outcomes

Environment: native WSL temp paths for pytest (`TMPDIR=/tmp TMP=/tmp TEMP=/tmp`).
All commands used `uv run` from the repository root. Only fake `agent` and fake
`codex` executables were used through pytest fixtures; no real model, real
systemd timer lifecycle, or state cleanup was executed.

| Command | Outcome |
|---|---|
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` | **PASSED** — 1955 passed, 1 skipped, 3 warnings in 924.07s |
| `uv run python -m ruff format --check .` | **PASSED** — 344 files already formatted |
| `uv run python -m ruff check .` | **PASSED** |
| `uv run python -m mypy src` | **PASSED** — no issues found in 166 source files |
| `uv run python -m build` | **PASSED** — built `ai_dev_loop-0.1.0` sdist and wheel |
| `uv run mkdocs build --strict` | **PASSED** |
| `git diff --check` | **PASSED** |

## Phase 17.1–17.6 Evidence Matrix

Evidence is drawn from committed phase plans, scheduler implementation surfaces,
and current automated tests. Historical per-phase findings files for Phase 17 were
not present under `archive/implementation-history/findings/` at validation time.
Manual workstation acceptance referenced in earlier phase plans is marked
unperformed here unless separately documented elsewhere.

| Phase | Contract area | Automated evidence | Manual / historical evidence |
|---|---|---|---|
| 17.1 | Central ledger, `scheduler submit`, protected artifacts, read-only status/list | `tests/integration/test_phase17_1_submit.py`; `tests/unit/scheduler/test_sqlite_store.py`; `tests/unit/scheduler/test_schema.py`; `tests/unit/scheduler/test_protected_artifacts.py`; `tests/unit/scheduler/test_start.py`; `tests/unit/scheduler/test_status_readonly.py` | Phase 17.1 manual validation referenced in later plans: **unperformed in this record** |
| 17.1.5 | One fresh Codex B per run; exact later resume; no B at prepare/submit | `tests/integration/test_phase17_1_5_fresh_codex_reviewer.py`; fresh-B schema coverage in `tests/unit/scheduler/test_schema.py` | Real Codex bootstrap smoke: **not run** |
| 17.1.75 | Side-effect-free repository target resolution; no Git status at submit | `tests/integration/test_phase17_1_submit.py::test_submit_ignores_worktree_git_state_and_writes_no_baseline`; `tests/unit/scheduler/test_repository_target.py`; v3 schema rejection in `tests/unit/scheduler/test_schema.py` | — |
| 17.2 | `scheduler start`, tick leader/claims, admission preflight, controller lookup | `tests/integration/test_phase17_2_tick_control.py`; `tests/integration/test_phase17_2_controller_lookup.py`; `tests/unit/scheduler/test_tick.py`; `tests/unit/scheduler/test_tick_regressions.py`; admission contract in `tests/unit/scheduler/test_schema.py` | — |
| 17.3 | Systemd attempt backend, active-attempt tick behavior, packaged timer assets without enablement | `tests/integration/test_phase17_3_attempt_tick.py`; `tests/unit/scheduler/test_attempt_executor.py` | Real `systemctl --user` enable/install: **not run** |
| 17.4 | Cursor chat/turn, staging normalization, usage-limit continuation | `tests/integration/test_phase17_4_cursor_workflow.py`; `tests/unit/scheduler/test_cursor_retry_after.py`; `tests/unit/scheduler/test_cursor_workflow_corrections.py`; `tests/unit/scheduler/test_cursor_evidence_corrections.py` | Real Cursor/systemd acceptance: **not run** |
| 17.5 | Fresh-B bootstrap at first review, exact resume, bounded correction loop, structured JSON decisions | `tests/integration/test_phase17_5_scheduler_review_loop.py`; `tests/unit/scheduler/test_phase17_5_codex_corrections.py` | Real Codex review smoke: **not run** |
| 17.6 | Abort, reconciliation, redacted status/history/controller, timer ops documentation | `tests/integration/test_phase17_6_abort_lifecycle.py`; `tests/integration/test_phase17_6_privacy.py`; `tests/integration/test_phase17_6_restart_reconcile.py`; `tests/unit/scheduler/test_phase17_6_abort.py`; `tests/unit/scheduler/test_phase17_6_abort_corrections.py`; `tests/unit/scheduler/test_phase17_6_history.py` | Real systemd timer enable/disable lifecycle: **not run** |

Shared migration coverage: `tests/unit/scheduler/test_v4_migration.py`.

Deferred-reply PR-review v2 behavior exercised by the full suite includes
`tests/integration/test_phase16_4_acceptance_matrix.py` (updated during this
remediation).

## Fake-Agent Limitation

Automated validation exercised scheduler, legacy, and probe behavior only through
pytest with fake `agent` and fake `codex` executables on temporary XDG roots. A
passing full suite proves the present working-tree baseline under those
constraints only. It does not retroactively prove manual workstation actions,
real provider auth, real systemd timer operation, or real model-backed review.

## Unperformed Manual Actions

The following were intentionally not executed in Phase 17.6.5:

- Real Cursor or Codex model calls
- Real `systemctl --user` timer install, enable, observe, or disable
- Destructive cleanup of `$XDG_STATE_HOME/ai_dev_loop/runs/` or
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/`
- Changes to global integrations (`~/.agents`, `~/.codex`, hooks, bridge)
- Deletion or mutation of user-global XDG state
- Git staging, commit, push, reset, clean, stash, or unstage operations

## Residual Risks

- **No Phase 17 historical findings:** Earlier Phase 17 manual acceptance steps
  referenced in plans are not reproduced in this artifact.
- **Manual cutover surfaces untested:** Timer enablement, legacy state deletion,
  and real provider behavior remain unverified here by design.
- **Human acceptance still required:** Phase 17.7 destructive and control-plane
  changes require independent review before any real state cleanup or timer
  enablement.
- **Uncommitted remediation:** Validation covered the working tree with
  uncommitted Phase 17.6.5 changes; commit and branch hygiene remain the user's
  decision.

## Phase 17.7 Gate

**Automated pre-cutover gate: ELIGIBLE.**

This artifact now records:

1. A **passing** full validation command set (including
   `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` with zero
   failures).
2. An explicit phase-to-evidence matrix for Phases 17.1–17.6.

Phase 17.7 implementation may proceed under the plan's remaining requirements:

- Independent human acceptance review for control-plane and destructive changes.
- No real timer enablement or legacy state deletion until that review completes
  and explicit authorization is given.

An incomplete or failed validation would block cutover. This artifact remains
not authority to clean user state or enable a timer without the separate human
acceptance step.

## Next Safe Action

1. Commit the Phase 17.6.5 remediation on branch `rba/phase_17`.
2. Begin Phase 17.7 implementation under independent human review, using this
   artifact as the consolidated pre-cutover evidence gate.
3. Do not enable a production systemd timer or delete legacy state roots until
   Phase 17.7's explicit destructive confirmation and human acceptance are
   complete.
