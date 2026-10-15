# Phase 27.7 recovery integration and acceptance evidence

Worktree: `/home/rojobad/Projects/ai_dev_loop-phase27-execution`.
Branch: `codex/phase-27-dual-implementer`. Starting HEAD: `024ae65`, after the
accepted Phase 27.1-27.6 commits `062f43e`, `128b5cb`, `8693cf9`, `bcb63cb`,
`2ae1f0e`, and `024ae65`. Initial Git status was clean. The correction turn began
from the orchestrator's existing staged snapshot; Codex did not stage or unstage
files.

This finding records automated evidence from temporary Git repositories, fake
provider transports, isolated XDG state, the real scheduler CLI/services, and
the production Codex implementer adapter. It is not live workstation or real
model acceptance.

## Contract evidence

| Contract | Production callers | Independent evidence in this implementation session |
| --- | --- | --- |
| C-01 | `config.py`, `cli.py`, `scheduler/application/submission.py` | Phase 27.4-27.6 prerequisite selection plus the new public `scheduler submit` smoke preserve explicit provider/model/effort/timeout values independently for implementer and reviewer. |
| C-02 | `scheduler/domain/state.py`, submitted-context schemas, `infrastructure/protected_artifacts.py` | New standalone and sequence smoke reload validated SQLite snapshots and protected recovery records across fresh service instances; source snapshots and exact prompt hashes remain immutable. |
| C-03 | `scheduler/application/scheduler_preflight.py`, `git_admission.py` | All new scenarios enter through public CLI start/tick with `BoundedGitAdmissionPort` against temporary real Git repositories and fake executable/model/auth surfaces. |
| C-04 | Cursor workflow, reviewer bootstrap, historical context readers | Mixed manifest v2 ends on the frozen Cursor provider; updated Phase 23 assertions preserve historical Cursor recovery artifact bytes while independently checking the accepted non-interactive transport prefix. |
| C-05 | `scheduler/codex_implementer_attempt_runner.py`, Codex argv/evidence paths | `_ProductionAdapterBackend` invokes the production adapter and replaces only its process transport. Tests assert bootstrap once, exact `resume <implementer-id>`, no `--last`, frozen model/effort, and byte-exact stdin. |
| C-06 | `scheduler/application/codex_workflow_service.py`, staging/review paths | Standalone recovery proceeds through findings, exact correction bytes, the same implementer, an independently created reviewer, two reviews, accepted completion, and final staged content. |
| C-07 | state/capacity continuations, `codex_capacity_probe.py`, Codex workflow | Timeout then structured quota reaches implementer-role capacity wait; EXHAUSTED makes no provider call, AVAILABLE resumes the exact session/turn, and reservations remain held throughout uncertainty. |
| C-08 | attempt intent/evidence, tick restart, implementer capture runner | Fresh `TickService` instances cross timeout, capacity, publication, and abort boundaries. Exact original/correction prompt bytes and authenticated implementer identity survive restart. |
| C-09 | `abort.py`, `sequence_abort.py`, retry guards | Sequence abort during a restarted capacity wait prevents another provider call or future leaf/checkpoint, preserves partial work, resolves the run as aborted, and releases the reservation only after termination is resolved. |
| C-10 | `domain/sequence.py`, prepare/materialization/context schemas | Public manifest-v2 prepare/start freezes Codex then Cursor settings; the mixed smoke observes both materialized entries without mutating the definition. Historical v1 remains Cursor-only from accepted prerequisite coverage. |
| C-11 | tick, `sequence_handoff.py`, `sequence_reconcile.py`, `sequence_implementer_recovery.py` | `test_mixed_sequence_recovery_keeps_current_ordinal_and_checkpoint_authority` proves source-to-successor adoption at ordinal 1, planned+implementer-retry lineage, one non-final checkpoint, next frozen provider, and final staged authority. |
| C-12 | status/history/timeline contracts, sequence status, integration projections | The new runbook uses supported inspection surfaces; the full/focused selections exercise existing projections. The new smoke reloads run/sequence status from read-only stores and uses production `SequenceStatusService`. |
| C-13 | `AGENTS.md`, repository rules, current CLI/docs | `docs/operacion/codex-implementer-recovery.md` documents only supported commands and separates privacy-safe automated evidence, live operator authorization, timer/package actions, and historical compatibility. Strict MkDocs succeeds. |
| R-01 | `cli.py:scheduler_implementer_retry_command`, `ImplementerRetryService` | Public `scheduler implementer-retry` is used for timeout and `--force`; existing Cursor delegation remains covered by Phase 27.4-27.6 and Phase 23 compatibility tests. |
| R-02 | `CodexImplementerTimeoutRetryService`, attempt/tick Codex callers | New smoke observes timeout continuation, manual advancement, bounded counter state, exact session/prompt continuation, and unchanged review budget. Phase 27.4 tests independently cover schedule/exhaustion edges. |
| R-03 | timeout evidence and abort/reconciliation callers | New restart and abort smoke plus Phase 27.4 tests cover termination evidence, no duplicate launch, retained reservation under uncertainty, and no revival after abort. |
| R-04 | `ImplementerRetryService.retry(force=True)`, `CodexImplementerFailureRecoveryService` | New public CLI smoke publishes one successor for an authenticated generic failure and returns the same successor with `changed=false` on replay. Phase 27.5 supplies detailed eligibility/rejection coverage. |
| R-05 | immutable recovery intent/record/protected-artifact and launch readers | New test reloads the production recovery record, verifies implementer session, exact prompt SHA-256, inherited retry count/settings, one relation, and immutable source state. |
| R-06 | failure publication reconciliation, tick, abort | Fresh services reconcile the successor and dispatch once. Repeated `--force` is idempotent; restart and abort scenarios do not duplicate or revive effects. Phase 27.5/27.6 tests cover injected publication boundaries. |
| R-07 | `sequence_implementer_recovery.py`, lineage store/domain | Mixed v2 sequence adopts only the current leaf at the same ordinal and records `implementer_retry`; source is immutable and stale-leaf policy remains in Phase 27.6 coverage. |
| R-08 | sequence reconcile/handoff/abort | Mixed completion proves one checkpoint for the accepted non-final leaf and final staged state; abort proves no future materialization/checkpoint and no partial-work deletion. |
| R-09 | status/integration projections and API versioned schemas | Existing Phase 27.6 selection and repository suite exercise provider/retry/lineage projections; runbook diagnostics use current status/history/timeline/phase-runs commands. |
| R-10 | `tests/integration/test_phase27_7_recovery_acceptance.py` | Three independent production-path tests cover the complete standalone chain, mixed-provider current-leaf/checkpoint authority, and restart/abort during capacity wait. Standalone asserts exactly five implementer calls and independently declared recovery bytes in the staged blob. Mixed sequence asserts those bytes in the authorized checkpoint, the unchanged admitted config there, and independently predicted Cursor final content in the index. No real model is invoked. |
| R-11 | Repository pytest/build/static/docs gates | Focused recovery and affected compatibility reruns are green; Ruff lint, MyPy, and strict MkDocs are green. The complete post-fix suite has one failure: its nested isolated package build, also blocked because the sandbox cannot provision `hatchling` from PyPI. |
| R-12 | recovery runbook, MkDocs nav, this finding | Operator commands, privacy, immutable authority, idempotence, historical compatibility, and a separately pending live checklist are published without claiming fake tests as live evidence. |

