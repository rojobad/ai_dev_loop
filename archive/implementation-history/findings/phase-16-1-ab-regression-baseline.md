# Phase 16.1 - A/B Local Loop Regression Barrier Findings

Date: 2026-07-20
Branch context: `rba/phase_16-1`
Scope: observational barrier only (tests + this artifact). No production,
schema, config, public docs, global integration, or legacy PR-review edits.

## Blocking contract conflict (stop)

Characterization uncovered a conflict between the Phase 16.1 plan / A/B
authority contract and current local `recover` behavior. Per the plan, this
phase **stops** without encoding the defect as desired behavior and without
patching production code.

### Expected (plan + A/B authority)

- Eligible local failure → `recover` creates/reuses an immutable successor that
  **preserves controller A**, reviewer B, Cursor chat, frozen runtime, and
  required artifacts.
- Controller A remains able to discover and control the successor through the
  A/B control plane (`controller status`, and later `launch` where status
  allows).

### Observed

- Source A/B run after a fake Codex review failure retains
  `state.controller.controller_session_id == <controller-A>`.
- Successor created by `ai_dev_loop.commands.recover._create_successor_run`
  builds `RunState(...)` **without** `controller=source.controller`
  (see `src/ai_dev_loop/commands/recover.py` around the successor `RunState(`
  construction). Persisted successor has `controller: null`.
- Impact:
  - `controller status --run-id <successor>` raises
    `run has no controller metadata; controller status requires an A/B-prepared run`
    (`commands/controller.py::_require_controller_match`).
  - Discovery by controller session id cannot see the successor.
  - Reviewer B, Cursor chat, and Codex runtime **are** preserved; `resume`
    still continues the reviewing checkpoint without re-running completed
    Cursor work. The break is specifically A/B **controller ownership** on the
    successor, not Codex/Cursor identity.

### Evidence command

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py \
  --tb=line
```

Result (this phase): **1 failed, 11 passed** in ~42s.

Failing node:

- `tests/integration/test_phase16_1_ab_regression_barrier.py::test_ab_recover_successor_preserves_identities_and_skips_completed_cursor`

Failure message (abbreviated, no full session IDs):

> CONTRACT CONFLICT: local recover successor dropped controller metadata.
> Source had controller A; successor.controller is None. Evidence:
> `commands/recover.py` `_create_successor_run` constructs `RunState` without
> `controller=source.controller`.

### Required decision (plan amendment)

Do **not** weaken the barrier assertion. Choose explicitly:

1. **Amend Phase 16.1 / a tiny follow-on** to allow copying
   `controller=source.controller.model_copy(deep=True)` into local recover
   successors (production one-liner + tests), restoring A/B control-plane
   continuity; or
2. **Amend the plan** if dropping controller on local recover successors is
   intentional, and redefine how A discovers/controls recovered runs.

Until that amendment exists, Phase 16.1 cannot claim a green post-change
barrier, and Phase 16.2 must not treat this omission as frozen desired
behavior.

---

## Environment

| Item | Value |
| --- | --- |
| OS | linux WSL2 (`6.6.87.2-microsoft-standard-WSL2`) |
| System Python | 3.11.15 |
| uv | 0.11.26 |
| uv Python | 3.11.15 (CPython) |
| Worktree at start | clean except untracked plan `archive/implementation-history/plans/phase-16-1-ab-regression-barrier.md` |
| Temp policy | `TMPDIR=/tmp TMP=/tmp TEMP=/tmp` |

Historical pytest-cache / context count `817` was treated as context only.
Live pre-change collect also reported **817** collected tests.

---

## Pre-change validation baseline

Commands and live outcomes **before** adding the barrier suite:

### Focused local-loop set

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_controller_launch.py \
  tests/integration/test_launch_worker.py \
  tests/integration/test_prepare.py \
  tests/integration/test_start_cursor.py \
  tests/integration/test_start_staging.py \
  tests/integration/test_start_codex_review.py \
  tests/integration/test_phase5_loop_resume.py \
  tests/integration/test_phase11_recover.py \
  tests/integration/test_phase12_index_mutations_and_staging_recovery.py \
  tests/integration/test_phase13_cursor_usage_limit_recovery.py \
  tests/integration/test_phase14_5_initial_staging_recovery.py \
  tests/integration/test_abort.py \
  tests/integration/test_e2e_acceptance.py
```

