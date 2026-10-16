# Phase 28.1 short-name findings

Status: **READY_FOR_REVIEW**. This is an implementation/evidence label only; it
does not authorize acceptance, staging, a checkpoint, installation, or any Git
operation.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-28-dashboard-refinements`, starting HEAD
`284d51b853542698ed6dc05a09f4d0edfbcb0275`. The accepted predecessor
`67b8edc` was verified as an ancestor before dependent work. Initial Git status
was clean. The plan and shared Phase 28 contract had no OpenQuestions.

The implementation adds SQLite schema 17 presentation tables, strict bounded
short-name validation, manifest v3 and required standalone naming, local
metadata-only rename commands, exact materialization/recovery propagation,
Integration API 1.10 projections, and Dashboard labels. Presentation metadata
does not enter submitted contexts, prepared definitions, state identities,
lineage, reservations, effects, or authorization. Accepted v1/v2 payloads and
artifacts are not rewritten.

All automated evidence used isolated XDG/SQLite/artifact roots, temporary Git
repositories, and fake executables/providers. No live ledger was opened or
migrated; no model, installed scheduler, timer, integration, or workstation
configuration was invoked or changed.

## Contract evidence

| ID | Real production caller/path | Independent caller/test evidence | Exact result |
| --- | --- | --- | --- |
| C281-1 | `SqliteSchedulerStore.bootstrap` -> `_migrate_v16_to_v17` -> `backfill_presentation_short_names`; `run_short_name`, `sequence_short_name`, and `planned_run_short_name` own typed reads and schema-16 fallback. `SqliteDashboardReadRepository` opens the store read-only. | `test_schema16_read_fallback_and_v17_migration_preserve_authority` creates run/sequence/planned-leaf authority, removes only migration 17 to form an isolated schema-16 fixture, snapshots immutable rows and artifact digests, performs the real read-only Dashboard query, migrates, reruns backfill after edits, and proves IDs, payload/hash bytes, reservations, and artifacts unchanged. `test_schema17_migration_fault_rolls_back_all_presentation_tables` injects a mid-migration fault. Domain/schema tests cover padded 35-code-point multibyte input and null/bool/number/blank/edge-or-embedded-control/36-code-point rejection. Historical migration/read selection covers schema 3/4/5/6/8/12/14/15 readers and migrations. | Phase 28 focused: **25 passed in 6.75s**. Migration/read compatibility: **72 passed in 107.13s**. |
| C281-2 | Typer `scheduler submit` -> `submit_run` -> `SubmissionService.submit`; `prepare_sequence` parses legacy inputs, opens `SqliteSchedulerStore.open_readonly`, and lets `SequencePrepareService.prepare` perform its exact identity lookup in a read transaction before any writable store exists. Current v3 proceeds through the writable service. `SequenceManifest` and `scheduler-sequence-manifest-v3.json` preserve v2 implementer semantics. | Real CLI tests reject omitted `--short-name` without a database, persist trimmed valid input atomically, and assert help. `test_fresh_legacy_rejection_does_not_change_existing_ledger` covers schema 16 and 17, proving main-ledger bytes, schema version, migration rows, sequence rows, and artifact absence are unchanged. The accepted v1 and predecessor-generated mixed-provider v2 fixtures replay through the public wrapper, preserve later aliases and exact artifact digests, then materialize successfully. Submit regressions cover frozen independent implementer/reviewer settings. | Focused **25/25**; submit/frozen-input selection **76 passed in 32.71s**; corrected prepare/materialization selection **71 passed in 131.98s**. Predecessor fixture generation remains **1 passed in 0.91s**. |
| C281-3 | `insert_prepared_sequence` stores exact planned-leaf aliases; `insert_materialized_sequence_run` and `complete_sequence_checkpoint_handoff` bind the exact ordinal/planned ID. Cursor initial/correction, Codex implementer, standalone review, and sequence review recovery paths call `copy_run_short_name_if_absent` inside their publication transaction. `presentation_metadata.py` backs `scheduler run/sequence set-short-name`. | V3 prepare/start proves exact planned alias and unchanged frozen definition. CLI/application rename tests cover duplicate names, prepared/blocked records, invalid/missing targets, and unchanged hashes/reservations/effects. `test_schema_v16_intent_and_initial_recovery_continue_checkpoint_and_final_stage` renames the source, proves the successor copies it, renames the successor, reconstructs services, replays recovery, and proves the edit is not clobbered. Existing recovery suites exercise every patched successor publisher and sequence checkpoint authority with fake providers. | Focused **25/25**; affected recovery selection **78 passed in 215.41s**; corrected sequence prepare/start/provider selection **71/71 in 131.98s**. |
| C281-4 | Integration run/sequence/dashboard services obtain aliases from the store and emit additive DTO/schema fields; API 1.10 advertises `shortNames`. `SqliteDashboardReadRepository` -> `DashboardQueries` -> public DTO -> strict browser parsers -> workload chart, `RunMap`, and `AttemptTimeline`. IDs remain selection/grouping/lineage keys. | Python API/dashboard tests validate current schemas and old schema-4/schema-8/schema-16 fallback. Browser tests assert short labels, full names/IDs in details and tooltips, duplicate aliases retaining distinct exact IDs, and malicious aliases rendered as escaped/literal text. The schema-16 Dashboard read leaves `user_version`, metadata-table absence, and DB size unchanged. No content endpoint, preload, polling, cancellation, Pause, visibility, or two-read bound was altered. | API/schema/Dashboard/server selection: **132 passed in 115.98s**. TypeScript typecheck: **exit 0**. Complete Vitest suite: **12 files / 55 tests passed in 3.55s**. Production frontend build: **655 modules, exit 0 in 4.79s**; only Vite's existing >500 kB advisory. |
| C281-5 | Current CLI help plus maintained reference, quick-start, handoff, complete-flow, submit/recovery operation, artifact, Dashboard, integration, and privacy guidance document schema 17, schema-16 read fallback, manifest v3, required flags, explicit rename commands, immutable boundaries, and historical replay. | Real Typer help is asserted for submit and both rename commands. `test_current_submit_documentation_examples_match_required_cli_options` scans every maintained executable submit example, requires one valid bounded `--short-name`, and cross-checks both frozen reviewer options. Strict MkDocs builds the changed documentation. Archived plans/incidents, frozen fixtures, rules, and root YAML were not edited. | Focused **25/25**; final documentation regression **1 passed in 0.99s**; strict MkDocs: **exit 0, built in 0.74s**; only the existing Material/MkDocs informational notice and unnav-listed incident-page notices. Ruff, MyPy, and diff checks below all pass. |

## Correction findings

- **F-01 — FIXED.** The affected production path is the public
  `prepare_sequence` wrapper and the first admission lookup in
  `SequencePrepareService.prepare`. A legacy manifest now opens the existing
  ledger read-only and performs the same complete frozen-definition identity
  calculation and lookup before a writable store can bootstrap or migrate.
  A miss on isolated schema 16 or 17 leaves the main ledger bytes, user version,
  migration/sequence authority, and artifact absence unchanged. Exact accepted
  v1/v2 replay still verifies protected artifacts, retains a later metadata
  rename, and materializes with the original definition and identities. Evidence:
  Phase 28 focused **25 passed in 6.75s** and affected prepare/start/provider
  **71 passed in 131.98s**.
- **F-02 — FIXED.** The affected production-facing paths are the current submit
  examples and required-option descriptions in `docs/referencia/cli.md`, the
  quick-start/complete-flow/handoff guides, the submit and Codex recovery
  operation guides, configuration reference, and artifact guidance. Every
  executable example now supplies an explicit meaningful name within 35 Unicode
  code points. The focused suite checks all maintained examples against the real
  required CLI option set; the final targeted rerun passed **1/1 in 0.99s** and
  strict MkDocs passed in **0.74s**. Archived plans,
  accepted fixtures, and historical incident reproductions remain unchanged.

## Exact final validation

All Python commands used fresh temporary `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
`XDG_CACHE_HOME`, and `UV_CACHE_DIR` directories. The selections were scoped to
the changed production boundaries; no repository-wide suite was represented as
run.