## New production-path smoke

`tests/integration/test_phase27_7_recovery_acceptance.py` uses public Typer CLI
commands for submit/start/tick/retry, sequence prepare/start/tick/abort, real
SQLite/protected-artifact stores, real bounded Git admission, production retry
services, and the production Codex implementer adapter. Only the final provider
process call is deterministic. The fake Codex reviewer remains independent from
the implementer identity. No live ledger, scheduler process, timer, package, or
model is touched.

The standalone scenario executes timeout -> manual retry -> quota/capacity wait
-> capacity resume -> generic failure -> idempotent `--force` successor -> review
findings -> correction -> accepted completion. The sequence scenario repeats the
recovery as the current Codex ordinal, then hands off to a frozen Cursor final
phase. The abort scenario restarts while capacity-blocked and proves no new call,
checkpoint, future leaf, or partial-work deletion after explicit sequence abort.
The expected recovery bytes are declared before execution from the five scripted
outcomes, not read from the worktree or Git. The expected Cursor index bytes are
the admitted pre-run input plus the fake Cursor's literal documented append; the
test separately reads the checkpoint and index through Git and compares them to
those expectations.

## Exact validation

The read-only home sandbox requires `UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache` for
`uv` itself. This cache override does not install packages. Pytest commands also
use the plan-required native temporary-directory variables.

