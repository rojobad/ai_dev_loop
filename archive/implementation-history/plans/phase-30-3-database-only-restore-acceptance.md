# Phase 30.3 — Single-database backup/restore acceptance

## Goals

Prove that a consistent backup containing only `engine.sqlite3` preserves every
recorded numeric statistic and current statistical views. Deliver an exact operator
procedure for install, historical import, readiness and backup verification, without
performing those live operations automatically.

## Non-Goals

Repository-wide pytest, real history migration, storage cleanup, backup service
installation, UI redesign, billing or resuming agents from a database-only restore.

## Scope

Isolated backup/restore acceptance through installed public callers, adversarial
artifact absence/source changes, both-provider/lineage/retry regression coverage,
package compatibility and current operation/privacy/data documentation.

## Out of Scope

Live ledger/artifacts/credentials, provider calls, installation into user runtime,
timer/retention/restic changes, deletion, YAML/skills edits and Git publication.

## Required Context

This work is authored/prepared/executed on **Beelink-SER5** only, checkout
`/home/rodrigo/project/ai_dev_loop`. Baseline is `1a97becaff1af8c54f892f42740f040d4cd7478c`. Phase 29 token analytics,
Phase 25.7 retention and Phase 25.8 no-progress recovery are implemented. Inspect
accepted source/tests, not older Phase 25 assumptions. SQLite is currently v20;
API is 1.13. Recheck version allocation before editing.

Read [the Phase 30 overview](phase-30-sqlite-token-statistics.md). Preserve the
independent installed driver and frozen source config throughout this sequence.
Never install editable/changing source into that driver. Temporary Linux homes,
SQLite and fake providers are required for tests. Do not execute live backfill,
provider inference, runtime installation, cleanup, timer/backup-service changes,
push/PR/merge or unrelated Git actions as implementation side effects.


Verify accepted S-01–S-04 and B-01–B-04 in code/findings, not just their reported
counts. Read `docs/operacion/estado-artefactos.md`, `seguridad-privacidad.md`,
`observabilidad.md`, `docs/referencia/{cli,integration-api,dashboard,dashboard-ux}.md`,
current installation/cutover guidance, hatch packaging and available Python/Node
tooling. Baseline package API/schema versions are not release-version proof.

## Cursor Rules And Skills
- `AGENTS.md`.
- `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-docs-acceptance-contracts.mdc`,
  `ai-dev-loop-global-integrations-contracts.mdc`, `ai-dev-loop-dashboard-contracts.mdc`,
  `ai-dev-loop-dashboard-refinements-contracts.mdc` (all paths under `.cursor/rules/`).
- `.agents/skills/create-cursor-plan/SKILL.md` and
  `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`.