```bash
env -u AI_DEV_LOOP_REVIEW_EVIDENCE_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT \
  uv run python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase28_short_names.py \
  tests/integration/test_phase28_short_names.py
```

Result: **25 passed in 6.75s, exit 0**. The correction runs removed inherited
`AI_DEV_LOOP_REVIEW_EVIDENCE_REL`, `AI_DEV_LOOP_REVIEW_PROMPT_REL`, and
`AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT` only from the pytest subprocess
environment.

After the final complete-flow example edit, the documentation/CLI contract was
rerun directly:

```bash
env -u AI_DEV_LOOP_REVIEW_EVIDENCE_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT \
  uv run python -m pytest -q --tb=short \
  tests/integration/test_phase28_short_names.py::test_current_submit_documentation_examples_match_required_cli_options
```

Result: **1 passed in 0.99s, exit 0**.

```bash
uv run python -m pytest -q --tb=short \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/unit/scheduler/test_v4_migration.py \
  tests/unit/scheduler/test_v5_migration.py \
  tests/unit/scheduler/test_v6_migration.py \
  tests/unit/scheduler/test_sqlite_store.py \
  tests/unit/scheduler/test_phase20_8_sequence_review_retry.py \
  tests/unit/scheduler/test_phase23_2_initial_recovery.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py \
  tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py::test_v15_migration_adds_sequence_replacement_without_rewriting_runs
```

