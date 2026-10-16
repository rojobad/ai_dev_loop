# Phase 26.1 shared dashboard query findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`e308d985f5e5213f1763925ad70aa57864696572`. Initial Git status was clean.
Implementation used the accepted Phase 27.1-27.7 production baseline: public
Integration API 1.8, SQLite schema 16, dual implementer bindings, role-aware
attempt inspection, and authoritative `implementer_retry` lineage. Phase 24 and
Phase 25 remain unexecuted planning and were not treated as dependencies.

The implementation adds the shared domain/application query layer, one bounded
read-only SQLite adapter, explicit public DTOs and five packaged schemas, and the
four JSON-only `integration dashboard` commands. The compatible public API is
now 1.9 and advertises `dashboard=true`; an absent historical capability remains
false and explicit null/coerced values remain invalid.

No live scheduler driver or provider was invoked during implementation. No
scheduler run was started, ticked, retried, extended, or aborted. Tests used
temporary owned ledgers/repos and fake providers. No installed package, timer,
root YAML, scheduler/review schema, provider runtime, user ledger/artifact, Git
index, commit, push, PR, or merge was changed.

## Admission and prerequisite evidence

The accepted production prerequisite was checked once before dependent work:

- `src/ai_dev_loop/integration_api/version.py` reported API 1.8.
- `src/ai_dev_loop/scheduler/infrastructure/schema.py` reported SQLite schema 16.
- Current code defined all five owned execution effects:
  `cursor.run_turn`, `codex.implementer_bootstrap`,
  `codex.implementer_resume`, `codex.bootstrap_review`, and
  `codex.resume_review`.
- Current state/projection/lineage code preserved historical Cursor defaults,
  current `implementerProvider`, pending role/turn/capacity and retry metadata,
  and `implementer_retry` source/generation lineage.

The first prerequisite invocation could not acquire uv's read-only home cache:

```bash
uv run python -m pytest -q tests/integration/test_phase27_3_inspection.py tests/unit/scheduler/test_schema_readonly_historical.py
```