- Preserve `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; reviewer B
  follows its separate contract. No `.cursor/skills/` directory exists.

Read `docs/referencia/dashboard-ux.md` and `docs/referencia/dashboard.md`.
Affected DUX-06/09: bounded coherent read-only scopes and shared data queries.
Preserve DUX-01–08 presentation, labels, model filtering, dates and layout. No UI
redesign or new controls are needed. All governance applies to either implementer.

## Architecture Guardrails

A backup is taken via SQLite backup API (or a proven compatible SQLite snapshot
mechanism), not copying a live main file without its WAL contents. The restore
has one DB and zero artifacts/stores/credentials/source repos/provider CLIs.
Normal reads cannot manufacture files, migrate tables or change durable state.
Existing runtime/session/content recovery still needs its original evidence.
Do not claim that SQLite checksums prove external artifact authenticity on restore.

## Implementation Plan

**A-01 — Restored statistical equivalence.** Build temp fixture with multiple
projects, standalone/named sequences, phases/replaced ancestors, Cursor SDK and
Codex implementer/reviewer, multiple configured models (including null), initial/
correction/review, retries, failures, cancellation, partial/unknown/zero, skipped
preflight and historical missing sources. Materialize through normal ingestion
and actual backfill CLI. Compute independent fixed expected totals and counts
before backup; include input/output/cache/reasoning categories without double sums.
Backup with a live-WAL committed transaction, then restore only the produced
SQLite file into a second fresh HOME and process. Original roots are nonexistent
or deliberately inaccessible; provider commands are absent. Compare public
run/direct/recovery, sequence, Integration inspect and Dashboard token views
including configured-model choices, flows, coverage and diagnostics.

**A-02 — Preserve all non-token statistics and safety.** Compare project/run/
sequence counts, short names, states, review-round counts, observed process time,
calendar buckets/date zones, timelines and current cohort filters across both
copies. Only non-semantic observation timestamps/file-availability fields may
change. Missing texts are typed not-recorded, never a failure of numeric surfaces.
A text read cannot auto-reconstruct from a provider. Assert database-only restores
with and without an imported unknown/missing-evidence attempt, and pending/import
readiness classifications, so the proof never drops difficult rows. Read-only
operations leave DB authority/digests/schema unchanged. Stored counts still work
when source files change after verified import; explicit re-import reports conflict.

**A-03 — Installed distribution and regressions.** Build current wheel/sdist using
normal hatch source/asset provenance. In isolated Python environments (including
project minimum 3.11 when available), install the base wheel without SDK and run
restored read commands and shared Dashboard HTTP fixture without providers/keys.
Verify current DTO/schema consumers and live-code-independent import origin.
Keep the Phase 25.8 exact-message same-agent no-progress policy, ownership, delays,
abort fences, ready hashes and output-install dispatch barrier unaffected. Add
focused regression coverage for changed ingestion/retention interactions; metrics
persistence must not prevent legal existing cleanup or relax execution evidence.
If frontend code has not changed, no Node installation/browser rewrite is needed.

**A-04 — Backup/import runbook and completion.** Add maintained instructions for
1) accepting/installing the final package during an authorized maintenance window;
2) consistent initial backup while retaining all artifacts;
3) `usage-backfill --check` then explicit bounded apply/resume with supported help;
4) comparing per-project/run/sequence/dashboard totals and readiness;
5) backing up just SQLite and verifying a restore with no external roots;
6) recording unknowns/conflicts and retaining original evidence until resolved.
These commands are documentation, not permission to run on production now.
Do not change a restic job, prune files or call cleanup. Explain that current
model/role/stage metrics are now DB-authoritative while detailed prompts/patches/
reviews and session recovery are not preserved by a DB-only policy. Note storage
size depends on facts, not promise a fixed 11 MB or compression ratio.
Produce exact final contract mapping and the pending separate global-test gate.

Boundary: accepted materialization -> consistent SQLite snapshot -> separate
one-file restore -> read-only equivalence -> operator evidence. A partially imported
snapshot is visibly partial; source-loss cannot downgrade already imported known
facts. No restore verification grants execution/Git/deletion authority.

## Testing Criteria

Add `tests/integration/test_phase30_3_database_only_restore.py` and
`tests/integration/test_phase30_3_installed_statistics.py`. Tests must use real
CLI/store/query/backup/HTTP paths and separate processes, independent constants,
temp database/artifacts and fake models, with no live Beelink history. Include
WAL snapshot correctness and negative assertions that artifact/runtime openers
are never used. Preserve optional content availability and skipped check honesty.

| ID | Required gate evidence |
| --- | --- |
| A-01 | One-file restore preserves independent exact token totals, rows/categories/models/roles/stages, missingness and all scopes with no external roots. |
| A-02 | Project/run/sequence/name/state/round/time/filter statistics match; queries do not mutate or require content; difficult unknown/pending rows stay visible. |
| A-03 | Isolated installed base wheel reads restored stats without SDK/provider/auth; affected schema/DTO/no-progress/ingestion regression selections pass. |
| A-04 | Maintained runbook/help agree; all S/B/A IDs map to evidence; live installation/import/deletion and full pytest are honestly pending. |

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase30_3_database_only_restore.py tests/integration/test_phase30_3_installed_statistics.py tests/integration/test_phase30_2_database_statistics.py tests/integration/test_phase29_1_token_queries.py tests/unit/dashboard/test_token_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase25_8_sdk_no_progress.py
```

Use explicit test paths/cases only. **DO NOT RUN THE REPOSITORY-WIDE SUITE**, its
wrappers or an equivalent full-suite run split into batches. This operator
constraint applies to implementation, corrections and review, including 30.3.
The global pipeline gate remains separately pending, without waiving any mandatory
phase coverage. After a fix rerun failed/affected tests; broaden only for a
specific remaining risk. Record executed, failed, skipped and unexecuted checks.

Run Ruff lint/format on changed maintained files, `python -m mypy src/ai_dev_loop`,
strict MkDocs for changed docs, and `git diff --check` with the available isolated
`.venv/bin/python`/uv tooling. No new tests solely mirroring documentation wording.
Existing Node/npm is absent; no new frontend behavior is authorized. If DTO or
frontend source must change, preserve existing build provenance and record any
unavailable focused frontend checks rather than install tools or weaken tests.

Run `python -m build --outdir <owned-temp>` and isolated installed tests when
required by A-03. No full pytest run belongs to this phase. Write
`archive/implementation-history/findings/phase-30-database-statistics-acceptance.md`
with exact results, source/import/readiness limits and later operator actions.

## Risks Or Recovery Notes

The full suite remains separately pending by explicit user instruction, not a
reason to omit mandatory focused gates. Retain existing artifact backups until
production import and isolated restore are accepted. Installation and a live
backfill require a later operator action; their absence here is not missing code.

## OpenQuestions

None for implementation. Report prerequisite failures without inventing sources,
claiming nonexistent historical counters or authorizing deletion.