Accepted prerequisite recheck, before implementation:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py
```

Result: **67 passed in 254.45s**.

New smoke alone:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase27_7_recovery_acceptance.py
```

Initial result: **3 passed in 24.27s**. After the R-10 final-content correction,
the same command reports **3 passed in 24.51s** and the exact final test bytes
report **3 passed in 26.38s**.

Mandatory focused recovery selection:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase27_7_recovery_acceptance.py tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py
```

Initial result: **70 passed in 264.16s**. Final-tree rerun after the compatibility,
runbook, and evidence changes: **70 passed in 328.29s**.

The first complete repository run exposed 12 historical assertion mismatches and
three sandbox-environment failures:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q
```

Result: **15 failed, 1714 passed, 5 skipped, 29 warnings in 3693.37s**. The 12
Phase 21/23 failures were accepted-contract compatibility debt: role-aware
capacity detail and Cursor's production non-interactive transport prefix. Those
assertions now preserve raw recovery artifact bytes separately from exact runtime
argv bytes. Two lock tests now request the existing isolated-XDG fixture instead
of trying to chmod the sandbox's read-only home state directory. The remaining
wheel test failed while its nested isolated build tried to download `hatchling`.

All 12 compatibility failures plus both lock failures were rerun after correction:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase21_2_run_inspection.py::test_c02_history_pagination_capacity_waits_vs_attempt_durations tests/integration/test_phase23_2_initial_recovery.py::test_force_publishes_one_successor_and_preserves_worktree tests/integration/test_phase23_2_initial_recovery.py::test_usage_limit_continuation_keeps_the_current_prompt tests/integration/test_phase23_2_initial_recovery.py::test_correction_usage_limit_is_not_overridden_by_iteration_one tests/integration/test_phase23_2_initial_recovery.py::test_literal_note_preserves_sentinels_and_separate_publishers tests/integration/test_phase23_3_correction_recovery.py::test_new_correction_after_recovery_uses_the_new_envelope tests/integration/test_phase23_3_correction_recovery.py::test_usage_limit_continuation_keeps_the_recovered_correction tests/integration/test_phase23_3_correction_recovery.py::test_correction_restart_dispatches_the_failed_envelope_once tests/integration/test_phase23_3_correction_recovery.py::test_usage_limit_failure_after_recovery_uses_the_terminal_attempt tests/integration/test_phase23_3_correction_recovery.py::test_ordinary_correction_usage_limit_recovers_the_same_reviewer tests/integration/test_phase23_3_correction_recovery.py::test_newer_correction_after_recovered_correction_survives_usage_limit tests/integration/test_phase23_3_correction_recovery.py::test_first_correction_after_recovered_initial_survives_usage_limit tests/unit/test_locking.py::test_run_lock_prevents_concurrent_acquire tests/unit/test_locking.py::test_run_lock_released_when_repository_lock_fails
```

Initial result: **14 passed in 106.41s**. Final-tree rerun: **14 passed in
136.75s**. A subsequent full rerun reached 16% without a failure before it was
interrupted by repeated user input; it is not counted as a successful full gate.

Exact required build:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m build
```

Result: **blocked, exit 1**. Build isolation invoked pip for `hatchling`; network
resolution of `pypi.org` is unavailable and no matching local distribution was
available. No package was installed. `--no-isolation` was tested diagnostically
and also failed because `hatchling.build` is not installed in the locked virtual
environment; that diagnostic is not presented as the required build.

