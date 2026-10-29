# Phase 30 planning evidence — Beelink

Date: 2026-10-28, America/La_Paz. Host: Beelink-SER5, repository
`/home/rodrigo/project/ai_dev_loop`. Baseline main:
`1a97becaff1af8c54f892f42740f040d4cd7478c`, PR #42 merged after Phase 29.
Planning branch: `codex/phase-30-sqlite-statistics`.

## Requested and approved scope

Prepare plans and a scheduler submission on Beelink so SQLite alone preserves
recorded statistics, including tokens, after explicit historical backfill.
The operator selected Cursor grok-4.7/high (256k, fast false) implementation and
Codex gpt-6.1-sol/xhigh review. We chose three bounded slices: durable facts and
new-attempt ingestion, explicit backfill plus independent queries, then database-only
restore acceptance. No implementation/start/deployment/live import is performed.

## Source evidence

- The installed ledger is schema 20 with 171 scheduler runs and no usage/token
  tables. This metadata was read using SQLite mode=ro, not copied or migrated.
- Source `token_usage_capture.py` writes versioned observations to protected
  artifacts. `token_usage_query.py` authenticates invocation/outcome/source files
  on each query. `dashboard/adapters/token_usage_read.py` shares this collector.
- Phase 29 now supplies cohort/direct token charts, configured-model filters,
  categories/flows and API 1.13, so database independence must cover all of them.
- Existing recovery lineage and phase-attempt membership are in SQLite; artifact
  authority is retained for operational recovery/content, not numeric reads after
  verified import. Plan explicitly distinguishes import-time verification.
- Phase 25.7 retention and Phase 25.8 exact-message no-progress policy are accepted
  prerequisites to preserve. No changes to their policy are authorized.
- Installed scheduler supports named manifest v4; no active runs were reported.
- `rg` is unavailable on Beelink; read-only Python/grep served as search fallback.
  Node/npm are also unavailable. Python dev tooling exists in `.venv`.

## Authored artifacts and contracts

Overview plus three plans, three concise prompts and named v4 manifest under
`archive/implementation-history/plans/phase-30*` and `prompt_phase-30*`.
Twelve IDs: S-01–S-04, B-01–B-04 and A-01–A-04. Each links implementation to
production caller acceptance; each subphase contains the twelve required sections.
No full pytest run, wrapper or equivalent split run is permitted, including 30.3;
that gate stays separately pending. Final acceptance instead mandates focused
single-file backup/restore, both providers/roles, coverage, model/category/flow,
project/state/round/time and all supported statistical scope equivalence.

Key design decisions are explicit: additive typed SQLite facts/observations and
provenance; no source text or secrets; bounded resumable check/apply job; per-attempt
idempotent revision-checked commits; imports retain unknowns and diagnose conflicts;
readers never perform implicit writes/backfill. Readiness distinguishes import
materialization from provider coverage and works without sources after import.
No artifact deletion/backup-service switch is implemented or performed.

## Executed checks on Beelink

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/unit/dashboard/test_token_usage.py
```

**20 passed in 0.74s**, exit 0. This is baseline prerequisite evidence, not future
Phase 30 implementation acceptance.

```bash
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase30-planning-site
```

**Passed**, exit 0, 0.62s. Existing Material/MkDocs notice and unlisted incident
pages were informational. Archive relative links are checked separately.

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Both global baseline checks **failed**, exit 1, in unchanged frontend helper files:
- lint: I001 unsorted imports in `dashboard-web/e2e/fixture_server.py`;
- format: `dashboard-web/e2e/token_fixture.py` would be reformatted;
  430 files already formatted.
No product/test code was changed to fix unrelated baseline issues. Subphases
require lint/format of their changed maintained files and report baseline failures
separately.

Static plan check **passed** with current domain parser: manifest v4, three
explicit selected implementer/reviewer bindings and bounded names; all four plans
have twelve sections, all twelve IDs have acceptance rows, eleven relative
Markdown links resolve, nine existing selected regression file paths exist,
and all three prompts point to the matching plan and prohibit the full suite.
All new text is checked for whitespace before preparation.

Installed Codex model probe reports `compatible` for `gpt-6.1-sol` / `xhigh`.
It queried the public model catalog, not inference. Final preparation uses the
installed driver and freezes resolved inputs, never a source-installed WIP runtime.

## Remaining boundaries

Planning files are not committed/staged/pushed by this request. The three prompts
exist on Beelink but match the repository's general prompt ignore rule; include
those exact files explicitly in a later authorized planning commit. Prepared
sequence inputs are frozen independently in protected artifacts. Before a future
start, review/commit the planning artifacts under the repository's normal Git
approval policy so clean admission can pass; do not bypass admission.

The requested preparation does not authorize sequence start or non-final
checkpoint commits yet. It also does not authorize live production backfill,
artifact cleanup, installation, backup-policy edits or global pytest. Those later
operator actions remain separate from implementation/test acceptance. No plan or
source changes were written to NucBox; all deliverables are on Beelink.

## Scheduler preparation receipt

Installed Beelink `scheduler sequence prepare` completed successfully.
Exact sequence ID: `ai-dev-loop-seq-34a21c365f4f`; state `prepared`; three entries;
`reused_existing=false`. A fresh read-only status confirms it remains prepared
and has no materialized current run. No start/tick was issued.
Receipt: `/home/rodrigo/.local/state/ai_dev_loop/operator-receipts/phase-30-sequence-prepared.json`.

Final scope: four Markdown plans, three concise prompts, one manifest and this
planning finding. No existing tracked file or index entry was changed; planning
files remain uncommitted and prompts follow the existing ignore convention.
