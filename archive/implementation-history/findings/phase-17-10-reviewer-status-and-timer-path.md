# Phase 17.10 findings — Reviewer status projection and timer CLI path

## Summary

Repaired two independent operational defects found during real Phase 17 acceptance,
without touching the active Parish360 run or any live scheduler/systemd state:

1. Scheduler status/list/controller-read now render a redacted fresh-B reviewer
   prefix from the bound workflow checkpoint after authenticated binding; null
   remains correct before binding and legacy context-backed prefixes are unchanged.
2. The packaged user-systemd tick service resolves `ai_dev_loop` through
   `/usr/bin/env` with a static, shell-free, constrained `PATH` that includes
   `%h/.local/bin` for the documented `uv tool` layout.

## Observed defects (acceptance evidence)

- Fresh B reviewers were created and bound correctly in mutable workflow state,
  but status/list read only immutable review context. Fresh context intentionally
  carries no session ID, so the rendered reviewer prefix stayed null after binding.
- The installed timer service invoked a bare `ai_dev_loop` name. Under user systemd
  the minimal environment often omits the user-local bin directory, producing exit
  203/EXEC for `uv tool` installations. The acceptance timer was disabled after
  diagnosis and was not re-enabled in this phase.

## Changes

- Added shared projection helpers:
  `bound_reviewer_session_id_from_state`,
  `reviewer_session_id_prefix_for_projection`, and wired them through
  `summary_from_context`, `SchedulerStatusService`, and controller-read adapters.
- Propagated `reviewer_session_id_prefix` through `ControllerSchedulerCandidate`
  and `controller status` JSON so controller and scheduler surfaces agree.
- Updated `ai-dev-loop-scheduler-tick.service` to set
  `Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
  and `ExecStart=/usr/bin/env ai_dev_loop scheduler tick`.
- Extended static asset validation to require the PATH/env contract and reject
  bare `ExecStart=ai_dev_loop` variants.
- Added focused regressions in
  `tests/unit/scheduler/test_phase17_10_reviewer_status_and_timer_path.py` and
  extended timer install assertions.
- Updated operational docs for reviewer prefix semantics and timer PATH behavior.

## Non-actions

- Did not mutate, resume, abort, install, enable, disable, or tick any existing
  scheduler run, including the active Parish360 acceptance run.
- Did not invoke real systemd, real Codex/Cursor models, or production test hooks.
- Did not change reviewer creation/binding/resume semantics, state transitions,
  safe-next-action logic, timer cadence, or enablement behavior.
- Did not stage, commit, push, or otherwise alter Git state.

## Validation

Focused suite:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase17_10_reviewer_status_and_timer_path.py \
  tests/unit/scheduler/test_phase17_7_timer_ops.py \
  tests/integration/test_phase17_1_submit.py \
  tests/integration/test_phase17_2_tick_control.py \
  tests/integration/test_phase17_6_privacy.py \
  tests/unit/scheduler/test_attempt_executor.py::test_packaged_systemd_assets_are_safe
```

Result: **35 passed**

Full suite and quality gates:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

Results:

- pytest: **585 passed**, 1 skipped
- ruff format/check: **pass**
- mypy: **pass**
- build: **pass**
- mkdocs `--strict`: **pass**
- `git diff --check`: **pass**

## Residual risks

- Status display remains diagnostic only; runtime identity continues to use the
  stored exact reviewer session ID internally. Projection bugs must never affect
  resume targeting or create replacement reviewers.
- The constrained service `PATH` supports the documented `uv tool` layout only.
  Other installation locations remain an explicit future compatibility decision.
- The active Parish360 acceptance run continues on manual ticks until this work
  is reviewed, approved, committed, locally installed, and an operator deliberately
  re-enables the timer for live acceptance.

## Operator-only live acceptance (remaining)

After independent review, approval, commit, and local installation:

1. Run `ai_dev_loop scheduler timer validate`.
2. Run `ai_dev_loop scheduler timer install --enable` only when explicitly desired.
3. Observe at least two timer activations and progressing run history/status without
   manual ticks.
4. Disable the timer with `ai_dev_loop scheduler timer disable` when finished.
   Disabling prevents later ticks but does not abort an already-started attempt.

## OpenQuestions

None.