- Result: **163 passed** in 203.68s (0:03:23)
- Exit: 0

### Static / full suite

| Command | Outcome |
| --- | --- |
| `uv run python -m ruff format --check .` | 141 files already formatted; exit 0 |
| `uv run python -m ruff check .` | All checks passed; exit 0 |
| `uv run python -m mypy src` | Success: no issues found in 70 source files; exit 0 |
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest --collect-only -q` | **817 tests collected** in 1.34s; exit 0 |
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` | **817 passed** in 352.70s (0:05:52); exit 0 |
| `uv run mkdocs build --strict` | Documentation built in 0.62s; exit 0 |
| `uv run python -m build` | sdist + wheel built successfully; exit 0 |

Skips/xfails on the live full pre-change run: **none reported** in the
summary line (`817 passed`).

---

## Observable contract matrix (local A/B)

Privacy note: matrix uses role names (A/B/chat), never full session IDs.

| Surface | Authorized actor | Accepted source states / checkpoints | Observable transition | Identity / artifact invariants | Git / repo invariants | Privacy / output | Regression evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `prepare` (A/B) | Reviewer B supplies `--codex-session-id`; A id via `--controller-session-id` | clean repo baseline; no agents | → `prepared` | Distinct A≠B; frozen review model/reasoning + provenance; plan/prompt snapshots+hashes; `launch_command` for A; `github_pr_review` absent | no agent edits; no commits/pushes; state outside target repo | JSON omits dedicated full `controller_session_id` field (id may appear inside copy-paste `launch_command`); reviewer id absent from JSON | `test_ab_prepare_github_absent_persists_identities_without_agents`; `test_ab_prepare_github_explicitly_disabled`; `tests/unit/test_controller_launch.py::test_prepare_ab_persists_controller_and_launch_eligibility` |
| `launch` | Controller A only | `prepared` or `waiting_for_cursor_fix` | spawns detached worker → start/resume path | Rejects wrong A; idempotent if live worker; worker reviews resume exact B | no commit/push from launch itself | launch JSON uses session id prefixes | `test_ab_detached_launch_multi_iteration_completed_without_github`; `test_ab_wrong_controller_and_duplicate_launch_are_fail_safe`; `tests/unit/test_controller_launch.py::test_launch_requires_controller_and_is_idempotent` |
| `start` | Legacy / in-process loop (also usable in tests) | `prepared` (+ resume checkpoints via engine) | Cursor→stage→Codex→… | One Cursor chat; every Codex review resumes exact B; never A/`--last` | `git add -A` normalization; accepted patch left staged; no orchestrator commit/push | errors point at artifacts | `test_ab_result_*`; phase5 / start_* suites |
| `resume` | Operator (A/B docs: A continues; CLI has no controller flag on local resume) | `waiting_for_cursor_fix`, staging/reviewing/interrupted checkpoints | continues without repeating proven turns | same chat + B session + runtime | staged-patch equality where required | redacted status/logs | phase5 resume tests; recover→resume path in barrier (blocked before completion by controller conflict) |
| `recover` | Operator on `failed` source | artifact-driven eligible checkpoints | source stays `failed`; successor `interrupted` + lineage | **must** preserve A, B, chat, runtime (plan); **current code drops A** — see blocking conflict | read-only to target repo / source | no raw errors in lineage | barrier recover test (failing on A); `tests/integration/test_phase11_recover.py` (legacy non-A/B sources) |
| `extend` | Controller A (docs) | `max_iterations_reached` | → `waiting_for_cursor_fix`; budget +1 | same A/B/chat; final fix prompt retained | staged preserved | events record extension | `test_ab_max_iterations_extend_then_detached_launch_completes`; `tests/integration/test_phase5_loop_resume.py::test_extend_maxed_run_resumes_stored_final_fix_prompt` |
| `abort` | Controller A (docs) / CLI | non-terminal | abort request durable → `aborted` when safe | identities preserved; no later Cursor/Codex/stage | HEAD/commits preserved; staged preserved at Codex abort boundary | status shortens ids | `test_ab_abort_during_detached_cursor_preserves_repo_and_identities`; `test_ab_abort_during_detached_codex_preserves_staged_work`; `tests/integration/test_abort.py` |
| `status` | read-only | any | n/a | shows iteration/status/next action | n/a | shortens session ids; no github lifecycle for local-only | `test_ab_status_logs_inspect_expose_loop_without_github_fields` |
| `controller status` | Controller A | A/B runs with controller metadata | n/a | exact A match; ambiguity requires `--run-id` | n/a | shortens ids | `tests/unit/test_controller_launch.py::test_controller_status_*`; barrier recover expected path blocked by missing successor controller |
| `logs` / `inspect` | read-only | any | n/a | artifact paths / summaries | n/a | no review Markdown / fix prompts / full B id by default | `test_ab_status_logs_inspect_expose_loop_without_github_fields`; codex log redaction tests |

