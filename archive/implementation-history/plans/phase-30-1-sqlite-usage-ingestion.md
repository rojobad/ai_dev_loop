# Phase 30.1 — SQLite usage facts and authenticated ingestion

## Goals

Persist complete compact statistical facts for new Cursor SDK and both Codex
roles. After a finished attempt's statistics are materialized, removing external
artifacts cannot destroy its counters, attribution or coverage. Keep existing
public readers operational until 30.2 changes their source.

## Non-Goals

Historical bulk import, final query switch, production deployment/backfill,
new UI or changes to review/recovery decisions.

## Scope

Additive versioned SQLite schema, typed store/validation ports, per-attempt facts,
per-contribution observations and idempotent ingestion/reconciliation hooks;
focused contracts, affected source docs/rules and legacy schema tests.

## Out of Scope

Live ledger/artifacts, root YAML, provider/model policy, retention/disposal,
backup services, timer changes, planning/review skills and unrelated refactors.

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


Inspect `attempt_service.py:_persist_attempt_completion`, Cursor/Codex workflow
outcome authentication and `mark_attempt_ingested` branches, abort/stale-result
paths, `cursor_sdk_reconciliation.py`, SDK native journal/successor/no-progress,
`token_usage_capture.py` and current artifact-backed query authentication. Current
worker files are untrusted until their attempt/fence/hash bindings authenticate.

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

Follow all overview payload/authority constraints. Create typed versioned models
and JSON schemas for the persisted statistics payloads and matching SQLite
migration/verification/read compatibility. Suggested tables are
`scheduler_usage_attempt_facts` and `scheduler_usage_observations`, linked to
real scheduler attempts/runs with unique logical observation IDs and appropriate
indexes; exact final names and next free migration are recorded in findings.
Keep safe provider/role/model indexes plus validated canonical payload/digest;
no raw files or source text. Do not change hashes of existing snapshots/events.

Record a terminal capture classification even for no-counter and failed/cancelled
inference. Safe unavailable facts may be derived from validated ledger attempts
when provider evidence is insufficient; never label them as verified counters.
Represent pending statistics reconciliation separately from provider unknowns.
No new metrics failure may schedule inference or turn accepted review into rejection.

## Implementation Plan

**S-01 — Typed schema and transactional repository.** Add tables/migration,
validation/serialization, source provenance and indices. Empty migration performs
no artifact walk and no inference. Test rollback on migration/write failure and
old readonly schema support. Preserve no-secret/strict counters and attribution.
A conflicting same-key payload yields a bounded conflict diagnostic and retains
previous known data. Only verified-compatible monotonic enrichment (e.g. partial
capture becoming complete or null optional counters filled without disagreement)
may upgrade facts; preserve its recorded provenance.

**S-02 — Production ingestion for every role.** After existing outcome evidence
authentication, persist attempt facts and contributions for Cursor initial/fix,
Codex implementation/fix and reviewer. Hook common ingestion/publication callers,
not merely worker output. Cover timeout, quota, generic failure and abort. Before
a successful ingestion checkpoint is considered statistically settled, commit the
facts or a durable stats-only reconciliation obligation. Include current fenced
attempt revision/digest so replay and an obsolete worker cannot corrupt attribution.
Do not let late aborted output revive workflow or acquire dispatch authority.

**S-03 — Crash/replay and SDK recovered evidence.** Add bounded stats-only
reconciliation through existing owned scheduler reconciliation, without fresh
prompts. Crash after publication/before database commit and after commit/before
acknowledgement must converge to exactly one contribution. A reconciliation
publisher inherits the original logical attempt, not a second inferred turn.
Persist terminal coverage/diagnostics before marking its stats obligation settled.
Prove parent restart and lost acknowledgement, multiple Codex completed turns,
SDK no-progress successor/recovered-result paths and normal retry ancestry.
If a source disappears before an unsettled write, retain a visible pending/debt
classification; no fabricated complete totals or automatic agent resend.

**S-04 — Scope isolation and missingness.** Ingestion changes only new stats rows
and required stats-only bookkeeping, not prompts/review outcomes/ceilings/identity/
Git. Preserve inference denominators, unknown model/category semantics and zero.
Run the normal implementation/correction/review/recovery flows through production
callers with fake providers and assert exact independent facts stored in SQLite.
Write affected docs/rules describing import-time trust and the temporary reader
boundary; don't claim the historical query switch or readiness gate yet.

Boundary: owned completed/cancelled attempt + authenticated source -> classified
facts/contributions -> atomic idempotent SQLite commit -> stats settled. A crash
leaves replayable obligation/uniqueness, never a second prompt. Abort still fences
operational adoption; purely observational accounting grants no Git/state authority.

## Testing Criteria

Add `tests/unit/scheduler/test_phase30_1_usage_store.py` and
`tests/integration/test_phase30_1_usage_ingestion.py`. Use temp SQLite/roots,
fake Cursor/Codex/backend, synthetic keys and deterministic barriers at the commit
boundary. Expected counters are independent constants, not values recomputed by
the normalizer under test. Inspect new-schema rows after source artifacts become
inaccessible through the store API; full public-read independence is owned by 30.2.

| ID | Acceptance through production callers |
| --- | --- |
| S-01 | v20 migration, current schema validation, rollback and readonly pre-30 load preserve immutable state; payload null/type/overflow/conflict contracts hold. |
| S-02 | Initial/correction/review for both providers persist exact attribution/counters once; failed/cancelled inference remains measurable/unknown and preflight is not fabricated consumption. |
| S-03 | Injected crashes/replays, SDK reconciliation/no-progress and duplicate workers cannot duplicate logical consumption; eventual stats convergence does not resend prompts. |
| S-04 | Independent assertions show operational decisions, reservation/fence/budget/Git unchanged; configured model and all coverage facts are retained without raw text. |

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase30_1_usage_store.py tests/integration/test_phase30_1_usage_ingestion.py tests/unit/scheduler/test_schema.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/scheduler/test_phase25_8_sdk_no_progress.py
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

Record S-01–S-04, final migration/table/schema contracts, crash evidence and exact
results in `archive/implementation-history/findings/phase-30-1-usage-ingestion.md`.

## Risks Or Recovery Notes

Metrics cannot bypass source authentication to get complete counts. Don't hide
unsettled metric writes as provider unknowns. The live driver stays installed and
independent; this checkpoint does not authorize migration of production history.

## OpenQuestions

None. Missing accepted prerequisites must be reported before dependent work.