Result: **exit 2 before collection**; uv could not open its lock below
`~/.cache/uv`. This was an environment-path failure, not a test result. The
same selection was rerun with the owned temporary cache:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase27_3_inspection.py tests/unit/scheduler/test_schema_readonly_historical.py
```

Result: **exit 0, 9 passed in 9.94s**. Dependent implementation proceeded only
after this pass.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact result |
| --- | --- | --- | --- |
| C261-1 | `DashboardQueries` supplies `DashboardReadScope` -> `SqliteDashboardReadRepository.read_observations` -> summary/runs service and real CLI | `test_c261_1_real_cli_started_cohort_and_totals_outlive_first_page` creates 27 started rows, a pre-range submission whose execution starts in range, a queued-only row, and pre-execution chat evidence in a real schema-16 ledger. It asserts 27 cohort totals independently of the default 25-row map page and checks the authoritative first execution timestamp. `test_c261_4_range_and_effect_admission_excludes_old_and_chat_history` proves reduced admission limits ignore old resolved/non-execution history while still using the full-history earliest owned launch. The accepted Phase 27 lineage regression asserts planned generation 1 plus two `implementer_retry` generations and the exact source chain, without parsing IDs. | Included in the corrected final mandatory selection: **53 passed in 23.37s**. |
| C261-2 | `domain.metrics.first_execution_launches`, `first_review_round_launches`, `resolved_process_interval`, and `process_second_buckets` -> `DashboardQueries.activity` -> typed activity result / CLI | Pure tests assert owned component/effect pairs, implementer/reviewer separation, one review round per run+iteration, no cross-run dedup, parallel process-second addition, resolved failed/cancelled policy, exclusion of active/cancelled-unreconciled/missing intervals, clipping, and a 23-hour named-zone DST day. Real service tests assert one review round and 14,400 process-seconds for normal Cursor/Codex ownership, while the reduced-threshold test proves a pre-range run still contributes its in-range interval. | Included in the corrected final mandatory selection: **53 passed in 23.37s**. |
| C261-3 | `commands.integration.dashboard_app` -> `IntegrationDashboardReadService` -> explicit `dashboard_models.py` DTOs -> Integration API envelope and registered packaged schemas; `build_integration_info_data` advertises API 1.9/dashboard | `test_c261_3_four_real_cli_commands_schemas_help_and_compatibility` invokes all four real CLI commands, requires exactly one JSON document, validates each data document against its packaged schema, checks help and invalid input, current Cursor/Codex providers, API 1.9, historical omitted capability=false, and explicit null rejection. Existing contract tests cover mismatched major and historical provider defaults; affected Phase 21/27 selections cover current role/retry DTOs. | Corrected mandatory selection: **53 passed in 23.37s**. Affected regressions: **26 passed in 25.32s**. |
| C261-4 | `SqliteDashboardReadRepository` -> `SqliteSchedulerStore.open_readonly` / `begin_read` validated schema, state, hash, reservation, effect ownership, sequence aggregate and lineage readers -> safe Integration API errors | Missing/historical/unsupported tests preserve read-only compatibility. Eight operation/fixture combinations assert malformed SQLite and supported-version structural corruption are DATA_INTEGRITY with fixed safe text and unchanged bytes; a real CLI regression asserts one safe JSON failure. The foreign-dispatch fixture passes SQLite FK checks but is rejected because effect/run ownership contradicts the attempt. A real rollback-journal `BEGIN EXCLUSIVE` fixture asserts contention is not corruption and the healthy ledger reads after release; a separate operational-open assertion proves fixed IO_ERROR redaction. Scope tests force genuine relevant-run/contributing-attempt oversize, catalog-envelope, cancellation and deadline failures without partial success; a WAL writer succeeds after snapshot retirement. | Included in the corrected final mandatory selection: **53 passed in 23.37s**. |

## Implemented semantics and safety boundary

- `[from,to)` filters require strict RFC3339 instants with explicit offsets,
  normalize to UTC, admit at most 366 days, use a validated IANA zone, and reject
  explicit null/blank/oversized project names. Dashboard pages are strict
  integers with default 25 and maximum 100.
- Started-cohort and process-time ownership is the exact five-effect allowlist.
  Chat creation, preflight, ingestion/staging, fake effects and unknown ownership
  are excluded. Codex implementer attempts contribute to execution start/time;
  only Codex reviewer effect/component pairs contribute review rounds.
- Process intervals require resolved public evidence, clip to the query, split at
  named-zone calendar midnights and sum parallel seconds. Resolved cancellation
  additionally requires ingested/reconciled termination evidence. Active or
  uncertain time is never fabricated.
- State grouping follows the reviewed mapping; unknown future state strings are
  retained and grouped as Other. Summary totals use the complete query cohort,
  not the current map page. Activity uses event/interval dates and may include a
  run first launched before the selected range.
- Public map rows preserve historical Cursor defaults, current provider, raw
  state, and authoritative sequence name/phase/generation/attempt/source fields.
  Capacity, prompts, reviews and process-output content are not aggregated or
  loaded. Existing deliberate content reads remain bounded and memory-only.
- Reads use one bounded read transaction and close it before DTO/envelope
  serialization. Missing ledgers remain absent. Operation/range-aware admission
  bounds 10,000 relevant runs and 100,000 contributing owned execution attempts;
  complete authoritative history is still validated for earliest-launch,
  lineage and coverage semantics. Five monotonic seconds with SQLite progress
  cancellation and a 240 KiB serialized envelope provide the other bounds.
  Oversize/cancel/unsupported/corrupt paths fail safely without partial totals.

## Correction finding disposition

- **F-01 fixed.** Production path: `DashboardQueries._observation` now passes a
  typed operation/project/range scope to the read port; the adapter validates all
  selected state, attempt and lineage evidence in one snapshot, derives earliest
  owned launches from full history, and applies run/attempt ceilings only to the
  requested operation's relevant runs and contributing owned execution attempts.
  Old resolved attempts and chat creation no longer exhaust the attempt limit;
  pre-range runs still contribute in-range activity; missing/unknown/unstarted
  coverage remains visible. Reduced-threshold and genuine-oversize real-ledger
  regressions are in the final **53-test** mandatory pass.
- **F-02 fixed.** Production path: read-only store validation now distinguishes
  a supported-version corrupt schema (`CORRUPTION`) from genuinely unsupported
  versions (`SCHEMA`), and both store opening and reads are inside the dashboard's
  safe error boundary. Malformed files and a schema-16 ledger missing
  `scheduler_timers` produce fixed DATA_INTEGRITY responses for projects,
  summary, runs and activity; an additional CLI assertion proves no path,
  SQLite class, SQL, table name or driver text leaks. Fixture bytes remain
  unchanged. Unsupported version 3 remains UNSUPPORTED.
- **F-03 fixed.** Production path: the attempt/effect join now selects the effect
  owner and rejects a present effect whose `run_id` differs from the attempt's
  `run_id` before classifying its metric role. The independent target-project
  fixture has no foreign-key violations but now returns DATA_INTEGRITY instead of
  one fabricated review round. Historical absent-effect evidence still projects
  as unknown coverage; normal Cursor/Codex role metrics remain green.
- **F-04 fixed.** Production path: `_bounded` accepts a fixed operation-specific
  oversize message and `projects()` uses `Dashboard project catalog is too large;
  retry or reduce the page size.` The forced 32-byte envelope regression asserts
  this exact INVALID_ARGUMENT and receives no partial data.
- **F-05 fixed.** Production path: `SqliteDashboardReadRepository._open_store`
  now classifies the SQLite primary result code before its generic database-error
  fallback. `SQLITE_BUSY`/`SQLITE_LOCKED` use the existing safe transient
  internal-error path, `SQLITE_CORRUPT`/`SQLITE_NOTADB` remain DATA_INTEGRITY,
  and other `OperationalError` openings use the fixed IO_ERROR path. F-02 had
  introduced the regression because `OperationalError` is a subclass of
  `DatabaseError`, so its broad opening guard treated a healthy exclusive lock
  as corruption. A real rollback-journal `BEGIN EXCLUSIVE` regression now gets
  fixed `INTERNAL_ERROR` text with no path/SQL/driver disclosure, then reads the
  same one-run ledger successfully after rollback. The independent operational
  failure assertion gets fixed IO_ERROR text; all pre-existing corruption and
  unsupported-version assertions remain green.

No finding was disputed or blocked.

## Exact focused validation

The required uv commands use `UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache` because
the sandbox home cache is read-only. This is an ephemeral cache override, not an
installation or live-runtime change.

Correction production-adapter/service regression file:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase26_1_dashboard_queries.py
```