### Local result statuses → executable evidence

| Result | Evidence |
| --- | --- |
| `completed` | `test_ab_detached_launch_multi_iteration_completed_without_github`; phase5 findings→no_findings |
| `completed_with_residual_risk` | `test_ab_result_completed_with_residual_risk`; `test_start_completed_with_residual_risk_for_blocked_environment` |
| `max_iterations_reached` | `test_ab_max_iterations_extend_then_detached_launch_completes` (pre-extend); phase5 max-iter tests |
| `interrupted` | `test_ab_result_interrupted_on_codex_timeout`; start_codex_review timeout test |
| `failed` | `test_ab_result_failed_on_codex_nonzero`; recover setup path |
| `aborted` | `test_ab_abort_during_detached_*`; abort integration suite |

GitHub-disabled negative assertions (barrier): fake `gh` never invoked; no
`run_directory/github/`; `state.github_pr_review is None`; commit count
unchanged; no remotes; no publication artifact trees.

---

## Local → legacy PR-review coupling inventory

Inventory only. No renames/moves in 16.1.

### `workflow_engine._run_cursor_turn`

- **Coupling:** branch `is_external_cursor_prompt_iteration` →
  `validate_external_feedback_pre_cursor(state)` when
  `state.github_pr_review.lifecycle == fixing_external_feedback`.
- **Local contract to preserve:** ordinary local correction still validates
  previous staged patch via `validate_correction_pre_cursor`; usage-limit
  recovery preflight remains separate.
- **Legacy responsibility that must not move into reusable local loop:**
  external-feedback clean-commit preflight and PR lifecycle interpretation.
- **Local evidence:** barrier multi-iteration / residual / failed paths with
  `github_pr_review is None` (external branch not taken).

### `workflow_engine._apply_review_result`

- **Coupling:** when no actionable findings **and**
  `github_pr_review.lifecycle == fixing_external_feedback`, dynamic import of
  `commands.pr_review._spawn_pr_review_worker` and
  `refresh_external_publication_patch_fingerprint`, then transition to
  `publishing_external_fix` and spawn PR worker.
- **Local contract to preserve:** no-findings → `completed` /
  `completed_with_residual_risk`; findings → wait/continue/max-iter using
  local budget helpers.
- **Legacy responsibility:** publication fingerprint refresh, PR worker spawn,
  external lifecycle mutation.
- **Local evidence:** barrier completed / residual / max-iter with
  `github_pr_review is None` and zero `gh` / `github/` artifacts.

### `iterations.py` external helpers

| Symbol | Depends on | Local side to keep | Legacy side to extract later |
| --- | --- | --- | --- |
| `pending_external_cursor_iteration` | `RunState.github_pr_review` | returns `None` when github absent | scheduling external Cursor iter |
| `derive_external_cursor_iteration_for_recovery` | same | unused for local A/B | recovery allocation for external |
| `is_external_cursor_prompt_iteration` | same | false for local A/B | prompt routing for external |
| `cursor_prompt_path` | github external prompt path | snapshot / fix prompt / usage-limit envelope | external fix prompt path |
| `local_review_budget_used` / `begin_external_local_review_budget` / `record_local_review_for_budget` | `workflow.local_review_count` (set for external segments) | local budget uses `current_review_iteration` when count is null | reset/count for post-external segments |

