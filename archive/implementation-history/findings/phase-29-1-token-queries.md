# Phase 29.1 token query findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-29-dashboard-tokens-retry`, HEAD
`d6cfc0935bd8f678b8f7711a5017761106822a6b` (`docs: plan Phase 29 Dashboard token analytics sequence`).
No commit, push, pull request, or merge was made. The scheduler owns the
authorized intermediate checkpoint.

OpenQuestions: none. No frontend, price, billing total, new inference capture,
event-time analytics, or remote Hub work was added. Charts remain Phase 29.2.

## Admission and prerequisite evidence

The Phase 29 baseline was checked once before dependent work:

```bash
git merge-base --is-ancestor 04f8e52a297bfd895e7a4e1cfa720b8e8d97894c HEAD
```

Result: **exit 0**. `04f8e52` is an ancestor of HEAD. HEAD also contains the
Phase 25.7 Cursor SDK commits and the Phase 29 planning commit. Current code
already had authenticated usage collection, dashboard cohort membership,
Integration API 1.12, and the invocation model fields. That was not a blocker.
No substitute baseline was invented.

No live scheduler driver or provider was invoked. Tests used temporary ledgers,
the packaged Phase 21.4 historical fixture, and fake Cursor SDK/Codex programs.
No root YAML, timer, integration install, ledger migration, or user state root
was changed.

## Contract evidence

| ID | Production entry | Independent test evidence | Result |
| --- | --- | --- | --- |
| T291-01 | `dashboard/application/token_usage.py` `TokenUsageQueries.query` and `_cohort_runs` use `first_execution_launches`. `scheduler/application/token_usage_query.py` `collect_direct` reads only the requested run ids. | `test_t291_01_cohort_is_started_runs_and_direct_does_not_expand_recovery` inserts a real ledger: in-window launch after an earlier submission, a later turn outside the window, chat/preflight, a recovery successor, another project, and an out-of-cohort launch. Cohort count matches `summary.totals.runs_started` while `runs(limit=1)` stays one page. Direct successor attempts stay 1. `NOT_FOUND` is raised before `SqliteDirectUsageReader.collect`. | Included in the mandatory selection below: **145 passed**. |
| T291-02 | `configured_model_from_invocation` attaches `cursor_model`, implementer `model`, or `review_model` from the authenticated invocation. Invalid evidence stays unattributed. | Unit cases reject blank, control, non-string, and overlong models. `test_t291_02_03_codex_roles_keep_configured_models_and_historical_jsonl` uses the Phase 27 fake Codex producer: `gpt-5.5` on initial and correction, `gpt-5.6-sol` on review, then a production-hash model removal leaves `configuredModel` null and the 110-token total in place. `test_t291_02_03_sdk_replay_is_one_configured_model_observation` uses the Phase 25 SDK replay producer: `grok-4.7` once at 7519, reviewer `gpt-6.1-sol`. `test_t291_02_historical_cli_model_comes_from_the_authenticated_invocation` reads the Phase 21.4 fixture: `composer-2.5-fast` and `gpt-5.6-sol`. Later YAML edits do not change those models. | Included in **145 passed**. |
| T291-03 | `dashboard/domain/token_analytics.py` `flow_leaves` and `aggregate_token_usage`. | Unit expectations are literals: Cursor 140 with leaves 100/20/5/12/3; Codex 115 with leaves 80/20/12/3 and native cache write 7 not a second leaf; missing splits stay null; recorded zero stays 0; mixed scope is 255 with leaf literals 180/40/5/24/6. The Codex and SDK integration tests repeat those leaf literals on the public payload. Contradiction and magnitude do not publish a partial total. | Included in **145 passed**. |
| T291-04 | `integration_api/dashboard_token_models.py`, `schemas/integration-api-dashboard-token-usage-data-v1.json`, API minor 13, `dashboardTokenUsage`, `IntegrationDashboardReadService.token_usage`, `integration dashboard token-usage`, and HTTP `dashboard.token-usage`. | The DTO/schema test rejects a leaf sum that disagrees with `knownTotalTokens`. Omitted capability stays false; explicit null is invalid. CLI and HTTP return the same rows for one direct run. Unknown `runId` forms fail before storage. Existing `tokenUsage` phase 25.4 tests still pass. The offline wheel reports minor 13. | Mandatory selection **145 passed**. Affected selection **4 passed**. |
| T291-05 | Shared `absolute_deadline`, `collect_direct` attempt count before artifact reads, 10,000-run and 100,000-attempt bounds, 240 KiB envelope, and read-only historical schema 11. | `test_t291_04_05_cli_http_bounds_cancellation_and_historical_reads` exceeds run and attempt bounds independently, cancels on the evidence checkpoint before authentication, rejects a 32-byte envelope, leaves a missing database absent, and keeps the schema-11 fixture byte-identical. SDK and Codex reads also keep the ledger bytes unchanged. | Included in **145 passed**. |