Result after the four production corrections: **exit 0, 17 passed in 16.78s**.
After adding the separate malformed-file CLI-envelope assertion, the unchanged
command reported **exit 0, 18 passed in 15.78s** (16.62s tool wall). After F-05
added real contention/recovery and operational-I/O regressions, the final command
reports **exit 0, 20 passed in 22.70s** (23.3s tool wall).

Focused F-02/F-05 storage classification and recovery selection:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase26_1_dashboard_queries.py -k 'storage_corruption or malformed_catalog or missing_old_unsupported or exclusive_writer_contention or open_operational_failure'
```

Result: **exit 0, 12 passed and 8 deselected in 6.99s** (7.5s tool wall).
This selection includes all eight malformed-file/supported-schema corruption
operation combinations, the real CLI corruption envelope, unsupported-version
compatibility, the real exclusive-lock recovery, and safe operational I/O.

Mandatory Phase 26.1 selection:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_run_projection.py tests/unit/scheduler/test_schema_readonly_historical.py
```

An intermediate strengthened test run reported **exit 1, 1 failed and 39 passed
in 16.85s**. The new real-CLI fixture expected valid submitted scheduler states
under Other, but current production materializes them as `queued`; the contract
maps `queued` to Waiting. The assertion was corrected to the frozen mapping. The
pre-correction final-tree command reported **exit 0, 40 passed in 16.95s**. The
correction mandatory run first passed **50 tests in 19.85s**; after adding the
CLI-level malformed-file assertion it passed **51 tests in 21.30s**. The final
post-F-05 command reports **exit 0, 53 passed in 23.37s** (23.9s tool wall).

Affected existing Integration API, capacity, and accepted Phase 27 inspection
regressions:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase21_1_integration_contract.py tests/integration/test_phase21_6_capacity.py tests/integration/test_phase27_3_inspection.py tests/unit/scheduler/test_status_readonly.py
```

Final F-05 rerun result: **exit 0, 26 passed in 25.32s** (25.9s tool wall).
This includes the scheduler read-only path affected by the schema-integrity
classification correction.

Static contract checks:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m ruff check src/ai_dev_loop/dashboard src/ai_dev_loop/integration_api src/ai_dev_loop/commands/integration.py src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m mypy src
```

Final F-05 results: **both exit 0 in 0.70s combined tool wall**. Ruff reports
`All checks passed!`, and MyPy reports no issues in **198 source files**. During
development, MyPy exposed the observation/metric protocol as mutable and
therefore non-structural; the boundary was corrected to read-only protocol
properties before this final pass.

Documentation build:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run mkdocs build --strict
```

Final F-05 result: **exit 0**, documentation built in **0.78s** (1.3s tool
wall). Output
contains the upstream Material-for-MkDocs informational banner and the existing
incident-pages-outside-nav notice; neither is a strict-build failure. The docs
governance skill kept changes limited to the current CLI/Integration API source
of truth and separated aggregate metadata from sensitive content interfaces.

Repository whitespace:

```bash
git diff --check
git diff HEAD --check
```

Results: **both exit 0** after the final findings update; the second command
checks the complete working snapshot across the pre-existing index plus this
correction. A diagnostic `git diff --cached --check` returned **exit 0** for the
orchestrator-staged pre-F-05 snapshot. The F-05 working-tree correction remains
pending the orchestrator's announced `git add -A`; a post-staging cached check
therefore remains for the orchestrator/reviewer boundary and is not claimed as
executed here.

Earlier focused development checks also found and corrected three fixture
assertion defects and six Ruff/two initial MyPy findings before the final
mandatory commands above. A later layering refactor independently exposed eight
MyPy protocol-variance errors; those were corrected and the complete final MyPy
selection passed. No failed production prerequisite, unresolved OpenQuestion,
or mandatory 26.1 behavior remains hidden by a narrower successful command.

## Preserved surfaces and unfinished work

The following protected inputs retain their admission SHA-256 values:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

Mandatory Phase 26.1 implementation and focused automated coverage are complete.
The repository-wide pytest suite, installed-wheel/runtime acceptance, browser
tests, and human/operator walkthrough were deliberately **not executed**: the
approved contract assigns the single repository-wide/integration and live
acceptance gate to Phase 26.7. HTTP/frontend/runtime packaging belongs to later
Phase 26 subphases. These deferred gates are not represented as 26.1 success,
and green checks here do not authorize final commit, deployment, or human
acceptance.