Result: **72 passed in 107.13s, exit 0**.

```bash
uv run python -m pytest -q --tb=short \
  tests/integration/test_phase17_1_submit.py \
  tests/integration/test_phase17_1_5_fresh_codex_reviewer.py \
  tests/integration/test_phase18_optional_controller_provenance.py \
  tests/unit/scheduler/test_start.py \
  tests/unit/scheduler/test_phase27_1_implementer_inputs.py \
  tests/unit/scheduler/test_phase27_2_codex_implementer.py
```

Result: **76 passed in 32.71s, exit 0**.

```bash
env -u AI_DEV_LOOP_REVIEW_EVIDENCE_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_REL \
  -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT \
  uv run python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/integration/test_phase27_3_implementer_sequences.py
```

Result: **71 passed in 131.98s, exit 0**. The same three inherited reviewer
witness variables were removed only from this pytest subprocess environment.

```bash
uv run python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py \
  tests/unit/scheduler/test_cursor_workflow_corrections.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/integration/test_phase27_7_recovery_acceptance.py
```

Result: **78 passed in 215.41s, exit 0**.

```bash
uv run python -m pytest -q --tb=short \
  tests/unit/integration_api/test_contract.py \
  tests/integration/test_phase21_1_integration_contract.py \
  tests/integration/test_phase21_2_run_inspection.py \
  tests/integration/test_phase21_3_sequence_inspection.py \
  tests/integration/test_phase21_6_capacity.py \
  tests/unit/dashboard \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py
```

Result: **132 passed in 115.98s, exit 0**.

```bash
# dashboard-web, using the available Cursor-owned Node 24 binary
node ./node_modules/typescript/bin/tsc --noEmit
node ./node_modules/vitest/vitest.mjs --run
node ./node_modules/vite/bin/vite.js build
node scripts/write-build-manifest.mjs
```

Results: typecheck **exit 0**; Vitest **12 files / 55 tests passed in
3.55s**; Vite build **655 modules in 4.79s, exit 0**; the build manifest was
regenerated after the final frontend source edit.

```bash
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict --site-dir <owned-temp>
git diff --check
```

Results: Ruff `All checks passed!`; MyPy `Success: no issues found in 204
source files`; strict MkDocs **exit 0, built in 0.74s**; staged and unstaged
`git diff --check` **exit 0**.