## Validation

The home uv cache was not used. Every command below set
`UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache` and `--no-sync`. The mandatory command
also includes `tests/unit/integration_api/test_contract.py`, which the version
bump affects.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m pytest -q tests/unit/dashboard tests/integration/test_phase29_1_token_queries.py tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/integration/test_phase25_4_token_usage.py tests/integration/test_phase25_4_dashboard_compatibility.py tests/unit/integration_api/test_contract.py
```

Result: **exit 0, 145 passed in 198.66s**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m pytest -q tests/integration/test_phase26_7_installed_dashboard.py::test_c267_3_docs_and_handoff_match_public_contract tests/integration/test_phase21_6_capacity.py::test_c06_info_reports_codex_capacity_capability tests/integration/test_phase21_6_capacity.py::test_c01_cli_available_capacity_with_fake_app_server tests/integration/test_phase21_1_integration_contract.py::test_f07_offline_wheel_install_and_info
```

Result: **exit 0, 4 passed in 14.13s**.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase29-1-docs
```

Result: **exit 0**, documentation built in 1.20s. The Material theme printed its
existing MkDocs 2.0 warning. Strict mode did not fail.

```bash
git diff --check
```

Result: **exit 0**.

The repository-wide suite was not run. The plan reserves that gate for after
Phase 29.2.

## Unfinished mandatory items

None for the Phase 29.1 contract. New Cursor CLI submission is retired, so
historical CLI attribution was proven by reading the existing Phase 21.4
authenticated fixture rather than by starting a CLI run. The repository-wide
suite remains deferred until after Phase 29.2. The correction round below did
not rerun the original 145-test selection.

## Correction round

F-01: **fixed**. `TokenUsageQueries.query` now translates every
`TokenUsageScopeLimit` from the shared deadline checkpoint into
`DashboardReadError(INVALID_ARGUMENT, "Dashboard scope is too large; narrow the project or time range.")`.
The translation covers the checkpoint before cohort collection, the checkpoint
immediately after cohort reading, cancellation inside `collect_direct`, and the
checkpoint after aggregation. `IntegrationDashboardReadService.token_usage`
maps that error to `INVALID_ARGUMENT`, so the public CLI and loopback HTTP
envelope stay failures with `data: null`. Authentication, run/attempt bounds,
and read-only opens are unchanged.

Production path: `src/ai_dev_loop/dashboard/application/token_usage.py`
(`query` / `_query_bounded`). Public callers:
`commands/integration.py` `integration dashboard token-usage` and
`dashboard/adapters/http.py` `dashboard.token-usage` through
`dashboard/server.py`.

Regression evidence:
`test_token_query_deadline_checkpoints_are_narrow_scope_errors` (initial
cancellation with no cohort read; post-read expiry before aggregation;
post-aggregation expiry) and
`test_f01_public_deadline_checkpoints_return_narrow_scope_errors` (real CLI
dispatch and loopback HTTP for the same three boundaries). The existing
in-collect cancellation assertion remains in
`test_t291_04_05_cli_http_bounds_cancellation_and_historical_reads`.

F-02: **fixed**. `uv run --no-sync python -m mypy src` reported 10 errors before
the correction and reports none after it. Strict checking is unchanged.
`token_analytics.py` now annotates `TOKEN_FLOW_NAMES` as
`tuple[TokenFlowName, ...]`, builds each `FlowFact` from explicit
`CategoryFact` fields, and types grouped observations as `GroupedAttempt`.
`http.py` keeps the optional token-query selection in `selected_run_id`, so
inspect, timeline, and content callers still pass non-null `run_id` strings
from `_required_text`.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m mypy src
```

Result: **exit 0**, `Success: no issues found in 223 source files`.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync ruff check src tests
```

Result: **exit 0**, `All checks passed!`

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync ruff format --check src tests
```

Result: **exit 0**, `420 files already formatted`.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run --no-sync python -m pytest -q tests/unit/dashboard tests/integration/test_phase29_1_token_queries.py
```

Result: **exit 0, 62 passed in 17.12s**. This includes the new deadline
regressions. No commit, push, pull request, merge, install, or live
scheduler/timer action was made.