### Dynamic PR boundaries activated only when github state is present

- `commands.pr_review._spawn_pr_review_worker`
- `commands.pr_review.refresh_external_publication_patch_fingerprint`
- GitHub runners / publication (not exercised by local barrier; explicitly
  trapped via fake `gh`)

---

## Barrier suite added

File: `tests/integration/test_phase16_1_ab_regression_barrier.py`

Covered scenarios (plan §3):

1. A/B prepare with github absent / explicitly disabled — **pass**
2. Detached launch multi-iteration findings→fix→completed, one chat, exact B, no github — **pass**
3. Wrong controller + duplicate live launch — **pass**
4. max_iterations → extend → detached launch → completed — **pass**
5. recover successor preserves A/B/chat — **FAIL (contract conflict)**
6. Abort during detached Cursor / Codex — **pass**
7. Result matrix residual / failed / interrupted + status/logs/inspect — **pass**

No `conftest.py` changes. No production source changes.

---

## Post-change validation (partial — stopped on conflict)

Phase remains **blocked**. This section records live post-correction evidence
only; it does **not** claim a green full post-change suite.

### Post-correction focused checks (2026-07-20 correction turn)

Exact commands and outcomes after tightening launcher cleanup identity:

```bash
git diff --cached --check
```

- Outcome: no whitespace errors reported (empty check output); exit **0**

```bash
uv run python -m ruff format --check tests/integration/test_phase16_1_ab_regression_barrier.py
```

- Outcome: `1 file already formatted`; exit **0**

```bash
uv run python -m ruff check tests/integration/test_phase16_1_ab_regression_barrier.py
```

- Outcome: `All checks passed!`; exit **0**

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase16_1_ab_regression_barrier.py --tb=line
```

- Outcome: **1 failed, 11 passed** in 39.32s
- Sole failure: known recover-controller contract conflict (see Blocking section)
- Not a cleanup/abort/ruff regression

| Command | Outcome |
| --- | --- |
| Barrier suite alone | **1 failed, 11 passed** (~39s) — fail is the recover controller contract check |
| Full suite / focused re-run after green barrier | **not claimed** — barrier not green; phase blocked |
| Production/schema/docs diff | none; only tests + findings + plan |

### Diff scope check (expected)

- `tests/integration/test_phase16_1_ab_regression_barrier.py` (new)
- `archive/implementation-history/findings/phase-16-1-ab-regression-baseline.md` (this file)
- `archive/implementation-history/plans/phase-16-1-ab-regression-barrier.md` (pre-existing untracked plan)

No edits under `src/ai_dev_loop/`, schemas, `docs/`, `.cursor/rules/`, or global
integration assets.

Cleanup note (tests only): `_terminate_run_worker` now captures the full live
launcher identity tuple `(run_id, worker_token, pid, pid_starttime, pgid)` with
non-null `pid_starttime` before `SIGTERM`, and requires that exact same live
tuple before `SIGKILL`, refusing replaced/ambiguous records.

---

## Residual risks

- Detached-worker tests use bounded polling + best-effort process-group cleanup;
  slow hosts could still flake near timeouts.
- `launch_command` intentionally embeds the full controller session id for
  copy-paste; other prepare JSON fields omit a dedicated full-id key. Documented
  as current observable behavior, not a privacy regression vs unit tests.
- Local recover still preserves B/chat/runtime and supports `resume`; operators
  can continue by known `recovery_run_id`, but A/B controller discovery is
  broken until the conflict is resolved by amended plan.
- Coupling inventory is symbol-level and human-maintained; it is not an
  automated AST snapshot.

---

## Handoff

Phase 16.1 is **blocked** on the recover-controller contract conflict.
Next action for the user/architect: choose amendment option (1) or (2) above.
Do not start Phase 16.2 extraction until the A/B recover identity contract is
explicitly decided and the barrier can go green under that decision.