## Failed checks encountered and corrected

- The first broad scheduler selection used two obsolete dashboard filenames and
  collected no tests; the corrected filenames were used in all reported runs.
- The first broad affected selection reported **13 failed, 207 passed**. Nine
  failures were a fresh Phase 27 helper still defaulting to manifest v2; the
  remainder were expected schema/API-version assertions. After correction the
  exact affected subsets passed (**40/40**, then the final selections above).
- The first migration selection reported **7 failed, 64 passed**. Historical
  low-level inserts were trying to write schema-17 tables, and downgrade tests
  had not removed migration-17 tables. Production inserts now feature-detect
  the schema and authentic downgrade fixtures remove version-17 objects; the
  final migration selection is **72/72**.
- The first API/Dashboard selection reported **2 failed, 130 passed**. Schema-4
  fallback queried a lineage table that did not yet exist, and a schema-8
  fixture attempted a fresh v1 preparation. The fallback is version-aware and
  the fixture now prepares v3 without migrating schema 8; the full rerun is
  **132/132**.
- The first explicit-submit regression rerun reported **2 failed, 74 passed**:
  one legacy test still expected fresh v1 preparation and one CLI test omitted
  the now-required name. Both contracts were corrected; the full rerun is
  **76/76**.
- The first final recovery selection reported **2 failed, 76 passed** because
  its remaining fresh sequence fixture was v2. After upgrading only that fresh
  fixture to v3, the complete rerun is **78/78**.
- The first focused frontend run reported **2 failed, 22 passed** because two
  selected-run expectations still assumed an ID-only title. The corrected
  focused run passed **25/25**. A later full run reported **1 failed, 54 passed**
  after workload nodes began retaining full sequence names/IDs; its exact test
  fixture was updated, and the complete final suite passed **55/55**.
- Ruff initially found one import-order issue; its formatter fixed that import
  block and the final repository check passes. MyPy initially found two
  `Literal[1,2]` annotation errors in manifest-v3 freezing; the explicit
  annotation fixed both and the final check passes.
- The first predecessor-fixture generator test failed at collection because a
  module-local fixture was not registered. The generator was made self-contained
  and passed **1/1**. It ran predecessor preparation only; fake executables were
  never launched.
- Calling `scripts/verify-phase26-dashboard.py` without a mode exited 2 with its
  usage message. The read-only `--list` discovery then exited 0 and identified
  packaging/API/browser/safety gates. Those installed/package gates remain
  deliberately pending for their owning later phase; no install was attempted.
- The first correction-focused run reported **2 failed, 23 passed** because the
  new no-publication assertion included SQLite's transient WAL shared-memory
  bookkeeping. The durable boundary was corrected to compare the main ledger
  bytes, schema/migration/sequence authority, and artifact absence. No product
  change was needed for that test correction; the complete rerun passed
  **25/25**.

## Preserved boundaries and unfinished checks

- Full repository pytest is **not run**. The plan assigns that gate to the
  separate final integration pipeline; focused results do not imply it.
- Isolated wheel/sdist installation, installed-server/browser acceptance, and
  the Phase 28 manual walkthrough are **not run**. Phase 28.3 owns package/browser
  smoke, and the shared contract requires final human acceptance after the
  sequence.
- The final manual governance review of the conditional AGENTS/planning-skill
  naming guidance is explicitly **pending**. The existing guidance already
  matches the implemented CLI, so implementation did not modify agent policy.
- No unresolved automated C281 contract is known. No real provider capacity,
  authentication, billing/quota, timer, WSL, integration, or live scheduler
  behavior was tested.
- `ai_dev_loop.yaml`, AGENTS/rules/skills, the approved plans, and pre-existing
  frozen fixtures are unchanged. The new v2 fixture has predecessor provenance
  and is additive.
- The native sequence had staged the implementation snapshot before this
  correction turn. Correction edits are left for the orchestrator's final
  `git add -A` normalization. This implementation made no commit, push, PR,
  merge, or destructive Git-history action.