Final static/docs/diff commands:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m ruff check .
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m mypy src
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run mkdocs build --strict
git diff --check
```

Results: **all exit 0**; Ruff format reports **356 files already formatted**,
Ruff lint reports `All checks passed!`, MyPy reports no issues in **187 source
files**, strict MkDocs builds successfully with only the upstream Material for
MkDocs informational banner, and `git diff --check` is clean.

Final complete post-fix repository run, with the same command shown above:
**1 failed, 1728 passed, 5 skipped, 29 warnings in 3302.38s (55:02)**. The sole
failure is
`tests/unit/test_codex_integration_assets.py::test_built_wheel_includes_integration_assets`;
its nested exact isolated build hits the same unavailable-`hatchling` blocker.
All other repository tests, including both corrected lock tests, pass. The 29
warnings are existing Pydantic serializer warnings from synthetic state fixtures.

## Correction finding disposition

- **F-01 fixed.** Production path: public scheduler CLI/services, production
  Codex implementer adapter, normal staging, sequence checkpoint commit, and
  final-phase index. Standalone now asserts exactly five implementer calls and
  compares `:phase27-7-recovery.txt` with independently declared bytes for
  timeout, quota, failure, resumed success, and correction success. The mixed
  sequence compares the same bytes at `HEAD`, proves the admitted config is
  unchanged in that checkpoint, and compares the final index config with the
  pre-run admitted bytes plus the fake Cursor's literal append. The affected
  smoke command reports **3 passed in 26.38s**; targeted Ruff format/lint and
  `git diff --check` exit 0.
- **F-02 blocked.** Production path: PEP 517 isolated package build and
  `test_built_wheel_includes_integration_assets`. The executor's complete suite
  remains **1 failed, 1728 passed, 5 skipped**; the sole failure is the nested
  isolated build. The separate exact build exits 1 because pip cannot resolve
  PyPI to obtain `hatchling`. A correction-turn read-only check reports
  `importlib.util.find_spec("hatchling") == None` and finds no usable hatchling
  distribution in `/tmp/ai-dev-loop-uv-cache`. No build or wheel test was
  retried, and the focused F-01 pass is not represented as full-suite success.

## Mandatory unfinished work and blocker

- Automated acceptance is **incomplete** until the exact isolated package build
  and its wheel-asset test succeed. The complete post-fix suite finished with
  only that failure. The build is currently blocked by unavailable
  PyPI/build-backend provisioning in this sandbox; it is not relabeled as a
  passing no-isolation build.
- No missing Phase 27.7 behavior or focused automated coverage is known. No
  unresolved recovery-policy or safety decision remains.
- If the environment blocker is removed, rerun the exact build and the wheel
  asset test without changing package contents or installing a persistent tool.
  The required operator/CI decision is to provide an authorized reachable package
  index or ephemeral wheelhouse containing `hatchling` and its build dependencies
  for PEP 517 isolation. Until then, do not repeat the same unavailable download.

## Historical compatibility

- Historical submitted contexts without implementer metadata remain Cursor.
  Manifest v1 remains Cursor-only; manifest v2 freezes provider/settings per
  phase. Provider switching inside a run remains unsupported.
- `scheduler cursor-retry` remains the Cursor-only compatibility surface; the
  shared `scheduler implementer-retry` delegates to the frozen provider policy.
- Existing Cursor recovery artifacts retain their original/effective prompt
  bytes. `NON_INTERACTIVE_PROMPT` is a runtime transport prefix and is now
  asserted independently rather than confused with protected artifact content.
- No legacy top-level `resume`/`recover`, reviewer identity guessing, `--last`,
  provider/model fallback, or model-specific capacity policy was introduced.

## Separately pending operator acceptance

Every live item in `docs/operacion/codex-implementer-recovery.md` remains
**pending**: package/timer refresh, disposable-repository run, controlled timeout,
natural capacity exhaustion if it occurs, safely reproducible generic failure,
restart, idempotent force publication, mixed sequence, abort, and final evidence
capture. These require separate authorization. Automated fake transports do not
validate live workstation installation, WSL timer behavior, provider billing or
quota, authentication, or real-model session continuity.

No live scheduler state was operated. No package/timer was installed, no model
was invoked, and no stage, commit, push, PR, merge, planning/review skill,
`ai_dev_loop.yaml`, or destructive Git action was performed.
